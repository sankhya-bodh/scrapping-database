"""Offline tests for scripts/youtube.py and scripts/youtube_profile.py. No network:
ScrapeCreators and Airtable are fakes. Run: python3 tests/test_youtube.py"""
import contextlib, copy, io, os, re, sys, types
from datetime import datetime, timedelta, timezone

ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT + "/scripts")
import youtube as yt
import youtube_profile as yp

os.environ.update(AIRTABLE_ACCESS_TOKEN="fake", SCRAPE_CREATORS="fake")
yt.log = yp.log = lambda m: None
NOW = datetime(2026, 10, 1, 3, 37, tzinfo=timezone.utc)


class FixedDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


yt.datetime = yp.datetime = FixedDT
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
CH = "UCbo-KbSjJDG6JWQ_MTZ_rNA"
passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name)


def video(vid, days_ago, exact=True, **kw):
    """Shaped like a channel-videos item (includeExtras=true). publishDate has an offset;
    publishedTime is the rough estimate."""
    pub = NOW - timedelta(days=days_ago)
    v = {"type": "video", "id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": f"Video {vid}",
         "thumbnail": f"https://i.ytimg.com/vi/{vid}/hq720.jpg", "description": "Full description",
         "publishDate": (pub.astimezone(timezone(timedelta(hours=-7)))).isoformat() if exact else None,
         # the estimate: right day for recent videos, drifting by weeks for older ones
         "publishedTime": iso(pub.replace(hour=12) if days_ago < 7 else pub + timedelta(days=days_ago / 3)),
         "lengthSeconds": 1200, "viewCountInt": 1000, "likeCountInt": 50, "commentCountInt": 7}
    v.update(kw)
    return v


def channel_row(rec="recCH0000000000AA", last=None, scrape="Active", pid=CH):
    f = {yt.A_NAME: "Nick Saraev", yt.A_PLATFORM: "YouTube", yt.A_PLATFORM_ID: pid, yt.A_SCRAPE: scrape}
    if last:
        f[yt.A_LAST_SCRAPED] = iso(last)
    return {"id": rec, "fields": f}


