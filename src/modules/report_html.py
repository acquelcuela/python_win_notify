import csv
import html
import json
import logging
import re
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


JST = timezone(timedelta(hours=9), "JST")


def _yahoo_finance_link(ticker: str) -> str:
    ticker = str(ticker or "").strip()
    if not ticker or ticker == "-" or ticker.startswith("^"):
        return html.escape(ticker or "-")
    url = f"https://finance.yahoo.co.jp/quote/{urllib.parse.quote(ticker)}"
    return f'<a href="{url}" target="_blank" rel="noopener">{html.escape(ticker)}</a>'


def _fmt_number(value) -> str:
    if value is None:
        return "-"
    return f"{int(value):,}"


def _fmt_decimal(value, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.{digits}f}"


def _fmt_change(change, change_pct) -> tuple[str, str]:
    if change is None:
        return "-", "#334155"
    sign = "+" if change >= 0 else ""
    color = "#047857" if change >= 0 else "#b91c1c"
    return f"{sign}{float(change):,.2f} ({sign}{float(change_pct):.2f}%)", color


def _change_state(change) -> tuple[str, str]:
    if change is None:
        return "flat", "横ばい"
    if change > 0:
        return "up", "上昇"
    if change < 0:
        return "down", "下落"
    return "flat", "flat"


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _generated_at_label(payload: dict) -> str:
    try:
        generated_at = datetime.fromisoformat(str(payload.get("generated_at")))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=JST)
        return generated_at.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
    except (TypeError, ValueError):
        return "-"


def _data_alias_terms(root: Path, text: str) -> list[str]:
    terms = []
    data_path = root / "data" / "data_j.csv"
    if data_path.exists():
        with data_path.open(encoding="utf-8", newline="") as file:
            rows = csv.reader(file)
            first = next(rows, None)
            headers = next(rows, None) if first == ["陦ｨ1"] else first
            if headers:
                reader = csv.DictReader(file, fieldnames=headers)
                for row in reader:
                    name = str(row.get("name") or row.get("銘柄名") or "").strip()
                    if len(name) >= 4 and name in text and name not in terms:
                        terms.append(name)

    alias_path = root / "data" / "data_j_aliases.json"
    aliases = _load_json(alias_path)
    if aliases:
        for item in aliases.get("aliases", []):
            for alias in item.get("aliases", []):
                value = str(alias).strip()
                if value and value in text and value not in terms:
                    terms.append(value)
    return terms


def _ai_highlight_terms(root: Path, text: str = "") -> list[str]:
    terms = []
    watchlist = _load_json(root / "output" / "stock_watchlist.json")
    if watchlist and watchlist.get("data"):
        for item in watchlist["data"]:
            if item.get("market") != "japan":
                continue
            for value in (item.get("name"), item.get("ticker")):
                if value and value not in terms:
                    terms.append(str(value))
    movers = _load_json(root / "output" / "news_movers.json")
    if movers and movers.get("data"):
        for item in movers["data"]:
            for value in (item.get("name"), item.get("ticker")):
                if value and value not in terms:
                    terms.append(str(value))
    for value in _data_alias_terms(root, text):
        if value not in terms:
            terms.append(value)
    extra_terms = [
        "ニュースからの考察",
        "カカクコム",
        "LINEヤフー",
        "ispace",
        "JX金属",
        "日経225先物",
        "TOPIX連動ETF",
        "日経平均",
        "TOPIX",
    ]
    for value in extra_terms:
        if value not in terms:
            terms.append(value)
    return sorted(terms, key=len, reverse=True)


def _escape_and_highlight(text: str, terms: list[str]) -> str:
    escaped = html.escape(text)
    escaped_terms = [html.escape(term) for term in terms if term]
    if not escaped_terms:
        return escaped.replace("\n", "<br>")
    pattern = re.compile("|".join(re.escape(term) for term in escaped_terms))
    highlighted = pattern.sub(
        lambda match: f'<strong class="ai-emphasis">{match.group(0)}</strong>',
        escaped,
    )
    return highlighted.replace("\n", "<br>")


def _ai_summary_failure_note(errors: dict | None, provider: str | None) -> str:
    if not errors:
        return ""
    label = "Claude" if provider in (None, "", "claude") else provider
    items = "".join(f"<li>{html.escape(str(k))}: {html.escape(str(v)[:300])}</li>" for k, v in errors.items())
    return f"""
      <div class="alert" style="margin-top:8px;">
        <strong>AI要約の作成に失敗しました({html.escape(label)})。</strong>
        <ul>{items}</ul>
      </div>
    """


def _ai_summary_section(root: Path) -> str:
    payload = _load_json(root / "output" / "ai_summary.json")
    if not payload:
        return ""
    if payload.get("status") == "error":
        # Shown rather than silently dropped (user request 2026-10-06: with
        # Claude and no Gemini fallback, a failure should be reported as one).
        return f"""
    <section class="panel">
      <div class="section-title">AI概要と考察</div>
      {_ai_summary_failure_note(payload.get("error") if isinstance(payload.get("error"), dict) else {"error": payload.get("error")}, payload.get("provider"))}
    </section>
    """
    if payload.get("status") != "ok" or not payload.get("data"):
        return ""

    raw_market_data = payload["data"].get("market_data", "")
    raw_news = payload["data"].get("news", "")
    terms = _ai_highlight_terms(root, f"{raw_market_data}\n{raw_news}")
    market_data = _escape_and_highlight(raw_market_data, terms)
    news = _escape_and_highlight(raw_news, terms)
    blocks = ""
    if market_data:
        blocks += f"""
        <div class="ai-block">
          <div class="ai-block-title">指定銘柄・市場データからの考察</div>
          <div>{market_data}</div>
        </div>
        """
    if news:
        blocks += f"""
        <div class="ai-block">
          <div class="ai-block-title">ニュースからの考察</div>
          <div>{news}</div>
        </div>
        """
    if not blocks:
        return ""

    return f"""
    <section class="panel">
      <div class="section-title">AI概要と考察</div>
      <div class="ai-summary">{blocks}</div>
      {_ai_summary_failure_note(payload.get("warnings"), payload.get("provider"))}
      <div class="muted">生成モデル: {html.escape("Claude(Claude Pro)" if payload.get("provider") == "claude" else payload.get("model", "-"))}</div>
    </section>
    """


