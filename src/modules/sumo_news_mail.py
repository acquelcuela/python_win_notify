import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.mail_gmail import send_html_mail


JST = timezone(timedelta(hours=9), "JST")
OTHER_LABEL = "その他"
RANK_ORDER = ["横綱", "大関", "関脇", "小結", "前頭", "十両"]
RANK_LETTER_ORDER = {"Y": 0, "O": 1, "S": 2, "K": 3, "M": 4, "J": 5}


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _news_card(item: dict) -> str:
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
    <div style="margin-top:8px;padding:8px;background:#ffffff;border:1px solid #e5e7eb;border-radius:6px;">
      <span style="font-weight:bold;">{link}</span>
      <div style="color:#6b7280;font-size:12px;margin-top:2px;">{source} / {time_text}</div>
    </div>
    """


def _group_by_wrestler(items: list[dict]) -> list[tuple[str, str | None, list[dict]]]:
    """Groups items by wrestler, ordered by peak rank (横綱 first, 十両
    last, unranked/unrecognized wrestlers after that), with untagged items
    collected last under OTHER_LABEL. Within a rank tier, groups keep the
    order wrestlers first appeared in (items are already sorted
    newest-first)."""
    groups: dict[str, list[dict]] = {}
    peak_ranks: dict[str, str | None] = {}
    order: list[str] = []
    other: list[dict] = []
    for item in items:
        wrestler = item.get("wrestler")
        if not wrestler:
            other.append(item)
            continue
        if wrestler not in groups:
            groups[wrestler] = []
            peak_ranks[wrestler] = item.get("wrestler_peak_rank")
            order.append(wrestler)
        groups[wrestler].append(item)

    def _rank_sort_key(name: str) -> int:
        rank = peak_ranks.get(name)
        return RANK_ORDER.index(rank) if rank in RANK_ORDER else len(RANK_ORDER)

    order.sort(key=_rank_sort_key)
    result = [(name, peak_ranks[name], groups[name]) for name in order]
    if other:
        result.append((OTHER_LABEL, None, other))
    return result


RANK_LABEL = {"Y": "横綱", "O": "大関", "S": "関脇", "K": "小結", "M": "前頭", "J": "十両"}


def _rank_code_parts(rank_code: str) -> tuple[str, int, str] | None:
    m = re.match(r"([A-Z][a-z]?)(\d+)([ew])", rank_code)
    if not m:
        return None
    letter, num, side = m.groups()
    return letter, int(num), side


def _tier_label(letter: str, num: int) -> str:
    label = RANK_LABEL.get(letter, letter)
    if letter in ("Y", "O", "S", "K") and num == 1:
        return label
    return f"{label}{num}"


def _win_loss_counts(rikishi_id: str | None, results: dict, day_no: int) -> tuple[int, int]:
    record = results.get(rikishi_id, {}) if rikishi_id else {}
    wins = sum(1 for day in range(1, day_no + 1) if (record.get(str(day)) or {}).get("win") is True)
    losses = sum(1 for day in range(1, day_no + 1) if (record.get(str(day)) or {}).get("win") is False)
    return wins, losses


def _marks_and_record(rikishi_id: str | None, results: dict, day_no: int) -> tuple[str, str]:
    record = results.get(rikishi_id, {}) if rikishi_id else {}
    marks = ""
    for day in range(1, day_no + 1):
        day_result = record.get(str(day))
        if day_result is None:
            marks += '<span style="color:#cbd5e1;">・</span>'
        elif day_result.get("win"):
            marks += '<span style="color:#dc2626;font-weight:bold;">○</span>'
        else:
            marks += '<span style="color:#2563eb;font-weight:bold;">●</span>'
    wins, losses = _win_loss_counts(rikishi_id, results, day_no)
    return marks, f"{wins}勝{losses}敗"


# Warm palette for top records (1st place reddest, fading toward gold),
# cool palette for worst records (1st-worst darkest navy, fading toward a
# lighter blue) - one shade per record-rank, not per individual, so ties
# within a record share a shade.
TOP_COLORS = ["#b91c1c", "#ea580c", "#ca8a04"]
WORST_COLORS = ["#1e3a8a", "#2563eb"]


def _ranked_groups(entries: list[dict], results: dict, day_no: int):
    """Groups wrestlers with at least one decided bout by (wins, losses),
    then picks the top-3 and worst-2 records (not individuals) - ties share
    a record group, so e.g. nine wrestlers can all be "top" at 4-1."""
    ranked = []
    for e in entries:
        wins, losses = _win_loss_counts(e["rikishi_id"], results, day_no)
        if wins + losses == 0:
            continue
        ranked.append((e, wins, losses))

    groups: dict[tuple[int, int], list[dict]] = {}
    for e, wins, losses in ranked:
        groups.setdefault((wins, losses), []).append(e)

    top_records = sorted(groups.keys(), key=lambda r: (-r[0], r[1]))[:3]
    worst_records = sorted(groups.keys(), key=lambda r: (-r[1], -r[0]))[:2]
    return groups, top_records, worst_records


def _id_color_map(groups: dict, top_records: list, worst_records: list) -> dict[str, str]:
    colors: dict[str, str] = {}
    for rank, record in enumerate(top_records):
        color = TOP_COLORS[min(rank, len(TOP_COLORS) - 1)]
        for e in groups[record]:
            colors[e["rikishi_id"]] = color
    for rank, record in enumerate(worst_records):
        color = WORST_COLORS[min(rank, len(WORST_COLORS) - 1)]
        for e in groups[record]:
            colors[e["rikishi_id"]] = color
    return colors


def _side_cell(entry: dict | None, results: dict, day_no: int, id_colors: dict[str, str]) -> str:
    if not entry:
        return (
            '<td style="padding:2px 6px;"></td>'
            '<td style="padding:2px 6px;letter-spacing:2px;"></td>'
            '<td style="padding:2px 6px;"></td>'
        )
    marks, record_text = _marks_and_record(entry["rikishi_id"], results, day_no)
    name = html.escape(entry["kanji"])
    wins, losses = _win_loss_counts(entry["rikishi_id"], results, day_no)
    color = id_colors.get(entry["rikishi_id"])
    if wins == 0 and losses == 0:
        # No decided bouts at all so far this basho - almost certainly kyujo
        # (absent), so gray out the name rather than leaving it looking like
        # an ordinary un-highlighted wrestler.
        name_style = "padding:2px 6px;color:#9ca3af;"
    elif color:
        name_style = f"padding:2px 6px;color:{color};font-weight:bold;"
    else:
        name_style = "padding:2px 6px;font-weight:bold;"
    return (
        f'<td style="{name_style}">{name}</td>'
        f'<td style="padding:2px 6px;letter-spacing:2px;">{marks}</td>'
        f'<td style="padding:2px 6px;color:#6b7280;font-size:11px;white-space:nowrap;">{record_text}</td>'
    )


def _hoshitori_table(entries: list[dict], results: dict, day_no: int, label: str, id_colors: dict[str, str]) -> str:
    tiers: dict[tuple[str, int], dict[str, dict]] = {}
    for e in entries:
        parts = _rank_code_parts(e["rank_code"])
        if not parts:
            continue
        letter, num, side = parts
        tiers.setdefault((letter, num), {})[side] = e

    rows = ""
    for (letter, num), sides in sorted(
        tiers.items(), key=lambda kv: (RANK_LETTER_ORDER.get(kv[0][0], 90), kv[0][1])
    ):
        tier_label = html.escape(_tier_label(letter, num))
        east_cells = _side_cell(sides.get("e"), results, day_no, id_colors)
        west_cells = _side_cell(sides.get("w"), results, day_no, id_colors)
        rows += (
            f'<tr><td style="padding:2px 6px;color:#6b7280;font-size:11px;white-space:nowrap;">{tier_label}</td>'
            f"{east_cells}{west_cells}</tr>"
        )
    return (
        f'<h3 style="margin-top:16px;">{html.escape(label)}星取表</h3>'
        f'<table style="border-collapse:collapse;font-size:13px;">'
        f'<tr style="color:#6b7280;font-size:11px;"><td></td>'
        f'<td colspan="3" style="text-align:center;">東</td>'
        f'<td colspan="3" style="text-align:center;">西</td></tr>'
        f"{rows}</table>"
    )


def _leaderboard(entries: list[dict], results: dict, day_no: int, label: str) -> str:
    groups, top_records, worst_records = _ranked_groups(entries, results, day_no)

    def _list_items(sorted_records, palette):
        items = ""
        for rank, record in enumerate(sorted_records):
            wins, losses = record
            color = palette[min(rank, len(palette) - 1)]
            names = "、".join(html.escape(e["kanji"]) for e in groups[record])
            items += f'<li style="color:{color};font-weight:bold;">{wins}勝{losses}敗　{names}</li>'
        return items

    return (
        f'<h3 style="margin-top:16px;">{html.escape(label)}成績上位・下位</h3>'
        f'<div style="font-size:13px;">'
        f'<b>勝ち星トップ3</b><ul style="margin:4px 0 8px;">{_list_items(top_records, TOP_COLORS)}</ul>'
        f'<b>負け込みワースト2</b><ul style="margin:4px 0 8px;">{_list_items(worst_records, WORST_COLORS)}</ul>'
        f"</div>"
    )


def _hoshitori_section(root: Path, config: dict) -> str:
    """Renders makuuchi/juryo star charts for the tail of the mail - skipped
    entirely (returns "") whenever sumo_basho.code isn't configured or its
    cached banzuke/hoshitori state files don't exist, per the design where a
    missing local file means "not in a honbasho period, omit the section"."""
    code = str((config.get("sumo_basho") or {}).get("code") or "").strip()
    if not code:
        return ""
    banzuke = _load_json(root / "state" / f"sumo_banzuke_{code}.json")
    hoshitori = _load_json(root / "state" / f"sumo_hoshitori_{code}.json")
    if not banzuke or not hoshitori:
        return ""
    days_done = hoshitori.get("days_done") or []
    if not days_done:
        return ""
    day_no = max(days_done)
    results = hoshitori.get("results") or {}
    title = html.escape(banzuke.get("title") or "")
    section = f'<div style="color:#6b7280;font-size:12px;margin-top:24px;">{title} {day_no}日目終了時点</div>'
    for entries, label in ((banzuke.get("makuuchi") or [], "幕内"), (banzuke.get("juryo") or [], "十両")):
        groups, top_records, worst_records = _ranked_groups(entries, results, day_no)
        id_colors = _id_color_map(groups, top_records, worst_records)
        section += _leaderboard(entries, results, day_no, label)
        section += _hoshitori_table(entries, results, day_no, label, id_colors)
    return section


def _load_config(root: Path) -> dict:
    path = root / "config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "sumo_news_mail.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()

    data = _load_json(root / "output" / "sumo_news.json")
    if not data:
        # sumo_news never ran (missing output entirely) - a pipeline
        # problem rather than "no news today", so stay silent rather than
        # sending a misleading "no news" mail.
        result = {
            "module": "sumo_news_mail",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "sumo_news output is not available.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[sumo_news_mail] skipped: sumo_news output is not available")
        return

    items = data.get("data") or []
    if items:
        sections = ""
        for wrestler, peak_rank, group in _group_by_wrestler(items):
            cards = "".join(_news_card(item) for item in group)
            rank_badge = (
                f'<span style="color:#6b7280;font-size:12px;font-weight:normal;">(最高位: {html.escape(peak_rank)})</span>'
                if peak_rank
                else ""
            )
            sections += f'<h3 style="margin-top:16px;">{html.escape(wrestler)} {rank_badge}</h3>{cards}'
    else:
        sections = '<div style="color:#6b7280;">本日は大相撲関連の新着記事がありませんでした。</div>'

    hoshitori_section = _hoshitori_section(root, _load_config(root))

    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>大相撲ニュース</h2>
        <div style="color:#6b7280;font-size:12px;">{now.strftime('%Y-%m-%d %H:%M')} JST時点</div>
        {sections}
        {hoshitori_section}
      </body>
    </html>
    """

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
        "module": "sumo_news_mail",
        "generated_at": generated_at,
        "status": "ok",
        "item_count": len(items),
    }
    if missing:
        result["status"] = "error"
        result["reason"] = "Missing Gmail settings: " + ", ".join(missing)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.warning("[sumo_news_mail] mail skipped: missing Gmail settings: %s", ", ".join(missing))
        return

    subject = f"[NightlyBatchNotify] 大相撲ニュース {now.strftime('%Y-%m-%d %H:%M')}"
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[sumo_news_mail] sent %d news items", len(items))
    except Exception as exc:
        result["status"] = "error"
        result["reason"] = str(exc)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[sumo_news_mail] mail send failed: %s", exc)
        return

    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
