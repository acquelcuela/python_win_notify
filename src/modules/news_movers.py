import csv
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf


JST = timezone(timedelta(hours=9), "JST")
DEFAULT_MAX_TICKERS = 12
COMPANY_CACHE_PATH = Path(".cache") / "listed_companies.json"
DEFAULT_ALIAS_FILE = "data/data_j_aliases.json"
ALLOWED_BOUNDARY_CHARS = set(" \t\r\n　、。，．・／/（）()[]【】「」『』:：;；+-－―—")
ALLOWED_BEFORE_CHARS = ALLOWED_BOUNDARY_CHARS | set("や")
ALLOWED_AFTER_CHARS = ALLOWED_BOUNDARY_CHARS | set("にはがをもやへでと")


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[news_movers] invalid JSON ignored: %s", path)
        return None


def _load_config(root: Path) -> dict:
    config = _load_json(root / "config.json")
    return config if isinstance(config, dict) else {}


def _data_file_path(root: Path, config: dict) -> Path:
    configured = config.get("news_movers", {}).get("data_file")
    if configured:
        path = Path(str(configured))
        return path if path.is_absolute() else root / path
    return root / "data" / "data_j.csv"


def _alias_file_path(root: Path, config: dict) -> Path:
    configured = config.get("news_movers", {}).get("alias_file", DEFAULT_ALIAS_FILE)
    path = Path(str(configured))
    return path if path.is_absolute() else root / path


