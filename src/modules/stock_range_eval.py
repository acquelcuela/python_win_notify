from __future__ import annotations

import json
import logging
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path

import yfinance as yf

from modules.market_calendar import is_trading_day
from modules.market_realtime import fetch_yahoo_top_indices
from modules.stock_range import _load_predictions_log, _save_predictions_log


JST = timezone(timedelta(hours=9), "JST")

MARKET_FORECAST_LOG_PATH = Path("state") / "market_forecast_accuracy.json"
FORECAST_LOG_RETENTION_DAYS = 90

# Before Tokyo's 15:00 close, a run only sees partial-day intraday moves, so
# it's treated as a non-final "interim" read (recorded on the candidate but
# not marked evaluated - the run after close still re-evaluates it and can
# see whether the interim verdict flipped by the close).
MARKET_CLOSE_TIME = dt_time(15, 0)


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _is_from_today(payload: dict, now: datetime) -> bool:
    try:
        generated_at = datetime.fromisoformat(str(payload.get("generated_at")))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=JST)
        return generated_at.astimezone(JST).date() == now.date()
    except (TypeError, ValueError):
        return False


def _log_market_forecast_accuracy(root: Path, now: datetime, today_label: str) -> None:
    """Logs predicted (overnight Nikkei futures, read by stock_range at
    06:45 scoring time) vs actual (day-session Nikkei average) change_pct,
    so the quality of the futures-based market signal used for scoring can
    be checked later - independent of whether any candidates were logged
    or evaluated today."""
    range_payload = _load_json(root / "output" / "stock_range.json")
    if not range_payload or range_payload.get("status") != "ok" or not _is_from_today(range_payload, now):
        return
    predicted = range_payload.get("market_change_pct")
    if predicted is None:
        return

    nikkei_payload = _load_json(root / "output" / "stock_nikkei.json")
    if not nikkei_payload or nikkei_payload.get("status") != "ok" or not _is_from_today(nikkei_payload, now):
        return
    actual = ((nikkei_payload.get("data") or {}).get("indices") or {}).get("nikkei_average", {}).get("change_pct")
    if actual is None:
        return

    path = root / MARKET_FORECAST_LOG_PATH
    records = _load_json(path) or []
    if not isinstance(records, list):
        records = []
    if any(r.get("date") == today_label for r in records):
        return

    records.append(
        {
            "date": today_label,
            "predicted_change_pct": predicted,
            "actual_change_pct": actual,
            "diff": round(actual - predicted, 2),
            "same_sign": (predicted >= 0) == (actual >= 0),
        }
    )
    cutoff_label = (now - timedelta(days=FORECAST_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
    trimmed = [r for r in records if r.get("date", "9999-99-99") >= cutoff_label]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info(
        "[stock_range_eval] logged market forecast accuracy: predicted=%+.2f%% actual=%+.2f%%",
        predicted,
        actual,
    )


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


def _fetch_today_open_close(ticker_symbol: str, today_date) -> dict:
    """Today's opening gap and open -> close move, recorded next to the
    close-vs-previous-close hit (2026-10-22): a gap-up morning makes the
    previous-close hit look better than what buying at the open would get."""
    try:
        hist = yf.Ticker(ticker_symbol).history(period="5d", interval="1d", auto_adjust=False).dropna(subset=["Close"])
        if len(hist) < 2 or hist.index[-1].date() != today_date:
            return {}
        today, prev_close = hist.iloc[-1], float(hist.iloc[-2]["Close"])
        open_price, close = float(today["Open"]), float(today["Close"])
        if not open_price or not prev_close:
            return {}
        open_to_close = (close / open_price - 1) * 100
        return {
            "opening_gap_pct": round((open_price / prev_close - 1) * 100, 2),
            "open_to_close_pct": round(open_to_close, 2),
            "hit_open_to_close": bool(open_to_close > 0),
        }
    except Exception as exc:
        logging.warning("[stock_range_eval] %s open/close fetch failed: %s", ticker_symbol, exc)
        return {}


# The report splits each candidate's track record by the day's market
# direction (user request 2026-10-06: a single hit rate mixed ~65% on up
# days with ~30% on down days). TOPIX itself isn't available from yfinance,
# so the TOPIX-tracking ETF 1306 stands in for it.
NIKKEI_TICKER = "^N225"
TOPIX_PROXY_TICKER = "1306.T"


def classify_market_day(nikkei_change_pct: float | None, topix_change_pct: float | None) -> str | None:
    """"up" if Nikkei and TOPIX both rose, "down" if both fell, else "mixed"."""
    if nikkei_change_pct is None or topix_change_pct is None:
        return None
    if nikkei_change_pct > 0 and topix_change_pct > 0:
        return "up"
    if nikkei_change_pct <= 0 and topix_change_pct <= 0:
        return "down"
    return "mixed"


def _daily_change_pcts(ticker_symbol: str, start: str) -> dict[str, float]:
    try:
        hist = yf.Ticker(ticker_symbol).history(start=start, interval="1d", auto_adjust=False)
    except Exception as exc:
        logging.warning("[stock_range_eval] %s history fetch failed: %s", ticker_symbol, exc)
        return {}
    closes = hist["Close"].dropna() if not hist.empty else hist
    changes = closes.pct_change() * 100
    return {d.strftime("%Y-%m-%d"): float(v) for d, v in changes.items() if v == v}


def _backfill_market_day(records: list[dict]) -> bool:
    """Fills market_day for evaluated records that don't have it yet (logged
    before 2026-10-06, or a day whose final run couldn't fetch the indices)."""
    missing = [r for r in records if r.get("evaluated") and not r.get("market_day") and r.get("logged_date")]
    if not missing:
        return False
    start = (datetime.fromisoformat(min(r["logged_date"] for r in missing)) - timedelta(days=10)).strftime("%Y-%m-%d")
    nikkei = _daily_change_pcts(NIKKEI_TICKER, start)
    topix = _daily_change_pcts(TOPIX_PROXY_TICKER, start)
    filled = False
    for record in missing:
        day = record.get("evaluated_date") or record["logged_date"]
        market_day = classify_market_day(nikkei.get(day), topix.get(day))
        if market_day:
            record["market_day"] = market_day
            record["nikkei_change_pct"] = round(nikkei[day], 2)
            record["topix_change_pct"] = round(topix[day], 2)
            filled = True
    return filled


# Market day (up / mixed / down) for every trading day, whether or not the
# 30-day-range had candidates that day. Before 2026-10-14 it was only stored
# on the evaluated candidate records, so days with 0 candidates (10/9, 10/13)
# had no record at all.
MARKET_DAY_LOG_PATH = Path("state") / "market_day_log.json"
MARKET_DAY_LOG_RETENTION_DAYS = 400
MARKET_DAY_BACKFILL_DAYS = 30


def _load_market_day_log(root: Path) -> list[dict]:
    data = _load_json(root / MARKET_DAY_LOG_PATH)
    return data if isinstance(data, list) else []


def _save_market_day_log(root: Path, entries: list[dict], now: datetime) -> None:
    cutoff = (now - timedelta(days=MARKET_DAY_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
    entries = sorted((e for e in entries if e.get("date", "") >= cutoff), key=lambda e: e["date"])
    path = root / MARKET_DAY_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def _todays_index_changes(today_date) -> tuple[float | None, float | None, str]:
    # Prefer the Yahoo!ファイナンス top page (real TOPIX, not the 1306
    # proxy); fall back to yfinance if the page can't be read.
    try:
        top = fetch_yahoo_top_indices()
        return top["nikkei"]["change_pct"], top["topix"]["change_pct"], "yahoo_top"
    except Exception as exc:
        logging.warning("[stock_range_eval] Yahoo!ファイナンス top unavailable, using yfinance: %s", exc)
        return (
            _fetch_today_change_pct(NIKKEI_TICKER, today_date),
            _fetch_today_change_pct(TOPIX_PROXY_TICKER, today_date),
            "yfinance",
        )


def _record_market_day(root: Path, now: datetime, nikkei: float | None, topix: float | None, source: str) -> None:
    """Records today's market day (after the close) and backfills recent
    trading days that are missing, from yfinance daily closes."""
    entries = {e["date"]: e for e in _load_market_day_log(root) if e.get("date")}
    today_label = now.strftime("%Y-%m-%d")
    market_day = classify_market_day(nikkei, topix)
    if market_day:
        entries[today_label] = {
            "date": today_label,
            "market_day": market_day,
            "nikkei_change_pct": round(nikkei, 2),
            "topix_change_pct": round(topix, 2),
            "source": source,
            "recorded_at": now.isoformat(),
        }
    start_date = now.date() - timedelta(days=MARKET_DAY_BACKFILL_DAYS)
    missing = [
        d for d in (start_date + timedelta(days=i) for i in range(MARKET_DAY_BACKFILL_DAYS))
        if is_trading_day(d) and d.strftime("%Y-%m-%d") not in entries
    ]
    if missing:
        start = (start_date - timedelta(days=10)).strftime("%Y-%m-%d")
        nikkei_days = _daily_change_pcts(NIKKEI_TICKER, start)
        topix_days = _daily_change_pcts(TOPIX_PROXY_TICKER, start)
        for d in missing:
            label = d.strftime("%Y-%m-%d")
            # yfinance sometimes shows the latest day as exactly 0.0% (or not
            # at all) before its daily bar is final; leave it for a later run.
            if 0.0 in (nikkei_days.get(label), topix_days.get(label)):
                continue
            backfilled = classify_market_day(nikkei_days.get(label), topix_days.get(label))
            if backfilled:
                entries[label] = {
                    "date": label,
                    "market_day": backfilled,
                    "nikkei_change_pct": round(nikkei_days[label], 2),
                    "topix_change_pct": round(topix_days[label], 2),
                    "source": "yfinance_daily_backfill",
                    "recorded_at": now.isoformat(),
                }
    _save_market_day_log(root, list(entries.values()), now)


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "stock_range_eval.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()
    today_label = now.strftime("%Y-%m-%d")

    _log_market_forecast_accuracy(root, now, today_label)

    is_final_run = now.time() >= MARKET_CLOSE_TIME
    index_changes = None
    if is_final_run and is_trading_day(now.date()):
        index_changes = _todays_index_changes(now.date())
        try:
            _record_market_day(root, now, *index_changes)
        except Exception as exc:
            logging.warning("[stock_range_eval] market day log failed: %s", exc)

    records = _load_predictions_log(root)
    if _backfill_market_day(records):
        _save_predictions_log(root, records)
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

    stage = "interim" if now.time() < MARKET_CLOSE_TIME else "final"

    today_date = now.date()
    market = {}
    if stage == "final":
        nikkei, topix, _source = index_changes or _todays_index_changes(today_date)
        market_day = classify_market_day(nikkei, topix)
        if market_day:
            market = {
                "market_day": market_day,
                "nikkei_change_pct": round(nikkei, 2),
                "topix_change_pct": round(topix, 2),
            }
    evaluated = []
    changed = False
    for record in todays_candidates:
        change_pct = _fetch_today_change_pct(record.get("ticker"), today_date)
        if change_pct is None:
            continue
        hit = bool(change_pct > 0)
        if stage == "interim":
            record["interim_hit"] = hit
            record["interim_change_pct"] = round(change_pct, 2)
            record["interim_evaluated_at"] = generated_at
            record["flipped"] = False
        else:
            previous_hit = record.get("interim_hit")
            record["evaluated"] = True
            record["evaluated_date"] = today_label
            record["actual_change_pct"] = round(change_pct, 2)
            record["hit"] = hit
            record.update(_fetch_today_open_close(record.get("ticker"), today_date))
            record["flipped"] = previous_hit is not None and previous_hit != hit
            record["previous_hit"] = previous_hit if record["flipped"] else None
            record.update(market)
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

    def _result_hit(r: dict) -> bool | None:
        return r.get("hit") if stage == "final" else r.get("interim_hit")

    def _result_change_pct(r: dict) -> float | None:
        return r.get("actual_change_pct") if stage == "final" else r.get("interim_change_pct")

    hit_count = sum(1 for r in evaluated if _result_hit(r))
    skipped_count = len(todays_candidates) - len(evaluated)
    results_payload = [
        {
            "type": r.get("type"),
            "ticker": r.get("ticker"),
            "name": r.get("name"),
            "score": r.get("score"),
            "reasons": r.get("reasons"),
            "actual_change_pct": _result_change_pct(r),
            "hit": _result_hit(r),
            "flipped": r.get("flipped", False),
            "previous_hit": r.get("previous_hit"),
        }
        for r in evaluated
    ]

    result = {
        "module": "stock_range_eval",
        "generated_at": generated_at,
        "status": "ok",
        "stage": stage,
        "evaluated_count": len(evaluated),
        "hit_count": hit_count,
        "skipped_count": skipped_count,
        "results": results_payload,
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[stock_range_eval] (%s) evaluated %d candidates (%d hits)", stage, len(evaluated), hit_count)


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
