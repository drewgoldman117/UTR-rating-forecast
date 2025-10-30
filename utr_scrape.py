import os
import re
import argparse
from pathlib import Path
from typing import Optional

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

FULL_HISTORY_HEADER_CANDIDATES = ["Full Rating History", "Full Ratings History"]

# =========================
# Parsing (HTML -> rows)
# =========================

def extract_name_from_title(title: str) -> str:
    if not title:
        return ""
    return title.split("|")[0].strip()

def parse_full_history_from_html(html: str):
    """
    Parse the 'Full Rating(s) History' section.
    Returns: list of dicts with keys {'date','UTR'}.
    """
    soup = BeautifulSoup(html, "lxml")

    # Try to anchor near the header
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

# =========================
# Diagnostics (optional)
# =========================

def enable_diagnostics(context, page, out_dir="diagnostics"):
    os.makedirs(out_dir, exist_ok=True)
    try:
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
    except Exception:
        pass
    # NOTE: use .type and .text as PROPERTIES (no parentheses)
    page.on("console", lambda msg: print(f"[console.{msg.type}] {msg.text}"))
    page.on("pageerror", lambda exc: print(f"[pageerror] {exc}"))

def save_diagnostics(page, step):
    try:
        path = f"diagnostics/{step}.png"
        page.screenshot(path=path, full_page=True)
        print(f"[diag] screenshot -> {path}")
    except Exception as e:
        print(f"[diag] screenshot failed ({step}): {e}")

def stop_tracing(context):
    try:
        context.tracing.stop(path="diagnostics/trace.zip")
        print("[diag] trace -> diagnostics/trace.zip")
    except Exception as e:
        print(f"[diag] trace stop failed: {e}")

# =========================
# Playwright helpers
# =========================

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
            sign_in = root.locator("button:has-text('Sign In'), button:has-text('Sign in'), button:has-text('Sign-In')")
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

def wait_for_login_form(page, timeout_ms=8000) -> bool:
    """Wait for the login form to appear after clicking overlay Sign In."""
    try:
        page.wait_for_function(
            """() => !!(document.querySelector('#emailInput') || document.querySelector('#passwordInput'))""",
            timeout=timeout_ms
        )
        return True
    except Exception:
        try:
            page.wait_for_selector("input#emailInput, input#passwordInput, input[type='email'], input[type='password']", timeout=2000)
            return True
        except Exception:
            return False

def try_fill_login_in_context(query_ctx, email: str, password: str) -> bool:
    """
    Fill email/password and submit inside the given context (page/locator/frame).
    Uses typing to trigger onChange/onInput handlers.
    """
    try:
        email_loc = query_ctx.locator("#emailInput, input[name='email'], input[type='email']").first
        pass_loc  = query_ctx.locator("#passwordInput, input[name='password'], input[type='password']").first
        if email_loc.count() == 0 or pass_loc.count() == 0:
            return False

        email_loc.wait_for(state="visible", timeout=6000)
        pass_loc.wait_for(state="visible", timeout=6000)

        # Clear then type (real key events)
        try: email_loc.fill("")
        except Exception: pass
        email_loc.click()
        email_loc.type(email, delay=20)

        try: pass_loc.fill("")
        except Exception: pass
        pass_loc.click()
        pass_loc.type(password, delay=20)

        # Submit
        submit = query_ctx.locator("form button[type='submit']:has-text('SIGN IN')").first
        if submit.count() == 0:
            submit = query_ctx.locator("button:has-text('SIGN IN'), button:has-text('Sign in'), button:has-text('Sign In')").first

        if submit.count() > 0:
            try:
                query_ctx.wait_for_function("""btn => !btn.disabled""", arg=submit, timeout=3000)
            except Exception:
                pass
            submit.click()
        else:
            pass_loc.press("Enter")

        return True
    except Exception:
        return False

def login_if_needed(page, email: str, password: str) -> bool:
    """
    Tries inline/page login, overlay→login flow, then iframe login.
    Returns True if an attempt was made.
    """
    if not email or not password:
        return False
    attempted = False

    # Inline/page-level
    if try_fill_login_in_context(page, email, password):
        attempted = True
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            page.wait_for_timeout(800)
        return True

    # Overlay → login page or inline
    if click_overlay_sign_in_if_present(page):
        attempted = True
        wait_for_login_form(page, timeout_ms=8000)
        if try_fill_login_in_context(page, email, password):
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PWTimeout:
                page.wait_for_timeout(800)
            return True

    # Iframe-based
    try:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            if try_fill_login_in_context(frame, email, password):
                attempted = True
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except PWTimeout:
                    page.wait_for_timeout(800)
                return True
    except Exception:
        pass

    return attempted

