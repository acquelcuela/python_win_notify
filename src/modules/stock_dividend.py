import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf


JST = timezone(timedelta(hours=9), "JST")
TARGETS = [
    {
        "ticker": "RYLD",
        "name": "Global X Russell 2000 Covered Call ETF",
    },
    {
        "ticker": "SDIV",
        "name": "Global X SuperDividend ETF",
    },
]
CONFIG_PATH = "config.json"


def _load_dividend_config(root: Path) -> dict:
    path = root / CONFIG_PATH
    if not path.exists():
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[stock_dividend] config.json is invalid; manual dividend schedule ignored.")
        return {}
    schedule = config.get("dividend_schedule", {})
    if not isinstance(schedule, dict):
        return {}
    targets = schedule.get("targets", {})
    return targets if isinstance(targets, dict) else {}


def _to_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=JST).date()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _phase_for_days(days: int | None) -> tuple[str, str, str]:
    if days is None:
        return (
            "unknown",
            "manual_check",
            "次回の権利落ち日を取得できませんでした。手動確認してください。",
        )
    if days >= 30:
        return (
            "buy_window",
            "consider_buy",
            f"権利落ち日まであと{days}日です。早めの購入検討期間です。",
        )
    if 7 <= days <= 29:
        return (
            "buy_now",
            "buy_recommended",
            f"権利落ち日まであと{days}日です。購入候補として確認する時期です。",
        )
    if 1 <= days <= 6:
        return (
            "hold",
            "hold",
            f"権利落ち日まであと{days}日です。配当取りに向けて保有継続の時期です。",
        )
    if -7 <= days <= 0:
        return (
            "sell_start",
            "consider_sell",
            f"権利落ち日から{abs(days)}日経過しました。売却検討を始める時期です。",
        )
    if -30 <= days <= -8:
        return (
            "sell_now",
            "sell_recommended",
            f"権利落ち日から{abs(days)}日経過しました。売却候補として確認する時期です。",
        )
    return (
        "neutral",
        "wait",
        f"権利落ち日から{abs(days)}日経過しました。次の配当サイクル待ちです。",
    )


def _is_japanese_weekday(value: date) -> bool:
    return value.weekday() < 5


def _previous_japanese_weekday(value: date) -> date:
    current = value - timedelta(days=1)
    while not _is_japanese_weekday(current):
        current -= timedelta(days=1)
    return current


def _next_japanese_weekday(value: date) -> date:
    current = value + timedelta(days=1)
    while not _is_japanese_weekday(current):
        current += timedelta(days=1)
    return current


def _weekday_jp(value: date) -> str:
    return ["月", "火", "水", "木", "金", "土", "日"][value.weekday()]


def _fmt_jp_date(value: date | None) -> str | None:
    if value is None:
        return None
    return f"{value.isoformat()}（{_weekday_jp(value)}）"


def _build_timing_plan(ex_dividend_date: date | None) -> dict:
    if ex_dividend_date is None:
        return {
            "buy_deadline": None,
            "hold_date": None,
            "sell_from": None,
            "summary": "権利落ち日が未設定のため、売買目安を作成できません。",
        }

    buy_deadline = _previous_japanese_weekday(ex_dividend_date)
    sell_from = _next_japanese_weekday(ex_dividend_date)
    return {
        "buy_deadline": buy_deadline.isoformat(),
        "buy_deadline_label": _fmt_jp_date(buy_deadline),
        "hold_date": ex_dividend_date.isoformat(),
        "hold_date_label": _fmt_jp_date(ex_dividend_date),
        "sell_from": sell_from.isoformat(),
        "sell_from_label": _fmt_jp_date(sell_from),
        "summary": (
            f"{_fmt_jp_date(buy_deadline)}までに買い増し、"
            f"{_fmt_jp_date(ex_dividend_date)}はHOLD、"
            f"{_fmt_jp_date(sell_from)}以降は売却検討可。"
        ),
    }


def _latest_close(ticker: yf.Ticker) -> float | None:
    hist = ticker.history(period="5d", interval="1d", auto_adjust=False)
    if hist.empty:
        return None
    return round(float(hist["Close"].dropna().iloc[-1]), 2)


