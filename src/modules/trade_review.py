"""売買考察: 30日レンジの候補とXトレンド銘柄を、検証済みのルールに当てはめて
考察し、テキストに残してメールする(2026-10-08、手作業でやっていた考察のバッチ化)。

  08:00 朝       - 07:00レポートの内容(市場が開く前)
  09:45 寄り付き後 - 寄り付きの値の飛びと最初の30〜45分で、10時の判断材料を出す
  13:00 昼   - 前場の結果で朝の考察を答え合わせ
  18:00 夕方 - 15時の値と終値で1日を振り返る

数字の収集は Python(このモジュール)、文章は Claude(`claude -p`、ツールなし)。
判断のルールは docs/rule/stock_range_analytics_rule.md をそのまま渡すので、
ルールを更新すれば考察も追従する。プロンプトは prompts/trade_review/ で編集できる。

出力(ほかのモジュールと同じく「データ → JSON → メール用モジュール」):
  output/trade_review.json                 - 集めたデータ・メール用の文章・ログ用の文章(trade_review_mail が読んで送る)
  output/history/trade_review/*.json       - 実行ごとの写し(+ *_prompt.txt に渡したプロンプト)、120日
  <notes_dir>/YYYY-MM-DD_0800_考察.txt 等  - 人が読む用(既定は リポジトリ直下の trade_notes/、git管理外)

    python -m modules.trade_review --stage morning|opening|midday|evening   # 手動実行(メールは送らない)
    python -m modules.trade_review_mail [--resend]                         # そのあとメール
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.llm_client import _call_claude
from modules.local_config import load_config

JST = timezone(timedelta(hours=9), "JST")
WEEKDAYS_JP = "月火水木金土日"
STAGES = {
    # stage: (label, file suffix, prompt file)
    "morning": ("朝", "0800_考察", "morning.md"),
    "opening": ("寄り付き後", "0945_考察", "opening.md"),
    "midday": ("昼", "1300_考察", "midday.md"),
    "evening": ("夕方", "1800_振り返り", "evening.md"),
}
PROMPT_DIR = Path("prompts") / "trade_review"
RULE_FILE = Path("docs") / "rule" / "stock_range_analytics_rule.md"
GAP_LOG_PATH = Path("state") / "trade_review_gap_log.json"
DEFAULT_TIMEOUT_SECONDS = 420
BIG_GAP_PCT = 3.0
# Checkpoints for the hindsight "best time to buy and sell" (09:00 = the open).
CHECKPOINTS = ("09:00", "09:15", "09:30", "10:00", "10:30", "11:00", "12:30", "13:30", "14:00", "15:00")
INDEXES = (("日経平均", "^N225"), ("TOPIX(1306で代用)", "1306.T"))


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _stage_for(now: datetime) -> str:
    if now.hour < 9:
        return "morning"
    if now.hour < 12:
        return "opening"
    return "midday" if now.hour < 17 else "evening"


def _notes_dir(root: Path, settings: dict) -> Path:
    path = Path(settings.get("notes_dir") or "../trade_notes")
    return path if path.is_absolute() else (root / path).resolve()


# ---------------------------------------------------------------- data

def _stock_range_data(root: Path, today: str) -> dict:
    from modules import report_html as rh

    payload = _load_json(root / "output" / "history" / "stock_range" / f"stock_range_{today.replace('-', '')}.json")
    if not payload:
        current = _load_json(root / "output" / "stock_range.json") or {}
        payload = current if str(current.get("generated_at", "")).startswith(today) else None
    if not payload:
        return {"available": False}
    stats = rh._market_day_stats(root)
    special = rh._special_watch_tickers(root)
    candidates = []
    for kind in ("momentum", "reversal"):
        for c in payload.get(f"{kind}_candidates") or []:
            candidates.append({
                "type": "モメンタム型" if kind == "momentum" else "リバーサル型",
                "name": c.get("name"),
                "ticker": c.get("ticker"),
                "score": c.get("score"),
                "position_pct_30d": c.get("position_pct"),
                "change_pct_prev_day": c.get("change_pct"),
                "reasons": c.get("reasons"),
                "market_day_record": _strip_html(rh._ticker_market_line(stats, kind, c.get("ticker"))),
                "special_watch": (kind, c.get("ticker")) in special,
            })
    market_change_pct = payload.get("market_change_pct")
    bucket = rh._futures_bucket(market_change_pct)
    forecasts = _load_json(root / "state" / "market_forecast_accuracy.json") or []
    same_bucket = [f for f in forecasts if rh._futures_bucket(f.get("predicted_change_pct")) == bucket and f.get("date") != today]
    candidate_tickers = {c["ticker"] for c in candidates}
    watchlist = []
    for item in payload.get("items") or []:
        if item.get("market") != "japan":
            continue
        rng = item.get("range_30d") or {}
        watchlist.append({
            "name": item.get("name"),
            "ticker": item.get("ticker"),
            "close_prev_day": item.get("close"),
            "change_pct_prev_day": item.get("change_pct"),
            "position_pct_30d": rng.get("position_pct"),
            "trend_5d": rng.get("trend"),
            "trend_change_pct_5d": rng.get("trend_change_pct"),
            "is_today_candidate": item.get("ticker") in candidate_tickers,
            "market_day_record_momentum": _strip_html(rh._ticker_market_line(stats, "momentum", item.get("ticker"))),
        })
    return {
        "available": True,
        "generated_at": payload.get("generated_at"),
        "overnight_change_pct": market_change_pct,
        "overnight_source": payload.get("market_change_source"),
        "outlook": _strip_html(rh._market_outlook_block(stats, market_change_pct)),
        "past_mornings_same_overnight_level": same_bucket[-12:],
        "candidates": candidates,
        "watchlist": watchlist,
    }


def _x_trend_data(root: Path, today: str) -> dict:
    path = root / "output" / "history" / "stock_x_trends_runs" / f"stock_x_trends_{today.replace('-', '')}_0700.json"
    payload = _load_json(path)
    if not payload:
        return {"available": False}
    data = payload.get("data") or payload
    keep = ("name", "ticker", "sentiment", "reason", "detail")
    return {
        "available": True,
        "generated_at": payload.get("generated_at"),
        "stocks": [{k: f.get(k) for k in keep} for f in data.get("stock_findings") or []],
        "themes": [{k: f.get(k) for k in keep} for f in data.get("theme_findings") or []],
    }


def _intraday(symbols: list[str], today) -> dict:
    """Per symbol: prices at the open / 9:15 / 10:00 / 11:30 / 15:00 / last, and the previous close."""
    import yfinance as yf

    daily = yf.download(symbols, period="5d", interval="1d", progress=False, auto_adjust=False, group_by="ticker", threads=True)
    intra = yf.download(symbols, period="1d", interval="5m", progress=False, auto_adjust=False, group_by="ticker", threads=True)
    result = {}
    for symbol in symbols:
        try:
            day = daily[symbol].dropna(subset=["Close"])
            bars = intra[symbol].dropna(subset=["Close"])
            bars = bars[[i.date() == today for i in bars.index]]
            if bars.empty:
                continue
            previous_close = float(day["Close"].iloc[-2] if day.index[-1].date() == today else day["Close"].iloc[-1])

            def price_at(hhmm: str):
                for index, row in bars.iterrows():
                    if index.strftime("%H:%M") >= hhmm:
                        return float(row["Open"])
                return None

            morning = bars[[i.strftime("%H:%M") < "11:30" for i in bars.index]]
            # A stock pinned at limit-up/down stops printing bars, so there may be
            # no 15:00 bar; fall back to today's close once the daily bar exists.
            today_close = float(day["Close"].iloc[-1]) if day.index[-1].date() == today else None
            result[symbol] = {
                "previous_close": previous_close,
                "open": float(bars["Open"].iloc[0]),
                "p0915": price_at("09:15"),
                "p1000": price_at("10:00"),
                "p1500": price_at("15:00") or today_close,
                "morning_close": float(morning["Close"].iloc[-1]) if not morning.empty else None,
                "checkpoints": {
                    hhmm: (float(bars["Open"].iloc[0]) if hhmm == "09:00" else price_at(hhmm))
                    for hhmm in CHECKPOINTS
                    if hhmm == "09:00" or bars.index[-1].strftime("%H:%M") >= hhmm
                },
                "high": float(bars["High"].max()),
                "low": float(bars["Low"].min()),
                "last": float(bars["Close"].iloc[-1]),
                "last_bar": bars.index[-1].strftime("%H:%M"),
            }
        except Exception as exc:  # one bad symbol shouldn't sink the rest
            logging.warning("[trade_review] intraday %s failed: %s", symbol, exc)
    return result


def _pct(a, b):
    return None if a is None or b is None or not b else round((a / b - 1) * 100, 2)


def _moves(p: dict, stage: str) -> dict:
    o = p["open"]
    end = p["p1500"] if stage == "evening" else p["last"]
    to10 = _pct(p["p1000"], o)
    moves = {
        "opening_gap": _pct(o, p["previous_close"]),
        "open_to_10": to10,
        "buy_open_to_now" if stage != "evening" else "buy_open_sell_15": _pct(end, o),
        "buy_0915_to_now" if stage != "evening" else "buy_0915_sell_15": _pct(end, p["p0915"]),
        "buy_10_to_now" if stage != "evening" else "buy_10_sell_15": _pct(end, p["p1000"]),
        "morning_close_vs_prev": _pct(p["morning_close"], p["previous_close"]),
        "now_vs_prev": _pct(p["last"], p["previous_close"]),
        "high_since_open": _pct(p["high"], o),
        "low_since_open": _pct(p["low"], o),
        "data_as_of": p["last_bar"],
    }
    if stage == "evening" and to10 is not None:
        moves["ten_oclock_rule"] = _pct(end, o) if to10 > 0 else to10
    moves.update(_hindsight(p))
    return moves


def _hindsight(p: dict) -> dict:
    """In hindsight, the best (and worst) buy -> sell pair among the checkpoints
    seen so far today - for the "you should have bought at X and sold at Y" line."""
    points = [(t, v) for t, v in (p.get("checkpoints") or {}).items() if v]
    best = worst = None
    for i, (buy_t, buy_v) in enumerate(points):
        for sell_t, sell_v in points[i + 1:]:
            r = _pct(sell_v, buy_v)
            if best is None or r > best[2]:
                best = (buy_t, sell_t, r)
            if worst is None or r < worst[2]:
                worst = (buy_t, sell_t, r)
    if not best:
        return {}
    return {
        "hindsight_best": {"buy": best[0], "sell": best[1], "return_pct": best[2]},
        "hindsight_worst": {"buy": worst[0], "sell": worst[1], "return_pct": worst[2]},
        "checkpoint_prices": {t: round(v, 1) for t, v in points},
    }


def _update_gap_log(root: Path, today: str, x_stocks: list[dict], prices: dict) -> list[dict]:
    """Keeps every X-trend stock that opened >= +3% (the "don't chase" rule) with
    how it did from the open to 15:00, so the evening review can track the rule."""
    path = root / GAP_LOG_PATH
    log = _load_json(path) or []
    log = [r for r in log if r.get("date") != today]
    for stock in x_stocks:
        p = prices.get(f"{stock.get('ticker')}.T")
        if not p:
            continue
        gap = _pct(p["open"], p["previous_close"])
        if gap is not None and gap >= BIG_GAP_PCT:
            log.append({
                "date": today, "name": stock.get("name"), "ticker": stock.get("ticker"),
                "sentiment": stock.get("sentiment"), "opening_gap": gap,
                "buy_open_sell_15": _pct(p["p1500"], p["open"]),
            })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    return log


def _collect(root: Path, stage: str, now: datetime) -> dict:
    today = now.strftime("%Y-%m-%d")
    data = {
        "now": now.strftime("%Y-%m-%d %H:%M JST"),
        "weekday": WEEKDAYS_JP[now.weekday()],
        "stock_range": _stock_range_data(root, today),
        "x_trends": _x_trend_data(root, today),
    }
    if stage == "morning":
        return data
    symbols = [ticker for _, ticker in INDEXES]
    symbols += [c["ticker"] for c in data["stock_range"].get("candidates") or []]
    symbols += [w["ticker"] for w in data["stock_range"].get("watchlist") or []]
    symbols += [f"{s['ticker']}.T" for s in data["x_trends"].get("stocks") or [] if s.get("ticker")]
    prices = _intraday(sorted(set(symbols)), now.date())
    if "^N225" not in prices:
        data["market_closed"] = True
        return data
    data["indexes"] = {name: _moves(prices[t], stage) for name, t in INDEXES if t in prices}
    for c in data["stock_range"].get("candidates") or []:
        if c["ticker"] in prices:
            c["moves"] = _moves(prices[c["ticker"]], stage)
    for w in data["stock_range"].get("watchlist") or []:
        if w["ticker"] in prices:
            w["moves"] = _moves(prices[w["ticker"]], stage)
    for s in data["x_trends"].get("stocks") or []:
        p = prices.get(f"{s.get('ticker')}.T")
        if p:
            s["moves"] = _moves(p, stage)
    if stage == "evening":
        data["big_gap_x_stocks_history"] = _update_gap_log(root, today, data["x_trends"].get("stocks") or [], prices)
        day = next((r for r in _load_json(root / "state" / "stock_range_predictions.json") or [] if r.get("logged_date") == today and r.get("market_day")), None)
        data["market_day_recorded"] = day and {k: day.get(k) for k in ("market_day", "nikkei_change_pct", "topix_change_pct")}
    return data


# ---------------------------------------------------------------- prompt / output

def _earlier_notes(notes_dir: Path, today: str, own_suffix: str) -> str:
    texts = []
    for path in sorted(notes_dir.glob(f"{today}_*.txt")):
        if own_suffix not in path.name:
            texts.append(f"--- {path.name} ---\n{path.read_text(encoding='utf-8')[:6000]}")
    return "\n\n".join(texts)


def _build_prompt(root: Path, stage: str, data: dict, earlier: str) -> str:
    prompt_dir = root / PROMPT_DIR
    parts = [
        (prompt_dir / "common.md").read_text(encoding="utf-8"),
        (prompt_dir / STAGES[stage][2]).read_text(encoding="utf-8"),
        "## ルールファイル(docs/rule/stock_range_analytics_rule.md)\n\n" + (root / RULE_FILE).read_text(encoding="utf-8"),
        "## 今日のデータ(JSON)\n\n" + json.dumps(data, ensure_ascii=False, indent=1),
    ]
    if earlier:
        parts.append("## 今日のこれまでの考察(前の時間帯)\n\n" + earlier)
    parts.append("以上をもとに、指定の形式で考察を書いてください。考察の本文だけを出力すること。")
    return "\n\n".join(parts)


PROMPT_ARCHIVE_DIR = Path("output") / "history" / "trade_review"
PROMPT_RETENTION_DAYS = 120


def _archive_payload(root: Path, now: datetime, stage: str, payload: dict) -> None:
    """Per-run copy of output/trade_review.json (data + mail text + log text),
    kept next to the prompt for later analysis."""
    try:
        directory = root / PROMPT_ARCHIVE_DIR
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{now:%Y%m%d_%H%M}_{stage}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logging.warning("[trade_review] payload archive failed: %s", exc)


def _archive_prompt(root: Path, now: datetime, stage: str, prompt: str) -> None:
    """Keeps the exact prompt sent to Claude (instructions + rules + the day's
    data JSON + earlier notes), so an odd review can be traced back to its input."""
    try:
        directory = root / PROMPT_ARCHIVE_DIR
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{now:%Y%m%d_%H%M}_{stage}_prompt.txt").write_text(prompt, encoding="utf-8")
        cutoff = (now - timedelta(days=PROMPT_RETENTION_DAYS)).strftime("%Y%m%d")
        for path in [*directory.glob("*_prompt.txt"), *directory.glob("*.json")]:
            if path.name[:8] < cutoff:
                path.unlink()
    except OSError as exc:
        logging.warning("[trade_review] prompt archive failed: %s", exc)


def _split_output(text: str) -> tuple[str, str]:
    """Claude answers with a <<<MAIL>>> part (plain-language, for the mail) and a
    <<<LOG>>> part (detailed review + hand-off notes). If the markers are missing,
    the whole answer is used for both."""
    if "<<<MAIL>>>" not in text or "<<<LOG>>>" not in text:
        return text.strip(), ""
    mail = text.split("<<<MAIL>>>", 1)[1].split("<<<LOG>>>", 1)[0].strip()
    log = text.split("<<<LOG>>>", 1)[1].strip()
    return mail, log


def run(root: Path, stage: str | None = None) -> None:
    output_path = root / "output" / "trade_review.json"
    now = datetime.now(JST)
    stage = stage or _stage_for(now)
    label, suffix, _ = STAGES[stage]
    today = now.strftime("%Y-%m-%d")
    settings = load_config(root).get("trade_review") or {}

    def _write(status: str, **extra) -> dict:
        payload = {"module": "trade_review", "generated_at": now.isoformat(), "stage": stage, "status": status, **extra}
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    if now.weekday() >= 5:
        _write("skipped", reason="weekend")
        return
    title = f"売買考察 {now:%m/%d} {label}"
    try:
        data = _collect(root, stage, now)
        if data.get("market_closed"):
            _write("skipped", reason="no market data today (holiday?)")
            logging.info("[trade_review] skipped: no market data today")
            return
        notes_dir = _notes_dir(root, settings)
        notes_dir.mkdir(parents=True, exist_ok=True)
        prompt = _build_prompt(root, stage, data, _earlier_notes(notes_dir, today, suffix))
        _archive_prompt(root, now, stage, prompt)
        text = _call_claude(
            prompt,
            settings.get("claude_model") or None,
            root,
            int(settings.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        )
    except Exception as exc:
        # trade_review_mail turns this into a 【失敗】 mail.
        _write("error", title=title, stage_label=label, error=str(exc))
        logging.error("[trade_review] %s failed: %s", stage, exc)
        return

    mail_text, log_text = _split_output(text)
    note_path = notes_dir / f"{today}_{suffix}.txt"
    # The note keeps both: the plain-language mail version and the detailed review.
    note_path.write_text(f"{mail_text}\n\n\n{log_text}\n" if log_text else text + "\n", encoding="utf-8")
    payload = _write(
        "ok",
        title=title,
        stage_label=label,
        date=today,
        note_path=str(note_path),
        mail_text=mail_text,
        log_text=log_text,
        data=data,
    )
    _archive_payload(root, now, stage, payload)
    logging.info("[trade_review] %s review written to %s", stage, note_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Write the daily trade review (morning / midday / evening).")
    parser.add_argument("--stage", choices=list(STAGES), help="default: chosen from the current time")
    args = parser.parse_args()
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(root, stage=args.stage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
