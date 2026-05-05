---
title: "Zendeskのサポートチケットをオフライン全文検索できるようにした話"
emoji: "🔍"
type: "tech"
topics: ["python", "playwright", "flask", "sqlite", "zendesk"]
published: false
---

## はじめに

仕事でお客さんとZendeskでやり取りをしているのですが、過去チケットを探すのが大変でした。

- Zendeskの検索はそこそこ使えるけど、**オフラインで使えない**
- 添付ファイルを一括で手元に置いておきたい
- 「あの件名なんだっけ」を毎回ブラウザで調べるのが面倒

そこで Python + Playwright + Flask + SQLite で**ローカル動作のチケット全文検索システム**を作りました。

GitHub: https://github.com/hoshinoatsunori-ui/zendesk-scrapper

## 構成

```
scraper.py    … Playwright でチケットを自動取得
webapp.py     … Flask製の検索Webアプリ
reindex.py    … FTSインデックス再構築・データ修正
templates/    … Jinja2テンプレート
data/         … ローカルデータ置き場（gitignore済み）
```

## 技術スタック

| 役割 | 使用技術 |
|------|---------|
| ブラウザ自動操作 | Playwright (Python) |
| HTTPダウンロード | httpx |
| Webアプリ | Flask |
| 全文検索 | SQLite FTS5 (trigramトークナイザ) |
| Markdownレンダリング | markdown (Python) |

## スクレイパーの仕組み

### ログインセッションの永続化

Playwright の `launch_persistent_context` を使ってブラウザプロファイルをディスクに保存します。初回だけ手動ログインすれば、2回目以降はセッションが再利用されます。

```python
context = await pw.chromium.launch_persistent_context(
    str(PROFILE_DIR.resolve()),
    headless=False,
    locale="ja-JP",
)
```

### 差分更新

チケット一覧ページから各チケットの「更新日時」を取得し、DBに保存済みの値と比較。一致していればスキップ、変わっていれば再ダウンロードします。

```python
if row and row[0] and row[0] == zd_updated:
    print(f"#{tid} スキップ（更新なし）")
    skipped += 1
    continue
```

### ステータス判定の落とし穴

このZendeskテーマ（altimaiot.zendesk.com）では、ステータスラベルの**テキストに担当チーム名**が入っていました。

```html
<!-- オープンチケットのHTML -->
<span class="status-label status-label-request status-label-open"
      title="スタッフによるチケット対応中">
  MACNICA  ← チーム名が入っている！
</span>
```

innerTextではなく**CSSクラス**からステータスを判定することで解決しました。

```python
def _class_to_status(class_attr: str) -> str:
    m = re.search(r'status-label-((?!request)\w[\w-]*)', class_attr)
    return _STATUS_CLASS_MAP.get(m.group(1), m.group(1)) if m else ""

_STATUS_CLASS_MAP = {
    "solved":   "解決済み",
    "open":     "オープン",
    "answered": "回答済み",
    "pending":  "保留中",
    "new":      "新規",
    "closed":   "クローズ",
}
```

### 更新日時の誤取得を防ぐ

日付らしい値（`YYYY-MM` や `HH:MM` パターン）でない文字列は保存しないよう、JavaScript側でバリデーションしています。

```javascript
const isDateLike = s => /\d{4}[-\/]\d{2}|\d{2}:\d{2}/.test(s);
```

### アクセス不可チケットの検知

存在しないチケットIDにアクセスすると Zendesk はリクエスト一覧にリダイレクトします。これを検知してスキップするようにしました。

```python
has_details = await page.query_selector(".request-details")
h1_text = (await h1_el.inner_text()).strip() if h1_el else ""
if not has_details and h1_text in ("リクエスト", "Requests"):
    raise RuntimeError(f"ticket {ticket_id} redirected to list page")
```

## Webアプリの機能

### 日本語全文検索

SQLite FTS5 の trigram トークナイザを使っています。SQLite 3.34 以上が必要ですが、対応していない場合は unicode61 に自動フォールバックします。

```python
def _detect_fts_tokenizer(conn):
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts_probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE _fts_probe")
        return "trigram"
    except sqlite3.OperationalError:
        return "unicode61"
```

### ステータスフィルタ

一覧ページにステータス別のフィルタボタンを追加しています。DBから distinct な値を取得してボタンを動的生成するため、新しいステータスが増えても自動対応します。

```python
all_statuses = [
    r[0] for r in conn.execute(
        "SELECT DISTINCT status FROM tickets WHERE status != '' ORDER BY status"
    ).fetchall()
]
```

### summary.md の自動表示

各チケットディレクトリに `summary.md` が存在する場合、チケット詳細の添付ファイル欄の上にサマリーを表示します。別途AIでサマリーを生成してこのファイルに保存しておくと便利です。

```python
summary_path = ticket_dir / "summary.md"
if summary_path.exists():
    md_text = summary_path.read_text(encoding="utf-8")
    summary_html = markdown.markdown(md_text)
```

## データ修復ツール（reindex.py）

過去に誤ったデータを保存してしまったチケットを、保存済みHTMLから読み直して修正します。

- ステータスが "MACNICA" 等のチーム名になっているものを、HTMLのCSSクラスから正しい値に修正
- 更新日時が日付でない値になっているものを、`datetime` 属性から抽出して修正
- FTS全文検索インデックスを再構築

```bash
python reindex.py
```

## 使い方

```powershell
# セットアップ
.\setup.ps1

# チケット取得（初回はブラウザでログイン）
.\.venv\Scripts\python.exe scraper.py

# 特定チケットのみ強制再取得
.\.venv\Scripts\python.exe scraper.py -t 3217 3364

# 検索アプリ起動
.\.venv\Scripts\python.exe webapp.py
# → http://127.0.0.1:5000 を開く
```

## まとめ

Playwright のセッション永続化とSQLite FTS5 を組み合わせることで、手軽にオフライン検索システムが作れました。Zendeskに限らず、ログインが必要なWebサービスのデータをローカルに落として検索したい場面に応用できると思います。

テーマ固有のHTMLの癖（ステータス判定など）をどう吸収するかが一番の課題でしたが、innerTextではなくCSSクラスを見るようにしてから安定しました。
