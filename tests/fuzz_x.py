import copy, json, os, sys, random, io, contextlib
ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT + "/scripts")
import x
os.environ.update(AIRTABLE_ACCESS_TOKEN="fake", TWITTER_API="fake")
x.log = lambda m: None
x.time = __import__('types').SimpleNamespace(sleep=lambda s: None)
SAMPLE = json.load(open(ROOT + "/tests/fixtures/advanced_search_sample.json"))
BASE = [t for t in SAMPLE["tweets"] if not t.get("quoted_tweet")][0]
ACC = {"id": "recACC", "fields": {x.A_NAME: "Example", x.A_PLATFORM: "X", x.A_PLATFORM_ID: "1000000000000000001",
                                  x.A_HANDLE: "example_user", x.A_SCRAPE: "Active"}}

mutations = {
    "extendedEntities list": ("extendedEntities", []),
    "extendedEntities None": ("extendedEntities", None),
    "media not list": ("extendedEntities", {"media": {"type": "photo"}}),
    "media item None": ("extendedEntities", {"media": [None]}),
    "media item str": ("extendedEntities", {"media": ["x"]}),
    "photo no url": ("extendedEntities", {"media": [{"type": "photo"}]}),
    "video variants None": ("extendedEntities", {"media": [{"type": "video", "video_info": None}]}),
    "variant bitrate str": ("extendedEntities", {"media": [{"type": "video", "video_info": {"variants": [{"content_type": "video/mp4", "url": "u", "bitrate": "high"}, {"content_type": "video/mp4", "url": "v", "bitrate": 5}]}}]}),
    "variants not list": ("extendedEntities", {"media": [{"type": "video", "video_info": {"variants": "x"}}]}),
    "createdAt garbage": ("createdAt", "yesterday"),
    "createdAt None": ("createdAt", None),
    "createdAt int": ("createdAt", 1727000000),
    "viewCount str": ("viewCount", "1234"),
    "viewCount 1.2K": ("viewCount", "1.2K"),
    "viewCount None": ("viewCount", None),
    "viewCount dict": ("viewCount", {"a": 1}),
    "likeCount negative": ("likeCount", -1),
    "text None": ("text", None),
    "text int": ("text", 42),
    "text huge": ("text", "a" * 200000),
    "id int": ("id", 2000000000000000002),
    "url None": ("url", None),
    "author None": ("author", None),
    "author str": ("author", "example_user"),
    "isReply str": ("isReply", "false"),
    "quoted_tweet {}": ("quoted_tweet", {}),
}
page_mutations = {
    "tweets None": {"tweets": None, "has_next_page": False},
    "tweets missing": {"has_next_page": False},
    "tweets dict": {"tweets": {"a": 1}, "has_next_page": False},
    "tweet item None": {"tweets": [None], "has_next_page": False},
    "tweet item str": {"tweets": ["x"], "has_next_page": False},
    "response list": [],
    "status error": {"status": "error", "msg": "bad"},
}

class FakeAT:
    def __init__(self): self.writes = []
    def __call__(self, method, table, params=None, body=None):
        if method == "GET":
            return {"records": [copy.deepcopy(ACC)] if table == x.ACCOUNTS else []}
        if table.endswith("/listRecords"):  # Tweet ID lookup: nothing stored yet
            return {"records": []}
        self.writes.append((table, copy.deepcopy(body)))
        return {"records": [{"id": f"rec{i}", "fields": r["fields"]} for i, r in enumerate(body["records"])]}

def run(pages):
    it = iter(pages)
    x.twitterapi = lambda path, params=None: copy.deepcopy(next(it, {"tweets": [], "has_next_page": False}))
    at = FakeAT(); x.airtable = at
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            code = x.main()
    except Exception as e:
        return "CRASH", f"{type(e).__name__}: {e}", at
    acc = [b for t, b in at.writes if t == x.ACCOUNTS][0]["records"][0]["fields"]
    posts = [r for t, b in at.writes if t == x.POSTS for r in b["records"]]
    return acc[x.A_LAST_STATUS], (acc[x.A_SCRAPE_ERROR] or "")[:110], posts

good = copy.deepcopy(BASE); good["id"] = "999"
bad_results = []
for name, (k, v) in mutations.items():
    t = copy.deepcopy(BASE); t[k] = v
    status, err, posts = run([{"tweets": [t, good], "has_next_page": False}])
    good_saved = isinstance(posts, list) and any(p["fields"].get(x.P_TWEET_ID) == "999" for p in posts)
    # Without author info a tweet can't be matched to an account: skipped + flagged is correct.
    expect = "error" if name in ("author None", "author str") else "ok"
    ok = status == expect and good_saved
    print(f"{'OK ' if ok else 'BAD'} {name:<24} status={status:<5} good_tweet_saved={good_saved} {err}")
    if not ok: bad_results.append(name)
for name, page in page_mutations.items():
    status, err, posts = run([page])
    print(f"--- page: {name:<18} status={status:<5} {err}")
print(f"{len(mutations) - len(bad_results)}/{len(mutations)} malformed-tweet cases OK")
sys.exit(1 if bad_results else 0)
