"""Finding and fetching a feed: the address given, the feed a page
advertises, or one at a usual place on the site (``/feed``,
``/rss.xml``, ``/atom.xml``…). Every fetch is the safe one, in a thread."""

from __future__ import annotations

import asyncio

from .feed_parse import FEED_TYPES, FeedProblem, advertised_feeds, is_feed, parse_feed, probes
from .fetch import FetchError, fetch
from .page_text import kind_of

USER_AGENT = "DecentAI-WebWatch/2.0 (checks a page or feed a DecentAI user asked to watch)"
ACCEPT = ", ".join(FEED_TYPES) + ", application/xml;q=0.9, text/xml;q=0.9, text/html;q=0.8, */*;q=0.5"


async def fetch_feed(url: str):
    """The feed at this address, or FeedProblem / FetchError."""
    fetched = await fetch(url, USER_AGENT, ACCEPT)
    if not is_feed(fetched):
        raise FeedProblem(f"{fetched.final_url} is not an RSS, Atom or JSON feed "
                          f"({fetched.content_type or 'unknown type'}).")
    return await _parsed(fetched)


async def _parsed(fetched):
    if fetched.truncated:
        raise FeedProblem(f"The feed at {fetched.final_url} is larger than "
                          f"{len(fetched.body):,} bytes and was not read.")
    return await asyncio.to_thread(parse_feed, fetched.body, fetched.final_url, fetched.header_charset)


async def resolve_feed(url: str):
    """(feed, the page it was found from or '')."""
    fetched = await fetch(url, USER_AGENT, ACCEPT)
    if is_feed(fetched):
        return await _parsed(fetched), ""
    if kind_of(fetched.content_type, fetched.body) != "html":
        raise FeedProblem(f"{fetched.final_url} is {fetched.content_type or 'neither'} a feed or "
                          f"a web page.")
    advertised = advertised_feeds(fetched.text(), fetched.final_url)
    if advertised:
        return await fetch_feed(advertised[0]), fetched.final_url
    # None advertised: look where sites usually keep one, all at once.
    tries = probes(fetched.final_url)

    async def tried(address):
        try:
            return await fetch_feed(address)
        except (FetchError, FeedProblem):
            return None

    for feed in await asyncio.gather(*(tried(address) for address in tries)):
        if feed is not None:
            return feed, fetched.final_url
    raise FeedProblem(f"{fetched.final_url} is a web page that advertises no RSS, Atom or JSON "
                      f"feed, and there is none at {', '.join('/' + p.split('/', 3)[-1] for p in tries)}. "
                      f"Ask for the feed's own address.")
