"""Saves each trading day's 5-minute bars for the stocks we analyse
(user request 2026-10-22), so time-of-day rules ("buy at 10:00", "decide at
12:15") can be tested over months later - yfinance only serves 5-minute bars
for about the last 60 days, so anything not saved is gone.

Runs after the close (17:30 slot). Covers the Nikkei 225, the TOPIX ETF 1306,
the watchlist, holdings (config.local.json), today's 30-day-range candidates
and today's X-trend stocks. One gzip JSON per day:

  state/intraday_archive/YYYY-MM-DD.json.gz
  {"date", "saved_at", "symbols": {ticker: {"name", "groups": [...],
   "previous_close", "bars": [["09:00", open, high, low, close, volume], ...]}}}
"""
from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.local_config import load_config
from modules.market_calendar import is_trading_day

JST = timezone(timedelta(hours=9), "JST")
ARCHIVE_DIR = Path("state") / "intraday_archive"
INDEXES = {"^N225": "日経平均", "1306.T": "TOPIX連動ETF(1306)"}


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _targets(root: Path, today: str) -> dict[str, dict]:
    """ticker -> {"name", "groups"} for everything worth keeping today."""
    targets: dict[str, dict] = {}

    def add(ticker, name, group):
        if not ticker:
            return
        entry = targets.setdefault(ticker, {"name": name, "groups": []})
        if group not in entry["groups"]:
            entry["groups"].append(group)

    for ticker, name in INDEXES.items():
        add(ticker, name, "指数")
    stock_range = _load_json(root / "output" / "history" / "stock_range" / f"stock_range_{today.replace('-', '')}.json") \
        or _load_json(root / "output" / "stock_range.json") or {}
    if str(stock_range.get("generated_at", "")).startswith(today):
        for item in stock_range.get("items") or []:
            if item.get("market") == "japan":
                add(item.get("ticker"), item.get("name"), "注目銘柄")
        for kind in ("momentum", "reversal"):
            for c in stock_range.get(f"{kind}_candidates") or []:
                if str(c.get("ticker", "")).endswith(".T"):
                    add(c["ticker"], c.get("name"), f"30日レンジ候補({kind})")
    for h in (load_config(root).get("holdings") or {}).get("items") or []:
        add(h.get("ticker"), h.get("name"), "保有銘柄")
    x_run = _load_json(root / "output" / "history" / "stock_x_trends_runs" / f"stock_x_trends_{today.replace('-', '')}_0700.json") or {}
    for f in (x_run.get("data") or x_run).get("stock_findings") or []:
        if f.get("ticker"):
            add(f"{f['ticker']}.T", f.get("name"), f"Xトレンド({f.get('sentiment')})")
    return targets


def _download(tickers: list[str], today) -> dict[str, dict]:
    import yfinance as yf

    intra = yf.download(tickers, period="1d", interval="5m", progress=False, auto_adjust=False, group_by="ticker", threads=True)
    daily = yf.download(tickers, period="5d", interval="1d", progress=False, auto_adjust=False, group_by="ticker", threads=True)
    result = {}
    for ticker in tickers:
        try:
            bars = intra[ticker].dropna(subset=["Close"])
            bars = bars[[i.date() == today for i in bars.index]]
            if bars.empty:
                continue
            day = daily[ticker].dropna(subset=["Close"])
            prev = day[[i.date() < today for i in day.index]]
            result[ticker] = {
                "previous_close": round(float(prev["Close"].iloc[-1]), 4) if not prev.empty else None,
                "bars": [
                    [i.tz_convert(JST).strftime("%H:%M") if i.tzinfo else i.strftime("%H:%M"),
                     round(float(r["Open"]), 4), round(float(r["High"]), 4), round(float(r["Low"]), 4),
                     round(float(r["Close"]), 4), int(r["Volume"]) if r["Volume"] == r["Volume"] else None]
                    for i, r in bars.iterrows()
                ],
            }
        except Exception as exc:  # one bad symbol shouldn't lose the rest
            logging.warning("[intraday_archive] %s failed: %s", ticker, exc)
    return result


