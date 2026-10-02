#!/usr/bin/env python3
"""YouTube scraper for the Content OS Database Airtable base.

Runs daily; each channel is scraped every RUN_DAYS days. For the Accounts rows with
Scrape = Active and Platform = YouTube that are due (no Last Scraped yet, or Last Scraped
RUN_DAYS days ago, less DUE_SLACK_HOURS so a slightly early daily run still counts):
  1. Fetch the channel's 30 newest regular videos (no Shorts) from ScrapeCreators
     (channel-videos, sort=latest, includeExtras=true): one call per channel, no retries.
  2. Create every returned video not yet in Scraped Content (Platform = YouTube, Content ID =
     Video ID, Type = Video, Status = New): a new channel's first scrape stores its 30 newest
     videos; later runs store whatever is new since.
     Refresh every returned video already stored (every field except Status, so "Reviewed"
     marks are kept). Only the returned Video IDs are looked up in Airtable. The thumbnail
     is saved as an attachment in Media, only when that field is empty, so it's never
     duplicated.
  3. On success: Last Scraped = now, Last Scrape Status = ok, Scrape Error cleared. On
     failure: error and the reason; Last Scraped is kept, so the next daily run retries.

  python3 scripts/youtube.py                 daily run: the channels that are due
  python3 scripts/youtube.py --new-accounts  only channels with no Last Scraped (just added)

Runs unattended (standard library only). Keys come from ../.env or the
environment: AIRTABLE_ACCESS_TOKEN, SCRAPE_CREATORS. See project.md.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
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
A_HANDLE = "fldABp0JmnZKyiajW"
A_SCRAPE = "flduo9Gt6GDKLEEs3"
A_LAST_SCRAPED = "fldn87Q7Uz8fl7erL"
A_LAST_STATUS = "flduqhmL46oI0XJPo"
A_SCRAPE_ERROR = "flde8rfQchM6wk7Ho"

# Scraped Content tblViAU74E50jTA5H: one table for every platform; rows match on
# Platform + Content ID (here the Video ID)
VIDEOS = "tblViAU74E50jTA5H"
V_CONTENT_ID = "fldQo17O1tcoje3HB"
V_TITLE = "fldNkd3XzlbpdNPW6"
V_PLATFORM = "fld79CFY4T9xkiA6Q"
V_ACCOUNT = "fldn8KHcs9SurgWBE"
V_URL = "fldQRzqXqCWMbYaLD"
V_TYPE = "fld4g5cokEPmzaoVA"
V_TEXT = "fldkuaPl7DjFmlj7o"
V_PUBLISHED = "fldTdsMUfOHYCkmVP"
V_DURATION = "fldlIejl80YEiJ4th"
V_VIEWS = "fldoJ7VfdRHC78CeJ"
V_LIKES = "fldBybnTMpW6Egc3O"
V_COMMENTS = "fldPIA6Zm9Rib9Xe8"
V_MEDIA = "fld0CsTd3XkegpUEP"  # attachment: the video's thumbnail
V_STATUS = "fld2D5rCtlEW9w2Hn"
V_LAST_SCRAPED = "fldtqUWCeQmuP2Ofs"
PLATFORM = "YouTube"  # the Platform value of this script's rows

RUN_DAYS = 3          # each channel is scraped every 3 days
DUE_SLACK_HOURS = 6   # a channel is due this long before its 3 days are up (daily-run jitter)
LOOKUP_CHUNK = 50     # Video IDs per Airtable lookup
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
    """One paid call: the 30 newest regular videos (Shorts come back in a separate list,
    which is ignored). Returns (videos, credits_charged, credits_remaining)."""
    data = scrapecreators("/v1/youtube/channel-videos",
                          {"channelId": channel_id, "sort": "latest", "includeExtras": "true"})
    videos = data.get("videos") if isinstance(data.get("videos"), list) else []
    return [v for v in videos if isinstance(v, dict)], data.get("credits_charged"), data.get("credits_remaining")


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


def parse_iso(value):
    """ISO 8601 string -> aware datetime (UTC), or None."""
    utc = to_utc(value)
    return datetime.strptime(utc, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=timezone.utc) if utc else None


def video_fields(video, account_id, now, is_new):
    """Airtable fields for one video. Status only on create, never on update."""
    exact = to_utc(video.get("publishDate"))
    fields = {
        V_CONTENT_ID: video.get("id"),
        V_TITLE: video.get("title"),
        V_PLATFORM: PLATFORM,
        V_TYPE: "Video",  # channel-videos returns regular videos only (Shorts are skipped)
        V_ACCOUNT: [account_id],
        V_URL: video.get("url"),
        V_TEXT: video.get("description"),
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

def is_due(account, now_dt):
    """No Last Scraped yet (a new channel), or scraped RUN_DAYS ago (less the slack)."""
    last = parse_iso(account["fields"].get(A_LAST_SCRAPED))
    return last is None or last <= now_dt - timedelta(days=RUN_DAYS) + timedelta(hours=DUE_SLACK_HOURS)


def thumbnail_files(video):
    """The thumbnail, as the attachment list for the Media field (Airtable copies the image).
    The API's link (hq720.jpg?sqp=...) serves AVIF; without the query it's a 1280x720 JPEG.
    A thumbnail being A/B tested (hq720_custom_2.jpg?sqp=...) 404s without the query, so it
    falls back to the video's hq720.jpg (Airtable silently drops a file it can't download)."""
    url = video.get("thumbnail")
    if isinstance(url, str) and url.startswith("http"):
        url = re.sub(r"_custom_\d+(?=\.jpg$)", "", url.split("?")[0])
        return [{"url": url, "filename": f"{video['id']}.jpg"}]
    return []


def lookup_existing(ids, existing):
    """Add to `existing` (video ID -> {"id", "media"}) the given IDs that are already in Scraped
    Content (among the YouTube rows), LOOKUP_CHUNK per filtered query, instead of loading the
    whole table."""
    todo = [i for i in dict.fromkeys(ids) if i not in existing]
    for i in range(0, len(todo), LOOKUP_CHUNK):
        chunk = todo[i:i + LOOKUP_CHUNK]
        quoted = ",".join("{%s}='%s'" % (V_CONTENT_ID, v.replace("\\", "\\\\").replace("'", "\\'")) for v in chunk)
        body = {"filterByFormula": f"AND({{{V_PLATFORM}}}='{PLATFORM}',OR({quoted}))", "fields": [V_CONTENT_ID, V_MEDIA],
                "returnFieldsByFieldId": True, "pageSize": 100}
        while True:
            page = airtable("POST", f"{VIDEOS}/listRecords", body=body)
            for r in page.get("records", []):
                vid = r["fields"].get(V_CONTENT_ID)
                if vid in chunk:
                    # Empty attachment fields are left out of Airtable's response: presence = filled.
                    existing[vid] = {"id": r["id"], "media": bool(r["fields"].get(V_MEDIA))}
            if not page.get("offset"):
                break
            body["offset"] = page["offset"]


def scrape_account(account, existing, now):
    """Scrape one account; mutates `existing` (video ID -> {"id", "media"}). Returns stats."""
    fields = account["fields"]
    channel_id = (fields.get(A_PLATFORM_ID) or "").strip()
    if not channel_id:
        raise ValueError("Platform ID (channelId) is empty")

    videos, charged, remaining = fetch_videos(channel_id)
    stats = {"created": 0, "updated": 0, "thumbnails": 0, "returned": len(videos),
             "credits": charged, "remaining": remaining}
    videos = [v for v in videos if isinstance(v.get("id"), str) and v["id"]]
    lookup_existing([v["id"] for v in videos], existing)
    creates, updates, seen = [], [], set()
    for video in videos:
        vid = video["id"]
        if vid in seen:
            continue
        seen.add(vid)
        info = existing.get(vid)
        if info is not None:
            record = {"id": info["id"], "fields": video_fields(video, account["id"], now, False)}
            files = thumbnail_files(video)
            if files and not info["media"]:  # fill an empty Media; never re-send a filled one
                record["fields"][V_MEDIA] = files
            updates.append(record)
            continue
        record = {"fields": video_fields(video, account["id"], now, True)}
        files = thumbnail_files(video)
        if files:
            record["fields"][V_MEDIA] = files
        creates.append(record)

    try:
        for method, records, key in (("PATCH", updates, "updated"), ("POST", creates, "created")):
            written = write_batches(method, VIDEOS, records)
            stats[key] = len(written)
            for sent, rec in zip(records, written):
                vid = sent["fields"][V_CONTENT_ID]
                info = existing.setdefault(vid, {"id": rec["id"], "media": False})
                info["media"] = info["media"] or V_MEDIA in sent["fields"]
                stats["thumbnails"] += V_MEDIA in sent["fields"]
    except Exception as e:
        e.stats = stats  # the scrape was paid for; keep its credit numbers in the summary
        raise
    return stats


def main(new_accounts=False):
    """new_accounts: only the channels with no Last Scraped (just added)."""
    load_env()
    started = datetime.now(timezone.utc)
    log("YouTube scrape started" + (" (new accounts only)" if new_accounts else ""))

    active = [
        a for a in list_records(ACCOUNTS, [A_NAME, A_PLATFORM, A_PLATFORM_ID, A_SCRAPE, A_LAST_SCRAPED])
        if a["fields"].get(A_SCRAPE) == "Active" and a["fields"].get(A_PLATFORM) == "YouTube"
    ]
    if new_accounts:
        accounts = [a for a in active if not parse_iso(a["fields"].get(A_LAST_SCRAPED))]
        log(f"{len(accounts)} new YouTube account(s)")
    else:
        accounts = [a for a in active if is_due(a, started)]
        log(f"{len(accounts)} of {len(active)} active YouTube account(s) due (every {RUN_DAYS} days)")

    existing = {}  # video ID -> record ID, filled by lookups of the returned IDs
    results, remaining = [], None
    for account in accounts:
        name = account["fields"].get(A_NAME) or account["id"]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        log(f"Scraping {name}")
        try:
            stats = scrape_account(account, existing, now)
            status, error = "ok", None
        except Exception as e:  # no retries: record on the account and move on
            stats = getattr(e, "stats", None) or {"created": 0, "updated": 0, "thumbnails": 0, "returned": 0,
                                                  "credits": None, "remaining": None}
            status, error = "error", f"{now} {type(e).__name__}: {e}"[:5000]
            log(f"  ERROR: {error}")
        remaining = stats["remaining"] if stats["remaining"] is not None else remaining

        # Last Scraped only moves on success, so a failed channel stays due and is retried.
        update = {A_LAST_STATUS: status, A_SCRAPE_ERROR: error}
        if status == "ok":
            update[A_LAST_SCRAPED] = now
        try:
            airtable("PATCH", ACCOUNTS, body={"records": [{"id": account["id"], "fields": update}]})
        except HttpError as e:
            log(f"  Could not update account row: {e}")

        results.append((name, status, stats, error))

    print()
    print("Summary")
    for name, status, stats, error in results:
        used = stats["credits"] if stats["credits"] is not None else 0
        print(f"  {name}: {status} | created {stats['created']}, updated {stats['updated']}"
              f" (of {stats['returned']} returned) | thumbnails saved"
              f" {stats['thumbnails']} | credits used {used}")
        if error:
            print(f"    error: {error}")
    total = sum(s["credits"] or 0 for _, _, s, _ in results)
    print(f"  Total credits used: {total} | credits remaining: {remaining if remaining is not None else '?'}")
    print(f"  Duration: {(datetime.now(timezone.utc) - started).total_seconds():.1f}s")

    return 1 if any(r[1] == "error" for r in results) else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args not in ([], ["--new-accounts"]):
        sys.exit("Usage: youtube.py [--new-accounts]")
    sys.exit(main(new_accounts=bool(args)))
