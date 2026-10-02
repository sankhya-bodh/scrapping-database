"""Offline stress simulation of scripts/x.py. No network: twitterapi.io and Airtable are fakes
that behave like the real ones (20 tweets per page, a trailing empty page, 15 credits per tweet
and at least 15 per call, since_time inclusive / until_time exclusive, (from:a OR from:b) queries).

Each scenario runs x.main() on GitHub's 6-hour schedule (runs start 0-30 minutes late, and
about 3% are skipped, as GitHub's scheduler does under load) for DAYS days
over a set of accounts and checks: no missed original tweets, no duplicates, nothing read from
before a new account's last 24 hours, Reviewed marks kept, and credit usage. The webhook
scenarios also run x.main(new_accounts=True) when an account is added, as the Airtable
automation does, and check it leaves the other accounts alone.
Run: python3 tests/stress_x.py
"""
import copy, io, contextlib, os, random, re, statistics, sys
from datetime import datetime as real_dt, timezone, timedelta

ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT + "/scripts")
import x
os.environ.update(AIRTABLE_ACCESS_TOKEN="fake", TWITTER_API="fake")
x.log = lambda msg: None
x.time = __import__("types").SimpleNamespace(sleep=lambda s: None)  # retries don't wait

PAGE = 20
T0 = real_dt(2026, 10, 1, tzinfo=timezone.utc)
SIM = {"now": T0}


class SimDT(real_dt):
    @classmethod
    def now(cls, tz=None):
        n = SIM["now"]
        return n if tz else n.replace(tzinfo=None)


x.datetime = SimDT


def make_world(specs, days, rnd, quote_share=0.3, late_share=0.0, late_lag=(0.33, 3.0)):
    """specs: list of (handle, author_id, tweets_per_day). Tweets from 10 days before T0 to
    the end (so any backfill would show up). Index lag: 0-5 min, or late_lag hours for late_share."""
    tweets, n = [], 0
    for handle, aid, rate in specs:
        t = T0 - timedelta(days=10)
        end = T0 + timedelta(days=days + 1)
        while rate > 0:
            t += timedelta(days=rnd.expovariate(rate))
            if t >= end:
                break
            n += 1
            lag = timedelta(hours=rnd.uniform(*late_lag)) if rnd.random() < late_share else timedelta(minutes=rnd.uniform(0, 5))
            tweets.append({"id": str(10**15 + n), "handle": handle, "aid": aid, "created": t,
                           "indexed": t + lag, "kind": "quote" if rnd.random() < quote_share else "orig"})
    return tweets


class FakeTwitter:
    def __init__(self, world, rnd):
        self.world, self.rnd = world, rnd
        self.p_first_fail = self.p_later_fail = 0.0
        self.down = False
        self.calls = self.credits = 0
        self.max_handles = 0

    def __call__(self, path, params=None):
        q = params["query"]
        handles = {h.lower() for h in re.findall(r"from:(\w+)", q)}
        self.max_handles = max(self.max_handles, len(handles))
        since = int(re.search(r"since_time:(\d+)", q).group(1))
        until = int(re.search(r"until_time:(\d+)", q).group(1))
        assert "-filter:replies" in q and "-filter:retweets" in q
        offset = int(params["cursor"][1:]) if params.get("cursor") else 0
        if self.down or self.rnd.random() < (self.p_first_fail if offset == 0 else self.p_later_fail):
            raise x.HttpError("HTTP 500 from /twitter/tweet/advanced_search: simulated", 500)
        now = SIM["now"]
        match = sorted((t for t in self.world if t["handle"].lower() in handles
                        and since <= t["created"].timestamp() < until and t["indexed"] <= now),
                       key=lambda t: t["created"], reverse=True)
        page = match[offset:offset + PAGE]
        self.calls += 1
        self.credits += max(15, 15 * len(page))
        out = [{"id": t["id"], "url": f"https://x.com/{t['handle']}/status/{t['id']}", "text": "hi &amp; bye",
                "createdAt": t["created"].strftime("%a %b %d %H:%M:%S +0000 %Y"),
                "viewCount": self.rnd.randint(0, 10**6), "likeCount": 5, "replyCount": 1, "retweetCount": 0,
                "quoteCount": 0, "bookmarkCount": 0, "isReply": False, "retweeted_tweet": None,
                "author": {"id": t["aid"], "userName": t["handle"]}, "extendedEntities": {},
                "quoted_tweet": {"id": "q"} if t["kind"] == "quote" else None} for t in page]
        # Observed live: has_next_page is true after any non-empty page, even a short one.
        return {"tweets": out, "has_next_page": bool(page), "next_cursor": f"o{offset + PAGE}" if page else ""}