def _get_info_value(info: dict, *keys):
    for key in keys:
        value = info.get(key)
        if value not in (None, "", 0):
            return value
    return None


def _fetch_market_data(ticker_symbol: str) -> tuple[dict, str | None]:
    try:
        ticker = yf.Ticker(ticker_symbol)
        info = ticker.get_info()
        return {
            "info": info,
            "current_price": _latest_close(ticker),
            "error": None,
        }, None
    except Exception as exc:
        return {
            "info": {},
            "current_price": None,
            "error": str(exc),
        }, str(exc)


def _fetch_target(target: dict, manual_config: dict) -> tuple[dict, str | None]:
    ticker_symbol = target["ticker"]
    manual = manual_config.get(ticker_symbol, {})
    if not isinstance(manual, dict):
        manual = {}
    market, market_error = _fetch_market_data(ticker_symbol)
    info = market["info"]
    today = datetime.now(JST).date()

    manual_ex_dividend_date = _to_date(manual.get("ex_dividend_date"))
    market_ex_dividend_date = _to_date(_get_info_value(info, "exDividendDate", "ex_dividend_date"))
    ex_dividend_date = manual_ex_dividend_date or market_ex_dividend_date
    date_source = "manual_config" if manual_ex_dividend_date else "yfinance"
    if not ex_dividend_date:
        date_source = "unavailable"

    days_to_ex_dividend = None
    if ex_dividend_date:
        days_to_ex_dividend = (ex_dividend_date - today).days

    phase, action, message = _phase_for_days(days_to_ex_dividend)
    dividend_yield = _get_info_value(info, "dividendYield", "trailingAnnualDividendYield")
    if dividend_yield is not None:
        dividend_yield = round(float(dividend_yield) * 100, 2)

    last_dividend = _get_info_value(info, "lastDividendValue", "trailingAnnualDividendRate")
    if last_dividend is None:
        last_dividend = manual.get("last_dividend")
    if last_dividend is not None:
        last_dividend = round(float(last_dividend), 4)

    result = {
        "ticker": ticker_symbol,
        "name": info.get("shortName") or info.get("longName") or manual.get("name") or target["name"],
        "current_price": market["current_price"],
        "ex_dividend_date": ex_dividend_date.isoformat() if ex_dividend_date else None,
        "days_to_ex_dividend": days_to_ex_dividend,
        "dividend_yield": dividend_yield,
        "last_dividend": last_dividend,
        "phase": phase,
        "action": action,
        "message": message,
        "timing_plan": _build_timing_plan(ex_dividend_date),
        "date_source": date_source,
        "date_confidence": manual.get("date_confidence") if manual_ex_dividend_date else None,
        "source": manual.get("source") if manual_ex_dividend_date else "yfinance",
    }
    if market_error:
        result["market_data_error"] = market_error
    return result, market_error


def run(root: Path) -> None:
    output_dir = root / "output"
    cache_dir = root / ".cache" / "yfinance"
    output_dir.mkdir(exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(cache_dir))
    output_path = output_dir / "stock_dividend.json"
    generated_at = datetime.now(JST).isoformat()
    manual_config = _load_dividend_config(root)

    results = []
    warnings = []
    for target in TARGETS:
        try:
            result, market_error = _fetch_target(target, manual_config)
            results.append(result)
            if market_error:
                warnings.append(
                    f"{target['ticker']}: 市場データを取得できないため、config.json の権利落ち日で判定しました。{market_error}"
                )
            logging.info(
                "[stock_dividend] %s phase=%s ex_dividend_date=%s",
                result["ticker"],
                result["phase"],
                result["ex_dividend_date"] or "-",
            )
        except Exception as exc:
            warnings.append(f"{target['ticker']}: {exc}")
            logging.error("[stock_dividend] %s fetch failed: %s", target["ticker"], exc)

    if results:
        payload = {
            "module": "stock_dividend",
            "generated_at": generated_at,
            "status": "ok",
            "data": results,
        }
        if warnings:
            payload["warnings"] = warnings
    else:
        payload = {
            "module": "stock_dividend",
            "generated_at": generated_at,
            "status": "error",
            "error": "; ".join(warnings) if warnings else "No dividend data returned.",
            "data": None,
        }

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
