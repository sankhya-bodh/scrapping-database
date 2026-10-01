#!/usr/bin/env python3
"""Set up a new X account from its Profile URL, for the Content OS Database Airtable base.

Started by the X new account workflow (x-new-account.yml) with the Accounts record ID that the
Airtable form created. The form only fills Profile URL, e.g. https://x.com/eptwts.
  1. Username = what follows the domain's "/" up to the next "/" (or "?" / "#"), without "@".
     x.com and twitter.com links, links without https:// and a bare username all work.
     X usernames are 1-15 letters, digits or _; anything else is flagged, with no API call.
  2. If another X account row already has that username, or (after step 3) that user ID,
     the new row is flagged as a duplicate and not activated.
  3. ScrapeCreators GET /v1/twitter/profile?handle= (1 credit).
  4. The row gets Name, Platform = X, Platform ID, Handle, Profile URL, Avatar URL, Bio
     (t.co links expanded), Verified, Followers, Following, Last Scrape Status = never, and
     Scrape = Active last. Last Scraped stays empty, so x.py --new-accounts then reads the
     account's last 24 hours.
A row that fails is left with Scrape not Active, Last Scrape Status = error and the reason in
Scrape Error; the run exits 1. A row that already has a Platform ID (set up before, or an
existing account, active or paused) is left alone (exit 0), so a repeated webhook call costs
nothing and never re-activates a paused account.

  python3 scripts/x_profile.py recXXXXXXXXXXXXXX

Keys come from ../.env or the environment: AIRTABLE_ACCESS_TOKEN, SCRAPE_CREATORS.
"""

import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone

import x
from x import (ACCOUNTS, A_NAME, A_PLATFORM, A_PLATFORM_ID, A_HANDLE, A_SCRAPE,
               A_LAST_STATUS, A_SCRAPE_ERROR, HANDLE_RE, HttpError, log)

SC_API = "https://api.scrapecreators.com"

# Accounts fields only this script writes (the rest are imported from x.py)
A_PROFILE_URL = "fldsMfa0gZRwsnVGv"
A_AVATAR = "fldMODmLL1BXXhhj3"
A_BIO = "fldLfX8iZAcNbz28q"
A_VERIFIED = "fld7vjwHoX60TZkAz"
A_FOLLOWERS = "fldA195eXeZnOBSU4"
A_FOLLOWING = "fld06NkkzzXjnRgo6"

RECORD_RE = re.compile(r"^rec[A-Za-z0-9]{14}$")
URL_RE = re.compile(r"^(?:https?://)?(?:www\.|mobile\.)?(?:x|twitter)\.com/([^/?#]*)", re.I)


def handle_from_url(text):
    """'https://x.com/eptwts' -> 'eptwts'. None if it's not an X profile link (or username)."""
    text = str(text or "").strip()
    m = URL_RE.match(text)
    handle = (m.group(1) if m else text).strip().lstrip("@")
    return handle if HANDLE_RE.match(handle) else None


def scrapecreators(path, params):
    url = f"{SC_API}{path}?" + urllib.parse.urlencode(params)
    data = x.request("GET", url, {"x-api-key": os.environ["SCRAPE_CREATORS"]})
    if not isinstance(data, dict) or data.get("success") is False:
        raise HttpError(f"{path} returned an error: {str(data.get('message') if isinstance(data, dict) else data)[:300]}")
    return data


def bio(legacy):
    """legacy.description with its t.co links replaced by the full URLs."""
    text = str(legacy.get("description") or "")
    urls = ((legacy.get("entities") or {}).get("description") or {}).get("urls") or []
    for u in urls:
        if isinstance(u, dict) and u.get("url") and u.get("expanded_url"):
            text = text.replace(u["url"], u["expanded_url"])
    return text


def profile_fields(profile, handle):
    """Accounts fields from a ScrapeCreators X profile. Newer responses put name and
    screen_name under core, older ones under legacy: both are read."""
    core = profile.get("core") if isinstance(profile.get("core"), dict) else {}
    legacy = profile.get("legacy") if isinstance(profile.get("legacy"), dict) else {}
    screen = core.get("screen_name") or legacy.get("screen_name")
    screen = screen if isinstance(screen, str) and HANDLE_RE.match(screen) else handle
    avatar = (profile.get("avatar") or {}).get("image_url") or legacy.get("profile_image_url_https")
    fields = {
        A_NAME: core.get("name") or legacy.get("name") or screen,
        A_PLATFORM: "X",
        A_PLATFORM_ID: str(profile.get("rest_id") or ""),
        A_HANDLE: screen,
        A_PROFILE_URL: f"https://x.com/{screen}",
        A_AVATAR: avatar.replace("_normal.", "_400x400.") if isinstance(avatar, str) and avatar else None,
        A_BIO: bio(legacy) or None,
        A_VERIFIED: bool(profile.get("is_blue_verified")),
        A_FOLLOWERS: x.to_int(legacy.get("followers_count")),
        A_FOLLOWING: x.to_int(legacy.get("friends_count")),
    }
    return {k: v for k, v in fields.items() if v is not None and v != ""}


