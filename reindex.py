"""
FTS インデックス再構築スクリプト

保存済みの ticket.html からテキストを抽出し、全文検索インデックスを再構築します。
scraper.py 実行後に検索できない場合や、DB の FTS テーブルを修正した後に一度だけ実行してください。

Usage:
    python reindex.py
"""

import json
import re
import sqlite3
from html.parser import HTMLParser
from pathlib import Path

DATA_DIR    = Path("data")
TICKETS_DIR = DATA_DIR / "tickets"
DB_PATH     = DATA_DIR / "zendesk.db"


# ---------------------------------------------------------------------------
# HTML → plain text
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript", "head"}

    def __init__(self):
        super().__init__()
        self._depth = 0   # depth inside a skip tag
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self._SKIP_TAGS:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in self._SKIP_TAGS and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if not self._depth:
            text = data.strip()
            if text:
                self._parts.append(text)

    def get_text(self) -> str:
        return " ".join(self._parts)


_STATUS_CLASS_MAP = {
    "solved":   "解決済み",
    "open":     "オープン",
    "pending":  "保留中",
    "new":      "新規",
    "closed":   "クローズ",
    "on-hold":  "保留中",
    "answered": "回答済み",
}
_VALID_STATUSES = set(_STATUS_CLASS_MAP.values()) | {""}  # empty is also acceptable


def _extract_status_from_html(html_path: Path) -> str:
    """Derive status from status-label CSS class in saved ticket HTML."""
    try:
        html = html_path.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r'class="([^"]*status-label[^"]*)"', html)
        if m:
            m2 = re.search(r'status-label-((?!request)\w[\w-]*)', m.group(1))
            if m2:
                return _STATUS_CLASS_MAP.get(m2.group(1), m2.group(1))
    except Exception:
        pass
    return ""


def _extract_updated_from_html(html_path: Path) -> str:
    """Return the last datetime attribute found in the saved ticket HTML."""
    try:
        html = html_path.read_text(encoding="utf-8", errors="ignore")
        # Collect all datetime="" values; the last one in main content is updated_at
        datetimes = re.findall(r'datetime="([^"]+)"', html)
        # Filter out obviously wrong values (nav timestamps outside the ticket)
        # Take the last match which is typically the most recent activity
        for dt in reversed(datetimes):
            if re.match(r'\d{4}-\d{2}-\d{2}', dt):
                return dt
    except Exception:
        pass
    return ""


def html_to_text(html_path: Path) -> str:
    try:
        html = html_path.read_text(encoding="utf-8", errors="ignore")
        parser = _TextExtractor()
        parser.feed(html)
        return parser.get_text()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# FTS helpers (same logic as scraper.py)
# ---------------------------------------------------------------------------

def _detect_tokenizer(conn: sqlite3.Connection) -> str:
    try:
        conn.execute("CREATE VIRTUAL TABLE _probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE _probe")
        return "trigram"
    except sqlite3.OperationalError:
        return "unicode61"


def _recreate_fts(conn: sqlite3.Connection, tokenizer: str) -> None:
    conn.execute("DROP TABLE IF EXISTS tickets_fts")
    conn.execute(f"""
        CREATE VIRTUAL TABLE tickets_fts USING fts5(
            ticket_id UNINDEXED,
            subject,
            body,
            tokenize='{tokenizer}'
        )
    """)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not DB_PATH.exists():
        print("[!] データベースが見つかりません。先に scraper.py を実行してください。")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    tokenizer = _detect_tokenizer(conn)
    print(f"[i] FTSトークナイザー: {tokenizer}")

    print("[i] FTSテーブルを再作成中...")
    _recreate_fts(conn, tokenizer)

    ticket_dirs = sorted(
        (d for d in TICKETS_DIR.iterdir() if d.is_dir()),
        key=lambda d: d.name,
    ) if TICKETS_DIR.exists() else []

    total   = len(ticket_dirs)
    indexed = 0
    errors  = 0

    print(f"[i] {total} 件のチケットをインデックス化中...\n")

    for i, ticket_dir in enumerate(ticket_dirs, 1):
        ticket_id = ticket_dir.name
        json_path = ticket_dir / "ticket.json"
        html_path = ticket_dir / "ticket.html"

        if not json_path.exists():
            continue

        try:
            meta    = json.loads(json_path.read_text(encoding="utf-8"))
            subject = meta.get("subject", f"Request {ticket_id}")

            # Concatenate text from all saved comment pages
            parts = [html_to_text(html_path)] if html_path.exists() else [subject]
            for pg in range(2, 51):
                extra_html = ticket_dir / f"ticket_page{pg}.html"
                if not extra_html.exists():
                    break
                parts.append(html_to_text(extra_html))
            body = " ".join(parts)

            conn.execute(
                "INSERT INTO tickets_fts (ticket_id, subject, body) VALUES (?, ?, ?)",
                (ticket_id, subject, body),
            )

            db_row = conn.execute(
                "SELECT status, zd_updated FROM tickets WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
            current_status = db_row[0] if db_row else ""
            current_zd     = db_row[1] if db_row else ""

            # Fix status when it contains a team name instead of a real status value
            if current_status not in _VALID_STATUSES and html_path.exists():
                fixed_status = _extract_status_from_html(html_path)
                if fixed_status:
                    conn.execute(
                        "UPDATE tickets SET status=? WHERE ticket_id=?",
                        (fixed_status, ticket_id),
                    )

            # Fix zd_updated when it is missing OR not a date (e.g. "MACNICA")
            needs_zd_fix = not current_zd or not re.search(r'\d{4}[-/]\d{2}|\d{2}:\d{2}', current_zd)
            if needs_zd_fix and html_path.exists():
                extracted = _extract_updated_from_html(html_path)
                if extracted:
                    conn.execute(
                        "UPDATE tickets SET zd_updated=? WHERE ticket_id=?",
                        (extracted, ticket_id),
                    )

            indexed += 1
            print(f"  [{i:>4}/{total}] #{ticket_id}: {subject[:60]}")

        except Exception as e:
            errors += 1
            print(f"  [{i:>4}/{total}] #{ticket_id}: エラー — {e}")

    conn.commit()
    conn.close()

    print(f"\n=== 完了 ===")
    print(f"  インデックス化: {indexed} 件")
    print(f"  エラー:         {errors} 件")
    print("\nWebアプリを再起動して検索を試してください。")


if __name__ == "__main__":
    main()
