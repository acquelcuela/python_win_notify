from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf

from modules.stock_range import _load_predictions_log, _save_predictions_log


JST = timezone(timedelta(hours=9), "JST")


def _fetch_today_change_pct(ticker_symbol: str, today_date) -> float | None:
    """Live same-day change vs previous close. Returns None if the latest
    bar yfinance has isn't today's JST date yet - e.g. US tickers at 17:00
    JST, since the US session hasn't opened. Those candidates are left
    unevaluated here and get picked up by stock_range's own next-run
    evaluation once a fresh change_pct exists for them."""
    try:
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period="5d", interval="1d", auto_adjust=False)
        hist = hist.dropna(subset=["Close"])
        if len(hist) < 2:
            return None
        latest = hist.iloc[-1]
        if hist.index[-1].date() != today_date:
            return None
        close = float(latest["Close"])
        prev_close = float(hist.iloc[-2]["Close"])
        if not prev_close:
            return None
        return (close - prev_close) / prev_close * 100
    except Exception as exc:
        logging.warning("[stock_range_eval] %s fetch failed: %s", ticker_symbol, exc)
        return None


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "stock_range_eval.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()
    today_label = now.strftime("%Y-%m-%d")

    records = _load_predictions_log(root)
    todays_candidates = [
        r for r in records
        if r.get("logged_date") == today_label and not r.get("evaluated")
    ]

    if not todays_candidates:
        result = {
            "module": "stock_range_eval",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "No unevaluated candidates logged today.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[stock_range_eval] skipped: no unevaluated candidates logged today")
        return

    today_date = now.date()
    evaluated = []
    changed = False
    for record in todays_candidates:
        change_pct = _fetch_today_change_pct(record.get("ticker"), today_date)
        if change_pct is None:
            continue
        record["evaluated"] = True
        record["evaluated_date"] = today_label
        record["actual_change_pct"] = round(change_pct, 2)
        record["hit"] = bool(change_pct > 0)
        changed = True
        evaluated.append(record)

    if changed:
        _save_predictions_log(root, records)

    if not evaluated:
        result = {
            "module": "stock_range_eval",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "Today's close price is not available yet for any logged candidate.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[stock_range_eval] skipped: no fresh same-day price data available")
        return

    hit_count = sum(1 for r in evaluated if r.get("hit"))
    skipped_count = len(todays_candidates) - len(evaluated)
    results_payload = [
        {
            "type": r.get("type"),
            "ticker": r.get("ticker"),
            "name": r.get("name"),
            "score": r.get("score"),
            "reasons": r.get("reasons"),
            "actual_change_pct": r.get("actual_change_pct"),
            "hit": r.get("hit"),
        }
        for r in evaluated
    ]

    result = {
        "module": "stock_range_eval",
        "generated_at": generated_at,
        "status": "ok",
        "evaluated_count": len(evaluated),
        "hit_count": hit_count,
        "skipped_count": skipped_count,
        "results": results_payload,
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[stock_range_eval] evaluated %d candidates (%d hits)", len(evaluated), hit_count)


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
