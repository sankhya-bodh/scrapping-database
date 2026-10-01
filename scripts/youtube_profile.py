#!/usr/bin/env python3
"""Set up a new YouTube channel from its Profile URL, for the Content OS Database Airtable base.

Started by the YouTube new account workflow (youtube-new-account.yml) with the Accounts record
ID that the Airtable form created. The form fills Profile URL (and Platform = YouTube).
  1. The channel = what follows "youtube.com/" up to the next "/" (or "?" / "#"):
       youtube.com/@nicksaraev(/videos)  -> handle nicksaraev
       youtube.com/channel/UC...          -> channel ID UC...
       youtube.com/c/Name, /user/Name     -> sent to ScrapeCreators as the URL
     m.youtube.com, links without https:// and a bare @handle work too. Anything else
     (a video link, another site) is flagged, with no API call.
  2. If another YouTube row already has that handle, or (after step 3) that channel ID, the
     new row is flagged as a duplicate and not activated.
  3. ScrapeCreators GET /v1/youtube/channel (1 credit).
  4. The row gets Name, Platform = YouTube, Platform ID (channel ID), Handle, Profile URL,
     Avatar URL (800px), Bio, Verified, Subscribers, Last Scrape Status = never, and
     Scrape = Active in the same write. Last Scraped stays empty, so
     youtube.py --new-accounts then stores the channel's 30 newest videos.
A row that fails is left with Scrape not Active, Last Scrape Status = error and the reason in
Scrape Error; the run exits 1. A row that already has a Platform ID (set up before, or an
existing channel, active or paused) is left alone (exit 0).

  python3 scripts/youtube_profile.py recXXXXXXXXXXXXXX

Keys come from ../.env or the environment: AIRTABLE_ACCESS_TOKEN, SCRAPE_CREATORS.
"""

import re
import sys
import urllib.parse
from datetime import datetime, timezone

import youtube as yt
from youtube import (ACCOUNTS, A_NAME, A_PLATFORM, A_PLATFORM_ID, A_HANDLE, A_SCRAPE,
                     A_LAST_STATUS, A_SCRAPE_ERROR, HttpError, log)

# Accounts fields only this script writes (the rest are imported from youtube.py)
A_PROFILE_URL = "fldsMfa0gZRwsnVGv"
A_AVATAR = "fldMODmLL1BXXhhj3"
A_BIO = "fldLfX8iZAcNbz28q"
A_VERIFIED = "fld7vjwHoX60TZkAz"
A_SUBSCRIBERS = "fld220GdTpf2asunO"

