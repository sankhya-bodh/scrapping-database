"""Offline tests for scripts/instagram.py. No network: ScrapeCreators and Airtable are fakes.
Run: python3 tests/test_instagram.py"""
import contextlib, copy, io, os, re, sys
from datetime import datetime, timedelta, timezone

ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT + "/scripts")
import instagram as ig

os.environ.update(AIRTABLE_ACCESS_TOKEN="fake", SCRAPE_CREATORS="fake")
ig.log = lambda m: None
NOW = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)


class FixedDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


ig.datetime = FixedDT
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
ACC = "recIG0000000000AA"
passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name)


def versions(name, sizes):
    return [{"url": f"https://cdn.instagram.com/{name}_{w}.{'mp4' if 'v' in name else 'jpg'}?oe=1", "width": w, "height": w}
            for w in sizes]


def item(code, kind=2, hours_ago=5, caption="First line\nSecond line", **kw):
    """Shaped like a ScrapeCreators /v2/instagram/user/posts item."""
    created = NOW - timedelta(hours=hours_ago)
    i = {"code": code, "media_type": kind, "url": f"https://www.instagram.com/p/{code}/",
         "caption": {"text": caption} if caption is not None else None,
         "display_uri": f"https://cdn.instagram.com/{code}_thumb.jpg?oe=1", "created_at": created.isoformat(),
         "taken_at": int(created.timestamp()), "ig_play_count": 12345, "like_count": 678, "comment_count": 9,
         "image_versions2": {"candidates": versions(f"{code}_img", [320, 1080, 640])}}
    if kind == 2:
        i.update(video_versions=versions(f"{code}_v", [480, 720]), video_duration=31.6)
    if kind == 8:
        i["carousel_media"] = [{"image_versions2": {"candidates": versions(f"{code}_s1img", [640, 1080])}},
                               {"video_versions": versions(f"{code}_s2v", [720, 480])},
                               {"image_versions2": {"candidates": []}}]
    i.update(kw)
    return i


def account(rec=ACC, handle="mavgpt", scrape="Active", platform="Instagram"):
    return {"id": rec, "fields": {ig.A_NAME: handle, ig.A_PLATFORM: platform, ig.A_HANDLE: handle, ig.A_SCRAPE: scrape}}


