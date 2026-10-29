"""
Poll the Sreeleathers supplier dashboard and send a Telegram alert for new orders (POs).
Designed to run on GitHub Actions (see .github/workflows/order-alerts.yml),
but also works on your own PC.

Environment variables (never hard-code passwords):
    SL_USER, SL_PASS   - your dashboard login
    TG_TOKEN           - Telegram bot token from @BotFather
    TG_CHAT_IDS        - your Telegram chat id (several ids separated by commas)
"""
import hashlib
import json
import os
import re
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path

import requests
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

BASE_URL = "https://sreeleathers.net/"
ORDERS_URL = "https://sreeleathers.net/admin_panel.php?pgid=de0577856966eb0df0825bdb1873c46c"
SEEN_FILE = Path(__file__).with_name("seen_orders.json")

# The report only shows orders from the "From Date" onwards. Look back this many days.
DAYS_BACK = 60


def clean(text):
    """Collapse whitespace, including &nbsp; and newlines."""
    return " ".join(text.split())


def col(rec, prefix):
    """Get a column value by header prefix (tolerates 'Order Date.' vs 'Order Date')."""
    for k, v in rec.items():
        if k.lower().startswith(prefix.lower()):
            return v
    return ""


def to_int(s):
    return int(re.sub(r"\D", "", s) or 0)


def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return None  # None = first run


def save_seen(keys):
    SEEN_FILE.write_text(json.dumps(sorted(keys), indent=0))


def goto(page, url, tries=3):
    """Page kholo bina 'networkidle' ka wait kiye; slow network par retry karo."""
    for attempt in range(1, tries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return
        except PWTimeout:
            print(f"goto timeout (attempt {attempt}/{tries})")
            if attempt == tries:
                raise


def login(page):
    """Homepage par form hidden hota hai: pehle top-right 'Login' button, phir modal form."""
    goto(page, BASE_URL)

    try:
        # 1. Top-right ka "Login" button (exact match, "Login to Dashboard" nahi)
        page.get_by_text("Login", exact=True).first.click(timeout=20000)
        # 2. Modal ka form visible hone tak wait
        page.locator("#uname").wait_for(state="visible", timeout=15000)
    except Exception:
        # Credentials fill hone se pehle liya jaata hai, isliye screenshot mein password nahi aata
        page.screenshot(path="debug.png", full_page=True)
        raise

    page.locator("#uname").fill(os.environ["SL_USER"])
    page.get_by_label("Password").fill(os.environ["SL_PASS"])

    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
            page.get_by_role("button", name="Login to Dashboard").click()
    except PWTimeout:
        pass  # login AJAX se hua ho to page navigate nahi hota, aage badho


def fetch_items():
    """Return one dict per order line: PO number, item, size, qty, etc."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 768})

        try:
            login(page)

            goto(page, ORDERS_URL)
            # Fails loudly (timeout) if login didn't work
            page.get_by_text("Report Filters").first.wait_for(timeout=30000)
        except Exception:
            page.screenshot(path="debug.png", full_page=True)
            browser.close()
            raise

        # From Date field readonly hai aur default value pehle se bhari hoti hai,
        # isliye use chhue bina seedha Display dabate hain (jaise manually karte ho)
        page.get_by_text("Display", exact=True).first.click()

        try:
            page.wait_for_selector('#dis_dtl div[id^="PO-"]', timeout=30000)
        except PWTimeout:
            print("No PO blocks found after Display (no orders, or report didn't load).")
            page.screenshot(path="debug.png", full_page=True)
            browser.close()
            return []


        # --- TEMP DEBUG (kaam hone ke baad hata dena) ---
        page.screenshot(path="debug.png", full_page=True)
        blocks = page.locator('#dis_dtl div[id^="PO-"]')
        print(f"DEBUG: PO blocks = {blocks.count()}")
        first = blocks.first
        print(f"DEBUG: first block th={first.locator('th').count()} "
              f"tr={first.locator('tr').count()} td={first.locator('td').count()}")
        # ------------------------------------------------

        items = []
        for block in page.locator('#dis_dtl div[id^="PO-"]').all():
            po = block.get_attribute("id")  # e.g. PO-A000080688
            headers = [clean(h) for h in block.locator("th").all_inner_texts()]
            if not headers:
                continue
            for row in block.locator("tr").all():
                cells = [clean(c) for c in row.locator("td").all_inner_texts()]
                if len(cells) < len(headers):
                    continue  # header/info rows
                rec = dict(zip(headers, cells))
                item, size = col(rec, "ItemNo"), col(rec, "Size")
                if not item:
                    continue
                items.append({
                    # hashed, so a public seen_orders.json reveals nothing readable
                    "key": hashlib.sha256(f"{po}|{item}|{size}".encode()).hexdigest()[:16],
                    "po": po,
                    "item": item,
                    "size": size,
                    "pending": to_int(col(rec, "Pending")),
                    "delivery": col(rec, "Delivery"),
                })

                print(f"DEBUG: items parsed = {len(items)}")
        browser.close()
        return items


def send_telegram(po, group):
    total = sum(i["pending"] for i in group)
    lines = [
        f"🆕 New order {po}",
        f"{len(group)} item(s), total qty {total}, deliver by {group[0]['delivery']}",
        "",
    ]
    for i in group[:20]:
        lines.append(f"• {i['item']} ({i['size']}) – qty {i['pending']}")
    if len(group) > 20:
        lines.append(f"…and {len(group) - 20} more")
    text = "\n".join(lines)

    token = os.environ["TG_TOKEN"]
    for chat_id in os.environ["TG_CHAT_IDS"].split(","):
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id.strip(), "text": text},
            timeout=30,
        )
        if not r.ok:
            raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")


def main():
    items = fetch_items()
    seen = load_seen()

    if seen is None:  # first run: existing orders yaad rakho, purane ke alerts mat bhejo
        save_seen({i["key"] for i in items})  # items khali ho tab bhi file banegi
        print(f"First run: recorded {len(items)} existing order lines.")
        return

    by_po = OrderedDict()
    for i in items:
        if i["key"] not in seen:
            by_po.setdefault(i["po"], []).append(i)

    for po, group in by_po.items():
        send_telegram(po, group)
        seen |= {i["key"] for i in group}
        save_seen(seen)  # save after each send so a failure never causes duplicates
        print("Notified 1 order.")  # no order details in logs (repo may be public)

    print(f"Checked {len(items)} order lines, {len(by_po)} new order(s).")


if __name__ == "__main__":
    main()
