#!/usr/bin/env python3
"""Reddit scraper for the Content OS Database Airtable base.

Runs daily. For every Accounts row with Scrape = Active and Platform = Reddit:
  1. Fetch the subreddit's top posts from ScrapeCreators (reddit/subreddit, sort=top),
     exactly one call per subreddit (1 credit): no retries, no fallback call, no paging.
     Timeframe "day" (the last 24 hours, about 25 posts) on the daily run. "week" for a new
     subreddit's first scrape (no Last Scraped yet), and to catch up when the last
     successful scrape is more than CATCHUP_HOURS old (missed days).
  2. Create new posts in Scraped Content (Platform = Reddit, Content ID = Post ID, Type from
     post_hint, Status = New, Media) and refresh existing ones (every field except Status,
     so "Reviewed" marks are kept).
     Media is only sent when the stored field is empty, so files are never duplicated.
     Reddit videos need a free DASHPlaylist.mpd fetch from v.redd.it; if that fails
     the post is saved without Media and the next run tries again.
     Only the returned Post IDs are looked up in Airtable.
  3. On success: Last Scraped = now, Last Scrape Status = ok, Scrape Error cleared. On
     failure: error and the reason; Last Scraped is kept, so the next run catches up.

  python3 scripts/reddit.py                 daily run: every active subreddit
  python3 scripts/reddit.py --new-accounts  only subreddits with no Last Scraped (just added)

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
REDDIT_URL = "https://www.reddit.com"

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
# Platform + Content ID (here the Post ID)
POSTS = "tblViAU74E50jTA5H"
P_CONTENT_ID = "fldQo17O1tcoje3HB"
P_TITLE = "fldNkd3XzlbpdNPW6"
P_PLATFORM = "fld79CFY4T9xkiA6Q"
P_ACCOUNT = "fldn8KHcs9SurgWBE"
P_URL = "fldQRzqXqCWMbYaLD"
P_TYPE = "fld4g5cokEPmzaoVA"
P_TEXT = "fldkuaPl7DjFmlj7o"
P_AUTHOR = "fldTvHNMgNc2DjXEk"
P_FLAIR = "fldQISDIW9m5QF5QA"
P_PUBLISHED = "fldTdsMUfOHYCkmVP"
P_COMMENTS = "fldPIA6Zm9Rib9Xe8"
P_SCORE = "fldAlSelO3OXm353v"
P_UPVOTE_RATIO = "fldXWnVTWEqgepC02"
P_MEDIA = "fld0CsTd3XkegpUEP"
P_STATUS = "fld2D5rCtlEW9w2Hn"
P_LAST_SCRAPED = "fldtqUWCeQmuP2Ofs"
PLATFORM = "Reddit"  # the Platform value of this script's rows

# v.redd.it serves CMAF files to browser-like clients; a bare urllib UA may be refused.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CATCHUP_HOURS = 36   # last success older than this (a missed day): read the top of the week
LOOKUP_CHUNK = 50    # Post IDs per Airtable lookup
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


def fetch_text(url, timeout=30):
    """Plain-text GET with a browser User-Agent, single attempt (used for v.redd.it)."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise HttpError(f"HTTP {e.code} from {url}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise HttpError(f"Network error for {url}: {e}") from None


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


def fetch_posts(subreddit, timeframe="day"):
    """One paid call, first page only ("after" is ignored). The subreddit name is not
    case-sensitive here. Returns (posts, credits_charged, credits_remaining)."""
    data = scrapecreators("/v1/reddit/subreddit",
                          {"subreddit": subreddit, "sort": "top", "timeframe": timeframe})
    posts = data.get("posts") if isinstance(data.get("posts"), list) else []
    return [p for p in posts if isinstance(p, dict)], data.get("credits_charged"), data.get("credits_remaining")


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


def thread_url(post):
    """The Reddit thread link, built from permalink (posts[].url is the linked content)."""
    permalink = (post.get("permalink") or "").strip()
    if not permalink:
        return None
    if permalink.startswith("http"):
        return permalink
    return REDDIT_URL + "/" + permalink.lstrip("/")


def file_number(name):
    """The last number in a file name, ignoring the extension (CMAF_720.mp4 -> 720), 0 if none."""
    stem = os.path.splitext(name.split("?")[0].rsplit("/", 1)[-1])[0]
    numbers = re.findall(r"\d+", stem)
    return int(numbers[-1]) if numbers else 0


def video_files(post_id, video_url):
    """Read {video_url}/DASHPlaylist.mpd (one free request to Reddit, no retry) and return
    attachments for the best video file and the best audio file. Raises HttpError."""
    base = video_url.split("?")[0].rstrip("/")
    playlist = fetch_text(f"{base}/DASHPlaylist.mpd")
    names = [n.strip() for n in re.findall(r"<BaseURL>(.*?)</BaseURL>", playlist, re.S) if n.strip()]
    videos = [n for n in names if "audio" not in n.lower()]
    audios = [n for n in names if "audio" in n.lower()]
    if not videos:  # audio alone isn't worth attaching; leave Media empty for the next run
        raise HttpError(f"no video file listed in {base}/DASHPlaylist.mpd")
    files = []
    for group, suffix in ((videos, "video"), (audios, "audio")):
        if group:  # silent videos have no audio file; attach the video alone
            best = max(group, key=file_number)
            url = best if best.startswith("http") else f"{base}/{best}"
            files.append({"url": url, "filename": f"{post_id}_{suffix}.mp4"})
    return files


