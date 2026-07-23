from __future__ import annotations

# Standard-tier Gemini API pricing, USD per 1M tokens (text).
# https://ai.google.dev/gemini-api/docs/pricing
GEMINI_PRICING_USD_PER_MILLION = {
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
}
DEFAULT_PRICING = {"input": 0.25, "output": 1.50}
USD_TO_JPY = 160


def cost_jpy(model: str, prompt_tokens: int, output_tokens: int) -> float:
    pricing = GEMINI_PRICING_USD_PER_MILLION.get(model, DEFAULT_PRICING)
    cost_usd = (prompt_tokens / 1_000_000 * pricing["input"]) + (
        output_tokens / 1_000_000 * pricing["output"]
    )
    return cost_usd * USD_TO_JPY


class GeminiUsageTracker:
    """Accumulates token usage across one or more Gemini calls within a
    single module run, so the caller can report a total cost."""

    def __init__(self, model: str):
        self.model = model
        self.call_count = 0
        self.prompt_tokens = 0
        self.output_tokens = 0

    def add(self, usage: dict | None) -> None:
        if not usage:
            return
        self.call_count += 1
        self.prompt_tokens += int(usage.get("promptTokenCount") or 0)
        self.output_tokens += int(usage.get("candidatesTokenCount") or 0)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens

    @property
    def cost_jpy(self) -> float:
        return cost_jpy(self.model, self.prompt_tokens, self.output_tokens)
