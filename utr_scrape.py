import os
import re
import argparse
import pandas as pd
from bs4 import BeautifulSoup
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

FULL_HISTORY_HEADER_CANDIDATES = [
    "Full Rating History",
    "Full Ratings History",
]

# ---------- Parsing (HTML -> rows) ----------

def extract_name_from_title(title: str) -> str:
    if not title:
        return ""
    return title.split("|")[0].strip()

def parse_full_history_from_html(html: str):
    """
    Parse the 'Full Rating(s) History' section.
    Returns: [{'date': 'YYYY-MM-DD', 'UTR': '11.87'}, ...]
    """
    soup = BeautifulSoup(html, "lxml")

    # Anchor near the header if possible
    header_node = None
    for node in soup.find_all(string=True):
        text = (node or "").strip()
        if text and text.lower() in (h.lower() for h in FULL_HISTORY_HEADER_CANDIDATES):
            header_node = node
            break

    container = soup
    if header_node:
        container = header_node.find_parent()
        for _ in range(4):
            if not container:
                break
            if container.select("div[class*='historyItem__'], div:has(div[class*='historyItemDate__'])"):
                break
            container = container.parent
        if not container:
            container = soup

    rows = []
    item_nodes = container.select("div[class*='historyItem__']")
    if not item_nodes:
        item_nodes = container.select("div:has(div[class*='historyItemDate__'])")

    for item in item_nodes:
        date_el = item.select_one("div[class*='historyItemDate__']")
        utr_el  = item.select_one("div[class*='historyItemRating__']")
        date_text = date_el.get_text(strip=True) if date_el else ""
        utr_text  = utr_el.get_text(strip=True) if utr_el else ""

        if not date_text or not utr_text:
            # Fallback: scan text for date/UTR patterns
            txt = " ".join(x.get_text(" ", strip=True) for x in item.select("div, span"))
            m_date = re.search(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b", txt)
            m_utr  = re.search(r"\b(\d{1,2}\.\d{1,2})\b", txt)
            if m_date and not date_text:
                date_text = m_date.group(1)
            if m_utr and not utr_text:
                utr_text = m_utr.group(1)

        if utr_text:
            m = re.search(r"\d{1,2}\.\d{1,2}", utr_text)
            if m:
                utr_text = m.group(0)

        if date_text and utr_text:
            rows.append({"date": date_text, "UTR": utr_text})

    # Deduplicate, preserve order
    seen, uniq = set(), []
    for r in rows:
        key = (r["date"], r["UTR"])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq

# ---------- Playwright helpers ----------

def try_fill_login_in_context(query_ctx, email: str, password: str) -> bool:
    """
    Fill email/password in the login form (#emailInput/#passwordInput) and click SIGN IN.
    """
    try:
        email_input = query_ctx.locator("#emailInput, input[type='email']").first
        pwd_input   = query_ctx.locator("#passwordInput, input[type='password']").first

        if email_input.count() == 0 or pwd_input.count() == 0:
            return False

        email_input.wait_for(state="visible", timeout=5000)
        pwd_input.wait_for(state="visible", timeout=5000)

        email_input.fill(email)
        pwd_input.fill(password)

        sign_in_btn = query_ctx.locator("button[type='submit']:has-text('SIGN IN')")
        if sign_in_btn.count() == 0:
            sign_in_btn = query_ctx.locator(
                "button:has-text('SIGN IN'), button:has-text('Sign in'), button:has-text('Sign In')"
            )

        if sign_in_btn.count() > 0:
            sign_in_btn.first.click()
        else:
            (getattr(query_ctx, "page", query_ctx)).keyboard.press("Enter")

        return True
    except Exception:
        return False

def click_overlay_sign_in_if_present(page, timeout_ms=8000) -> bool:
    """
    Handles UTR's div-based popup overlay with a 'Sign In' button.
    """
    try:
        overlay = page.locator("div[class*='popup__overlay']")
        if overlay.count() == 0:
            try:
                page.wait_for_selector("div[class*='popup__overlay']", timeout=1500)
            except Exception:
                return False
            overlay = page.locator("div[class*='popup__overlay']")

        if overlay.count() == 0:
            return False

        root = overlay.first
        sign_in = root.locator("button.btn.btn-primary-inv:has-text('Sign In')")
        if sign_in.count() == 0:
            sign_in = root.locator("button:has-text('Sign In'), button:has-text('Sign in')")
        if sign_in.count() > 0:
            sign_in.first.click()
            try:
                page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except Exception:
                page.wait_for_timeout(300)
            return True

        return False
    except Exception:
        return False

def login_if_needed(page, email: str, password: str) -> bool:
    if not email or not password:
        return False

    attempted = try_fill_login_in_context(page, email, password)
    if attempted:
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            page.wait_for_timeout(800)
        return True

    if click_overlay_sign_in_if_present(page):
        attempted = try_fill_login_in_context(page, email, password)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            page.wait_for_timeout(800)
        if attempted:
            return True

    # Check if login form is inside an iframe
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        if try_fill_login_in_context(frame, email, password):
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PWTimeout:
                page.wait_for_timeout(800)
            return True
    return attempted

def wait_for_full_history_header(page):
    for txt in FULL_HISTORY_HEADER_CANDIDATES:
        try:
            page.get_by_text(txt, exact=True).wait_for(timeout=8000)
            return
        except PWTimeout:
            continue
    try:
        page.get_by_text("Full Rating", exact=False).wait_for(timeout=5000)
    except PWTimeout:
        pass

def click_show_all_if_present(page, timeout_ms=8000):
    candidates = [
        page.locator("button:has-text('Show all')"),
        page.locator("a:has-text('Show all')"),
    ]
    for loc in candidates:
        try:
            if loc.count() > 0:
                loc.first.click()
                try:
                    page.wait_for_timeout(150)
                except PWTimeout:
                    pass
                return True
        except Exception:
            continue
    return False

def live_fetch_profile_html(user_id: int, headless: bool = True) -> tuple[str, str]:
    profile_url = f"https://app.utrsports.net/profiles/{user_id}?t=6"
    email = os.getenv("UTR_EMAIL", "").strip()
    password = os.getenv("UTR_PASSWORD", "").strip()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(10000)

        page.goto(profile_url, wait_until="domcontentloaded")

        if email and password:
            click_overlay_sign_in_if_present(page)
            login_if_needed(page, email, password)
            page.goto(profile_url, wait_until="domcontentloaded")

        wait_for_full_history_header(page)
        click_show_all_if_present(page)

        title = page.title()
        html = page.content()
        context.close()
        browser.close()
        return html, title

# ---------- Output ----------

def write_csv(user_id: int, player_name: str, rows: list[dict], out_path: str):
    df = pd.DataFrame(rows)
    if df.empty:
        print("No rows found.")
        return
    df.insert(0, "player_name", player_name)
    df.insert(0, "user_id", user_id)
    df.to_csv(out_path, index=False)
    print(f"Wrote {len(df)} rows → {out_path}")

# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="Fetch UTR Full Rating History to CSV")
    ap.add_argument("--user-id", type=int, required=True)
    ap.add_argument("--out", type=str, default="utr_history.csv")
    ap.add_argument("--html", type=str, help="Parse from a saved HTML file instead of live site")
    ap.add_argument("--headed", action="store_true", help="Run browser with UI")
    args = ap.parse_args()

    if args.html:
        html = Path(args.html).read_text(encoding="utf-8", errors="ignore")
        title_match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
        player_name = extract_name_from_title(title_match.group(1)) if title_match else ""
        rows = parse_full_history_from_html(html)
        write_csv(args.user_id, player_name, rows, args.out)
        return

    html, title = live_fetch_profile_html(args.user_id, headless=(not args.headed))
    player_name = extract_name_from_title(title)
    rows = parse_full_history_from_html(html)
    write_csv(args.user_id, player_name, rows, args.out)

if __name__ == "__main__":
    main()
