import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.mail_gmail import send_html_mail


JST = timezone(timedelta(hours=9), "JST")
DEFAULT_SCHEDULE_DAYS = [10, 20, 30]
CONFIG_PATH = Path("config.json")


def _load_config(root: Path) -> dict:
    config_path = root / CONFIG_PATH
    if not config_path.exists():
        return {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return config.get("oricon_album_mail", {}) if isinstance(config, dict) else {}


def _is_scheduled_today(config: dict, now: datetime) -> bool:
    schedule_days = config.get("schedule_days") or DEFAULT_SCHEDULE_DAYS
    return now.day in {int(d) for d in schedule_days}


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _date_label(release_date: str | None) -> str:
    if not release_date:
        return "発売日未定"
    try:
        return datetime.strptime(release_date, "%Y-%m-%d").strftime("%Y年%m月%d日")
    except ValueError:
        return release_date


OTHER_COMPANY_LABEL = "その他/不明"


def _album_card(item: dict) -> str:
    title = html.escape(item.get("title") or "-")
    artist = html.escape(item.get("artist") or "-")
    price = html.escape(item.get("price") or "-")
    url = item.get("url") or ""
    link = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>' if url else title
    return f"""
    <div style="margin-top:8px;padding:8px;background:#ffffff;border:1px solid #e5e7eb;border-radius:6px;">
      <span style="font-weight:bold;">{link}</span>
      <div style="color:#334155;font-size:13px;margin-top:2px;">{artist}</div>
      <div style="color:#6b7280;font-size:12px;margin-top:2px;">{price}</div>
    </div>
    """


def _group_by_date(items: list[dict]) -> list[tuple[str | None, list[dict]]]:
    groups: dict[str | None, list[dict]] = {}
    order: list[str | None] = []
    for item in items:
        release_date = item.get("release_date")
        if release_date not in groups:
            groups[release_date] = []
            order.append(release_date)
        groups[release_date].append(item)
    order.sort(key=lambda d: d or "9999-99-99")
    return [(release_date, groups[release_date]) for release_date in order]


def _group_by_company(items: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for item in items:
        company = (item.get("company") or "").strip() or OTHER_COMPANY_LABEL
        if company not in groups:
            groups[company] = []
            order.append(company)
        groups[company].append(item)
    order.sort(key=lambda name: (name == OTHER_COMPANY_LABEL, name))
    return [(company, groups[company]) for company in order]


def _company_accordion(company: str, items: list[dict]) -> str:
    cards = "".join(_album_card(item) for item in items)
    return f"""
    <details open style="margin-top:10px;background:#f8fafc;border:1px solid #e5e7eb;border-radius:8px;padding:8px 10px;">
      <summary style="cursor:pointer;font-weight:bold;color:#0f172a;">{html.escape(company)}<span style="color:#6b7280;font-weight:normal;font-size:12px;"> ({len(items)}件)</span></summary>
      {cards}
    </details>
    """


# Record-company groups, one mail each, in this order. A company goes to the
# first group with a name fragment it contains; everything else is "その他".
# Overridable via config.json oricon_album_mail.company_groups.
DEFAULT_COMPANY_GROUPS = [
    {
        "name": "主要レコード会社",
        "contains": [
            "ソニー", "Sony", "エイベックス", "avex", "ユニバーサル", "Universal", "ワーナー", "Warner",
            "キングレコード", "ビクター", "Victor", "ポニーキャニオン", "コロムビア", "徳間", "テイチク",
            "日本クラウン", "バンダイナムコ", "ランティス", "アニプレックス", "Aniplex", "EMI",
            "FlyingDog", "フライングドッグ", "ドリーミュージック",
        ],
    },
    {"name": "ウルトラ・ヴァイヴ", "contains": ["ウルトラ・ヴァイヴ"]},
]
REST_GROUP_NAME = "その他"


def _split_by_company_group(items: list[dict], config: dict) -> list[tuple[str, list[dict]]]:
    groups = config.get("company_groups") or DEFAULT_COMPANY_GROUPS
    buckets: dict[str, list[dict]] = {g["name"]: [] for g in groups}
    buckets[REST_GROUP_NAME] = []
    for item in items:
        company = (item.get("company") or "").lower()
        target = next(
            (g["name"] for g in groups if any(word.lower() in company for word in g.get("contains") or [])),
            REST_GROUP_NAME,
        )
        buckets[target].append(item)
    return list(buckets.items())


def _mail_body(items: list[dict], now: datetime, group_name: str, index: int, total: int, overview: list) -> str:
    if items:
        sections = ""
        for release_date, date_group in _group_by_date(items):
            accordions = "".join(
                _company_accordion(company, company_group)
                for company, company_group in _group_by_company(date_group)
            )
            sections += f'<h3 style="margin-top:16px;">{html.escape(_date_label(release_date))}</h3>{accordions}'
    else:
        sections = '<div style="color:#6b7280;">対象期間のアルバム新譜情報がありませんでした。</div>'
    heading = f"アルバム新譜情報(今週〜7週先){' - ' + html.escape(group_name) if group_name else ''}"
    parts = " / ".join(
        f"{'<b>' if i == index else ''}{i}. {html.escape(n)} {c}件{'</b>' if i == index else ''}"
        for i, (n, c) in enumerate(overview, start=1)
    ) if total > 1 else ""
    parts_html = f'<div style="color:#334155;font-size:12px;margin-top:4px;">全{total}通: {parts}</div>' if parts else ""
    return f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>{heading}</h2>
        <div style="color:#6b7280;font-size:12px;">{now.strftime('%Y-%m-%d %H:%M')} JST時点</div>
        {parts_html}
        {sections}
      </body>
    </html>
    """


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "oricon_album_mail.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()

    config = _load_config(root)
    if not _is_scheduled_today(config, now):
        result = {
            "module": "oricon_album_mail",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": f"Not a scheduled day (schedule_days={config.get('schedule_days', DEFAULT_SCHEDULE_DAYS)}).",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[oricon_album_mail] skipped: not a scheduled day")
        return

    data = _load_json(root / "output" / "oricon_album.json")
    if not data:
        result = {
            "module": "oricon_album_mail",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "oricon_album output is not available.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[oricon_album_mail] skipped: oricon_album output is not available")
        return

    items = data.get("data") or []
    gmail_address = os.getenv("GMAIL_ADDRESS", "").strip()
    app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    mail_to = os.getenv("MAIL_TO", "").strip()
    missing = [
        name
        for name, value in [
            ("GMAIL_ADDRESS", gmail_address),
            ("GMAIL_APP_PASSWORD", app_password),
            ("MAIL_TO", mail_to),
        ]
        if not value
    ]
    result = {
        "module": "oricon_album_mail",
        "generated_at": generated_at,
        "status": "ok",
        "item_count": len(items),
        "mails": [],
    }
    if missing:
        result["status"] = "error"
        result["reason"] = "Missing Gmail settings: " + ", ".join(missing)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.warning("[oricon_album_mail] mail skipped: missing Gmail settings: %s", ", ".join(missing))
        return

    # One mail with everything was too long (366 albums on 2026-10-10, 150 of
    # them from ウルトラ・ヴァイヴ alone), so it is split into a few mails by
    # record company group, nothing dropped (user request 2026-10-11).
    groups = [(name, group) for name, group in _split_by_company_group(items, config) if group]
    if not groups:
        groups = [("", [])]
    for index, (group_name, group_items) in enumerate(groups, start=1):
        part = f"({index}/{len(groups)} {group_name} {len(group_items)}件)" if group_name else ""
        subject = f"アルバム新譜情報 {now.strftime('%Y-%m-%d')}{part}"
        body = _mail_body(group_items, now, group_name, index, len(groups), [(n, len(g)) for n, g in groups])
        try:
            send_html_mail(gmail_address, app_password, mail_to, subject, body)
            result["mails"].append({"subject": subject, "item_count": len(group_items), "status": "sent"})
            logging.info("[oricon_album_mail] sent %s", subject)
        except Exception as exc:
            result["status"] = "error"
            result["mails"].append({"subject": subject, "item_count": len(group_items), "status": "error", "error": str(exc)})
            logging.error("[oricon_album_mail] mail send failed (%s): %s", subject, exc)

    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
