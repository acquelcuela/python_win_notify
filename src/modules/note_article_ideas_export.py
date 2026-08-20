import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path


JST = timezone(timedelta(hours=9), "JST")
DEFAULT_TARGET_DIR = r"C:\Users\user\OneDrive - LIFEWORK\send@OneDrive2027"


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _load_config(root: Path) -> dict:
    config_path = root / "note_article_ideas_config.json"
    if not config_path.exists():
        return {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[note_article_ideas_export] note_article_ideas_config.json is invalid; using defaults.")
        return {}
    return config if isinstance(config, dict) else {}


def _format_ideas_text(data: dict, now: datetime) -> str:
    lines = [f"note記事 構想メモ {now.strftime('%Y-%m-%d %H:%M')} JST", ""]

    ideas = data.get("ideas") or []
    if not ideas:
        lines.append("本日は提案なしでした。")
    for index, idea in enumerate(ideas, start=1):
        lines.append(f"[{index}] {idea.get('title') or '-'}")
        lines.append(f"    切り口: {idea.get('angle') or '-'}")
        lines.append(f"    カテゴリ: {idea.get('category') or '-'}")
        lines.append(f"    参考: {idea.get('inspired_by') or '-'}")
        lines.append("")

    trend_seeds = (data.get("trend_seeds") or [])[:8]
    if trend_seeds:
        lines.append("--- 参考: 今伸びている記事(note「投資」トピック) ---")
        for seed in trend_seeds:
            like_count = seed.get("like_count")
            like_text = f"いいね{like_count:,}" if isinstance(like_count, int) else "-"
            lines.append(f"- {seed.get('title') or '-'} ({like_text})")

    return "\n".join(lines) + "\n"


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "note_article_ideas_export.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()

    data = _load_json(root / "output" / "note_article_ideas.json")
    if not data or data.get("status") != "ok":
        result = {
            "module": "note_article_ideas_export",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "note_article_ideas output is not available or not ok.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[note_article_ideas_export] skipped: note_article_ideas status is not ok")
        return

    config = _load_config(root)
    target_dir = Path(config.get("export_dir") or DEFAULT_TARGET_DIR)
    if not target_dir.exists():
        result = {
            "module": "note_article_ideas_export",
            "generated_at": generated_at,
            "status": "error",
            "reason": f"Directory not found: {target_dir}",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[note_article_ideas_export] directory not found: %s", target_dir)
        return

    text = _format_ideas_text(data, now)
    filename = f"note_article_ideas_{now.strftime('%Y-%m-%d')}.txt"
    staging_path = output_dir / filename
    staging_path.write_text(text, encoding="utf-8")

    final_path = target_dir / filename
    shutil.move(str(staging_path), str(final_path))

    result = {
        "module": "note_article_ideas_export",
        "generated_at": generated_at,
        "status": "ok",
        "path": str(final_path),
        "idea_count": len(data.get("ideas") or []),
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[note_article_ideas_export] moved %s to %s", filename, target_dir)


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
