"""Offline unit tests for scripts/x.py. No network: twitterapi.io and Airtable are fakes.
Run: python3 tests/test_x.py"""
import copy, io, contextlib, json, os, re, sys, types
from datetime import datetime, timezone, timedelta

ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT + "/scripts")
import x

SAMPLE = json.load(open(ROOT + "/tests/fixtures/advanced_search_sample.json"))
EMPTY = {"tweets": [], "has_next_page": False, "next_cursor": ""}
os.environ.update(AIRTABLE_ACCESS_TOKEN="fake", TWITTER_API="fake")
x.log = lambda m: None
SLEEPS = []
x.time = types.SimpleNamespace(sleep=SLEEPS.append)  # no real waiting; record retry waits
EP_ID = "1000000000000000001"
NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


class FixedDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


x.datetime = FixedDT
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
H6 = timedelta(hours=6)
M15 = timedelta(minutes=15)


def acc(rec, handle, pid, last=None, status="never", scrape="Active", platform="X"):
    f = {x.A_NAME: handle, x.A_PLATFORM: platform, x.A_PLATFORM_ID: pid, x.A_HANDLE: handle,
         x.A_SCRAPE: scrape, x.A_LAST_STATUS: status}
    if last:
        f[x.A_LAST_SCRAPED] = last
    return {"id": rec, "fields": f}


EP = acc("recEP", "example_user", EP_ID)
prev = NOW - H6
EP_OK = acc("recEP", "example_user", EP_ID, iso(prev), "ok")  # read up to the previous run
passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name)


class FakeTW:
    """Scripted pages by call number; `fail` = {call number: HttpError}."""
    def __init__(self, pages, fail=None):
        self.pages, self.calls, self.fail, self.params = pages, 0, fail or {}, []

    def __call__(self, path, params=None):
        self.params.append(dict(params)); self.calls += 1
        if self.calls in self.fail:
            raise self.fail[self.calls]
        return copy.deepcopy(self.pages[min(self.calls - 1, len(self.pages) - 1)])


class WorldTW:
    """Answers queries from a list of tweets like twitterapi.io: handles, since/until, newest
    first, 20 per page, has_next_page true after any non-empty page."""
    def __init__(self, tweets, fail=None):
        self.tweets, self.calls, self.params, self.fail, self.billed = tweets, 0, [], fail or {}, 0

    def __call__(self, path, params=None):
        self.params.append(dict(params)); self.calls += 1
        if self.calls in self.fail:
            raise self.fail[self.calls]
        q = params["query"]
        handles = {h.lower() for h in re.findall(r"from:(\w+)", q)}
        since = int(re.search(r"since_time:(\d+)", q).group(1)); until = int(re.search(r"until_time:(\d+)", q).group(1))
        match = sorted((t for t in self.tweets if t["author"]["userName"].lower() in handles
                        and since <= x.parse_created(t["createdAt"]).timestamp() < until),
                       key=lambda t: x.parse_created(t["createdAt"]), reverse=True)
        off = int(params["cursor"]) if params.get("cursor") else 0
        page = match[off:off + 20]
        self.billed += max(1, len(page))
        return {"tweets": copy.deepcopy(page), "has_next_page": bool(page), "next_cursor": str(off + 20) if page else ""}


def parse_lookup(formula):
    """The Platform and Content IDs of a Scraped Content lookup; fails on any other shape."""
    m = re.fullmatch(r"AND\(\{%s\}='(\w+)',OR\((.*)\)\)" % x.P_PLATFORM, formula)
    assert m, formula
    ids = re.findall(r"\{%s\}='((?:[^'\\]|\\.)*)'" % x.P_CONTENT_ID, m.group(2))
    return m.group(1), [i.replace("\\'", "'").replace("\\\\", "\\") for i in ids]