def _nikkei_section(root: Path) -> str:
    payload = _load_json(root / "output" / "stock_nikkei.json")
    if not payload:
        return "<p>市場概況データファイルは生成されていません。</p>"

    if payload.get("status") != "ok" or not payload.get("data"):
        error = html.escape(payload.get("error", "unknown error"))
        return f"""
        <div class="alert">
          <strong>市場概況データの取得に失敗しました。</strong>
          <div>{error}</div>
        </div>
        """

    data = payload["data"]
    indices = data.get("indices") or {"nikkei_futures": data}
    nikkei_futures = indices.get("nikkei_futures")
    nikkei_average = indices.get("nikkei_average")
    topix = indices.get("topix")
    primary = nikkei_average or topix or nikkei_futures or {}
    change_state, change_label = _change_state(primary.get("change"))

    def index_card(item: dict | None) -> str:
        if not item:
            return ""
        change_text, change_color = _fmt_change(item.get("change"), item.get("change_pct", 0))
        return f"""
        <div class="index-card">
          <div class="index-head">
            <strong>{html.escape(item.get("label", item.get("symbol", "-")))}</strong>
            <span class="muted">{html.escape(item.get("symbol", "-"))}</span>
          </div>
          <div class="index-current">{_fmt_decimal(item.get("current"))}</div>
          <div class="change" style="color:{change_color};">{change_text}</div>
          <table>
            <tr><th>蟋句､</th><td>{_fmt_decimal(item.get("open"))}</td></tr>
            <tr><th>鬮伜､</th><td>{_fmt_decimal(item.get("high"))}</td></tr>
            <tr><th>螳牙､</th><td>{_fmt_decimal(item.get("low"))}</td></tr>
            <tr><th>蜑榊屓邨ょ､</th><td>{_fmt_decimal(item.get("prev_close"))}</td></tr>
          </table>
        </div>
        """

    def index_compact_cell(item: dict | None) -> str:
        if not item:
            return '<td class="index-grid-cell"></td>'
        change_text, change_color = _fmt_change(item.get("change"), item.get("change_pct", 0))
        return f"""
        <td class="index-grid-cell">
          <div class="index-mini-card">
            <div class="index-mini-label">{html.escape(item.get("label", item.get("symbol", "-")))}</div>
            <div class="muted">{html.escape(item.get("symbol", "-"))}</div>
            <div class="index-mini-current">{_fmt_decimal(item.get("current"))}</div>
            <div class="index-mini-change" style="color:{change_color};">{change_text}</div>
          </div>
        </td>
        """

    index_grid = f"""
      <table class="index-grid">
        <tr>
          {index_compact_cell(nikkei_average)}
          {index_compact_cell(nikkei_futures)}
          {index_compact_cell(topix)}
        </tr>
      </table>
    """

    warnings = ""
    if payload.get("warnings"):
        warning_items = "".join(f"<li>{html.escape(item)}</li>" for item in payload["warnings"])
        warnings = f'<div class="note"><strong>注意</strong><ul>{warning_items}</ul></div>'

    range_payload = _load_json(root / "output" / "stock_range.json") or {}
    range_cards = _index_range_cards(range_payload.get("index_items") or [])

    return f"""
    <section class="panel market-{change_state}">
      <div class="section-title">日経平均 / TOPIX 市場概況</div>
      <div class="section-body">
        <div class="state-label">{change_label}</div>
        <div class="muted"><a href="https://search.yahoo.co.jp/realtime/search?p=%E6%97%A5%E7%B5%8C%E3%80%80%E6%99%82%E9%96%93%E5%A4%96" target="_blank" rel="noopener">日経 時間外(Yahoo!リアルタイム検索)</a></div>
      </div>
      {index_grid}
      {warnings}
      {range_cards}
    </section>
    """


def _stock_range_hit_rate_text(hit_rate: dict) -> str:
    def _text(kind: str) -> str:
        stats = hit_rate.get(kind) or {}
        hits, total = stats.get("hits", 0), stats.get("total", 0)
        return f"{hits}/{total}({hits / total * 100:.0f}%)" if total else "データ蓄積中"

    return f"モメンタム型 {_text('momentum')} / リバーサル型 {_text('reversal')}"


# "特別注目銘柄": (type, ticker) pairs whose recorded stock_range hit rate
# exceeds SPECIAL_WATCH_HIT_RATE_PCT with at least SPECIAL_WATCH_MIN_N
# evaluations of that type - computed live from
# state/stock_range_predictions.json on every render (user request
# 2026-09-17) so membership stays current and a ticker can drop back out.
# Momentum and reversal are counted separately since a ticker can do well as
# one and poorly as the other. The thresholds are meant to be re-tuned about
# monthly (user request 2026-10-02); at that time n>=10 / >50% flagged 17 of
# 32 eligible pairs, and a walk-forward check had them hitting 57.8% vs
# 50.3% for the rest - promising but not yet clearly beyond noise.
SPECIAL_WATCH_MIN_N = 10
SPECIAL_WATCH_HIT_RATE_PCT = 50.0


def _special_watch_tickers(root: Path) -> dict[tuple[str, str], tuple[int, int]]:
    path = root / "state" / "stock_range_predictions.json"
    if not path.exists():
        return {}
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    by_key: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for r in records:
        if r.get("evaluated") and r.get("hit") is not None and r.get("ticker") and r.get("type"):
            by_key[(r["type"], r["ticker"])].append(bool(r["hit"]))
    result = {}
    for (kind, ticker), hits in by_key.items():
        n = len(hits)
        if n < SPECIAL_WATCH_MIN_N:
            continue
        if sum(hits) / n * 100 > SPECIAL_WATCH_HIT_RATE_PCT:
            result[(kind, ticker)] = (sum(hits), n)
    return result


# 地合い別の成績(2026-10-06): 「当日プラスか」の的中率は、日経・TOPIXが
# 両方上がった日は約65%、両方下がった日は約30%と地合いでほぼ決まっていた。
# 単一の的中率だと「今日は相場が上がりそうか」を別に判断する必要があるので、
# 地合い別の実績と、今朝の時間外の動きから見た「今日の見込み的中率」を出す。
# market_day は stock_range_eval が記録する(日経平均とTOPIX連動ETF 1306)。
MARKET_DAYS = ("up", "mixed", "down")
MARKET_DAY_LABELS = {"up": "両方上昇", "mixed": "まちまち", "down": "両方下落"}
FUTURES_BUCKETS = (
    ("-0.5%以下", lambda v: v <= -0.5),
    ("-0.5〜0%", lambda v: -0.5 < v < 0),
    ("0〜+1%", lambda v: 0 <= v < 1),
    ("+1%以上", lambda v: v >= 1),
)
# 先物の水準ごとの日数がこれより少なければ、全期間の地合いの比率で代用する
MIN_FUTURES_BUCKET_DAYS = 5
# 銘柄ラベル(「下げにも強い」「相場次第」)を付けるのに必要な日数
MIN_TICKER_DAYS_FOR_LABEL = 4


def _futures_bucket(value: float | None) -> str | None:
    if value is None:
        return None
    return next(label for label, matches in FUTURES_BUCKETS if matches(value))


def _market_day_stats(root: Path) -> dict:
    path = root / "state" / "stock_range_predictions.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        records = []
    by_type: dict = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    by_ticker: dict = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    days: dict[str, tuple[str, float | None]] = {}
    for r in records:
        day = r.get("market_day")
        if not r.get("evaluated") or r.get("hit") is None or day not in MARKET_DAYS:
            continue
        for bucket in (by_type[r.get("type")][day], by_ticker[(r.get("type"), r.get("ticker"))][day]):
            bucket[0] += int(bool(r["hit"]))
            bucket[1] += 1
        days.setdefault(r.get("logged_date"), (day, r.get("market_change_pct")))
    futures_days: dict = defaultdict(lambda: defaultdict(int))
    all_days: dict = defaultdict(int)
    for day, futures in days.values():
        all_days[day] += 1
        bucket = _futures_bucket(futures)
        if bucket:
            futures_days[bucket][day] += 1
    return {"by_type": by_type, "by_ticker": by_ticker, "futures_days": futures_days, "all_days": all_days}


def _rate_text(hits_n: list[int] | None) -> str:
    if not hits_n or not hits_n[1]:
        return "-"
    return f"{hits_n[0]}/{hits_n[1]}({hits_n[0] / hits_n[1] * 100:.0f}%)"


def _expected_hit_rate(stats: dict, kind: str, day_counts: dict) -> float | None:
    total = weight = 0.0
    for day in MARKET_DAYS:
        hits, n = stats["by_type"][kind][day]
        if n and day_counts.get(day):
            total += day_counts[day] * hits / n
            weight += day_counts[day]
    return total / weight if weight else None


