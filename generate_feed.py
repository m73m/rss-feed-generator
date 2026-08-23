#!/usr/bin/env python3
"""Generate an RSS 2.0 feed per configured news source.

Each source declares its listing pages, its own parser, and the file it
writes. Runs are incremental: the previously written feed is read, its newest
entry is used as a marker to stop scraping at, and its entries are carried
forward so the back catalogue survives.

Every failure is contained — a page that will not load, an item that will not
parse, or a source that fails outright leaves the other sources unaffected and
still produces a feed from whatever was collected.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from typing import Callable
from urllib.parse import unquote, urljoin, urlparse, urlsplit, urlunsplit
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

try:  # stdlib on 3.9+, but needs system tzdata — fall back rather than crash
    from zoneinfo import ZoneInfo

    SITE_TZ = ZoneInfo("Europe/Zagreb")
except Exception:  # noqa: BLE001 - a missing tz database must not break the run
    SITE_TZ = timezone.utc

# Croatian d.m.Y. dates ("29.07.2026."), sometimes with a trailing time.
DATE_RE = re.compile(
    r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\.?(?:\s+(\d{1,2}):(\d{2}))?"
)
# ISO dates carried in a query string, e.g. "...&d=2026-07-29".
ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

MAX_ITEMS = 200
REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (compatible; NewsRSSBot/1.0; "
    "+https://github.com/) rss-feed-generator"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("generate_feed")


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

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


def _text_or_none(tag) -> str | None:
    if tag is None:
        return None
    text = tag.get_text(strip=True)
    return text or None


def _joined_text(tag) -> str | None:
    """Text of a tag whose children are interleaved with markup, joined on
    spaces so neighbouring runs of text don't get glued together.
    """
    if tag is None:
        return None
    text = " ".join(tag.get_text(" ", strip=True).split())
    return text or None


def _looks_like_date(text: str) -> bool:
    """True when a heading holds nothing but a date, so date headings never
    get mistaken for the item's title (a short headline could otherwise lose
    the longest-candidate tie-break to '29.07.2026.').
    """
    return bool(DATE_RE.fullmatch(text.strip()))


def _parse_date(text: str | None) -> datetime | None:
    """Parse a d.m.Y. listing date into an aware datetime."""
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


def _parse_iso_date(text: str | None) -> datetime | None:
    """Pull a YYYY-MM-DD date out of a string into an aware datetime."""
    if not text:
        return None
    match = ISO_DATE_RE.search(text)
    if match is None:
        return None
    year, month, day = match.groups()
    try:
        return datetime(int(year), int(month), int(day), tzinfo=SITE_TZ)
    except ValueError:
        return None


def _looks_like_byline(text: str) -> bool:
    """Bylines/credit lines (e.g. 'Piše: Petar Jenjić', 'Foto: ...') sometimes
    sit in a heading tag ahead of the real headline within the same listing
    item; treat them as non-title text rather than let them win as the title.
    """
    normalized = text.strip().lower()
    return normalized.startswith(("piše", "pise", "autor", "foto:", "video:"))


# --------------------------------------------------------------------------
# Source: archive listing with <li> items (div.img / div.uvod / h4 date)
# --------------------------------------------------------------------------

def _is_numeric_id_link(href: str, base_url: str) -> bool:
    """Filter out nav/category/social/asset links that aren't real articles.

    Article URLs on this source always end in a numeric article ID
    (e.g. .../629732/), while section/category pages (e.g. /nogomet/,
    /kosarka/) don't — that's a much stronger signal than path depth for
    telling the two apart, since a category link can otherwise look just
    like a real one.
    """
    if not href or href.startswith("#"):
        return False
    parsed = urlparse(urljoin(base_url, href))
    if parsed.netloc and urlparse(base_url).netloc not in parsed.netloc:
        return False
    path = parsed.path.rstrip("/")
    if not path:
        return False
    last_segment = path.rsplit("/", 1)[-1]
    if not last_segment.isdigit():
        return False
    return True


def _extract_listing_item(container, base_url: str) -> dict | None:
    """Try to pull title/link/summary/image/date out of one listing item."""
    link_tag = container.find("a", href=True)
    if link_tag is None:
        return None

    href = link_tag["href"]
    if not _is_numeric_id_link(href, base_url):
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

    # Each listing item's intro text sits in <div class="uvod">.
    uvod_div = container.find("div", class_="uvod")
    if uvod_div is not None:
        summary = _text_or_none(uvod_div)
    else:
        summary_tag = container.find(
            ["p", "span"], class_=lambda c: c and any(k in c.lower() for k in ("summary", "excerpt", "desc", "lead"))
        ) or container.find("p")
        summary = _text_or_none(summary_tag)

    # Each listing item's thumbnail sits in <div class="img"><a><img></a></div>.
    img_div = container.find("div", class_="img")
    img_tag = img_div.find("img") if img_div is not None else container.find("img")
    image_url = None
    if img_tag is not None:
        image_url = img_tag.get("src") or img_tag.get("data-src")
        if image_url:
            image_url = urljoin(base_url, image_url)

    # Each listing item's post date sits in an <h4> ("29.07.2026.").
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
        "link": urljoin(base_url, href),
        "summary": summary,
        "image": image_url,
        "published": published,
    }


def parse_listing(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")

    # Strategy 1: the real listing shape — a bare, unclassed <li> wrapping a
    # div.img thumbnail and/or a div.uvod intro alongside the title link.
    # This has to come first since these <li> elements have no class of their
    # own, so the later class-name and <article> strategies never see them and
    # would otherwise fall all the way back to bare heading tags, which are
    # too narrow to include the img/uvod siblings.
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

    return _collect(containers, base_url, _extract_listing_item)


# --------------------------------------------------------------------------
# Source: news board with <div class="vijest"> items
# --------------------------------------------------------------------------

def _extract_vijest_item(container, base_url: str) -> dict | None:
    """Pull title/link/summary/image/date out of one div.vijest item.

    Links are query-string based (`?...&id=41231`), so an article is
    identified by carrying a numeric `id` rather than by its path.
    """
    heading_link = container.select_one("h3 a[href]")
    if heading_link is None:
        return None

    href = heading_link["href"]
    if not re.search(r"[?&]id=\d+", href):
        return None

    title = _text_or_none(heading_link)
    if not title or _looks_like_byline(title):
        return None

    # The intro text shares div.vijestTekst with the thumbnail's anchor, so
    # the image has to be pulled out before reading the text.
    text_div = container.find("div", class_="vijestTekst")

    image_url = None
    img_tag = container.find("img", class_=lambda c: c and "vijestSlika" in c)
    if img_tag is None:
        img_tag = container.find("img")
    if img_tag is not None:
        src = img_tag.get("src") or img_tag.get("data-src")
        if src:
            image_url = urljoin(base_url, src)

    summary = None
    if text_div is not None:
        # Drop the thumbnail anchor so its markup can't bleed into the text.
        text_copy = BeautifulSoup(str(text_div), "lxml")
        for anchor in text_copy.find_all("a"):
            anchor.decompose()
        summary = _joined_text(text_copy)

    # The date is carried machine-readably in the datum attribute
    # ("...&d=2026-07-29"), which beats parsing the Croatian month name shown
    # to readers. The visible text is only a fallback.
    date_div = container.find("div", class_=lambda c: c and "linkDatum" in c)
    published = None
    if date_div is not None:
        published = _parse_iso_date(date_div.get("datum")) or _parse_date(_text_or_none(date_div))

    return {
        "title": title,
        "link": urljoin(base_url, href),
        "summary": summary,
        "image": image_url,
        "published": published,
    }


def parse_vijesti(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    containers = soup.find_all("div", class_="vijest")
    return _collect(containers, base_url, _extract_vijest_item)


# --------------------------------------------------------------------------
# Source: product grid with <a data-qa="product-card-link"> cards
# --------------------------------------------------------------------------

# The separator before the label is optional: most cards use an en-dash, but
# some carry the label with only whitespace in front of it.
LAUNCH_LABEL_RE = re.compile(
    r"\s*(?:[–—-]\s*)?(?:erscheinungsdatum|release date|launch date)\s*$",
    re.IGNORECASE,
)


def _clean_product_title(text: str | None) -> str | None:
    """Normalise a product card's alt text into a title.

    The alt ends in a "– Erscheinungsdatum" label separated by a non-breaking
    space; that's card chrome rather than part of the product name, so the
    whitespace is collapsed and the trailing label dropped.
    """
    if not text:
        return None
    normalized = " ".join(text.replace("\xa0", " ").split())
    normalized = LAUNCH_LABEL_RE.sub("", normalized).strip()
    return normalized or None


# Card names run: <model> "<colourway>" ['<variant>'] [suffix] (<style code>).
# The double-quoted colourway closes the product name proper; anything after
# it is secondary detail. Non-greedy so the split lands on the first quoted
# run rather than swallowing a later one.
PRODUCT_NAME_RE = re.compile(r'^(?P<name>.*?"[^"]*")\s*(?P<rest>.*)$', re.DOTALL)
# Only paired single quotes are unwrapped, so an apostrophe inside a word
# (e.g. "Women's") is left alone.
QUOTED_VARIANT_RE = re.compile(r"'([^']*)'")


def _split_product_title(text: str | None) -> tuple[str | None, str | None]:
    """Split a cleaned card name into a title and a subtitle.

    'Air Jordan 3 "Laser" \'Phantom and Sail\' (JA1369-001)' splits into
    ('Air Jordan 3 "Laser"', 'Phantom and Sail (JA1369-001)'). A name with
    nothing after the quoted colourway keeps the whole string as its title
    and gets no subtitle; one with no quoted colourway at all is left whole.
    """
    if not text:
        return None, None

    match = PRODUCT_NAME_RE.match(text)
    if match is None:
        return text, None

    name = match.group("name").strip()
    rest = QUOTED_VARIANT_RE.sub(r"\1", match.group("rest")).strip()
    return name or None, rest or None


def _normalize_image_url(url: str) -> str:
    """Rewrite this source's image URL into the plainest form that still
    resolves: no CDN transform segment, and a plain-ASCII filename.

    These URLs identify the asset by their UUID path segment; the filename
    after it is a human-readable slug the CDN ignores — the same URL serves
    the same image with the slug replaced. The slug is built from the
    product's display name, so it can carry accented letters, an en dash and
    a non-breaking space, percent-encoded into the path. Readers that
    normalise URLs before fetching tend to mangle or drop a %C2%A0, leaving
    the image broken, which is why images loaded from other sources but not
    this one.

    The CDN transform segment ("w_960,c_limit,q_auto,f_auto") is dropped for
    the same reason: it is the only part of the path carrying commas, and
    the working source's image URLs are a plain comma-free path. Confirmed
    the image still resolves without it.

    Only applied to this source, where the transform segment and slug are
    both known to be optional. On a source that serves images from real file
    paths, rewriting the path would simply 404.
    """
    parts = urlsplit(url)
    # Transform segments are the only ones carrying commas.
    path = "/".join(seg for seg in parts.path.split("/") if "," not in seg)
    head, _, filename = path.rpartition("/")
    if not filename:
        return url

    name, dot, ext = unquote(filename).rpartition(".")
    if not dot:  # no extension — treat the whole segment as the name
        name, ext = unquote(filename), ""

    # NFKD turns accented letters into base letter + combining mark (and a
    # non-breaking space into a plain one); dropping non-ASCII then leaves
    # the readable stem behind rather than deleting the whole word.
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_name)
    ascii_name = re.sub(r"-{2,}", "-", ascii_name).strip("-.") or "image"

    ext = re.sub(r"[^A-Za-z0-9]+", "", ext).lower()
    rebuilt = f"{head}/{ascii_name}" + (f".{ext}" if ext else "")
    return urlunsplit((parts.scheme, parts.netloc, rebuilt, parts.query, parts.fragment))


def _extract_product_card(link_tag, base_url: str) -> dict | None:
    """Pull title/link/image out of one product card anchor.

    The card's visible title, price and release date are rendered
    client-side: in the served HTML they are still empty
    <div class="nds-skeleton"> placeholders. The product image's alt text is
    the only place the name actually appears, so the title is taken from
    there and neither a summary nor a date is available.
    """
    href = link_tag.get("href")
    if not href:
        return None

    # Product detail pages live under /t/<slug>; nav and category links don't.
    if "/t/" not in urlparse(urljoin(base_url, href)).path:
        return None

    img_tag = link_tag.find("img")
    title, subtitle = _split_product_title(
        _clean_product_title(img_tag.get("alt") if img_tag is not None else None)
    )
    if not title:
        return None

    image_url = None
    if img_tag is not None:
        image_url = img_tag.get("src") or img_tag.get("data-src")
    if not image_url:
        # Fallback to the <picture> sources; each srcset entry is
        # "<url> <descriptor>", so the URL is the first whitespace-run.
        source_tag = link_tag.find("source")
        srcset = source_tag.get("srcset") if source_tag is not None else None
        if srcset:
            image_url = srcset.split(",")[0].strip().split(" ")[0]
    if image_url:
        image_url = _normalize_image_url(urljoin(base_url, image_url))

    return {
        "title": title,
        "link": urljoin(base_url, href),
        # RSS 2.0 has no item-level subtitle, so the secondary half of the
        # name goes in the description, which is what readers render beneath
        # the title and is otherwise unused for this source.
        "summary": subtitle,
        "image": image_url,
        "published": None,
    }


def parse_product_cards(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")

    # Anchored on the data-qa hook rather than the card's class names: those
    # are generated style hashes (css-1lu53xk, e4lt99o0, emoknll3) that change
    # on every front-end build, whereas the QA hook is stable.
    cards = soup.select('a[data-qa="product-card-link"][href]')
    if not cards:
        cards = soup.select("a.product-card-link[href]")

    return _collect(cards, base_url, _extract_product_card)


def _collect(containers, base_url: str, extract) -> list[dict]:
    """Run an extractor over candidate containers, skipping anything that
    fails to parse and de-duplicating by link.
    """
    articles: list[dict] = []
    seen_links: set[str] = set()

    for container in containers:
        try:
            item = extract(container, base_url)
        except Exception as exc:  # noqa: BLE001 - one bad item must not stop the run
            log.warning("Skipping an item due to parse error: %s", exc)
            continue

        if item is None or item["link"] in seen_links:
            continue

        seen_links.add(item["link"])
        articles.append(item)

    return articles


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    name: str
    base_url: str
    page_urls: tuple[str, ...]
    output_path: str
    feed_title: str
    feed_description: str
    parse: Callable[[str, str], list[dict]]
    language: str = "hr"


SOURCES: tuple[Source, ...] = (
    Source(
        name="sportnet",
        base_url="https://sportnet.hr",
        page_urls=tuple(f"https://sportnet.hr/arhiva/?pg={page}" for page in range(1, 11)),
        output_path="sportnet_feed.xml",
        feed_title="Sportnet.hr - Latest News",
        feed_description="Automatically generated RSS feed of the latest headlines from sportnet.hr",
        parse=parse_listing,
    ),
    Source(
        name="fsb",
        base_url="https://www.fsb.unizg.hr/index.php?fsbonline&novosti&cat=629",
        page_urls=("https://www.fsb.unizg.hr/index.php?fsbonline&novosti&cat=629",),
        output_path="fsb_feed.xml",
        feed_title="FSB - Novosti",
        feed_description="Automatically generated RSS feed of the latest news from fsb.unizg.hr",
        parse=parse_vijesti,
    ),
    Source(
        name="nike",
        base_url="https://www.nike.com/de/launch/in-stock",
        page_urls=("https://www.nike.com/de/launch/in-stock",),
        output_path="nike_feed.xml",
        feed_title="Nike Launch - In Stock",
        feed_description="Automatically generated RSS feed of in-stock launch products from nike.com",
        parse=parse_product_cards,
        language="de",
    ),
)


# --------------------------------------------------------------------------
# Shared pipeline
# --------------------------------------------------------------------------

def load_previous_feed(path: str) -> tuple[list[dict], str | None]:
    """Read the feed written by the previous run.

    Returns its entries plus its lastBuildDate. The newest entry is the
    marker scraping stops at, all entries are carried forward so the back
    catalogue survives, and the build date is reused when nothing changed so
    the file stays byte-identical and produces no commit. Returns ([], None)
    on the first run or an unreadable file, in which case every page gets
    scraped.
    """
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        log.info("No usable previous feed at %s (%s) — scraping every page.", path, exc)
        return [], None

    items: list[dict] = []
    for item in root.iterfind("./channel/item"):
        link = item.findtext("link")
        title = item.findtext("title")
        if not link or not title:
            continue
        enclosure = item.find("enclosure")
        items.append({
            "title": title,
            "link": link,
            "summary": item.findtext("description"),
            "image": enclosure.get("url") if enclosure is not None else None,
            # Kept as the RFC-822 string it was written as; feedgen re-parses it.
            "published": item.findtext("pubDate"),
        })

    log.info("Loaded %d item(s) from %s.", len(items), path)
    return items, root.findtext("./channel/lastBuildDate")


def fetch_all_articles(source: Source, stop_link: str | None = None) -> list[dict]:
    """Fetch a source's pages newest-first, collecting articles until the
    newest item from the previous run turns up.

    Both the listing and the previous feed are ordered newest-first, so
    reaching that one item means everything past it was already published —
    there is nothing left to find and the remaining pages can be skipped.
    If it is never found (e.g. it was removed from the listing), every page
    is scraped, which is the safe fallback.
    """
    articles: list[dict] = []
    seen_links: set[str] = set()

    for page_number, url in enumerate(source.page_urls, start=1):
        if len(articles) >= MAX_ITEMS:
            break

        html = fetch_html(url)
        if html is None:
            continue

        try:
            page_articles = source.parse(html, source.base_url)
        except Exception as exc:  # noqa: BLE001 - one bad page must not stop the run
            log.error("Failed to parse articles from %s: %s", url, exc)
            continue

        for article in page_articles:
            if stop_link is not None and article["link"] == stop_link:
                log.info(
                    "Reached the previous run's newest item on page %d — "
                    "stopping with %d new item(s).",
                    page_number, len(articles),
                )
                return articles

            if article["link"] in seen_links:
                continue
            seen_links.add(article["link"])
            articles.append(article)
            if len(articles) >= MAX_ITEMS:
                break

    return articles


IMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
}


def _image_mime(url: str) -> str:
    """Guess an image's MIME type from its URL, defaulting to JPEG."""
    path = urlparse(url).path.lower()
    for ext, mime in IMAGE_MIME_BY_EXT.items():
        if path.endswith(ext):
            return mime
    return "image/jpeg"