class FakeSC:
    def __init__(self, items=(), fail=None):
        self.items, self.fail, self.calls = list(items), fail, []

    def __call__(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if self.fail:
            raise self.fail
        return {"success": True, "credits_charged": 1, "credits_remaining": 800, "items": copy.deepcopy(self.items)}


class FakeAT:
    def __init__(self, accounts, posts=(), fail_post=False):
        self.accounts = {a["id"]: copy.deepcopy(a) for a in accounts}
        self.posts, self.writes, self.lookups, self.platforms = list(posts), [], [], set()
        self.full_loads, self.fail_post = 0, fail_post

    def __call__(self, method, table, params=None, body=None):
        if method == "GET" and table == ig.ACCOUNTS:
            return {"records": copy.deepcopy(list(self.accounts.values()))}
        if method == "GET" and table == ig.POSTS:
            self.full_loads += 1
            return {"records": copy.deepcopy(self.posts)}
        if table == f"{ig.POSTS}/listRecords":
            m = re.fullmatch(r"AND\(\{%s\}='(\w+)',OR\((.*)\)\)" % ig.P_PLATFORM, body["filterByFormula"])
            assert m, body["filterByFormula"]
            ids = [i.replace("\\'", "'") for i in re.findall(r"\{%s\}='((?:[^'\\]|\\.)*)'" % ig.P_CONTENT_ID, m.group(2))]
            self.lookups.append(ids)
            self.platforms.add(m.group(1))
            return {"records": [copy.deepcopy(p) for p in self.posts if p["fields"].get(ig.P_CONTENT_ID) in ids
                                and p["fields"].get(ig.P_PLATFORM) == m.group(1)]}
        self.writes.append((method, table, copy.deepcopy(body)))
        if table == ig.POSTS and method == "POST" and self.fail_post:
            raise ig.HttpError("HTTP 503 from /v0: down")
        out = []
        for i, r in enumerate(body["records"]):
            rec = {"id": r.get("id") or f"recI{len(self.posts) + i}", "fields": r["fields"]}
            out.append(rec)
            if table == ig.ACCOUNTS:
                self.accounts[r["id"]]["fields"].update(r["fields"])
            elif method == "POST":
                self.posts.append(copy.deepcopy(rec))
        return {"records": out}

    def posted(self):
        return [r for m, t, b in self.writes if t == ig.POSTS and m == "POST" for r in b["records"]]

    def patched(self):
        return [r for m, t, b in self.writes if t == ig.POSTS and m == "PATCH" for r in b["records"]]

    def account(self, rec=ACC):
        return self.accounts[rec]["fields"]


def run(sc, at):
    ig.scrapecreators, ig.airtable = sc, at
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        code = ig.main()
    return code, buf.getvalue()


# 1. A reel, a carousel and a photo: created with every field
sc, at = FakeSC([item("REEL1"), item("CAR1", kind=8), item("PHO1", kind=1)]), FakeAT([account()])
code, out = run(sc, at)
got = {r["fields"][ig.P_CONTENT_ID]: r["fields"] for r in at.posted()}
check("exit 0, one call by handle", code == 0 and sc.calls == [("/v2/instagram/user/posts", {"handle": "mavgpt"})])
check("all 3 created as New", set(got) == {"REEL1", "CAR1", "PHO1"} and all(f[ig.P_STATUS] == "New" for f in got.values()))
check("writes go to Scraped Content and Accounts only", {t for _, t, _ in at.writes} == {ig.POSTS, ig.ACCOUNTS}
      and ig.POSTS == "tblViAU74E50jTA5H")
check("table never loaded in full; only returned shortcodes looked up, among Instagram rows", at.full_loads == 0
      and at.lookups == [["REEL1", "CAR1", "PHO1"]] and at.platforms == {"Instagram"})
r = got["REEL1"]
check("reel: Platform, Type, Title = caption's first line, Text = caption", r[ig.P_PLATFORM] == "Instagram" and r[ig.P_TYPE] == "Reel"
      and r[ig.P_TITLE] == "First line" and r[ig.P_TEXT] == "First line\nSecond line")
check("reel: Views = plays, Likes, Comments, Duration rounded", (r[ig.P_VIEWS], r[ig.P_LIKES], r[ig.P_COMMENTS], r[ig.P_DURATION])
      == (12345, 678, 9, 32))
check("reel: link, account, UTC date, Last Scraped", r[ig.P_URL] == "https://www.instagram.com/p/REEL1/" and r[ig.P_ACCOUNT] == [ACC]
      and r[ig.P_PUBLISHED] == iso(NOW - timedelta(hours=5)) and r[ig.P_LAST_SCRAPED] == iso(NOW))
check("reel: Media = the biggest video, Thumbnail = display_uri", r[ig.P_MEDIA] == [
    {"url": "https://cdn.instagram.com/REEL1_v_720.mp4?oe=1", "filename": "REEL1.mp4"}]
      and r[ig.P_THUMBNAIL] == [{"url": "https://cdn.instagram.com/REEL1_thumb.jpg?oe=1"}])
c = got["CAR1"]
check("carousel: every usable slide in order, biggest version, numbered", [f["filename"] for f in c[ig.P_MEDIA]] == ["CAR1_1.jpg", "CAR1_2.mp4"]
      and c[ig.P_MEDIA][0]["url"].endswith("_1080.jpg?oe=1") and c[ig.P_MEDIA][1]["url"].endswith("_720.mp4?oe=1"))
check("carousel/photo: Type, no Duration", c[ig.P_TYPE] == "Carousel" and got["PHO1"][ig.P_TYPE] == "Photo"
      and ig.P_DURATION not in c and ig.P_DURATION not in got["PHO1"])
check("photo: the biggest image", got["PHO1"][ig.P_MEDIA] == [{"url": "https://cdn.instagram.com/PHO1_img_1080.jpg?oe=1", "filename": "PHO1.jpg"}])
a = at.account()
check("account: Last Scraped, ok, error cleared", a[ig.A_LAST_SCRAPED] == iso(NOW) and a[ig.A_LAST_STATUS] == "ok"
      and ig.A_SCRAPE_ERROR in a and a[ig.A_SCRAPE_ERROR] is None)
check("summary: attachments and credits", "attachments added 7" in out and "credits used 1" in out and "credits remaining: 800" in out)

# 2. Title edge cases
check("title: blank lines skipped, cut to 100", ig.post_fields(item("T", caption="\n \n" + "b" * 300), ACC, iso(NOW))[ig.P_TITLE] == "b" * 100)
for cap in (None, "", "  \n  "):
    check(f"title: none for caption {cap!r}", ig.P_TITLE not in ig.post_fields(item("T", caption=cap), ACC, iso(NOW)))
f = ig.post_fields(item("T", like_and_view_counts_disabled=True), ACC, iso(NOW))
check("hidden likes: Likes left out", ig.P_LIKES not in f and f[ig.P_VIEWS] == 12345)
f = ig.post_fields(item("T", created_at=None), ACC, iso(NOW))
check("no created_at: taken_at used", f[ig.P_PUBLISHED] == iso(NOW - timedelta(hours=5)))
f = ig.post_fields(item("T", kind=99), ACC, iso(NOW))
check("unknown media_type: no Type, no Media, still saved", ig.P_TYPE not in f and ig.P_MEDIA not in f and f[ig.P_CONTENT_ID] == "T")

# 3. Seen again: updated, Status kept, attachments only where empty
stored = [{"id": "recR", "fields": {ig.P_CONTENT_ID: "REEL1", ig.P_PLATFORM: "Instagram", ig.P_STATUS: "Reviewed",
                                     ig.P_THUMBNAIL: [{"id": "att1"}], ig.P_MEDIA: [{"id": "att2"}]}},
          {"id": "recP", "fields": {ig.P_CONTENT_ID: "PHO1", ig.P_PLATFORM: "Instagram"}}]
sc, at = FakeSC([item("REEL1", ig_play_count=99999), item("PHO1", kind=1)]), FakeAT([account()], stored)
run(sc, at)
up = {r["id"]: r["fields"] for r in at.patched()}
check("seen again: updated, new plays, no duplicate", set(up) == {"recR", "recP"} and up["recR"][ig.P_VIEWS] == 99999 and at.posted() == [])
check("seen again: Status never sent", all(ig.P_STATUS not in f for f in up.values()))
check("seen again: filled attachments not re-sent, empty ones filled", ig.P_THUMBNAIL not in up["recR"] and ig.P_MEDIA not in up["recR"]
      and len(up["recP"][ig.P_THUMBNAIL]) == 1 and len(up["recP"][ig.P_MEDIA]) == 1)
same = [{"id": "recYT", "fields": {ig.P_CONTENT_ID: "REEL1", ig.P_PLATFORM: "YouTube", ig.P_STATUS: "Reviewed"}}]
sc, at = FakeSC([item("REEL1")]), FakeAT([account()], same)
run(sc, at)
check("another platform's row with the same ID is left alone; the post is created", at.patched() == [] and len(at.posted()) == 1)
sc, at = FakeSC([item("REEL1")]), FakeAT([account()])
run(sc, at)
run(FakeSC([item("REEL1")]), at)
check("running twice: one row, updated the second time", len([p for p in at.posts if p["fields"][ig.P_CONTENT_ID] == "REEL1"]) == 1
      and len(at.patched()) == 1 and ig.P_MEDIA not in at.patched()[0]["fields"])

# 4. Lookups chunked; malformed items skipped
many = [item(f"C{n:03d}", kind=1) for n in range(120)] + [item("C000", kind=1)]
sc, at = FakeSC(many), FakeAT([account()])
run(sc, at)
check("lookups chunked by 50, duplicates in the response stored once", [len(l) for l in at.lookups] == [50, 50, 20]
      and len(at.posted()) == 120)
sc, at = FakeSC([None, "x", {"media_type": 1}, item("OK1", kind=1), item("Q'1", kind=1)]), FakeAT([account()])
code, out = run(sc, at)
check("malformed items skipped, good ones saved, quote in a code escaped", code == 0
      and {r["fields"][ig.P_CONTENT_ID] for r in at.posted()} == {"OK1", "Q'1"} and "Q'1" in at.lookups[0])

# 5. Failures, filters
sc, at = FakeSC(fail=ig.HttpError("HTTP 500 from /v2/instagram/user/posts: oops")), FakeAT([account()])
code, out = run(sc, at)
check("ScrapeCreators error: error, reason, exit 1", code == 1 and at.account()[ig.A_LAST_STATUS] == "error"
      and "HTTP 500" in at.account()[ig.A_SCRAPE_ERROR])
sc, at = FakeSC([item("N1")]), FakeAT([account()], fail_post=True)
code, out = run(sc, at)
check("Airtable write fails: error, credit still reported", code == 1 and "credits used 1" in out)
sc, at = FakeSC([item("N1")]), FakeAT([account(handle="")])
code, out = run(sc, at)
check("no Handle: error, no call", code == 1 and sc.calls == [])
rows = [account(), account("recPAU0000000000A", "paused", scrape="Paused"), account("recX00000000000AA", "eptwts", platform="X")]
sc, at = FakeSC([item("N1")]), FakeAT(rows)
run(sc, at)
touched = {r["id"] for m, t, b in at.writes if t == ig.ACCOUNTS for r in b["records"]}
check("only active Instagram accounts read", len(sc.calls) == 1 and touched == {ACC})

print(f"{passed}/{passed + failed} checks passed")
sys.exit(1 if failed else 0)