class FakeAirtable:
    def __init__(self, accounts, rnd):
        self.accounts, self.rnd, self.p_write_fail, self.down = accounts, rnd, 0.0, False
        self.rows, self.n = [], 0  # a list, so duplicates would show up
        self.full_loads = self.lookup_ids = 0

    def __call__(self, method, table, params=None, body=None):
        if method == "GET":
            if table == x.POSTS:
                self.full_loads += 1
            return {"records": copy.deepcopy(self.accounts if table == x.ACCOUNTS else self.rows)}
        if table == f"{x.POSTS}/listRecords":  # lookup of specific Tweet IDs
            m = re.fullmatch(r"AND\(\{%s\}='X',OR\((.*)\)\)" % x.P_PLATFORM, body["filterByFormula"])
            assert m, body["filterByFormula"]
            ids = set(re.findall(r"\{%s\}='(\d+)'" % x.P_CONTENT_ID, m.group(1)))
            self.lookup_ids += len(ids)
            return {"records": [copy.deepcopy(r) for r in self.rows if r["fields"].get(x.P_CONTENT_ID) in ids]}
        if table == x.ACCOUNTS:
            for r in body["records"]:
                next(a for a in self.accounts if a["id"] == r["id"])["fields"].update(r["fields"])
            return {"records": body["records"]}
        if self.down or self.rnd.random() < self.p_write_fail:
            raise x.HttpError("HTTP 503 from Airtable: simulated", 503)
        out = []
        for r in body["records"]:
            if method == "POST":
                self.n += 1
                rec = {"id": f"rec{self.n}", "fields": copy.deepcopy(r["fields"])}
                self.rows.append(rec)
            else:
                rec = next(row for row in self.rows if row["id"] == r["id"])
                rec["fields"].update(copy.deepcopy(r["fields"]))
            out.append(copy.deepcopy(rec))
        return {"records": out}


