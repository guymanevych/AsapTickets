#!/usr/bin/env python3
"""
main.py

Checks a single Viagogo event page for listings that match a section
keyword (e.g. "standing"), a minimum ticket quantity, and an "instant
download" style delivery method. If a match is found, emails an alert.

Designed to be run on a schedule (cron, Windows Task Scheduler, or a
GitHub Actions workflow) — it does ONE check per run and exits. It does
not loop or sleep internally.

--- IMPORTANT, READ BEFORE RELYING ON THIS ---
Viagogo is a JavaScript-heavy site and this script has not been tested
against the live page (I don't have a way to browse it interactively).
It works by:
  1. Loading the page in a real (headless) browser.
  2. Capturing every JSON network response the page makes, since sites
     like this usually load ticket listings via a background API call.
  3. Capturing any JSON blobs embedded directly in the page's HTML
     (common patterns like `__NEXT_DATA__` or `window.__INITIAL_STATE__`).
  4. Searching all of that captured data for objects that look like a
     ticket listing: something with a quantity/count field, section text
     containing your keyword, and delivery text implying instant/e-ticket.

Because I can't verify Viagogo's actual data shape, run this once with
`--debug` first (see README.md) and check debug_capture.json to confirm
real listings are being captured, and adjust SECTION_KEYWORD / the
matching logic in `find_matches()` if needed.

Also worth knowing going in: ticket resale sites commonly use bot
detection (Cloudflare, PerimeterX, DataDome, etc.). This script may get
blocked, especially when run from a cloud provider's IP range (like
GitHub Actions runners). If checks start silently failing, that's the
most likely reason — see README.md for mitigation ideas.
"""

