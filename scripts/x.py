#!/usr/bin/env python3
"""X (Twitter) scraper for the Content OS Database Airtable base.

Runs every 6 hours. For the Accounts rows with Scrape = Active and Platform = X:
  1. Fetch the original tweets posted since the previous run from twitterapi.io
     (tweet/advanced_search), following the twitterapi.io "monitor accounts for new
     tweets" guide, with up to HANDLES_PER_QUERY accounts batched into one query:
       (from:a OR from:b ...) since_time:<unix> until_time:<unix> -filter:replies -filter:retweets
     until_time = the moment the run starts. since_time = WINDOW_HOURS back, or earlier if
     the batch's last fully read window (its accounts' Last Scraped) ended before that, minus
     OVERLAP_MINUTES for tweets the search indexes late. So a run normally reads the last
     8 hours, and after a failed run the next one re-reads the gap (at most MAX_CATCHUP_HOURS).
     Accounts are batched with others whose Last Scraped is about the same, so one behind
     doesn't rewind the rest. A newly added account (no Last Scraped, or a resumed one whose
     Last Scraped is older than MAX_CATCHUP_HOURS) is read in its own batch from
     NEW_ACCOUNT_HOURS back: its last 24 hours, no further.
     Long (catch-up) windows are read oldest-first in SLICE_HOURS slices. Pages are followed
     as in the guide (while has_next_page and next_cursor), stopping early on an empty or
     repeated page. A query that hits MAX_PAGES continues with the older rest of its slice,
     within MAX_CALLS_PER_BATCH calls per run. twitterapi.io 429 / 5xx / network errors are
     retried once.
  2. Create new tweets in X Posts (Status = New, Media), matched to their account by the
     author's user ID (saved to an empty Platform ID), else by handle. Only the returned Tweet
     IDs are looked up in Airtable. Tweets seen again (the overlap) are updated except Status,
     so "Reviewed" marks are kept. Quote tweets, replies and retweets are skipped. Media is
     only sent when the stored field is empty.
  3. On each account: Last Scraped = end of the last fully read and saved slice (never moved
     past anything unread), Last Scrape Status, Scrape Error.
Only one run at a time: a second run started while one is going exits (lock file).

  python3 scripts/x.py                 scheduled run: every active X account
  python3 scripts/x.py --new-accounts  only the newly added accounts (their last 24 hours);
                                       started when an account is added. Does nothing if none.

Runs unattended (standard library only). Keys come from ../.env or the
environment: AIRTABLE_ACCESS_TOKEN, TWITTER_API. See project.md.
"""

import fcntl
import html
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
TW_API = "https://api.twitterapi.io"

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

# X Posts tblA1bdKffknF94aW
POSTS = "tblA1bdKffknF94aW"
P_TEXT = "fld5ofchYsjcFqFNg"
P_TWEET_ID = "fldgNotkqXPybn8TR"
P_ACCOUNT = "fldEERZVJ8pXjOXDs"
P_URL = "fld0zTtCEvLKLFdvR"
P_PUBLISHED = "fldF9lh8M6zFgT0in"
P_VIEWS = "fldWnMGkOcpxFGHiC"
P_LIKES = "fldycGNHIr7D3z3Dv"
P_COMMENTS = "fldF0ZE8S1fd61Grn"
P_REPOSTS = "fld4rPNGljHg0ZCyp"
P_QUOTES = "fldHAdZg0PFwfi1yT"
P_BOOKMARKS = "fldtG96YoM3RS8Z7k"
P_MEDIA = "fldp6WngAiPjfzOfD"
P_STATUS = "fldzDBcbEeiojh8ct"
P_LAST_SCRAPED = "fldGGg2QCSALoXka0"