def _read_listed_companies_csv(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Listed company data was not found: {path}")

    companies = []
    with path.open(encoding="utf-8", newline="") as file:
        rows = csv.reader(file)
        first = next(rows, None)
        if first == ["表1"]:
            headers = next(rows, None)
        else:
            headers = first
        if not headers:
            return []

        reader = csv.DictReader(file, fieldnames=headers)
        for row in reader:
            code = str(row.get("コード") or "").strip()
            name = str(row.get("銘柄名") or "").strip()
            market = str(row.get("市場・商品区分") or "").strip()
            if not code or not name:
                continue
            companies.append(
                {
                    "ticker": f"{code}.T",
                    "code": code,
                    "name": name,
                    "market_segment": market,
                }
            )
    return companies


def _load_aliases(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    payload = _load_json(path)
    if not payload:
        return {}

    aliases_by_ticker = {}
    for item in payload.get("aliases", []):
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").strip()
        aliases = item.get("aliases") or []
        if not ticker or not isinstance(aliases, list):
            continue
        aliases_by_ticker[ticker] = [
            str(alias).strip()
            for alias in aliases
            if str(alias).strip()
        ]
    return aliases_by_ticker


def _cache_is_fresh(cache_path: Path, source_path: Path) -> bool:
    if not cache_path.exists() or not source_path.exists():
        return False
    return cache_path.stat().st_mtime >= source_path.stat().st_mtime


def _load_listed_companies(root: Path, source_path: Path) -> list[dict]:
    cache_path = root / COMPANY_CACHE_PATH
    if _cache_is_fresh(cache_path, source_path):
        cached = _load_json(cache_path)
        if cached and isinstance(cached.get("data"), list):
            return cached["data"]

    companies = _read_listed_companies_csv(source_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_payload = {
        "source_file": str(source_path),
        "source_mtime": source_path.stat().st_mtime,
        "count": len(companies),
        "data": companies,
    }
    cache_path.write_text(json.dumps(cache_payload, ensure_ascii=False), encoding="utf-8")
    logging.info("[news_movers] rebuilt company cache: %s items", len(companies))
    return companies


def _is_name_char(value: str) -> bool:
    if not value:
        return False
    codepoint = ord(value)
    return (
        value.isalnum()
        or 0x3040 <= codepoint <= 0x30ff
        or 0x3400 <= codepoint <= 0x9fff
        or 0xff10 <= codepoint <= 0xff5a
    )


def _contains_company_name(title: str, name: str) -> bool:
    start = title.find(name)
    while start >= 0:
        end = start + len(name)
        before = title[start - 1] if start > 0 else ""
        after = title[end] if end < len(title) else ""
        before_ok = not _is_name_char(before) or before in ALLOWED_BEFORE_CHARS
        after_ok = not _is_name_char(after) or after in ALLOWED_AFTER_CHARS
        if before_ok and after_ok:
            return True
        start = title.find(name, start + 1)
    return False


def _match_terms(company: dict, aliases_by_ticker: dict[str, list[str]], min_name_length: int) -> list[str]:
    terms = []
    name = company["name"]
    if len(name) >= min_name_length:
        terms.append(name)
    for alias in aliases_by_ticker.get(company["ticker"], []):
        if len(alias) >= 2:
            terms.append(alias)
    return terms


def _matched_titles(
    company: dict,
    titles: list[str],
    aliases_by_ticker: dict[str, list[str]],
    min_name_length: int,
) -> list[str]:
    terms = _match_terms(company, aliases_by_ticker, min_name_length)
    if not terms:
        return []
    return [
        title for title in titles
        if any(_contains_company_name(title, term) for term in terms)
    ]


def _fetch_price(company: dict) -> dict:
    ticker_symbol = company["ticker"]
    ticker = yf.Ticker(ticker_symbol)
    hist = ticker.history(period="10d", interval="1d", auto_adjust=False)
    hist = hist.dropna(subset=["Close"])
    if len(hist) < 2:
        raise ValueError(f"Not enough price data returned for {ticker_symbol}.")

    latest = hist.iloc[-1]
    previous = hist.iloc[-2]
    close = float(latest["Close"])
    prev_close = float(previous["Close"])
    change = close - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0.0
    return {
        **company,
        "close": round(close, 2),
        "prev_close": round(prev_close, 2),
        "open": round(float(latest["Open"]), 2),
        "high": round(float(latest["High"]), 2),
        "low": round(float(latest["Low"]), 2),
        "change": round(change, 2),
        "change_pct": round(change_pct, 2),
        "volume": int(latest["Volume"]) if "Volume" in latest else None,
    }


def run(root: Path) -> None:
    output_dir = root / "output"
    cache_dir = root / ".cache" / "yfinance"
    output_dir.mkdir(exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(cache_dir))
    output_path = output_dir / "news_movers.json"
    generated_at = datetime.now(JST).isoformat()

    config = _load_config(root)
    module_config = config.get("news_movers", {})
    max_tickers = int(module_config.get("max_tickers", DEFAULT_MAX_TICKERS))
    min_name_length = int(module_config.get("min_name_length", 4))
    data_file = _data_file_path(root, config)
    alias_file = _alias_file_path(root, config)
    news = _load_json(root / "output" / "market_news.json")
    news_items = news.get("data") if news else None

    if not news_items:
        payload = {
            "module": "news_movers",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "market_news.json has no news data.",
            "data": [],
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[news_movers] skipped: no news data")
        return

    titles = [str(item.get("title") or "") for item in news_items]
    warnings = []
    try:
        companies = _load_listed_companies(root, data_file)
        aliases_by_ticker = _load_aliases(alias_file)
    except Exception as exc:
        payload = {
            "module": "news_movers",
            "generated_at": generated_at,
            "status": "error",
            "error": str(exc),
            "data": [],
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[news_movers] company data load failed: %s", exc)
        return

    matched = []
    for company in companies:
        company_titles = _matched_titles(company, titles, aliases_by_ticker, min_name_length)
        if company_titles:
            matched.append({**company, "matched_titles": company_titles[:3]})

    matched = matched[:max_tickers]
    results = []
    for company in matched:
        try:
            result = _fetch_price(company)
            result["matched_titles"] = company["matched_titles"]
            results.append(result)
            logging.info(
                "[news_movers] %s %s change=%+.2f%%",
                result["ticker"],
                result["name"],
                result["change_pct"],
            )
        except Exception as exc:
            warnings.append(f"{company['ticker']} {company['name']}: {exc}")
            logging.error("[news_movers] %s fetch failed: %s", company["ticker"], exc)

    results.sort(key=lambda item: item["change_pct"], reverse=True)
    payload = {
        "module": "news_movers",
        "generated_at": generated_at,
        "status": "ok",
        "data_file": str(data_file),
        "alias_file": str(alias_file),
        "matched_count": len(matched),
        "data": results,
    }
    if warnings:
        payload["warnings"] = warnings
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[news_movers] collected %s matched movers", len(results))
