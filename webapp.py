"""
Zendesk offline viewer — full-text search web app.

Usage:
    python webapp.py
Then open http://127.0.0.1:5000
"""

import json
import re
import sqlite3
from pathlib import Path

import markdown as _md
from flask import Flask, abort, redirect, render_template, request, send_file, url_for

DATA_DIR = Path("data")
TICKETS_DIR = DATA_DIR / "tickets"
DB_PATH = DATA_DIR / "zendesk.db"

app = Flask(__name__)

_DATE_RE = re.compile(r'\d{4}[-/]\d{2}|\d{2}:\d{2}')


@app.template_filter("clean_date")
def clean_date_filter(val):
    """Return val if it looks like a date/time, else '—'."""
    if val and _DATE_RE.search(str(val)):
        return val
    return "—"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ticket_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    tab = request.args.get("tab", "").strip()
    status_filter = request.args.get("status", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50
    offset = (page - 1) * per_page

    conn = get_db()
    total = ticket_count(conn)

    # Collect distinct valid statuses for filter UI
    all_statuses = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT status FROM tickets WHERE status != '' ORDER BY status"
        ).fetchall()
    ]

    tickets = []
    result_count = 0

    if q:
        # Strip FTS special characters that cause syntax errors
        fts_query = re.sub(r'["\'\(\)\[\]\{\}\^~\*\?\\]', ' ', q).strip()
        # trigram tokenizer requires ≥ 3 chars; fall through to LIKE for shorter queries
        use_fts = len(fts_query.replace(" ", "")) >= 3
        try:
            if not use_fts:
                raise sqlite3.OperationalError("query too short for FTS")
            rows = conn.execute(
                """
                SELECT
                    t.ticket_id, t.subject, t.status, t.tab, t.zd_updated,
                    snippet(tickets_fts, 2, '<mark>', '</mark>', ' … ', 32) AS snip
                FROM tickets_fts
                JOIN tickets t ON t.ticket_id = tickets_fts.ticket_id
                WHERE tickets_fts MATCH ?
                  AND (? = '' OR t.tab = ?)
                  AND (? = '' OR t.status = ?)
                ORDER BY rank
                LIMIT ? OFFSET ?
                """,
                (fts_query, tab, tab, status_filter, status_filter, per_page, offset),
            ).fetchall()
            count_row = conn.execute(
                """
                SELECT COUNT(*)
                FROM tickets_fts
                JOIN tickets t ON t.ticket_id = tickets_fts.ticket_id
                WHERE tickets_fts MATCH ?
                  AND (? = '' OR t.tab = ?)
                  AND (? = '' OR t.status = ?)
                """,
                (fts_query, tab, tab, status_filter, status_filter),
            ).fetchone()
            result_count = count_row[0] if count_row else 0
            tickets = rows
        except sqlite3.OperationalError:
            # FTS syntax error fallback: LIKE search
            like = f"%{q}%"
            rows = conn.execute(
                """
                SELECT ticket_id, subject, status, tab, zd_updated, '' AS snip
                FROM tickets
                WHERE (subject LIKE ? OR ticket_id LIKE ?)
                  AND (? = '' OR tab = ?)
                  AND (? = '' OR status = ?)
                ORDER BY zd_updated DESC
                LIMIT ? OFFSET ?
                """,
                (like, like, tab, tab, status_filter, status_filter, per_page, offset),
            ).fetchall()
            count_row = conn.execute(
                """
                SELECT COUNT(*) FROM tickets
                WHERE (subject LIKE ? OR ticket_id LIKE ?)
                  AND (? = '' OR tab = ?)
                  AND (? = '' OR status = ?)
                """,
                (like, like, tab, tab, status_filter, status_filter),
            ).fetchone()
            result_count = count_row[0] if count_row else 0
            tickets = rows
    else:
        rows = conn.execute(
            """
            SELECT ticket_id, subject, status, tab, zd_updated, '' AS snip
            FROM tickets
            WHERE (? = '' OR tab = ?)
              AND (? = '' OR status = ?)
            ORDER BY zd_updated DESC
            LIMIT ? OFFSET ?
            """,
            (tab, tab, status_filter, status_filter, per_page, offset),
        ).fetchall()
        count_row = conn.execute(
            """
            SELECT COUNT(*) FROM tickets
            WHERE (? = '' OR tab = ?)
              AND (? = '' OR status = ?)
            """,
            (tab, tab, status_filter, status_filter),
        ).fetchone()
        result_count = count_row[0] if count_row else 0
        tickets = rows

    conn.close()

    total_pages = max(1, (result_count + per_page - 1) // per_page)
    return render_template(
        "index.html",
        tickets=tickets,
        q=q,
        tab=tab,
        status_filter=status_filter,
        all_statuses=all_statuses,
        page=page,
        total_pages=total_pages,
        result_count=result_count,
        total=total,
    )


@app.route("/ticket/<ticket_id>")
def ticket_detail(ticket_id: str):
    if not re.match(r"^\d+$", ticket_id):
        abort(404)

    json_path = TICKETS_DIR / ticket_id / "ticket.json"
    if not json_path.exists():
        abort(404)

    meta = json.loads(json_path.read_text(encoding="utf-8"))

    attachments_dir = TICKETS_DIR / ticket_id / "attachments"
    attachments = []
    if attachments_dir.exists():
        for f in sorted(attachments_dir.iterdir()):
            if f.is_file():
                size_kb = f.stat().st_size / 1024
                attachments.append({
                    "name": f.name,
                    "size": f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb/1024:.1f} MB",
                })

    conn = get_db()
    row = conn.execute(
        "SELECT tab, status, zd_updated, dl_updated FROM tickets WHERE ticket_id=?", (ticket_id,)
    ).fetchone()
    conn.close()

    db_info = dict(row) if row else {}

    # Collect saved comment pages (ticket_page2.html, ticket_page3.html, …)
    ticket_dir = TICKETS_DIR / ticket_id
    comment_pages = []
    for page_num in range(2, 51):
        if (ticket_dir / f"ticket_page{page_num}.html").exists():
            comment_pages.append(page_num)
        else:
            break

    # Load summary.md if present
    summary_html = None
    summary_path = ticket_dir / "summary.md"
    if summary_path.exists():
        md_text = summary_path.read_text(encoding="utf-8")
        summary_html = _md.markdown(md_text)

    return render_template(
        "ticket.html",
        meta=meta,
        ticket_id=ticket_id,
        attachments=attachments,
        db_info=db_info,
        comment_pages=comment_pages,
        summary_html=summary_html,
    )