def _market_outlook_block(stats: dict, market_change_pct: float | None) -> str:
    rows = "".join(
        f'<tr><td style="padding:2px 8px;">{label}</td>'
        + "".join(f'<td style="padding:2px 8px;">{_rate_text(stats["by_type"][kind][day])}</td>' for day in MARKET_DAYS)
        + "</tr>"
        for kind, label in (("momentum", "モメンタム型"), ("reversal", "リバーサル型"))
    )
    table = (
        '<table style="margin-top:4px;border-collapse:collapse;font-size:12px;">'
        '<tr style="background:#f1f5f9;"><th style="padding:2px 8px;text-align:left;">当日の地合い</th>'
        + "".join(f'<th style="padding:2px 8px;text-align:left;">{MARKET_DAY_LABELS[d]}</th>' for d in MARKET_DAYS)
        + f"</tr>{rows}</table>"
    )
    outlook = ""
    bucket = _futures_bucket(market_change_pct)
    if bucket:
        counts = stats["futures_days"].get(bucket) or {}
        n_days = sum(counts.values())
        if n_days >= MIN_FUTURES_BUCKET_DAYS:
            basis = (
                f"過去の同じ水準({bucket})の朝 {n_days}日: "
                + " / ".join(f"{MARKET_DAY_LABELS[d]} {counts.get(d, 0)}日" for d in MARKET_DAYS)
            )
        else:
            counts = stats["all_days"]
            basis = f"同じ水準({bucket})の朝はまだ{n_days}日しかないため、全期間の地合いの比率で計算"
        expected = {kind: _expected_hit_rate(stats, kind, counts) for kind in ("momentum", "reversal")}
        expected_text = " / ".join(
            f"{label} 約{expected[kind] * 100:.0f}%"
            for kind, label in (("momentum", "モメンタム"), ("reversal", "リバーサル"))
            if expected[kind] is not None
        )
        if expected_text:
            outlook = (
                '<div style="margin-top:6px;padding:8px 10px;background:#eff6ff;border:1px solid #93c5fd;border-radius:6px;">'
                f'<div style="font-weight:bold;">今日の見込み的中率: {expected_text}</div>'
                f'<div class="muted">今朝の時間外 {market_change_pct:+.2f}% → {html.escape(basis)}</div>'
                "</div>"
            )
    return f"""
      {outlook}
      <div class="muted" style="margin-top:6px;">地合い別の的中率(これまで。当日の値動きがプラスで終わったか):</div>
      {table}
    """


def _ticker_market_line(stats: dict, kind: str, ticker: str | None) -> str:
    rates = stats["by_ticker"].get((kind, ticker))
    if not rates:
        return ""
    up, down = rates["up"], rates["down"]
    label = ""
    # 「下げの日にも強い」は、上げの日もきちんと当たっている銘柄に限る
    # (上げの日も5割前後なら、地合いと無関係にばらついているだけ)
    if (
        up[1] >= MIN_TICKER_DAYS_FOR_LABEL
        and down[1] >= MIN_TICKER_DAYS_FOR_LABEL
        and up[0] / up[1] >= 0.6
        and down[0] / down[1] >= 0.5
    ):
        label = '<span style="color:#047857;font-weight:bold;"> ← 下げの日にも強い</span>'
    elif (
        up[1] >= MIN_TICKER_DAYS_FOR_LABEL
        and down[1] >= MIN_TICKER_DAYS_FOR_LABEL
        and up[0] / up[1] >= 0.7
        and down[0] / down[1] <= 0.2
    ):
        label = '<span style="color:#b45309;font-weight:bold;"> ← 相場次第</span>'
    return (
        f'<div class="muted">地合い別の実績: 両方上昇の日 {_rate_text(up)}・まちまち {_rate_text(rates["mixed"])}'
        f"・両方下落の日 {_rate_text(down)}{label}</div>"
    )


def _stock_range_candidate_cards(
    candidates: list[dict],
    kind: str,
    special_watch: dict[tuple[str, str], tuple[int, int]],
    market_stats: dict | None = None,
) -> str:
    if not candidates:
        return '<div class="muted">該当銘柄なし</div>'
    cards = []
    for candidate in candidates[:5]:
        reasons = " / ".join(candidate.get("reasons") or [])
        # 先頭の履歴は当日変化と重なるので除外する(_watchlist_cardsと同じ理由)
        trend_rows = []
        for trend in (candidate.get("daily_changes") or [])[1:]:
            trend_text, trend_color = _fmt_change(trend.get("change"), trend.get("change_pct", 0))
            trend_rows.append(
                f"""
                <div class="stock-trend">
                  <span>{html.escape(trend.get("label", ""))}</span>
                  <strong style="color:{trend_color};">{trend_text}</strong>
                </div>
                """
            )
        position_pct = candidate.get("position_pct")
        range_bar = ""
        if position_pct is not None:
            position_pct_clamped = max(0.0, min(100.0, float(position_pct)))
            range_bar = f"""
            <div style="background:#e5e7eb;border-radius:4px;height:8px;width:100%;margin-top:6px;">
              <div style="background:#2563eb;border-radius:4px;height:8px;width:{position_pct_clamped}%;"></div>
            </div>
            <div class="muted">30日レンジの{html.escape(str(position_pct))}%地点</div>
            """
        change_text, change_color = _fmt_change(candidate.get("change"), candidate.get("change_pct", 0))
        special = special_watch.get((kind, candidate.get("ticker")))
        card_style = (
            "border:2px solid #f59e0b;background:#fffbeb;"
            if special is not None
            else ""
        )
        special_badge = (
            f'<span style="background:#f59e0b;color:#fff;font-size:11px;padding:1px 6px;'
            f'border-radius:10px;margin-left:6px;">⭐特別注目 的中{special[0]}/{special[1]}'
            f'({special[0] / special[1] * 100:.0f}%)</span>'
            if special is not None
            else ""
        )
        cards.append(
            f"""
            <div class="news-hit-card" style="{card_style}">
              <div class="news-hit-title">
                <strong>{html.escape(candidate.get("name", ""))}</strong>{special_badge}
                <span class="muted">{_yahoo_finance_link(candidate.get("ticker", "-"))} {_fmt_decimal(candidate.get("close"))}円</span>
                <span style="float:right;font-weight:bold;">{candidate.get("score")}点</span>
              </div>
              <div style="color:{change_color};font-weight:bold;clear:both;">{change_text}(前日比)</div>
              <div class="muted">{html.escape(reasons)}</div>
              {_ticker_market_line(market_stats, kind, candidate.get("ticker")) if market_stats else ""}
              {range_bar}
              {''.join(trend_rows)}
            </div>
            """
        )
    return "".join(cards)


def _index_range_card(item: dict) -> str:
    range_info = item.get("range_30d")
    if not range_info:
        return ""
    change_text, change_color = _fmt_change(item.get("change"), item.get("change_pct", 0))
    position_pct = range_info.get("position_pct")
    position_pct_clamped = max(0.0, min(100.0, float(position_pct))) if position_pct is not None else 0.0
    daily_rows = []
    for day in (item.get("daily_changes") or [])[:10]:
        day_text, day_color = _fmt_change(day.get("change"), day.get("change_pct", 0))
        daily_rows.append(
            f"""
            <div style="display:flex;justify-content:space-between;font-size:12px;color:#6b7280;margin-top:2px;">
              <span>{html.escape(day.get("label", ""))}</span>
              <strong style="color:{day_color};">{day_text}</strong>
            </div>
            """
        )
    daily_changes_block = (
        f"""
        <div style="margin-top:8px;padding-top:6px;border-top:1px solid #e5e7eb;">
          <div class="muted" style="margin-bottom:2px;">直近10営業日の増減</div>
          {''.join(daily_rows)}
        </div>
        """
        if daily_rows
        else ""
    )
    return f"""
    <div class="news-hit-card">
      <div class="news-hit-title">
        <strong>{html.escape(item.get("name", ""))}</strong>
        <span class="muted">{_yahoo_finance_link(item.get("ticker", "-"))}</span>
        <span style="float:right;font-weight:bold;">{_fmt_decimal(item.get("close"))}</span>
      </div>
      <div style="color:{change_color};font-weight:bold;clear:both;">{change_text}(前日比)</div>
      <div class="muted">30日高値: {_fmt_decimal(range_info.get("high_price"))}（{html.escape(str(range_info.get("high_date", "-")))}） / 30日安値: {_fmt_decimal(range_info.get("low_price"))}（{html.escape(str(range_info.get("low_date", "-")))}）</div>
      <div style="background:#e5e7eb;border-radius:4px;height:8px;width:100%;margin-top:6px;">
        <div style="background:#2563eb;border-radius:4px;height:8px;width:{position_pct_clamped}%;"></div>
      </div>
      <div class="muted">30日レンジの{html.escape(str(position_pct))}%地点</div>
      {daily_changes_block}
    </div>
    """


