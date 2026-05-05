"""
Zendesk Help Center scraper — altimaiot.zendesk.com

Usage:
    python scraper.py

First run: opens a browser window for manual login, then saves the session.
Subsequent runs: reuses saved session; only re-downloads tickets updated since last run.
"""

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from playwright.async_api import async_playwright

BASE_URL = "https://altimaiot.zendesk.com"
HC_BASE  = f"{BASE_URL}/hc/ja"

DATA_DIR    = Path("data")
TICKETS_DIR = DATA_DIR / "tickets"
DB_PATH     = DATA_DIR / "zendesk.db"
PROFILE_DIR = DATA_DIR / "browser_profile"

TABS = [
    ("my-requests",  "自分のリクエスト"),
    ("ccd-requests", "CCに入っているリクエスト"),
]

# Map status-label CSS class suffix → Japanese display text
_STATUS_CLASS_MAP = {
    "solved":   "解決済み",
    "open":     "オープン",
    "pending":  "保留中",
    "new":      "新規",
    "closed":   "クローズ",
    "on-hold":  "保留中",
    "answered": "回答済み",
}


def _class_to_status(class_attr: str) -> str:
    """Extract ticket status from a status-label CSS class string."""
    m = re.search(r'status-label-((?!request)\w[\w-]*)', class_attr)
    return _STATUS_CLASS_MAP.get(m.group(1), m.group(1)) if m else ""


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _detect_fts_tokenizer(conn: sqlite3.Connection) -> str:
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts_probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE _fts_probe")
        return "trigram"
    except sqlite3.OperationalError:
        return "unicode61"


def _ensure_fts_table(conn: sqlite3.Connection, tokenizer: str) -> None:
    """Create FTS table, or recreate it if it was made with the broken content='' option."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tickets_fts'"
    ).fetchone()

    if row:
        existing_sql = row[0] or ""
        # content='' makes the table contentless — ticket_id is not stored,
        # JOIN fails, and snippet() returns nothing. Drop and recreate.
        if "content=''" in existing_sql or 'content=""' in existing_sql:
            conn.execute("DROP TABLE tickets_fts")
            print("[DB] FTSテーブルを再作成しました（content='' を修正）。reindex.py を実行してください。")
        else:
            return  # already correct

    conn.execute(f"""
        CREATE VIRTUAL TABLE tickets_fts USING fts5(
            ticket_id UNINDEXED,
            subject,
            body,
            tokenize='{tokenizer}'
        )
    """)


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id   TEXT PRIMARY KEY,
            tab         TEXT,
            subject     TEXT,
            status      TEXT,
            zd_updated  TEXT,
            dl_updated  TEXT
        )
    """)

    tokenizer = _detect_fts_tokenizer(conn)
    _ensure_fts_table(conn, tokenizer)

    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def tab_page_url(tab: str, page_num: int) -> str:
    return (
        f"{HC_BASE}/requests"
        f"?query=&page={page_num}"
        f"&sort_by=updated_at&sort_order=desc"
        f"&selected_tab_name={tab}"
    )


def extract_ticket_id(href: str) -> str | None:
    m = re.search(r"/requests/(\d+)", href or "")
    return m.group(1) if m else None


async def goto(page, url: str) -> None:
    """Navigate and wait for XHR-loaded content to settle."""
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    # networkidle ensures ticket rows fetched via XHR are present.
    # Short timeout so analytics pings don't block us indefinitely.
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

async def ensure_logged_in(page) -> None:
    await page.goto(f"{HC_BASE}/requests", wait_until="domcontentloaded", timeout=60_000)

    if "sign_in" in page.url or "/auth" in page.url or "login" in page.url:
        print("\n[!] Zendeskへのログインが必要です。")
        print("    ブラウザウィンドウでログインしてください。")
        print("    ログイン完了後、自動的に処理が続きます...\n")
        await page.wait_for_url("**/hc/**", timeout=300_000)
        print("[+] ログイン完了を検出しました。\n")


# ---------------------------------------------------------------------------
# Request list scraping
# ---------------------------------------------------------------------------

