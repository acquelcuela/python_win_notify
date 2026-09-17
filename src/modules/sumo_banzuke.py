from __future__ import annotations

import json
import logging
import re
import ssl
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import certifi
from bs4 import BeautifulSoup


JST = timezone(timedelta(hours=9), "JST")
SUMODB_BASE = "http://sumodb.sumogames.de"
DIVISIONS = {"M": "makuuchi", "J": "juryo"}


def _load_config(root: Path) -> dict:
    path = root / "config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[sumo_banzuke] config.json is invalid.")
        return {}


def _fetch(url: str) -> str:
    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_date_range(html: str) -> tuple[str, str, str] | None:
    """Extracts (title, start_date, end_date) from a banzuke page's
    "<h1>Aki 2026</h1>...<h3>September 13, 2026 - September 27, 2026</h3>"
    header - sumodb publishes the actual honbasho date range on the page
    itself, so this is authoritative rather than a manual guess."""
    title_match = re.search(r"<h1>([^<]+)</h1>", html)
    range_match = re.search(
        r"<h3>([A-Za-z]+ \d{1,2}, \d{4}) - ([A-Za-z]+ \d{1,2}, \d{4})</h3>", html
    )
    if not title_match or not range_match:
        return None
    title = title_match.group(1).strip()
    start = datetime.strptime(range_match.group(1), "%B %d, %Y").strftime("%Y-%m-%d")
    end = datetime.strptime(range_match.group(2), "%B %d, %Y").strftime("%Y-%m-%d")
    return title, start, end


def _parse_division_table(soup: BeautifulSoup, anchor_name: str, division: str) -> list[dict]:
    anchor = soup.find("a", attrs={"name": anchor_name})
    if not anchor:
        return []
    table = anchor.find_next("table", class_="banzuke")
    if not table:
        return []
    tbody = table.find("tbody")
    if not tbody:
        return []

    entries = []
    counters: dict[str, int] = {}
    for tr in tbody.find_all("tr", recursive=False):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 5:
            continue
        east_td, rank_td, west_td = tds[1], tds[2], tds[3]
        short_rank = rank_td.get_text(strip=True)
        if not short_rank:
            continue
        # M/J short_rank already includes the number (e.g. "M17", "J14");
        # sanyaku ranks (Y/O/S/K) are letter-only and need a per-tier counter.
        rank_num_match = re.match(r"^([A-Za-z]+)(\d+)$", short_rank)
        if rank_num_match:
            letter, num = rank_num_match.group(1), int(rank_num_match.group(2))
        else:
            letter = short_rank
            counters[letter] = counters.get(letter, 0) + 1
            num = counters[letter]
        for td, side in ((east_td, "e"), (west_td, "w")):
            if "emptycell" in (td.get("class") or []):
                continue
            a = td.find("a")
            if not a:
                continue
            title = a.get("title") or ""
            parts = [p.strip() for p in title.split(",")]
            kanji = parts[0] if parts and parts[0] else None
            heya = parts[1] if len(parts) > 1 else None
            hometown = parts[2] if len(parts) > 2 else None
            romaji = a.get_text(strip=True)
            href = a.get("href") or ""
            id_match = re.search(r"r=(\d+)", href)
            if not id_match or not kanji or not romaji:
                continue
            entries.append(
                {
                    "rikishi_id": id_match.group(1),
                    "kanji": kanji,
                    "romaji": romaji,
                    "heya": heya,
                    "hometown": hometown,
                    "rank_code": f"{letter}{num}{side[0]}",
                    "division": division,
                }
            )
    return entries


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "sumo_banzuke.json"
    generated_at = datetime.now(JST).isoformat()

    config = _load_config(root).get("sumo_basho") or {}
    code = str(config.get("code") or "").strip()
    if not code:
        result = {
            "module": "sumo_banzuke",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "config.json sumo_basho.code is not set - not in a honbasho period.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    state_path = root / "state" / f"sumo_banzuke_{code}.json"
    if state_path.exists():
        result = {
            "module": "sumo_banzuke",
            "generated_at": generated_at,
            "status": "ok",
            "reason": f"banzuke already cached at {state_path.name}.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    try:
        html = _fetch(f"{SUMODB_BASE}/Banzuke.aspx?b={code}")
    except Exception as exc:
        result = {
            "module": "sumo_banzuke",
            "generated_at": generated_at,
            "status": "error",
            "reason": f"fetch failed: {exc}",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[sumo_banzuke] fetch failed for code %s: %s", code, exc)
        return

    date_range = _parse_date_range(html)
    if not date_range:
        result = {
            "module": "sumo_banzuke",
            "generated_at": generated_at,
            "status": "error",
            "reason": "could not find basho title/date range on the banzuke page.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[sumo_banzuke] could not parse date range for code %s", code)
        return
    title, start_date, end_date = date_range

    soup = BeautifulSoup(html, "html.parser")
    makuuchi = _parse_division_table(soup, "M", "makuuchi")
    juryo = _parse_division_table(soup, "J", "juryo")

    if not makuuchi or not juryo:
        result = {
            "module": "sumo_banzuke",
            "generated_at": generated_at,
            "status": "error",
            "reason": f"parsed makuuchi={len(makuuchi)} juryo={len(juryo)} entries - expected ~42/~28.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[sumo_banzuke] unexpected entry counts for code %s: makuuchi=%d juryo=%d", code, len(makuuchi), len(juryo))
        return

    banzuke = {
        "code": code,
        "title": title,
        "start_date": start_date,
        "end_date": end_date,
        "makuuchi": makuuchi,
        "juryo": juryo,
        "fetched_at": generated_at,
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(banzuke, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "module": "sumo_banzuke",
        "generated_at": generated_at,
        "status": "ok",
        "reason": f"fetched and cached {title} ({start_date} - {end_date}): makuuchi={len(makuuchi)} juryo={len(juryo)}.",
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[sumo_banzuke] cached banzuke for %s (%s): makuuchi=%d juryo=%d", code, title, len(makuuchi), len(juryo))


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