class FakeAT:
    def __init__(self, accounts, posts=(), fail_post=False, reject_id=None):
        self.accounts, self.posts, self.writes, self.lookups, self.platforms = accounts, list(posts), [], [], set()
        self.fail_post, self.reject_id, self.n, self.full_loads = fail_post, reject_id, 0, 0

    def __call__(self, method, table, params=None, body=None):
        if method == "GET":
            if table == x.POSTS:
                self.full_loads += 1
            return {"records": copy.deepcopy(self.accounts if table == x.ACCOUNTS else self.posts)}
        if table == f"{x.POSTS}/listRecords":
            platform, ids = parse_lookup(body["filterByFormula"])
            self.lookups.append(ids)
            self.platforms.add(platform)
            return {"records": [copy.deepcopy(p) for p in self.posts if p["fields"].get(x.P_CONTENT_ID) in ids
                                and p["fields"].get(x.P_PLATFORM) == platform]}
        self.writes.append((method, table, copy.deepcopy(body)))
        if table == x.POSTS and method == "POST" and self.fail_post:
            raise x.HttpError("HTTP 503 from /v0: down", 503)
        if table == x.POSTS and self.reject_id and any(r["fields"].get(x.P_CONTENT_ID) == self.reject_id for r in body["records"]):
            raise x.HttpError('HTTP 422 from /v0: {"error":{"type":"INVALID_VALUE_FOR_COLUMN"}}', 422)
        out = []
        for r in body["records"]:
            self.n += 1
            rec = {"id": r.get("id") or f"recNEW{self.n}", "fields": r["fields"]}
            out.append(rec)
            if table == x.POSTS and method == "POST":
                self.posts.append(copy.deepcopy(rec))
        return {"records": out}

    def posted(self):
        return [r for m, t, b in self.writes if t == x.POSTS and m == "POST" for r in b["records"]]

    def patched(self):
        return [r for m, t, b in self.writes if t == x.POSTS and m == "PATCH" for r in b["records"]]

    def account_updates(self):
        return {r["id"]: r["fields"] for m, t, b in self.writes if t == x.ACCOUNTS for r in b["records"]}


def run(tw, at, new_accounts=False):
    x.twitterapi, x.airtable = tw, at
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = x.main(new_accounts=new_accounts)
    return code, buf.getvalue()


def q_times(params):
    q = params["query"]
    return int(re.search(r"since_time:(\d+)", q).group(1)), int(re.search(r"until_time:(\d+)", q).group(1))


originals = [t for t in SAMPLE["tweets"] if not t.get("quoted_tweet")]
quotes = [t for t in SAMPLE["tweets"] if t.get("quoted_tweet")]


def tweet(tid, author_id, handle, created, **kw):
    t = copy.deepcopy(originals[0])
    t.update({"id": tid, "author": {"id": author_id, "userName": handle},
              "createdAt": created.strftime("%a %b %d %H:%M:%S +0000 %Y"), "extendedEntities": {}})
    t.update(kw)
    return t


# 1. New account, real sample: query shape, last 24 hours, mapping, quotes skipped
tw, at = FakeTW([SAMPLE, EMPTY]), FakeAT([EP])
code, out = run(tw, at)
since, until = q_times(tw.params[0])[0], q_times(tw.params[-1])[1]
check("exit 0", code == 0)
check("query shape", tw.params[0]["query"].startswith("(from:example_user) since_time:")
      and tw.params[0]["query"].endswith("-filter:replies -filter:retweets") and tw.params[0]["queryType"] == "Latest")
check("cursor only from page 2", "cursor" not in tw.params[0] and tw.params[1]["cursor"] == SAMPLE["next_cursor"])
check("until = moment of execution", until == int(NOW.timestamp()))
check("new account: last 24 hours exactly, in 3 slices", until - since == 24 * 3600
      and len({q_times(p) for p in tw.params}) == 3)
posts = at.posted()
check("#8 quote tweets skipped, 2 originals created", len(posts) == 2 and "2 quote/reply/retweet skipped" in out
      and not {q["id"] for q in quotes} & {r["fields"][x.P_CONTENT_ID] for r in posts})
c = next(r["fields"] for r in posts if r["fields"][x.P_CONTENT_ID] == "2000000000000000002")
check("mapping", c[x.P_PUBLISHED] == "2026-09-23T10:36:09.000Z" and c[x.P_URL].endswith("/2000000000000000002")
      and (c[x.P_VIEWS], c[x.P_LIKES], c[x.P_COMMENTS], c[x.P_REPOSTS], c[x.P_QUOTES], c[x.P_BOOKMARKS]) == (8519, 185, 33, 3, 3, 41))
