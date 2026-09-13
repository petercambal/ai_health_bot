"""Semantic Scholar lookup backing the search_scientific_studies tool - lets the bot
ground longevity/health answers in current peer-reviewed research instead of relying
only on the model's own (possibly stale or hallucinated) recollection of studies.

The public Semantic Scholar Graph API works without a key, but its unauthenticated
rate limit is a single small pool shared across every unauthenticated caller
worldwide, and is very easy to exhaust (observed a 429 on the very first request
during development, with no prior traffic from this app). Get a free key at
https://www.semanticscholar.org/product/api#api-key-form and set
SEMANTIC_SCHOLAR_API_KEY in .env for a dedicated, far higher limit - otherwise expect
this tool to fail gracefully (empty results) fairly often.
"""

import logging

import aiohttp

from app.config import settings

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
_FIELDS = "title,authors,year,abstract,url,venue"
_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def search_studies(query: str, limit: int = 5) -> list[dict]:
    """Returns [{title, authors, year, venue, url, abstract}, ...], or an empty list
    on any failure (rate limit, timeout, network error) - callers should treat that
    as "no studies found this time" and let Gemini answer without citations, not as
    a hard error, since the free tier of this API is unreliable by nature."""
    headers = {}
    if settings.semantic_scholar_api_key:
        headers["x-api-key"] = settings.semantic_scholar_api_key

    params = {"query": query, "fields": _FIELDS, "limit": str(limit)}
    try:
        async with (
            aiohttp.ClientSession(timeout=_TIMEOUT) as session,
            session.get(_SEARCH_URL, params=params, headers=headers) as resp,
        ):
            if resp.status != 200:
                logger.warning(
                    "Semantic Scholar search failed (status=%s) for query=%r", resp.status, query
                )
                return []
            data = await resp.json()
    except Exception:
        logger.exception("Semantic Scholar search request failed for query=%r", query)
        return []

    papers = data.get("data") or []
    results = []
    for paper in papers:
        authors = [a.get("name") for a in (paper.get("authors") or []) if a.get("name")]
        abstract = paper.get("abstract") or ""
        results.append(
            {
                "title": paper.get("title"),
                "authors": authors,
                "year": paper.get("year"),
                "venue": paper.get("venue"),
                "url": paper.get("url"),
                "abstract": abstract[:500] or None,
            }
        )
    return results
