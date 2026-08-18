from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf

from modules.stock_nikkei import _fetch_nikkei_futures_data
from modules.stock_watchlist import _range_position


JST = timezone(timedelta(hours=9), "JST")


def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _load_x_trend_hits(root: Path) -> dict[str, dict]:
    """Maps ticker code (e.g. "6857") to today's X-trend finding, so watchlist
    cards can show whether that ticker was also buzzing on X today - the
    correlation is just this same-day cross-reference, no separate history
    needs to be kept for it."""
    payload = _load_json(root / "output" / "stock_x_trends.json")
    if not payload or payload.get("status") != "ok" or not payload.get("data"):
        return {}
    try:
        generated_at = datetime.fromisoformat(str(payload.get("generated_at")))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=JST)
        if generated_at.astimezone(JST).date() != datetime.now(JST).date():
            return {}
    except (TypeError, ValueError):
        return {}

    hits: dict[str, dict] = {}
    for finding in payload["data"].get("stock_findings") or []:
        code = str(finding.get("ticker") or "").strip()
        if code:
            hits[code] = finding
    return hits


def _x_trend_finding(item: dict, x_trend_hits: dict[str, dict]) -> dict | None:
    code = str(item.get("ticker") or "").split(".")[0]
    return x_trend_hits.get(code)


_SENTIMENT_BONUS = {"strong_positive": 30, "positive": 20}


def _market_change_pct() -> float | None:
    """Overnight Nikkei futures move, used to down-weight candidates ahead
    of a broad-selloff day. Deliberately uses nikkei_futures, not
    nikkei_average: stock_range runs at 06:45, before the Tokyo cash
    session opens, so the day session's change_pct doesn't exist yet at
    scoring time - using it would be scoring with information from the
    future. A 2026-07-28 check showed the reversal signal (buy the 30-day
    low) going 0/9 on a day futures were down sharply overnight - deep-low
    stocks got sold even harder, not bought back, because the weakness was
    market-wide rather than stock-specific.

    Fetched live via yfinance rather than read from output/stock_nikkei.json:
    that file is only refreshed by stock_nikkei's own schedule slots
    (07:00/09:30/12:15/22:45), none of which run at 06:45, so reading it
    here would return the previous evening's snapshot - stale by up to 8
    hours of further overnight futures movement."""
    try:
        return float(_fetch_nikkei_futures_data()["change_pct"])
    except Exception as exc:
        logging.warning("[stock_range] nikkei futures fetch failed: %s", exc)
        return None


def _market_adjustment(market_change_pct: float | None, factor: float, cap: float) -> tuple[float, str]:
    """Symmetric market-condition adjustment: penalize on a down day, but
    also bonus on an up day. Added 2026-08-11 after the predictions log
    (state/stock_range_predictions.json) showed the same split holding up
    as the sample grew past 130 records - both candidate types hit more
    often and with a bigger average move on days the overnight futures
    were positive (momentum 60%/+2.31% vs 44%/-0.39%; reversal
    76%/+5.28% vs 41%/-0.50%) than the earlier penalty-only logic
    accounted for."""
    if market_change_pct is None:
        return 0.0, ""
    if market_change_pct >= 0:
        bonus = min(market_change_pct * factor, cap)
        if not bonus:
            return 0.0, ""
        return bonus, f"市場全体{market_change_pct:+.1f}%のため加点"
    penalty = max(market_change_pct * factor, -cap)
    return penalty, f"市場全体{market_change_pct:+.1f}%のため減点"


def _momentum_score(item: dict, x_trend_hits: dict[str, dict], market_change_pct: float | None = None) -> tuple[int, list[str]]:
    """Rule-based signal, not a prediction: recent uptrend + strength within
    the 30-day range + same-day positive X buzz. Purely mechanical scoring
    from data already computed elsewhere - no extra fetches."""
    range_info = item.get("range_30d") or {}
    score = 0
    reasons: list[str] = []

    if range_info.get("trend") == "up":
        score += 40
        reasons.append("直近5日上昇トレンド")
        change_pct = float(range_info.get("trend_change_pct") or 0)
        bonus = min(max(change_pct, 0) * 2, 20)
        if bonus:
            score += bonus
            reasons.append(f"上昇幅+{change_pct:.1f}%")

    position_pct = range_info.get("position_pct")
    if position_pct is not None and position_pct >= 70:
        score += 20
        reasons.append(f"30日レンジ上位({position_pct:.0f}%)")

    finding = _x_trend_finding(item, x_trend_hits)
    sentiment = str((finding or {}).get("sentiment") or "").strip()
    bonus = _SENTIMENT_BONUS.get(sentiment)
    if bonus:
        score += bonus
        reasons.append(f"当日Xで{sentiment}に話題")

    adjustment, adjustment_reason = _market_adjustment(market_change_pct, factor=4, cap=30)
    if adjustment:
        score += adjustment
        reasons.append(adjustment_reason)

    return max(min(round(score), 100), 0), reasons


