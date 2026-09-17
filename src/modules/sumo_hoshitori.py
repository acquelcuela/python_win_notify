from __future__ import annotations

import json
import logging
import re
import ssl
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import certifi


JST = timezone(timedelta(hours=9), "JST")
SUMODB_BASE = "http://sumodb.sumogames.de"
BOUT_RE = re.compile(r"([A-Z][a-z]?\d+[ew])\s+([^\s(]+)\s*\((\d+-\d+(?:-\d+)?)\)")
MAX_DAYS = 15


def _load_config(root: Path) -> dict:
    path = root / "config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[sumo_hoshitori] config.json is invalid.")
        return {}


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _fetch(url: str) -> str:
    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _fetch_day_results(code: str, day: int) -> dict[str, tuple[bool, str]]:
    """Fetches one day's torikumi text and returns {rank_code: (won, opponent_rank_code)}
    for every bout found. sumodb sometimes omits the kimarite field depending on how
    recently the day's data was entered, so the parser only relies on left-entry-wins-
    right-entry-loses ordering within a line, which has held in every format seen."""
    html = _fetch(f"{SUMODB_BASE}/Results_text.aspx?b={code}&d={day}")
    pre_match = re.search(r"<pre>(.*?)</pre>", html, re.S)
    if not pre_match:
        return {}
    results: dict[str, tuple[bool, str]] = {}
    for line in pre_match.group(1).splitlines():
        matches = BOUT_RE.findall(line)
        if len(matches) != 2:
            continue
        (winner_code, _winner_name, _wr), (loser_code, _loser_name, _lr) = matches
        results[winner_code] = (True, loser_code)
        results[loser_code] = (False, winner_code)
    return results


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "sumo_hoshitori.json"
    generated_at = datetime.now(JST).isoformat()

    config = _load_config(root).get("sumo_basho") or {}
    code = str(config.get("code") or "").strip()
    if not code:
        result = {
            "module": "sumo_hoshitori",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "config.json sumo_basho.code is not set - not in a honbasho period.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    banzuke_path = root / "state" / f"sumo_banzuke_{code}.json"
    banzuke = _load_json(banzuke_path)
    if not banzuke:
        result = {
            "module": "sumo_hoshitori",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": f"no cached banzuke file ({banzuke_path.name}) - run sumo_banzuke first.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    start_date = datetime.strptime(banzuke["start_date"], "%Y-%m-%d").date()
    end_date = datetime.strptime(banzuke["end_date"], "%Y-%m-%d").date()
    today = datetime.now(JST).date()
    if today < start_date or today > end_date:
        result = {
            "module": "sumo_hoshitori",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": f"today ({today}) is outside the honbasho period ({start_date} - {end_date}).",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    day_no = min((today - start_date).days + 1, MAX_DAYS)

    hoshitori_path = root / "state" / f"sumo_hoshitori_{code}.json"
    state = _load_json(hoshitori_path) or {"code": code, "days_done": [], "results": {}}
    days_done = set(state.get("days_done") or [])
    results = state.get("results") or {}

    rank_code_to_id = {
        e["rank_code"]: e["rikishi_id"] for e in banzuke["makuuchi"] + banzuke["juryo"]
    }

    fetched_new = False
    for day in range(1, day_no + 1):
        if day in days_done:
            continue
        try:
            day_bouts = _fetch_day_results(code, day)
        except Exception as exc:
            logging.warning("[sumo_hoshitori] fetch failed for day %d: %s", day, exc)
            continue
        relevant = {rc: v for rc, v in day_bouts.items() if rc in rank_code_to_id}
        if not relevant:
            # today's bouts may not be finished/entered yet - don't mark as done
            # so the next run retries; past days with genuinely no data would
            # keep retrying too, which is an acceptable cost at this frequency.
            continue
        for rank_code, (won, opponent_code) in relevant.items():
            rikishi_id = rank_code_to_id[rank_code]
            opponent_id = rank_code_to_id.get(opponent_code)
            results.setdefault(rikishi_id, {})[str(day)] = {
                "win": won,
                "opponent_rikishi_id": opponent_id,
            }
        days_done.add(day)
        fetched_new = True

    state["days_done"] = sorted(days_done)
    state["results"] = results
    state["updated_at"] = generated_at
    if fetched_new:
        hoshitori_path.parent.mkdir(parents=True, exist_ok=True)
        hoshitori_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "module": "sumo_hoshitori",
        "generated_at": generated_at,
        "status": "ok",
        "day_no": day_no,
        "days_done": sorted(days_done),
        "reason": "fetched new day(s)" if fetched_new else "no new completed days to fetch",
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[sumo_hoshitori] day_no=%d days_done=%s", day_no, sorted(days_done))


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
