# RSS Feed Generator

Automatically generates RSS 2.0 feeds from a set of configured news sources,
on a schedule, via GitHub Actions.

Each source writes its own feed file in the repo root:

| Feed file |
| --- |
| `sportnet_feed.xml` |
| `fsb_feed.xml` |
| `nike_feed.xml` |

## How it works

- `generate_feed.py` holds a `SOURCES` list. Each entry declares the pages to
  fetch, the parser to use, the output file, and the feed's title and
  description. Adding another source means appending one `Source` entry (and
  a parser, if its markup differs from the existing ones).
- Every source runs through the same pipeline: fetch its listing pages, scrape
  the articles, and write a valid RSS 2.0 feed using
  [feedgen](https://feedgen.kiesow.be/). Each item carries a title, link,
  intro text, thumbnail and publication date where the source provides them.
- Failures are contained. A page that won't load, an item that won't parse, or
  a whole source that fails leaves the other sources untouched and still
  produces a feed from whatever was collected — the run doesn't crash.
  Articles seen more than once are de-duplicated by URL and the total per feed
  is capped at `MAX_ITEMS`.
- Runs are **incremental**. Before scraping, the previous feed file is read and
  its newest entry is used as a stop marker: since both the listings and the
  feeds run newest-first, reaching that entry means everything beyond it is
  already published, so the remaining pages are skipped. A typical scheduled
  run therefore fetches a single page. Entries from the previous feed are
  carried forward and appended after the new ones, so the back catalogue is
  preserved and a run that finds nothing new republishes the existing feed
  unchanged rather than emptying it. If the marker isn't found (e.g. it aged
  out of the listing), every page is scraped as a fallback.
- When a feed's item set is unchanged, its previous `lastBuildDate` is reused
  rather than restamped. That is the only time-varying field, so the file comes
  out byte-identical and the workflow commits nothing — quiet runs leave no
  commit behind. A fresh build date is written as soon as the items change.
- `.github/workflows/rss.yml` runs the script on two schedules and commits
  any feed file that changed back to the repo: one every 2 hours for
  frequently-updated sources, one twice a day at 07:23 and 19:23 CET for
  slower-moving ones. Which sources belong to which schedule is set in the
  workflow's source-selection step. Both fire at `:23` rather than `:00` on
  purpose — GitHub delays and sometimes drops scheduled runs at the top of
  the hour, when load peaks. A manual `workflow_dispatch` runs every source.
- Which sources run is chosen with `--only NAME...` / `--exclude NAME...`;
  with neither, every source runs. An unrecognised name is a hard error, so
  a typo can't silently leave a feed unwritten. Cron is UTC-only and cannot
  follow DST, so a schedule pinned to CET lands an hour later in local time
  while Central Europe is on CEST.

**Scheduling caveat:** `schedule` triggers are best-effort on GitHub's side, so
individual runs can be late by anything from minutes to hours, or skipped
entirely. Nothing is lost when that happens — the next run simply picks up
every item published since the last one. Note also that cron only fires from
the repository's **default branch**; the schedule is ignored on every other
branch, and changing the default branch re-registers it.

**Note:** the selectors in `generate_feed.py` are matched to each source's
current markup, with generic fallbacks. If a source changes its markup, inspect
the live page's HTML — the raw served HTML, via view-source, not the
post-JavaScript DOM shown by the inspector — and adjust that source's parser
accordingly. Where a source renders part of a listing client-side, only the
fields present in the served HTML can be extracted, so some feeds carry a
title, link and image but no summary or publication date.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python generate_feed.py
```

This writes/updates every configured feed file in the repo root.

## Enabling the scheduled workflow + hosting the feeds via GitHub Pages

1. **Make the repository public.** GitHub Pages on the free plan only serves
   public repositories. Go to **Settings → General → Danger Zone → Change
   visibility** and set it to Public.
2. **Allow Actions to push commits.** Under **Settings → Actions → General →
   Workflow permissions**, select **Read and write permissions** (the
   workflow itself also requests `contents: write`, but this repo setting
   must allow it too).
3. **Enable GitHub Pages.** Go to **Settings → Pages**, and under
   **Build and deployment → Source**, choose **Deploy from a branch**, then
   pick branch `main` and folder `/ (root)`. Save.
4. Once Pages is enabled, each feed is publicly available at:
   `https://<your-username>.github.io/<repo-name>/<feed-file>` — for example
   `https://<your-username>.github.io/<repo-name>/sportnet_feed.xml`
5. The workflow runs automatically every 2 hours, or you can trigger it
   manually from the **Actions** tab via **Run workflow**
   (`workflow_dispatch`).

## Subscribing

Point any RSS reader at a published feed URL
(`https://<your-username>.github.io/<repo-name>/<feed-file>`) once GitHub Pages
is live.