class FakeSC:
    def __init__(self, videos=(), fail=None, channel=None):
        self.videos, self.fail, self.channel, self.calls = list(videos), fail, channel, []

    def __call__(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if self.fail:
            raise self.fail
        if path == "/v1/youtube/channel":
            return copy.deepcopy(self.channel)
        return {"success": True, "credits_charged": 1, "credits_remaining": 900,
                "videos": copy.deepcopy(self.videos), "shorts": [{"id": "short1", "type": "shorts"}]}


class FakeAT:
    def __init__(self, accounts, videos=(), fail_post=False):
        self.accounts = {a["id"]: copy.deepcopy(a) for a in accounts}
        self.videos, self.writes, self.lookups, self.full_loads, self.fail_post = list(videos), [], [], 0, fail_post
        self.platforms = set()

    def __call__(self, method, table, params=None, body=None):
        if method == "GET" and table == yt.ACCOUNTS:
            return {"records": copy.deepcopy(list(self.accounts.values()))}
        if method == "GET" and table.startswith(yt.ACCOUNTS + "/"):
            return copy.deepcopy(self.accounts[table.split("/")[1]])
        if method == "GET" and table == yt.VIDEOS:
            self.full_loads += 1
            return {"records": copy.deepcopy(self.videos)}
        if table == f"{yt.VIDEOS}/listRecords":
            m = re.fullmatch(r"AND\(\{%s\}='(\w+)',OR\((.*)\)\)" % yt.V_PLATFORM, body["filterByFormula"])
            assert m, body["filterByFormula"]
            ids = re.findall(r"\{%s\}='([^']*)'" % yt.V_CONTENT_ID, m.group(2))
            self.lookups.append(ids)
            self.platforms.add(m.group(1))
            return {"records": [copy.deepcopy(v) for v in self.videos if v["fields"].get(yt.V_CONTENT_ID) in ids
                                and v["fields"].get(yt.V_PLATFORM) == m.group(1)]}
        if table == f"{yt.ACCOUNTS}/listRecords":  # duplicate check in youtube_profile
            formula = body["filterByFormula"]
            field, value = re.search(r"LOWER\(\{(\w+)\}\)='([^']*)'", formula).groups()
            me = re.search(r"RECORD_ID\(\)!='(\w+)'", formula).group(1)
            return {"records": [copy.deepcopy(a) for a in self.accounts.values() if a["id"] != me
                                and a["fields"].get(yt.A_PLATFORM) == "YouTube"
                                and str(a["fields"].get(field, "")).lower() == value]}
        self.writes.append((method, table, copy.deepcopy(body)))
        if table == yt.VIDEOS and method == "POST" and self.fail_post:
            raise yt.HttpError("HTTP 503 from /v0: down")
        out = []
        for i, r in enumerate(body["records"]):
            rec = {"id": r.get("id") or f"recV{len(self.videos) + i}", "fields": r["fields"]}
            out.append(rec)
            if table == yt.ACCOUNTS:
                self.accounts[r["id"]]["fields"].update(r["fields"])
            elif method == "POST":
                self.videos.append(copy.deepcopy(rec))
        return {"records": out}

    def posted(self):
        return [r for m, t, b in self.writes if t == yt.VIDEOS and m == "POST" for r in b["records"]]

    def patched(self):
        return [r for m, t, b in self.writes if t == yt.VIDEOS and m == "PATCH" for r in b["records"]]

    def account(self, rec="recCH0000000000AA"):
        return self.accounts[rec]["fields"]


def run(sc, at, new_accounts=False):
    yt.scrapecreators, yt.airtable = sc, at
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        code = yt.main(new_accounts=new_accounts)
    return code, buf.getvalue()


# 1. New channel: all 30 newest (here 4) are stored; one call; Shorts ignored
vids = [video("new1", 0.5), video("new2", 2.9), video("old1", 3.1), video("old2", 40)]
sc, at = FakeSC(vids), FakeAT([channel_row()])
code, out = run(sc, at)
check("new channel: exit 0, one call, sort=latest + includeExtras", code == 0 and sc.calls == [
    ("/v1/youtube/channel-videos", {"channelId": CH, "sort": "latest", "includeExtras": "true"})])
check("new channel: every returned video created, old ones too", {r["fields"][yt.V_CONTENT_ID] for r in at.posted()}
      == {"new1", "new2", "old1", "old2"} and "created 4" in out)
check("Shorts list ignored", "short1" not in str(at.writes))
check("created: Status New, exact UTC date", all(r["fields"][yt.V_STATUS] == "New" for r in at.posted())
      and next(r for r in at.posted() if r["fields"][yt.V_CONTENT_ID] == "new1")["fields"][yt.V_PUBLISHED] == iso(NOW - timedelta(days=0.5)))
a = at.account()
check("success: Last Scraped = now, ok, error cleared", a[yt.A_LAST_SCRAPED] == iso(NOW) and a[yt.A_LAST_STATUS] == "ok"
      and yt.A_SCRAPE_ERROR in a and a[yt.A_SCRAPE_ERROR] is None)
check("table never loaded in full; only returned IDs looked up", at.full_loads == 0 and at.lookups == [["new1", "new2", "old1", "old2"]])

# 2. Due every 3 days (checked daily)
check("due: no Last Scraped", yt.is_due(channel_row(), NOW))
check("due: 3 days ago", yt.is_due(channel_row(last=NOW - timedelta(days=3)), NOW))
check("due: 2 days 19h ago (daily run a bit early)", yt.is_due(channel_row(last=NOW - timedelta(days=2, hours=19)), NOW))
check("not due: 2 days 12h ago", not yt.is_due(channel_row(last=NOW - timedelta(days=2, hours=12)), NOW))
check("not due: 1 day ago", not yt.is_due(channel_row(last=NOW - timedelta(days=1)), NOW))
sc, at = FakeSC(vids), FakeAT([channel_row(last=NOW - timedelta(days=1))])
code, out = run(sc, at)
check("not due: no call, no writes, exit 0", code == 0 and sc.calls == [] and at.writes == [])

# 3. A scheduled run: every returned video not yet stored is created, every stored one refreshed
last = NOW - timedelta(days=3)
stored = [{"id": "recOLD1", "fields": {yt.V_CONTENT_ID: "old1", yt.V_PLATFORM: "YouTube", yt.V_STATUS: "Reviewed"}},
          {"id": "recOLD2", "fields": {yt.V_CONTENT_ID: "old2", yt.V_PLATFORM: "YouTube"}}]
vids = [video("v_new", 1), video("v_late", 5.5), video("old1", 3.1, viewCountInt=5000), video("old2", 40, viewCountInt=9),
        video("v_before", 7)]
sc, at = FakeSC(vids), FakeAT([channel_row(last=last)], stored)
code, out = run(sc, at)
check("scheduled: every video not yet stored created (late-public and older ones too)",
      {r["fields"][yt.V_CONTENT_ID] for r in at.posted()} == {"v_new", "v_late", "v_before"})
up = {r["id"]: r["fields"] for r in at.patched()}
check("scheduled: every stored video refreshed, any age", set(up) == {"recOLD1", "recOLD2"} and up["recOLD1"][yt.V_VIEWS] == 5000
      and up["recOLD2"][yt.V_VIEWS] == 9)
check("scheduled: Status never sent on update (Reviewed kept)", all(yt.V_STATUS not in f for f in up.values()))
check("scheduled: Last Scraped = now", at.account()[yt.A_LAST_SCRAPED] == iso(NOW))
code, out = run(FakeSC(vids), at)
check("running again: no duplicates", len([v for v in at.videos if v["fields"].get(yt.V_CONTENT_ID) == "v_new"]) == 1)

# 3b. Thumbnail saved as an attachment, once
sc, at = FakeSC([video("t1", 1, thumbnail="https://i.ytimg.com/vi/t1/hq720.jpg?sqp=-oaymwEnCNAF&rs=AOn4CL")]), FakeAT([channel_row()])
code, out = run(sc, at)
c = at.posted()[0]["fields"]  # the query (which makes YouTube serve AVIF) is dropped: JPEG
check("thumbnail: attached on create, named by video ID", c.get(yt.V_THUMBNAIL) == [
    {"url": "https://i.ytimg.com/vi/t1/hq720.jpg", "filename": "t1.jpg"}] and "thumbnails saved 1" in out)
stored = [{"id": "recT1", "fields": {yt.V_CONTENT_ID: "t1", yt.V_PLATFORM: "YouTube"}},
          {"id": "recT2", "fields": {yt.V_CONTENT_ID: "t2", yt.V_PLATFORM: "YouTube", yt.V_THUMBNAIL: [{"id": "attX", "url": "https://dl.airtable.com/x.jpg"}]}}]
sc, at = FakeSC([video("t1", 1), video("t2", 1)]), FakeAT([channel_row(last=NOW - timedelta(days=3))], stored)
run(sc, at)
up = {r["id"]: r["fields"] for r in at.patched()}
check("thumbnail: empty field filled on update (backfill)", len(up["recT1"].get(yt.V_THUMBNAIL, [])) == 1)
check("thumbnail: filled field never re-sent (no duplicates)", yt.V_THUMBNAIL not in up["recT2"])
custom = "https://i.ytimg.com/vi/t4/hq720_custom_2.jpg?sqp=CPi6_dUG-oaymwEnCNAF&rs=AOn4CLBm"  # as seen live 2026-10-02
check("thumbnail: A/B-test custom thumbnail -> the video's hq720.jpg (the custom one 404s without its query)",
      yt.thumbnail_files(video("t4", 1, thumbnail=custom)) == [{"url": "https://i.ytimg.com/vi/t4/hq720.jpg", "filename": "t4.jpg"}])
check("thumbnail: other file names kept as they are", [f["url"] for f in yt.thumbnail_files(video("t5", 1, thumbnail="https://i.ytimg.com/vi/t5/hqdefault.jpg?sqp=x"))]
      == ["https://i.ytimg.com/vi/t5/hqdefault.jpg"])
sc, at = FakeSC([video("t3", 1, thumbnail=None)]), FakeAT([channel_row()])
run(sc, at)
check("thumbnail: missing in the API -> no attachment, video still saved", len(at.posted()) == 1 and yt.V_THUMBNAIL not in at.posted()[0]["fields"])

# 4. Dates: Published = exact publishDate in UTC; the rough estimate only when it's missing
sc, at = FakeSC([video("exact", 1), video("rough", 60, exact=False), video("nodate", 1, exact=False, publishedTime=None)]), FakeAT([channel_row()])
run(sc, at)
pub = {r["fields"][yt.V_CONTENT_ID]: r["fields"].get(yt.V_PUBLISHED) for r in at.posted()}
check("dates: all stored whatever their date", set(pub) == {"exact", "rough", "nodate"})
check("dates: exact publishDate converted to UTC", pub["exact"] == iso(NOW - timedelta(days=1)))
check("dates: estimate used when publishDate is missing; none -> left empty",
      pub["rough"] == iso(NOW - timedelta(days=60) + timedelta(days=20)) and pub["nodate"] is None)

# 4b. Scraped Content: one table for every platform
sc, at = FakeSC([video("c1", 1, title="My title", description="Line 1\nLine 2")]), FakeAT([channel_row()])
run(sc, at)
c = at.posted()[0]["fields"]
check("content: writes go to Scraped Content and Accounts only", {t for _, t, _ in at.writes} == {yt.VIDEOS, yt.ACCOUNTS}
      and yt.VIDEOS == "tblViAU74E50jTA5H")
check("content: Platform YouTube, Type Video, Content ID = video ID", c[yt.V_PLATFORM] == "YouTube" and c[yt.V_TYPE] == "Video"
      and c[yt.V_CONTENT_ID] == "c1")
check("content: Title = video title, Text = full description", c[yt.V_TITLE] == "My title" and c[yt.V_TEXT] == "Line 1\nLine 2")
check("content: duration, views, likes, comments, link", (c[yt.V_DURATION], c[yt.V_VIEWS], c[yt.V_LIKES], c[yt.V_COMMENTS])
      == (1200, 1000, 50, 7) and c[yt.V_ACCOUNT] == ["recCH0000000000AA"] and c[yt.V_URL].endswith("v=c1"))
check("content: lookups only among YouTube rows", at.platforms == {"YouTube"})
same = [{"id": "recXX", "fields": {yt.V_CONTENT_ID: "c1", yt.V_PLATFORM: "X", yt.V_STATUS: "Reviewed"}}]
sc, at = FakeSC([video("c1", 1)]), FakeAT([channel_row()], same)
run(sc, at)
check("content: an X row with the same ID is left alone; the video is created", at.patched() == [] and len(at.posted()) == 1)
stored = [{"id": "recU", "fields": {yt.V_CONTENT_ID: "c1", yt.V_PLATFORM: "YouTube", yt.V_STATUS: "Reviewed"}}]
sc, at = FakeSC([video("c1", 1)]), FakeAT([channel_row(last=NOW - timedelta(days=3))], stored)
run(sc, at)
u = at.patched()[0]["fields"]
check("update: Platform/Type sent, Status not", u[yt.V_PLATFORM] == "YouTube" and u[yt.V_TYPE] == "Video" and yt.V_STATUS not in u)

# 5. Failures: Last Scraped kept, so the channel is due again tomorrow
sc, at = FakeSC(fail=yt.HttpError("HTTP 500 from /v1/youtube/channel-videos: oops")), FakeAT([channel_row(last=last)])
code, out = run(sc, at)
a = at.account()
check("ScrapeCreators error: error, reason, Last Scraped kept, exit 1", code == 1 and a[yt.A_LAST_STATUS] == "error"
      and "HTTP 500" in a[yt.A_SCRAPE_ERROR] and a[yt.A_LAST_SCRAPED] == iso(last))
check("...and still due the next day", yt.is_due(at.accounts["recCH0000000000AA"], NOW + timedelta(days=1)))
sc, at = FakeSC([video("n", 1)]), FakeAT([channel_row(last=last)], fail_post=True)
code, out = run(sc, at)
check("Airtable write fails: error, Last Scraped kept, credit still reported", code == 1 and at.account()[yt.A_LAST_SCRAPED] == iso(last)
      and "credits used 1" in out)
sc, at = FakeSC([video("n", 1)]), FakeAT([channel_row(pid="")])
code, out = run(sc, at)
check("no channel ID: error, no call", code == 1 and sc.calls == [] and "Platform ID" in at.account()[yt.A_SCRAPE_ERROR])

# 6. --new-accounts: only channels with no Last Scraped
rows = [channel_row(), channel_row("recDUE0000000000A", last=NOW - timedelta(days=4)),
        channel_row("recPAU0000000000A", scrape="Paused"),
        {"id": "recX00000000000AA", "fields": {yt.A_PLATFORM: "X", yt.A_SCRAPE: "Active", yt.A_PLATFORM_ID: "1"}}]
sc, at = FakeSC([video("n", 1)]), FakeAT(rows)
code, out = run(sc, at, new_accounts=True)
touched = {r["id"] for m, t, b in at.writes if t == yt.ACCOUNTS for r in b["records"]}
check("new-accounts: only the new channel read and updated", code == 0 and len(sc.calls) == 1 and touched == {"recCH0000000000AA"})
sc = FakeSC([video("n", 1)])
code, out = run(sc, at, new_accounts=True)
check("new-accounts: once read, a later webhook run skips it", code == 0 and sc.calls == [])
sc, at = FakeSC([video("n", 1)]), FakeAT(rows)
run(sc, at)
check("scheduled: due + new channels read, paused and X not", len(sc.calls) == 2)

# 7. youtube_profile: channel from the Profile URL
cases = {
    "https://www.youtube.com/@nicksaraev": ("handle", "nicksaraev"),
    "https://youtube.com/@nicksaraev/videos": ("handle", "nicksaraev"),
    "https://m.youtube.com/@nicksaraev?si=abc": ("handle", "nicksaraev"),
    "youtube.com/@NickSaraev": ("handle", "NickSaraev"), "@nicksaraev": ("handle", "nicksaraev"),
    "https://www.youtube.com/@%E0%A4%B9%E0%A4%BF%E0%A4%82%E0%A4%A6%E0%A5%80": ("handle", "हिंदी"),
    f"https://www.youtube.com/channel/{CH}": ("channelId", CH),
    f"https://www.youtube.com/channel/{CH}/featured": ("channelId", CH),
    "https://www.youtube.com/c/NickSaraev": ("url", "https://www.youtube.com/c/NickSaraev"),
    "https://www.youtube.com/user/somebody/videos": ("url", "https://www.youtube.com/user/somebody"),
    "https://www.youtube.com/watch?v=abc123": None, "https://youtu.be/abc123": None, "https://x.com/eptwts": None,
    "https://www.youtube.com/": None, "https://www.youtube.com/@ab": None, "https://www.youtube.com/channel/abc": None,
    "https://www.youtube.com/@a'b'c": None, "nicksaraev": None, "": None, None: None,
}
for text, want in cases.items():
    check(f"channel from {text!r} = {want!r}", yp.channel_from_url(text) == want)

CHANNEL = {"success": True, "credits_charged": 1, "credits_remaining": 900, "channelId": CH,
           "channel": "http://www.youtube.com/@nicksaraev", "handle": "@nicksaraev", "isVerified": False,
           "name": "Nick Saraev", "description": "Hi, I'm Nick.", "subscriberCount": 530000,
           "avatar": {"image": {"sources": [{"url": "https://yt3.googleusercontent.com/abc=s68-c-k-c0x00ffffff-no-rj", "width": 68}]}}}
f = yp.channel_fields(copy.deepcopy(CHANNEL), ("handle", "nicksaraev"))
check("profile mapping", f == {
    yt.A_NAME: "Nick Saraev", yt.A_PLATFORM: "YouTube", yt.A_PLATFORM_ID: CH, yt.A_HANDLE: "nicksaraev",
    yp.A_PROFILE_URL: "https://www.youtube.com/@nicksaraev", yp.A_AVATAR: "https://yt3.googleusercontent.com/abc=s800-c-k-c0x00ffffff-no-rj",
    yp.A_BIO: "Hi, I'm Nick.", yp.A_VERIFIED: False, yp.A_SUBSCRIBERS: 530000})
f = yp.channel_fields({"channelId": CH}, ("channelId", CH))
check("profile mapping: no handle -> /channel/ URL", f[yp.A_PROFILE_URL] == f"https://www.youtube.com/channel/{CH}" and yt.A_HANDLE not in f)

NEW = "recNEW0000000000A"
OTHER = {"id": "recOTHER00000000A", "fields": {yt.A_NAME: "Nick Saraev", yt.A_PLATFORM: "YouTube", yt.A_HANDLE: "nicksaraev",
                                                 yt.A_PLATFORM_ID: CH, yt.A_SCRAPE: "Active"}}


def setup(url, others=(), channel=CHANNEL, fail=None, extra=None):
    row = {"id": NEW, "fields": {yp.A_PROFILE_URL: url, yt.A_PLATFORM: "YouTube", **(extra or {})}}
    at, sc = FakeAT([row, *others]), FakeSC(channel=channel, fail=fail)
    yt.airtable, yt.scrapecreators = at, sc
    code = yp.setup(NEW)
    return code, at.accounts[NEW]["fields"], at, sc


code, row, at, sc = setup("https://www.youtube.com/@nicksaraev/videos")
check("setup ok: one call by handle", code == 0 and sc.calls == [("/v1/youtube/channel", {"handle": "nicksaraev"})])
check("setup ok: filled, Active, never, single write", row[yt.A_PLATFORM_ID] == CH and row[yt.A_SCRAPE] == "Active"
      and row[yt.A_LAST_STATUS] == "never" and row[yt.A_SCRAPE_ERROR] is None and len(at.writes) == 1
      and yt.A_LAST_SCRAPED not in row)
check("setup ok: the scraper then treats it as new", yt.is_due({"fields": row}, NOW) and not yt.parse_iso(row.get(yt.A_LAST_SCRAPED)))
code, row, at, sc = setup(f"https://www.youtube.com/channel/{CH}")
check("setup by channel ID", code == 0 and sc.calls[0][1] == {"channelId": CH})
code, row, at, sc = setup("https://www.youtube.com/c/NickSaraev")
check("setup by legacy /c/ URL", code == 0 and sc.calls[0][1] == {"url": "https://www.youtube.com/c/NickSaraev"})
code, row, at, sc = setup("https://www.youtube.com/watch?v=abc123")
check("video link: error, no call, not activated", code == 1 and sc.calls == [] and "not a YouTube channel link" in row[yt.A_SCRAPE_ERROR]
      and yt.A_SCRAPE not in row)
code, row, at, sc = setup("https://www.youtube.com/@NickSaraev", others=[OTHER])
check("duplicate handle: flagged before the call", code == 1 and sc.calls == [] and "Already tracked" in row[yt.A_SCRAPE_ERROR])
code, row, at, sc = setup("https://www.youtube.com/c/NickSaraev", others=[OTHER])
check("duplicate channel ID (legacy URL): flagged after the call", code == 1 and len(sc.calls) == 1
      and f"channel {CH}" in row[yt.A_SCRAPE_ERROR] and yt.A_SCRAPE not in row)
code, row, at, sc = setup("https://www.youtube.com/@nobody_here", fail=yt.HttpError("HTTP 404 from /v1/youtube/channel: not found"))
check("not found: flagged with the reason", code == 1 and "HTTP 404" in row[yt.A_SCRAPE_ERROR])
code, row, at, sc = setup("https://www.youtube.com/@nobody_here", channel={"success": True})
check("no channel ID in the response: flagged", code == 1 and "not found" in row[yt.A_SCRAPE_ERROR])
code, row, at, sc = setup("https://www.youtube.com/@nicksaraev", extra={yt.A_PLATFORM_ID: CH, yt.A_SCRAPE: "Paused"})
check("already has a channel ID (even paused): left alone", code == 0 and sc.calls == [] and at.writes == [])
code, row, at, sc = setup("https://x.com/eptwts", extra={yt.A_PLATFORM: "X"})
check("other platform: left alone", code == 0 and sc.calls == [] and at.writes == [])

# 8. Command lines
for mod, bad in ((yp, []), (yp, ["rec1"]), (yp, ["recNEW0000000000A; rm -rf /"])):
    try:
        mod.main(bad)
        check(f"bad args {bad} rejected", False)
    except SystemExit as e:
        check(f"bad args {bad} rejected", "Usage" in str(e.code))

print(f"{passed}/{passed + failed} checks passed")
sys.exit(1 if failed else 0)
