# Content OS Database scrapers

Python scripts that fill an Airtable base with posts from creators and communities on X, YouTube, Instagram and Reddit. Each script creates new posts (marked `New` for review) and updates the metrics on posts it has already stored.

- **Python 3.9+, standard library only.** Nothing to install.
- **X** uses [twitterapi.io](https://twitterapi.io). YouTube, Instagram and Reddit use the [ScrapeCreators API](https://docs.scrapecreators.com/).
- **Airtable field IDs are hard-coded** at the top of each script, for the base these scripts were built for. To use your own base, create the same tables and replace the IDs.

| Script | Reads | Writes to | Schedule |
|---|---|---|---|
| `scripts/x.py` | Accounts with Platform = X, Scrape = Active | X Posts | every 6 hours |
| `scripts/youtube.py` | YouTube channels | YouTube Videos | every 3 days |
| `scripts/instagram.py` | Instagram accounts | Instagram Posts | every 3 days |
| `scripts/reddit.py` | Subreddits | Reddit Posts | weekly |

## Setup

Put the keys in a `.env` file in the project folder. `.env` is git-ignored and never committed.

```
AIRTABLE_ACCESS_TOKEN=...
TWITTER_API=...          # twitterapi.io key (X)
SCRAPE_CREATORS=...      # ScrapeCreators key (YouTube, Instagram, Reddit)
```

If there's no `.env`, the scripts read the same names from environment variables instead (that's how GitHub Actions runs them).

Run a script:

```bash
python3 scripts/x.py
```

It prints a summary per account and the estimated credits used. It exits with code 1 if any account failed, so a scheduler can alert on it.

## How the X scraper works

- **Only new tweets, no backfill.** Each run reads the tweets posted since the previous run, which is the last 6 hours on the schedule. A newly added account starts from the next run.
- **The window:** it ends at the moment the run starts. It starts where the last successful run ended (the account's Last Scraped), minus 15 minutes for tweets X's search shows late.
- **After a failure:** the next run re-reads the missed time, up to 72 hours back.
- **Batched:** up to 15 accounts per query, `(from:a OR from:b …) since_time:… until_time:… -filter:replies -filter:retweets`, following [twitterapi.io's monitoring guide](https://twitterapi.io/blog/how-to-monitor-twitter-accounts-for-new-tweets-in-real-time).
- **What it keeps:** original tweets only. Replies and retweets are filtered out in the query; quote tweets are dropped after.
- **Metrics:** likes, views and other counts are captured once, when a tweet is 0–6 hours old.
- **Cost:** twitterapi.io charges about 15 credits per tweet (1M credits = $10). Example: 20 accounts posting 3 times a day ≈ 32k credits (about $0.32) a month.

## Tests

The tests are offline: twitterapi.io and Airtable are replaced with fakes, so they use no credits and need no keys.

```bash
python3 tests/test_x.py     # unit tests
python3 tests/fuzz_x.py     # malformed API data
python3 tests/stress_x.py   # 30-day simulations: missed tweets, duplicates, credit use
```

They also run on GitHub on every push (`.github/workflows/tests.yml`).

## Deployment (X scraper on GitHub Actions)

`.github/workflows/x-scrape.yml` runs `scripts/x.py`. Make.com starts it every 6 hours through GitHub's API. Only one run happens at a time: a run started while another is going waits for it to finish.

1. **Repository secrets** (Settings → Secrets and variables → Actions): add `AIRTABLE_ACCESS_TOKEN` and `TWITTER_API`.
2. **A GitHub token for Make.com.** Create a fine-grained personal access token with:
   - access to this repository only;
   - the permission **Actions: Read and write**.
3. **Make.com scenario.** Add a Schedule trigger set to every 6 hours, followed by an HTTP "Make a request" module:
   - Method: `POST`
   - URL: `https://api.github.com/repos/<owner>/<repo>/actions/workflows/x-scrape.yml/dispatches`
   - Headers: `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`
   - Body (JSON): `{"ref": "main"}`
   - GitHub answers `204 No Content` when the run has started.
4. **Alerts.** A run that fails (exit code 1) shows red in the Actions tab, and GitHub emails the token's owner. The failing accounts are also marked `error` in Airtable, with the reason in Scrape Error.

In a public repository, the Actions run logs are public. They show account handles, tweet counts and error messages, but never the keys: GitHub masks secrets in the logs.
