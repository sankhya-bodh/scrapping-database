"""Offline tests for scripts/x_profile.py. No network: ScrapeCreators and Airtable are fakes.
Run: python3 tests/test_x_profile.py"""
import copy, os, re, sys

ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT + "/scripts")
import x
import x_profile as xp

os.environ.update(AIRTABLE_ACCESS_TOKEN="fake", SCRAPE_CREATORS="fake")
x.log = xp.log = lambda m: None
passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name)


# Shaped like a ScrapeCreators /v1/twitter/profile response (fake account).
PROFILE = {
    "success": True, "credits_charged": 1, "credits_remaining": 999, "rest_id": "1000000000000000009",
    "is_blue_verified": True, "avatar": {"image_url": "https://pbs.twimg.com/profile_images/1/abc_normal.jpg"},
    "core": {"name": "Example Person", "screen_name": "Example_User"},
    "legacy": {"name": "Example Person", "screen_name": "Example_User", "followers_count": 1234, "friends_count": 56,
               "description": "Writes about AI | https://t.co/abc123",
               "entities": {"description": {"urls": [{"url": "https://t.co/abc123", "expanded_url": "https://example.com"}]}}},
}

# 1. Username from the Profile URL
cases = {
    "https://x.com/eptwts": "eptwts", "https://x.com/jakezward": "jakezward", "https://x.com/eptwts/": "eptwts",
    "https://x.com/eptwts?s=20": "eptwts", "https://twitter.com/EPtwts/status/123": "EPtwts",
    "http://www.x.com/eptwts": "eptwts", "https://mobile.twitter.com/eptwts": "eptwts", "x.com/eptwts": "eptwts",
    "  https://x.com/@eptwts  ": "eptwts", "@eptwts": "eptwts", "eptwts": "eptwts", "https://x.com/a_1": "a_1",
    "https://x.com/abcdefghijklmno": "abcdefghijklmno",  # 15 characters: the longest X allows
    "https://x.com/abcdefghijklmnop": None,  # 16
    "https://x.com/": None, "https://x.com/ep-twts": None, "https://instagram.com/eptwts": None,
    "https://notx.com/eptwts": None, "": None, None: None,
}
for text, want in cases.items():
    check(f"username from {text!r} = {want!r}", xp.handle_from_url(text) == want)


# 2. Mapping
f = xp.profile_fields(copy.deepcopy(PROFILE), "example_user")
check("mapping", f == {
    x.A_NAME: "Example Person", x.A_PLATFORM: "X", x.A_PLATFORM_ID: "1000000000000000009", x.A_HANDLE: "Example_User",
    xp.A_PROFILE_URL: "https://x.com/Example_User", xp.A_AVATAR: "https://pbs.twimg.com/profile_images/1/abc_400x400.jpg",
    xp.A_BIO: "Writes about AI | https://example.com", xp.A_VERIFIED: True, xp.A_FOLLOWERS: 1234, xp.A_FOLLOWING: 56})
old_shape = copy.deepcopy(PROFILE); del old_shape["core"]; del old_shape["avatar"]
old_shape["legacy"]["profile_image_url_https"] = "https://pbs.twimg.com/profile_images/1/abc_normal.jpg"
f = xp.profile_fields(old_shape, "example_user")
check("mapping: name, handle and avatar from legacy", f[x.A_NAME] == "Example Person" and f[x.A_HANDLE] == "Example_User"
      and f[xp.A_AVATAR].endswith("abc_400x400.jpg"))
f = xp.profile_fields({"rest_id": "5", "legacy": {}}, "example_user")
check("mapping: sparse profile -> typed handle, empty values left out", f[x.A_HANDLE] == "example_user"
      and f[x.A_NAME] == "example_user" and xp.A_BIO not in f and xp.A_AVATAR not in f and xp.A_FOLLOWERS not in f)


# 3. The whole setup against fakes
class FakeAT:
    def __init__(self, rows):
        self.rows, self.patches, self.lookups = {r["id"]: copy.deepcopy(r) for r in rows}, [], []

    def __call__(self, method, table, params=None, body=None):
        if method == "GET":
            return copy.deepcopy(self.rows[table.split("/")[1]])
        if table.endswith("/listRecords"):
            formula = body["filterByFormula"]
            self.lookups.append(formula)
            field, value = re.search(r"LOWER\(\{(\w+)\}\)='([^']*)'", formula).groups()
            me = re.search(r"RECORD_ID\(\)!='(\w+)'", formula).group(1)
            return {"records": [copy.deepcopy(r) for r in self.rows.values() if r["id"] != me
                                and r["fields"].get(x.A_PLATFORM) == "X" and str(r["fields"].get(field, "")).lower() == value]}
        for r in body["records"]:
            self.patches.append(copy.deepcopy(r))
            self.rows[r["id"]]["fields"].update(r["fields"])
        return body


class FakeSC:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response or PROFILE, error, []

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        if self.error:
            raise self.error
        return copy.deepcopy(self.response)


