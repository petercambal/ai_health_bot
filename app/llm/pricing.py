"""USD pricing per 1M tokens, by Gemini model - used only for the /tokens Telegram
command's cost estimate. Gemini pricing changes over time and isn't exposed via the
API, so this is a hand-maintained snapshot (verified via ai.google.dev/gemini-api/docs
/pricing on 2026-09-11) rather than something fetched live - re-verify before trusting
it for anything beyond a rough estimate, and add new models here as you switch to them.
"""

MODEL_PRICING_USD_PER_MILLION = {
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
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
