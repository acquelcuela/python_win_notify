from __future__ import annotations

import json
import logging
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path

import yfinance as yf


JST = timezone(timedelta(hours=9), "JST")

# Before Tokyo's 15:00 close the read is partial-day/intraday, so it's
# treated as a non-final "interim" verdict - the log (used for long-term
# accuracy tracking) is only updated from the after-close "final" run, so a
# noon snapshot never gets locked in as the day's official outcome.
MARKET_CLOSE_TIME = dt_time(15, 0)

# strong_positive/positive findings imply "expect the stock to be up today";
# negative implies "expect it to be down". neutral has no directional claim
# to check, so it's excluded from evaluation entirely.
_EXPECTED_DIRECTION = {
    "strong_positive": "up",
    "positive": "up",
    "negative": "down",
}

# A move under this magnitude is treated as noise, not a real hit - e.g.
# +0.1% on an "up" call isn't meaningfully different from flat, so counting
# it as a hit overstates the signal. Symmetric for "down" calls.
HIT_MIN_MAGNITUDE_PCT = 0.5

PREDICTIONS_LOG_PATH = Path("state") / "stock_x_trends_predictions.json"
PREDICTION_LOG_RETENTION_DAYS = 90
HISTORY_DIR_NAME = "history"
HISTORY_RETENTION_DAYS = 30

# Durable record of each day's interim (pre-close) verdicts, keyed by
# trends_generated_at so the final run can diff against noon's actual
# result even if output/stock_x_trends_eval.json was overwritten in
# between (e.g. by a manual re-run) - unlike that output file, this state
# file is only ever written by the interim stage, never overwritten by a
# later run.
INTERIM_STATE_PATH = Path("state") / "stock_x_trends_interim.json"
INTERIM_STATE_RETENTION_DAYS = 3


def _archive_history(root: Path, payload: dict) -> None:
    """Keep a dated copy of each day's evaluation snapshot, mirroring
    stock_x_trends's own history archive - so which extraction a given day's
    evaluation judged (via trends_generated_at) can still be looked up later,
    not just the hit/miss tally in state/stock_x_trends_predictions.json."""
    history_dir = root / "output" / HISTORY_DIR_NAME
    history_dir.mkdir(parents=True, exist_ok=True)
    generated_at = payload.get("generated_at") or datetime.now(JST).isoformat()
    try:
        date_label = datetime.fromisoformat(str(generated_at)).astimezone(JST).strftime("%Y%m%d")
    except (TypeError, ValueError):
        date_label = datetime.now(JST).strftime("%Y%m%d")
    history_path = history_dir / f"stock_x_trends_eval_{date_label}.json"
    history_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    cutoff = datetime.now(JST) - timedelta(days=HISTORY_RETENTION_DAYS)
    for existing in history_dir.glob("stock_x_trends_eval_*.json"):
        try:
            file_date = datetime.strptime(existing.stem.split("_")[-1], "%Y%m%d").replace(tzinfo=JST)
        except ValueError:
            continue
        if file_date < cutoff:
            existing.unlink(missing_ok=True)


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


def _load_interim_state(root: Path) -> dict:
    path = root / INTERIM_STATE_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _save_interim_verdicts(root: Path, now: datetime, trends_generated_at: str | None, results: list[dict]) -> None:
    if not trends_generated_at:
        return
    path = root / INTERIM_STATE_PATH
    state = _load_interim_state(root)
    state[trends_generated_at] = {
        "logged_date": now.strftime("%Y-%m-%d"),
        "hit_by_ticker": {r["ticker"]: r["hit"] for r in results},
    }
    cutoff_label = (now - timedelta(days=INTERIM_STATE_RETENTION_DAYS)).strftime("%Y-%m-%d")
    trimmed = {
        key: value
        for key, value in state.items()
        if str(value.get("logged_date", "9999-99-99")) >= cutoff_label
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_interim_hit_by_ticker(root: Path, trends_generated_at: str | None) -> dict[str, bool | None]:
    if not trends_generated_at:
        return {}
    entry = _load_interim_state(root).get(trends_generated_at)
    return entry.get("hit_by_ticker", {}) if entry else {}


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

    stage = "interim" if now.time() < MARKET_CLOSE_TIME else "final"
    trends_generated_at = payload.get("generated_at")

    # Interim verdict per ticker for this same stock_x_trends generation,
    # used by the final run to detect a flip. Read from the durable interim
    # state file (not the previous output/stock_x_trends_eval.json), so a
    # manual re-run in between never clobbers what noon actually judged.
    previous_hit_by_ticker = _load_interim_hit_by_ticker(root, trends_generated_at)

    today_date = now.date()
    results = []
    for finding in findings:
        ticker = str(finding.get("ticker")).strip()
        sentiment = str(finding.get("sentiment") or "").strip()
        expected = _EXPECTED_DIRECTION.get(sentiment)
        change_pct = _fetch_today_change_pct(ticker, today_date)
        if change_pct is None:
            continue
        if expected == "up":
            hit = change_pct > HIT_MIN_MAGNITUDE_PCT
        elif expected == "down":
            hit = change_pct < -HIT_MIN_MAGNITUDE_PCT
        else:
            # neutral findings still show the actual move but no verdict.
            hit = None
        previous_hit = previous_hit_by_ticker.get(ticker)
        flipped = hit is not None and previous_hit is not None and previous_hit != hit
        results.append(
            {
                "ticker": ticker,
                "name": finding.get("name"),
                "sentiment": sentiment,
                "reason": finding.get("reason"),
                "actual_change_pct": round(change_pct, 2),
                "expected_direction": expected,
                "hit": hit,
                "flipped": flipped,
                "previous_hit": previous_hit if flipped else None,
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
    if stage == "interim":
        # Durable record of noon's verdicts, so the final run can detect a
        # flip even if this output file gets overwritten by another manual
        # run before 15:00.
        _save_interim_verdicts(root, now, trends_generated_at, results)
    else:
        # The long-term accuracy log only ever records the after-close
        # (final) outcome - an interim noon read must never get locked in
        # as the day's official result.
        _append_predictions_log(root, today_label, results)

    judged = [r for r in results if r["hit"] is not None]
    hit_count = sum(1 for r in judged if r["hit"])
    result = {
        "module": "stock_x_trends_eval",
        "generated_at": generated_at,
        "status": "ok",
        "stage": stage,
        # Ties this evaluation to the exact stock_x_trends.json generation it
        # judged, so a later fetch (e.g. the 23:00 reset) that produces a
        # differently-timestamped file is recognizable as not yet evaluated,
        # even if a ticker happens to reappear in the new list.
        "trends_generated_at": payload.get("generated_at"),
        "evaluated_count": len(judged),
        "hit_count": hit_count,
        "results": results,
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _archive_history(root, result)
    logging.info("[stock_x_trends_eval] (%s) evaluated %d findings (%d hits of %d judged)", stage, len(results), hit_count, len(judged))


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