def _reversal_score(item: dict, x_trend_hits: dict[str, dict], market_change_pct: float | None = None) -> tuple[int, list[str]]:
    """Rule-based signal, not a prediction: deep in the 30-day low range,
    no longer actively falling, plus same-day positive X buzz."""
    range_info = item.get("range_30d") or {}
    score = 0
    reasons: list[str] = []

    position_pct = range_info.get("position_pct")
    if position_pct is not None and position_pct <= 20:
        score += 40
        reasons.append(f"30日安値圏(位置{position_pct:.0f}%)")
        score += min((20 - position_pct) * 2, 20)

    if range_info.get("trend") and range_info.get("trend") != "down":
        score += 10
        reasons.append("下落一服")

    finding = _x_trend_finding(item, x_trend_hits)
    sentiment = str((finding or {}).get("sentiment") or "").strip()
    bonus = _SENTIMENT_BONUS.get(sentiment)
    if bonus:
        score += bonus
        reasons.append(f"当日Xで{sentiment}に話題")

    # Reversal candidates get a bigger market-condition swing than momentum
    # ones in both directions: "buy the 30-day low" assumes stock-specific
    # weakness, which is exactly the assumption a market-wide selloff (or
    # rally) breaks hardest.
    adjustment, adjustment_reason = _market_adjustment(market_change_pct, factor=8, cap=50)
    if adjustment:
        score += adjustment
        reasons.append(adjustment_reason)

    return max(min(round(score), 100), 0), reasons


def _score_candidates(items: list[dict], x_trend_hits: dict[str, dict], market_change_pct: float | None = None) -> tuple[list, list]:
    momentum = sorted(
        ((s, item, r) for item in items for s, r in [_momentum_score(item, x_trend_hits, market_change_pct)] if s >= 40),
        key=lambda entry: entry[0],
        reverse=True,
    )
    reversal = sorted(
        ((s, item, r) for item in items for s, r in [_reversal_score(item, x_trend_hits, market_change_pct)] if s >= 40),
        key=lambda entry: entry[0],
        reverse=True,
    )
    return momentum, reversal


PREDICTIONS_LOG_PATH = Path("state") / "stock_range_predictions.json"
PREDICTION_LOG_RETENTION_DAYS = 90