def run(root: Path) -> None:
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")
    output_path = root / "output" / "intraday_archive.json"
    output_path.parent.mkdir(exist_ok=True)
    result = {"module": "intraday_archive", "generated_at": now.isoformat(), "date": today}
    if not is_trading_day(now.date()):
        result.update(status="skipped", reason="not a trading day")
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[intraday_archive] skipped: not a trading day")
        return
    targets = _targets(root, today)
    data = _download(sorted(targets), now.date())
    if "^N225" not in data:
        result.update(status="skipped", reason="no Nikkei bars for today (holiday or data not ready)")
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.warning("[intraday_archive] no Nikkei 5m bars for %s; nothing saved", today)
        return
    archive = {"date": today, "saved_at": now.isoformat(),
               "symbols": {t: {**targets[t], **v} for t, v in data.items()}}
    path = root / ARCHIVE_DIR / f"{today}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(archive, f, ensure_ascii=False)
    missing = sorted(set(targets) - set(data))
    result.update(status="ok", path=str(path), symbol_count=len(data), missing=missing, bytes=path.stat().st_size)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[intraday_archive] saved %d symbols (%d KB) to %s%s", len(data), path.stat().st_size // 1024, path,
                 f"; missing {len(missing)}: {', '.join(missing[:10])}" if missing else "")


def backfill(root: Path) -> None:
    """One-off: saves the past ~60 days that yfinance still serves (run by
    hand: python -m modules.intraday_archive --backfill). Uses each day's own
    stock_range / X-trend history files where they exist, and today's
    holdings. Days that already have a file are left alone."""
    import yfinance as yf

    now = datetime.now(JST)
    days = [d for d in ((now - timedelta(days=i)).date() for i in range(1, 59)) if is_trading_day(d)]
    targets_by_day = {d: _targets(root, d.strftime("%Y-%m-%d")) for d in days}
    # Days before the stock_range history began have no watchlist of their
    # own; use the current one so the watchlist is covered on every day.
    current = _load_json(root / "output" / "stock_range.json") or {}
    for targets in targets_by_day.values():
        for item in current.get("items") or []:
            if item.get("market") == "japan" and item.get("ticker") not in targets:
                targets[item["ticker"]] = {"name": item.get("name"), "groups": ["注目銘柄"]}
    tickers = sorted({t for ts in targets_by_day.values() for t in ts})
    intra = yf.download(tickers, period="60d", interval="5m", progress=False, auto_adjust=False, group_by="ticker", threads=True)
    daily = yf.download(tickers, period="4mo", interval="1d", progress=False, auto_adjust=False, group_by="ticker", threads=True)
    for d in days:
        path = root / ARCHIVE_DIR / f"{d:%Y-%m-%d}.json.gz"
        if path.exists():
            continue
        symbols = {}
        for ticker, meta in targets_by_day[d].items():
            try:
                bars = intra[ticker].dropna(subset=["Close"])
                bars = bars[[i.tz_convert(JST).date() == d for i in bars.index]]
                if bars.empty:
                    continue
                day = daily[ticker].dropna(subset=["Close"])
                prev = day[[i.date() < d for i in day.index]]
                symbols[ticker] = {**meta,
                    "previous_close": round(float(prev["Close"].iloc[-1]), 4) if not prev.empty else None,
                    "bars": [[i.tz_convert(JST).strftime("%H:%M"), round(float(r["Open"]), 4), round(float(r["High"]), 4),
                              round(float(r["Low"]), 4), round(float(r["Close"]), 4),
                              int(r["Volume"]) if r["Volume"] == r["Volume"] else None] for i, r in bars.iterrows()]}
            except Exception as exc:
                logging.warning("[intraday_archive] backfill %s %s failed: %s", d, ticker, exc)
        if "^N225" not in symbols:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump({"date": f"{d:%Y-%m-%d}", "saved_at": now.isoformat(), "backfilled": True, "symbols": symbols}, f, ensure_ascii=False)
        logging.info("[intraday_archive] backfilled %s: %d symbols", d, len(symbols))


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    project_root = Path(__file__).resolve().parents[1]
    backfill(project_root) if "--backfill" in sys.argv else run(project_root)
