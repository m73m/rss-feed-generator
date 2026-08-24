#!/usr/bin/env python3
"""Build test_feed.xml: one item from each source, in one feed.

A diagnostic for readers that show thumbnails for some sources but not
others. Both items are rebuilt through the same build_feed() the real feeds
use, so their markup is identical in every respect except the image host —
which makes a reader showing one thumbnail and not the other conclusive
about where the difference lies.

Run after the real feeds exist:  python make_test_feed.py
"""
from __future__ import annotations

import sys

from generate_feed import SOURCES, Source, build_feed, load_previous_feed, log, parse_listing

OUTPUT = "test_feed.xml"

TEST_SOURCE = Source(
    name="test",
    base_url="https://m73m.github.io/rss-feed-generator/",
    page_urls=(),
    output_path=OUTPUT,
    feed_title="Feed image diagnostic",
    feed_description=(
        "One item from each source, sharing identical markup, to isolate "
        "which image host a reader will and won't render a thumbnail for."
    ),
    parse=parse_listing,  # unused: this feed is assembled, never scraped
    language="en",
)


def main() -> int:
    articles: list[dict] = []

    for source in SOURCES:
        items, _ = load_previous_feed(source.output_path)
        newest_with_image = next((i for i in items if i.get("image")), None)
        if newest_with_image is None:
            log.warning("%s has no item with an image — skipping.", source.name)
            continue
        # Label the title so it is obvious which source a row came from.
        item = dict(newest_with_image)
        item["title"] = f"[{source.name}] {item['title']}"
        articles.append(item)
        log.info("Took 1 item from %s: %s", source.name, item["image"])

    if not articles:
        log.error("No source had an item with an image.")
        return 1

    build_feed(TEST_SOURCE, articles).rss_file(OUTPUT)
    log.info("Wrote %d item(s) to %s", len(articles), OUTPUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