def _load_predictions_log(root: Path) -> list[dict]:
    path = root / PREDICTIONS_LOG_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _save_predictions_log(root: Path, records: list[dict]) -> None:
    path = root / PREDICTIONS_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def _evaluate_predictions(root: Path, today_label: str, items_by_ticker: dict[str, dict]) -> list[dict]:
    """Marks previously-logged candidates as hit/miss using the next fresh
    daily change_pct we see for that ticker - i.e. the price move that
    happened after the candidate was flagged. Same-day entries are left
    alone so a candidate never gets evaluated against the data that
    produced it."""
    records = _load_predictions_log(root)
    changed = False
    for record in records:
        if record.get("evaluated") or record.get("logged_date", "") >= today_label:
            continue
        item = items_by_ticker.get(record.get("ticker"))
        change_pct = item.get("change_pct") if item else None
        if change_pct is None:
            continue
        record["evaluated"] = True
        record["evaluated_date"] = today_label
        record["actual_change_pct"] = change_pct
        record["hit"] = bool(change_pct > 0)
        changed = True

    cutoff_label = (datetime.now(JST) - timedelta(days=PREDICTION_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
    trimmed = [r for r in records if r.get("logged_date", "9999-99-99") >= cutoff_label]
    if changed or len(trimmed) != len(records):
        _save_predictions_log(root, trimmed)
    return trimmed


def _append_predictions(
    root: Path,
    records: list[dict],
    today_label: str,
    momentum: list,
    reversal: list,
    x_trend_hits: dict[str, dict],
    market_change_pct: float | None,
) -> None:
    # Skip tickers already logged today under the same candidate type, so a
    # manual re-run doesn't double-count the same day's hit-rate stats.
    already_logged = {
        (r.get("type"), r.get("ticker"))
        for r in records
        if r.get("logged_date") == today_label
    }
    for kind, candidates in (("momentum", momentum), ("reversal", reversal)):
        for score, item, reasons in candidates:
            key = (kind, item.get("ticker"))
            if key in already_logged:
                continue
            range_info = item.get("range_30d") or {}
            finding = _x_trend_finding(item, x_trend_hits)
            records.append(
                {
                    "logged_date": today_label,
                    "type": kind,
                    "ticker": item.get("ticker"),
                    "name": item.get("name"),
                    "score": score,
                    "reasons": reasons,
                    # Structured scoring inputs, kept alongside the
                    # human-readable "reasons" text so hit-rate analysis
                    # doesn't need to parse strings back into numbers.
                    "position_pct": range_info.get("position_pct"),
                    "trend": range_info.get("trend"),
                    "trend_change_pct": range_info.get("trend_change_pct"),
                    "x_trend_sentiment": (finding or {}).get("sentiment"),
                    "market_change_pct": market_change_pct,
                    "evaluated": False,
                    "hit": None,
                    "actual_change_pct": None,
                }
            )
    _save_predictions_log(root, records)


def _hit_rate_stats(records: list[dict]) -> dict:
    def _stats(kind: str) -> dict:
        evaluated = [r for r in records if r.get("type") == kind and r.get("evaluated")]
        hits = sum(1 for r in evaluated if r.get("hit"))
        return {"hits": hits, "total": len(evaluated)}

    return {"momentum": _stats("momentum"), "reversal": _stats("reversal")}


def _candidate_payload(candidates: list[tuple[int, dict, list[str]]]) -> list[dict]:
    return [
        {
            "ticker": item.get("ticker"),
            "name": item.get("name"),
            "score": score,
            "reasons": reasons,
            "daily_changes": item.get("daily_changes"),
            "position_pct": (item.get("range_30d") or {}).get("position_pct"),
            "close": item.get("close"),
            "change": item.get("change"),
            "change_pct": item.get("change_pct"),
        }
        for score, item, reasons in candidates
    ]


INDEX_TARGETS = [
    {"ticker": "^N225", "name": "日経平均"},
    {"ticker": "1306.T", "name": "TOPIX"},
]


def _fetch_index_range_items() -> list[dict]:
    """30-day range for the broad-market indices themselves, so the range
    mail always has a market-wide reference card at the top - independent
    of the japan-stock-only momentum/reversal scoring below, which never
    includes these."""
    items = []
    for target in INDEX_TARGETS:
        try:
            ticker = yf.Ticker(target["ticker"])
            hist = ticker.history(period="30d", interval="1d", auto_adjust=False)
            hist = hist.dropna(subset=["Close"])
            if len(hist) < 2:
                continue
            latest = hist.iloc[-1]
            previous = hist.iloc[-2]
            close = float(latest["Close"])
            prev_close = float(previous["Close"])
            change = close - prev_close
            change_pct = (change / prev_close * 100) if prev_close else 0.0
            items.append(
                {
                    "ticker": target["ticker"],
                    "name": target["name"],
                    "market": "index",
                    "close": round(close, 2),
                    "change": round(change, 2),
                    "change_pct": round(change_pct, 2),
                    "range_30d": _range_position(hist),
                }
            )
        except Exception as exc:
            logging.warning("[stock_range] index range fetch failed for %s: %s", target["ticker"], exc)
    return items


def _item_payload(item: dict, x_trend_hits: dict[str, dict]) -> dict:
    finding = _x_trend_finding(item, x_trend_hits)
    return {
        "ticker": item.get("ticker"),
        "name": item.get("name"),
        "market": item.get("market"),
        "close": item.get("close"),
        "change": item.get("change"),
        "change_pct": item.get("change_pct"),
        "range_30d": item.get("range_30d"),
        "x_trend": (
            {
                "reason": finding.get("reason"),
                "sentiment": finding.get("sentiment"),
            }
            if finding
            else None
        ),
    }


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "stock_range.json"
    generated_at = datetime.now(JST).isoformat()

    payload = _load_json(root / "output" / "stock_watchlist.json")
    if not payload or payload.get("status") != "ok" or not payload.get("data"):
        result = {
            "module": "stock_range",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "stock_watchlist output is not available.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[stock_range] skipped: stock_watchlist output is not available")
        return

    items = [item for item in payload["data"] if item.get("range_30d")]
    items.sort(key=lambda item: item["range_30d"].get("trend_change_pct") or 0, reverse=True)
    if not items:
        result = {
            "module": "stock_range",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "No range_30d data found in stock_watchlist output.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[stock_range] skipped: no range_30d data")
        return

    x_trend_hits = _load_x_trend_hits(root)

    now = datetime.now(JST)
    today_label = now.strftime("%Y-%m-%d")
    items_by_ticker = {item.get("ticker"): item for item in items}
    prediction_records = _evaluate_predictions(root, today_label, items_by_ticker)
    market_change_pct = _market_change_pct()
    # Scoring candidates are Japan-listed stocks only: the momentum/reversal
    # score never had a market-appropriate way to compare US tickers against
    # the Nikkei-futures-based market penalty, so mixing markets here just
    # added noise. The per-ticker range cards below still cover all markets.
    japan_score_items = [item for item in items if item.get("market") == "japan"]
    momentum, reversal = _score_candidates(japan_score_items, x_trend_hits, market_change_pct)
    _append_predictions(root, prediction_records, today_label, momentum, reversal, x_trend_hits, market_change_pct)
    hit_rate = _hit_rate_stats(prediction_records)

    result = {
        "module": "stock_range",
        "generated_at": generated_at,
        "status": "ok",
        "ticker_count": len(items),
        "market_change_pct": market_change_pct,
        "index_items": _fetch_index_range_items(),
        "items": [_item_payload(item, x_trend_hits) for item in items],
        "momentum_candidates": _candidate_payload(momentum),
        "reversal_candidates": _candidate_payload(reversal),
        "hit_rate": hit_rate,
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[stock_range] scored %d tickers (%d momentum, %d reversal candidates)", len(items), len(momentum), len(reversal))


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
