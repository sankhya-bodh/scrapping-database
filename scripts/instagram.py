#!/usr/bin/env python3
"""Instagram scraper for the Content OS Database Airtable base.

For every Accounts row with Scrape = Active and Platform = Instagram:
  1. Fetch the account's first page of posts from ScrapeCreators (v2 user/posts),
     exactly one call per account: no retries, no fallback call, no paging.
  2. Create new posts in Scraped Content (Platform = Instagram, Content ID = Shortcode,
     Title = the caption's first line, Views = plays, Status = New, Thumbnail, Media) and
     refresh existing ones (every field except Status, so "Reviewed" marks are kept). Only
     the returned shortcodes are looked up in Airtable, among the Instagram rows.
     Thumbnail / Media are only sent when the stored field is empty, because
     Instagram media URLs expire and re-sending would duplicate the files.
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
A_HANDLE = "fldABp0JmnZKyiajW"
A_SCRAPE = "flduo9Gt6GDKLEEs3"
A_LAST_SCRAPED = "fldn87Q7Uz8fl7erL"
A_LAST_STATUS = "flduqhmL46oI0XJPo"
A_SCRAPE_ERROR = "flde8rfQchM6wk7Ho"

# Scraped Content tblViAU74E50jTA5H: one table for every platform; rows match on
# Platform + Content ID (here the Shortcode)
POSTS = "tblViAU74E50jTA5H"
P_CONTENT_ID = "fldQo17O1tcoje3HB"
P_TITLE = "fldNkd3XzlbpdNPW6"
P_PLATFORM = "fld79CFY4T9xkiA6Q"
P_ACCOUNT = "fldn8KHcs9SurgWBE"
P_URL = "fldQRzqXqCWMbYaLD"
P_TYPE = "fld4g5cokEPmzaoVA"
P_TEXT = "fldkuaPl7DjFmlj7o"
P_PUBLISHED = "fldTdsMUfOHYCkmVP"
P_DURATION = "fldlIejl80YEiJ4th"
P_VIEWS = "fldoJ7VfdRHC78CeJ"  # Instagram plays
P_LIKES = "fldBybnTMpW6Egc3O"
P_COMMENTS = "fldPIA6Zm9Rib9Xe8"
P_THUMBNAIL = "fldtvY62II7Sz99wS"
P_MEDIA = "fld0CsTd3XkegpUEP"
P_STATUS = "fld2D5rCtlEW9w2Hn"
P_LAST_SCRAPED = "fldtqUWCeQmuP2Ofs"
PLATFORM = "Instagram"  # the Platform value of this script's rows

MEDIA_TYPES = {1: "Photo", 2: "Reel", 8: "Carousel"}

LOOKUP_CHUNK = 50  # Shortcodes per Airtable lookup
TITLE_CHARS = 100  # Title = the first line of the caption, cut to this length
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


def fetch_posts(handle):
    """One paid call, first page only (next_max_id is ignored).
    Returns (items, credits_charged, credits_remaining)."""
    data = scrapecreators("/v2/instagram/user/posts", {"handle": handle})
    return data.get("items") or [], data.get("credits_charged"), data.get("credits_remaining")


# --- Mapping --------------------------------------------------------------

def to_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value):
    number = to_float(value)
    return int(number) if number is not None else None


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


def unix_to_utc(value):
    """Unix seconds -> ISO UTC string, or None."""
    seconds = to_int(value)
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def largest(versions):
    """The entry with the biggest width x height that has a URL, or None."""
    candidates = [v for v in versions or [] if isinstance(v, dict) and v.get("url")]
    if not candidates:
        return None
    return max(candidates, key=lambda v: (to_int(v.get("width")) or 0) * (to_int(v.get("height")) or 0))


def largest_image(media):
    return largest((media.get("image_versions2") or {}).get("candidates"))


def media_files(item, code):
    """Attachment list for the Media field: reel -> its video, carousel -> every
    slide in order, photo -> the image. Empty list if nothing usable."""
    kind = MEDIA_TYPES.get(item.get("media_type"))
    files = []
    if kind == "Reel":
        best = largest(item.get("video_versions"))
        if best:
            files.append({"url": best["url"], "filename": f"{code}.mp4"})
    elif kind == "Carousel":
        for n, slide in enumerate(item.get("carousel_media") or [], start=1):
            if slide.get("video_versions"):
                best, ext = largest(slide.get("video_versions")), "mp4"
            else:
                best, ext = largest_image(slide), "jpg"
            if best:
                files.append({"url": best["url"], "filename": f"{code}_{n}.{ext}"})
    elif kind == "Photo":
        best = largest_image(item)
        if best:
            files.append({"url": best["url"], "filename": f"{code}.jpg"})
    return files


def first_line(text):
    """The first non-empty line of `text`, cut to TITLE_CHARS (None if there is none)."""
    return next((line.strip()[:TITLE_CHARS] for line in str(text or "").splitlines() if line.strip()), None)


def post_fields(item, account_id, now, existing=None):
    """Airtable fields for one post. `existing` is None for a new post, otherwise the
    stored record's info. Status only on create; attachments only where still empty."""
    code = item.get("code")
    kind = MEDIA_TYPES.get(item.get("media_type"))
    duration = to_float(item.get("video_duration")) if kind == "Reel" else None
    caption = (item.get("caption") or {}).get("text")
    fields = {
        P_CONTENT_ID: code,
        P_TITLE: first_line(caption),
        P_PLATFORM: PLATFORM,
        P_TYPE: kind,
        P_TEXT: caption,
        P_ACCOUNT: [account_id],
        P_URL: item.get("url"),
        P_PUBLISHED: to_utc(item.get("created_at")) or unix_to_utc(item.get("taken_at")),
        P_DURATION: round(duration) if duration is not None else None,
        P_VIEWS: to_int(item.get("ig_play_count")),
        # Skip Likes when the owner has hidden like counts.
        P_LIKES: None if item.get("like_and_view_counts_disabled") else to_int(item.get("like_count")),
        P_COMMENTS: to_int(item.get("comment_count")),
        P_LAST_SCRAPED: now,
    }
    if existing is None:
        fields[P_STATUS] = "New"
    # Media URLs expire, so attach them once; never re-send a filled field (duplicates files).
    if (existing is None or not existing["thumbnail"]) and item.get("display_uri"):
        fields[P_THUMBNAIL] = [{"url": item["display_uri"]}]
    if existing is None or not existing["media"]:
        fields[P_MEDIA] = media_files(item, code) or None
    # Omit missing values rather than blanking out what's already stored.
    return {k: v for k, v in fields.items() if v is not None}


