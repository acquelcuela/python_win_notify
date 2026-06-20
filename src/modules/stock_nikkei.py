import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf


JST = timezone(timedelta(hours=9), "JST")
SYMBOL = "NKD=F"


def _fetch_nikkei_data() -> dict:
    ticker = yf.Ticker(SYMBOL)
    hist = ticker.history(period="5d", interval="1h", auto_adjust=False)
    if hist.empty:
        raise ValueError(f"No price data returned for {SYMBOL}.")

    if hist.index.tz is None:
        hist.index = hist.index.tz_localize("UTC").tz_convert(JST)
    else:
        hist.index = hist.index.tz_convert(JST)

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
        "current": round(current, 0),
        "open": round(float(session["Open"].iloc[0]), 0),
        "high": round(float(session["High"].max()), 0),
        "low": round(float(session["Low"].min()), 0),
        "prev_close": round(prev_close, 0),
        "change": round(change, 0),
        "change_pct": round(change_pct, 2),
        "volume": int(session["Volume"].sum()),
    }


def run(root: Path) -> None:
    output_dir = root / "output"
    cache_dir = root / ".cache" / "yfinance"
    output_dir.mkdir(exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(cache_dir))
    output_path = output_dir / "stock_nikkei.json"
    generated_at = datetime.now(JST).isoformat()

    try:
        data = _fetch_nikkei_data()
        payload = {
            "module": "stock_nikkei",
            "generated_at": generated_at,
            "status": "ok",
            "data": data,
        }
        logging.info(
            "[stock_nikkei] current=%s change=%+s (%+.2f%%)",
            f"{int(data['current']):,}",
            f"{int(data['change']):,}",
            data["change_pct"],
        )
    except Exception as exc:
        payload = {
            "module": "stock_nikkei",
            "generated_at": generated_at,
            "status": "error",
            "error": str(exc),
            "data": None,
        }
        logging.error("[stock_nikkei] fetch failed: %s", exc)

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