def _index_range_cards(index_items: list[dict]) -> str:
    cards = "".join(_index_range_card(item) for item in index_items)
    if not cards:
        return ""
    return f"""
    <h3>主要指数(日経平均・TOPIX)</h3>
    {cards}
    """


# 2026-09-11時点の暫定的な目安: 先物+1%以上の朝はモメンタム的中率68%
# (n=65、ただし実質4営業日分)だった一方、-0.5%未満の朝は43%だった。まだ
# データが薄いためスコアには反映せず、目立たせるだけに留める。
STRONG_FUTURES_THRESHOLD_PCT = 1.0


def _stock_range_score_section(root: Path) -> str:
    payload = _load_json(root / "output" / "stock_range.json")
    if not payload or payload.get("status") != "ok":
        return ""

    market_change_pct = payload.get("market_change_pct")
    market_note = ""
    if market_change_pct is not None:
        source = payload.get("market_change_source") or {}
        source_label = source.get("label") or "日経225先物(CME・シカゴ)"
        detail = ""
        if source.get("source") == "yahoo_realtime_search":
            posted = str(source.get("latest_post_at") or "")[11:16]
            detail = (
                f'(水準 {source.get("value"):,.0f} / 前日終値 {source.get("previous_close"):,.0f}、'
                f'最新{source.get("posts_used")}件の投稿の中央値、最新 {posted})'
            )
        market_note = (
            f'<div class="muted">今朝の時間外の動き: {html.escape(source_label)} {market_change_pct:+.2f}%'
            f"{html.escape(detail)}</div>"
        )
        if market_change_pct >= STRONG_FUTURES_THRESHOLD_PCT:
            market_note += (
                '<div style="margin-top:6px;padding:8px 10px;background:#ecfdf5;border:1px solid #6ee7b7;'
                'border-radius:6px;color:#047857;font-weight:bold;">'
                f'📈 はっきりした時間外高({market_change_pct:+.2f}%) - 過去データではこの水準の朝はモメンタム型の'
                'あたりが多い傾向(参考値、まだ実績日数は少なめ)</div>'
            )

    special_watch = _special_watch_tickers(root)
    market_stats = _market_day_stats(root)

    return f"""
    <section class="panel">
      <div class="section-title">30日レンジ 本日の上昇候補(機械的スコアリング・投資助言ではありません)</div>
      <div class="muted">算出時刻: {_generated_at_label(payload)}(1日1回・朝06:45の市場が開く前に算出し、本日の値動きを対象にした候補です。終日この結果を表示します)</div>
      <div class="muted">30日レンジ位置・直近5営業日のトレンド・当日Xの話題・時間外の地合い(日経時間外、取れないときはCME先物)を組み合わせた参考指標です。的中を保証するものではありません。</div>
      <div class="muted">⭐特別注目銘柄: 同じ型(モメンタム/リバーサル)で{SPECIAL_WATCH_MIN_N}回以上候補に出て、的中率が{SPECIAL_WATCH_HIT_RATE_PCT:.0f}%を超えた銘柄。カードを金色で強調表示します(基準は月1回程度見直し)。</div>
      {market_note}
      {_market_outlook_block(market_stats, market_change_pct)}
      <h3>モメンタム型(上昇継続を期待)</h3>
      {_stock_range_candidate_cards(payload.get("momentum_candidates") or [], "momentum", special_watch, market_stats)}
      <h3>リバーサル型(反発を期待)</h3>
      {_stock_range_candidate_cards(payload.get("reversal_candidates") or [], "reversal", special_watch, market_stats)}
      <div class="muted" style="margin-top:8px;">これまでの的中率(当日の実際の値動きがプラスだったか): {_stock_range_hit_rate_text(payload.get("hit_rate") or {})}</div>
    </section>
    """


def _verdict_label(hit: bool | None, stage: str, miss_label: str = "不発") -> tuple[str, str]:
    if hit is None:
        return "-", "#6b7280"
    if stage == "interim":
        return ("現在的中", "#047857") if hit else (f"現在{miss_label}", "#b91c1c")
    return ("的中", "#047857") if hit else (miss_label, "#b91c1c")


def _flip_note(r: dict, stage: str) -> str:
    if stage != "final" or not r.get("flipped"):
        return ""
    previous_text, _ = _verdict_label(r.get("previous_hit"), "final")
    current_text, _ = _verdict_label(r.get("hit"), "final")
    return f'<div style="font-size:11px;color:#b45309;margin-top:2px;">昼時点から変化: {previous_text}→{current_text}</div>'


def _stock_range_eval_result_cards(results: list[dict], stage: str) -> str:
    if not results:
        return '<div class="muted">該当銘柄なし</div>'
    cards = []
    for r in results:
        verdict_text, verdict_color = _verdict_label(r.get("hit"), stage)
        actual_pct = r.get("actual_change_pct")
        actual_text = f"{actual_pct:+.2f}%" if actual_pct is not None else "-"
        reasons = " / ".join(r.get("reasons") or [])
        cards.append(
            f"""
            <div class="news-hit-card">
              <div class="news-hit-title">
                <strong>{html.escape(r.get("name", ""))}</strong>
                <span class="muted">{_yahoo_finance_link(r.get("ticker", "-"))}</span>
                <span style="float:right;font-weight:bold;color:{verdict_color};">{verdict_text}</span>
              </div>
              <div class="muted" style="clear:both;">スコア{r.get("score")}点 / 本日{actual_text} / {html.escape(reasons)}</div>
              {_flip_note(r, stage)}
            </div>
            """
        )
    return "".join(cards)


def _stock_range_eval_section(root: Path) -> str:
    payload = _load_json(root / "output" / "stock_range_eval.json")
    if not payload or payload.get("status") != "ok":
        return ""
    try:
        generated_at = datetime.fromisoformat(str(payload.get("generated_at")))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=JST)
        if generated_at.astimezone(JST).date() != datetime.now(JST).date():
            return ""
    except (TypeError, ValueError):
        return ""

    stage = payload.get("stage", "final")
    results = payload.get("results") or []
    momentum = [r for r in results if r.get("type") == "momentum"]
    reversal = [r for r in results if r.get("type") == "reversal"]
    hit_count = payload.get("hit_count", 0)
    evaluated_count = payload.get("evaluated_count", len(results))
    skipped_count = payload.get("skipped_count", 0)
    skipped_note = (
        f'<div class="muted">{skipped_count}件は現時点の価格が未取得のため未評価です(米国株など)。</div>'
        if skipped_count
        else ""
    )
    title = "30日レンジ 昼時点の中間結果" if stage == "interim" else "30日レンジ 本日の的中結果"
    stage_note = (
        "(12:15時点・場中の暫定値です。取引終了後に最終結果へ更新されます)"
        if stage == "interim"
        else "(取引終了後の最終結果です。昼時点から結果が変わった銘柄は変化を表示しています)"
    )
    summary_label = f"昼時点 {hit_count}/{evaluated_count} 的中" if stage == "interim" else f"本日 {hit_count}/{evaluated_count} 的中"

    return f"""
    <section class="panel">
      <div class="section-title">{title}</div>
      <div class="muted">算出時刻: {_generated_at_label(payload)} {stage_note}</div>
      <div class="muted">今朝の30日レンジ候補が、本日の値動きでプラスになったかを評価しています。</div>
      <div style="margin-top:8px;font-weight:bold;">{summary_label}</div>
      {skipped_note}
      <h3>モメンタム型</h3>
      {_stock_range_eval_result_cards(momentum, stage)}
      <h3>リバーサル型</h3>
      {_stock_range_eval_result_cards(reversal, stage)}
    </section>
    """