RECORD_RE = re.compile(r"^rec[A-Za-z0-9]{14}$")
URL_RE = re.compile(r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/([^?#]*)", re.I)
# YouTube handles are 3-30 characters in any script; this only keeps out what would break
# the URL or the Airtable formula (spaces, / ? #, quotes, backslash, @).
HANDLE_RE = re.compile(r"^[^\s/?#'\"\\@]{3,30}$")
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_\-]{22}$")
LEGACY_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")       # /c/Name and /user/Name


def channel_from_url(text):
    """The channel a Profile URL points to, as the ScrapeCreators parameter to look it up by:
    ("handle", "nicksaraev"), ("channelId", "UC..."), ("url", "https://www.youtube.com/c/Name"),
    or None if it isn't a YouTube channel link."""
    text = str(text or "").strip()
    m = URL_RE.match(text)
    if not m:
        handle = text[1:] if text.startswith("@") else None
        return ("handle", handle) if handle and HANDLE_RE.match(handle) else None
    parts = [p for p in m.group(1).split("/") if p]
    if not parts:
        return None
    first = parts[0]
    if first.startswith("@"):
        handle = urllib.parse.unquote(first[1:])
        return ("handle", handle) if HANDLE_RE.match(handle) else None
    if first == "channel" and len(parts) > 1 and CHANNEL_ID_RE.match(parts[1]):
        return ("channelId", parts[1])
    if first in ("c", "user") and len(parts) > 1 and LEGACY_RE.match(parts[1]):
        return ("url", f"https://www.youtube.com/{first}/{parts[1]}")
    return None


def avatar_url(avatar):
    """The largest avatar source, at 800px (the API's =s68 URL accepts any size)."""
    sources = ((avatar or {}).get("image") or {}).get("sources") if isinstance(avatar, dict) else None
    urls = [s.get("url") for s in sources or [] if isinstance(s, dict) and isinstance(s.get("url"), str)]
    return re.sub(r"=s\d+", "=s800", urls[-1], count=1) if urls else None


def channel_fields(channel, lookup):
    """Accounts fields from a ScrapeCreators /v1/youtube/channel response."""
    handle = str(channel.get("handle") or "").lstrip("@") or (lookup[1] if lookup[0] == "handle" else "")
    channel_id = str(channel.get("channelId") or "")
    fields = {
        A_NAME: channel.get("name") or handle or channel_id,
        A_PLATFORM: "YouTube",
        A_PLATFORM_ID: channel_id,
        A_HANDLE: handle or None,
        A_PROFILE_URL: f"https://www.youtube.com/@{handle}" if handle else f"https://www.youtube.com/channel/{channel_id}",
        A_AVATAR: avatar_url(channel.get("avatar")),
        A_BIO: channel.get("description") or None,
        A_VERIFIED: bool(channel.get("isVerified")),
        A_SUBSCRIBERS: yt.to_int(channel.get("subscriberCount")),
    }
    return {k: v for k, v in fields.items() if v is not None and v != ""}


def find_other(record_id, field, value):
    """Another YouTube row whose `field` equals `value` (case-insensitive), or None.
    `value` is a validated handle or channel ID, so it's safe in the formula."""
    formula = (f"AND({{{A_PLATFORM}}}='YouTube', LOWER({{{field}}})='{value.lower()}',"
               f" RECORD_ID()!='{record_id}')")
    body = {"filterByFormula": formula, "fields": [A_NAME, A_HANDLE],
            "returnFieldsByFieldId": True, "pageSize": 1}
    records = yt.airtable("POST", f"{ACCOUNTS}/listRecords", body=body).get("records", [])
    return records[0] if records else None


def duplicate_error(other, what):
    name = other["fields"].get(A_NAME) or other["fields"].get(A_HANDLE) or ""
    return f"Already tracked: {what} is on record {other['id']} ({name}). This row was not activated."


def setup(record_id):
    """Enrich one Accounts row. Returns 0 if it's set up (or already was), 1 if it failed."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def fail(message):
        log(f"ERROR {record_id}: {message}")
        fields = {A_PLATFORM: "YouTube", A_LAST_STATUS: "error", A_SCRAPE_ERROR: f"{now} {message}"[:5000]}
        try:
            yt.airtable("PATCH", ACCOUNTS, body={"records": [{"id": record_id, "fields": fields}]})
        except HttpError as e:
            log(f"  Could not update the row: {e}")
        return 1

    record = yt.airtable("GET", f"{ACCOUNTS}/{record_id}", {"returnFieldsByFieldId": "true"})
    f = record.get("fields", {})
    if f.get(A_PLATFORM) not in (None, "YouTube"):
        log(f"{record_id} is a {f[A_PLATFORM]} account, not YouTube: nothing to do")
        return 0
    if f.get(A_PLATFORM_ID):
        log(f"{record_id} ({f.get(A_NAME)}) is already set up: nothing to do")
        return 0

    source = f.get(A_PROFILE_URL) or (f"@{f[A_HANDLE].lstrip('@')}" if f.get(A_HANDLE) else None)
    lookup = channel_from_url(source)
    if not lookup:
        return fail(f"Profile URL {source!r} is not a YouTube channel link like"
                    " https://www.youtube.com/@handle or https://www.youtube.com/channel/UC...")
    other = find_other(record_id, A_HANDLE if lookup[0] == "handle" else A_PLATFORM_ID, lookup[1]) \
        if lookup[0] != "url" else None
    if other:
        return fail(duplicate_error(other, f"{'@' if lookup[0] == 'handle' else ''}{lookup[1]}"))

    try:
        channel = yt.scrapecreators("/v1/youtube/channel", {lookup[0]: lookup[1]})
    except HttpError as e:
        return fail(f"Could not load {lookup[1]} from ScrapeCreators: {e}")
    fields = channel_fields(channel, lookup)
    if not CHANNEL_ID_RE.match(fields.get(A_PLATFORM_ID, "")):
        return fail(f"YouTube channel {lookup[1]} not found (no channel ID in the ScrapeCreators response).")
    other = find_other(record_id, A_PLATFORM_ID, fields[A_PLATFORM_ID])
    if other:
        return fail(duplicate_error(other, f"channel {fields[A_PLATFORM_ID]}"))

    # Activated in the same write as the details: the scraper never sees a half-filled row.
    fields.update({A_SCRAPE: "Active", A_LAST_STATUS: "never", A_SCRAPE_ERROR: None})
    yt.airtable("PATCH", ACCOUNTS, body={"records": [{"id": record_id, "fields": fields}]})
    log(f"{record_id} set up: {fields[A_NAME]} ({fields[A_PROFILE_URL]}), channel {fields[A_PLATFORM_ID]},"
        f" {fields.get(A_SUBSCRIBERS, 0)} subscribers. ScrapeCreators credits used:"
        f" {channel.get('credits_charged')}, left: {channel.get('credits_remaining')}")
    return 0


def main(argv):
    if len(argv) != 1 or not RECORD_RE.match(argv[0].strip()):
        sys.exit("Usage: youtube_profile.py recXXXXXXXXXXXXXX (an Accounts record ID)")
    yt.load_env()
    return setup(argv[0].strip())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
