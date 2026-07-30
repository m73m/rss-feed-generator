#!/usr/bin/env python3
"""Generate an RSS 2.0 feed (sportnet_feed.xml) from sportnet.hr's archive listing.

The site's markup isn't guaranteed to follow a single fixed pattern, so this
scraper tries several common article-listing shapes in order and falls back
to a generic "headline link" heuristic. Any failure while fetching a page,
or while parsing an individual article, is caught and logged so one bad item
(or a temporarily unreachable page) never stops the whole run — the feed is
still (re)written with whatever items were successfully collected.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

try:  # stdlib on 3.9+, but needs system tzdata — fall back rather than crash
    from zoneinfo import ZoneInfo

    SITE_TZ = ZoneInfo("Europe/Zagreb")
except Exception:  # noqa: BLE001 - a missing tz database must not break the run
    SITE_TZ = timezone.utc

# Listing items date themselves in Croatian d.m.Y. form ("29.07.2026."),
# sometimes with a trailing time.
DATE_RE = re.compile(
    r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\.?(?:\s+(\d{1,2}):(\d{2}))?"
)

BASE_URL = "https://sportnet.hr"
ARCHIVE_URL_TEMPLATE = "https://sportnet.hr/arhiva/?pg={page}"
ARCHIVE_PAGES = range(1, 11)
OUTPUT_PATH = "sportnet_feed.xml"
MAX_ITEMS = 200
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
    """Filter out nav/category/social/asset links that aren't real articles.

    sportnet.hr article URLs always end in a numeric article ID
    (e.g. .../629732/), while section/category pages (e.g. /nogomet/,
    /kosarka/) don't — that's a much stronger signal than path depth for
    telling the two apart, since a category link can otherwise look just
    like a real one.
    """
    if not href or href.startswith("#"):
        return False
    parsed = urlparse(urljoin(BASE_URL, href))
    if parsed.netloc and urlparse(BASE_URL).netloc not in parsed.netloc:
        return False
    path = parsed.path.rstrip("/")
    if not path:
        return False
    last_segment = path.rsplit("/", 1)[-1]
    if not last_segment.isdigit():
        return False
    return True


def _text_or_none(tag) -> str | None:
    if tag is None:
        return None
    text = tag.get_text(strip=True)
    return text or None


def _looks_like_date(text: str) -> bool:
    """True when a heading holds nothing but a date, so date headings never
    get mistaken for the item's title (a short headline could otherwise lose
    the longest-candidate tie-break to '29.07.2026.').
    """
    return bool(DATE_RE.fullmatch(text.strip()))


def _parse_date(text: str | None) -> datetime | None:
    """Parse sportnet.hr's d.m.Y. listing date into an aware datetime."""
    if not text:
        return None
    match = DATE_RE.search(text)
    if match is None:
        return None
    day, month, year, hour, minute = match.groups()
    try:
        return datetime(
            int(year), int(month), int(day),
            int(hour or 0), int(minute or 0),
            tzinfo=SITE_TZ,
        )
    except ValueError:  # e.g. an impossible day/month combination
        return None


def _looks_like_byline(text: str) -> bool:
    """Bylines/credit lines (e.g. 'Piše: Petar Jenjić', 'Foto: ...') sometimes
    sit in a heading tag ahead of the real headline within the same listing
    item; treat them as non-title text rather than let them win as the title.
    """
    normalized = text.strip().lower()
    return normalized.startswith(("piše", "pise", "autor", "foto:", "video:"))


