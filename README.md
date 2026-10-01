# Content OS Database scrapers

Python scripts that fill an Airtable base with posts from creators and communities on X, YouTube, Instagram and Reddit. Each script creates new posts (marked `New` for review) and updates the metrics on posts it has already stored.

- **Python 3.9+, standard library only.** Nothing to install.
- **X** uses [twitterapi.io](https://twitterapi.io). YouTube, Instagram and Reddit use the [ScrapeCreators API](https://docs.scrapecreators.com/).
- **Airtable field IDs are hard-coded** at the top of each script, for the base these scripts were built for. To use your own base, create the same tables and replace the IDs.

| Script | Reads | Writes to | Schedule |
|---|---|---|---|
| `scripts/x.py` | Accounts with Platform = X, Scrape = Active | X Posts | every 6 hours, and when an X account is added |
| `scripts/x_profile.py` | One new Accounts row (its Profile URL) | that row's X profile details | when an X account is added |
| `scripts/youtube.py` | Accounts with Platform = YouTube, Scrape = Active | YouTube Videos | each channel every 3 days (checked daily), and when a channel is added |
| `scripts/youtube_profile.py` | One new Accounts row (its Profile URL) | that row's YouTube channel details | when a channel is added |
| `scripts/instagram.py` | Instagram accounts | Instagram Posts | every 3 days |
| `scripts/reddit.py` | Accounts with Platform = Reddit, Scrape = Active | Reddit Posts | daily, and when a subreddit is added |
| `scripts/reddit_profile.py` | One new Accounts row (its Profile URL) | that row's subreddit details | when a subreddit is added |

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

## How the YouTube scraper works

- **Every 3 days per channel.** `.github/workflows/youtube-scrape.yml` runs daily at 03:37 UTC (09:07 IST) and scrapes the channels whose Last Scraped is 3 days old (or empty). Days when nothing is due cost nothing.
- **One call per channel:** ScrapeCreators `channel-videos` (`sort=latest`, `includeExtras=true`, 1 credit) returns the 30 newest regular videos. Shorts come back in a separate list and are not stored.
- **New videos:** every returned video not yet stored is created with Status `New`. A new channel's first scrape stores its 30 newest videos; later runs store whatever it has posted since.
- **Existing videos** among the 30 newest get fresh views, likes and comments; Status is kept. Older videos stop being updated once they drop out of the 30 newest.
- **Thumbnail** is saved as an image attachment (Airtable keeps its own copy), once: when a video is stored, or on the next scrape if the field is empty.
- **After a failure:** the channel is marked `error` with the reason and keeps its Last Scraped, so the next daily run retries it.
- **Cost:** 1 credit per channel every 3 days (about 10 a month per channel), plus 1 when a channel is added.

## How the Reddit scraper works

- **Daily, top of the day.** `.github/workflows/reddit-scrape.yml` runs daily at 04:07 UTC (09:37 IST). Each active subreddit gets one ScrapeCreators call (`/v1/reddit/subreddit`, `sort=top`, `timeframe=day`, 1 credit): about 25 posts from the last 24 hours.
- **New subreddits: top of the week, once.** The first scrape reads `timeframe=week`. It also reads the week when the last successful scrape is more than 36 hours old (a missed or failed day).
- **Posts** are created with Status `New`; posts seen again get a fresh score, upvote ratio and comment count, and their Status is kept.
- **Media:** images (`i.redd.it`) and Reddit videos (`v.redd.it`, best video plus its audio) are saved as attachments, once. Galleries can't be collected. If a video's playlist can't be read, the post is saved without it.
- **After a failure:** the subreddit is marked `error` and keeps its Last Scraped, so the next run catches up with the week.
- **Cost:** 1 credit per subreddit per day (about 30 a month), plus 1–2 when a subreddit is added.

## Tests

The tests are offline: twitterapi.io and Airtable are replaced with fakes, so they use no credits and need no keys.

```bash
python3 tests/test_x.py     # unit tests
python3 tests/test_x_profile.py  # new X account setup
python3 tests/test_youtube.py    # YouTube scraper and new channel setup
python3 tests/test_reddit.py     # Reddit scraper and new subreddit setup
python3 tests/fuzz_x.py     # malformed API data
python3 tests/stress_x.py   # 30-day simulations: missed tweets, duplicates, credit use
```

They also run on GitHub on every push (`.github/workflows/tests.yml`).

## Deployment (GitHub Actions)

- `.github/workflows/x-scrape.yml` runs `scripts/x.py` every 6 hours at 00:17, 06:17, 12:17 and 18:17 UTC.
- `.github/workflows/youtube-scrape.yml` runs `scripts/youtube.py` daily at 03:37 UTC; each channel is scraped every 3 days.
- `.github/workflows/reddit-scrape.yml` runs `scripts/reddit.py` daily at 04:07 UTC.

You can also start either by hand: Actions → the workflow → Run workflow. Only one run per platform happens at a time (scheduled or new-account): a run started while another is going waits for it to finish.

1. **Repository secrets** (Settings → Secrets and variables → Actions): `AIRTABLE_ACCESS_TOKEN`, `TWITTER_API` (X) and `SCRAPE_CREATORS` (X profiles, YouTube and the other scrapers).
2. **Timing:** GitHub can start scheduled runs late, or occasionally skip one, when it's busy. Nothing is lost: an X run reads from where the last successful run ended (up to 72 hours back), a YouTube channel stays due until it's scraped, and a subreddit that missed a day reads the top of the week.
3. **60-day rule:** GitHub turns off scheduled workflows in a public repository after 60 days with no activity (commits, issues, pull requests). It emails a warning first. To turn it back on, make any commit, or open Actions → the workflow → Enable workflow.
4. **Alerts:** a run that fails (exit code 1) shows red in the Actions tab, and GitHub emails the repository owner. The failing accounts are also marked `error` in Airtable, with the reason in Scrape Error.

In a public repository, the Actions run logs are public. They show account handles, tweet counts and error messages, but never the keys: GitHub masks secrets in the logs.

## New accounts (X, YouTube and Reddit)

New accounts come in through an Airtable form that fills **Profile URL** and **Platform** (e.g. `https://x.com/eptwts` + X, `https://www.youtube.com/@nicksaraev` + YouTube, or `https://www.reddit.com/r/ClaudeAI/` + Reddit). An Airtable automation then calls a webhook with the new row's record ID, which starts that platform's workflow:

| Platform | Workflow | 1. Setup | 2. First scrape |
|---|---|---|---|
| X | `x-new-account.yml` | `scripts/x_profile.py` (ScrapeCreators `/v1/twitter/profile`, 1 credit) | `x.py --new-accounts`: the last 24 hours |
| YouTube | `youtube-new-account.yml` | `scripts/youtube_profile.py` (ScrapeCreators `/v1/youtube/channel`, 1 credit) | `youtube.py --new-accounts`: the 30 newest videos |
| Reddit | `reddit-new-account.yml` | `scripts/reddit_profile.py` (ScrapeCreators `/v1/reddit/subreddit/details`, 1 credit) | `reddit.py --new-accounts`: the top of the week |

**Setup** reads the account from the Profile URL, checks it isn't already tracked, and fills Name, Platform ID, Handle, Profile URL, Avatar URL, Bio, Verified and Followers (X) or Subscribers (YouTube), then sets Scrape = Active.
- X: the username is what follows `x.com/` up to the next `/` (or `?`), without `@`; `twitter.com` links and a bare username work too. X usernames are 1–15 letters, digits or `_`.
- YouTube: `youtube.com/@handle` (with or without `/videos`), `youtube.com/channel/UC…`, and the older `/c/Name` and `/user/Name` links. A video link is not a channel.
- Reddit: the subreddit is what follows `reddit.com/r/` up to the next `/`; `old.`/`m.` links, `r/Name` and a post link work too. The details lookup needs the exact capitalization; if the link has it wrong, the right one is read from the subreddit's posts (1 more credit).

If setup fails (a wrong link, an account that's already tracked, or one that doesn't exist), the row is left with Scrape not Active, Last Scrape Status = error and the reason in Scrape Error, and the run shows red in the Actions tab. Fix the Profile URL and run the automation again (or the workflow, by hand, with the record ID). A row that already has a Platform ID is never changed, so calling the webhook twice costs nothing.

If the first scrape doesn't happen (webhook not called after setup, or the scrape job fails), nothing is lost: the next scheduled run reads the new account instead.

### The webhook

```
POST https://api.github.com/repos/sankhya-bodh/scrapping-database/actions/workflows/<workflow>/dispatches
Authorization: Bearer <GitHub token>
Accept: application/vnd.github+json
Content-Type: application/json

{"ref": "main", "inputs": {"record_id": "recXXXXXXXXXXXXXX"}}
```

`<workflow>` is `x-new-account.yml`, `youtube-new-account.yml` or `reddit-new-account.yml`. GitHub answers `204 No Content` and starts the run within a few seconds. The token is a fine-grained personal access token (GitHub → Settings → Developer settings → Fine-grained tokens) with **Actions: Read and write** on this repository.

### The Airtable automation

1. Trigger: *When a form is submitted* (the new account form), or *When a record is created* on Accounts.
2. Action: *Run a script*. Input variable `record_id` = the trigger's Airtable record ID. Secret `GITHUB_TOKEN` = the token.

The script starts the workflow for the row's Platform, and skips any other platform. The form must set Platform: add it to the form, or prefill and hide it with the form link (`…?prefill_Platform=X&hide_Platform=true`, or `prefill_Platform=YouTube` / `prefill_Platform=Reddit`).

```js
const WORKFLOWS = { X: 'x-new-account.yml', YouTube: 'youtube-new-account.yml', Reddit: 'reddit-new-account.yml' };

const { record_id } = input.config();
if (!/^rec[A-Za-z0-9]{14}$/.test(record_id)) throw new Error(`Not an Airtable record ID: ${record_id}`);

// Accounts table and its Platform field, by ID so renaming them doesn't break this.
const record = await base.getTable('tbl04PJf51XEAiA8h').selectRecordAsync(record_id, { fields: ['fldWYqll9jXSskuSC'] });
if (!record) throw new Error(`Record ${record_id} not found in Accounts`);
const platform = record.getCellValueAsString('fldWYqll9jXSskuSC');
const workflow = WORKFLOWS[platform];

if (!workflow) {
  console.log(`Skipped ${record_id}: no new-account workflow for Platform "${platform}"`);
} else {
  const response = await fetch(
    `https://api.github.com/repos/sankhya-bodh/scrapping-database/actions/workflows/${workflow}/dispatches`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${input.secret('GITHUB_TOKEN')}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ ref: 'main', inputs: { record_id } }),
    },
  );
  if (!response.ok) throw new Error(`GitHub returned ${response.status}: ${await response.text()}`);
  console.log(`Started ${workflow} for ${record_id} (${platform})`);
}
```