RUN_HOURS = 6             # schedule interval
WINDOW_HOURS = 8          # each scheduled run reads at least the last 8 hours
NEW_ACCOUNT_HOURS = 24    # a new account's first read goes back this far, no further
OVERLAP_MINUTES = 15      # a catch-up window starts this much before the last one ended
MAX_CATCHUP_HOURS = 72    # after failed runs, re-read at most this far back
HANDLES_PER_QUERY = 15    # X search takes ~22 operators: 15 from: + since/until/2 filters
GROUP_TOLERANCE_MINUTES = 60  # accounts whose Last Scraped are this close share a query
SLICE_HOURS = 8           # longer windows are read oldest-first in slices of this size
MAX_PAGES = 10            # pages per query (~200 tweets); past that, the query is narrowed
MAX_CALLS_PER_BATCH = 40  # credit guard per batch per run; a longer catch-up continues next run
RETRY_WAIT_SECONDS = 10   # twitterapi.io 429 / 5xx / network error: wait, then retry once
MAX_RETRY_WAIT = 60       # longest Retry-After honoured
LOOKUP_CHUNK = 50         # tweet IDs per Airtable lookup
LOCK_FILE = ROOT / ".x_scrape.lock"
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
CREDITS_PER_TWEET = 15    # twitterapi.io: $0.15 / 1k tweets, 1 USD = 100k credits
MIN_CREDITS_PER_CALL = 15

BATCH = 10  # Airtable's max records per write request
MAX_TEXT = 100000  # Airtable's long text limit


class HttpError(Exception):
    def __init__(self, message, code=None, retry_after=None):
        super().__init__(message)
        self.code = code                # HTTP status; None for network / invalid-response errors
        self.retry_after = retry_after  # Retry-After header, if the server sent one


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
    missing = [k for k in ("AIRTABLE_ACCESS_TOKEN", "TWITTER_API") if not os.environ.get(k)]
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
        path = urllib.parse.urlsplit(url).path
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code == 429 and attempt < rate_limit_waits:
                time.sleep(30)
                continue
            retry_after = e.headers.get("Retry-After") if e.headers else None
            raise HttpError(f"HTTP {e.code} from {path}: {detail}", e.code, retry_after) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise HttpError(f"Network error for {path}: {e}") from None
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise HttpError(f"Invalid JSON from {path}: {raw[:200]!r}") from None


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


# --- twitterapi.io --------------------------------------------------------

def twitterapi(path, params=None):
    url = f"{TW_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = request("GET", url, {"X-API-Key": os.environ["TWITTER_API"]})
    if isinstance(data, dict) and data.get("status") == "error":
        raise HttpError(f"{path} returned status=error: {str(data.get('msg') or data)[:300]}", 200)
    return data


def retryable(e):
    """twitterapi.io errors worth one retry: rate limit, server error, network error."""
    return isinstance(e, HttpError) and (e.code is None or e.code == 429 or e.code >= 500)


def search_page(params):
    """One advanced_search call, retried once (after Retry-After, else RETRY_WAIT_SECONDS) on a
    rate limit, server error or network error. Other errors, or a second failure, raise."""
    for attempt in (1, 2):
        try:
            data = twitterapi("/twitter/tweet/advanced_search", params)
        except HttpError as e:
            if attempt == 2 or not retryable(e):
                raise
            try:
                wait = min(MAX_RETRY_WAIT, max(1, int(e.retry_after)))
            except (TypeError, ValueError):
                wait = RETRY_WAIT_SECONDS
            log(f"  twitterapi.io: {e}; retrying once in {wait}s")
            time.sleep(wait)
            continue
        if not isinstance(data, dict) or not isinstance(data.get("tweets") or [], list):
            raise HttpError(f"unexpected advanced_search response: {str(data)[:200]}", 200)
        return data


def estimated_credits(tweet_count):
    """twitterapi.io reports no credit usage; estimate it from its published pricing."""
    return max(MIN_CREDITS_PER_CALL, tweet_count * CREDITS_PER_TWEET)


