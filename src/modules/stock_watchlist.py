import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf


JST = timezone(timedelta(hours=9), "JST")
DEFAULT_TARGETS = [
    {"ticker": "7203.T", "name": "トヨタ自動車"},
    {"ticker": "9983.T", "name": "ファーストリテイリング"},
    {"ticker": "8035.T", "name": "東京エレクトロン"},
    {"ticker": "1570.T", "name": "NEXT FUNDS 日経平均レバレッジ"},
    {"ticker": "AAPL", "name": "Apple"},
    {"ticker": "NVDA", "name": "NVIDIA"},
]


def _load_targets(root: Path) -> list[dict]:
    config_path = root / "config.json"
    if not config_path.exists():
        return DEFAULT_TARGETS
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[stock_watchlist] config.json is invalid; using default watchlist.")
        return DEFAULT_TARGETS

    raw_targets = config.get("watchlist", {}).get("tickers", [])
    if not isinstance(raw_targets, list) or not raw_targets:
        return DEFAULT_TARGETS

    targets = []
    for item in raw_targets:
        if isinstance(item, str):
            ticker = item.strip()
            if ticker:
                targets.append({"ticker": ticker, "name": ticker})
        elif isinstance(item, dict):
            ticker = str(item.get("ticker", "")).strip()
            if ticker:
                targets.append({"ticker": ticker, "name": str(item.get("name") or ticker)})
    return targets or DEFAULT_TARGETS


RECENT_TREND_WINDOW_DAYS = 5
RECENT_TREND_FLAT_THRESHOLD_PCT = 1.0


def _recent_trend(closes) -> dict:
    """Direction of the last few trading days, so a mid-range position can
    still say whether price is currently climbing or falling."""
    window = min(RECENT_TREND_WINDOW_DAYS, len(closes) - 1)
    if window < 1:
        return {"trend": "unknown", "trend_days": 0, "trend_change_pct": 0.0}

    recent_start = float(closes.iloc[-(window + 1)])
    current = float(closes.iloc[-1])
    trend_change_pct = ((current - recent_start) / recent_start * 100) if recent_start else 0.0

    if trend_change_pct > RECENT_TREND_FLAT_THRESHOLD_PCT:
        trend = "up"
    elif trend_change_pct < -RECENT_TREND_FLAT_THRESHOLD_PCT:
        trend = "down"
    else:
        trend = "flat"

    return {
        "trend": trend,
        "trend_days": window,
        "trend_change_pct": round(trend_change_pct, 2),
    }


def _range_position(hist) -> dict:
    """30-day trading range summary derived from the same fetch used for the
    daily-change table, so this adds no extra yfinance calls."""
    closes = hist["Close"]
    start_price = float(closes.iloc[0])
    current_price = float(closes.iloc[-1])
    change = current_price - start_price
    change_pct = (change / start_price * 100) if start_price else 0.0

    high_price = float(closes.max())
    low_price = float(closes.min())
    high_date = closes.idxmax().date().isoformat()
    low_date = closes.idxmin().date().isoformat()

    if high_price != low_price:
        position_pct = (current_price - low_price) / (high_price - low_price) * 100
    else:
        position_pct = 50.0

    distance_from_low_pct = ((current_price - low_price) / low_price * 100) if low_price else 0.0
    distance_from_high_pct = ((current_price - high_price) / high_price * 100) if high_price else 0.0

    return {
        "trading_days": len(closes),
        "start_date": closes.index[0].date().isoformat(),
        "start_price": round(start_price, 2),
        "change_since_start": round(change, 2),
        "change_pct_since_start": round(change_pct, 2),
        "high_price": round(high_price, 2),
        "high_date": high_date,
        "low_price": round(low_price, 2),
        "low_date": low_date,
        "position_pct": round(position_pct, 1),
        "distance_from_low_pct": round(distance_from_low_pct, 2),
        "distance_from_high_pct": round(distance_from_high_pct, 2),
        **_recent_trend(closes),
    }


def _fetch_target(target: dict) -> dict:
    ticker_symbol = target["ticker"]
    ticker = yf.Ticker(ticker_symbol)
    hist = ticker.history(period="30d", interval="1d", auto_adjust=False)
    hist = hist.dropna(subset=["Close"])
    if len(hist) < 2:
        raise ValueError(f"Not enough price data returned for {ticker_symbol}.")

    latest = hist.iloc[-1]
    previous = hist.iloc[-2]
    close = float(latest["Close"])
    prev_close = float(previous["Close"])
    change = close - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0.0
    daily_changes = []
    closes = hist["Close"].tail(11)
    for idx in range(len(closes) - 1, 0, -1):
        current = float(closes.iloc[idx])
        previous_close = float(closes.iloc[idx - 1])
        delta = current - previous_close
        delta_pct = (delta / previous_close * 100) if previous_close else 0.0
        current_date = closes.index[idx].date().isoformat()
        previous_date = closes.index[idx - 1].date().isoformat()
        daily_changes.append(
            {
                "label": f"{previous_date} → {current_date}",
                "change": round(delta, 2),
                "change_pct": round(delta_pct, 2),
            }
        )

    info = {}
    try:
        info = ticker.get_info()
    except Exception as exc:
        logging.warning("[stock_watchlist] %s name lookup failed: %s", ticker_symbol, exc)

    return {
        "ticker": ticker_symbol,
        "name": target["name"] or info.get("shortName") or info.get("longName") or ticker_symbol,
        "market": "japan" if ticker_symbol.endswith(".T") else "us",
        "close": round(close, 2),
        "prev_close": round(prev_close, 2),
        "open": round(float(latest["Open"]), 2),
        "high": round(float(latest["High"]), 2),
        "low": round(float(latest["Low"]), 2),
        "change": round(change, 2),
        "change_pct": round(change_pct, 2),
        "daily_changes": daily_changes,
        "volume": int(latest["Volume"]) if "Volume" in latest else None,
        "range_30d": _range_position(hist),
    }


def run(root: Path) -> None:
    output_dir = root / "output"
    cache_dir = root / ".cache" / "yfinance"
    output_dir.mkdir(exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(cache_dir))
    output_path = output_dir / "stock_watchlist.json"
    generated_at = datetime.now(JST).isoformat()

    results = []
    warnings = []
    for target in _load_targets(root):
        try:
            result = _fetch_target(target)
            results.append(result)
            logging.info(
                "[stock_watchlist] %s close=%s change=%+.2f%%",
                result["ticker"],
                result["close"],
                result["change_pct"],
            )
        except Exception as exc:
            warnings.append(f"{target['ticker']}: {exc}")
            logging.error("[stock_watchlist] %s fetch failed: %s", target["ticker"], exc)

    if results:
        results.sort(key=lambda item: item["change_pct"], reverse=True)
        payload = {
            "module": "stock_watchlist",
            "generated_at": generated_at,
            "status": "ok",
            "data": results,
        }
        if warnings:
            payload["warnings"] = warnings
    else:
        payload = {
            "module": "stock_watchlist",
            "generated_at": generated_at,
            "status": "error",
            "error": "; ".join(warnings) if warnings else "No watchlist data returned.",
            "data": None,
        }

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
