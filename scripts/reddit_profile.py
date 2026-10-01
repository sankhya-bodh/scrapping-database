#!/usr/bin/env python3
"""Set up a new subreddit from its Profile URL, for the Content OS Database Airtable base.

Started by the Reddit new account workflow (reddit-new-account.yml) with the Accounts record
ID that the Airtable form created. The form fills Profile URL (and Platform = Reddit).
  1. The subreddit = what follows "reddit.com/r/" up to the next "/" (or "?" / "#").
     www., old., new. and m. links, "r/name" and a bare name work too. Subreddit names are
     2-21 letters, digits or _; anything else is flagged, with no API call.
  2. If another Reddit row already has that name, or (after step 3) that subreddit ID, the
     new row is flagged as a duplicate and not activated.
  3. ScrapeCreators GET /v1/reddit/subreddit/details (1 credit). It's case-sensitive
     (r/claudeai is "not found", r/ClaudeAI works) and a miss costs nothing, so on a miss the
     right capitalization is read from the subreddit's posts (1 credit) and it's asked again.
  4. The row gets Name (r/...), Platform = Reddit, Platform ID (t5_...), Handle, Profile URL,
     Avatar URL, Bio, Last Scrape Status = never, and Scrape = Active in the same write.
     Last Scraped stays empty, so reddit.py --new-accounts then reads the top of the week.
A row that fails is left with Scrape not Active, Last Scrape Status = error and the reason in
Scrape Error; the run exits 1. A row that already has a Platform ID (set up before, or an
existing subreddit, active or paused) is left alone (exit 0).

  python3 scripts/reddit_profile.py recXXXXXXXXXXXXXX

Keys come from ../.env or the environment: AIRTABLE_ACCESS_TOKEN, SCRAPE_CREATORS.
"""

import html
import re
import sys
from datetime import datetime, timezone

import reddit as rd
from reddit import (ACCOUNTS, A_NAME, A_PLATFORM, A_PLATFORM_ID, A_HANDLE, A_SCRAPE,
                    A_LAST_STATUS, A_SCRAPE_ERROR, HttpError, log)

# Accounts fields only this script writes (the rest are imported from reddit.py)
A_PROFILE_URL = "fldsMfa0gZRwsnVGv"
A_AVATAR = "fldMODmLL1BXXhhj3"
A_BIO = "fldLfX8iZAcNbz28q"

RECORD_RE = re.compile(r"^rec[A-Za-z0-9]{14}$")
URL_RE = re.compile(r"^(?:https?://)?(?:(?:www|old|new|m|np)\.)?reddit\.com/r/([^/?#]*)", re.I)
NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")
SUBREDDIT_ID_RE = re.compile(r"^t5_[a-z0-9]+$")
MAX_TEXT = 100000  # Airtable's long text limit


def subreddit_from_url(text):
    """'https://www.reddit.com/r/ClaudeAI/' -> 'ClaudeAI'. None if it isn't a subreddit link."""
    text = str(text or "").strip()
    m = URL_RE.match(text)
    if m:
        name = m.group(1)
    else:
        name = text.strip("/")
        name = name[2:] if name.lower().startswith("r/") else name
    return name if NAME_RE.match(name) else None


def details(name):
    """The subreddit's details, or None if ScrapeCreators doesn't know that exact name (free)."""
    try:
        return rd.scrapecreators("/v1/reddit/subreddit/details", {"subreddit": name})
    except HttpError as e:
        if "not_found" in str(e) or "HTTP 404" in str(e) or "exist" in str(e):
            return None
        raise


def subreddit_fields(info):
    """Accounts fields from a ScrapeCreators /v1/reddit/subreddit/details response."""
    name = str(info.get("display_name") or "")
    avatar = info.get("icon_img") or info.get("community_icon")
    fields = {
        A_NAME: f"r/{name}",
        A_PLATFORM: "Reddit",
        A_PLATFORM_ID: str(info.get("subreddit_id") or ""),
        A_HANDLE: name,
        A_PROFILE_URL: f"https://www.reddit.com/r/{name}/",
        A_AVATAR: html.unescape(avatar) if isinstance(avatar, str) and avatar.startswith("http") else None,
        A_BIO: str(info.get("public_description") or info.get("description") or "")[:MAX_TEXT] or None,
    }
    return {k: v for k, v in fields.items() if v is not None and v != ""}