def build_feed(
    source: Source,
    articles: list[dict],
    last_build_date: str | None = None,
) -> FeedGenerator:
    fg = FeedGenerator()
    # Adds the Media RSS namespace, so each item can advertise its image in
    # the form the big aggregators actually read.
    fg.load_extension("media")
    fg.id(source.base_url)
    fg.title(source.feed_title)
    fg.link(href=source.base_url, rel="alternate")
    fg.description(source.feed_description)
    fg.language(source.language)
    # Reusing the previous build date when the items are unchanged keeps the
    # output byte-identical, so the workflow has nothing to commit.
    fg.lastBuildDate(last_build_date or datetime.now(timezone.utc))

    for article in articles:
        # feedgen prepends by default, which would reverse our newest-first
        # scrape order and emit the feed oldest-first; append preserves it.
        fe = fg.add_entry(order="append")
        fe.id(article["link"])
        fe.title(article["title"])
        fe.link(href=article["link"])
        summary = article.get("summary")
        image = article.get("image")

        if summary:
            fe.description(summary)

        if image:
            mime = _image_mime(image)
            # Readers disagree on where an item's image lives, so advertise
            # it three ways: an <img> in content:encoded, which is the one
            # almost all of them render; media:content, which the large
            # aggregators read; and the enclosure, for those that only look
            # there. An enclosure alone — how this used to work — is widely
            # treated as a podcast attachment and skipped for images.
            #
            # The markup goes in content:encoded rather than the description
            # on purpose: the description is what load_previous_feed reads
            # back, so building HTML into it would re-wrap the same entry
            # again on every carry-forward.
            body = f'<img src="{escape(image, quote=True)}"'
            body += f' alt="{escape(article["title"], quote=True)}"/>'
            if summary:
                body += f"<p>{escape(summary)}</p>"
            fe.content(body, type="CDATA")

            # length is required by the RSS spec but only knowable by
            # fetching each image, which would mean an extra request per
            # item; readers that need a real size read media:content anyway.
            fe.enclosure(image, 0, mime)
            # group=None keeps these as direct children of <item> instead of
            # nesting them in <media:group>, which is the shape readers look
            # for. media:thumbnail is what list/card views read to show a
            # preview, separately from whatever the article body renders.
            fe.media.content(url=image, medium="image", type=mime, group=None)
            fe.media.thumbnail(url=image, group=None)
        if article.get("published"):
            try:
                fe.pubDate(article["published"])
            except Exception:  # noqa: BLE001 - bad/unparseable date shouldn't break the entry
                pass

    return fg