def attachment_count(fields):
    return len(fields.get(P_THUMBNAIL, [])) + len(fields.get(P_MEDIA, []))


# --- Main -----------------------------------------------------------------

def lookup_existing(codes, existing):
    """Add to `existing` (shortcode -> {"id", "thumbnail", "media"}) the given shortcodes already
    in Scraped Content (among the Instagram rows), LOOKUP_CHUNK per filtered query, instead of
    loading the whole table."""
    todo = [c for c in dict.fromkeys(codes) if c not in existing]
    for i in range(0, len(todo), LOOKUP_CHUNK):
        chunk = todo[i:i + LOOKUP_CHUNK]
        quoted = ",".join("{%s}='%s'" % (P_CONTENT_ID, c.replace("\\", "\\\\").replace("'", "\\'")) for c in chunk)
        body = {"filterByFormula": f"AND({{{P_PLATFORM}}}='{PLATFORM}',OR({quoted}))",
                "fields": [P_CONTENT_ID, P_THUMBNAIL, P_MEDIA], "returnFieldsByFieldId": True, "pageSize": 100}
        while True:
            page = airtable("POST", f"{POSTS}/listRecords", body=body)
            for r in page.get("records", []):
                code = r["fields"].get(P_CONTENT_ID)
                if code in chunk:
                    # Empty attachment fields are left out of Airtable's response: presence = filled.
                    existing[code] = {"id": r["id"], "thumbnail": bool(r["fields"].get(P_THUMBNAIL)),
                                      "media": bool(r["fields"].get(P_MEDIA))}
            if not page.get("offset"):
                break
            body["offset"] = page["offset"]


