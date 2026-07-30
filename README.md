# RSS Feed Generator

Automatically generates an RSS 2.0 feed (`sportnet_feed.xml`) from the latest headlines
on [sportnet.hr](https://sportnet.hr), on a schedule, via GitHub Actions.

## How it works

- `generate_feed.py` fetches sportnet.hr's archive listing
  (`https://sportnet.hr/arhiva/?pg=N` for pages 1 through 10), scrapes the
  articles found across those pages, and writes a valid RSS 2.0 feed to
  `sportnet_feed.xml` in the repo root using [feedgen](https://feedgen.kiesow.be/).
- The scraper tries a few common article-listing patterns (`<article>` tags,
  common "post/news-item/card" class names, then a generic headline-link
  fallback) since it isn't tied to one exact markup shape. Any failure —
  a single archive page not loading, or a single article failing to parse —
  is caught and logged so the run always finishes and writes a feed (even an
  empty one) instead of crashing. Articles that appear on more than one page
  are de-duplicated by URL, and the total is capped at `MAX_ITEMS`.
- Runs are **incremental**. Before scraping, the previous `sportnet_feed.xml`
  is read and its newest entry is used as a stop marker: since both the
  archive and the feed run newest-first, reaching that entry means everything
  beyond it is already published, so the remaining pages are skipped. A
  typical scheduled run therefore fetches only page 1 instead of all ten.
  Entries from the previous feed are carried forward and appended after the
  new ones, so the back catalogue is preserved and a run that finds nothing
  new republishes the existing feed unchanged rather than emptying it. If the
  marker isn't found (e.g. it aged out of the archive), every page is scraped
  as before.
- When the item set is unchanged, the previous `lastBuildDate` is reused
  rather than restamped. That is the feed's only time-varying field, so the
  file comes out byte-identical and the workflow commits nothing — quiet
  hours leave no commit behind. A fresh build date is written as soon as the
  items actually change.
- `.github/workflows/rss.yml` runs the script every 2 hours (and on manual
  `workflow_dispatch`), then commits `sportnet_feed.xml` back to the repo if it
  changed.

**Note:** the exact selectors in `generate_feed.py` are best-effort generic
heuristics. If sportnet.hr's markup doesn't match one of the patterns tried,
inspect the live page's HTML and adjust the selectors in
`_extract_from_container` / `parse_articles` accordingly.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python generate_feed.py
```

This writes/updates `sportnet_feed.xml` in the repo root.

## Enabling the scheduled workflow + hosting the feed via GitHub Pages

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
4. Once Pages is enabled, the feed will be publicly available at:
   `https://<your-username>.github.io/<repo-name>/sportnet_feed.xml`
5. The workflow runs automatically every 2 hours, or you can trigger it
   manually from the **Actions** tab via **Run workflow**
   (`workflow_dispatch`).

## Subscribing

Point any RSS reader at your published `sportnet_feed.xml` URL
(`https://<your-username>.github.io/<repo-name>/sportnet_feed.xml`) once GitHub Pages
is live.