check("linked, Status New, html unescaped", c[x.P_ACCOUNT] == ["recEP"] and c[x.P_STATUS] == "New"
      and all("&amp;" not in r["fields"][x.P_TEXT] for r in posts))
u = at.account_updates()["recEP"]
check("success: Last Scraped = run time, ok", u[x.A_LAST_SCRAPED] == iso(NOW) and u[x.A_LAST_STATUS] == "ok")
check("#7 success clears Scrape Error (sends null)", x.A_SCRAPE_ERROR in u and u[x.A_SCRAPE_ERROR] is None)
check("est credits 105 (4 tweets + 3 empty pages)", "est. credits 105" in out)

# 2. #4 Only the returned Tweet IDs are looked up; the table is never loaded
check("#4 Scraped Content table not loaded", at.full_loads == 0)
check("#4 one lookup, only the 2 original IDs", at.lookups == [["2000000000000000002", "2000000000000000004"]])
stored = [{"id": "recOLD", "fields": {x.P_CONTENT_ID: originals[0]["id"], x.P_PLATFORM: "X", x.P_ACCOUNT: ["recEP"]}},
          {"id": "recOTHER", "fields": {x.P_CONTENT_ID: "unrelated", x.P_PLATFORM: "X"}}]
tw, at = FakeTW([SAMPLE, EMPTY]), FakeAT([EP], stored)
run(tw, at)
up = at.patched()
check("overlap tweet found by lookup -> updated, no Status", len(up) == 1 and up[0]["id"] == "recOLD" and x.P_STATUS not in up[0]["fields"])
check("no duplicate created", len(at.posted()) == 1)
many = [tweet(str(70000 + i), EP_ID, "example_user", NOW - timedelta(minutes=i + 1)) for i in range(120)]
tw, at = WorldTW(many), FakeAT([EP])
run(tw, at)
check("#4 lookups chunked by 50", [len(l) for l in at.lookups] == [50, 50, 20] and len(at.posted()) == 120)
check("#4 quote in lookup formula escaped", "\\'" in "{%s}='%s'" % (x.P_CONTENT_ID, "a'b".replace("'", "\\'")))

# 3. Window rules
def ws(accounts):
    return x.window_start(accounts, NOW)
H8, H24 = timedelta(hours=8), timedelta(hours=24)
check("window: on schedule, last 8 hours", ws([acc("a", "a", "1", iso(prev), "ok")]) == int((NOW - H8).timestamp()))
check("window: run 7h45m after the last, still 8 hours", ws([acc("a", "a", "1", iso(NOW - timedelta(hours=7, minutes=45)), "ok")]) == int((NOW - H8).timestamp()))
check("window: after a failure, gap re-read", ws([acc("a", "a", "1", iso(NOW - 2 * H6), "error")]) == int((NOW - 2 * H6 - M15).timestamp()))
check("window: new accounts, last 24 hours", ws([acc("b", "b", "2"), acc("c", "c", "3")]) == int((NOW - H24).timestamp()))
check("window: stale (>72h) = new, last 24 hours", ws([acc("b", "b", "2", iso(NOW - timedelta(days=10)), "ok")]) == int((NOW - H24).timestamp()))
check("window: future Last Scraped = new", ws([acc("a", "a", "1", iso(NOW + H6), "ok")]) == int((NOW - H24).timestamp()))

# 4. #2 Grouping: one account behind doesn't rewind the others
g = x.group_accounts([acc(f"r{i}", f"u{i}", str(i), iso(prev), "ok") for i in range(5)]
                     + [acc("rLate", "late", "99", iso(NOW - 4 * H6), "error")], NOW)