import argparse
import asyncio
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Configuration (all read from environment variables so no secrets live in
# this file — see README.md for how to set these, locally or as GitHub
# Actions secrets)
# ---------------------------------------------------------------------------

VIAGOGO_URL = "https://www.viagogo.com/ww/Concert-Tickets/Rap-and-Hip-Hop-Music/A-AP-Rocky-Tickets/E-161267796?lt=32.08&lg=34.781&quantity=4"
SECTION_KEYWORD = os.environ.get("SECTION_KEYWORD", "standing")
MIN_QUANTITY = int(os.environ.get("MIN_QUANTITY", "4"))

END_DATETIME_STR = os.environ.get("END_DATETIME", "2026-10-05T20:00:00")
END_TZ = os.environ.get("END_TZ", "Asia/Jerusalem")

SMTP_SERVER = os.environ.get("SMTP_SERVER", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
ALERT_TO = os.environ.get("ALERT_TO", "")

STATE_FILE = os.environ.get("STATE_FILE", "last_alert.json")
COOLDOWN_HOURS = float(os.environ.get("ALERT_COOLDOWN_HOURS", "2"))

INSTANT_KEYWORDS = ["instant", "e-ticket", "eticket", "mobile transfer", "instant download"]
QUANTITY_KEYS = ["quantity", "ticketcount", "availablequantity", "qty", "numberoftickets", "maxquantity"]


# ---------------------------------------------------------------------------
# Balanced-brace JSON extraction (regex alone can't reliably find the end
# of a nested JSON object, so we walk the string and track brace depth)
# ---------------------------------------------------------------------------

def _extract_balanced_json(text: str, start_idx: int):
    depth = 0
    in_str = False
    esc = False
    for i in range(start_idx, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start_idx : i + 1]
    return None


def extract_inline_json_blobs(html: str):
    """Find JSON embedded directly in <script type="application/json"> tags
    or `window.__SOMETHING__ = {...}` assignments."""
    blobs = []

    for m in re.finditer(r'<script[^>]+type=["\']application/json["\'][^>]*>', html):
        start = html.find("{", m.end())
        if start == -1:
            continue
        blob_str = _extract_balanced_json(html, start)
        if blob_str:
            try:
                blobs.append(json.loads(blob_str))
            except Exception:
                pass

    for m in re.finditer(r"window\.__[A-Za-z0-9_]+__\s*=\s*", html):
        start = html.find("{", m.end())
        if start == -1:
            continue
        blob_str = _extract_balanced_json(html, start)
        if blob_str:
            try:
                blobs.append(json.loads(blob_str))
            except Exception:
                pass

    return blobs


# ---------------------------------------------------------------------------
# Browser fetch: load the page, capture network JSON + inline JSON
# ---------------------------------------------------------------------------

async def fetch_captured_data(debug: bool = False):
    captured = []
    network_urls = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = await context.new_page()

        async def on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if "application/json" not in ct:
                    return
                network_urls.append(response.url)
                body = await response.json()
                captured.append(body)
            except Exception:
                pass

        page.on("response", on_response)

        await page.goto(VIAGOGO_URL, wait_until="networkidle", timeout=45000)
        # Give lazy-loaded XHRs a little extra time to fire and resolve.
        await page.wait_for_timeout(4000)

        html = await page.content()
        captured.extend(extract_inline_json_blobs(html))

        if debug:
            with open("debug_capture.json", "w") as f:
                json.dump({"network_json_urls": network_urls, "blobs": captured}, f, indent=2)
            with open("debug_page.html", "w") as f:
                f.write(html)
            print(f"[debug] Captured {len(captured)} JSON blob(s) from {len(network_urls)} network response(s).")
            print("[debug] Wrote debug_capture.json and debug_page.html for inspection.")

        await browser.close()

    return captured


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

def find_matches(blobs, section_keyword: str, min_quantity: int):
    matches = []
    seen = set()

    def walk(node):
        if isinstance(node, dict):
            text_blob = json.dumps(node).lower()
            has_section = section_keyword.lower() in text_blob
            is_instant = any(k in text_blob for k in INSTANT_KEYWORDS)

            qty = None
            for key, val in node.items():
                if key.lower() in QUANTITY_KEYS and isinstance(val, (int, float)):
                    qty = int(val)
                    break

            if has_section and is_instant and qty is not None and qty >= min_quantity:
                fingerprint = json.dumps(node, sort_keys=True)
                if fingerprint not in seen:
                    seen.add(fingerprint)
                    matches.append(node)

            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for blob in blobs:
        walk(blob)

    return matches


# ---------------------------------------------------------------------------
# Email alert
# ---------------------------------------------------------------------------

def send_alert(matches):
    lines = [
        f"Found {len(matches)} listing(s) matching your criteria "
        f"('{SECTION_KEYWORD}', qty >= {MIN_QUANTITY}, instant delivery):",
        "",
        f"Check it now: {VIAGOGO_URL}",
        "",
        "Raw listing data (for your reference):",
    ]
    for i, m in enumerate(matches, 1):
        lines.append(f"--- Listing {i} ---")
        lines.append(json.dumps(m, indent=2)[:1000])
        lines.append("")

    body = "\n".join(lines)
    msg = MIMEText(body)
    msg["Subject"] = "Tickets found: A$AP Rocky Budapest — standing, instant download"
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_TO

    # with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
    #     server.starttls()
    #     server.login(SMTP_USER, SMTP_PASS)
    #     server.sendmail(SMTP_USER, [ALERT_TO], msg.as_string())


# ---------------------------------------------------------------------------
# State (so we don't email you every 30 minutes once tickets appear)
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_alert_ts": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ---------------------------------------------------------------------------
# Deadline check
# ---------------------------------------------------------------------------

def past_deadline() -> bool:
    end = datetime.fromisoformat(END_DATETIME_STR).replace(tzinfo=ZoneInfo(END_TZ))
    now = datetime.now(ZoneInfo(END_TZ))
    return now >= end


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save all captured JSON + page HTML to disk instead of alerting. "
        "Run this once manually first to sanity-check what's being captured.",
    )
    parser.add_argument(
        "--force-alert",
        action="store_true",
        help="Send a test email immediately, bypassing the live check (to confirm SMTP works).",
    )
    args = parser.parse_args()

    if not VIAGOGO_URL and not args.force_alert:
        print("ERROR: VIAGOGO_URL is not set.", file=sys.stderr)
        sys.exit(1)

    if args.force_alert:
        send_alert([{"note": "This is a test alert triggered by --force-alert."}])
        print("Test alert sent.")
        return

    if past_deadline():
        print(f"Past END_DATETIME ({END_DATETIME_STR} {END_TZ}) — not checking. Nothing to do.")
        return

    try:
        blobs = await fetch_captured_data(debug=args.debug)
    except Exception as e:
        print(f"ERROR during page fetch: {e}", file=sys.stderr)
        return

    if args.debug:
        return

    matches = find_matches(blobs, SECTION_KEYWORD, MIN_QUANTITY)

    if not matches:
        print("No matching listings this run.")
        return

    state = load_state()
    now_ts = datetime.now(ZoneInfo(END_TZ)).timestamp()
    last_ts = state.get("last_alert_ts")
    if last_ts and (now_ts - last_ts) < COOLDOWN_HOURS * 3600:
        print(
            f"Match found ({len(matches)} listing(s)) but an alert was already sent within "
            f"the last {COOLDOWN_HOURS}h — skipping to avoid spamming."
        )
        return

    send_alert(matches)
    state["last_alert_ts"] = now_ts
    save_state(state)
    print(f"ALERT SENT — {len(matches)} matching listing(s).")


if __name__ == "__main__":
    asyncio.run(main())