def media_files(post):
    """Attachment list for the Media field: image post -> the image, v.redd.it -> video and
    audio. Galleries, text and link posts -> empty list. Raises HttpError if a playlist fails."""
    post_id = post.get("id")
    url = (post.get("url") or "").strip()
    if post.get("post_hint") == "image" and "i.redd.it" in url:
        ext = os.path.splitext(urllib.parse.urlsplit(url).path)[1] or ".jpg"
        return [{"url": url, "filename": f"{post_id}{ext}"}]
    if "v.redd.it" in url:
        return video_files(post_id, url)
    return []


def post_type(post):
    """Type from ScrapeCreators' post_hint (text, image, video, gallery, multi_media, link, ...)
    and the link: Gallery, Photo, Video, Text (self posts, also with inline media), else Link."""
    hint = post.get("post_hint")
    url = str(post.get("url") or "")
    if hint == "gallery" or "reddit.com/gallery/" in url:
        return "Gallery"
    if hint == "image" or "i.redd.it" in url:
        return "Photo"
    if hint in ("video", "hosted:video") or "v.redd.it" in url or post.get("is_video") is True:
        return "Video"
    if hint in ("text", "self", "multi_media") or post.get("is_self") is True \
            or str(post.get("domain") or "").startswith("self."):
        return "Text"
    return "Link"


def post_fields(post, account_id, now, existing=None):
    """Airtable fields for one post (Media is added separately). `existing` is None for a
    new post, otherwise the stored record's info. Status only on create, never on update."""
    fields = {
        P_CONTENT_ID: post.get("id"),
        P_TITLE: post.get("title"),
        P_PLATFORM: PLATFORM,
        P_TYPE: post_type(post),
        P_ACCOUNT: [account_id],
        P_URL: thread_url(post),
        P_AUTHOR: post.get("author"),
        P_TEXT: post.get("selftext"),
        P_FLAIR: post.get("link_flair_text"),
        P_PUBLISHED: to_utc(post.get("created_at_iso")) or unix_to_utc(post.get("created_utc")),
        P_SCORE: to_int(post.get("score")),
        P_UPVOTE_RATIO: to_float(post.get("upvote_ratio")),  # 0-1; the percent field expects that
        P_COMMENTS: to_int(post.get("num_comments")),
        P_LAST_SCRAPED: now,
    }
    if existing is None:
        fields[P_STATUS] = "New"
    # Omit missing or empty values rather than blanking out what's already stored.
    return {k: v for k, v in fields.items() if v is not None and v != "" and v != []}


# --- Main -----------------------------------------------------------------

def parse_iso(value):
    """ISO 8601 string -> aware datetime (UTC), or None."""
    utc = to_utc(value)
    return datetime.strptime(utc, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=timezone.utc) if utc else None


def timeframe_for(account, now_dt):
    """"week" for a new subreddit (no Last Scraped) or one whose last success is more than
    CATCHUP_HOURS old, else "day"."""
    last = parse_iso(account["fields"].get(A_LAST_SCRAPED))
    return "day" if last and now_dt - last <= timedelta(hours=CATCHUP_HOURS) else "week"


def lookup_existing(ids, existing):
    """Add to `existing` (post ID -> {"id", "media"}) the given IDs already in Scraped Content
    (among the Reddit rows), LOOKUP_CHUNK per filtered query, instead of loading the whole table."""
    todo = [i for i in dict.fromkeys(ids) if i not in existing]
    for i in range(0, len(todo), LOOKUP_CHUNK):
        chunk = todo[i:i + LOOKUP_CHUNK]
        quoted = ",".join("{%s}='%s'" % (P_CONTENT_ID, v.replace("\\", "\\\\").replace("'", "\\'")) for v in chunk)
        body = {"filterByFormula": f"AND({{{P_PLATFORM}}}='{PLATFORM}',OR({quoted}))", "fields": [P_CONTENT_ID, P_MEDIA],
                "returnFieldsByFieldId": True, "pageSize": 100}
        while True:
            page = airtable("POST", f"{POSTS}/listRecords", body=body)
            for r in page.get("records", []):
                pid = r["fields"].get(P_CONTENT_ID)
                if pid in chunk:
                    # Empty attachment fields are left out of Airtable's response: presence = filled.
                    existing[pid] = {"id": r["id"], "media": bool(r["fields"].get(P_MEDIA))}
            if not page.get("offset"):
                break
            body["offset"] = page["offset"]