def simulate(rates, days=30, seed=1, p_first_fail=0.0, p_later_fail=0.0, p_write_fail=0.0,
             api_outage=None, airtable_outage=None, added_day=None, paused=None, late_share=0.0,
             quote_share=0.3, broken=None, webhook=False):
    """rates: tweets/day (originals + quotes) per account. added_day: {index: day added}.
    paused: (index, from_day, to_day). webhook: an account added after the start is added
    1-5 hours before a scheduled run and read right away with x.main(new_accounts=True)."""
    rnd = random.Random(seed)
    specs = [(f"acct{i}", str(5000 + i), r) for i, r in enumerate(rates)]
    world = make_world(specs, days, rnd, quote_share=quote_share, late_share=late_share)
    added_day = added_day or {}
    accounts = []
    tw = FakeTwitter(world, rnd)
    tw.p_first_fail, tw.p_later_fail = p_first_fail, p_later_fail
    at = FakeAirtable(accounts, rnd)
    at.p_write_fail = p_write_fail
    x.twitterapi, x.airtable = tw, at

    first_run_at, per_run, errors, reviewed, webhook_touched = {}, [], 0, set(), 0
    runs = days * 4 + 1
    for i in range(runs):
        day = i / 4
        SIM["now"] = T0 + timedelta(hours=6 * i, minutes=rnd.uniform(0, 30))  # GitHub schedule delay
        if 0 < i < runs - 1 and rnd.random() < 0.03:
            continue  # GitHub skipped this scheduled run
        scheduled_at, before, added = SIM["now"], tw.credits, False
        if webhook and i > 0:
            SIM["now"] = scheduled_at - timedelta(hours=rnd.uniform(1, 5))
        for j, (h, aid, _) in enumerate(specs):
            if j not in first_run_at and day >= added_day.get(j, 0):
                accounts.append({"id": f"acc{j}", "fields": {x.A_NAME: h, x.A_PLATFORM: "X", x.A_PLATFORM_ID: aid,
                                                             x.A_HANDLE: h, x.A_SCRAPE: "Active", x.A_LAST_STATUS: "never"}})
                first_run_at[j] = SIM["now"]
                added = True
        if added and SIM["now"] != scheduled_at:  # the Airtable automation starts the new-account run
            others = {a["id"]: copy.deepcopy(a["fields"]) for a in accounts if a["fields"].get(x.A_LAST_SCRAPED)}
            with contextlib.redirect_stdout(io.StringIO()):
                errors += x.main(new_accounts=True)
            webhook_touched += sum(a["fields"] != others[a["id"]] for a in accounts if a["id"] in others)
        SIM["now"] = scheduled_at
        if paused:
            j, a, b = paused
            acct = next(a_ for a_ in accounts if a_["id"] == f"acc{j}")
            acct["fields"][x.A_SCRAPE] = "Paused" if a <= day < b else "Active"
        if broken:  # (index, from_day, to_day): that account's Handle is invalid for a while
            j, a, b = broken
            acct = next(a_ for a_ in accounts if a_["id"] == f"acc{j}")
            acct["fields"][x.A_HANDLE] = "broken handle!" if a <= day < b else specs[j][0]
        tw.down = bool(api_outage and api_outage[0] <= day < api_outage[1])
        at.down = bool(airtable_outage and airtable_outage[0] <= day < airtable_outage[1])
        with contextlib.redirect_stdout(io.StringIO()):
            errors += x.main()
        per_run.append(tw.credits - before)
        for row in at.rows[::5]:  # the user reviews some tweets along the way
            row["fields"][x.P_STATUS] = "Reviewed"
            reviewed.add(row["id"])

    stored = [r["fields"][x.P_CONTENT_ID] for r in at.rows]
    stored_set = set(stored)
    overlap = timedelta(minutes=x.OVERLAP_MINUTES)
    last_run = SIM["now"]
    expected, late_missed, backfilled, pause_gap = set(), 0, 0, 0
    for t in world:
        j = int(t["handle"][4:])
        start = first_run_at[j] - timedelta(hours=x.NEW_ACCOUNT_HOURS)  # a new account's last 24h
        if t["kind"] != "orig":
            continue
        if t["created"] < start - overlap:
            backfilled += t["id"] in stored_set
            continue
        if t["created"] < start:
            continue  # within the overlap before a new account's 24h: may or may not be read
        if t["created"] >= last_run - timedelta(minutes=10):
            continue  # posted after the final run's cut-off
        if paused and j == paused[0] and T0 + timedelta(days=paused[1]) - timedelta(hours=6) <= t["created"] < T0 + timedelta(days=paused[2]) - timedelta(hours=6):
            pause_gap += t["id"] not in stored_set
            continue
        if t["indexed"] - t["created"] > overlap:
            late_missed += t["id"] not in stored_set
            continue
        expected.add(t["id"])
    missed = len(expected - stored_set)
    steady = per_run[1:]
    per_month = sum(steady) / (len(steady) / 4) * 30
    return {"missed": missed, "expected": len(expected), "late_missed": late_missed, "backfilled": backfilled,
            "pause_gap": pause_gap, "dupes": len(stored) - len(stored_set),
            "lost_review": sum(1 for r in at.rows if r["id"] in reviewed and r["fields"][x.P_STATUS] != "Reviewed"),
            "quotes": sum(1 for t in world if t["kind"] == "quote" and t["id"] in stored_set),
            "run_min": min(steady), "run_med": statistics.median(steady), "run_max": max(steady),
            "calls_per_run": tw.calls / runs, "per_month": per_month, "accounts": len(rates),
            "max_handles": tw.max_handles, "error_runs": errors, "full_loads": at.full_loads,
            "webhook_touched": webhook_touched}


FAILURES = []


