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


def _fmt_change(change, change_pct) -> tuple[str, str]:
    if change is None:
        return "-", "#334155"
    sign = "+" if change >= 0 else ""
    color = "#047857" if change >= 0 else "#b91c1c"
    return f"{sign}{int(change):,} ({sign}{change_pct:.2f}%)", color


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _nikkei_section(root: Path) -> str:
    payload = _load_json(root / "output" / "stock_nikkei.json")
    if not payload:
        return "<p>No Nikkei data file was generated.</p>"

    if payload.get("status") != "ok" or not payload.get("data"):
        error = html.escape(payload.get("error", "unknown error"))
        return f"""
        <div class="alert">
          <strong>Nikkei 225 futures data failed.</strong>
          <div>{error}</div>
        </div>
        """

    data = payload["data"]
    change_text, change_color = _fmt_change(data.get("change"), data.get("change_pct", 0))

    return f"""
    <section class="panel">
      <h2>Nikkei 225 Futures ({html.escape(data.get("symbol", "NKD=F"))})</h2>
      <div class="current">{_fmt_number(data.get("current"))}</div>
      <div class="change" style="color:{change_color};">{change_text}</div>
      <table>
        <tr><th>Open</th><td>{_fmt_number(data.get("open"))}</td></tr>
        <tr><th>High</th><td>{_fmt_number(data.get("high"))}</td></tr>
        <tr><th>Low</th><td>{_fmt_number(data.get("low"))}</td></tr>
        <tr><th>Previous close</th><td>{_fmt_number(data.get("prev_close"))}</td></tr>
        <tr><th>Volume</th><td>{_fmt_number(data.get("volume"))}</td></tr>
      </table>
    </section>
    """


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "report.html"
    now = datetime.now(JST)

    body = _nikkei_section(root)
    document = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NightlyBatchNotify - {now.strftime("%Y-%m-%d")}</title>
  <style>
    body {{ margin:0; padding:0; background:#f3f4f6; color:#111827; font-family:Arial, sans-serif; }}
    .wrap {{ max-width:640px; margin:0 auto; background:#ffffff; }}
    header {{ padding:24px; background:#111827; color:#ffffff; }}
    header h1 {{ margin:0; font-size:22px; }}
    header p {{ margin:8px 0 0; color:#d1d5db; font-size:13px; }}
    main {{ padding:24px; }}
    .panel {{ border:1px solid #e5e7eb; border-radius:8px; padding:20px; }}
    h2 {{ margin:0 0 14px; font-size:16px; }}
    .current {{ font-size:34px; font-weight:bold; line-height:1.1; }}
    .change {{ margin-top:8px; font-size:18px; font-weight:bold; }}
    table {{ width:100%; margin-top:18px; border-collapse:collapse; font-size:14px; }}
    th, td {{ padding:10px 0; border-top:1px solid #e5e7eb; text-align:left; }}
    th {{ width:48%; color:#6b7280; font-weight:normal; }}
    td {{ font-weight:bold; }}
    .alert {{ border-left:4px solid #b91c1c; background:#fef2f2; padding:16px; color:#7f1d1d; }}
    footer {{ padding:14px 24px; border-top:1px solid #e5e7eb; color:#6b7280; font-size:12px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>NightlyBatchNotify</h1>
      <p>{now.strftime("%Y-%m-%d %H:%M")} JST</p>
    </header>
    <main>{body}</main>
    <footer>Generated at {now.isoformat()}</footer>
  </div>
</body>
</html>
"""
    output_path.write_text(document, encoding="utf-8")
    logging.info("[report_html] wrote %s", output_path)