async def scrape_current_page(page, tab: str) -> list[dict]:
    """Extract ticket entries from the currently loaded list page."""
    tickets: list[dict] = []
    seen_ids: set[str] = set()

    links = await page.query_selector_all("a[href*='/requests/']")
    for link in links:
        href = await link.get_attribute("href") or ""
        tid  = extract_ticket_id(href)
        if not tid or tid in seen_ids:
            continue
        seen_ids.add(tid)

        subject = (await link.inner_text()).strip()
        if not subject:
            continue

        container_js = (
            "el => el.closest('tr')"
            " || el.closest('li')"
            " || el.closest('[class*=\"request\"]')"
            " || el.parentElement"
        )
        container = await link.evaluate_handle(container_js)

        status     = ""
        zd_updated = ""

        try:
            status_el = await container.query_selector(".status-label")
            if status_el:
                class_attr = await status_el.get_attribute("class") or ""
                status = _class_to_status(class_attr)
        except Exception:
            pass

        try:
            zd_updated = await container.evaluate("""el => {
                // Value must look like a date/time: YYYY-MM or HH:MM pattern.
                const isDateLike = s => /\\d{4}[-\\/]\\d{2}|\\d{2}:\\d{2}/.test(s);
                const times = [...el.querySelectorAll('time, [datetime]')];
                for (let i = times.length - 1; i >= 0; i--) {
                    const dt = times[i].getAttribute('datetime') || times[i].innerText.trim();
                    if (dt && isDateLike(dt)) return dt;
                }
                // Fallback: scan <td> cells from the right for date-like text.
                const tds = [...el.querySelectorAll('td')];
                for (let i = tds.length - 1; i >= 0; i--) {
                    const txt = tds[i].innerText.trim();
                    if (txt && isDateLike(txt)) return txt;
                }
                return '';
            }""") or ""
        except Exception:
            pass

        tickets.append({
            "ticket_id":  tid,
            "subject":    subject,
            "status":     status,
            "zd_updated": zd_updated,
            "tab":        tab,
        })

    return tickets


async def collect_tab_tickets(page, tab: str, tab_name: str) -> list[dict]:
    """Page through ?page=1, 2, 3 … until an empty page is returned."""
    all_tickets: list[dict] = []

    for page_num in range(1, 500):
        url = tab_page_url(tab, page_num)
        print(f"  {tab_name} — ページ {page_num} を取得中...", end=" ", flush=True)

        await goto(page, url)
        tickets = await scrape_current_page(page, tab)

        if not tickets:
            # Retry once: re-navigate so networkidle fires again
            print("(0件、再試行)...", end=" ", flush=True)
            await asyncio.sleep(3.0)
            await goto(page, url)
            tickets = await scrape_current_page(page, tab)

        if not tickets:
            print("(チケットなし) 終了。")
            break

        all_tickets.extend(tickets)
        print(f"{len(tickets)} 件")

    return all_tickets


# ---------------------------------------------------------------------------
# Attachment download
# ---------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "attachment"


async def download_attachments(ticket_id: str, page, cookies: list) -> list[dict]:
    attachments_dir = TICKETS_DIR / ticket_id / "attachments"

    selectors = (
        "a[href*='attachments'],"
        "a[href*='/hc/'][href*='token='],"
        ".attachment a,"
        "[class*='attachment'] a,"
        "a[download]"
    )
    links = await page.query_selector_all(selectors)
    if not links:
        return []

    attachments_dir.mkdir(parents=True, exist_ok=True)
    cookie_dict       = {c["name"]: c["value"] for c in cookies}
    meta: list[dict]  = []
    downloaded_urls: set[str] = set()

    async with httpx.AsyncClient(
        cookies=cookie_dict,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124"},
        follow_redirects=True,
        timeout=120,
    ) as client:
        for link in links:
            href = await link.get_attribute("href") or ""
            if not href or href.startswith("#") or href in downloaded_urls:
                continue
            if href.startswith("/"):
                href = BASE_URL + href
            elif not href.startswith("http"):
                continue
            downloaded_urls.add(href)

            display_name = (await link.inner_text()).strip()
            fallback     = Path(urlparse(href).path).name or f"file_{len(meta)+1}"
            filename     = sanitize_filename(display_name or fallback)
            dest         = attachments_dir / filename

            if dest.exists():
                meta.append({"filename": filename, "url": href})
                continue

            try:
                resp = await client.get(href)
                resp.raise_for_status()

                cd      = resp.headers.get("content-disposition", "")
                m_utf8  = re.search(r"filename\*=UTF-8''(.+)",   cd, re.IGNORECASE)
                m_plain = re.search(r'filename="?([^";\r\n]+)"?', cd, re.IGNORECASE)
                cd_name = ""
                if m_utf8:
                    cd_name = sanitize_filename(unquote(m_utf8.group(1).strip()))
                elif m_plain:
                    cd_name = sanitize_filename(m_plain.group(1).strip())
                if cd_name:
                    filename = cd_name
                    dest     = attachments_dir / filename

                dest.write_bytes(resp.content)
                meta.append({"filename": filename, "url": href})
                print(f"    添付: {filename} ({len(resp.content):,} bytes)")

            except httpx.HTTPStatusError as e:
                print(f"    添付ダウンロード失敗 [{e.response.status_code}]: {href}")
            except Exception as e:
                print(f"    添付ダウンロード失敗: {e}")

    return meta


