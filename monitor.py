#!/usr/bin/env python3
"""
2dehands.be new-listing monitor.

Run once per check cycle (invoked by a GitHub Actions schedule, see
.github/workflows/monitor.yml - or a systemd timer if self-hosted, see
deploy/systemd/). For each category in config.yaml:
  - fetch current listings (internal JSON API, HTML fallback)
  - diff against the "seen" store (a JSON file, so a CI workflow can
    persist it by committing it back to the repo between runs)
  - notify via Telegram for anything genuinely new
  - on the very first run for a category, seed the store silently

See README.md for setup/deployment instructions.
"""

import json
import logging
import os
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from dotenv import load_dotenv

LOCAL_TZ = ZoneInfo("Europe/Brussels")

# --------------------------------------------------------------------------
# Config / constants
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

CONFIG_PATH = Path(os.environ.get("MONITOR_CONFIG", BASE_DIR / "config.yaml"))
STATE_PATH = Path(os.environ.get("MONITOR_STATE_FILE", BASE_DIR / "data" / "seen.json"))

# How long to keep a "seen" listing ID around before pruning it, so the
# state file (which gets committed to git every run) doesn't grow forever.
# Marketplace listings essentially never stay live this long, so this is
# just a safety net against unbounded growth, not a real dedup risk.
PRUNE_SEEN_AFTER_DAYS = 180

# How often to force a state commit even when nothing changed, purely so
# the repo never goes fully quiet - GitHub auto-disables scheduled
# workflows after 60 days of zero repository activity. 24h leaves a huge
# safety margin while cutting steady-state commit noise from ~144/day to 1/day.
HEARTBEAT_INTERVAL = timedelta(hours=24)

BASE_URL = "https://www.2dehands.be"
SEARCH_API_PATH = "/lrp/api/search"
API_RESULT_LIMIT = 60

REQUEST_TIMEOUT_SECONDS = 15
RETRY_DELAY_RANGE_SECONDS = (10, 20)
CATEGORY_STAGGER_RANGE_SECONDS = (5, 10)

# After this many consecutive fully-failed cycles (every category failed),
# send one "monitoring is down" alert. At 10 min between cycles this is
# roughly an hour.
FAILURE_ALERT_THRESHOLD = 6

# Only positively-confirmed business/professional sellers are filtered out.
# 2dehands also has an "UNKNOWN" seller type covering both unclassified
# private sellers and some businesses - we'd rather risk showing an
# occasional business listing than hiding real private-seller listings.
EXCLUDED_SELLER_TYPES = {"TRADER"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8",
}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