def fetch_tweets(handles, since, until, budget=None):
    """Tweets from any of `handles` between two unix times (since inclusive, until exclusive),
    newest first, paged as in the twitterapi.io guide: follow next_cursor while has_next_page.
    Stops early on an empty page or a page with no new IDs (so a stuck cursor can't burn
    credits), at MAX_PAGES, or when `budget` ({"calls": n}, shared by a batch) runs out.
    A first page that fails (after its retry) raises.
    Returns (tweets, calls, credits, incomplete) where incomplete is None or
    ("cap" | "budget" | "failed", reason) when the window was not fully read."""
    budget = budget if budget is not None else {"calls": MAX_CALLS_PER_BATCH}
    who = " OR ".join(f"from:{h}" for h in handles)
    query = f"({who}) since_time:{since} until_time:{until} -filter:replies -filter:retweets"
    tweets, seen, cursor, calls, credits = [], set(), "", 0, 0
    while True:
        if calls >= MAX_PAGES:
            return tweets, calls, credits, ("cap", f"stopped at MAX_PAGES={MAX_PAGES} with more pages left")
        if budget["calls"] <= 0:
            return tweets, calls, credits, ("budget", f"used all {MAX_CALLS_PER_BATCH} calls for this run")
        params = {"query": query, "queryType": "Latest"}
        if cursor:
            params["cursor"] = cursor
        budget["calls"] -= 1
        try:
            data = search_page(params)
        except Exception as e:
            if not calls:
                raise
            # A later page failed: keep what was already paid for and report it.
            return tweets, calls, credits, ("failed", f"page {calls + 1} failed: {e}")
        calls += 1
        page = data.get("tweets") or []
        credits += estimated_credits(len(page))
        page = [t for t in page if isinstance(t, dict) and t.get("id")]
        for t in page:
            t["id"] = str(t["id"])
        fresh = [t for t in page if t["id"] not in seen]
        seen.update(t["id"] for t in fresh)
        tweets.extend(fresh)
        cursor = data.get("next_cursor") or ""
        if not (data.get("has_next_page") and cursor and fresh):
            return tweets, calls, credits, None


# --- Mapping --------------------------------------------------------------

def to_int(value):
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_created(value):
    """'Wed Sep 23 10:36:09 +0000 2026' (or ISO) -> aware datetime (UTC), or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y").astimezone(timezone.utc)
    except ValueError:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z") if dt else None


def parse_iso(value):
    """Airtable dateTime string -> aware datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def is_original(tweet):
    """Original tweets only: no replies, retweets or quote tweets."""
    return not (tweet.get("isReply") or tweet.get("retweeted_tweet") or tweet.get("quoted_tweet"))


def tweet_account(tweet, by_id, by_handle):
    """The tracked account that posted `tweet`: by author user ID (stable across handle
    changes), else by handle. None if the author is not one of the batch's accounts."""
    author = tweet.get("author")
    if not isinstance(author, dict):
        return None
    return by_id.get(str(author.get("id") or "")) or by_handle.get(str(author.get("userName") or "").lower())


def media_files(tweet):
    """Attachment list for the Media field from extendedEntities.media[]:
    photo -> original-size image, video / animated_gif -> highest-bitrate mp4."""
    def as_dict(v):
        return v if isinstance(v, dict) else {}

    def as_list(v):
        return v if isinstance(v, list) else []

    tid = tweet.get("id")
    files = []
    for n, media in enumerate(as_list(as_dict(tweet.get("extendedEntities")).get("media")), 1):
        media = as_dict(media)
        kind = media.get("type")
        url = media.get("media_url_https")
        if kind == "photo" and isinstance(url, str) and url:
            ext = os.path.splitext(urllib.parse.urlsplit(url).path)[1] or ".jpg"
            files.append({"url": f"{url}?name=orig", "filename": f"{tid}_{n}{ext}"})
        elif kind in ("video", "animated_gif"):
            mp4s = [v for v in map(as_dict, as_list(as_dict(media.get("video_info")).get("variants")))
                    if v.get("content_type") == "video/mp4" and isinstance(v.get("url"), str) and v["url"]]
            if mp4s:
                best = max(mp4s, key=lambda v: to_int(v.get("bitrate")) or 0)
                files.append({"url": best["url"], "filename": f"{tid}_{n}.mp4"})
    return files