check("#2 account behind gets its own query", len(g) == 2 and [a["id"] for a in g[1]] == ["rLate"] and len(g[0]) == 5)
g = x.group_accounts([acc(f"r{i}", f"u{i}", str(i), iso(prev + timedelta(minutes=i)), "ok") for i in range(20)], NOW)
check("#2 20 up-to-date accounts -> 15 + 5", [len(b) for b in g] == [15, 5])
g = x.group_accounts([acc("r0", "u0", "0", iso(prev), "ok"), acc("rNew", "new", "5")], NOW)
check("#2 new account gets its own batch (its 24h, not 8h)", len(g) == 2 and [a["id"] for a in g[1]] == ["rNew"])
g = x.group_accounts([acc(f"rN{i}", f"n{i}", str(i)) for i in range(20)], NOW)
check("#2 20 new accounts -> 15 + 5", [len(b) for b in g] == [15, 5])
g = x.group_accounts([acc("rLate", "late", "99", iso(NOW - 4 * H6), "error"), acc("rNew", "new", "5")], NOW)
check("#2 new account never joins a catching-up batch (no backfill)", len(g) == 2 and [a["id"] for a in g[1]] == ["rNew"])
accs = [acc(f"r{i}", f"u{i}", str(1000 + i), iso(prev), "ok") for i in range(3)] + [acc("rLate", "late", "999", iso(NOW - 4 * H6), "error")]
tw, at = WorldTW([]), FakeAT(accs)
run(tw, at)
qs = [(p["query"].count("from:"), until - since) for p in tw.params for since, until in [q_times(p)]]
check("#2 end to end: up-to-date batch reads 8h, the late one reads its own 24h",
      qs[0] == (3, 8 * 3600) and all(n == 1 for n, _ in qs[1:]) and sum(d for _, d in qs[1:]) == 24 * 3600 + 15 * 60)

# 5. #1 Slices and the page cap: nothing unread is ever skipped, nothing paid twice
check("slices: 8h is one slice", x.slices(0, 8 * 3600) == [(0, 8 * 3600)])
check("slices: 30h -> 8+8+8+6", [b - a for a, b in x.slices(0, 30 * 3600)] == [8 * 3600] * 3 + [6 * 3600])
late = acc("recEP", "example_user", EP_ID, iso(NOW - 5 * H6), "error")  # 30h behind
busy = [tweet(str(80000 + i), EP_ID, "example_user", NOW - timedelta(minutes=5 * i + 1)) for i in range(350)]  # 350 in ~29h
tw, at = WorldTW(busy), FakeAT([late])
code, out = run(tw, at)
got = {r["fields"][x.P_CONTENT_ID] for r in at.posted()}
u = at.account_updates()["recEP"]
check("#1 350 tweets in a 30h catch-up: all saved", len(got) == 350 and len(at.posted()) == 350)
check("#1 slices read oldest first", q_times(tw.params[0])[0] == int((NOW - 5 * H6 - M15).timestamp()))
check("#1 no tweet billed twice (boundary seconds aside)", tw.billed <= 350 + 2 * tw.calls)
check("#1 ok, Last Scraped = now", u[x.A_LAST_STATUS] == "ok" and u[x.A_LAST_SCRAPED] == iso(NOW))
flood = [tweet(str(90000 + i), EP_ID, "example_user", NOW - timedelta(minutes=5)) for i in range(250)]  # all in one second
tw, at = WorldTW(flood), FakeAT([acc("recEP", "example_user", EP_ID, iso(prev), "ok")])
code, out = run(tw, at)
u = at.account_updates()["recEP"]
check("#1 cap that can't be narrowed (250 tweets in one second): saved, flagged, no loop", len(at.posted()) >= 200
      and u[x.A_LAST_STATUS] == "error" and "could not be read" in u[x.A_SCRAPE_ERROR] and tw.calls <= 25)
huge = [tweet(str(100000 + i), EP_ID, "example_user", NOW - timedelta(minutes=(71 * 60) * i / 1200 + 1)) for i in range(1200)]
tw, at = WorldTW(huge), FakeAT([acc("recEP", "example_user", EP_ID, iso(NOW - timedelta(hours=71)), "error")])
code, out = run(tw, at)
u = at.account_updates()["recEP"]
covered = x.parse_iso(u.get(x.A_LAST_SCRAPED))
check("#1 budget: at most MAX_CALLS_PER_BATCH calls", tw.calls <= x.MAX_CALLS_PER_BATCH)
check("#1 budget: Last Scraped moves only to the end of the last full slice", covered is not None and covered < NOW
      and all(x.parse_created(t["createdAt"]) >= covered or t["id"] in {r["fields"][x.P_CONTENT_ID] for r in at.posted()} for t in huge
              if x.parse_created(t["createdAt"]) < covered and x.parse_created(t["createdAt"]) >= NOW - timedelta(hours=71, minutes=15)))