logging.basicConfig(
    level=os.environ.get("MONITOR_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("2dehands-monitor")


class FetchError(Exception):
    """Raised when a category's listings couldn't be fetched/parsed at all."""


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------

def load_categories():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    categories = data.get("categories") or []
    if not categories:
        raise RuntimeError(f"No categories configured in {CONFIG_PATH}")
    for c in categories:
        if not c.get("name") or not c.get("url"):
            raise RuntimeError(f"Category entry missing name/url: {c!r}")
    return categories


# --------------------------------------------------------------------------
# State store (JSON file - so a stateless CI runner can persist it by
# committing it back to the repo between runs; see .github/workflows/)
# --------------------------------------------------------------------------

def load_store():
    if not STATE_PATH.exists():
        return {"monitor_state": {}, "categories": {}}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("monitor_state", {})
    data.setdefault("categories", {})
    return data


def save_store(store):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False, sort_keys=True)
    tmp_path.replace(STATE_PATH)


def get_state(store, key, default=None):
    return store["monitor_state"].get(key, default)


def set_state(store, key, value):
    store["monitor_state"][key] = value


def is_category_initialized(store, category):
    return category in store["categories"]


def mark_category_initialized(store, category):
    store["categories"].setdefault(category, {"initialized_at": now_iso(), "seen": {}})
    store["categories"][category]["initialized_at"] = now_iso()


def get_seen_ids(store, category):
    return set(store["categories"].get(category, {}).get("seen", {}).keys())


def save_listing(store, category, item, detected_at=None):
    cat = store["categories"].setdefault(category, {"initialized_at": now_iso(), "seen": {}})
    cat["seen"].setdefault(
        item["item_id"],
        {
            "title": item["title"],
            "price_display": item["price_display"],
            "url": item["url"],
            "first_seen_at": detected_at or now_iso(),
        },
    )


def prune_old_entries(store):
    cutoff = datetime.now(timezone.utc) - timedelta(days=PRUNE_SEEN_AFTER_DAYS)
    for category, cat_data in store["categories"].items():
        seen = cat_data.get("seen", {})
        to_delete = []
        for item_id, info in seen.items():
            first_seen_at = info.get("first_seen_at")
            if not first_seen_at:
                continue
            try:
                seen_at = datetime.fromisoformat(first_seen_at)
            except ValueError:
                continue
            if seen_at < cutoff:
                to_delete.append(item_id)
        for item_id in to_delete:
            del seen[item_id]
        if to_delete:
            log.info("%r: pruned %d listings older than %d days", category, len(to_delete), PRUNE_SEEN_AFTER_DAYS)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def format_local_time(iso_str):
    """Render a stored UTC ISO timestamp as a friendly Belgium-local time."""
    dt = datetime.fromisoformat(iso_str).astimezone(LOCAL_TZ)
    return dt.strftime("%d %b %Y, %H:%M")


# --------------------------------------------------------------------------
# Fetching / parsing 2dehands
# --------------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)


def http_get(session, url, params=None, accept_json=False):
    headers = dict(REQUEST_HEADERS)
    if accept_json:
        headers["Accept"] = "application/json"
    last_error = None
    for attempt in (1, 2):
        try:
            resp = session.get(
                url, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS
            )
            if resp.status_code == 200:
                return resp
            last_error = FetchError(f"HTTP {resp.status_code} for {url}")
            if resp.status_code in (429,) or resp.status_code >= 500:
                pass  # transient, worth a retry
            else:
                break  # e.g. 404 - retrying won't help
        except requests.exceptions.RequestException as exc:
            last_error = FetchError(f"{type(exc).__name__}: {exc}")

        if attempt == 1:
            delay = random.uniform(*RETRY_DELAY_RANGE_SECONDS)
            log.warning("Request to %s failed (%s), retrying in %.0fs", url, last_error, delay)
            time.sleep(delay)

    raise last_error


def extract_next_data(html):
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise FetchError("__NEXT_DATA__ script tag not found (page layout may have changed)")
    return json.loads(m.group(1))


def normalize_item(raw):
    """Turn a raw listing dict from 2dehands JSON into our compact shape."""
    item_id = raw.get("itemId")
    title = raw.get("title") or "(geen titel)"
    vip_url = raw.get("vipUrl") or ""
    url = urllib.parse.urljoin(BASE_URL, vip_url)

    price_info = raw.get("priceInfo") or {}
    price_display = format_price(price_info)

    image_url = None
    pictures = raw.get("pictures") or []
    if pictures:
        pic = pictures[0]
        image_url = (
            pic.get("extraExtraLargeUrl") or pic.get("largeUrl") or pic.get("mediumUrl")
        )
    if not image_url:
        image_urls = raw.get("imageUrls") or []
        if image_urls:
            image_url = image_urls[0]
            if image_url.startswith("//"):
                image_url = "https:" + image_url

    return {
        "item_id": item_id,
        "title": title,
        "url": url,
        "price_display": price_display,
        "image_url": image_url,
    }


def format_price(price_info):
    price_type = price_info.get("priceType")
    cents = price_info.get("priceCents")

    def euros(c):
        return f"€ {c / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    if price_type == "FIXED" and cents is not None:
        return euros(cents)
    if price_type in ("MIN_BID", "FAST_BID") and cents is not None:
        return f"Bieden vanaf {euros(cents)}"
    if price_type == "FREE":
        return "Gratis"
    if price_type == "ON_REQUEST":
        return "Prijs op aanvraag"
    if price_type == "SEE_DESCRIPTION":
        return "Zie omschrijving"
    return "Prijs onbekend"


def extract_candidate_items(payload):
    """Union of the main listings and any promoted/top-block items, deduped."""
    items = list(payload.get("listings") or [])
    items += list(payload.get("topBlock") or [])
    seen_ids = set()
    result = []
    for raw in items:
        item_id = raw.get("itemId")
        if not item_id or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        result.append(normalize_item(raw))
    return result


def fetch_category_listings(session, category_url):
    """
    Primary path: fetch the (possibly filtered) category page, extract the
    resolved search params from its embedded __NEXT_DATA__, then replay
    those params against the internal JSON search API with newest-first
    sorting and a larger page size - this is what actually guarantees
    chronological ordering (the plain page route ignores sort query params).

    Fallback path: if the API call fails for any reason (endpoint changed,
    blocked, bad response), fall back to whatever the page itself embedded,
    which is still correct just not guaranteed to be strictly newest-first.
    """
    resp = http_get(session, category_url)
    next_data = extract_next_data(resp.text)

    try:
        page_props = next_data["props"]["pageProps"]
        search_response = page_props["searchRequestAndResponse"]
        base_query = dict(next_data.get("query") or {})
    except (KeyError, TypeError) as exc:
        raise FetchError(f"Unexpected page data shape: {exc}")

    if not base_query.get("l1CategoryId"):
        raise FetchError("Could not determine category from page data")

    try:
        api_params = dict(base_query)
        api_params["sortBy"] = "SORT_INDEX"
        api_params["sortOrder"] = "DECREASING"
        api_params["limit"] = str(API_RESULT_LIMIT)
        api_params["offset"] = "0"
        api_resp = http_get(
            session,
            BASE_URL + SEARCH_API_PATH,
            params=api_params,
            accept_json=True,
        )
        api_payload = api_resp.json()
        return extract_candidate_items(api_payload)
    except (FetchError, ValueError) as exc:
        log.warning(
            "Sorted API fetch failed (%s), falling back to embedded page listings", exc
        )
        return extract_candidate_items(search_response)


_SELLER_TYPE_RE = re.compile(r'"sellerType":"(\w+)"')


def fetch_seller_type(session, listing_url):
    """
    CONSUMER/TRADER/UNKNOWN - only present on the individual listing page,
    not in search results, so this is only called for genuinely new
    listings (a handful per cycle at most), not every listing checked.
    Returns None if it can't be determined (network error or the page
    layout changed) - callers should treat that as "don't filter out",
    consistent with only excluding positively-confirmed traders.
    """
    try:
        resp = http_get(session, listing_url)
    except FetchError as exc:
        log.warning("Could not fetch seller type for %s: %s", listing_url, exc)
        return None
    m = _SELLER_TYPE_RE.search(resp.text)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# Telegram notifications
# --------------------------------------------------------------------------

def telegram_configured():
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def telegram_api_url(method):
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def send_telegram_message(session, text):
    if not telegram_configured():
        log.warning("Telegram not configured, skipping notification: %s", text)
        return
    try:
        session.post(
            telegram_api_url("sendMessage"),
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        ).raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.error("Failed to send Telegram message: %s", exc)


def notify_new_listing(session, category, item, detected_at):
    caption = (
        f"📂 {escape_html(category)}\n"
        f"<b>{escape_html(item['title'])}</b>\n"
        f"{escape_html(item['price_display'])}\n"
        f"🕒 Gevonden: {format_local_time(detected_at)}\n"
        f"{item['url']}"
    )
    if not telegram_configured():
        log.warning("Telegram not configured, skipping notification: %s", caption)
        return
    try:
        if item.get("image_url"):
            resp = session.post(
                telegram_api_url("sendPhoto"),
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "photo": item["image_url"],
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if resp.status_code != 200:
                # e.g. Telegram couldn't fetch that image URL - fall back to text.
                log.warning(
                    "sendPhoto failed (%s), falling back to text message", resp.text[:200]
                )
                send_telegram_message(session, caption)
        else:
            send_telegram_message(session, caption)
    except requests.exceptions.RequestException as exc:
        log.error("Failed to send Telegram notification: %s", exc)


def escape_html(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------
# Outage alerting
# --------------------------------------------------------------------------

def handle_cycle_outcome(store, session, any_category_succeeded):
    if any_category_succeeded:
        alert_sent = get_state(store, "outage_alert_sent", False)
        if alert_sent:
            send_telegram_message(
                session,
                "✅ 2dehands monitor is back up and checking normally again.",
            )
        set_state(store, "consecutive_failed_cycles", 0)
        set_state(store, "outage_alert_sent", False)
        return

    failures = int(get_state(store, "consecutive_failed_cycles", 0)) + 1
    set_state(store, "consecutive_failed_cycles", failures)
    if failures == 1:
        set_state(store, "first_failure_at", now_iso())

    log.error("All categories failed this cycle (%d consecutive failed cycles)", failures)

    alert_sent = get_state(store, "outage_alert_sent", False)
    if failures >= FAILURE_ALERT_THRESHOLD and not alert_sent:
        first_failure_at = get_state(store, "first_failure_at", "unknown time")
        send_telegram_message(
            session,
            "⚠️ 2dehands monitor has been failing every check since "
            f"{first_failure_at} ({failures} consecutive failed cycles). "
            "It will keep retrying automatically; check the logs if this persists.",
        )
        set_state(store, "outage_alert_sent", True)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def check_category(store, session, category):
    """Returns (success, changed) - changed is True if the store now holds
    something worth persisting (new listings seen, or first-run seeding)."""
    name = category["name"]
    url = category["url"]
    try:
        items = fetch_category_listings(session, url)
    except FetchError as exc:
        log.error("Failed to fetch category %r: %s", name, exc)
        return False, False

    if not is_category_initialized(store, name):
        for item in items:
            save_listing(store, name, item)
        mark_category_initialized(store, name)
        log.info("First run for %r: seeded %d listings silently", name, len(items))
        return True, True

    seen_ids = get_seen_ids(store, name)
    new_items = [i for i in items if i["item_id"] not in seen_ids]

    if new_items:
        log.info("%r: %d new listing(s)", name, len(new_items))
    else:
        log.info("%r: no new listings (%d checked)", name, len(items))

    detected_at = now_iso()
    filtered_count = 0
    for item in new_items:
        seller_type = fetch_seller_type(session, item["url"])
        if seller_type in EXCLUDED_SELLER_TYPES:
            filtered_count += 1
        else:
            notify_new_listing(session, name, item, detected_at)
        save_listing(store, name, item, detected_at)

    if filtered_count:
        log.info("%r: filtered out %d listing(s) from business/trader sellers", name, filtered_count)
    return True, bool(new_items)


def main():
    try:
        categories = load_categories()
    except Exception:
        log.exception("Could not load config from %s", CONFIG_PATH)
        return 0

    store = load_store()
    session = requests.Session()

    any_succeeded = False
    any_changed = False
    for idx, category in enumerate(categories):
        try:
            ok, changed = check_category(store, session, category)
            any_succeeded = any_succeeded or ok
            any_changed = any_changed or changed
        except Exception:
            log.exception("Unexpected error checking category %r", category.get("name"))
        if idx < len(categories) - 1:
            time.sleep(random.uniform(*CATEGORY_STAGGER_RANGE_SECONDS))

    try:
        handle_cycle_outcome(store, session, any_succeeded)
    except Exception:
        log.exception("Error while handling cycle outcome/outage alerting")

    prune_old_entries(store)

    # Only touch last_checked_at (and thus produce a git diff worth
    # committing) when something actually happened, or periodically as a
    # heartbeat so the repo doesn't go dormant. Pruning also changed the
    # store when it happens, but that's already reflected on disk either
    # way. Without this, every single run would commit just to update a
    # timestamp nobody needs, at ~144 commits/day.
    if any_changed or _heartbeat_due(store):
        set_state(store, "last_checked_at", now_iso())

    save_store(store)
    return 0


def _heartbeat_due(store):
    last_checked_at = get_state(store, "last_checked_at")
    if not last_checked_at:
        return True
    try:
        elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last_checked_at)
    except ValueError:
        return True
    return elapsed >= HEARTBEAT_INTERVAL


if __name__ == "__main__":
    sys.exit(main())