def generate(source: Source) -> bool:
    """Build and write one source's feed. Returns False only if the file
    could not be written.
    """
    log.info("--- %s ---", source.name)
    previous, previous_build_date = load_previous_feed(source.output_path)

    # Only the previous feed's newest item is used as the stopping marker.
    stop_link = previous[0]["link"] if previous else None

    try:
        new_articles = fetch_all_articles(source, stop_link)
    except Exception as exc:  # noqa: BLE001 - one bad page must never crash the run
        log.error("Failed to fetch articles for %s: %s", source.name, exc)
        new_articles = []

    # Sources that publish no date leave items with no pubDate at all, which
    # readers handle poorly — they cannot sort them, and some skip rendering
    # parts of a dateless item. Fall back to when the item was first seen.
    # Only newly scraped items are stamped; carried-over ones keep the date
    # already stored for them, so an item's date never moves once set.
    first_seen = datetime.now(timezone.utc)
    for article in new_articles:
        if not article.get("published"):
            article["published"] = first_seen

    # Newly scraped items are the most recent, so they lead; the previous
    # feed's entries follow to preserve history. Without carrying them over,
    # a run that found nothing new would publish an empty feed.
    new_links = {article["link"] for article in new_articles}
    articles = new_articles + [item for item in previous if item["link"] not in new_links]
    articles = articles[:MAX_ITEMS]

    log.info(
        "%d new, %d carried over, %d total item(s).",
        len(new_articles), len(articles) - len(new_articles), len(articles),
    )

    if not articles:
        log.warning("No articles found (fetch or parse failed) — writing an empty feed.")

    # Only stamp a fresh build date when the item set actually moved; an
    # unchanged feed is rewritten exactly as it was so git sees no diff.
    unchanged = [a["link"] for a in articles] == [p["link"] for p in previous]
    if unchanged and previous_build_date:
        log.info("Items unchanged — keeping the previous build date, no commit expected.")

    try:
        feed = build_feed(source, articles, previous_build_date if unchanged else None)
        feed.rss_file(source.output_path)
        log.info("Wrote %d item(s) to %s", len(articles), source.output_path)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to write %s: %s", source.output_path, exc)
        return False

    return True


