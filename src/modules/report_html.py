import html
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path


JST = timezone(timedelta(hours=9), "JST")


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
        return "flat", "前日比: 不明"
    if change > 0:
        return "up", "前日比: 上昇"
    if change < 0:
        return "down", "前日比: 下落"
    return "flat", "前日比: 横ばい"


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _nikkei_section(root: Path) -> str:
    payload = _load_json(root / "output" / "stock_nikkei.json")
    if not payload:
        return "<p>日経225先物データファイルは生成されていません。</p>"

    if payload.get("status") != "ok" or not payload.get("data"):
        error = html.escape(payload.get("error", "unknown error"))
        return f"""
        <div class="alert">
          <strong>日経225先物データの取得に失敗しました。</strong>
          <div>{error}</div>
        </div>
        """

    data = payload["data"]
    change_text, change_color = _fmt_change(data.get("change"), data.get("change_pct", 0))
    change_state, change_label = _change_state(data.get("change"))

    return f"""
    <section class="panel market-{change_state}">
      <div class="section-title">日経225先物 ({html.escape(data.get("symbol", "NKD=F"))})</div>
      <div class="section-body">
        <div class="state-label">{change_label}</div>
        <div class="current">{_fmt_number(data.get("current"))}</div>
        <div class="change" style="color:{change_color};">{change_text}</div>
      </div>
      <table>
        <tr><th>始値</th><td>{_fmt_number(data.get("open"))}</td></tr>
        <tr><th>高値</th><td>{_fmt_number(data.get("high"))}</td></tr>
        <tr><th>安値</th><td>{_fmt_number(data.get("low"))}</td></tr>
        <tr><th>前回終値</th><td>{_fmt_number(data.get("prev_close"))}</td></tr>
        <tr><th>出来高</th><td>{_fmt_number(data.get("volume"))}</td></tr>
      </table>
    </section>
    """


def _watchlist_cards(items: list[dict]) -> str:
    cards = []
    for item in items:
        change_text, change_color = _fmt_change(item.get("change"), item.get("change_pct", 0))
        cards.append(
            f"""
            <div class="stock-card">
              <table class="mini-table">
                <tr>
                  <td>
                    <strong class="stock-name">{html.escape(item.get("name", ""))}</strong>
                    <div class="muted">{html.escape(item.get("ticker", "-"))}</div>
                  </td>
                  <td class="stock-price">{_fmt_decimal(item.get("close"))}</td>
                </tr>
              </table>
              <div class="stock-change" style="color:{change_color};">{change_text}</div>
            </div>
            """
        )
    return "".join(cards)


def _watchlist_table(title: str, items: list[dict]) -> str:
    if not items:
        return ""
    return f"""
    <h3>{html.escape(title)}</h3>
    {_watchlist_cards(items)}
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
                <strong>{html.escape(item.get("ticker", "-"))}</strong>
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


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "report.html"
    now = datetime.now(JST)

    body = _nikkei_section(root) + _watchlist_section(root) + _dividend_section(root)
    document = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NightlyBatchNotify - {now.strftime("%Y-%m-%d")}</title>
  <style>
    body {{ margin:0; padding:0; background:#f3f4f6; color:#111827; font-family:Arial, sans-serif; }}
    .wrap {{ max-width:430px; margin:0 auto; background:#ffffff; }}
    header {{ padding:18px 16px; background:#111827; color:#ffffff; }}
    header h1 {{ margin:0; font-size:20px; }}
    header p {{ margin:8px 0 0; color:#d1d5db; font-size:13px; }}
    main {{ padding:14px 12px; }}
    .panel {{ border:1px solid #d1d5db; border-radius:8px; padding:0 12px 14px; margin:0 0 18px; overflow:hidden; }}
    .market-up {{ border-left:6px solid #047857; background:#f0fdf4; }}
    .market-down {{ border-left:6px solid #b91c1c; background:#fef2f2; }}
    .market-flat {{ border-left:6px solid #64748b; background:#f8fafc; }}
    .section-title {{ margin:0 -12px 16px; padding:12px 14px; background:#111827; color:#ffffff; font-size:16px; font-weight:bold; border-bottom:1px solid #111827; }}
    .section-body {{ padding-top:2px; }}
    h3 {{ margin:18px 0 0; font-size:14px; color:#111827; }}
    .state-label {{ display:inline-block; margin:0 0 12px; padding:5px 8px; border-radius:6px; background:#111827; color:#ffffff; font-size:12px; font-weight:bold; }}
    .current {{ font-size:32px; font-weight:bold; line-height:1.1; }}
    .change {{ margin-top:8px; font-size:18px; font-weight:bold; }}
    .stock-name {{ display:block; font-size:15px; line-height:1.25; }}
    .stock-card {{ margin-top:9px; padding:10px; background:#ffffff; border:1px solid #e5e7eb; border-radius:8px; }}
    .mini-table {{ width:100%; margin:0; border-collapse:collapse; }}
    .mini-table td {{ border:0; padding:0; vertical-align:top; }}
    .stock-price {{ width:35%; white-space:nowrap; font-size:14px; font-weight:bold; text-align:right; }}
    .stock-change {{ margin-top:6px; font-size:14px; font-weight:bold; }}
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