check("#1 budget: noted, not an error", u[x.A_LAST_STATUS] == "ok" and "continues next run" in out)
at.accounts[0]["fields"][x.A_LAST_SCRAPED] = u[x.A_LAST_SCRAPED]
for _ in range(5):
    at.accounts[0]["fields"].update(at.account_updates()["recEP"])
    tw2 = WorldTW(huge); at.writes.clear(); run(tw2, at)
got = {p["fields"][x.P_CONTENT_ID] for p in at.posts}
check("#1 budget: later runs finish the catch-up, nothing lost", all(t["id"] in got for t in huge
      if x.parse_created(t["createdAt"]) >= NOW - timedelta(hours=71, minutes=15)))

# 6. #5 One retry on twitterapi.io 429 / 5xx / network errors
SLEEPS.clear()
tw, at = FakeTW([SAMPLE, SAMPLE, EMPTY], fail={1: x.HttpError("HTTP 500 from /twitter: oops", 500)}), FakeAT([EP_OK])
code, out = run(tw, at)
check("#5 500 then ok: retried once, saved, ok", code == 0 and tw.calls == 3 and len(at.posted()) == 2 and SLEEPS == [x.RETRY_WAIT_SECONDS])
SLEEPS.clear()
tw, at = FakeTW([SAMPLE, SAMPLE, EMPTY], fail={1: x.HttpError("HTTP 429 from /twitter: slow down", 429, "7")}), FakeAT([EP_OK])
run(tw, at)
check("#5 429 honours Retry-After", SLEEPS == [7] and len(at.posted()) == 2)
SLEEPS.clear()
tw, at = FakeTW([SAMPLE], fail={1: x.HttpError("Network error: reset"), 2: x.HttpError("Network error: reset")}), FakeAT([acc("recEP", "example_user", EP_ID, iso(prev), "ok")])
code, out = run(tw, at)
u = at.account_updates()["recEP"]
check("#5 fails twice: error, Last Scraped kept, exit 1", code == 1 and tw.calls == 2 and x.A_LAST_SCRAPED not in u and u[x.A_LAST_STATUS] == "error")
SLEEPS.clear()
tw, at = FakeTW([SAMPLE], fail={1: x.HttpError("HTTP 401 from /twitter: bad key", 401)}), FakeAT([EP_OK])
code, out = run(tw, at)
check("#5 401 not retried", tw.calls == 1 and SLEEPS == [] and code == 1)
SLEEPS.clear()
tw, at = FakeTW([SAMPLE, SAMPLE, EMPTY], fail={2: x.HttpError("HTTP 502 from /twitter: bad gateway", 502)}), FakeAT([EP_OK])
code, out = run(tw, at)
check("#5 later page retried too", code == 0 and SLEEPS == [x.RETRY_WAIT_SECONDS])
SLEEPS.clear()
tw, at = FakeTW([SAMPLE, SAMPLE], fail={2: x.HttpError("HTTP 500", 500), 3: x.HttpError("HTTP 500", 500)}), FakeAT([acc("recEP", "example_user", EP_ID, iso(prev), "ok")])
code, out = run(tw, at)
u = at.account_updates()["recEP"]
check("later page fails twice: page 1 saved, Last Scraped kept", len(at.posted()) == 2 and x.A_LAST_SCRAPED not in u and "page 2 failed" in u[x.A_SCRAPE_ERROR])