_HIDE_NAV_CSS = """
<style id="__zd-offline-override">
/* Hide Zendesk page chrome; show only ticket body */
header,
.header,
[class~="header"]:not([class*="request"]):not([class*="comment"]):not([class*="ticket"]),
nav,
.nav,
.sub-nav,
[class~="sub-nav"],
.user-nav,
[class~="user-nav"],
.topbar,
[class~="topbar"],
footer,
.footer,
[class~="footer"],
/* Skip-to-main link at very top */
a[href="#main-content"],
a[href="#main"],
.skip-nav,
[class~="skip-nav"],
/* Mobile drawer / overlay */
.mobile-menu,
[class*="mobile-nav"],
[class*="side-menu"],
/* Cookie / GDPR banners */
[id*="cookie"],
[class*="cookie"],
[id*="gdpr"],
[class*="gdpr"] {
    display: none !important;
}

body {
    padding-top: 0 !important;
    margin-top: 0 !important;
}

#main-content,
main,
.main-content {
    padding-top: 16px !important;
    max-width: 100% !important;
}
</style>
"""


@app.route("/ticket/<ticket_id>/html")
@app.route("/ticket/<ticket_id>/html/<int:page_num>")
def ticket_html(ticket_id: str, page_num: int = 1):
    if not re.match(r"^\d+$", ticket_id):
        abort(404)

    filename  = "ticket.html" if page_num == 1 else f"ticket_page{page_num}.html"
    html_path = TICKETS_DIR / ticket_id / filename
    if not html_path.exists():
        abort(404)

    html = html_path.read_text(encoding="utf-8")

    if "</head>" in html:
        html = html.replace("</head>", _HIDE_NAV_CSS + "</head>", 1)
    else:
        html = _HIDE_NAV_CSS + html

    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/ticket/<ticket_id>/reset", methods=["POST"])
def ticket_reset(ticket_id: str):
    """Clear zd_updated so the ticket is re-downloaded on the next scraper run."""
    if not re.match(r"^\d+$", ticket_id):
        abort(404)
    conn = get_db()
    conn.execute("UPDATE tickets SET zd_updated='' WHERE ticket_id=?", (ticket_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.route("/attachment/<ticket_id>/<path:filename>")
def serve_attachment(ticket_id: str, filename: str):
    if not re.match(r"^\d+$", ticket_id):
        abort(404)
    # Prevent path traversal
    filepath = (TICKETS_DIR / ticket_id / "attachments" / filename).resolve()
    allowed_root = (TICKETS_DIR / ticket_id / "attachments").resolve()
    if not str(filepath).startswith(str(allowed_root)):
        abort(403)
    if not filepath.exists():
        abort(404)
    return send_file(filepath, as_attachment=True)


if __name__ == "__main__":
    if not DB_PATH.exists():
        print("[!] データベースが見つかりません。先に scraper.py を実行してください。")
    else:
        print("[+] http://127.0.0.1:5000 でサーバーを起動します")
        app.run(debug=False, host="127.0.0.1", port=5000)
