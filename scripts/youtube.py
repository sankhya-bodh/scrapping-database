#!/usr/bin/env python3
"""YouTube scraper for the Content OS Database Airtable base.

For every Accounts row with Scrape = Active and Platform = YouTube:
  1. Fetch the channel's newest videos from ScrapeCreators (channel-videos),
     exactly one call per channel: no retries, no fallback call.
  2. Create new videos in YouTube Videos (Status = New) and refresh existing
     ones (every field except Status, so "Reviewed" marks are kept).
  3. Record Last Scraped / Last Scrape Status / Scrape Error on the account.

Runs unattended (standard library only). Keys come from ../.env or the
environment: AIRTABLE_ACCESS_TOKEN, SCRAPE_CREATORS. See project.md.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

AIRTABLE_API = "https://api.airtable.com/v0"
BASE_ID = "appNPeZ6BoFaLfP5C"
SC_API = "https://api.scrapecreators.com"

# Accounts tbl04PJf51XEAiA8h
ACCOUNTS = "tbl04PJf51XEAiA8h"
A_NAME = "fldPwDwLjGikNchgw"
A_PLATFORM = "fldWYqll9jXSskuSC"
A_PLATFORM_ID = "fldVaDjv7ixoxEE8N"
A_SCRAPE = "flduo9Gt6GDKLEEs3"
A_LAST_SCRAPED = "fldn87Q7Uz8fl7erL"
A_LAST_STATUS = "flduqhmL46oI0XJPo"
A_SCRAPE_ERROR = "flde8rfQchM6wk7Ho"

# YouTube Videos tblpMr9jQNMdm865J
VIDEOS = "tblpMr9jQNMdm865J"
V_TITLE = "fld0lhAm41ZN8FNY6"
V_VIDEO_ID = "fldHbJmqwmwpzRqNy"
V_ACCOUNT = "fldLyiD9EOEM7JHrx"
V_URL = "fldPR4PqR4NsKru51"
V_THUMBNAIL = "fld0qq4zJ1dTzJqtL"
V_DESCRIPTION = "fldDFQKkhZon4n068"
V_PUBLISHED = "fldGXBPbOBna0Ww9c"
V_DURATION = "fldPx3oe0AKQiD6iF"
V_VIEWS = "fldcW8PfaDGj2SYWg"
V_LIKES = "fldsvQdR1gyILQzNH"
V_COMMENTS = "fldJ1L7ClYGut2diw"
V_STATUS = "fld1cf4LCqxglAUGN"
V_LAST_SCRAPED = "fldGSuB2qHERC4p5l"

BATCH = 10  # Airtable's max records per write request


class HttpError(Exception):
    pass


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_env():
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
    missing = [k for k in ("AIRTABLE_ACCESS_TOKEN", "SCRAPE_CREATORS") if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing required keys: {', '.join(missing)} (set them in {env_file})")


def request(method, url, headers, body=None, timeout=90, rate_limit_waits=0):
    """JSON request, no retries on failure. rate_limit_waits only covers Airtable's free 429s."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {**headers, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(rate_limit_waits + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code == 429 and attempt < rate_limit_waits:
                time.sleep(30)
                continue
            raise HttpError(f"HTTP {e.code} from {urllib.parse.urlsplit(url).path}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise HttpError(f"Network error for {urllib.parse.urlsplit(url).path}: {e}") from None


# --- Airtable -------------------------------------------------------------

def airtable(method, table, params=None, body=None):
    url = f"{AIRTABLE_API}/{BASE_ID}/{table}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    headers = {"Authorization": f"Bearer {os.environ['AIRTABLE_ACCESS_TOKEN']}"}
    # Airtable 429 = 5 req/s limit; waiting is free and avoids losing an already-paid scrape.
    return request(method, url, headers, body, rate_limit_waits=2)


def list_records(table, field_ids):
    """All records of a table (following offset paging), fields keyed by field ID."""
    records, offset = [], None
    while True:
        params = {"pageSize": 100, "returnFieldsByFieldId": "true", "fields[]": field_ids}
        if offset:
            params["offset"] = offset
        page = airtable("GET", table, params)
        records.extend(page.get("records", []))
        offset = page.get("offset")
        if not offset:
            return records


def write_batches(method, table, records):
    """POST (create) or PATCH (update) records in batches of BATCH."""
    done = []
    for i in range(0, len(records), BATCH):
        result = airtable(method, table, body={"records": records[i:i + BATCH]})
        done.extend(result.get("records", []))
    return done


# --- ScrapeCreators -------------------------------------------------------

def scrapecreators(path, params=None):
    url = f"{SC_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = request("GET", url, {"x-api-key": os.environ["SCRAPE_CREATORS"]})
    if isinstance(data, dict) and data.get("success") is False:
        raise HttpError(f"{path} returned success=false: {str(data.get('message') or data)[:300]}")
    return data


def fetch_videos(channel_id):
    """One paid call. Returns (videos, credits_charged, credits_remaining)."""
    data = scrapecreators("/v1/youtube/channel-videos",
                          {"channelId": channel_id, "includeExtras": "true"})
    return data.get("videos") or [], data.get("credits_charged"), data.get("credits_remaining")


# --- Mapping --------------------------------------------------------------

def to_int(value):
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def to_utc(value):
    """ISO 8601 (with offset or Z) -> ISO UTC string, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def video_fields(video, account_id, now, is_new):
    """Airtable fields for one video. Status only on create, never on update."""
    exact = to_utc(video.get("publishDate"))
    fields = {
        V_TITLE: video.get("title"),
        V_VIDEO_ID: video.get("id"),
        V_ACCOUNT: [account_id],
        V_URL: video.get("url"),
        V_THUMBNAIL: video.get("thumbnail"),
        V_DESCRIPTION: video.get("description"),
        # publishedTime is only day-accurate ("3 days ago"); use it for new videos when
        # publishDate is missing, but never let it overwrite an exact date on an update.
        V_PUBLISHED: exact or (to_utc(video.get("publishedTime")) if is_new else None),
        V_DURATION: to_int(video.get("lengthSeconds")),
        V_VIEWS: to_int(video.get("viewCountInt")),
        V_LIKES: to_int(video.get("likeCountInt")),
        V_COMMENTS: to_int(video.get("commentCountInt")),
        V_LAST_SCRAPED: now,
    }
    if is_new:
        fields[V_STATUS] = "New"
    # Omit missing values rather than blanking out what's already stored.
    return {k: v for k, v in fields.items() if v is not None}


# --- Main -----------------------------------------------------------------

def scrape_account(account, existing, now):
    """Scrape one account; mutates `existing` (video ID -> record ID). Returns stats."""
    fields = account["fields"]
    channel_id = (fields.get(A_PLATFORM_ID) or "").strip()
    if not channel_id:
        raise ValueError("Platform ID (channelId) is empty")

    videos, charged, remaining = fetch_videos(channel_id)
    stats = {"created": 0, "updated": 0, "returned": len(videos),
             "credits": charged, "remaining": remaining}
    creates, updates, seen = [], [], set()
    for video in videos:
        vid = video.get("id")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        if vid in existing:
            updates.append({"id": existing[vid],
                            "fields": video_fields(video, account["id"], now, False)})
        else:
            creates.append({"fields": video_fields(video, account["id"], now, True)})

    try:
        stats["updated"] = len(write_batches("PATCH", VIDEOS, updates))
        created = write_batches("POST", VIDEOS, creates)
        stats["created"] = len(created)
        for rec in created:
            existing[rec["fields"].get(V_VIDEO_ID)] = rec["id"]
    except Exception as e:
        e.stats = stats  # the scrape was paid for; keep its credit numbers in the summary
        raise
    return stats


def main():
    load_env()
    started = datetime.now(timezone.utc)
    log("YouTube scrape started")

    accounts = [
        a for a in list_records(ACCOUNTS, [A_NAME, A_PLATFORM, A_PLATFORM_ID, A_SCRAPE])
        if a["fields"].get(A_SCRAPE) == "Active" and a["fields"].get(A_PLATFORM) == "YouTube"
    ]
    log(f"{len(accounts)} active YouTube account(s)")

    existing = {r["fields"][V_VIDEO_ID]: r["id"]
                for r in list_records(VIDEOS, [V_VIDEO_ID]) if r["fields"].get(V_VIDEO_ID)}
    log(f"{len(existing)} video(s) already in YouTube Videos")

    results, remaining = [], None
    for account in accounts:
        name = account["fields"].get(A_NAME) or account["id"]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        log(f"Scraping {name}")
        try:
            stats = scrape_account(account, existing, now)
            status, error = "ok", None
        except Exception as e:  # no retries: record on the account and move on
            stats = getattr(e, "stats", None) or {"created": 0, "updated": 0, "returned": 0,
                                                  "credits": None, "remaining": None}
            status, error = "error", f"{now} {type(e).__name__}: {e}"[:5000]
            log(f"  ERROR: {error}")
        remaining = stats["remaining"] if stats["remaining"] is not None else remaining

        try:
            airtable("PATCH", ACCOUNTS, body={"records": [{"id": account["id"], "fields": {
                A_LAST_SCRAPED: now, A_LAST_STATUS: status, A_SCRAPE_ERROR: error}}]})
        except HttpError as e:
            log(f"  Could not update account row: {e}")

        results.append((name, status, stats, error))

    print()
    print("Summary")
    for name, status, stats, error in results:
        used = stats["credits"] if stats["credits"] is not None else 0
        print(f"  {name}: {status} | created {stats['created']}, updated {stats['updated']}"
              f" (of {stats['returned']} returned) | credits used {used}")
        if error:
            print(f"    error: {error}")
    total = sum(s["credits"] or 0 for _, _, s, _ in results)
    print(f"  Total credits used: {total} | credits remaining: {remaining if remaining is not None else '?'}")
    print(f"  Duration: {(datetime.now(timezone.utc) - started).total_seconds():.1f}s")

    return 1 if any(r[1] == "error" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