def show(name, r, loss_expected=False):
    """Print one scenario; record it as failed if it lost, duplicated or backfilled tweets
    (a lost tweet is only allowed where the scenario is a known limit)."""
    if (r["dupes"] or r["backfilled"] or r["lost_review"] or r["full_loads"] or r["webhook_touched"]
            or (r["missed"] and not loss_expected)):
        FAILURES.append(name)
    pm = r["per_month"]
    print(f"{name:<46} missed {r['missed']:>3}/{r['expected']:<5} dupes {r['dupes']} backfilled {r['backfilled']} "
          f"reviewedLost {r['lost_review']} quotesStored {r['quotes']} | run {r['run_min']}-{r['run_med']:.0f}-{r['run_max']} "
          f"calls/run {r['calls_per_run']:.1f} | month {pm:>7.0f} (${pm / 100000:.2f}), per acct {pm / r['accounts']:>6.0f}"
          + (f" | late-missed {r['late_missed']}" if r["late_missed"] else "")
          + (f" | pause-gap {r['pause_gap']}" if r["pause_gap"] else ""))
    return r


if __name__ == "__main__":
    print("== Clean (GitHub schedule: runs 0-30 min late, ~3% skipped) ==")
    show("6 accounts x 3/day", simulate([3] * 6))
    show("20 accounts x 3/day (2 batches)", simulate([3] * 20))
    show("20 accounts, mixed 0.3-10/day", simulate([0.3, 1, 2, 3, 5, 8, 10, 1, 2, 3] * 2))
    show("6 accounts incl. one heavy 60/day", simulate([3, 3, 3, 3, 3, 60]))
    show("6 accounts x 3/day, no quote tweets", simulate([3] * 6, quote_share=0))
    print("\n== Failures ==")
    for seed in (1, 2, 3):
        show(f"6x3/day, 5%/5%/3% random failures s{seed}", simulate([3] * 6, seed=seed, p_first_fail=0.05, p_later_fail=0.05, p_write_fail=0.03))
    show("20x3/day, 10%/10%/5% random failures", simulate([3] * 20, p_first_fail=0.1, p_later_fail=0.1, p_write_fail=0.05))
    show("6x3/day, twitterapi.io down 2 days", simulate([3] * 6, api_outage=(10, 12)))
    show("6x3/day, Airtable down 1 day", simulate([3] * 6, airtable_outage=(5, 6)))
    show("6x3/day, twitterapi.io down 4 days (>72h cap)", simulate([3] * 6, api_outage=(10, 14)), loss_expected=True)
    print("\n== Accounts added / paused ==")
    show("6x3/day, account 5 added on day 12", simulate([3] * 6, added_day={5: 12}))
    show("6x3/day, account 5 added day 12, webhook", simulate([3] * 6, added_day={5: 12}, webhook=True))
    show("20x3/day, 10 added over days 3-20, webhook", simulate([3] * 20, added_day={j: 3 + (j - 10) * 1.75 for j in range(10, 20)}, webhook=True))
    show("20x3/day, added via webhook, 10% failures", simulate([3] * 20, added_day={j: 3 + (j - 10) * 1.75 for j in range(10, 20)},
                                                              webhook=True, p_first_fail=0.1, p_later_fail=0.1, p_write_fail=0.05))
    show("6x3/day, account 2 paused days 5-15", simulate([3] * 6, paused=(2, 5, 15)))
    show("6x3/day, account 2 paused days 5-6 (1 day)", simulate([3] * 6, paused=(2, 5, 6)))
    show("6x3/day, one account broken 2 days (grouping)", simulate([3] * 6, broken=(3, 10, 12)))
    show("20x10/day, twitterapi.io down 60h (big catch-up)", simulate([10] * 20, api_outage=(10, 12.5)))
    print("\n== Index lag ==")
    r = show("6x3/day, 10% of tweets indexed 20min-3h late", simulate([3] * 6, late_share=0.1))
    print(f"\nScraped Content table fully loaded: {r['full_loads']} times (only returned Tweet IDs are looked up)")
    print(f"{'All scenarios OK' if not FAILURES else 'FAILED: ' + ', '.join(FAILURES)}")
    sys.exit(1 if FAILURES else 0)
