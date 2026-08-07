from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf


JST = timezone(timedelta(hours=9), "JST")

# strong_positive/positive findings imply "expect the stock to be up today";
# negative implies "expect it to be down". neutral has no directional claim
# to check, so it's excluded from evaluation entirely.
_EXPECTED_DIRECTION = {
    "strong_positive": "up",
    "positive": "up",
    "negative": "down",
}

PREDICTIONS_LOG_PATH = Path("state") / "stock_x_trends_predictions.json"
PREDICTION_LOG_RETENTION_DAYS = 90


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _load_predictions_log(root: Path) -> list[dict]:
    path = root / PREDICTIONS_LOG_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _append_predictions_log(root: Path, today_label: str, results: list[dict]) -> None:
    path = root / PREDICTIONS_LOG_PATH
    records = _load_predictions_log(root)
    # Evaluation only ever runs once per day (22:45), but stay idempotent
    # in case of a manual re-run so re-running doesn't duplicate the day's
    # entries.
    already_logged = {r.get("ticker") for r in records if r.get("logged_date") == today_label}
    for r in results:
        if r["ticker"] in already_logged:
            continue
        records.append(
            {
                "logged_date": today_label,
                "ticker": r["ticker"],
                "name": r["name"],
                "sentiment": r["sentiment"],
                "reason": r["reason"],
                "actual_change_pct": r["actual_change_pct"],
                "expected_direction": r["expected_direction"],
                "hit": r["hit"],
            }
        )
    cutoff_label = (datetime.now(JST) - timedelta(days=PREDICTION_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
    trimmed = [r for r in records if r.get("logged_date", "9999-99-99") >= cutoff_label]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")


def _fetch_today_change_pct(ticker_code: str, today_date) -> float | None:
    """Same-day change vs previous close for a bare Japan ticker code (e.g.
    "3103"). Returns None if today's bar isn't available yet (e.g. run
    before market close) or the ticker doesn't resolve on yfinance."""
    try:
        ticker = yf.Ticker(f"{ticker_code}.T")
        hist = ticker.history(period="5d", interval="1d", auto_adjust=False)
        hist = hist.dropna(subset=["Close"])
        if len(hist) < 2:
            return None
        if hist.index[-1].date() != today_date:
            return None
        close = float(hist.iloc[-1]["Close"])
        prev_close = float(hist.iloc[-2]["Close"])
        if not prev_close:
            return None
        return (close - prev_close) / prev_close * 100
    except Exception as exc:
        logging.warning("[stock_x_trends_eval] %s fetch failed: %s", ticker_code, exc)
        return None


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "stock_x_trends_eval.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()

    payload = _load_json(root / "output" / "stock_x_trends.json")
    if not payload or payload.get("status") != "ok" or not payload.get("data"):
        result = {
            "module": "stock_x_trends_eval",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "stock_x_trends output is not available.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[stock_x_trends_eval] skipped: stock_x_trends output is not available")
        return

    try:
        found_at = datetime.fromisoformat(str(payload.get("generated_at")))
        if found_at.tzinfo is None:
            found_at = found_at.replace(tzinfo=JST)
        if found_at.astimezone(JST).date() != now.date():
            result = {
                "module": "stock_x_trends_eval",
                "generated_at": generated_at,
                "status": "skipped",
                "reason": "stock_x_trends output is not from today.",
            }
            output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            logging.info("[stock_x_trends_eval] skipped: stock_x_trends output is not from today")
            return
    except (TypeError, ValueError):
        pass

    findings = [
        f for f in (payload["data"].get("stock_findings") or [])
        if str(f.get("ticker") or "").strip() and f.get("verified") is not False
    ]
    if not findings:
        result = {
            "module": "stock_x_trends_eval",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "No verified stock findings to evaluate.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[stock_x_trends_eval] skipped: no verified stock findings")
        return

    today_date = now.date()
    results = []
    for finding in findings:
        ticker = str(finding.get("ticker")).strip()
        sentiment = str(finding.get("sentiment") or "").strip()
        expected = _EXPECTED_DIRECTION.get(sentiment)
        change_pct = _fetch_today_change_pct(ticker, today_date)
        if change_pct is None:
            continue
        actual = "up" if change_pct > 0 else "down"
        results.append(
            {
                "ticker": ticker,
                "name": finding.get("name"),
                "sentiment": sentiment,
                "reason": finding.get("reason"),
                "actual_change_pct": round(change_pct, 2),
                "expected_direction": expected,
                # hit is only meaningful for positive/negative findings;
                # neutral findings still show the actual move but no verdict.
                "hit": (actual == expected) if expected else None,
            }
        )

    if not results:
        result = {
            "module": "stock_x_trends_eval",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "Today's close price is not available yet for any finding.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[stock_x_trends_eval] skipped: no fresh same-day price data available")
        return

    today_label = now.strftime("%Y-%m-%d")
    _append_predictions_log(root, today_label, results)

    judged = [r for r in results if r["hit"] is not None]
    hit_count = sum(1 for r in judged if r["hit"])
    result = {
        "module": "stock_x_trends_eval",
        "generated_at": generated_at,
        "status": "ok",
        "evaluated_count": len(judged),
        "hit_count": hit_count,
        "results": results,
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[stock_x_trends_eval] evaluated %d findings (%d hits of %d judged)", len(results), hit_count, len(judged))


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