def select_sources(only: list[str] | None, exclude: list[str] | None) -> list[Source]:
    """Resolve which sources to run.

    Unknown names are a hard error rather than a no-op: a typo in the
    workflow would otherwise quietly stop a feed from ever updating again,
    which is invisible until someone notices the feed has gone stale.
    """
    known = {source.name for source in SOURCES}
    unknown = sorted((set(only or ()) | set(exclude or ())) - known)
    if unknown:
        raise SystemExit(
            f"Unknown source(s): {', '.join(unknown)}. "
            f"Known sources: {', '.join(sorted(known))}."
        )

    chosen = [s for s in SOURCES if not only or s.name in only]
    return [s for s in chosen if s.name not in (exclude or ())]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate an RSS feed for each configured source.",
    )
    parser.add_argument(
        "--only", nargs="+", metavar="NAME",
        help="run only these sources (default: all of them)",
    )
    parser.add_argument(
        "--exclude", nargs="+", metavar="NAME",
        help="run every source except these",
    )
    args = parser.parse_args(argv)

    sources = select_sources(args.only, args.exclude)
    if not sources:
        log.warning("Every source was filtered out — nothing to do.")
        return 0

    log.info("Running source(s): %s", ", ".join(s.name for s in sources))
    # One source failing must not stop the others from being regenerated.
    results = [generate(source) for source in sources]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