# ---------------------------------------------------------------------------
# Ticket content download
# ---------------------------------------------------------------------------

_MAIN_TEXT_JS = """
() => {
    const el =
        document.querySelector('main') ||
        document.querySelector('#main-content') ||
        document.querySelector('.main-content') ||
        document.querySelector('article') ||
        document.body;
    return el ? el.innerText : document.body.innerText;
}
"""

_LAST_DATETIME_JS = """
() => {
    const main = document.querySelector('main, #main-content, article, .main-content');
    const scope = main || document;
    const times = [...scope.querySelectorAll('time, [datetime]')];
    if (!times.length) return '';
    const t = times[times.length - 1];
    return t.getAttribute('datetime') || t.innerText.trim();
}
"""


def _comment_page_url(ticket_id: str, page_num: int) -> str:
    """URL for a specific comment page of a ticket."""
    if page_num <= 1:
        return f"{HC_BASE}/requests/{ticket_id}"
    return f"{HC_BASE}/requests/{ticket_id}?page={page_num}#comments"


async def download_ticket(ticket_id: str, page, cookies: list) -> dict:
    base_url   = f"{HC_BASE}/requests/{ticket_id}"
    ticket_dir = TICKETS_DIR / ticket_id
    ticket_dir.mkdir(parents=True, exist_ok=True)

    await goto(page, base_url)

    # ── Detect redirect to list page (ticket not found / no access) ──────
    # When a ticket is inaccessible, Zendesk redirects to the request list.
    # The list page has no .request-details and its h1 is the generic "リクエスト".
    has_details = await page.query_selector(".request-details, article[class*='request']")
    h1_el = await page.query_selector("h1")
    h1_text = (await h1_el.inner_text()).strip() if h1_el else ""
    if not has_details and h1_text in ("リクエスト", "Requests", "My activities"):
        print(f"  [スキップ] #{ticket_id}: チケットにアクセスできません（一覧ページにリダイレクト）")
        raise RuntimeError(f"ticket {ticket_id} redirected to list page")

    # ── Extract subject & status from page 1 ────────────────────────────
    subject = ""
    for sel in ["h1.request-subject", ".request-subject", "h1", "[class*='subject']"]:
        el = await page.query_selector(sel)
        if el:
            txt = (await el.inner_text()).strip()
            if txt:
                subject = txt
                break
    if not subject:
        subject = f"Request {ticket_id}"

    status = ""
    status_el = await page.query_selector(".status-label")
    if status_el:
        class_attr = await status_el.get_attribute("class") or ""
        status = _class_to_status(class_attr)

    # ── Save page 1 ──────────────────────────────────────────────────────
    (ticket_dir / "ticket.html").write_text(await page.content(), encoding="utf-8")
    all_text       = [await page.evaluate(_MAIN_TEXT_JS)]
    detail_updated = await page.evaluate(_LAST_DATETIME_JS) or ""
    attachments    = await download_attachments(ticket_id, page, cookies)

    # ── Follow comment pagination (page 2, 3, …) ─────────────────────────
    # Increment page number directly (same strategy as collect_tab_tickets).
    # Zendesk does NOT redirect for out-of-range pages — it silently returns
    # the last valid page again. We detect this with an MD5 hash of the text:
    # if the new page's content matches the previous page, we have reached the end.
    comment_page = 1
    prev_hash = hashlib.md5(all_text[0].encode()).hexdigest()

    for next_num in range(2, 51):              # safety cap: 50 comment pages
        url = _comment_page_url(ticket_id, next_num)
        print(f"    コメント p{next_num} を確認中...", end=" ", flush=True)
        await goto(page, url)

        text = await page.evaluate(_MAIN_TEXT_JS)
        curr_hash = hashlib.md5(text.encode()).hexdigest()

        # Empty page or identical content → past the last comment page
        if not text.strip() or curr_hash == prev_hash:
            print("(終端) 終了。")
            break

        prev_hash    = curr_hash
        comment_page = next_num
        (ticket_dir / f"ticket_page{next_num}.html").write_text(
            await page.content(), encoding="utf-8"
        )
        all_text.append(text)

        extra = await download_attachments(ticket_id, page, cookies)
        attachments.extend(extra)
        print("完了")

    # ── Persist metadata ─────────────────────────────────────────────────
    meta = {
        "ticket_id":     ticket_id,
        "subject":       subject,
        "status":        status,
        "url":           base_url,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "attachments":   attachments,
        "comment_pages": comment_page,        # total pages downloaded
    }
    (ticket_dir / "ticket.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "subject":        subject,
        "status":         status,
        "content_text":   " ".join(all_text),   # combined text for FTS
        "detail_updated": detail_updated,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Zendesk Help Center scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例:\n"
            "  python scraper.py                   # 通常実行（差分更新）\n"
            "  python scraper.py -t 3364 5000      # 指定チケットのみ強制再取得\n"
        ),
    )
    parser.add_argument(
        "-t", "--ticket", nargs="+", metavar="ID",
        help="強制再取得するチケットID（スペース区切りで複数指定可）",
    )
    args = parser.parse_args()
    force_ids: set[str] = set(args.ticket) if args.ticket else set()

    for d in (DATA_DIR, TICKETS_DIR, PROFILE_DIR):
        d.mkdir(parents=True, exist_ok=True)

    conn    = init_db()
    now_iso = datetime.now(timezone.utc).isoformat()

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(PROFILE_DIR.resolve()),
            headless=False,
            locale="ja-JP",
            viewport={"width": 1280, "height": 900},
        )

        page = await context.new_page()
        await ensure_logged_in(page)

        # ── Collect ticket list (skip when specific tickets are forced) ──
        if force_ids:
            print(f"=== 強制再取得モード: {sorted(force_ids)} ===\n")
            all_tickets = []
            for tid in sorted(force_ids):
                row = conn.execute(
                    "SELECT tab, subject FROM tickets WHERE ticket_id=?", (tid,)
                ).fetchone()
                all_tickets.append({
                    "ticket_id": tid,
                    "tab":       row[0] if row else "",
                    "subject":   row[1] if row else f"#{tid}",
                    "zd_updated": "",       # empty → always download
                })
        else:
            print("=== リクエスト一覧を取得中 ===\n")
            raw_tickets: list[dict] = []
            for tab, tab_name in TABS:
                print(f"[{tab_name}]")
                tickets = await collect_tab_tickets(page, tab, tab_name)
                raw_tickets.extend(tickets)
                print(f"  → {len(tickets)} 件\n")

            seen: dict[str, dict] = {}
            for t in raw_tickets:
                tid = t["ticket_id"]
                if tid not in seen or t["tab"] == "my-requests":
                    seen[tid] = t
            all_tickets = list(seen.values())
            print(f"合計 {len(all_tickets)} 件（重複除去後）\n")

        cookies = await context.cookies()

        # ── Download / update tickets ────────────────────────────────────
        print("=== チケットをダウンロード中 ===\n")
        updated = skipped = errors = 0

        for i, ticket in enumerate(all_tickets, 1):
            tid             = ticket["ticket_id"]
            zd_updated      = ticket.get("zd_updated", "")
            tab             = ticket.get("tab", "")
            subject_preview = ticket.get("subject", "")[:60]

            # Skip unchanged tickets unless this ID was explicitly forced
            if tid not in force_ids:
                row = conn.execute(
                    "SELECT zd_updated FROM tickets WHERE ticket_id=?", (tid,)
                ).fetchone()
                if row and row[0] and row[0] == zd_updated:
                    print(f"[{i:>3}/{len(all_tickets)}] #{tid} スキップ（更新なし）")
                    skipped += 1
                    continue

            print(f"[{i:>3}/{len(all_tickets)}] #{tid} ダウンロード: {subject_preview}")

            try:
                content = await download_ticket(tid, page, cookies)

                # Use detail page date when list page gave a non-date value
                _detail_dt = content.get("detail_updated", "")
                _zd_is_date = bool(zd_updated and re.search(r'\d{4}[-/]\d{2}|\d{2}:\d{2}', zd_updated))
                effective_updated = zd_updated if _zd_is_date else _detail_dt

                conn.execute(
                    """
                    INSERT OR REPLACE INTO tickets
                        (ticket_id, tab, subject, status, zd_updated, dl_updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (tid, tab, content["subject"], content["status"], effective_updated, now_iso),
                )
                conn.execute("DELETE FROM tickets_fts WHERE ticket_id=?", (tid,))
                conn.execute(
                    "INSERT INTO tickets_fts (ticket_id, subject, body) VALUES (?, ?, ?)",
                    (tid, content["subject"], content["content_text"]),
                )
                conn.commit()
                updated += 1

            except Exception as e:
                print(f"  [エラー] {e}")
                errors += 1

        await context.close()

    conn.close()
    print(f"\n=== 完了 ===")
    print(f"  更新:     {updated} 件")
    print(f"  スキップ: {skipped} 件")
    print(f"  エラー:   {errors} 件")


if __name__ == "__main__":
    asyncio.run(main())