def find_other(record_id, field, value):
    """Another X account row whose `field` equals `value` (case-insensitive), or None.
    `value` is a validated username or a numeric user ID, so it's safe in the formula."""
    formula = (f"AND({{{A_PLATFORM}}}='X', LOWER({{{field}}})='{value.lower()}',"
               f" RECORD_ID()!='{record_id}')")
    body = {"filterByFormula": formula, "fields": [A_NAME, A_HANDLE],
            "returnFieldsByFieldId": True, "pageSize": 1}
    records = x.airtable("POST", f"{ACCOUNTS}/listRecords", body=body).get("records", [])
    return records[0] if records else None


def duplicate_error(other, what):
    name = other["fields"].get(A_NAME) or other["fields"].get(A_HANDLE) or ""
    return f"Already tracked: {what} is on record {other['id']} ({name}). This row was not activated."


def setup(record_id):
    """Enrich one Accounts row. Returns 0 if it's set up (or already was), 1 if it failed."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def fail(message):
        log(f"ERROR {record_id}: {message}")
        fields = {A_PLATFORM: "X", A_LAST_STATUS: "error", A_SCRAPE_ERROR: f"{now} {message}"[:5000]}
        try:
            x.airtable("PATCH", ACCOUNTS, body={"records": [{"id": record_id, "fields": fields}]})
        except HttpError as e:
            log(f"  Could not update the row: {e}")
        return 1

    record = x.airtable("GET", f"{ACCOUNTS}/{record_id}", {"returnFieldsByFieldId": "true"})
    f = record.get("fields", {})
    if f.get(A_PLATFORM) not in (None, "X"):
        log(f"{record_id} is a {f[A_PLATFORM]} account, not X: nothing to do")
        return 0
    if f.get(A_PLATFORM_ID):
        log(f"{record_id} (@{f.get(A_HANDLE)}) is already set up: nothing to do")
        return 0

    source = f.get(A_PROFILE_URL) or f.get(A_HANDLE)
    handle = handle_from_url(source)
    if not handle:
        return fail(f"Profile URL {source!r} is not an X profile link like https://x.com/username"
                    " (an X username is 1-15 letters, digits or _).")
    other = find_other(record_id, A_HANDLE, handle)
    if other:
        return fail(duplicate_error(other, f"@{handle}"))

    try:
        profile = scrapecreators("/v1/twitter/profile", {"handle": handle})
    except HttpError as e:
        return fail(f"Could not load @{handle} from ScrapeCreators: {e}")
    fields = profile_fields(profile, handle)
    if not fields.get(A_PLATFORM_ID, "").isdigit():
        return fail(f"X account @{handle} not found (no user ID in the ScrapeCreators response).")
    other = find_other(record_id, A_PLATFORM_ID, fields[A_PLATFORM_ID])
    if other:
        return fail(duplicate_error(other, f"@{fields[A_HANDLE]} (user ID {fields[A_PLATFORM_ID]})"))

    # Activated in the same write as the details: the scraper never sees a half-filled row.
    fields.update({A_SCRAPE: "Active", A_LAST_STATUS: "never", A_SCRAPE_ERROR: None})
    x.airtable("PATCH", ACCOUNTS, body={"records": [{"id": record_id, "fields": fields}]})
    log(f"{record_id} set up: {fields[A_NAME]} (@{fields[A_HANDLE]}), user ID {fields[A_PLATFORM_ID]},"
        f" {fields.get(A_FOLLOWERS, 0)} followers. ScrapeCreators credits used:"
        f" {profile.get('credits_charged')}, left: {profile.get('credits_remaining')}")
    return 0


def main(argv):
    if len(argv) != 1 or not RECORD_RE.match(argv[0].strip()):
        sys.exit("Usage: x_profile.py recXXXXXXXXXXXXXX (an Accounts record ID)")
    x.load_env(("AIRTABLE_ACCESS_TOKEN", "SCRAPE_CREATORS"))
    return setup(argv[0].strip())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
