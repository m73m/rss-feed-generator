#!/usr/bin/env python3
"""Generate an RSS 2.0 feed (feed.xml) from the latest headlines on sportnet.hr.

The site's markup isn't guaranteed to follow a single fixed pattern, so this
scraper tries several common article-listing shapes in order and falls back
to a generic "headline link" heuristic. Any failure while fetching the page,
or while parsing an individual article, is caught and logged so one bad item
(or a temporarily unreachable site) never stops the whole run — the feed is
still (re)written with whatever items were successfully collected.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

BASE_URL = "https://sportnet.hr"
OUTPUT_PATH = "feed.xml"
MAX_ITEMS = 30
REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (compatible; SportnetRSSBot/1.0; "
    "+https://github.com/) rss-feed-generator"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("generate_feed")


def fetch_html(url: str) -> str | None:
    """Fetch a page's HTML, returning None (and logging) on any failure."""
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        log.error("Failed to fetch %s: %s", url, exc)
        return None


def _is_probable_article_link(href: str) -> bool:
    """Filter out nav/social/anchor/asset links that aren't real articles."""
    if not href or href.startswith("#"):
        return False
    parsed = urlparse(urljoin(BASE_URL, href))
    if parsed.netloc and urlparse(BASE_URL).netloc not in parsed.netloc:
        return False
    path = parsed.path.rstrip("/")
    if not path or path.count("/") < 1:
        return False
    if path.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".svg", ".css", ".js", ".pdf")):
        return False
    return True


def _text_or_none(tag) -> str | None:
    if tag is None:
        return None
    text = tag.get_text(strip=True)
    return text or None


def _extract_from_container(container) -> dict | None:
    """Try to pull title/link/summary/image/date out of one listing item."""
    link_tag = container.find("a", href=True)
    if link_tag is None:
        return None

    href = link_tag["href"]
    if not _is_probable_article_link(href):
        return None

    title_tag = (
        container.find(["h1", "h2", "h3", "h4"])
        or container.find(class_=lambda c: c and "title" in c.lower())
        or link_tag
    )
    title = _text_or_none(title_tag) or _text_or_none(link_tag)
    if not title:
        return None

    summary_tag = container.find(
        ["p", "span"], class_=lambda c: c and any(k in c.lower() for k in ("summary", "excerpt", "desc", "lead"))
    ) or container.find("p")
    summary = _text_or_none(summary_tag)

    img_tag = container.find("img")
    image_url = None
    if img_tag is not None:
        image_url = img_tag.get("src") or img_tag.get("data-src")
        if image_url:
            image_url = urljoin(BASE_URL, image_url)

    time_tag = container.find("time")
    published = None
    if time_tag is not None:
        published = time_tag.get("datetime") or _text_or_none(time_tag)

    return {
        "title": title,
        "link": urljoin(BASE_URL, href),
        "summary": summary,
        "image": image_url,
        "published": published,
    }


def parse_articles(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    articles: list[dict] = []
    seen_links: set[str] = set()

    # Strategy 1: semantic <article> containers.
    containers = soup.find_all("article")

    # Strategy 2: common CMS class-name conventions.
    if not containers:
        containers = soup.find_all(
            ["div", "li"],
            class_=lambda c: c and any(
                k in c.lower() for k in ("article", "post", "news-item", "news__item", "card", "teaser")
            ),
        )

    # Strategy 3: generic heuristic — headline tags that wrap or contain a link.
    if not containers:
        containers = soup.find_all(["h1", "h2", "h3"])

    for container in containers:
        try:
            item = _extract_from_container(container)
        except Exception as exc:  # noqa: BLE001 - one bad item must not stop the run
            log.warning("Skipping an item due to parse error: %s", exc)
            continue

        if item is None or item["link"] in seen_links:
            continue

        seen_links.add(item["link"])
        articles.append(item)

        if len(articles) >= MAX_ITEMS:
            break

    return articles


def build_feed(articles: list[dict]) -> FeedGenerator:
    fg = FeedGenerator()
    fg.id(BASE_URL)
    fg.title("Sportnet.hr - Latest News")
    fg.link(href=BASE_URL, rel="alternate")
    fg.description("Automatically generated RSS feed of the latest headlines from sportnet.hr")
    fg.language("hr")
    fg.lastBuildDate(datetime.now(timezone.utc))

    for article in articles:
        fe = fg.add_entry()
        fe.id(article["link"])
        fe.title(article["title"])
        fe.link(href=article["link"])
        if article.get("summary"):
            fe.description(article["summary"])
        if article.get("image"):
            fe.enclosure(article["image"], 0, "image/jpeg")
        if article.get("published"):
            try:
                fe.pubDate(article["published"])
            except Exception:  # noqa: BLE001 - bad/unparseable date shouldn't break the entry
                pass

    return fg


def main() -> int:
    html = fetch_html(BASE_URL)
    articles: list[dict] = []

    if html is not None:
        try:
            articles = parse_articles(html)
        except Exception as exc:  # noqa: BLE001 - parsing must never crash the run
            log.error("Failed to parse articles: %s", exc)

    if not articles:
        log.warning("No articles found (fetch or parse failed) — writing an empty feed.")

    try:
        feed = build_feed(articles)
        feed.rss_file(OUTPUT_PATH)
        log.info("Wrote %d item(s) to %s", len(articles), OUTPUT_PATH)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to write feed file: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
