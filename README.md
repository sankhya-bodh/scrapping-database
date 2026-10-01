# Content OS Database scrapers

Python scripts that fill an Airtable base with posts from creators and communities on X, YouTube, Instagram and Reddit. Each script creates new posts (marked `New` for review) and updates the metrics on posts it has already stored.

- **Python 3.9+, standard library only.** Nothing to install.
- **X** uses [twitterapi.io](https://twitterapi.io). YouTube, Instagram and Reddit use the [ScrapeCreators API](https://docs.scrapecreators.com/).
- **Airtable field IDs are hard-coded** at the top of each script, for the base these scripts were built for. To use your own base, create the same tables and replace the IDs.

| Script | Reads | Writes to | Schedule |
|---|---|---|---|
| `scripts/x.py` | Accounts with Platform = X, Scrape = Active | X Posts | every 6 hours, and when an X account is added |
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

`python3 scripts/x.py --new-accounts` reads only the accounts that have no Last Scraped yet (see [New X accounts](#new-x-accounts)).

It prints a summary per account and the estimated credits used. It exits with code 1 if any account failed, so a scheduler can alert on it.

## How the X scraper works

- **Every 6 hours, the last 8 hours.** Each scheduled run reads the tweets posted in the 8 hours before it starts. The 2 hours of overlap cover runs GitHub starts late and tweets X's search shows late.
- **New accounts: the last 24 hours, once.** An account with no Last Scraped (or one paused for more than 72 hours) is read from 24 hours back, no further, in its own query. That happens straight away when the Airtable automation starts the [new-account run](#new-x-accounts), or else at the next scheduled run.
- **After a failure:** the next run re-reads the missed time, from where the last successful run ended (the account's Last Scraped) minus 15 minutes, up to 72 hours back. If a new account's first read fails, its Last Scraped is set to the start of its 24 hours, so the next run still reads all of them.
- **Batched:** up to 15 accounts per query, `(from:a OR from:b …) since_time:… until_time:… -filter:replies -filter:retweets`, following [twitterapi.io's monitoring guide](https://twitterapi.io/blog/how-to-monitor-twitter-accounts-for-new-tweets-in-real-time).
- **What it keeps:** original tweets only. Replies and retweets are filtered out in the query; quote tweets are dropped after.
- **Metrics:** likes, views and other counts are captured when a tweet is 0–6 hours old, and updated once more if it's 6–8 hours old at the next run. A tweet seen again is updated, never duplicated, and its Status (e.g. Reviewed) is kept.
- **Cost:** twitterapi.io charges about 15 credits per tweet (1M credits = $10). Example: 20 accounts posting 3 times a day ≈ 41k credits (about $0.41) a month. The 2-hour overlap costs about 25% more than reading only 6 hours. A new account's first 24 hours cost its tweets from that day, once.

## Tests

The tests are offline: twitterapi.io and Airtable are replaced with fakes, so they use no credits and need no keys.

```bash
python3 tests/test_x.py     # unit tests
python3 tests/fuzz_x.py     # malformed API data
python3 tests/stress_x.py   # 30-day simulations: missed tweets, duplicates, credit use
```

They also run on GitHub on every push (`.github/workflows/tests.yml`).

## Deployment (X scraper on GitHub Actions)

`.github/workflows/x-scrape.yml` runs `scripts/x.py` on GitHub's own schedule, every 6 hours at 00:17, 06:17, 12:17 and 18:17 UTC. You can also start it by hand: Actions → X scrape → Run workflow. Only one X run happens at a time (scheduled or new-account): a run started while another is going waits for it to finish.

1. **Repository secrets** (Settings → Secrets and variables → Actions): `AIRTABLE_ACCESS_TOKEN` and `TWITTER_API`. `SCRAPE_CREATORS` is there for the other scrapers.
2. **Timing:** GitHub can start scheduled runs late, or occasionally skip one, when it's busy. No tweets are lost: each run reads from where the last successful run ended, up to 72 hours back.
3. **60-day rule:** GitHub turns off scheduled workflows in a public repository after 60 days with no activity (commits, issues, pull requests). It emails a warning first. To turn it back on, make any commit, or open Actions → X scrape → Enable workflow.
4. **Alerts:** a run that fails (exit code 1) shows red in the Actions tab, and GitHub emails the repository owner. The failing accounts are also marked `error` in Airtable, with the reason in Scrape Error.

In a public repository, the Actions run logs are public. They show account handles, tweet counts and error messages, but never the keys: GitHub masks secrets in the logs.

## New X accounts

`.github/workflows/x-new-account.yml` runs `python3 scripts/x.py --new-accounts`. It reads only the active X accounts with no Last Scraped yet: their last 24 hours of tweets go into X Posts, and their Last Scraped, Last Scrape Status and Scrape Error are set. Other accounts are not touched. If there are no new accounts it makes no API calls.

It's started by a webhook call to GitHub's API:

```
POST https://api.github.com/repos/sankhya-bodh/scrapping-database/actions/workflows/x-new-account.yml/dispatches
Authorization: Bearer <GitHub token>
Accept: application/vnd.github+json
Content-Type: application/json

{"ref": "main"}
```

GitHub answers `204 No Content` and starts the run within a few seconds. No record ID is needed: each run reads every new account, so two accounts added together are both read even if GitHub merges the two runs.

**GitHub token:** a fine-grained personal access token (GitHub → Settings → Developer settings → Fine-grained tokens), repository access *Only select repositories* → `scrapping-database`, permission **Actions: Read and write**. Nothing else.

**Airtable automation:**

1. Trigger: *When a record matches conditions*, table Accounts: Platform is X, Scrape is Active, Handle is not empty. (Set Scrape to Active last, once the Handle is right.)
2. Action: *Run a script*. Add a secret named `GITHUB_TOKEN` with the token, and use:

```js
const response = await fetch(
  'https://api.github.com/repos/sankhya-bodh/scrapping-database/actions/workflows/x-new-account.yml/dispatches',
  {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${input.secret('GITHUB_TOKEN')}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ ref: 'main' }),
  },
);
if (!response.ok) throw new Error(`GitHub returned ${response.status}: ${await response.text()}`);
```

If the webhook isn't called, or its run fails, nothing is lost: the next scheduled run reads the new account's last 24 hours instead.
