# Sportnet.hr RSS Feed Generator

Automatically generates an RSS 2.0 feed (`feed.xml`) from the latest headlines
on [sportnet.hr](https://sportnet.hr), on a schedule, via GitHub Actions.

## How it works

- `generate_feed.py` fetches the sportnet.hr homepage, scrapes the latest
  articles, and writes a valid RSS 2.0 feed to `feed.xml` in the repo root
  using [feedgen](https://feedgen.kiesow.be/).
- The scraper tries a few common article-listing patterns (`<article>` tags,
  common "post/news-item/card" class names, then a generic headline-link
  fallback) since it isn't tied to one exact markup shape. Any failure —
  the whole page not loading, or a single article failing to parse — is
  caught and logged so the run always finishes and writes a feed (even an
  empty one) instead of crashing.
- `.github/workflows/rss.yml` runs the script every hour (and on manual
  `workflow_dispatch`), then commits `feed.xml` back to the repo if it
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

This writes/updates `feed.xml` in the repo root.

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
   `https://<your-username>.github.io/<repo-name>/feed.xml`
5. The workflow runs automatically every hour, or you can trigger it
   manually from the **Actions** tab via **Run workflow**
   (`workflow_dispatch`).

## Subscribing

Point any RSS reader at your published `feed.xml` URL
(`https://<your-username>.github.io/<repo-name>/feed.xml`) once GitHub Pages
is live.