NEW = "recNEW0000000000A"
EXISTING = {"id": "recOLD0000000000A", "fields": {x.A_NAME: "EP", x.A_PLATFORM: "X", x.A_HANDLE: "eptwts",
                                                    x.A_PLATFORM_ID: "1360265495363145730", x.A_SCRAPE: "Active"}}


def setup(url, others=(EXISTING,), sc=None, extra=None):
    row = {"id": NEW, "fields": {xp.A_PROFILE_URL: url, **(extra or {})}}
    at, sc = FakeAT([row, *others]), sc or FakeSC()
    x.airtable, xp.scrapecreators = at, sc
    code = xp.setup(NEW)
    return code, at.rows[NEW]["fields"], at, sc


code, row, at, sc = setup("https://x.com/example_user?s=20")
check("ok: exit 0, one ScrapeCreators call with the username", code == 0 and sc.calls == [("/v1/twitter/profile", {"handle": "example_user"})])
check("ok: details filled, Active, never, error cleared", row[x.A_HANDLE] == "Example_User" and row[x.A_PLATFORM_ID] == "1000000000000000009"
      and row[x.A_SCRAPE] == "Active" and row[x.A_LAST_STATUS] == "never" and row[x.A_SCRAPE_ERROR] is None
      and row[xp.A_PROFILE_URL] == "https://x.com/Example_User" and row[x.A_PLATFORM] == "X")
check("ok: activated in the same single write as the details", len(at.patches) == 1 and x.A_SCRAPE in at.patches[0]["fields"])
check("ok: Last Scraped left empty (the new-account run reads 24h)", x.A_LAST_SCRAPED not in row)
check("ok: the scraper then treats it as a new account", x.last_end({"fields": row}, x.datetime.now(x.timezone.utc)) is None
      and x.valid_handle({"fields": row}) == "Example_User")

code, row, at, sc = setup("https://x.com/this_is_way_too_long")
check("too long: error written, not activated, no API call", code == 1 and sc.calls == [] and row[x.A_LAST_STATUS] == "error"
      and "1-15 letters" in row[x.A_SCRAPE_ERROR] and x.A_SCRAPE not in row)
code, row, at, sc = setup("https://www.youtube.com/@someone")
check("not an X link: error, no API call", code == 1 and sc.calls == [] and "not an X profile link" in row[x.A_SCRAPE_ERROR])
code, row, at, sc = setup("https://x.com/EPTWTS")
check("duplicate username (any case): flagged before the API call", code == 1 and sc.calls == []
      and "Already tracked" in row[x.A_SCRAPE_ERROR] and EXISTING["id"] in row[x.A_SCRAPE_ERROR] and x.A_SCRAPE not in row)
renamed = copy.deepcopy(PROFILE); renamed["rest_id"] = EXISTING["fields"][x.A_PLATFORM_ID]
code, row, at, sc = setup("https://x.com/ep_new_name", sc=FakeSC(renamed))
check("duplicate user ID (handle changed): flagged, not activated", code == 1 and len(sc.calls) == 1
      and "user ID 1360265495363145730" in row[x.A_SCRAPE_ERROR] and x.A_SCRAPE not in row)
code, row, at, sc = setup("https://x.com/example_user", sc=FakeSC(error=x.HttpError("HTTP 404 from /v1/twitter/profile: not found", 404)))
check("ScrapeCreators error: flagged with the reason", code == 1 and "HTTP 404" in row[x.A_SCRAPE_ERROR] and x.A_SCRAPE not in row)
code, row, at, sc = setup("https://x.com/example_user", sc=FakeSC({"success": True, "legacy": {}}))
check("no user ID in the response: flagged as not found", code == 1 and "not found" in row[x.A_SCRAPE_ERROR])
code, row, at, sc = setup("https://x.com/example_user", extra={x.A_PLATFORM_ID: "77", x.A_SCRAPE: "Paused"})
check("already has a Platform ID (even paused): left alone, no call", code == 0 and sc.calls == [] and at.patches == [])
code, row, at, sc = setup("https://www.youtube.com/@someone", extra={x.A_PLATFORM: "YouTube"})
check("other platform: left alone", code == 0 and sc.calls == [] and at.patches == [])
code, row, at, sc = setup("", extra={x.A_HANDLE: "@example_user"})
check("no Profile URL but a Handle: uses the Handle", code == 0 and sc.calls[0][1] == {"handle": "example_user"})
code, row, at, sc = setup("https://x.com/example_user", extra={x.A_LAST_STATUS: "error", x.A_SCRAPE_ERROR: "old"})
check("retry after an earlier failure: works, error cleared", code == 0 and row[x.A_SCRAPE_ERROR] is None and row[x.A_SCRAPE] == "Active")


# 4. Command line: only a record ID is accepted
for bad in ([], ["rec123"], ["recNEW0000000000A; rm -rf /"], ["a", "b"]):
    try:
        xp.main(bad)
        check(f"bad args {bad} rejected", False)
    except SystemExit as e:
        check(f"bad args {bad} rejected", "Usage" in str(e.code))

print(f"{passed}/{passed + failed} checks passed")
sys.exit(1 if failed else 0)