def subreddit_name(account):
    """Handle without "r/" or slashes."""
    name = (account["fields"].get(A_HANDLE) or "").strip().strip("/")
    return (name[2:] if name.lower().startswith("r/") else name).strip("/")


def scrape_account(account, existing, now):
    """Scrape one subreddit; mutates `existing` (post ID -> record info). Returns stats."""
    subreddit = subreddit_name(account)
    if not subreddit:
        raise ValueError("Handle (subreddit) is empty")
    timeframe = timeframe_for(account, parse_iso(now))

    posts, charged, remaining = fetch_posts(subreddit, timeframe)
    stats = {"created": 0, "updated": 0, "media_added": 0, "media_skipped": 0, "timeframe": timeframe,
             "returned": len(posts), "credits": charged, "remaining": remaining}
    posts = [p for p in posts if isinstance(p.get("id"), str) and p["id"]]
    lookup_existing([p["id"] for p in posts], existing)
    creates, updates, seen = [], [], set()
    for post in posts:
        pid = post["id"]
        if pid in seen:
            continue
        seen.add(pid)
        info = existing.get(pid)
        record = {"fields": post_fields(post, account["id"], now, info)}
        # Attach media once; never re-send a filled field (that would duplicate files).
        if info is None or not info["media"]:
            try:
                files = media_files(post)
            except HttpError as e:  # not an account error: Media stays empty, next run retries
                files = []
                stats["media_skipped"] += 1
                log(f"  Media skipped for {pid}: {e}")
            if files:
                record["fields"][P_MEDIA] = files
        if info is None:
            creates.append(record)
        else:
            record["id"] = info["id"]
            updates.append(record)

    try:
        # One batch at a time so the stats stay accurate if a later batch fails.
        for method, records, key in (("PATCH", updates, "updated"), ("POST", creates, "created")):
            for i in range(0, len(records), BATCH):
                batch = records[i:i + BATCH]
                written = write_batches(method, POSTS, batch)
                stats[key] += len(written)
                stats["media_added"] += sum(len(r["fields"].get(P_MEDIA, [])) for r in batch)
                for sent, rec in zip(batch, written):
                    pid = sent["fields"][P_CONTENT_ID]
                    info = existing.setdefault(pid, {"id": rec["id"], "media": False})
                    info["media"] = info["media"] or P_MEDIA in sent["fields"]
    except Exception as e:
        e.stats = stats  # the scrape was paid for; keep its credit numbers in the summary
        raise
    return stats


def main(new_accounts=False):
    """new_accounts: only the subreddits with no Last Scraped (just added)."""
    load_env()
    started = datetime.now(timezone.utc)
    log("Reddit scrape started" + (" (new accounts only)" if new_accounts else ""))

    accounts = [
        a for a in list_records(ACCOUNTS, [A_NAME, A_PLATFORM, A_HANDLE, A_SCRAPE, A_LAST_SCRAPED])
        if a["fields"].get(A_SCRAPE) == "Active" and a["fields"].get(A_PLATFORM) == "Reddit"
    ]
    if new_accounts:
        accounts = [a for a in accounts if not parse_iso(a["fields"].get(A_LAST_SCRAPED))]
        log(f"{len(accounts)} new Reddit account(s)")
    else:
        log(f"{len(accounts)} active Reddit account(s)")

    existing = {}  # post ID -> {"id", "media"}, filled by lookups of the returned IDs
    results, remaining = [], None
    for account in accounts:
        name = account["fields"].get(A_NAME) or account["id"]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        log(f"Scraping {name}")
        try:
            stats = scrape_account(account, existing, now)
            status, error = "ok", None
        except Exception as e:  # no retries: record on the account and move on
            stats = getattr(e, "stats", None) or {"created": 0, "updated": 0, "media_added": 0,
                                                  "media_skipped": 0, "returned": 0, "timeframe": "-",
                                                  "credits": None, "remaining": None}
            status, error = "error", f"{now} {type(e).__name__}: {e}"[:5000]
            log(f"  ERROR: {error}")
        remaining = stats["remaining"] if stats["remaining"] is not None else remaining

        # Last Scraped only moves on success, so a failed subreddit catches up next run.
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
        print(f"  {name}: {status} | top of the {stats['timeframe']} | created {stats['created']},"
              f" updated {stats['updated']} (of {stats['returned']} returned) | media files added {stats['media_added']},"
              f" media skipped {stats['media_skipped']} | credits used {used}")
        if error:
            print(f"    error: {error}")
    total = sum(s["credits"] or 0 for _, _, s, _ in results)
    print(f"  Total credits used: {total} | credits remaining: {remaining if remaining is not None else '?'}")
    print(f"  Duration: {(datetime.now(timezone.utc) - started).total_seconds():.1f}s")

    return 1 if any(r[1] == "error" for r in results) else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args not in ([], ["--new-accounts"]):
        sys.exit("Usage: reddit.py [--new-accounts]")
    sys.exit(main(new_accounts=bool(args)))
