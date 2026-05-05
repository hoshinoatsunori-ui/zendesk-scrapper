---
title: "Python + Playwright + Flask でZendeskチケットをオフライン全文検索する"
tags:
  - Python
  - Playwright
  - Flask
  - SQLite
  - Zendesk
private: false
updated_at: ''
id: null
organization_url_name: null
slide: false
ignorePublish: false
---

## 概要

Zendeskのサポートチケットをローカルに保存し、オフラインで全文検索・閲覧できるシステムを作りました。

**解決したかった課題**
- オフライン環境でも過去チケットを検索したい
- 添付ファイルをローカルにまとめて保管しておきたい
- ブラウザを開かずに素早く過去のやり取りを参照したい

**GitHub**: https://github.com/hoshinoatsunori-ui/zendesk-scrapper

## 構成と技術スタック

```
scraper.py    … Playwright でチケット自動取得
webapp.py     … Flask 製の検索Webアプリ
reindex.py    … FTSインデックス再構築・データ修正
templates/    … Jinja2テンプレート
```

| 役割 | 技術 |
|------|------|
| ブラウザ自動操作 | Playwright (Python) |
| HTTPダウンロード | httpx |
| Webアプリ | Flask |
| 全文検索 | SQLite FTS5 trigramトークナイザ |
| Markdownレンダリング | markdown |

## セットアップ

```powershell
# リポジトリをクローン
git clone https://github.com/hoshinoatsunori-ui/zendesk-scrapper.git
cd zendesk-scrapper

# 依存パッケージ導入
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m playwright install chromium
```

## スクレイパー実装のポイント

### 1. ログインセッション永続化

`launch_persistent_context` でブラウザプロファイルをディスクに保存。初回ログイン後は2回目以降が不要になります。

```python
context = await pw.chromium.launch_persistent_context(
    str(PROFILE_DIR.resolve()),
    headless=False,
    locale="ja-JP",
)
```

ログインが必要かどうかはURLで判定:

```python
if "sign_in" in page.url or "/auth" in page.url:
    print("ログインしてください...")
    await page.wait_for_url("**/hc/**", timeout=300_000)
```

### 2. 差分更新

チケット一覧から取得した更新日時とDB保存値を比較し、変更があったものだけダウンロードします。

```python
row = conn.execute(
    "SELECT zd_updated FROM tickets WHERE ticket_id=?", (tid,)
).fetchone()
if row and row[0] and row[0] == zd_updated:
    skipped += 1
    continue  # 変更なし → スキップ
```

### 3. ステータス判定はCSSクラスから行う

このZendeskテーマではステータスラベルのテキストに担当チーム名が入っており、innerTextでは正しいステータスが取れませんでした。

```html
<!-- オープンチケットのHTML -->
<span class="status-label status-label-open" title="スタッフによる対応中">
  MACNICA  ← チーム名！ステータスではない
</span>
```

CSSクラス名 `status-label-{key}` からステータスを判定することで解決:

```python
_STATUS_CLASS_MAP = {
    "solved":   "解決済み",
    "open":     "オープン",
    "answered": "回答済み",
    "pending":  "保留中",
    "new":      "新規",
    "closed":   "クローズ",
}

def _class_to_status(class_attr: str) -> str:
    m = re.search(r'status-label-((?!request)\w[\w-]*)', class_attr)
    return _STATUS_CLASS_MAP.get(m.group(1), m.group(1)) if m else ""
```

### 4. 更新日時の誤取得を防ぐ

日付として使える値かどうかをJavaScript側でバリデーション:

```javascript
// YYYY-MM または HH:MM パターンを必須とする
const isDateLike = s => /\d{4}[-\/]\d{2}|\d{2}:\d{2}/.test(s);
```

ステータス文字列（"解決済み以下" 等）が誤って日時フィールドに入るのを防ぎます。

### 5. アクセス不可チケットの検知

存在しないチケットIDにアクセスするとリクエスト一覧にリダイレクトされます。これを検知してスキップ:

```python
has_details = await page.query_selector(".request-details")
h1_el = await page.query_selector("h1")
h1_text = (await h1_el.inner_text()).strip() if h1_el else ""
if not has_details and h1_text in ("リクエスト", "Requests"):
    raise RuntimeError(f"ticket {ticket_id} redirected to list page")
```

## Webアプリ実装のポイント

### 日本語全文検索（SQLite FTS5 trigram）

SQLite 3.34+ の trigram トークナイザを使うと、日本語を含む任意の部分文字列検索が可能になります。古いSQLiteへの自動フォールバックも実装:

```python
def _detect_fts_tokenizer(conn):
    try:
        conn.execute("CREATE VIRTUAL TABLE _probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE _probe")
        return "trigram"
    except sqlite3.OperationalError:
        return "unicode61"
```

FTSクエリはエスケープが必要なため、特殊文字を除去してからクエリ:

```python
fts_query = re.sub(r'["\'\(\)\[\]\{\}\^~\*\?\\]', ' ', q).strip()
```

### ステータスフィルタ（動的生成）

DBのdistinct値からフィルタボタンを動的生成するため、新しいステータスが登場しても自動対応:

```python
all_statuses = [
    r[0] for r in conn.execute(
        "SELECT DISTINCT status FROM tickets WHERE status != '' ORDER BY status"
    ).fetchall()
]
```

Jinja2テンプレート側:

```html
{% for s in all_statuses %}
<a href="/?q={{ q }}&tab={{ tab }}&status={{ s | urlencode }}"
   class="tab-btn {% if status_filter == s %}active{% endif %}">
  {{ s }}
</a>
{% endfor %}
```

### summary.md の自動表示

各チケットフォルダに `summary.md` を置いておくと、チケット詳細画面に自動でサマリーを表示します。AIで要約を生成したものを保存しておくと便利です。

```python
summary_path = TICKETS_DIR / ticket_id / "summary.md"
if summary_path.exists():
    summary_html = markdown.markdown(
        summary_path.read_text(encoding="utf-8")
    )
```

### 日付バリデーションフィルタ（Jinja2カスタムフィルタ）

DBに誤った値が入っていても表示側で `—` に置き換えるカスタムフィルタ:

```python
_DATE_RE = re.compile(r'\d{4}[-/]\d{2}|\d{2}:\d{2}')

@app.template_filter("clean_date")
def clean_date_filter(val):
    if val and _DATE_RE.search(str(val)):
        return val
    return "—"
```

テンプレートでは `{{ t.zd_updated | clean_date }}` のように使用。

## データ修復ツール（reindex.py）

保存済みHTMLを読み直してDBのデータを修正し、全文検索インデックスを再構築します。

**修正内容**
- ステータスがチーム名になっているレコードをCSSクラスから修正
- 更新日時が日付でないレコードを `datetime` 属性から抽出して修正

```python
def _extract_status_from_html(html_path: Path) -> str:
    html = html_path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r'class="([^"]*status-label[^"]*)"', html)
    if m:
        m2 = re.search(r'status-label-((?!request)\w[\w-]*)', m.group(1))
        if m2:
            return _STATUS_CLASS_MAP.get(m2.group(1), m2.group(1))
    return ""
```

```bash
python reindex.py
```

## 使い方まとめ

```powershell
# チケット取得（初回はブラウザでZendeskにログイン）
.\.venv\Scripts\python.exe scraper.py

# 特定チケットのみ強制再取得
.\.venv\Scripts\python.exe scraper.py -t 3217 3364

# 検索Webアプリ起動 → http://127.0.0.1:5000
.\.venv\Scripts\python.exe webapp.py

# データ修復が必要な場合
.\.venv\Scripts\python.exe reindex.py
```

## まとめ

Playwright の永続セッション + SQLite FTS5 trigramの組み合わせが、ログイン必須サービスのオフライン検索システムを作るのに非常に相性が良かったです。

Zendesk固有の癖（ステータスラベルのテキストがチーム名になる）のような問題は、CSSクラスを読む方針に切り替えることで安定して解決できました。同様の問題が出たときはinnerTextではなくクラス属性を確認してみてください。