def _watchlist_cards(items: list[dict]) -> str:
    cells = []
    for item in items:
        change_text, change_color = _fmt_change(item.get("change"), item.get("change_pct", 0))
        trend_rows = []
        # 先頭の履歴は大きく表示している当日変化と重なるので除外する
        for trend in (item.get("daily_changes") or [])[1:]:
            trend_text, trend_color = _fmt_change(trend.get("change"), trend.get("change_pct", 0))
            trend_rows.append(
                f"""
                <div class="stock-trend">
                  <span>{html.escape(trend.get("label", ""))}</span>
                  <strong style="color:{trend_color};">{trend_text}</strong>
                </div>
                """
            )
        cells.append(
            f"""
            <td class="stock-grid-cell">
              <div class="stock-card">
                <strong class="stock-name">{html.escape(item.get("name", ""))}</strong>
                <div class="muted">{_yahoo_finance_link(item.get("ticker", "-"))}</div>
                <div class="stock-price">{_fmt_decimal(item.get("close"))}</div>
                <div class="stock-change" style="color:{change_color};">{change_text}</div>
                {''.join(trend_rows)}
              </div>
            </td>
            """
        )

    rows = []
    for index in range(0, len(cells), 2):
        left = cells[index]
        right = cells[index + 1] if index + 1 < len(cells) else '<td class="stock-grid-cell"></td>'
        rows.append(f"<tr>{left}{right}</tr>")
    return f'<table class="stock-grid">{"".join(rows)}</table>'


def _watchlist_table(title: str, items: list[dict]) -> str:
    if not items:
        return ""
    return f"""
    <h3>{html.escape(title)}</h3>
    {_watchlist_cards(items)}
    """


def _news_matched_terms(item: dict) -> list[str]:
    terms = []
    for value in (item.get("name"), item.get("ticker")):
        if value:
            terms.append(str(value))
    ticker = str(item.get("ticker") or "")
    if "." in ticker:
        terms.append(ticker.split(".", 1)[0])
    return [term for term in terms if len(term) >= 2]


def _news_related_gain_section(root: Path) -> str:
    movers = _load_json(root / "output" / "news_movers.json")
    if movers and movers.get("status") == "ok":
        matches = movers.get("data") or []
        failures = movers.get("failed_matches") or []
        if matches or failures:
            parts = []
            if matches:
                parts.append(
                    _news_related_gain_cards(
                        matches,
                        "CSV銘柄一覧・略称マスターとニュース見出しを照合した銘柄です。上昇・下落の両方を表示します。",
                    )
                )
            if failures:
                parts.append(_news_related_failures_section(failures))
            return "".join(parts)

    watchlist = _load_json(root / "output" / "stock_watchlist.json")
    news = _load_json(root / "output" / "market_news.json")
    if not watchlist or not news or not watchlist.get("data") or not news.get("data"):
        return ""

    titles = [str(item.get("title") or "") for item in news["data"]]
    matches = []
    for item in watchlist["data"]:
        if item.get("market") != "japan":
            continue
        if float(item.get("change_pct") or 0) <= 0:
            continue

        matched_titles = []
        for title in titles:
            if any(term in title for term in _news_matched_terms(item)):
                matched_titles.append(title)
        if matched_titles:
            matches.append({**item, "matched_titles": matched_titles[:2]})

    if not matches:
        return ""

    matches.sort(key=lambda item: item.get("change_pct") or 0, reverse=True)
    return _news_related_gain_cards(matches, "注目銘柄リスト内で、ニュース見出しに出ていた銘柄です。上昇・下落の両方を表示します。")


def _news_related_gain_cards(matches: list[dict], note: str) -> str:
    cards = []
    for item in matches:
        change_text, change_color = _fmt_change(item.get("change"), item.get("change_pct", 0))
        headlines = "".join(
            f'<div class="news-hit-title">{html.escape(title)}</div>'
            for title in item.get("matched_titles", [])
        )
        cards.append(
            f"""
            <div class="news-hit-card">
              <div>
                <strong>{html.escape(item.get("name", ""))}</strong>
                <span class="muted">{_yahoo_finance_link(item.get("ticker", "-"))}</span>
              </div>
              <div class="news-hit-price">{_fmt_decimal(item.get("close"))}</div>
              <div class="stock-change" style="color:{change_color};">{change_text}</div>
              {headlines}
            </div>
            """
        )

    return f"""
    <section class="panel">
      <div class="section-title">ニュースに出た銘柄</div>
      <div class="muted">{html.escape(note)}</div>
      {''.join(cards)}
    </section>
    """


def _news_related_failures_section(matches: list[dict]) -> str:
    cards = []
    for item in matches:
        headlines = "".join(
            f'<div class="news-hit-title">{html.escape(title)}</div>'
            for title in item.get("matched_titles", [])
        )
        cards.append(
            f"""
            <div class="news-hit-card">
              <div>
                <strong>{html.escape(item.get("name", ""))}</strong>
                <span class="muted">{_yahoo_finance_link(item.get("ticker", "-"))}</span>
              </div>
              <div class="muted">萓｡譬ｼ蜿門ｾ怜､ｱ謨・/div>
              <div class="news-hit-title">{html.escape(item.get("error", "-"))}</div>
              {headlines}
            </div>
            """
        )

    return f"""
    <section class="panel">
      <div class="section-title">ニュースに出たが取得できなかった銘柄</div>
      <div class="muted">yfinance の取得失敗やデータ欠損があった銘柄です。</div>
      {''.join(cards)}
    </section>
    """


def _watchlist_section(root: Path) -> str:
    payload = _load_json(root / "output" / "stock_watchlist.json")
    if not payload:
        return ""

    if payload.get("status") != "ok" or not payload.get("data"):
        error = html.escape(payload.get("error", "unknown error"))
        return f"""
        <div class="alert">
          <strong>注目銘柄データの取得に失敗しました。</strong>
          <div>{error}</div>
        </div>
        """

    data = payload["data"]
    japan_items = [item for item in data if item.get("market") == "japan"]
    us_items = [item for item in data if item.get("market") == "us"]
    other_items = [item for item in data if item.get("market") not in {"japan", "us"}]
    tables = (
        _watchlist_table("日本株", japan_items)
        + _watchlist_table("米国株", us_items)
        + _watchlist_table("その他", other_items)
    )
    if not tables:
        tables = "<p>表示できる注目銘柄データがありません。</p>"

    warnings = ""
    if payload.get("warnings"):
        warning_items = "".join(f"<li>{html.escape(item)}</li>" for item in payload["warnings"])
        warnings = f'<div class="note"><strong>注意</strong><ul>{warning_items}</ul></div>'

    return f"""
    <section class="panel">
      <div class="section-title">注目銘柄 前日比</div>
      {tables}
      {warnings}
    </section>
    """


def _phase_color(phase: str) -> str:
    return {
        "buy_window": "#2563eb",
        "buy_now": "#047857",
        "hold": "#ca8a04",
        "sell_start": "#ea580c",
        "sell_now": "#b91c1c",
        "neutral": "#475569",
        "unknown": "#6b7280",
    }.get(phase, "#475569")