def post_fields(tweet, account_id, now, existing=None):
    """Airtable fields for one tweet (Media is added separately). `existing` is None for a
    new tweet, otherwise the stored record's info. Status only on create, never on update."""
    fields = {
        P_TEXT: html.unescape(str(tweet.get("text") or ""))[:MAX_TEXT] or None,
        P_TWEET_ID: tweet.get("id"),
        P_ACCOUNT: [account_id],
        P_URL: tweet.get("url") if isinstance(tweet.get("url"), str) else None,
        P_PUBLISHED: iso(parse_created(tweet.get("createdAt"))),
        P_VIEWS: to_int(tweet.get("viewCount")),
        P_LIKES: to_int(tweet.get("likeCount")),
        P_COMMENTS: to_int(tweet.get("replyCount")),
        P_REPOSTS: to_int(tweet.get("retweetCount")),
        P_QUOTES: to_int(tweet.get("quoteCount")),
        P_BOOKMARKS: to_int(tweet.get("bookmarkCount")),
        P_LAST_SCRAPED: now,
    }
    if existing is None:
        fields[P_STATUS] = "New"
    # Omit missing or empty values rather than blanking out what's already stored.
    return {k: v for k, v in fields.items() if v is not None and v != "" and v != []}


# --- Main -----------------------------------------------------------------

def valid_handle(account):
    handle = (account["fields"].get(A_HANDLE) or "").strip().lstrip("@")
    return handle if HANDLE_RE.match(handle) else None


def last_end(account, now_dt):
    """End of the account's last fully read window (its Last Scraped), if within
    MAX_CATCHUP_HOURS. None for a new account, one paused for long, or a future date."""
    end = parse_iso(account["fields"].get(A_LAST_SCRAPED))
    return end if end and now_dt - timedelta(hours=MAX_CATCHUP_HOURS) <= end <= now_dt else None


def window_start(accounts, now_dt):
    """Unix start of the fetch window for a batch: WINDOW_HOURS back, or the oldest of its
    accounts' last window ends minus OVERLAP_MINUTES if that's earlier (so a failed run's gap
    gets re-read). A batch of new accounts (none has a recent window) starts
    NEW_ACCOUNT_HOURS back."""
    ends = [e for e in (last_end(a, now_dt) for a in accounts) if e]
    if not ends:
        return int((now_dt - timedelta(hours=NEW_ACCOUNT_HOURS)).timestamp())
    start = min(min(ends) - timedelta(minutes=OVERLAP_MINUTES), now_dt - timedelta(hours=WINDOW_HOURS))
    return int(start.timestamp())


def group_accounts(accounts, now_dt):
    """Batches of at most HANDLES_PER_QUERY accounts whose windows end within
    GROUP_TOLERANCE_MINUTES of each other, so one account that's behind (after a failure or a
    short pause) is read in its own query instead of making the others re-read (and re-pay
    for) its longer window. New accounts are batched only with each other, for their 24 hours."""
    tol = timedelta(minutes=GROUP_TOLERANCE_MINUTES)
    dated = sorted((a for a in accounts if last_end(a, now_dt)), key=lambda a: last_end(a, now_dt), reverse=True)
    groups = []
    for a in dated:
        if groups and len(groups[-1]) < HANDLES_PER_QUERY and last_end(groups[-1][0], now_dt) - last_end(a, now_dt) <= tol:
            groups[-1].append(a)
        else:
            groups.append([a])
    new = [a for a in accounts if not last_end(a, now_dt)]
    return groups + [new[i:i + HANDLES_PER_QUERY] for i in range(0, len(new), HANDLES_PER_QUERY)]


def slices(since, until):
    """[start, end) pieces of at most SLICE_HOURS, oldest first. A normal 8-hour window is one."""
    out, step = [], SLICE_HOURS * 3600
    while since < until:
        out.append((since, min(until, since + step)))
        since += step
    return out


