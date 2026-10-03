"""All four scrapers writing into the one Scraped Content table. No network: twitterapi.io,
ScrapeCreators, v.redd.it and Airtable are fakes.

The fake Airtable holds a single Scraped Content table and checks every write against the
table's real schema (field IDs, types, select options, as created in Airtable on 2026-10-02,
without the Thumbnail field, which was removed: thumbnails go in Media):
an unknown field, a select value that isn't an option or a value of the wrong type is
rejected with a 422, as Airtable does, and counted as a failure here.

Ten simulated days: X, YouTube and Reddit daily, Instagram every 3 days, with
the same Content IDs used on every platform (c1, c2, ...) so any lookup that isn't limited to
its own platform would update or skip another platform's row. Some rows are marked Reviewed
along the way. Checked at the end: every returned item stored once per platform, no row
touched by another platform's scraper, Reviewed kept, attachments never re-sent, the table
never loaded in full.
Run: python3 tests/test_scraped_content.py"""
import contextlib, copy, io, os, random, re, sys, types
from datetime import datetime as real_dt, timedelta, timezone

ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT + "/scripts")
import instagram as ig
import reddit as rd
import x
import youtube as yt

MODULES = (x, yt, rd, ig)
os.environ.update(AIRTABLE_ACCESS_TOKEN="fake", TWITTER_API="fake", SCRAPE_CREATORS="fake")
T0 = real_dt(2026, 10, 2, 0, 0, tzinfo=timezone.utc)
SIM = {"now": T0}


class SimDT(real_dt):
    @classmethod
    def now(cls, tz=None):
        return SIM["now"] if tz else SIM["now"].replace(tzinfo=None)


for m in MODULES:
    m.log = lambda msg: None
    m.datetime = SimDT
x.time = types.SimpleNamespace(sleep=lambda s: None)
passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name)


# Scraped Content tblViAU74E50jTA5H as it is in Airtable: field ID -> (name, type, options)
CONTENT = "tblViAU74E50jTA5H"
SCHEMA = {
    "fldQo17O1tcoje3HB": ("Content ID", "singleLineText", None),
    "fldNkd3XzlbpdNPW6": ("Title", "singleLineText", None),
    "fld79CFY4T9xkiA6Q": ("Platform", "singleSelect", {"YouTube", "Instagram", "Reddit", "X"}),
    "fldn8KHcs9SurgWBE": ("Account", "multipleRecordLinks", None),
    "fldQRzqXqCWMbYaLD": ("URL", "url", None),
    "fld4g5cokEPmzaoVA": ("Type", "singleSelect", {"Video", "Reel", "Photo", "Carousel", "Text", "Link", "Gallery"}),
    "fldkuaPl7DjFmlj7o": ("Text", "multilineText", None),
    "fldTvHNMgNc2DjXEk": ("Author", "singleLineText", None),
    "fldQISDIW9m5QF5QA": ("Flair", "singleLineText", None),
    "fldTdsMUfOHYCkmVP": ("Published", "dateTime", None),
    "fldlIejl80YEiJ4th": ("Duration", "duration", None),
    "fldoJ7VfdRHC78CeJ": ("Views", "number", None),
    "fldBybnTMpW6Egc3O": ("Likes", "number", None),
    "fldPIA6Zm9Rib9Xe8": ("Comments", "number", None),
    "fldTzcMOI6pTw6L08": ("Reposts", "number", None),
    "fldOVfFBrh8tG84a5": ("Quotes", "number", None),
    "fldnpgk0h2RfSSsJX": ("Bookmarks", "number", None),
    "fldAlSelO3OXm353v": ("Score", "number", None),
    "fldXWnVTWEqgepC02": ("Upvote Ratio", "percent", None),
    "fld0CsTd3XkegpUEP": ("Media", "multipleAttachments", None),
    "fld2D5rCtlEW9w2Hn": ("Status", "singleSelect", {"New", "Reviewed"}),
    "fldtqUWCeQmuP2Ofs": ("Last Scraped", "dateTime", None),
}
ACCOUNTS = "tbl04PJf51XEAiA8h"
ACCOUNT_SCHEMA = {  # the Accounts fields the scrapers write
    "fldn87Q7Uz8fl7erL": ("Last Scraped", "dateTime", None),
    "flduqhmL46oI0XJPo": ("Last Scrape Status", "singleSelect", {"never", "ok", "error"}),
    "flde8rfQchM6wk7Ho": ("Scrape Error", "multilineText", None),
    "fldVaDjv7ixoxEE8N": ("Platform ID", "singleLineText", None),
}
ID, PLATFORM, ACCOUNT, STATUS = "fldQo17O1tcoje3HB", "fld79CFY4T9xkiA6Q", "fldn8KHcs9SurgWBE", "fld2D5rCtlEW9w2Hn"
ATTACHMENTS = ("fld0CsTd3XkegpUEP",)
DATE_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{3})?Z$")
PER_PLATFORM = {  # what each platform may fill (its own fields only)
    "X": {"Content ID", "Title", "Platform", "Type", "Text", "Account", "URL", "Published", "Views", "Likes", "Comments",
          "Reposts", "Quotes", "Bookmarks", "Media", "Status", "Last Scraped"},
    "YouTube": {"Content ID", "Title", "Platform", "Type", "Text", "Account", "URL", "Published", "Duration", "Views",
                "Likes", "Comments", "Media", "Status", "Last Scraped"},
    "Reddit": {"Content ID", "Title", "Platform", "Type", "Text", "Account", "URL", "Author", "Flair", "Published",
               "Comments", "Score", "Upvote Ratio", "Media", "Status", "Last Scraped"},
    "Instagram": {"Content ID", "Title", "Platform", "Type", "Text", "Account", "URL", "Published", "Duration", "Views",
                  "Likes", "Comments", "Media", "Status", "Last Scraped"},
}