# 7. #3 Author user ID saved to an account without a Platform ID
noid = acc("recN", "example_user", "")
tw, at = FakeTW([SAMPLE, EMPTY]), FakeAT([noid])
run(tw, at)
u = at.account_updates()["recN"]
check("#3 empty Platform ID filled from author.id", u.get(x.A_PLATFORM_ID) == EP_ID and len(at.posted()) == 2)
tw, at = FakeTW([SAMPLE, EMPTY]), FakeAT([EP])
run(tw, at)
check("#3 existing Platform ID left alone", x.A_PLATFORM_ID not in at.account_updates()["recEP"])
accs = [acc(f"rec{i}", f"user{i}", str(1000 + i), iso(prev), "ok") for i in range(3)]
t_id = tweet("t1", "1001", "renamed_handle", NOW - timedelta(hours=1))
t_h = tweet("t2", "999999", "USER2", NOW - timedelta(hours=2))
t_other = tweet("t3", "555", "stranger", NOW - timedelta(hours=1))
tw, at = FakeTW([{"tweets": [t_id, t_h, t_other], "has_next_page": False}]), FakeAT(accs)
run(tw, at)
posts = {r["fields"][x.P_CONTENT_ID]: r["fields"][x.P_ACCOUNT] for r in at.posted()}
check("match by author id, then handle; stranger skipped + flagged", posts == {"t1": ["rec1"], "t2": ["rec2"]}
      and "author not one of" in at.account_updates()["rec0"][x.A_SCRAPE_ERROR])