def lookup_existing(ids, known):
    """Look up in X Posts only the given tweet IDs not yet in `known` (tweet ID -> {"id",
    "media"}, or None when not in Airtable), LOOKUP_CHUNK IDs per filtered query, instead of
    loading the whole table."""
    todo = [i for i in dict.fromkeys(ids) if i not in known]
    for i in range(0, len(todo), LOOKUP_CHUNK):
        chunk = todo[i:i + LOOKUP_CHUNK]
        quoted = ",".join("{%s}='%s'" % (P_TWEET_ID, t.replace("\\", "\\\\").replace("'", "\\'")) for t in chunk)
        body = {"filterByFormula": f"OR({quoted})", "fields": [P_TWEET_ID, P_MEDIA],
                "returnFieldsByFieldId": True, "pageSize": 100}
        for tid in chunk:
            known[tid] = None
        while True:
            page = airtable("POST", f"{POSTS}/listRecords", body=body)
            for r in page.get("records", []):
                tid = r["fields"].get(P_TWEET_ID)
                if tid in known and known[tid] is None:
                    # Empty attachment fields are left out of Airtable's response: presence = filled.
                    known[tid] = {"id": r["id"], "media": bool(r["fields"].get(P_MEDIA))}
            if not page.get("offset"):
                break
            body["offset"] = page["offset"]


def save_tweets(tweets, accounts, handles, known, now, per, batch, pid_updates):
    """Match one read's tweets to the batch's accounts, skip non-originals, and create or
    update them in X Posts. Returns the problems to flag. Raises if an Airtable write fails
    (other than a 422, which is retried one record at a time)."""
    by_id = {str(a["fields"].get(A_PLATFORM_ID) or "").strip(): a for a in accounts}
    by_id.pop("", None)
    by_handle = {h.lower(): a for h, a in zip(handles, accounts)}
    matched, unreadable, unmatched = [], [], 0
    for tweet in tweets:
        # One malformed tweet must not block the batch: skip it, save the rest, flag it.
        try:
            account = tweet_account(tweet, by_id, by_handle)
            if account is None:
                unmatched += 1
            elif not is_original(tweet):
                batch["skipped"] += 1
            else:
                matched.append((tweet, account))
        except Exception as e:
            unreadable.append(f"{tweet.get('id')} ({type(e).__name__}: {e})")
    lookup_existing([t["id"] for t, _ in matched], known)

    creates, updates = [], []
    for tweet, account in matched:
        tid = tweet["id"]
        try:
            info = known.get(tid)
            record = {"fields": post_fields(tweet, account["id"], now, info)}
            # Attach media once; never re-send a filled field (that would duplicate files).
            if info is None or not info["media"]:
                files = media_files(tweet)
                if files:
                    record["fields"][P_MEDIA] = files
        except Exception as e:
            unreadable.append(f"{tid} ({type(e).__name__}: {e})")
            continue
        # Handles change, user IDs don't: save the author's ID to an account that has none.
        author_id = str(tweet["author"].get("id") or "")
        if author_id.isdigit() and not str(account["fields"].get(A_PLATFORM_ID) or "").strip():
            pid_updates[account["id"]] = author_id
        if info is None:
            creates.append(record)
        else:
            record["id"] = info["id"]
            updates.append(record)

    problems = []
    batch["unmatched"] += unmatched
    if unreadable:
        problems.append(f"{len(unreadable)} tweet(s) skipped as unreadable: {'; '.join(unreadable)[:1000]}")
    if unmatched:
        problems.append(f"{unmatched} tweet(s) skipped: author not one of this batch's accounts")

    for method, records, key in (("PATCH", updates, "updated"), ("POST", creates, "created")):
        for i in range(0, len(records), BATCH):
            chunk = records[i:i + BATCH]
            try:
                pairs = list(zip(chunk, write_batches(method, POSTS, chunk)))
            except HttpError as e:
                if e.code != 422:
                    raise
                # One invalid record makes Airtable reject the whole chunk, which would
                # repeat every run. Write one at a time (free) and skip only the bad one.
                pairs = []
                for one in chunk:
                    try:
                        pairs += list(zip([one], write_batches(method, POSTS, [one])))
                    except HttpError as e1:
                        if e1.code != 422:
                            raise
                        per[one["fields"][P_ACCOUNT][0]]["bad"].append(
                            f"{one['fields'][P_TWEET_ID]} (Airtable rejected it: {e1})")
            for sent, rec in pairs:
                st = per[sent["fields"][P_ACCOUNT][0]]
                st[key] += 1
                st["media_added"] += len(sent["fields"].get(P_MEDIA, []))
                info = known.get(sent["fields"][P_TWEET_ID]) or {"id": rec["id"], "media": False}
                info["media"] = info["media"] or P_MEDIA in sent["fields"]
                known[sent["fields"][P_TWEET_ID]] = info
    return problems