def problem(field, value, schema, accounts):
    """Why Airtable would reject `value` for `field`, or None."""
    if field not in schema:
        return f"unknown field {field}"
    name, kind, options = schema[field]
    if value is None:
        return None if schema is ACCOUNT_SCHEMA else f"{name}: null sent"
    ok = {
        "singleLineText": lambda v: isinstance(v, str) and "\n" not in v and "\r" not in v,
        "multilineText": lambda v: isinstance(v, str) and len(v) <= 100000,
        "singleSelect": lambda v: v in options,
        "multipleRecordLinks": lambda v: isinstance(v, list) and v and all(i in accounts for i in v),
        "url": lambda v: isinstance(v, str) and v.startswith("http"),
        "dateTime": lambda v: isinstance(v, str) and bool(DATE_RE.match(v)),
        "duration": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0,
        "number": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "percent": lambda v: isinstance(v, float) and 0 <= v <= 1,
        "multipleAttachments": lambda v: isinstance(v, list) and v and all(
            isinstance(a, dict) and str(a.get("url", "")).startswith("http") and set(a) <= {"url", "filename"} for a in v),
    }[kind]
    return None if ok(value) else f"{name} ({kind}): bad value {str(value)[:80]!r}"


class Airtable:
    """One Scraped Content table and Accounts, validating every write like Airtable."""
    def __init__(self, accounts):
        self.accounts = {a["id"]: a for a in accounts}
        self.rows, self.n = [], 0
        self.rejected, self.violations, self.full_loads, self.lookups = [], [], 0, 0

    def __call__(self, method, table, params=None, body=None):
        if method == "GET" and table == ACCOUNTS:
            return {"records": copy.deepcopy(list(self.accounts.values()))}
        if method == "GET":
            self.full_loads += 1
            return {"records": copy.deepcopy(self.rows)}
        if table == f"{CONTENT}/listRecords":
            m = re.fullmatch(r"AND\(\{%s\}='(\w+)',OR\((.*)\)\)" % PLATFORM, body["filterByFormula"])
            if not m:
                self.violations.append(f"lookup not limited to one platform: {body['filterByFormula'][:120]}")
                raise x.HttpError("HTTP 422: unexpected formula", 422)
            ids = {i.replace("\\'", "'").replace("\\\\", "\\")
                   for i in re.findall(r"\{%s\}='((?:[^'\\]|\\.)*)'" % ID, m.group(2))}
            self.lookups += 1
            fields = body["fields"]
            return {"records": [{"id": r["id"], "fields": {k: copy.deepcopy(v) for k, v in r["fields"].items() if k in fields}}
                                for r in self.rows if r["fields"][PLATFORM] == m.group(1) and r["fields"][ID] in ids]}
        assert table in (CONTENT, ACCOUNTS), table
        schema = CONTENT and SCHEMA if table == CONTENT else ACCOUNT_SCHEMA
        if len(body["records"]) > 10:
            self.violations.append(f"{len(body['records'])} records in one request")
        for r in body["records"]:
            bad = [p for f, v in r["fields"].items() if (p := problem(f, v, schema, self.accounts))]
            if bad:
                self.rejected.append(bad)
                raise x.HttpError(f"HTTP 422 INVALID_VALUE_FOR_COLUMN: {bad}", 422)
        out = []
        for r in body["records"]:
            if table == ACCOUNTS:
                self.accounts[r["id"]]["fields"].update(r["fields"])
                out.append(r)
                continue
            f = r["fields"]
            platform = f.get(PLATFORM)
            if method == "POST":
                if ID not in f or platform is None or f.get(STATUS) != "New":
                    self.violations.append(f"create without Content ID / Platform / Status New: {f}")
                if any(o["fields"][ID] == f.get(ID) and o["fields"][PLATFORM] == platform for o in self.rows):
                    self.violations.append(f"duplicate created: {platform} {f.get(ID)}")
                self.n += 1
                row = {"id": f"rec{self.n:014d}", "fields": copy.deepcopy(f), "attach_sends": 0}
                self.rows.append(row)
            else:
                row = next(o for o in self.rows if o["id"] == r["id"])
                if platform is not None and platform != row["fields"][PLATFORM]:
                    self.violations.append(f"{platform} scraper updated a {row['fields'][PLATFORM]} row {row['fields'][ID]}")
                if STATUS in f:
                    self.violations.append(f"Status sent on update for {row['fields'][ID]}")
                for a in ATTACHMENTS:
                    if a in f and row["fields"].get(a):
                        self.violations.append(f"filled attachment re-sent on {row['fields'][PLATFORM]} {row['fields'][ID]}")
                row["fields"].update(copy.deepcopy(f))
            allowed = PER_PLATFORM[row["fields"][PLATFORM]]
            extra = {SCHEMA[k][0] for k in f} - allowed
            if extra:
                self.violations.append(f"{row['fields'][PLATFORM]} wrote fields it has no data for: {extra}")
            acct = self.accounts[row["fields"][ACCOUNT][0]]["fields"]
            if acct[x.A_PLATFORM] != row["fields"][PLATFORM]:
                self.violations.append(f"{row['fields'][PLATFORM]} row linked to a {acct[x.A_PLATFORM]} account")
            out.append({"id": row["id"], "fields": copy.deepcopy(row["fields"])})
        return {"records": out}


