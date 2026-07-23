import csv
import html
import json
import logging
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path


JST = timezone(timedelta(hours=9), "JST")


def _yahoo_finance_link(ticker: str) -> str:
    ticker = str(ticker or "").strip()
    if not ticker or ticker == "-":
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


def _ai_summary_section(root: Path) -> str:
    payload = _load_json(root / "output" / "ai_summary.json")
    if not payload or payload.get("status") != "ok" or not payload.get("data"):
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
      <div class="muted">生成モデル: {html.escape(payload.get("model", "-"))}</div>
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

    return f"""
    <section class="panel market-{change_state}">
      <div class="section-title">日経平均 / TOPIX 市場概況</div>
      <div class="section-body">
        <div class="state-label">{change_label}</div>
        <div class="muted"><a href="https://search.yahoo.co.jp/realtime/search?p=%E6%97%A5%E7%B5%8C%E3%80%80%E6%99%82%E9%96%93%E5%A4%96" target="_blank" rel="noopener">日経 時間外(Yahoo!リアルタイム検索)</a></div>
      </div>
      {index_grid}
      {warnings}
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
    finding_html = ""
    for item in stock_findings + theme_findings:
        name = str(item.get("name") or "").strip()
        ticker = str(item.get("ticker") or "").strip()
        header = name or ticker or "-"
        code_line = f"{_yahoo_finance_link(ticker)} / " if ticker else ""
        finding_html += f"""
        <div class="news-hit-card">
          <div class="news-hit-title"><strong>{html.escape(header)}</strong></div>
          <div class="muted">{code_line}{html.escape(str(item.get("sentiment") or "-"))}</div>
          <div class="news-hit-title">{html.escape(str(item.get("reason") or "-"))}</div>
          <div class="muted">{html.escape(str(item.get("detail") or item.get("source") or "-"))}</div>
        </div>
        """

    return f"""
    <section class="panel">
      <div class="section-title">Xトレンド銘柄</div>
      <div class="muted">検索時刻: {generated_label}(1日1回・朝07:00のみ検索し、終日この結果を表示します)</div>
      {staleness_note}
      <h3>共通キーワード</h3>
      <div style="margin-top:8px;">{keyword_html}</div>
      <h3>銘柄別結果</h3>
      {finding_html}
    </section>
    """


def _gemini_cost_footer(root: Path) -> str:
    payload = _load_json(root / "output" / "ai_summary.json")
    if not payload or payload.get("status") != "ok":
        return ""
    cost_jpy = payload.get("gemini_cost_jpy")
    call_count = payload.get("gemini_call_count")
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
        + _watchlist_section(root)
        + _dividend_section(root)
        + _stock_x_trends_section(root)
        + _gemini_cost_footer(root)
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