def _dividend_section(root: Path) -> str:
    payload = _load_json(root / "output" / "stock_dividend.json")
    if not payload:
        return ""

    if payload.get("status") != "ok" or not payload.get("data"):
        error = html.escape(payload.get("error", "unknown error"))
        return f"""
        <div class="alert">
          <strong>配当タイミングデータの取得に失敗しました。</strong>
          <div>{error}</div>
        </div>
        """

    cards = []
    for item in payload["data"]:
        phase = item.get("phase", "unknown")
        timing_plan = item.get("timing_plan") or {}
        ex_date = item.get("ex_dividend_date") or "-"
        days = item.get("days_to_ex_dividend")
        days_text = "-" if days is None else str(days)
        cards.append(
            f"""
            <div class="dividend-item">
              <div class="dividend-head">
                <strong>{_yahoo_finance_link(item.get("ticker", "-"))}</strong>
                <span class="badge" style="background:{_phase_color(phase)};">{html.escape(phase)}</span>
              </div>
              <div class="muted">{html.escape(item.get("name", ""))}</div>
              <div class="dividend-message">{html.escape(item.get("message", "-"))}</div>
              <div class="timing-plan">
                <div><strong>権利落ち日:</strong> {html.escape(ex_date)}（あと{html.escape(days_text)}日）</div>
                <div><strong>買い増し期限:</strong> {html.escape(timing_plan.get("buy_deadline_label") or "-")}</div>
                <div><strong>HOLD日:</strong> {html.escape(timing_plan.get("hold_date_label") or "-")}</div>
                <div><strong>売却検討開始:</strong> {html.escape(timing_plan.get("sell_from_label") or "-")}</div>
              </div>
              <div class="muted">日付ソース: {html.escape(item.get("date_source") or "-")} / 確度: {html.escape(item.get("date_confidence") or "-")}</div>
            </div>
            """
        )

    warnings = ""
    if payload.get("warnings"):
        warning_items = "".join(f"<li>{html.escape(item)}</li>" for item in payload["warnings"])
        warnings = f'<div class="note"><strong>注意</strong><ul>{warning_items}</ul></div>'

    return f"""
    <section class="panel">
      <div class="section-title">RYLD / SDIV 配当タイミング</div>
      {''.join(cards)}
      {warnings}
    </section>
    """


# Xトレンド銘柄の目印(2026-10-06の検証: 255件・37営業日、当日の寄り付き→大引け)。
# 「前日終値→終値」では日経平均に60%勝っていたが、その大半は寄り付き前の
# 値の飛び(平均+2.1%)で、寄り付きで買うと全体では47%と優位性はなかった。
# 差が出たのは話題の種類と寄り付きの値の飛び:
#   strong_positive(「爆上げ確定」系)      日経平均に勝った41%(148件)
#   寄り付きで+3%以上値が飛んだ            日経平均に勝った32%(47件)
#   positive かつ 値の飛び+3%未満          日経平均に勝った60%(89件)
# 数字は固定(件数が増えたら検証し直して更新する)。
X_GAP_CHASE_PCT = 3.0


def _x_trend_opening_gaps(tickers: list[str]) -> dict[str, float]:
    """Today's opening gap (previous close -> today's open, %) per 4-digit code.
    Empty before 09:00 or when today's bar isn't available yet."""
    now = datetime.now(JST)
    if now.hour < 9 or now.weekday() >= 5 or not tickers:
        return {}
    try:
        import yfinance as yf

        symbols = [f"{t}.T" for t in tickers]
        data = yf.download(symbols, period="5d", interval="1d", progress=False, auto_adjust=False, group_by="ticker", threads=True)
    except Exception as exc:
        logging.warning("[report_html] opening gap fetch failed: %s", exc)
        return {}
    gaps = {}
    for ticker in tickers:
        try:
            frame = data[f"{ticker}.T"].dropna(subset=["Open", "Close"])
            if len(frame) < 2 or frame.index[-1].date() != now.date():
                continue
            previous_close = float(frame["Close"].iloc[-2])
            if previous_close:
                gaps[ticker] = (float(frame["Open"].iloc[-1]) / previous_close - 1) * 100
        except Exception:
            continue
    return gaps


def _x_trend_badge(sentiment: str, gap: float | None) -> str:
    def _badge(text: str, color: str, background: str) -> str:
        return (
            f'<div style="margin-top:4px;padding:3px 6px;border-radius:6px;font-size:12px;font-weight:bold;'
            f'color:{color};background:{background};">{text}</div>'
        )

    badges = []
    if gap is not None and gap >= X_GAP_CHASE_PCT:
        badges.append(_badge(f"⚠ 寄り付きで{gap:+.1f}%値が飛んだ：追いかけ注意(過去 日経平均に勝った32%・47件)", "#b91c1c", "#fef2f2"))
    if sentiment == "strong_positive":
        badges.append(_badge("⚠ 煽り系：寄り付き後に下げやすい(過去 日経平均に勝った41%・148件)", "#b45309", "#fffbeb"))
    elif sentiment == "positive" and (gap is None or gap < X_GAP_CHASE_PCT):
        condition = f"寄り付きの値の飛び{gap:+.1f}%" if gap is not None else "寄り付きの値の飛びが+3%未満なら"
        badges.append(_badge(f"◎ 材料系・{condition}：過去 日経平均に勝った60%(89件)", "#047857", "#ecfdf5"))
    return "".join(badges)


def _stock_x_trends_section(root: Path) -> str:
    payload = _load_json(root / "output" / "stock_x_trends.json")
    if not payload or payload.get("status") != "ok" or not payload.get("data"):
        return ""

    generated_label = html.escape(payload.get("generated_at", "-"))
    staleness_note = ""
    try:
        generated_at = datetime.fromisoformat(str(payload.get("generated_at")))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=JST)
        generated_label = generated_at.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
        if generated_at.astimezone(JST).date() != datetime.now(JST).date():
            staleness_note = "<div class=\"muted\">⚠ 本日分の検索結果ではありません</div>"
    except (TypeError, ValueError):
        pass

    data = payload["data"]
    keywords = data.get("common_keywords") or data.get("trending_keywords") or []
    stock_findings = data.get("stock_findings") or []
    theme_findings = data.get("theme_findings") or data.get("discovery_findings") or data.get("notable_posts") or []
    keyword_html = "".join(
        f'<span class="keyword-chip">{html.escape(str(value))}</span>' for value in keywords
    )

    eval_by_ticker = {}
    eval_stage = "final"
    eval_payload = _load_json(root / "output" / "stock_x_trends_eval.json")
    # Only attach verdicts if this evaluation actually judged the exact
    # stock_x_trends.json generation being displayed here - otherwise (e.g.
    # a fresh 23:00 reset that hasn't been evaluated yet) a leftover verdict
    # from an earlier cycle could wrongly attach to a same-ticker candidate
    # in the new, not-yet-evaluated list.
    if (
        eval_payload
        and eval_payload.get("status") == "ok"
        and eval_payload.get("trends_generated_at") == payload.get("generated_at")
    ):
        eval_stage = eval_payload.get("stage", "final")
        for r in eval_payload.get("results") or []:
            ticker = str(r.get("ticker") or "").strip()
            if ticker:
                eval_by_ticker[ticker] = r

    # Opening gaps only make sense for today's morning list (07:00); the
    # 23:00 list is for the next session, whose open hasn't happened yet.
    gaps: dict[str, float] = {}
    try:
        generated = datetime.fromisoformat(str(payload.get("generated_at"))).astimezone(JST)
        if generated.date() == datetime.now(JST).date() and generated.hour < 9:
            gaps = _x_trend_opening_gaps(
                sorted({str(i.get("ticker") or "").strip() for i in stock_findings if str(i.get("ticker") or "").strip()})
            )
    except (TypeError, ValueError):
        pass

    finding_cells = []
    for item in stock_findings + theme_findings:
        name = str(item.get("name") or "").strip()
        ticker = str(item.get("ticker") or "").strip()
        header = name or ticker or "-"
        code_line = f"{_yahoo_finance_link(ticker)} / " if ticker else ""
        unverified_note = (
            '<div class="muted">⚠ 銘柄データと未一致(表記ゆれの可能性)</div>'
            if ticker and item.get("verified") is False
            else ""
        )
        verdict_html = ""
        eval_result = eval_by_ticker.get(ticker)
        if eval_result:
            actual_pct = eval_result.get("actual_change_pct")
            hit = eval_result.get("hit")
            actual_text = f"{actual_pct:+.2f}%" if actual_pct is not None else "-"
            verdict_text, verdict_color = _verdict_label(hit, eval_stage, miss_label="外れ")
            flip_note = ""
            if eval_stage == "final" and eval_result.get("flipped"):
                previous_text, _ = _verdict_label(eval_result.get("previous_hit"), "final", miss_label="外れ")
                flip_note = f'<div style="font-size:11px;color:#b45309;margin-top:2px;">昼時点から変化: {previous_text}→{verdict_text}</div>'
            verdict_html = f'<div style="font-weight:bold;color:{verdict_color};">答え合わせ: {verdict_text}(本日{actual_text})</div>{flip_note}'
        finding_cells.append(
            f"""
            <td class="stock-grid-cell">
              <div class="news-hit-card">
                <div class="news-hit-title"><strong>{html.escape(header)}</strong></div>
                <div class="muted">{code_line}{html.escape(str(item.get("sentiment") or "-"))}</div>
                <div class="news-hit-title">{html.escape(str(item.get("reason") or "-"))}</div>
                <div class="muted">{html.escape(str(item.get("detail") or item.get("source") or "-"))}</div>
                {_x_trend_badge(str(item.get("sentiment") or ""), gaps.get(ticker))}
                {unverified_note}
                {verdict_html}
              </div>
            </td>
            """
        )

    finding_rows = []
    for index in range(0, len(finding_cells), 2):
        left = finding_cells[index]
        right = finding_cells[index + 1] if index + 1 < len(finding_cells) else '<td class="stock-grid-cell"></td>'
        finding_rows.append(f"<tr>{left}{right}</tr>")
    finding_html = f'<table class="stock-grid">{"".join(finding_rows)}</table>' if finding_cells else ""

    return f"""
    <section class="panel">
      <div class="section-title">Xトレンド銘柄</div>
      <div class="muted">検索時刻: {generated_label}(1日1回・朝07:00のみ検索し、終日この結果を表示します)</div>
      {staleness_note}
      <div class="muted">目印は過去の検証(寄り付きで買って大引けで売った場合、255件)から: ◎=材料系で寄り付きの値の飛びが小さい(日経平均に勝った60%)、⚠=煽り系や寄り付きで大きく値が飛んだ銘柄(勝率3〜4割)。寄り付きの値の飛びは09:00以降のレポートで表示します。</div>
      <h3>共通キーワード</h3>
      <div style="margin-top:8px;">{keyword_html}</div>
      <h3>銘柄別結果</h3>
      {finding_html}
    </section>
    """