# --- Fake platforms. Every platform uses the same ID sequence: c1, c2, ... --------------

def ids(n):
    return [f"c{i}" for i in range(1, n + 1)]


class Twitter:
    """twitterapi.io advanced_search over a list of tweets (20 per page)."""
    def __init__(self, tweets):
        self.tweets, self.returned = tweets, set()

    def __call__(self, path, params=None):
        q = params["query"]
        handles = {h.lower() for h in re.findall(r"from:(\w+)", q)}
        since, until = (int(re.search(rf"{k}:(\d+)", q).group(1)) for k in ("since_time", "until_time"))
        match = sorted((t for t in self.tweets if t["author"]["userName"].lower() in handles
                        and since <= x.parse_created(t["createdAt"]).timestamp() < until), key=lambda t: t["createdAt"])
        off = int(params.get("cursor") or 0)
        page = match[off:off + 20]
        self.returned |= {t["id"] for t in page if not t.get("quoted_tweet")}
        return {"tweets": copy.deepcopy(page), "has_next_page": bool(page), "next_cursor": str(off + 20) if page else ""}


class ScrapeCreators:
    def __init__(self, videos, posts, items):
        self.videos, self.posts, self.items = videos, posts, items
        self.returned = {"YouTube": set(), "Reddit": set(), "Instagram": set()}

    def __call__(self, path, params=None):
        now = SIM["now"]
        base = {"success": True, "credits_charged": 1, "credits_remaining": 1000}
        if path == "/v1/youtube/channel-videos":
            out = sorted((v for v in self.videos if v["at"] <= now), key=lambda v: v["at"], reverse=True)[:30]
            self.returned["YouTube"] |= {v["id"] for v in out}
            return {**base, "videos": [{k: w for k, w in v.items() if k != "at"} for v in out], "shorts": []}
        if path == "/v1/reddit/subreddit":
            span = timedelta(days=1 if params["timeframe"] == "day" else 7)
            out = sorted((p for p in self.posts if now - span <= p["at"] <= now), key=lambda p: -p["score"])[:25]
            self.returned["Reddit"] |= {p["id"] for p in out}
            return {**base, "posts": [{k: w for k, w in p.items() if k != "at"} for p in out]}
        if path == "/v2/instagram/user/posts":
            out = sorted((i for i in self.items if i["at"] <= now), key=lambda i: i["at"], reverse=True)[:12]
            self.returned["Instagram"] |= {i["code"] for i in out}
            return {**base, "items": [{k: w for k, w in i.items() if k != "at"} for i in out]}
        raise AssertionError(path)