def new_stats():
    return {"created": 0, "updated": 0, "media_added": 0, "bad": []}


def scrape_batch(accounts, known, now):
    """Read and save one batch's window, slice by slice (oldest first). Returns (per-account
    stats, batch stats, problems for every account in the batch, Platform ID updates).
    batch["covered"] is the end of the last slice fully read and saved (None if none was):
    Last Scraped may move there, never past anything unread. For new accounts it starts at the
    window start, so if their first read fails the next run still reads their full 24 hours."""
    now_dt = parse_iso(now)
    since, until = window_start(accounts, now_dt), int(now_dt.timestamp())
    handles = [valid_handle(a) for a in accounts]
    per = {a["id"]: new_stats() for a in accounts}
    new = not any(last_end(a, now_dt) for a in accounts)
    batch = {"since": iso(datetime.fromtimestamp(since, timezone.utc)), "returned": 0, "skipped": 0,
             "unmatched": 0, "calls": 0, "credits": 0, "covered": since if new else None, "backlog": None}
    log(f"  Window {batch['since']} -> {now} for {', '.join(handles)}")
    problems, pid_updates = [], {}
    budget = {"calls": MAX_CALLS_PER_BATCH}
    pending = [(a, b, b) for a, b in slices(since, until)]  # (read from, read until, slice end)
    while pending:
        a, b, end = pending.pop(0)
        try:
            tweets, calls, credits, incomplete = fetch_tweets(handles, a, b, budget)
        except Exception as e:  # nothing read; this slice and later ones wait for the next run
            problems.append(f"{type(e).__name__}: {e}")
            break
        batch["calls"] += calls
        batch["credits"] += credits
        batch["returned"] += len(tweets)
        try:
            problems += save_tweets(tweets, accounts, handles, known, now, per, batch, pid_updates)
        except Exception as e:  # tweets already written stay; the slice is re-read next run
            problems.append(f"Airtable write failed: {type(e).__name__}: {e}")
            break
        if incomplete is None:
            batch["covered"] = end
            continue
        kind, reason = incomplete
        if kind == "cap":
            # Results come newest first, so everything after the oldest tweet read is saved.
            # Read the older rest of the slice with a narrower query (no tweet is paid twice,
            # apart from the boundary second).
            oldest = min((d for d in (parse_created(t.get("createdAt")) for t in tweets) if d), default=None)
            narrower = int(oldest.timestamp()) + 1 if oldest else None
            if narrower is not None and a < narrower < b:
                pending.insert(0, (a, narrower, end))
                continue
            problems.append(f"{reason}; the rest of this slice could not be read")
            batch["covered"] = end  # nothing narrower to ask for; don't loop on it every run
            continue
        if kind == "budget":
            batch["backlog"] = f"{reason}; the rest of the window continues next run"
        else:
            problems.append(reason)
        break
    if batch["covered"] is not None:
        batch["covered"] = iso(datetime.fromtimestamp(batch["covered"], timezone.utc))
    return per, batch, problems, pid_updates


