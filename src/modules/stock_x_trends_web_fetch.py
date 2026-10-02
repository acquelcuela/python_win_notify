from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.grok_web import ask_grok
from modules.stock_x_trends import (
    WEB_CACHE_PATH,
    _build_prompt,
    _market_context,
    _module_config,
    _normalize_payload,
    _search_passes,
)


JST = timezone(timedelta(hours=9), "JST")

# Driving an actual Chrome session (page load, typing the question, waiting
# for Grok's web UI to finish answering) is far slower than the API path -
# this is deliberately generous and is why this runs as its own module on
# its own early schedule slot, well ahead of the stock_x_trends modules that
# read its cached output, rather than inline with them.
GROK_TIMEOUT_SECONDS = 600

RAW_RUNS_DIR_NAME = "stock_x_trends_web_fetch_runs"

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "common_keywords": {"type": "array", "items": {"type": "string"}},
        "discovery_findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "name": {"type": "string"},
                    "reason": {"type": "string"},
                    "sentiment": {"type": "string"},
                    "source": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["reason"],
            },
        },
    },
    "required": ["common_keywords", "discovery_findings"],
}


def _build_grok_question(context: str) -> str:
    # Reuses the exact same question stock_x_trends.py's API path would ask
    # Grok (the "broad" pass, index 0) so both sources answer the same
    # question and stay comparable - only the delivery mechanism differs.
    _, search_terms, focus = _search_passes()[0]
    return _build_prompt(focus, search_terms, context)


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "stock_x_trends_web_fetch.json"
    started = datetime.now(JST)
    generated_at = started.isoformat()
    # One raw Grok result file per run, never overwritten (kept indefinitely).
    schedule_key = os.getenv("BATCH_SCHEDULE_KEY", "").strip()
    slot_label = schedule_key.replace(":", "") if schedule_key else f"{started:%H%M%S}_manual"
    raw_path = output_dir / "history" / RAW_RUNS_DIR_NAME / f"stock_x_trends_web_fetch_{started:%Y%m%d}_{slot_label}.json"

    def _fail(reason: str) -> None:
        payload = {
            "module": "stock_x_trends_web_fetch",
            "generated_at": generated_at,
            "status": "error",
            "raw_file": str(raw_path.relative_to(root)) if raw_path.exists() else None,
            "error": reason,
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[stock_x_trends_web_fetch] %s", reason)

    # Only spend a Grok web call when stock_x_trends will actually read the
    # cache - flipping stock_x_trends.source back to "api" turns this off too.
    config = _module_config(root)
    source = str(config.get("source") or "api").strip().lower()
    if not config.get("enabled", False) or source != "web":
        payload = {
            "module": "stock_x_trends_web_fetch",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": f"stock_x_trends is disabled or its source is '{source}', not 'web'.",
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[stock_x_trends_web_fetch] skipped: stock_x_trends source is not web")
        return

    question = _build_grok_question(_market_context(root))
    result = ask_grok(
        question,
        raw_path,
        schema=JSON_SCHEMA,
        timeout_seconds=GROK_TIMEOUT_SECONDS,
        cwd=root,
        caller="stock_x_trends_web_fetch",
    )
    if result["status"] != "ok":
        _fail(f"grok_web: {result['error']}")
        return

    try:
        if not isinstance(result["data"], dict):
            raise ValueError("Grok's answer has no structured data")
        data = _normalize_payload(result["data"])
    except Exception as exc:
        _fail(f"could not normalize Grok's answer: {exc}")
        return

    cache_path = root / WEB_CACHE_PATH
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"generated_at": generated_at, "data": data}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    payload = {
        "module": "stock_x_trends_web_fetch",
        "generated_at": generated_at,
        "status": "ok",
        "common_keywords": len(data["common_keywords"]),
        # _normalize_payload splits Grok's findings by whether they carry a
        # ticker; its discovery_findings is only the ticker-less (theme) half.
        "stock_findings": len(data["stock_findings"]),
        "theme_findings": len(data["theme_findings"]),
        "elapsed_seconds": result["elapsed_seconds"],
        "raw_file": str(raw_path.relative_to(root)),
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info(
        "[stock_x_trends_web_fetch] cached %d keyword(s), %d stock finding(s), %d theme finding(s) via grok_web",
        len(data["common_keywords"]),
        len(data["stock_findings"]),
        len(data["theme_findings"]),
    )


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