def build_world(rnd, days):
    start = T0 - timedelta(days=12)
    tweets, videos, posts, items = [], [], [], []
    n = 0
    t = start
    while t < T0 + timedelta(days=days):  # ~6 tweets a day from one account, 1 in 4 a quote tweet
        n += 1
        t += timedelta(hours=rnd.uniform(1, 7))
        media = rnd.choice([{}, {"media": [{"type": "photo", "media_url_https": f"https://pbs.twimg.com/media/{n}.jpg"}]},
                            {"media": [{"type": "video", "video_info": {"variants": [
                                {"content_type": "video/mp4", "bitrate": 832000, "url": f"https://video.twimg.com/{n}.mp4"}]}}]}])
        tweets.append({"id": f"c{n}", "url": f"https://x.com/maker/status/c{n}", "text": f"Tweet {n} &amp; more\nsecond line",
                       "createdAt": t.strftime("%a %b %d %H:%M:%S +0000 %Y"), "viewCount": rnd.randint(0, 10**5),
                       "likeCount": 3, "replyCount": 1, "retweetCount": 0, "quoteCount": 0, "bookmarkCount": 2,
                       "isReply": False, "retweeted_tweet": None, "author": {"id": "4242", "userName": "maker"},
                       "extendedEntities": media, "quoted_tweet": {"id": "q"} if rnd.random() < 0.25 else None})
    for i, vid in enumerate(ids(40)):  # a video every ~10 hours, the newest 30 returned
        at = start + timedelta(hours=10 * i)
        videos.append({"at": at, "type": "video", "id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
                       "title": f"Video {vid}", "thumbnail": f"https://i.ytimg.com/vi/{vid}/hq720.jpg?sqp=x",
                       "description": "Full\ndescription", "publishDate": at.isoformat(), "lengthSeconds": 600 + i,
                       "viewCountInt": 1000 * i, "likeCountInt": 10 * i, "commentCountInt": i})
    hints = [("text", None), ("image", "https://i.redd.it/{}.png"), ("video", "https://v.redd.it/{}"),
             ("gallery", "https://www.reddit.com/gallery/{}"), ("link", "https://example.com/{}"), ("multi_media", None)]
    for i, pid in enumerate(ids(150)):  # ~7 posts a day
        at = start + timedelta(hours=3.4 * i)
        hint, url = rnd.choice(hints)
        posts.append({"at": at, "id": pid, "title": f"Post {pid}", "author": "someone", "selftext": "body" if url is None else "",
                      "link_flair_text": rnd.choice(["Discussion", None]), "post_hint": hint,
                      "url": url.format(pid) if url else f"https://www.reddit.com/r/ClaudeAI/comments/{pid}/p/",
                      "domain": "self.ClaudeAI" if url is None else "x", "permalink": f"/r/ClaudeAI/comments/{pid}/p/",
                      "created_utc": at.timestamp(), "created_at_iso": at.isoformat(), "score": rnd.randint(1, 5000),
                      "upvote_ratio": rnd.choice([0.5, 0.97, 1.0, 1]), "num_comments": rnd.randint(0, 300)})
    for i, code in enumerate(ids(30)):  # a post a day
        at = start + timedelta(days=i)
        kind = rnd.choice([1, 2, 8])
        img = {"candidates": [{"url": f"https://cdn.ig.com/{code}.jpg?oe=1", "width": 1080, "height": 1350}]}
        item = {"at": at, "code": code, "media_type": kind, "url": f"https://www.instagram.com/p/{code}/",
                "caption": {"text": f"Caption {code}\nmore"}, "display_uri": f"https://cdn.ig.com/{code}_t.jpg?oe=1",
                "created_at": at.isoformat(), "taken_at": int(at.timestamp()), "ig_play_count": 500 * i,
                "like_count": 40, "comment_count": 2, "image_versions2": img}
        if kind == 2:
            item.update(video_versions=[{"url": f"https://cdn.ig.com/{code}.mp4?oe=1", "width": 720, "height": 1280}],
                        video_duration=14.5)
        if kind == 8:
            item["carousel_media"] = [{"image_versions2": img}, {"image_versions2": img}]
        items.append(item)
    # Number each platform's items newest first, so the items the scrapers read share IDs
    for group, key, when in ((tweets, "id", lambda t: x.parse_created(t["createdAt"])), (videos, "id", lambda v: v["at"]),
                             (posts, "id", lambda p: p["at"]), (items, "code", lambda i: i["at"])):
        for n, it in enumerate(sorted(group, key=when, reverse=True), 1):
            it[key] = f"c{n}"
    return tweets, videos, posts, items