def looks_logged_in(page) -> bool:
    """Heuristic: if we don't see overlay or login form, assume logged-in."""
    try:
        if page.locator("div[class*='popup__overlay']").count() > 0:
            return False
        if page.locator("#emailInput, #passwordInput, button:has-text('SIGN IN')").count() > 0:
            return False
        return True
    except Exception:
        return False

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
        page.get_by_role("button", name=re.compile(r"^\s*show all\s*$", re.I)),
        page.get_by_role("link", name=re.compile(r"^\s*show all\s*$", re.I)),
        page.locator("button:has-text('Show all')"),
        page.locator("a:has-text('Show all')"),
        page.locator("text=/^\\s*Show all\\s*$/i"),
    ]
    for loc in candidates:
        try:
            if loc.count() > 0:
                loc.first.scroll_into_view_if_needed()
                loc.first.click()
                try:
                    page.wait_for_timeout(150)
                    page.wait_for_function("""() => !document.body.innerText.match(/\\bShow\\s+all\\b/i)""", timeout=timeout_ms)
                except PWTimeout:
                    pass
                return True
        except Exception:
            continue
    return False

# =========================
# Live fetch
# =========================

def live_fetch_profile_html(
    user_id: int,
    headless: bool = True,
    use_storage_state: Optional[str] = None,
    save_storage_state_to: Optional[str] = None
) -> tuple[str, str]:
    """
    Navigate to profile, ensure auth, expand 'Show all', return (html, title).
    """
    profile_url = f"https://app.utrsports.net/profiles/{user_id}?t=6"
    email = os.getenv("UTR_EMAIL", "").strip()
    password = os.getenv("UTR_PASSWORD", "").strip()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context_kwargs = {}
        if use_storage_state and os.path.exists(use_storage_state):
            context_kwargs["storage_state"] = use_storage_state
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        # Diagnostics
        enable_diagnostics(context, page)

        # Reasonable defaults
        page.set_default_timeout(15000)
        page.set_default_navigation_timeout(20000)

        # 1) Go to profile
        page.goto(profile_url, wait_until="domcontentloaded")
        save_diagnostics(page, "01_after_profile_nav")

        # 2) Auth if needed
        if email and password and not looks_logged_in(page):
            click_overlay_sign_in_if_present(page)
            save_diagnostics(page, "02_after_click_overlay_sign_in")

            wait_for_login_form(page, timeout_ms=8000)
            save_diagnostics(page, "03_login_form_visible")

            login_if_needed(page, email, password)
            save_diagnostics(page, "04_after_login_submit")

            page.goto(profile_url, wait_until="domcontentloaded")
            save_diagnostics(page, "05_after_reload_profile")

            # Optionally persist session
            if save_storage_state_to:
                try:
                    context.storage_state(path=save_storage_state_to)
                    print(f"[diag] saved storage state -> {save_storage_state_to}")
                except Exception as e:
                    print(f"[diag] save storage state failed: {e}")

        # 3) Wait for history header and expand
        wait_for_full_history_header(page)
        save_diagnostics(page, "06_after_wait_history_header")

        click_show_all_if_present(page)
        save_diagnostics(page, "07_after_click_show_all")

        # 4) Return content
        title = page.title()
        html = page.content()

        stop_tracing(context)
        context.close()
        browser.close()
        return html, title

# =========================
# Output
# =========================

def write_csv(user_id: int, player_name: str, rows: list[dict], out_path: str):
    df = pd.DataFrame(rows)
    if df.empty:
        print("No rows found.")
        return
    df.insert(0, "player_name", player_name)
    df.insert(0, "user_id", user_id)
    df.to_csv(out_path, index=False)
    print(f"Wrote {len[df]} rows → {out_path}")

# =========================
# CLI
# =========================

def main():
    ap = argparse.ArgumentParser(description="Fetch UTR Full Rating History to CSV")
    ap.add_argument("--user-id", type=int, required=True, help="UTR user id (e.g., 119061)")
    ap.add_argument("--out", type=str, default="utr_history.csv", help="Output CSV path")
    ap.add_argument("--html", type=str, help="Parse from a saved HTML file instead of live site")
    ap.add_argument("--headed", action="store_true", help="Run browser with UI (non-headless)")
    ap.add_argument("--use-storage", type=str, help="Path to storage state JSON to reuse session (optional)")
    ap.add_argument("--save-storage", type=str, help="Path to save storage state after login (optional)")
    args = ap.parse_args()

    if args.html:
        html = Path(args.html).read_text(encoding="utf-8", errors="ignore")
        title_match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
        player_name = extract_name_from_title(title_match.group(1)) if title_match else ""
        rows = parse_full_history_from_html(html)
        write_csv(args.user_id, player_name, rows, args.out)
        return

    html, title = live_fetch_profile_html(
        args.user_id,
        headless=(not args.headed),
        use_storage_state=args.use_storage,
        save_storage_state_to=args.save_storage
    )
    player_name = extract_name_from_title(title)
    rows = parse_full_history_from_html(html)
    write_csv(args.user_id, player_name, rows, args.out)

if __name__ == "__main__":
    main()
