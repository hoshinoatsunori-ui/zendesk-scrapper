# ZendeskScrapper

altimaiot.zendesk.com の問い合わせチケットをオフライン保存し、全文検索・閲覧できるWebアプリ。

## 機能

### スクレイパー (`scraper.py`)

- **自分のリクエスト** と **CCに入っているリクエスト** を両方収集
- 複数ページにわたるリクエスト一覧を自動で全ページ取得
- チケット本文・コメント（複数ページ対応）・添付ファイルをローカル保存
- 差分更新：更新日時を比較し、変更のあったチケットのみ再ダウンロード
- ステータスをCSSクラスから正確に取得（`status-label-open` → オープン 等）
- アクセス不可チケット（一覧ページへのリダイレクト）を自動検知してスキップ
- ブラウザセッション永続化（2回目以降はログイン不要）
- 指定チケットのみ強制再取得する `-t` オプション

### Webアプリ (`webapp.py`)

- SQLite FTS5 による日本語全文検索（trigram トークナイザ対応）
- **種別フィルタ**：すべて / 自分のリクエスト / CCに入っているリクエスト
- **ステータスフィルタ**：オープン / 回答済み / 解決済み 等
- 検索ヒット箇所をスニペット表示（キーワードハイライト）
- ページネーション（50件/ページ）
- チケット詳細：ステータスバッジ・更新日時・添付ファイルダウンロード
- 保存済みHTMLをインライン表示（複数コメントページ対応）
- `summary.md` が存在する場合はサマリーをチケット詳細に表示
- 「次回再取得」ボタンで任意のチケットを強制再ダウンロード対象にマーク

### FTS再インデックス (`reindex.py`)

- 保存済みHTMLからFTSインデックスを再構築
- ステータスが壊れているチケットをHTMLのCSSクラスから修正
- 更新日時が日付でない値（スクレイプミス）をHTMLの `datetime` 属性から修正

## セットアップ

Python 3.10 以上が必要。

```powershell
.\setup.ps1
```

手動セットアップ:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m playwright install chromium
```

## 使い方

### 1. チケットをダウンロード

```powershell
.\.venv\Scripts\python.exe scraper.py
```

- 初回はブラウザが開くので Zendesk にログインしてください
- ログイン後、全チケットの収集・ダウンロードが自動で開始されます
- 2回目以降は保存済みセッションを使用（ログイン不要）
- 更新されたチケットのみ再ダウンロード（差分更新）

特定チケットを強制再取得する場合:

```powershell
.\.venv\Scripts\python.exe scraper.py -t 3217 3364
```

### 2. 検索Webアプリを起動

```powershell
.\.venv\Scripts\python.exe webapp.py
```

ブラウザで http://127.0.0.1:5000 を開く。

### 3. FTSインデックス再構築（必要な場合）

DBの検索インデックスが壊れた場合や、ステータス・日付データを修正したい場合:

```powershell
.\.venv\Scripts\python.exe reindex.py
```

## ファイル構成

```
ZendeskScrapper/
├── scraper.py          ダウンロードスクリプト
├── webapp.py           検索Webアプリ (Flask)
├── reindex.py          FTS再インデックス・データ修正スクリプト
├── templates/
│   ├── index.html      検索・一覧画面
│   └── ticket.html     チケット詳細画面
├── requirements.txt
├── setup.ps1
└── data/               ← 実行時に自動生成
    ├── browser_profile/    Playwright ブラウザセッション（要秘匿）
    ├── tickets/
    │   └── {チケット番号}/
    │       ├── ticket.html         保存済みHTML（ページ1）
    │       ├── ticket_page2.html   コメントページ2以降
    │       ├── ticket.json         メタデータ（件名・ステータス・添付情報等）
    │       ├── summary.md          AIサマリー（存在する場合、詳細画面に表示）
    │       └── attachments/        添付ファイル
    └── zendesk.db          SQLite DB（状態管理 + FTS全文検索インデックス）
```

## データベース

`data/zendesk.db` に以下を保持:

| テーブル | 用途 |
|----------|------|
| `tickets` | チケットのメタデータ・ステータス・更新日時・ダウンロード日時 |
| `tickets_fts` | FTS5 全文検索インデックス（件名 + 本文） |

SQLite 3.34 以上では trigram トークナイザを使用（部分一致・日本語検索に有効）。それ以前は unicode61 にフォールバック。

## ステータスの判定

このZendeskテーマではステータスラベルのテキストに担当チーム名が入るため、CSSクラス名からステータスを判定しています:

| CSSクラス | 表示 |
|-----------|------|
| `status-label-solved` | 解決済み |
| `status-label-open` | オープン |
| `status-label-answered` | 回答済み |
| `status-label-pending` | 保留中 |
| `status-label-new` | 新規 |
| `status-label-closed` | クローズ |

## 注意事項

- `data/browser_profile/` にはログインセッションが保存されます。共有・コミットしないでください
- アクセスできないチケット（削除済み・権限なし）は一覧で「⚠ 取得失敗」と表示されます
- 添付ファイルのURLはセッション有効中のみ有効なため、`scraper.py` 実行中にダウンロードします