def account(rec, name, platform, handle, pid):
    return {"id": rec, "fields": {x.A_NAME: name, x.A_PLATFORM: platform, x.A_HANDLE: handle, x.A_PLATFORM_ID: pid,
                                  x.A_SCRAPE: "Active", x.A_LAST_STATUS: "never"}}


rd.fetch_text = lambda url, timeout=30: "<MPD><BaseURL>CMAF_720.mp4</BaseURL><BaseURL>CMAF_AUDIO_128.mp4</BaseURL></MPD>"


def simulate(seed, days=10):
    rnd = random.Random(seed)
    tweets, videos, posts, items = build_world(rnd, days)
    at = Airtable([account("recACCX0000000001", "Maker", "X", "maker", "4242"),
                   account("recACCYT000000001", "Channel", "YouTube", "channel", "UCaaaaaaaaaaaaaaaaaaaaaa"),
                   account("recACCRD000000001", "r/ClaudeAI", "Reddit", "ClaudeAI", "t5_x"),
                   account("recACCIG000000001", "Insta", "Instagram", "insta", "1")])
    tw, sc = Twitter(tweets), ScrapeCreators(videos, posts, items)
    for m in MODULES:
        m.airtable = at
    x.twitterapi = tw
    yt.scrapecreators = rd.scrapecreators = ig.scrapecreators = sc
    codes = []
    reviewed = set()
    for step in range(days * 4 + 1):
        SIM["now"] = T0 + timedelta(hours=6 * step, minutes=rnd.uniform(0, 30))
        jobs = []
        if step % 4 == 1:
            jobs += [x.main, yt.main, rd.main]
        if step % 12 == 2:
            jobs.append(ig.main)
        rnd.shuffle(jobs)
        for job in jobs:
            with contextlib.redirect_stdout(io.StringIO()):
                codes.append(job())
        for row in at.rows[::4]:  # the user reviews some rows along the way
            row["fields"][STATUS] = "Reviewed"
            reviewed.add(row["id"])
    return at, tw, sc, codes, reviewed


