#!/usr/bin/env python3
"""Standalone Chartink scanner -> Discord alerts and Chartink snapshot images.

This process is intentionally independent of Diamond-Bot and never places trades.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import signal
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration (environment variables; no Google Colab dependency)
# ---------------------------------------------------------------------------
RUN_SINGLE_CHART_TEST = os.getenv("RUN_SINGLE_CHART_TEST", "false").lower() in {"1", "true", "yes", "on"}
TEST_STOCK = os.getenv("TEST_STOCK", "GAIL").strip().upper()
SCAN_INTERVAL = max(30, int(os.getenv("SCAN_INTERVAL", "300")))
REQUEST_TIMEOUT = max(5, int(os.getenv("REQUEST_TIMEOUT", "15")))
ERROR_RETRY_INTERVAL = max(10, int(os.getenv("ERROR_RETRY_INTERVAL", "60")))
CHART_WIDTH = max(800, int(os.getenv("CHART_WIDTH", "1800")))
CHART_HEIGHT = max(600, int(os.getenv("CHART_HEIGHT", "1100")))
CHARTINK_WAIT_SECONDS = max(1, int(os.getenv("CHARTINK_WAIT_SECONDS", "10")))
CHART_MAX_RETRIES = max(1, int(os.getenv("CHART_MAX_RETRIES", "2")))
BASELINE_ON_FIRST_SCAN = os.getenv("BASELINE_ON_FIRST_SCAN", "false").lower() in {"1", "true", "yes", "on"}
STATE_FILE = Path(os.getenv("STATE_FILE", "./data/last_matches.json")).expanduser()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
CHARTINK_URL = "https://chartink.com/screener/process"
CHARTINK_SCREENER_PAGE = "https://chartink.com/screener"
IST = ZoneInfo("Asia/Kolkata")

# Preserved from the supplied Colab version.
CHARTINK_SNAPSHOT_URL = (
    "https://chartink.com/stocks-new?"
    "load-snapshot=relative-strength-index-chart-snapshot-2139"
)

# Original scan clause preserved as supplied.
SCAN_CLAUSE = (
    "( {cash} ( ( {cash} "
    "( daily rsi( 50 ) > "
    "daily ema( daily rsi( 50 ) , 23 ) "
    "and 1 day ago rsi( 50 ) <= "
    "1 day ago ema( daily rsi( 50 ) , 23 ) "
    "and weekly rsi( 50 ) > "
    "weekly ema( weekly rsi( 50 ) , 23 ) "
    "and market cap > 2500 "
    "and daily close > 30 "
    "and 1 day ago ema( daily rsi( 50 ) , 23 ) <= "
    "2 days ago ema( daily rsi( 50 ) , 23 ) "
    "and 2 days ago ema( daily rsi( 50 ) , 23 ) <= "
    "3 days ago ema( daily rsi( 50 ) , 23 ) "
    "and 3 days ago ema( daily rsi( 50 ) , 23 ) <= "
    "4 days ago ema( daily rsi( 50 ) , 23 ) "
    "and 4 days ago ema( daily rsi( 50 ) , 23 ) <= "
    "5 days ago ema( daily rsi( 50 ) , 23 ) "
    "and 5 days ago ema( daily rsi( 50 ) , 23 ) <= "
    "6 days ago ema( daily rsi( 50 ) , 23 ) "
    "and 6 days ago ema( daily rsi( 50 ) , 23 ) <= "
    "7 days ago ema( daily rsi( 50 ) , 23 ) "
    "and 7 days ago ema( daily rsi( 50 ) , 23 ) <= "
    "8 days ago ema( daily rsi( 50 ) , 23 ) "
    "and daily close > "
    "daily max( 250 , daily high ) * 0.75 "
    ") ) ) )"
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("chartink-discord")
chart_queue: queue.Queue[str] = queue.Queue()
stop_event = threading.Event()


def now_ist() -> datetime:
    return datetime.now(IST)


def get_ist_time() -> str:
    return now_ist().strftime("%I:%M %p IST")


def validate_config() -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError(
            "DISCORD_WEBHOOK_URL is missing. Set it in the server environment "
            "or in the local .env file; never commit the real webhook URL to GitHub."
        )
    if not DISCORD_WEBHOOK_URL.startswith("https://"):
        raise RuntimeError("DISCORD_WEBHOOK_URL must be an HTTPS URL.")


def test_discord_connection() -> bool:
    payload = {
        "username": "Chartink Scanner",
        "content": "🟢 **Chartink Scanner Connected**\nChartink Snapshot #2139 chart system ready.",
    }
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code in (200, 204):
            log.info("Discord webhook connection successful; test message delivered.")
            return True
        log.error("Discord connection test failed: HTTP %s: %s", response.status_code, response.text[:500])
    except requests.RequestException as exc:
        log.exception("Discord connection test error: %s", exc)
    return False


def get_chartink_stocks(clause: str) -> Optional[set[str]]:
    """Return a set on a successful scan; None means the scan itself failed."""
    try:
        with requests.Session() as session:
            page_response = session.get(CHARTINK_SCREENER_PAGE, timeout=REQUEST_TIMEOUT)
            if page_response.status_code != 200:
                log.error("Chartink screener page HTTP %s", page_response.status_code)
                return None

            soup = BeautifulSoup(page_response.text, "html.parser")
            csrf_element = soup.select_one("meta[name='csrf-token']")
            if not csrf_element or not csrf_element.get("content"):
                log.error("Chartink CSRF token was not found; site response may have changed.")
                return None

            response = session.post(
                CHARTINK_URL,
                headers={
                    "X-CSRF-TOKEN": csrf_element["content"],
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": CHARTINK_SCREENER_PAGE,
                },
                data={"scan_clause": clause},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code != 200:
                log.error("Chartink scanner HTTP %s: %s", response.status_code, response.text[:300])
                return None

            try:
                json_data = response.json()
            except ValueError:
                log.error("Chartink scanner response was not valid JSON.")
                return None

            rows = json_data.get("data")
            if not isinstance(rows, list):
                log.error("Chartink response did not contain a list in 'data'.")
                return None

            stocks: set[str] = set()
            for item in rows:
                if isinstance(item, dict) and item.get("nsecode"):
                    symbol = str(item["nsecode"]).strip().upper()
                    if symbol:
                        stocks.add(symbol)
            return stocks
    except requests.RequestException as exc:
        log.warning("Chartink request failed: %s", exc)
        return None
    except Exception:
        log.exception("Unexpected Chartink scan error")
        return None


def send_normal_discord_alert(stocks: set[str], current_time: str) -> bool:
    """Send stock names in Discord-safe-sized chunks and report aggregate success."""
    if not stocks:
        return True
    symbols = sorted(stocks)
    # Leave room for heading/time so every content message stays below Discord's 2000 chars.
    chunks: list[list[str]] = []
    chunk: list[str] = []
    chunk_len = 0
    for symbol in symbols:
        extra = len(symbol) + (2 if chunk else 0)
        if chunk and chunk_len + extra > 1600:
            chunks.append(chunk)
            chunk = []
            chunk_len = 0
            extra = len(symbol)
        chunk.append(symbol)
        chunk_len += extra
    if chunk:
        chunks.append(chunk)

    all_ok = True
    for index, group in enumerate(chunks, start=1):
        heading = "🚨 **RSI Breakout Alert**"
        if len(chunks) > 1:
            heading += f" (part {index}/{len(chunks)})"
        payload = {
            "username": "Chartink Scanner",
            "content": f"{heading}\n\n📈 **Stocks:** {', '.join(group)}\n⏰ **Time:** {current_time}",
        }
        try:
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
            if response.status_code in (200, 204):
                log.info("Discord alert sent for: %s", ", ".join(group))
            else:
                all_ok = False
                log.error("Discord alert failed: HTTP %s: %s", response.status_code, response.text[:500])
        except requests.RequestException:
            all_ok = False
            log.exception("Discord alert request failed for %s", ", ".join(group))
    return all_ok


def build_chartink_url(stock: str) -> str:
    stock = str(stock).strip().upper()
    return f"{CHARTINK_SNAPSHOT_URL}&symbol={quote(stock, safe='')}"


def create_chartink_chart(stock: str) -> Optional[str]:
    stock = str(stock).strip().upper()
    url = build_chartink_url(stock)
    output_file = f"/tmp/chartink_{stock}_{int(time.time() * 1000)}.png"
    log.info("Creating Chartink chart for NSE:%s (%s)", stock, url)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                channel="chromium",
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            try:
                context = browser.new_context(
                    viewport={"width": CHART_WIDTH, "height": CHART_HEIGHT},
                    device_scale_factor=1,
                    locale="en-IN",
                )
                try:
                    page = context.new_page()
                    page.set_extra_http_headers({"Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8"})
                    page.on("pageerror", lambda error: log.warning("Browser page error for %s: %s", stock, error))
                    response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    if response and response.status >= 400:
                        log.error("Chartink chart page returned HTTP %s for %s", response.status, stock)
                        return None
                    page.wait_for_timeout(CHARTINK_WAIT_SECONDS * 1000)
                    page.evaluate("() => window.scrollTo(0, 0)")
                    page.wait_for_timeout(500)
                    page.screenshot(path=output_file, full_page=False)
                finally:
                    context.close()
            finally:
                browser.close()
        if not os.path.exists(output_file):
            log.error("Screenshot was not created for %s", stock)
            return None
        size = os.path.getsize(output_file)
        if size < 10_000:
            log.error("Screenshot for %s looks too small (%s bytes)", stock, size)
            try:
                os.remove(output_file)
            except OSError:
                pass
            return None
        log.info("Screenshot created for NSE:%s (%s bytes)", stock, size)
        return output_file
    except Exception:
        log.exception("Chartink chart error for %s", stock)
        return None


def send_chart_to_discord(stock: str, image_path: str) -> bool:
    stock = str(stock).strip().upper()
    caption = f"📊 **Chartink Chart**\n**NSE:{stock}**\n📈 RSI Snapshot: `2139`\n⏰ {get_ist_time()}"
    try:
        with open(image_path, "rb") as image_file:
            response = requests.post(
                DISCORD_WEBHOOK_URL,
                data={"username": "Chartink Charts", "content": caption},
                files={"file": (f"NSE_{stock}_Chartink.png", image_file, "image/png")},
                timeout=30,
            )
        if response.status_code in (200, 204):
            log.info("Chart uploaded to Discord for NSE:%s", stock)
            return True
        log.error("Chart upload failed for %s: HTTP %s: %s", stock, response.status_code, response.text[:500])
    except (OSError, requests.RequestException):
        log.exception("Discord chart upload error for %s", stock)
    return False


def process_chart(stock: str) -> bool:
    stock = str(stock).strip().upper()
    for attempt in range(1, CHART_MAX_RETRIES + 1):
        log.info("Chart for NSE:%s attempt %s/%s", stock, attempt, CHART_MAX_RETRIES)
        image_path = create_chartink_chart(stock)
        if image_path:
            try:
                if send_chart_to_discord(stock, image_path):
                    return True
            finally:
                try:
                    if os.path.exists(image_path):
                        os.remove(image_path)
                except OSError:
                    log.warning("Could not remove temporary image %s", image_path)
        if attempt < CHART_MAX_RETRIES:
            time.sleep(3)
    log.error("Chart failed permanently for NSE:%s", stock)
    return False


def chart_worker() -> None:
    log.info("Chart worker started")
    while not stop_event.is_set():
        try:
            stock = chart_queue.get(timeout=1)
        except queue.Empty:
            continue
        try:
            process_chart(stock)
        except Exception:
            log.exception("Unexpected chart worker error for %s", stock)
        finally:
            chart_queue.task_done()


def load_state() -> tuple[set[str], bool]:
    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            raw = json.load(file)
        stocks = raw.get("last_matches", [])
        if not isinstance(stocks, list):
            raise ValueError("last_matches must be a list")
        return {str(s).strip().upper() for s in stocks if str(s).strip()}, bool(raw.get("initialized", True))
    except FileNotFoundError:
        return set(), False
    except Exception:
        log.exception("Could not read state file %s; starting with an empty state", STATE_FILE)
        return set(), False


def save_state(stocks: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    payload = {"initialized": True, "last_matches": sorted(stocks), "saved_at_ist": now_ist().isoformat()}
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)
        file.write("\n")
    os.replace(temporary_path, STATE_FILE)


def run_single_chart_test() -> bool:
    log.info("Running single chart test for NSE:%s; scanner loop will not start", TEST_STOCK)
    return process_chart(TEST_STOCK)


def request_shutdown(signum: int, _frame: object) -> None:
    log.info("Received signal %s; shutting down", signum)
    stop_event.set()


def run_scanner() -> None:
    previous_matches, initialized = load_state()
    worker = threading.Thread(target=chart_worker, name="chart-worker", daemon=True)
    worker.start()
    log.info("Full scanner started; polling every %s seconds. No trading orders are placed.", SCAN_INTERVAL)
    if BASELINE_ON_FIRST_SCAN and not initialized:
        log.info("BASELINE_ON_FIRST_SCAN=true: first successful scan will establish the baseline without alerts.")

    while not stop_event.is_set():
        started = time.monotonic()
        current_time_string = now_ist().strftime("%I:%M %p IST")
        log.info("Checking Chartink for new setups")
        current_matches = get_chartink_stocks(SCAN_CLAUSE)
        if current_matches is None:
            log.warning("Scan failed; keeping saved matching state unchanged and retrying soon")
            wait_seconds = ERROR_RETRY_INTERVAL
        else:
            log.info("Current matching stocks: %s", len(current_matches))
            if BASELINE_ON_FIRST_SCAN and not initialized:
                try:
                    save_state(current_matches)
                    previous_matches = set(current_matches)
                    initialized = True
                    log.info("Initial baseline saved; no alerts sent for this scan")
                except OSError:
                    log.exception("Failed to save initial baseline")
                wait_seconds = SCAN_INTERVAL
            else:
                new_stocks = current_matches - previous_matches
                alert_ok = True
                if new_stocks:
                    log.info("New stocks detected: %s", ", ".join(sorted(new_stocks)))
                    alert_ok = send_normal_discord_alert(new_stocks, current_time_string)
                    if alert_ok:
                        for stock in sorted(new_stocks):
                            chart_queue.put(stock)
                        log.info("Queued %s chart(s); pending queue=%s", len(new_stocks), chart_queue.qsize())
                    else:
                        log.warning("Normal Discord alert did not fully succeed; failed/new symbols will be retried")
                if alert_ok:
                    # Save only after the normal alert has succeeded. Persisting the *current* set also
                    # allows a stock to alert again if it later leaves the scan and re-enters.
                    try:
                        save_state(current_matches)
                        previous_matches = set(current_matches)
                        initialized = True
                    except OSError:
                        log.exception("Could not save scanner state; a restart may cause duplicate alerts")
                else:
                    # Drop symbols that left the scan, but do not mark failed new symbols as alerted.
                    previous_matches = previous_matches.intersection(current_matches)
                    try:
                        save_state(previous_matches)
                    except OSError:
                        log.exception("Could not save partial scanner state")
                wait_seconds = SCAN_INTERVAL

        elapsed = time.monotonic() - started
        stop_event.wait(max(1, wait_seconds - int(elapsed)))

    log.info("Scanner loop stopped. Queued charts not yet completed: %s", chart_queue.qsize())


def main() -> int:
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    validate_config()
    if not test_discord_connection():
        log.error("Discord connection test failed; stopping to avoid silent operation")
        return 1
    if RUN_SINGLE_CHART_TEST:
        return 0 if run_single_chart_test() else 1
    run_scanner()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
