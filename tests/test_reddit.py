"""Offline tests for scripts/reddit.py and scripts/reddit_profile.py. No network:
ScrapeCreators, Airtable and v.redd.it are fakes. Run: python3 tests/test_reddit.py"""
import contextlib, copy, io, os, re, sys
from datetime import datetime, timedelta, timezone

ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT + "/scripts")
import reddit as rd
import reddit_profile as rp

os.environ.update(AIRTABLE_ACCESS_TOKEN="fake", SCRAPE_CREATORS="fake")
rd.log = rp.log = lambda m: None
NOW = datetime(2026, 10, 1, 4, 7, tzinfo=timezone.utc)


class FixedDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


rd.datetime = rp.datetime = FixedDT
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
SUB_ID = "t5_7t8hvt"
passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name)


def post(pid, hours_ago=5, hint="text", url=None, **kw):
    """Shaped like a ScrapeCreators /v1/reddit/subreddit post."""
    created = NOW - timedelta(hours=hours_ago)
    p = {"id": pid, "name": f"t3_{pid}", "subreddit": "ClaudeAI", "subreddit_id": SUB_ID, "title": f"Post {pid}",
         "author": "someone", "selftext": "body", "link_flair_text": "Discussion", "post_hint": hint,
         "url": url or f"https://www.reddit.com/r/ClaudeAI/comments/{pid}/post/", "permalink": f"/r/ClaudeAI/comments/{pid}/post/",
         "created_utc": created.timestamp(), "created_at_iso": created.isoformat(), "score": 100, "upvote_ratio": 0.95,
         "num_comments": 12}
    p.update(kw)
    return p


def sub_row(rec="recSUB000000000AA", last=None, scrape="Active", handle="ClaudeAI"):
    f = {rd.A_NAME: "r/ClaudeAI", rd.A_PLATFORM: "Reddit", rd.A_HANDLE: handle, rd.A_SCRAPE: scrape, rd.A_PLATFORM_ID: SUB_ID}
    if last:
        f[rd.A_LAST_SCRAPED] = iso(last)
    return {"id": rec, "fields": f}


