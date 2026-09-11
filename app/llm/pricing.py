"""USD pricing per 1M tokens, by Gemini model - used only for the /tokens Telegram
command's cost estimate. Gemini pricing changes over time and isn't exposed via the
API, so this is a hand-maintained snapshot (verified 2026-09-11 via ai.google.dev
/gemini-api/docs/pricing and web search, gemini-2.5-flash and gemini-3.5-flash
pricing from ai.google.dev directly; gemini-3.6-flash from third-party aggregators
since Google's own pricing page didn't list it yet) rather than something fetched
live - re-verify before trusting it for anything beyond a rough estimate, and add
new models here as you switch to them.

gemini-3.6-flash is introductory pricing through 2026-12-31; standard pricing
($1.50 / $7.50) kicks in 2027-01-01 - update this entry then.

gemini-2.5-flash is no longer available to new projects as of 2026-09 (Google
returns 404, recommending gemini-3.6-flash) - kept here only in case an existing
project still has access to it.
"""

MODEL_PRICING_USD_PER_MILLION = {
    "gemini-3.6-flash": {"input": 0.75, "output": 3.75},  # introductory, through 2026-12-31
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},  # deprecated for new projects
}


def estimate_cost_usd(model: str, prompt_tokens: int, response_tokens: int) -> float | None:
    """Returns None (rather than 0) when the model isn't in the table, so callers can
    tell "no cost" apart from "cost unknown" instead of silently under-reporting."""
    pricing = MODEL_PRICING_USD_PER_MILLION.get(model)
    if pricing is None:
        return None
    return (prompt_tokens / 1_000_000) * pricing["input"] + (
        response_tokens / 1_000_000
    ) * pricing["output"]