def find_other(record_id, field, value):
    """Another Reddit row whose `field` equals `value` (case-insensitive), or None.
    `value` is a validated name or subreddit ID, so it's safe in the formula."""
    formula = (f"AND({{{A_PLATFORM}}}='Reddit', LOWER({{{field}}})='{value.lower()}',"
               f" RECORD_ID()!='{record_id}')")
    body = {"filterByFormula": formula, "fields": [A_NAME, A_HANDLE],
            "returnFieldsByFieldId": True, "pageSize": 1}
    records = rd.airtable("POST", f"{ACCOUNTS}/listRecords", body=body).get("records", [])
    return records[0] if records else None


def duplicate_error(other, what):
    name = other["fields"].get(A_NAME) or other["fields"].get(A_HANDLE) or ""
    return f"Already tracked: {what} is on record {other['id']} ({name}). This row was not activated."


def setup(record_id):
    """Enrich one Accounts row. Returns 0 if it's set up (or already was), 1 if it failed."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def fail(message):
        log(f"ERROR {record_id}: {message}")
        fields = {A_PLATFORM: "Reddit", A_LAST_STATUS: "error", A_SCRAPE_ERROR: f"{now} {message}"[:5000]}
        try:
            rd.airtable("PATCH", ACCOUNTS, body={"records": [{"id": record_id, "fields": fields}]})
        except HttpError as e:
            log(f"  Could not update the row: {e}")
        return 1

    record = rd.airtable("GET", f"{ACCOUNTS}/{record_id}", {"returnFieldsByFieldId": "true"})
    f = record.get("fields", {})
    if f.get(A_PLATFORM) not in (None, "Reddit"):
        log(f"{record_id} is a {f[A_PLATFORM]} account, not Reddit: nothing to do")
        return 0
    if f.get(A_PLATFORM_ID):
        log(f"{record_id} ({f.get(A_NAME)}) is already set up: nothing to do")
        return 0

    source = f.get(A_PROFILE_URL) or f.get(A_HANDLE)
    name = subreddit_from_url(source)
    if not name:
        return fail(f"Profile URL {source!r} is not a subreddit link like https://www.reddit.com/r/ClaudeAI"
                    " (a subreddit name is 2-21 letters, digits or _).")
    other = find_other(record_id, A_HANDLE, name)
    if other:
        return fail(duplicate_error(other, f"r/{name}"))

    credits = 0
    try:
        info = details(name)
        if info is None:
            # Details need the exact capitalization; the posts listing doesn't, and its posts
            # carry the subreddit's real name.
            posts, charged, _ = rd.fetch_posts(name, "week")
            credits += charged or 0
            real = next((p.get("subreddit") for p in posts if str(p.get("subreddit") or "").lower() == name.lower()), None)
            info = details(real) if real and real != name else None
        if info is None:
            return fail(f"Subreddit r/{name} not found on ScrapeCreators. Check the spelling in the Profile URL.")
    except HttpError as e:
        return fail(f"Could not load r/{name} from ScrapeCreators: {e}")
    credits += info.get("credits_charged") or 0
    fields = subreddit_fields(info)
    if not SUBREDDIT_ID_RE.match(fields.get(A_PLATFORM_ID, "")) or not NAME_RE.match(fields.get(A_HANDLE, "")):
        return fail(f"Subreddit r/{name} not found (no subreddit ID in the ScrapeCreators response).")
    other = find_other(record_id, A_PLATFORM_ID, fields[A_PLATFORM_ID])
    if other:
        return fail(duplicate_error(other, f"r/{fields[A_HANDLE]} ({fields[A_PLATFORM_ID]})"))

    # Activated in the same write as the details: the scraper never sees a half-filled row.
    fields.update({A_SCRAPE: "Active", A_LAST_STATUS: "never", A_SCRAPE_ERROR: None})
    rd.airtable("PATCH", ACCOUNTS, body={"records": [{"id": record_id, "fields": fields}]})
    log(f"{record_id} set up: {fields[A_NAME]} ({fields[A_PLATFORM_ID]}). ScrapeCreators credits used:"
        f" {credits}, left: {info.get('credits_remaining')}")
    return 0


def main(argv):
    if len(argv) != 1 or not RECORD_RE.match(argv[0].strip()):
        sys.exit("Usage: reddit_profile.py recXXXXXXXXXXXXXX (an Accounts record ID)")
    rd.load_env()
    return setup(argv[0].strip())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