def scrape_account(account, existing, now):
    """Scrape one account; mutates `existing` (shortcode -> record info). Returns stats."""
    fields = account["fields"]
    handle = (fields.get(A_HANDLE) or "").strip().lstrip("@")
    if not handle:
        raise ValueError("Handle is empty")

    items, charged, remaining = fetch_posts(handle)
    stats = {"created": 0, "updated": 0, "attachments": 0, "returned": len(items),
             "credits": charged, "remaining": remaining}
    items = [i for i in items if isinstance(i, dict)]
    lookup_existing([i["code"] for i in items if isinstance(i.get("code"), str) and i["code"]], existing)
    creates, updates, seen = [], [], set()
    for item in items:
        code = item.get("code")
        if not isinstance(code, str) or not code or code in seen:
            continue
        seen.add(code)
        if code in existing:
            updates.append({"id": existing[code]["id"],
                            "fields": post_fields(item, account["id"], now, existing[code])})
        else:
            creates.append({"fields": post_fields(item, account["id"], now)})

    try:
        # One batch at a time so the stats stay accurate if a later batch fails.
        for method, records, key in (("PATCH", updates, "updated"), ("POST", creates, "created")):
            for i in range(0, len(records), BATCH):
                batch = records[i:i + BATCH]
                written = write_batches(method, POSTS, batch)
                stats[key] += len(written)
                stats["attachments"] += sum(attachment_count(r["fields"]) for r in batch)
                for sent, rec in zip(batch, written):
                    code = sent["fields"][P_CONTENT_ID]
                    info = existing.setdefault(code, {"id": rec["id"], "thumbnail": False, "media": False})
                    info["thumbnail"] = info["thumbnail"] or P_THUMBNAIL in sent["fields"]
                    info["media"] = info["media"] or P_MEDIA in sent["fields"]
    except Exception as e:
        e.stats = stats  # the scrape was paid for; keep its credit numbers in the summary
        raise
    return stats


def main():
    load_env()
    started = datetime.now(timezone.utc)
    log("Instagram scrape started")

    accounts = [
        a for a in list_records(ACCOUNTS, [A_NAME, A_PLATFORM, A_HANDLE, A_SCRAPE])
        if a["fields"].get(A_SCRAPE) == "Active" and a["fields"].get(A_PLATFORM) == "Instagram"
    ]
    log(f"{len(accounts)} active Instagram account(s)")

    existing = {}  # shortcode -> {"id", "thumbnail", "media"}, filled by lookups of the returned codes
    results, remaining = [], None
    for account in accounts:
        name = account["fields"].get(A_NAME) or account["id"]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        log(f"Scraping {name}")
        try:
            stats = scrape_account(account, existing, now)
            status, error = "ok", None
        except Exception as e:  # no retries: record on the account and move on
            stats = getattr(e, "stats", None) or {"created": 0, "updated": 0, "attachments": 0,
                                                  "returned": 0, "credits": None, "remaining": None}
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
              f" (of {stats['returned']} returned) | attachments added {stats['attachments']}"
              f" | credits used {used}")
        if error:
            print(f"    error: {error}")
    total = sum(s["credits"] or 0 for _, _, s, _ in results)
    print(f"  Total credits used: {total} | credits remaining: {remaining if remaining is not None else '?'}")
    print(f"  Duration: {(datetime.now(timezone.utc) - started).total_seconds():.1f}s")

    return 1 if any(r[1] == "error" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