def _extract_from_container(container) -> dict | None:
    """Try to pull title/link/summary/image/date out of one listing item."""
    link_tag = container.find("a", href=True)
    if link_tag is None:
        return None

    href = link_tag["href"]
    if not _is_probable_article_link(href):
        return None

    # Some listing containers (e.g. a whole section block) wrap several
    # teaser links at once, each with its own heading. Scoping the heading
    # search to the specific link's own descendants first avoids picking up
    # a sibling teaser's headline instead of this link's actual title; the
    # wider container is only searched as a fallback for markup where the
    # title sits next to the link rather than inside it.
    def _heading_candidates(scope) -> list[str]:
        candidates = []
        for heading in scope.find_all(["h1", "h2", "h3", "h4"]):
            text = _text_or_none(heading)
            if text and not _looks_like_byline(text) and not _looks_like_date(text):
                candidates.append(text)
        title_class_text = _text_or_none(scope.find(class_=lambda c: c and "title" in c.lower()))
        if title_class_text and not _looks_like_byline(title_class_text):
            candidates.append(title_class_text)
        return candidates

    heading_candidates = _heading_candidates(link_tag) or _heading_candidates(container)
    title = max(heading_candidates, key=len) if heading_candidates else None

    if not title:
        link_text = _text_or_none(link_tag)
        if link_text and not _looks_like_byline(link_text):
            title = link_text

    if not title:
        return None

    # sportnet.hr wraps each listing item's intro text in <div class="uvod">.
    uvod_div = container.find("div", class_="uvod")
    if uvod_div is not None:
        summary = _text_or_none(uvod_div)
    else:
        summary_tag = container.find(
            ["p", "span"], class_=lambda c: c and any(k in c.lower() for k in ("summary", "excerpt", "desc", "lead"))
        ) or container.find("p")
        summary = _text_or_none(summary_tag)

    # sportnet.hr wraps each listing item's thumbnail in <div class="img"><a><img></a></div>.
    img_div = container.find("div", class_="img")
    img_tag = img_div.find("img") if img_div is not None else container.find("img")
    image_url = None
    if img_tag is not None:
        image_url = img_tag.get("src") or img_tag.get("data-src")
        if image_url:
            image_url = urljoin(BASE_URL, image_url)

    # sportnet.hr puts each listing item's post date in an <h4> ("29.07.2026.").
    published = None
    for heading in container.find_all("h4"):
        published = _parse_date(_text_or_none(heading))
        if published is not None:
            break

    if published is None:
        time_tag = container.find("time")
        if time_tag is not None:
            # Left as a raw string for feedgen/dateutil to interpret, since a
            # <time datetime> carries ISO form rather than the d.m.Y. above.
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

    # Strategy 1: sportnet.hr's real listing shape — a bare, unclassed <li>
    # wrapping a div.img thumbnail and/or a div.uvod intro alongside the
    # title link. This has to come first since these <li> elements have no
    # class of their own, so the later class-name and <article> strategies
    # never see them and would otherwise fall all the way back to bare
    # heading tags, which are too narrow to include the img/uvod siblings.
    containers = [
        li for li in soup.find_all("li")
        if li.find("div", class_="img") is not None or li.find("div", class_="uvod") is not None
    ]

    # Strategy 2: semantic <article> containers.
    if not containers:
        containers = soup.find_all("article")

    # Strategy 3: common CMS class-name conventions.
    if not containers:
        containers = soup.find_all(
            ["div", "li"],
            class_=lambda c: c and any(
                k in c.lower() for k in ("article", "post", "news-item", "news__item", "card", "teaser")
            ),
        )

    # Strategy 4: generic heuristic — headline tags that wrap or contain a link.
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

    return articles


def fetch_all_articles() -> list[dict]:
    """Fetch and parse every archive page, merging results with a global cap
    and de-duplicating articles that show up on more than one page.
    """
    articles: list[dict] = []
    seen_links: set[str] = set()

    for page in ARCHIVE_PAGES:
        if len(articles) >= MAX_ITEMS:
            break

        url = ARCHIVE_URL_TEMPLATE.format(page=page)
        html = fetch_html(url)
        if html is None:
            continue

        try:
            page_articles = parse_articles(html)
        except Exception as exc:  # noqa: BLE001 - one bad page must not stop the run
            log.error("Failed to parse articles from %s: %s", url, exc)
            continue

        for article in page_articles:
            if article["link"] in seen_links:
                continue
            seen_links.add(article["link"])
            articles.append(article)
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
        # feedgen prepends by default, which would reverse our newest-first
        # scrape order and emit the feed oldest-first; append preserves it.
        fe = fg.add_entry(order="append")
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
    try:
        articles = fetch_all_articles()
    except Exception as exc:  # noqa: BLE001 - one bad page must never crash the whole run
        log.error("Failed to fetch articles: %s", exc)
        articles = []

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