class FakeSC:
    """Posts per timeframe; details per exact (case-sensitive) name."""
    def __init__(self, day=(), week=(), fail=None, details=None):
        self.day, self.week, self.fail, self.details, self.calls = list(day), list(week), fail, details or {}, []

    def __call__(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if self.fail:
            raise self.fail
        if path == "/v1/reddit/subreddit/details":
            info = self.details.get(params["subreddit"])
            if info is None:
                raise rd.HttpError("/v1/reddit/subreddit/details returned success=false: Dang it doesn't look like the subreddit exists :(")
            return copy.deepcopy(info)
        posts = self.day if params["timeframe"] == "day" else self.week
        return {"success": True, "credits_charged": 1, "credits_remaining": 900, "posts": copy.deepcopy(posts), "after": "t3_x"}


class FakeAT:
    def __init__(self, accounts, posts=(), fail_post=False):
        self.accounts = {a["id"]: copy.deepcopy(a) for a in accounts}
        self.posts, self.writes, self.lookups, self.full_loads, self.fail_post = list(posts), [], [], 0, fail_post

    def __call__(self, method, table, params=None, body=None):
        if method == "GET" and table == rd.ACCOUNTS:
            return {"records": copy.deepcopy(list(self.accounts.values()))}
        if method == "GET" and table.startswith(rd.ACCOUNTS + "/"):
            return copy.deepcopy(self.accounts[table.split("/")[1]])
        if method == "GET" and table == rd.POSTS:
            self.full_loads += 1
            return {"records": copy.deepcopy(self.posts)}
        if table == f"{rd.POSTS}/listRecords":
            ids = re.findall(r"='([^']*)'", body["filterByFormula"])
            self.lookups.append(ids)
            return {"records": [copy.deepcopy(p) for p in self.posts if p["fields"].get(rd.P_POST_ID) in ids]}
        if table == f"{rd.ACCOUNTS}/listRecords":
            formula = body["filterByFormula"]
            field, value = re.search(r"LOWER\(\{(\w+)\}\)='([^']*)'", formula).groups()
            me = re.search(r"RECORD_ID\(\)!='(\w+)'", formula).group(1)
            return {"records": [copy.deepcopy(a) for a in self.accounts.values() if a["id"] != me
                                and a["fields"].get(rd.A_PLATFORM) == "Reddit" and str(a["fields"].get(field, "")).lower() == value]}
        self.writes.append((method, table, copy.deepcopy(body)))
        if table == rd.POSTS and method == "POST" and self.fail_post:
            raise rd.HttpError("HTTP 503 from /v0: down")
        out = []
        for i, r in enumerate(body["records"]):
            rec = {"id": r.get("id") or f"recP{len(self.posts) + i}", "fields": r["fields"]}
            out.append(rec)
            if table == rd.ACCOUNTS:
                self.accounts[r["id"]]["fields"].update(r["fields"])
            elif method == "POST":
                self.posts.append(copy.deepcopy(rec))
        return {"records": out}

    def posted(self):
        return [r for m, t, b in self.writes if t == rd.POSTS and m == "POST" for r in b["records"]]

    def patched(self):
        return [r for m, t, b in self.writes if t == rd.POSTS and m == "PATCH" for r in b["records"]]

    def account(self, rec="recSUB000000000AA"):
        return self.accounts[rec]["fields"]


PLAYLIST = "<MPD><BaseURL>CMAF_480.mp4</BaseURL><BaseURL>CMAF_720.mp4</BaseURL><BaseURL>CMAF_AUDIO_128.mp4</BaseURL></MPD>"
rd.fetch_text = lambda url, timeout=30: PLAYLIST


def run(sc, at, new_accounts=False):
    rd.scrapecreators, rd.airtable = sc, at
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        code = rd.main(new_accounts=new_accounts)
    return code, buf.getvalue()


# 1. Daily run: top of the day, one call, everything created, Last Scraped set
day = [post("a1", 2), post("a2", 20, hint="image", url="https://i.redd.it/abc.png"),
       post("a3", 23, hint="video", url="https://v.redd.it/zvcc"), post("a4", 10, hint="gallery", url="https://www.reddit.com/gallery/a4")]
sc, at = FakeSC(day=day, week=[post("w1", 100)]), FakeAT([sub_row(last=NOW - timedelta(days=1))])
code, out = run(sc, at)
check("daily: exit 0, one call, top of the day", code == 0 and sc.calls == [
    ("/v1/reddit/subreddit", {"subreddit": "ClaudeAI", "sort": "top", "timeframe": "day"})])
check("daily: all returned posts created as New", {r["fields"][rd.P_POST_ID] for r in at.posted()} == {"a1", "a2", "a3", "a4"}
      and all(r["fields"][rd.P_STATUS] == "New" for r in at.posted()))
m = {r["fields"][rd.P_POST_ID]: r["fields"].get(rd.P_MEDIA, []) for r in at.posted()}
check("media: image attached, video = best video + audio, gallery/text none", [f["filename"] for f in m["a2"]] == ["a2.png"]
      and [f["url"] for f in m["a3"]] == ["https://v.redd.it/zvcc/CMAF_720.mp4", "https://v.redd.it/zvcc/CMAF_AUDIO_128.mp4"]
      and m["a1"] == [] and m["a4"] == [])
c = next(r["fields"] for r in at.posted() if r["fields"][rd.P_POST_ID] == "a1")
check("mapping: thread URL from permalink, UTC date, ratio 0-1", c[rd.P_URL] == "https://www.reddit.com/r/ClaudeAI/comments/a1/post/"
      and c[rd.P_PUBLISHED] == iso(NOW - timedelta(hours=2)) and c[rd.P_UPVOTE_RATIO] == 0.95 and c[rd.P_ACCOUNT] == ["recSUB000000000AA"])
a = at.account()
check("success: Last Scraped = now, ok, error cleared", a[rd.A_LAST_SCRAPED] == iso(NOW) and a[rd.A_LAST_STATUS] == "ok"
      and rd.A_SCRAPE_ERROR in a and a[rd.A_SCRAPE_ERROR] is None)
check("table never loaded in full; only returned IDs looked up", at.full_loads == 0 and at.lookups == [["a1", "a2", "a3", "a4"]])
check("summary shows the timeframe", "top of the day" in out)

# 2. Seen again: updated, Status kept, media only filled when empty
stored = [{"id": "recA1", "fields": {rd.P_POST_ID: "a1", rd.P_STATUS: "Reviewed"}},
          {"id": "recA2", "fields": {rd.P_POST_ID: "a2", rd.P_MEDIA: [{"id": "att1"}]}},
          {"id": "recA3", "fields": {rd.P_POST_ID: "a3"}}]
sc, at = FakeSC(day=[post("a1", 2, score=900), day[1], day[2]]), FakeAT([sub_row(last=NOW - timedelta(days=1))], stored)
run(sc, at)
up = {r["id"]: r["fields"] for r in at.patched()}
check("seen again: updated, new score, no Status, no duplicate", set(up) == {"recA1", "recA2", "recA3"} and up["recA1"][rd.P_SCORE] == 900
      and all(rd.P_STATUS not in f for f in up.values()) and at.posted() == [])
check("seen again: filled media not re-sent, empty media filled", rd.P_MEDIA not in up["recA2"] and len(up["recA3"][rd.P_MEDIA]) == 2)

# 3. Timeframes: week for a new subreddit and after missed days, day otherwise
check("timeframe: new -> week", rd.timeframe_for(sub_row(), NOW) == "week")
check("timeframe: 1 day ago -> day", rd.timeframe_for(sub_row(last=NOW - timedelta(hours=24, minutes=40)), NOW) == "day")
check("timeframe: 36h ago -> day", rd.timeframe_for(sub_row(last=NOW - timedelta(hours=36)), NOW) == "day")
check("timeframe: 2 days ago (a missed day) -> week", rd.timeframe_for(sub_row(last=NOW - timedelta(days=2)), NOW) == "week")
sc, at = FakeSC(day=day, week=[post("w1", 100), post("w2", 150)]), FakeAT([sub_row()])
code, out = run(sc, at)
check("new subreddit on the daily run: top of the week", sc.calls[0][1]["timeframe"] == "week"
      and {r["fields"][rd.P_POST_ID] for r in at.posted()} == {"w1", "w2"} and "top of the week" in out)

# 4. --new-accounts: only subreddits with no Last Scraped, top of the week, once
rows = [sub_row(), sub_row("recOLD000000000AA", last=NOW - timedelta(days=1), handle="LocalLLaMA"),
        sub_row("recPAU000000000AA", scrape="Paused", handle="paused"),
        {"id": "recX00000000000AA", "fields": {rd.A_PLATFORM: "X", rd.A_SCRAPE: "Active", rd.A_HANDLE: "eptwts"}}]
sc, at = FakeSC(day=day, week=[post("w1", 100)]), FakeAT(rows)
code, out = run(sc, at, new_accounts=True)
touched = {r["id"] for m_, t, b in at.writes if t == rd.ACCOUNTS for r in b["records"]}
check("new-accounts: only the new subreddit, top of the week", code == 0 and sc.calls == [
    ("/v1/reddit/subreddit", {"subreddit": "ClaudeAI", "sort": "top", "timeframe": "week"})] and touched == {"recSUB000000000AA"})
sc = FakeSC(day=day, week=[post("w1", 100)])
code, out = run(sc, at, new_accounts=True)
check("new-accounts: once read, never again (week runs once)", code == 0 and sc.calls == [])
sc = FakeSC(day=day, week=[post("w1", 100)])
run(sc, at)
check("after that, the daily run reads the top of the day for both", [c[1]["timeframe"] for c in sc.calls] == ["day", "day"])

# 5. Failures: Last Scraped kept, so the next run catches up with the week
last = NOW - timedelta(days=1)
sc, at = FakeSC(fail=rd.HttpError("HTTP 500 from /v1/reddit/subreddit: oops")), FakeAT([sub_row(last=last)])
code, out = run(sc, at)
a = at.account()
check("ScrapeCreators error: error, reason, Last Scraped kept, exit 1", code == 1 and a[rd.A_LAST_STATUS] == "error"
      and "HTTP 500" in a[rd.A_SCRAPE_ERROR] and a[rd.A_LAST_SCRAPED] == iso(last))
check("...so the next day it reads the week", rd.timeframe_for(at.accounts["recSUB000000000AA"], NOW + timedelta(days=1)) == "week")
sc, at = FakeSC(day=[post("n", 1)]), FakeAT([sub_row(last=last)], fail_post=True)
code, out = run(sc, at)
check("Airtable write fails: error, Last Scraped kept, credit reported", code == 1 and at.account()[rd.A_LAST_SCRAPED] == iso(last)
      and "credits used 1" in out)
rd.fetch_text = lambda url, timeout=30: (_ for _ in ()).throw(rd.HttpError("HTTP 403 from v.redd.it"))
sc, at = FakeSC(day=[day[2]]), FakeAT([sub_row(last=last)])
code, out = run(sc, at)
check("video playlist blocked: post saved without media, not an error", code == 0 and len(at.posted()) == 1
      and rd.P_MEDIA not in at.posted()[0]["fields"] and "media skipped 1" in out)
rd.fetch_text = lambda url, timeout=30: PLAYLIST
sc, at = FakeSC(day=day), FakeAT([sub_row(handle="")])
code, out = run(sc, at)
check("no Handle: error, no call", code == 1 and sc.calls == [])

# 6. reddit_profile: subreddit from the Profile URL
cases = {
    "https://www.reddit.com/r/ClaudeAI/": "ClaudeAI", "https://reddit.com/r/ClaudeAI": "ClaudeAI",
    "https://old.reddit.com/r/ClaudeAI/top/?t=week": "ClaudeAI", "https://www.reddit.com/r/ClaudeAI/comments/1wu4rv6/x/": "ClaudeAI",
    "https://m.reddit.com/r/ClaudeAI": "ClaudeAI", "reddit.com/r/ClaudeAI": "ClaudeAI", "r/ClaudeAI": "ClaudeAI",
    "/r/ClaudeAI/": "ClaudeAI", "ClaudeAI": "ClaudeAI", "https://www.reddit.com/r/de": "de",
    "https://www.reddit.com/user/someone": None, "https://www.reddit.com/r/": None,
    "https://www.reddit.com/r/this_name_is_way_too_long": None, "https://x.com/eptwts": None, "": None, None: None,
}
for text, want in cases.items():
    check(f"subreddit from {text!r} = {want!r}", rp.subreddit_from_url(text) == want)

DETAILS = {"success": True, "credits_charged": 1, "credits_remaining": 900, "subreddit_id": SUB_ID, "display_name": "ClaudeAI",
           "icon_img": "https://styles.redditmedia.com/t5_7t8hvt/styles/communityIcon_x.png?width=128&amp;s=abc",
           "description": "A Claude discussion subreddit.", "subscribers": None}
f = rp.subreddit_fields(copy.deepcopy(DETAILS))
check("profile mapping", f == {
    rd.A_NAME: "r/ClaudeAI", rd.A_PLATFORM: "Reddit", rd.A_PLATFORM_ID: SUB_ID, rd.A_HANDLE: "ClaudeAI",
    rp.A_PROFILE_URL: "https://www.reddit.com/r/ClaudeAI/", rp.A_AVATAR: "https://styles.redditmedia.com/t5_7t8hvt/styles/communityIcon_x.png?width=128&s=abc",
    rp.A_BIO: "A Claude discussion subreddit."})

NEW = "recNEW0000000000A"
OTHER = {"id": "recOTHER00000000A", "fields": {rd.A_NAME: "r/ClaudeAI", rd.A_PLATFORM: "Reddit", rd.A_HANDLE: "ClaudeAI",
                                                 rd.A_PLATFORM_ID: SUB_ID, rd.A_SCRAPE: "Active"}}


def setup(url, others=(), sc=None, extra=None):
    row = {"id": NEW, "fields": {rp.A_PROFILE_URL: url, rd.A_PLATFORM: "Reddit", **(extra or {})}}
    at, sc = FakeAT([row, *others]), sc or FakeSC(details={"ClaudeAI": DETAILS}, week=[post("w1", 100)])
    rd.airtable, rd.scrapecreators = at, sc
    code = rp.setup(NEW)
    return code, at.accounts[NEW]["fields"], at, sc


code, row, at, sc = setup("https://www.reddit.com/r/ClaudeAI/")
check("setup ok: one details call", code == 0 and sc.calls == [("/v1/reddit/subreddit/details", {"subreddit": "ClaudeAI"})])
check("setup ok: filled, Active, never, single write, no Last Scraped", row[rd.A_PLATFORM_ID] == SUB_ID and row[rd.A_SCRAPE] == "Active"
      and row[rd.A_LAST_STATUS] == "never" and row[rd.A_SCRAPE_ERROR] is None and len(at.writes) == 1 and rd.A_LAST_SCRAPED not in row)
check("setup ok: the scraper then reads its top of the week", rd.timeframe_for({"fields": row}, NOW) == "week")
code, row, at, sc = setup("https://www.reddit.com/r/claudeai/")
check("wrong case: miss (free), name from the posts, asked again", code == 0 and [c[0] for c in sc.calls] == [
    "/v1/reddit/subreddit/details", "/v1/reddit/subreddit", "/v1/reddit/subreddit/details"]
      and sc.calls[2][1] == {"subreddit": "ClaudeAI"} and row[rd.A_HANDLE] == "ClaudeAI")
code, row, at, sc = setup("https://www.reddit.com/r/NoSuchSub/", sc=FakeSC(details={}, week=[]))
check("not found: flagged, not activated", code == 1 and "not found" in row[rd.A_SCRAPE_ERROR] and rd.A_SCRAPE not in row)
code, row, at, sc = setup("https://www.reddit.com/user/someone")
check("not a subreddit link: error, no call", code == 1 and sc.calls == [] and "not a subreddit link" in row[rd.A_SCRAPE_ERROR])
code, row, at, sc = setup("https://www.reddit.com/r/claudeAI", others=[OTHER])
check("duplicate name (any case): flagged before the call", code == 1 and sc.calls == [] and "Already tracked" in row[rd.A_SCRAPE_ERROR])
renamed = dict(OTHER, fields={**OTHER["fields"], rd.A_HANDLE: "OldName"})
code, row, at, sc = setup("https://www.reddit.com/r/ClaudeAI", others=[renamed])
check("duplicate subreddit ID: flagged after the call", code == 1 and SUB_ID in row[rd.A_SCRAPE_ERROR] and rd.A_SCRAPE not in row)
code, row, at, sc = setup("https://www.reddit.com/r/ClaudeAI", sc=FakeSC(fail=rd.HttpError("HTTP 500 from /v1/reddit/subreddit/details: oops")))
check("ScrapeCreators error: flagged with the reason", code == 1 and "HTTP 500" in row[rd.A_SCRAPE_ERROR])
code, row, at, sc = setup("https://www.reddit.com/r/ClaudeAI", extra={rd.A_PLATFORM_ID: SUB_ID, rd.A_SCRAPE: "Paused"})
check("already has a subreddit ID (even paused): left alone", code == 0 and sc.calls == [] and at.writes == [])
code, row, at, sc = setup("https://x.com/eptwts", extra={rd.A_PLATFORM: "X"})
check("other platform: left alone", code == 0 and sc.calls == [] and at.writes == [])

for bad in ([], ["rec1"], ["recNEW0000000000A; rm -rf /"]):
    try:
        rp.main(bad)
        check(f"bad args {bad} rejected", False)
    except SystemExit as e:
        check(f"bad args {bad} rejected", "Usage" in str(e.code))

print(f"{passed}/{passed + failed} checks passed")
sys.exit(1 if failed else 0)