def _rating_news_card(item: dict) -> str:
    title = html.escape(item.get("title") or "-")
    url = item.get("url") or ""
    source = html.escape(item.get("source") or "")
    published_at = item.get("published_at")
    time_text = "-"
    if published_at:
        try:
            dt = datetime.fromisoformat(published_at)
            time_text = dt.astimezone(JST).strftime("%m/%d %H:%M")
        except ValueError:
            pass
    link = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>' if url else title
    return f"""
    <div style="margin-top:6px;padding:6px 8px;background:#f8fafc;border-radius:6px;font-size:12px;">
      {link}
      <div class="muted" style="margin-top:2px;">{source} / {time_text}</div>
    </div>
    """


def _stock_ratings_section(root: Path) -> str:
    payload = _load_json(root / "output" / "stock_ratings.json")
    if not payload or payload.get("status") != "ok":
        return ""
    try:
        generated_at = datetime.fromisoformat(str(payload.get("generated_at")))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=JST)
        if generated_at.astimezone(JST).date() != datetime.now(JST).date():
            return ""
    except (TypeError, ValueError):
        return ""

    data = payload.get("data") or []
    if not data:
        return ""

    cards = []
    for entry in data:
        items_html = "".join(_rating_news_card(item) for item in entry.get("items") or [])
        cards.append(
            f"""
            <div class="news-hit-card">
              <div class="news-hit-title"><strong>{html.escape(entry.get("name", ""))}</strong> <span class="muted">{_yahoo_finance_link(entry.get("ticker", "-"))}</span></div>
              {items_html}
            </div>
            """
        )

    return f"""
    <section class="panel">
      <div class="section-title">レーティング・目標株価の関連ニュース</div>
      <div class="muted">算出時刻: {_generated_at_label(payload)}(1日1回・朝算出し、終日この結果を表示します)</div>
      {"".join(cards)}
    </section>
    """


def _turnover_watch_card(item: dict) -> str:
    change_pct = item.get("change_pct")
    change_color = "#047857" if (change_pct or 0) >= 0 else "#b91c1c"
    change_text = f'{item.get("change", "-")} ({change_pct:+.2f}%)' if change_pct is not None else "-"
    trading_value = item.get("trading_value")
    trading_value_text = f"{int(trading_value):,}万円" if trading_value and str(trading_value).isdigit() else "-"
    return f"""
    <div class="news-hit-card">
      <div class="news-hit-title">
        <strong>{html.escape(item.get("name", ""))}</strong>
        <span class="muted">{_yahoo_finance_link(item.get("ticker", "-"))} / {html.escape(item.get("market", "-"))}</span>
      </div>
      <div style="color:{change_color};font-weight:bold;">{html.escape(item.get("price", "-"))}円 {change_text}</div>
      <div class="muted">売買代金 {trading_value_text}</div>
    </div>
    """


def _stock_turnover_watch_section(root: Path) -> str:
    payload = _load_json(root / "output" / "stock_turnover_watch.json")
    if not payload or payload.get("status") != "ok":
        return ""
    try:
        generated_at = datetime.fromisoformat(str(payload.get("generated_at")))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=JST)
        if generated_at.astimezone(JST).date() != datetime.now(JST).date():
            return ""
    except (TypeError, ValueError):
        return ""

    items = payload.get("data") or []
    if not items:
        return ""

    cards = "".join(_turnover_watch_card(item) for item in items)
    top_n = payload.get("top_n")
    min_change_pct = payload.get("min_change_pct")
    return f"""
    <section class="panel">
      <div class="section-title">売買代金ランキング 新顔ウォッチ</div>
      <div class="muted">算出時刻: {_generated_at_label(payload)}(1日1回・朝算出し、終日この結果を表示します)</div>
      <div class="muted">本日の売買代金上位{html.escape(str(top_n))}銘柄のうち、ウォッチリスト未登録かつ前日比{html.escape(str(min_change_pct))}%以上動いた銘柄です。注目銘柄への追加はご自身の判断でどうぞ。</div>
      {cards}
    </section>
    """


def _nikkei_constituents_section(root: Path) -> str:
    payload = _load_json(root / "output" / "nikkei_constituents.json")
    if not payload:
        return ""
    try:
        generated_at = datetime.fromisoformat(str(payload.get("generated_at")))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=JST)
        if generated_at.astimezone(JST).date() != datetime.now(JST).date():
            return ""
    except (TypeError, ValueError):
        return ""

    items = payload.get("data") or [] if payload.get("status") == "ok" else []
    if items:
        cards = "".join(
            f"""
            <div class="news-hit-card">
              <div class="muted">{html.escape(item.get("date", "-"))}</div>
              <div class="news-hit-title"><a href="{html.escape(item.get("url", ""))}" target="_blank" rel="noopener"><strong>{html.escape(item.get("title", "-"))}</strong></a></div>
            </div>
            """
            for item in items
        )
    else:
        cards = '<div class="muted">なかった</div>'

    return f"""
    <section class="panel">
      <div class="section-title">日経平均 構成銘柄関連の新着発表</div>
      {cards}
    </section>
    """