for seed in (1, 2, 3):
    at, tw, sc, codes, reviewed = simulate(seed)
    tag = f"[seed {seed}]"
    stored = {}
    for r in at.rows:
        stored.setdefault(r["fields"][PLATFORM], []).append(r["fields"][ID])
    check(f"{tag} every run exits 0", set(codes) == {0})
    check(f"{tag} no write rejected by the schema", at.rejected == [])
    check(f"{tag} no rule broken (duplicates, cross-platform updates, Status, attachments)", at.violations == [])
    if at.violations or at.rejected:
        print("  ", (at.violations + at.rejected)[:5])
    check(f"{tag} the table is never loaded in full", at.full_loads == 0 and at.lookups > 0)
    expected = {"X": tw.returned, **sc.returned}
    for platform, want in expected.items():
        got = stored.get(platform, [])
        check(f"{tag} {platform}: every returned item stored, once ({len(want)})", len(got) == len(set(got)) and set(got) == want
              and len(want) > 0)
    shared = set.intersection(*(set(v) for v in stored.values()))
    check(f"{tag} the same Content IDs exist on all 4 platforms as separate rows", len(shared) >= 10 and len(stored) == 4)
    check(f"{tag} Reviewed marks kept", all(r["fields"][STATUS] == "Reviewed" for r in at.rows if r["id"] in reviewed))
    check(f"{tag} every account ends ok", all(a["fields"][x.A_LAST_STATUS] == "ok" for a in at.accounts.values()))
    types_ = {}
    for r in at.rows:
        types_.setdefault(r["fields"][PLATFORM], set()).add(r["fields"].get("fld4g5cokEPmzaoVA"))
    check(f"{tag} Types per platform", types_["YouTube"] == {"Video"} and types_["X"] <= {"Text", "Photo", "Video"}
          and types_["Reddit"] <= {"Text", "Photo", "Video", "Gallery", "Link"} and types_["Instagram"] <= {"Reel", "Photo", "Carousel"}
          and None not in set().union(*types_.values()))
    check(f"{tag} every row has a Title", all(r["fields"].get("fldNkd3XzlbpdNPW6") for r in at.rows))
    check(f"{tag} every YouTube row has its thumbnail in Media", all(r["fields"].get("fld0CsTd3XkegpUEP")
          for r in at.rows if r["fields"][PLATFORM] == "YouTube"))

# Every field ID the scripts use is in the schema, and the four scripts agree on them
consts = {}
for m in MODULES:
    for k, v in vars(m).items():
        if re.fullmatch(r"[PV]_[A-Z_]+", k) and isinstance(v, str) and v.startswith("fld"):
            check(f"{m.__name__}.{k} is a Scraped Content field", v in SCHEMA)
            consts.setdefault(SCHEMA.get(v, ("?",))[0], set()).add(v)
    check(f"{m.__name__} writes to Scraped Content", getattr(m, "POSTS", getattr(m, "VIDEOS", None)) == CONTENT)
check("the scripts use one field ID per field name", all(len(v) == 1 for v in consts.values()))
check("the scripts' Platform values are options", {m.PLATFORM for m in MODULES} == {"X", "YouTube", "Reddit", "Instagram"})

print(f"{passed}/{passed + failed} checks passed")
sys.exit(1 if failed else 0)