def main(new_accounts=False):
    """new_accounts: read only the accounts with no recent Last Scraped (just added)."""
    load_env()
    started = datetime.now(timezone.utc)
    log("X scrape started" + (" (new accounts only)" if new_accounts else ""))

    accounts = [
        a for a in list_records(ACCOUNTS, [A_NAME, A_PLATFORM, A_PLATFORM_ID, A_HANDLE, A_SCRAPE,
                                           A_LAST_SCRAPED, A_LAST_STATUS])
        if a["fields"].get(A_SCRAPE) == "Active" and a["fields"].get(A_PLATFORM) == "X"
    ]
    if new_accounts:
        accounts = [a for a in accounts if not last_end(a, started)]
        log(f"{len(accounts)} new X account(s)")
    else:
        log(f"{len(accounts)} active X account(s)")

    known = {}  # tweet ID -> {"id", "media"} (None = not in Airtable), filled per read
    valid = [a for a in accounts if valid_handle(a)]
    invalid = [a for a in accounts if not valid_handle(a)]
    results, batches, updates = [], [], []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    for a in invalid:
        error = f"{now} Handle is empty or not a valid X handle: {a['fields'].get(A_HANDLE)!r}"
        updates.append({"id": a["id"], "fields": {A_LAST_STATUS: "error", A_SCRAPE_ERROR: error}})
        results.append((a, "error", new_stats(), error))

    for n, group in enumerate(group_accounts(valid, parse_iso(now)), 1):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        log(f"Batch {n}: {len(group)} account(s)")
        per, batch, problems, pid_updates = scrape_batch(group, known, now)
        batches.append(batch)
        for a in group:
            st = per[a["id"]]
            issues = problems + ([f"{len(st['bad'])} tweet(s) not saved: {'; '.join(st['bad'])[:1000]}"]
                                 if st["bad"] else [])
            status = "error" if issues else "ok"
            error = f"{now} " + " | ".join(issues) if issues else None
            # None clears Scrape Error in Airtable (checked against this base).
            fields = {A_LAST_STATUS: status, A_SCRAPE_ERROR: error[:5000] if error else None}
            if batch["covered"]:
                fields[A_LAST_SCRAPED] = batch["covered"]
            if a["id"] in pid_updates:
                fields[A_PLATFORM_ID] = pid_updates[a["id"]]
            updates.append({"id": a["id"], "fields": fields})
            results.append((a, status, st, error))
            if error:
                log(f"  ERROR {a['fields'].get(A_NAME)}: {error}")

    for i in range(0, len(updates), BATCH):
        try:
            airtable("PATCH", ACCOUNTS, body={"records": updates[i:i + BATCH]})
        except HttpError as e:
            log(f"  Could not update account rows: {e}")

    print()
    print("Summary")
    for n, b in enumerate(batches, 1):
        print(f"  Batch {n}: window from {b['since']}, read up to {b['covered'] or 'nothing (see errors)'}"
              f" | {b['returned']} returned, {b['skipped']} quote/reply/retweet skipped,"
              f" {b['unmatched']} from other authors | calls {b['calls']}, est. credits {b['credits']}")
        if b["backlog"]:
            print(f"    note: {b['backlog']}")
    for a, status, st, error in results:
        print(f"  {a['fields'].get(A_NAME) or a['id']}: {status} | created {st['created']},"
              f" updated {st['updated']} | media files added {st['media_added']}")
        if error:
            print(f"    error: {error}")
    total = sum(b["credits"] for b in batches)
    print(f"  Total est. credits used: {total} (~${total / 100000:.4f})")
    print(f"  Duration: {(datetime.now(timezone.utc) - started).total_seconds():.1f}s")

    return 1 if any(r[1] == "error" for r in results) else 0


def acquire_lock():
    """Single-flight: an exclusive lock on LOCK_FILE, or None if another run holds it.
    (On GitHub Actions each run is its own machine: use a workflow `concurrency` group.)"""
    handle = open(LOCK_FILE, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


if __name__ == "__main__":
    args = sys.argv[1:]
    if args not in ([], ["--new-accounts"]):
        sys.exit("Usage: x.py [--new-accounts]")
    lock = acquire_lock()
    if lock is None:
        print("Another x.py run is in progress; exiting.")
        sys.exit(0)
    sys.exit(main(new_accounts=bool(args)))
