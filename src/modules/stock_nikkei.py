import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf


JST = timezone(timedelta(hours=9), "JST")
SYMBOL = "NKD=F"
TOPIX_SYMBOL = "1306.T"


def _normalize_index_timezone(hist):
    if hist.index.tz is None:
        return hist.index.tz_localize("UTC").tz_convert(JST)
    return hist.index.tz_convert(JST)


def _fetch_nikkei_futures_data() -> dict:
    ticker = yf.Ticker(SYMBOL)
    hist = ticker.history(period="5d", interval="1h", auto_adjust=False)
    if hist.empty:
        raise ValueError(f"No price data returned for {SYMBOL}.")

    hist.index = _normalize_index_timezone(hist)

    now = datetime.now(JST)
    session_start = (now - timedelta(days=1)).replace(
        hour=16,
        minute=30,
        second=0,
        microsecond=0,
    )
    session = hist[hist.index >= session_start]
    before_session = hist[hist.index < session_start]

    if session.empty:
        logging.warning("[stock_nikkei] No session data; using latest 10 rows as fallback.")
        session = hist.tail(10)

    if before_session.empty:
        prev_close = float(hist["Close"].iloc[0])
    else:
        prev_close = float(before_session["Close"].iloc[-1])

    current = float(session["Close"].iloc[-1])
    change = current - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0.0

    return {
        "symbol": SYMBOL,
        "label": "日経225先物",
        "current": round(current, 0),
        "open": round(float(session["Open"].iloc[0]), 0),
        "high": round(float(session["High"].max()), 0),
        "low": round(float(session["Low"].min()), 0),
        "prev_close": round(prev_close, 0),
        "change": round(change, 0),
        "change_pct": round(change_pct, 2),
        "volume": int(session["Volume"].sum()),
    }


def _fetch_topix_data() -> dict:
    ticker = yf.Ticker(TOPIX_SYMBOL)
    hist = ticker.history(period="10d", interval="1d", auto_adjust=False)
    hist = hist.dropna(subset=["Close"])
    if len(hist) < 2:
        raise ValueError(f"No enough price data returned for {TOPIX_SYMBOL}.")

    latest = hist.iloc[-1]
    previous = hist.iloc[-2]
    current = float(latest["Close"])
    prev_close = float(previous["Close"])
    change = current - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0.0

    return {
        "symbol": TOPIX_SYMBOL,
        "label": "TOPIX連動ETF",
        "current": round(current, 2),
        "open": round(float(latest["Open"]), 2),
        "high": round(float(latest["High"]), 2),
        "low": round(float(latest["Low"]), 2),
        "prev_close": round(prev_close, 2),
        "change": round(change, 2),
        "change_pct": round(change_pct, 2),
        "volume": int(latest["Volume"]) if "Volume" in latest else None,
    }


def _build_comparison(nikkei: dict | None, topix: dict | None) -> dict | None:
    if not nikkei or not topix:
        return None

    diff_pct = round(float(nikkei["change_pct"]) - float(topix["change_pct"]), 2)
    if diff_pct > 0.1:
        summary = "日経225先物がTOPIX連動ETFより強い動きです。"
    elif diff_pct < -0.1:
        summary = "TOPIX連動ETFが日経225先物より強い動きです。"
    else:
        summary = "日経225先物とTOPIX連動ETFはほぼ同じ方向感です。"

    return {
        "diff_pct": diff_pct,
        "summary": summary,
    }


def run(root: Path) -> None:
    output_dir = root / "output"
    cache_dir = root / ".cache" / "yfinance"
    output_dir.mkdir(exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(cache_dir))
    output_path = output_dir / "stock_nikkei.json"
    generated_at = datetime.now(JST).isoformat()

    data = {}
    warnings = []

    try:
        nikkei = _fetch_nikkei_futures_data()
        data.update(nikkei)
        data["indices"] = {"nikkei_futures": nikkei}
    except Exception as exc:
        nikkei = None
        warnings.append(f"{SYMBOL}: {exc}")
        logging.error("[stock_nikkei] nikkei futures fetch failed: %s", exc)

    try:
        topix = _fetch_topix_data()
        data.setdefault("indices", {})["topix"] = topix
    except Exception as exc:
        topix = None
        warnings.append(f"{TOPIX_SYMBOL}: {exc}")
        logging.error("[stock_nikkei] topix fetch failed: %s", exc)

    comparison = _build_comparison(nikkei, topix)
    if comparison:
        data["comparison"] = comparison

    if data.get("indices"):
        payload = {
            "module": "stock_nikkei",
            "generated_at": generated_at,
            "status": "ok",
            "data": data,
        }
        if warnings:
            payload["warnings"] = warnings
        for key, item in data["indices"].items():
            logging.info(
                "[stock_nikkei] %s current=%s change=%+.2f%%",
                key,
                f"{float(item['current']):,.2f}",
                item["change_pct"],
            )
    else:
        payload = {
            "module": "stock_nikkei",
            "generated_at": generated_at,
            "status": "error",
            "error": "; ".join(warnings) if warnings else "No index data returned.",
            "data": None,
        }

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