def _gemini_cost_footer(root: Path) -> str:
    payload = _load_json(root / "output" / "ai_summary.json")
    if not payload or payload.get("status") != "ok":
        return ""
    cost_jpy = payload.get("gemini_cost_jpy")
    call_count = payload.get("gemini_call_count")
    if payload.get("provider") == "claude":
        return """
    <div class="muted" style="margin-top:12px;padding:0 16px 16px;">
      AI要約: Claude(Claude Pro・定額、API料金なし)で生成
    </div>
    """
    if cost_jpy is None:
        return ""
    return f"""
    <div class="muted" style="margin-top:12px;padding:0 16px 16px;">
      Gemini API使用料(概算・本レポート分): 約{cost_jpy:.3f}円（{html.escape(str(call_count or 0))}回呼び出し、1ドル160円換算）
    </div>
    """


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "report.html"
    now = datetime.now(JST)

    body = (
        _nikkei_section(root)
        + _ai_summary_section(root)
        + _news_related_gain_section(root)
        + _stock_range_eval_section(root)
        + _stock_range_score_section(root)
        + _watchlist_section(root)
        + _stock_ratings_section(root)
        + _stock_turnover_watch_section(root)
        + _dividend_section(root)
        + _stock_x_trends_section(root)
        + _gemini_cost_footer(root)
        + _nikkei_constituents_section(root)
    )
    document = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NightlyBatchNotify - {now.strftime("%Y-%m-%d")}</title>
  <style>
    body {{ margin:0; padding:0; background:#f3f4f6; color:#111827; font-family:Arial, sans-serif; }}
    .wrap {{ width:100%; max-width:560px; margin:0 auto; background:#ffffff; }}
    header {{ padding:18px 16px; background:#111827; color:#ffffff; }}
    header h1 {{ margin:0; font-size:20px; }}
    header p {{ margin:8px 0 0; color:#d1d5db; font-size:13px; }}
    main {{ padding:12px 10px; }}
    .panel {{ border:1px solid #d1d5db; border-radius:8px; padding:0 12px 14px; margin:0 0 18px; overflow:hidden; }}
    .market-up {{ border-left:6px solid #047857; background:#f0fdf4; }}
    .market-down {{ border-left:6px solid #b91c1c; background:#fef2f2; }}
    .market-flat {{ border-left:6px solid #64748b; background:#f8fafc; }}
    .section-title {{ margin:0 -12px 14px; padding:12px 14px; background:#111827; color:#ffffff; font-size:15px; line-height:1.25; font-weight:bold; border-bottom:1px solid #111827; }}
    .section-body {{ padding-top:2px; }}
    h3 {{ margin:18px 0 0; font-size:14px; color:#111827; }}
    .state-label {{ display:inline-block; margin:0 0 12px; padding:5px 8px; border-radius:6px; background:#111827; color:#ffffff; font-size:12px; font-weight:bold; }}
    .current {{ font-size:32px; font-weight:bold; line-height:1.1; }}
    .index-card {{ margin-top:10px; padding:11px; background:#ffffff; border:1px solid #e5e7eb; border-radius:8px; }}
    .index-head {{ font-size:15px; line-height:1.35; }}
    .index-current {{ margin-top:8px; font-size:28px; font-weight:bold; line-height:1.1; }}
    .index-grid {{ width:100%; margin-top:6px; border-collapse:separate; border-spacing:4px 0; table-layout:fixed; }}
    .index-grid-cell {{ width:33.33%; padding:0; border:0; vertical-align:top; }}
    .index-mini-card {{ min-height:96px; padding:8px 6px; background:#ffffff; border:1px solid #e5e7eb; border-radius:8px; }}
    .index-mini-label {{ min-height:28px; font-size:12px; font-weight:bold; line-height:1.25; overflow-wrap:anywhere; }}
    .index-mini-current {{ margin-top:6px; font-size:15px; font-weight:bold; line-height:1.15; overflow-wrap:anywhere; }}
    .index-mini-change {{ margin-top:5px; font-size:11px; font-weight:bold; line-height:1.25; }}
    .comparison-box {{ margin-top:12px; padding:10px; background:#ffffff; border:1px solid #d1d5db; border-radius:8px; font-size:13px; line-height:1.5; }}
    .ai-summary {{ font-size:14px; line-height:1.65; }}
    .ai-block {{ margin-top:10px; padding:10px; background:#ffffff; border:1px solid #e5e7eb; border-radius:8px; }}
    .ai-block-title {{ margin-bottom:6px; font-size:13px; font-weight:bold; color:#111827; }}
    .ai-emphasis {{ font-weight:bold; color:#111827; background:#fef3c7; padding:0 2px; border-radius:3px; }}
    .change {{ margin-top:8px; font-size:18px; font-weight:bold; }}
    .stock-grid {{ width:100%; margin-top:8px; border-collapse:separate; border-spacing:6px 8px; table-layout:fixed; }}
    .stock-grid-cell {{ width:50%; padding:0; border:0; vertical-align:top; }}
    .stock-name {{ display:block; min-height:34px; font-size:13px; line-height:1.3; overflow-wrap:anywhere; }}
    .stock-card {{ min-height:132px; padding:9px; background:#ffffff; border:1px solid #e5e7eb; border-radius:8px; }}
    .stock-price {{ margin-top:8px; white-space:nowrap; font-size:14px; font-weight:bold; }}
    .stock-change {{ margin-top:5px; font-size:13px; font-weight:bold; line-height:1.25; }}
    .stock-trend {{ margin-top:4px; color:#64748b; font-size:11px; line-height:1.25; font-weight:normal; }}
    .stock-trend span {{ display:block; }}
    .stock-trend strong {{ display:block; font-size:11px; }}
    .news-hit-card {{ margin-top:10px; padding:10px; background:#ffffff; border:1px solid #e5e7eb; border-radius:8px; }}
    .news-hit-price {{ margin-top:7px; font-size:16px; font-weight:bold; line-height:1.15; }}
    .news-hit-title {{ margin-top:7px; color:#334155; font-size:12px; line-height:1.45; font-weight:normal; }}
    .keyword-chip {{ display:inline-block; margin:0 6px 6px 0; padding:4px 8px; background:#e2e8f0; border-radius:999px; font-size:12px; }}
    .muted {{ margin-top:3px; color:#6b7280; font-size:12px; font-weight:normal; }}
    .badge {{ display:inline-block; padding:3px 7px; border-radius:6px; color:#ffffff; font-size:12px; font-weight:bold; white-space:nowrap; }}
    .dividend-item {{ margin-top:12px; padding:11px; background:#f8fafc; border:1px solid #e5e7eb; border-radius:8px; }}
    .dividend-head {{ display:flex; justify-content:space-between; align-items:center; gap:8px; }}
    .dividend-message {{ margin-top:8px; font-size:13px; line-height:1.5; }}
    .timing-plan {{ margin-top:8px; padding:8px; background:#ffffff; border:1px solid #e5e7eb; border-radius:6px; color:#111827; font-size:13px; font-weight:normal; }}
    .timing-plan div + div {{ margin-top:4px; }}
    .note {{ margin-top:14px; padding:12px; background:#f8fafc; border:1px solid #e5e7eb; color:#334155; font-size:13px; }}
    .note ul {{ margin:8px 0 0; padding-left:18px; }}
    table {{ width:100%; margin-top:14px; border-collapse:collapse; font-size:13px; }}
    th, td {{ padding:9px 6px 9px 0; border-top:1px solid #e5e7eb; text-align:left; vertical-align:top; }}
    th {{ color:#6b7280; font-weight:normal; }}
    td {{ font-weight:bold; }}
    .alert {{ border-left:4px solid #b91c1c; background:#fef2f2; padding:16px; color:#7f1d1d; }}
    footer {{ padding:12px 16px; border-top:1px solid #e5e7eb; color:#6b7280; font-size:12px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>NightlyBatchNotify</h1>
      <p>{now.strftime("%Y-%m-%d %H:%M")} JST</p>
    </header>
    <main>{body}</main>
    <footer>生成日時: {now.isoformat()}</footer>
  </div>
</body>
</html>
"""
    output_path.write_text(document, encoding="utf-8")
    logging.info("[report_html] wrote %s", output_path)