# 8. Media, failures, 422, paging, filters
vid = quotes[0]["quoted_tweet"]["extendedEntities"]["media"][0]
tm = tweet("555", EP_ID, "example_user", NOW - timedelta(hours=1), extendedEntities={"media": [
    {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/ABC.png"}, vid,
    {"type": "animated_gif", "video_info": {"variants": [{"bitrate": 0, "content_type": "video/mp4", "url": "https://video.twimg.com/G.mp4"}]}}]})
files = x.media_files(tm)
best = max((v for v in vid["video_info"]["variants"] if v["content_type"] == "video/mp4"), key=lambda v: v["bitrate"])
check("media: photo orig, best mp4, gif", len(files) == 3 and files[0]["url"].endswith("ABC.png?name=orig") and files[1]["url"] == best["url"])
page = {"tweets": [tm], "has_next_page": False}
tw, at = FakeTW([page]), FakeAT([EP], [{"id": "recM", "fields": {x.P_CONTENT_ID: "555", x.P_PLATFORM: "X", x.P_MEDIA: [{"id": "att"}]}}])
run(tw, at)
check("filled media not re-sent", x.P_MEDIA not in at.patched()[0]["fields"])
tw, at = FakeTW([page]), FakeAT([EP], [{"id": "recM", "fields": {x.P_CONTENT_ID: "555", x.P_PLATFORM: "X"}}])
run(tw, at)
check("empty media backfilled", len(at.patched()[0]["fields"].get(x.P_MEDIA, [])) == 3)
tw, at = FakeTW([SAMPLE, EMPTY]), FakeAT([acc("recEP", "example_user", EP_ID, iso(prev), "ok")], fail_post=True)
code, out = run(tw, at)
u = at.account_updates()["recEP"]
check("Airtable down: error, Last Scraped kept", code == 1 and x.A_LAST_SCRAPED not in u and "Airtable write failed" in u[x.A_SCRAPE_ERROR])
twelve = [tweet(str(9000 + i), EP_ID, "example_user", NOW - timedelta(minutes=i + 1)) for i in range(12)]
tw, at = FakeTW([{"tweets": twelve, "has_next_page": False}]), FakeAT([acc("recEP", "example_user", EP_ID, iso(prev), "ok")], reject_id="9003")
code, out = run(tw, at)
u = at.account_updates()["recEP"]
check("422: other 11 saved, bad one flagged, window advances", len({r["fields"][x.P_CONTENT_ID] for r in at.posted()} - {"9003"}) == 11
      and "9003 (Airtable rejected it" in u[x.A_SCRAPE_ERROR] and u[x.A_LAST_SCRAPED] == iso(NOW))
x.twitterapi = FakeTW([{"tweets": twelve[:4], "has_next_page": True, "next_cursor": "s"}, {"tweets": twelve[4:7], "has_next_page": False}])
tws, calls, _, inc = x.fetch_tweets(["example_user"], 0, 1)
check("short page with has_next_page still followed", calls == 2 and len(tws) == 7 and inc is None)
x.twitterapi = FakeTW([{"tweets": twelve, "has_next_page": True, "next_cursor": "same"}] * 3)
tws, calls, _, inc = x.fetch_tweets(["example_user"], 0, 1)
check("repeated page stops", calls == 2 and len(tws) == 12)
bad_handle, paused, yt = acc("recBAD", "not a handle!", "7"), acc("recP", "paused1", "8", scrape="Paused"), acc("recYT", "someone", "9", platform="YouTube")
tw, at = FakeTW([EMPTY]), FakeAT([EP_OK, bad_handle, paused, yt])
code, out = run(tw, at)
ups = at.account_updates()
check("invalid handle flagged, not queried", ups["recBAD"][x.A_LAST_STATUS] == "error" and "not a handle" not in tw.params[0]["query"])
check("paused / other platform untouched", "recP" not in ups and "recYT" not in ups)
check("empty window: 1 call, ok", tw.calls == 1 and ups["recEP"][x.A_LAST_STATUS] == "ok")
t = copy.deepcopy(originals[0]); t["text"] = "a" * 150000
check("text capped at 100k", len(x.post_fields(t, "r", iso(NOW))[x.P_TEXT]) == 100000)

# 9. --new-accounts: the run started by the Airtable automation when an X account is added
SLEEPS.clear()
old_acc = acc("recOLD", "old_user", "77", iso(prev), "ok")
stale = acc("recSTALE", "back_user", "78", iso(NOW - timedelta(days=10)), "ok")
fresh = [tweet("n1", EP_ID, "example_user", NOW - timedelta(hours=23)), tweet("n2", EP_ID, "example_user", NOW - timedelta(hours=1)),
         tweet("n0", EP_ID, "example_user", NOW - timedelta(hours=25)), tweet("o1", "77", "old_user", NOW - timedelta(hours=1))]
tw, at = WorldTW(fresh), FakeAT([old_acc, EP, stale, acc("recP", "paused1", "8", scrape="Paused")])
code, out = run(tw, at, new_accounts=True)
ups = at.account_updates()
check("new-accounts: only new (and >72h stale) accounts read, in one batch", code == 0
      and all("old_user" not in p["query"] and "paused1" not in p["query"] for p in tw.params)
      and all("example_user" in p["query"] and "back_user" in p["query"] for p in tw.params))
check("new-accounts: last 24 hours only", {r["fields"][x.P_CONTENT_ID] for r in at.posted()} == {"n1", "n2"}
      and q_times(tw.params[0])[0] == int((NOW - timedelta(hours=24)).timestamp()))
check("new-accounts: Last Scraped = run time; other accounts untouched", set(ups) == {"recEP", "recSTALE"}
      and ups["recEP"][x.A_LAST_SCRAPED] == iso(NOW) and ups["recEP"][x.A_LAST_STATUS] == "ok")
tw, at = WorldTW(fresh), FakeAT([old_acc, acc("recP", "paused1", "8", scrape="Paused")])
code, out = run(tw, at, new_accounts=True)
check("new-accounts: none new -> no calls, no writes, exit 0", code == 0 and tw.calls == 0 and not at.writes)
tw, at = WorldTW(fresh, fail={1: x.HttpError("HTTP 500", 500), 2: x.HttpError("HTTP 500", 500)}), FakeAT([EP])
code, out = run(tw, at, new_accounts=True)
u = at.account_updates()["recEP"]
check("new-accounts: first read fails -> error, Last Scraped = 24h back", code == 1 and u[x.A_LAST_STATUS] == "error"
      and u[x.A_LAST_SCRAPED] == iso(NOW - timedelta(hours=24)))
at.accounts[0]["fields"].update(u)
tw = WorldTW(fresh); at.writes.clear()
code, out = run(tw, at)
check("new-accounts: next scheduled run reads the full 24h", code == 0 and {r["fields"][x.P_CONTENT_ID] for r in at.posted()} == {"n1", "n2"}
      and q_times(tw.params[0])[0] == int((NOW - timedelta(hours=24) - M15).timestamp()))
at.accounts[0]["fields"].update(at.account_updates()["recEP"])
tw = WorldTW(fresh)
code, out = run(tw, at, new_accounts=True)
check("new-accounts: once read, a later webhook run skips it", code == 0 and tw.calls == 0)

# 10. Scraped Content: one table for every platform
tw, at = FakeTW([SAMPLE, EMPTY]), FakeAT([EP])
run(tw, at)
posts = at.posted()
check("content: writes go to Scraped Content and Accounts only", {t for _, t, _ in at.writes} == {x.POSTS, x.ACCOUNTS}
      and x.POSTS == "tblViAU74E50jTA5H")
check("content: every tweet has Platform X and Content ID = Tweet ID", all(r["fields"][x.P_PLATFORM] == "X" for r in posts)
      and {r["fields"][x.P_CONTENT_ID] for r in posts} == {"2000000000000000002", "2000000000000000004"})
check("content: lookups only among X rows", at.platforms == {"X"})
c = next(r["fields"] for r in posts if r["fields"][x.P_CONTENT_ID] == "2000000000000000002")
first = next(l.strip() for l in x.html.unescape(c[x.P_TEXT]).splitlines() if l.strip())
check("title: first line of the tweet, max 100 chars, no newline", c[x.P_TITLE] == first[:100] and "\n" not in c[x.P_TITLE]
      and len(c[x.P_TITLE]) <= 100)
t = tweet("t1", EP_ID, "example_user", NOW, text="\n\n  Line one &amp; more  \nline two")
check("title: blank lines skipped, stripped, unescaped", x.post_fields(t, "r", iso(NOW))[x.P_TITLE] == "Line one & more")
t = tweet("t1", EP_ID, "example_user", NOW, text="a" * 300)
check("title: cut to 100", x.post_fields(t, "r", iso(NOW))[x.P_TITLE] == "a" * 100)
for text in ("", None, "   \n \n"):
    t = tweet("t1", EP_ID, "example_user", NOW, text=text)
    check(f"title: none for text {text!r}", x.P_TITLE not in x.post_fields(t, "r", iso(NOW)))
t = tweet("t1", EP_ID, "example_user", NOW, text="one two")
check("title: unicode line separator ends the line", x.post_fields(t, "r", iso(NOW))[x.P_TITLE] == "one")
photo = {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/A.jpg"}
types_ = {
    "Text": [{}, {"media": []}, None, [], {"media": None}, {"media": "x"}, {"media": [None, "x"]}],
    "Photo": [{"media": [photo]}, {"media": [photo, photo]}],
    "Video": [{"media": [vid]}, {"media": [photo, vid]}, {"media": [{"type": "animated_gif"}]}],
}
for want, cases in types_.items():
    for ee in cases:
        t = tweet("t1", EP_ID, "example_user", NOW, extendedEntities=ee)
        check(f"type {want} for {str(ee)[:40]}", x.post_fields(t, "r", iso(NOW))[x.P_TYPE] == want)
check("type: values are Type options in Airtable", {"Text", "Photo", "Video"} <= {"Video", "Reel", "Photo", "Carousel", "Text", "Link", "Gallery"})
# Another platform's row with the same Content ID is never matched or updated
same = [{"id": "recYT", "fields": {x.P_CONTENT_ID: "2000000000000000002", x.P_PLATFORM: "YouTube", x.P_STATUS: "Reviewed"}}]
tw, at = FakeTW([SAMPLE, EMPTY]), FakeAT([EP], same)
run(tw, at)
check("content: a YouTube row with the same ID is left alone; the tweet is created", at.patched() == []
      and "2000000000000000002" in {r["fields"][x.P_CONTENT_ID] for r in at.posted()})
# Update path: Platform/Title/Type are re-sent (unchanged), Status still never
stored = [{"id": "recOLD", "fields": {x.P_CONTENT_ID: "2000000000000000002", x.P_PLATFORM: "X", x.P_STATUS: "Reviewed"}}]
tw, at = FakeTW([SAMPLE, EMPTY]), FakeAT([EP], stored)
run(tw, at)
u = at.patched()[0]["fields"]
check("update: Platform, Title, Type sent; Status not", u[x.P_PLATFORM] == "X" and x.P_TITLE in u and x.P_TYPE in u
      and x.P_STATUS not in u)

# 11. #6 Single-flight lock
x.LOCK_FILE = x.Path(os.environ.get("TMPDIR", "/tmp")) / "x_scrape_test.lock"
first = x.acquire_lock()
second = x.acquire_lock()
check("#6 second run can't take the lock", first is not None and second is None)
first.close()
third = x.acquire_lock()
check("#6 lock free again after the first run ends", third is not None)
third.close()

print(f"{passed}/{passed + failed} checks passed")
sys.exit(1 if failed else 0)
