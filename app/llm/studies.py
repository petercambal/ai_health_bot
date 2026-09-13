"""OpenAlex lookup backing the search_scientific_studies tool - lets the bot ground
longevity/health answers in current peer-reviewed research instead of relying only on
the model's own (possibly stale or hallucinated) recollection of studies.

Originally built against Semantic Scholar's Graph API, but its unauthenticated rate
limit (~100 requests/5min shared across every unauthenticated caller worldwide) proved
unusable in practice - every single request during development and initial real usage
came back 429, and a dedicated key requires manual approval (community reports ~5 day
turnaround). OpenAlex is a fully open, keyless-by-default alternative built for
exactly this kind of use case, verified working live on 2026-09-13: instant 200 OK, no
signup. An optional `mailto` contact (OPENALEX_EMAIL in .env) puts requests in
OpenAlex's "polite pool" for more reliable service - not an API key, just a courtesy
identifier, no approval needed.
"""

import logging

import aiohttp

from app.config import settings

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.openalex.org/works"
_SELECT_FIELDS = "title,publication_year,doi,authorships,primary_location"
_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def search_studies(query: str, limit: int = 5) -> list[dict]:
    """Returns [{title, authors, year, venue, url}, ...], or an empty list on any
    failure (rate limit, timeout, network error) - callers should treat that as "no
    studies found this time" and let Gemini answer without citations, not as a hard
    error."""
    params = {"search": query, "per-page": str(limit), "select": _SELECT_FIELDS}
    if settings.openalex_email:
        params["mailto"] = settings.openalex_email

    try:
        async with (
            aiohttp.ClientSession(timeout=_TIMEOUT) as session,
            session.get(_SEARCH_URL, params=params) as resp,
        ):
            if resp.status != 200:
                logger.warning(
                    "OpenAlex search failed (status=%s) for query=%r", resp.status, query
                )
                return []
            data = await resp.json()
    except Exception:
        logger.exception("OpenAlex search request failed for query=%r", query)
        return []

    works = data.get("results") or []
    results = []
    for work in works:
        authors = [
            (a.get("author") or {}).get("display_name")
            for a in (work.get("authorships") or [])
            if (a.get("author") or {}).get("display_name")
        ]
        source = ((work.get("primary_location") or {}).get("source")) or {}
        results.append(
            {
                "title": work.get("title"),
                "authors": authors,
                "year": work.get("publication_year"),
                "venue": source.get("display_name"),
                "url": work.get("doi"),
            }
        )
    return results
