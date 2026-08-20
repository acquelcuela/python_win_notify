from __future__ import annotations

import html
import json
import logging
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.ai_summary import _call_gemini
from modules.gemini_pricing import GeminiUsageTracker


JST = timezone(timedelta(hours=9), "JST")
NOTE_SEARCH_URL = "https://note.com/api/v3/searches"
YAHOO_RANKING_URL = "https://finance.yahoo.co.jp/stocks/ranking/bbs"
YAHOO_RANKING_PAGES = [1, 2]
DEFAULT_QUERIES = ["高配当株", "配当生活", "FIRE 配当"]
DEFAULT_DAILY_COUNT = 2
DEFAULT_CANDIDATE_COUNT = 5
DEFAULT_SIMILARITY_THRESHOLD = 0.2
DEFAULT_TREND_SEED_COUNT = 15
DEFAULT_MODEL = "gemini-3.1-flash-lite"

STOCK_SEEN_LOG_PATH = Path("state") / "note_stock_ranking_seen.json"
STOCK_SEEN_LOG_RETENTION_DAYS = 365

# Mirrors docs/note_magazine_categories_20260709.md - keeps proposed concepts
# aligned with the account's actual content buckets instead of drifting into
# unrelated finance topics.
CATEGORIES = [
    "高配当株の個別レビュー",
    "ETF・インデックス",
    "FIRE・配当生活",
    "税金・相続・制度",
    "高配当の考え方・ポートフォリオ設計",
]

IDEAS_LOG_PATH = Path("state") / "note_article_ideas_log.json"
IDEAS_LOG_RETENTION_DAYS = 180


def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


CONFIG_PATH = Path("note_article_ideas_config.json")


def _load_config(root: Path) -> dict:
    config_path = root / CONFIG_PATH
    if not config_path.exists():
        return {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[note_article_ideas] %s is invalid; using defaults.", CONFIG_PATH)
        return {}
    return config if isinstance(config, dict) else {}


def _fetch_popular_notes(query: str, size: int = 10) -> list[dict]:
    """note.com's own frontend calls this same search endpoint (reverse
    engineered, no official docs) - there's no dedicated "topic trending"
    listing endpoint that returns article data server-side (the topic page
    itself ships as an empty client-rendered shell, checked 2026-08-20), but
    this search API supports sort=popular and returns like_count directly."""
    params = urllib.parse.urlencode({"context": "note", "q": query, "size": size, "sort": "popular"})
    url = f"{NOTE_SEARCH_URL}?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))

    notes = ((payload.get("data") or {}).get("notes") or {}).get("contents") or []
    results = []
    for note in notes:
        key = note.get("key")
        user = note.get("user") or {}
        urlname = user.get("urlname")
        results.append(
            {
                "query": query,
                "title": str(note.get("name") or "").strip(),
                "like_count": int(note.get("like_count") or 0),
                "category": note.get("category"),
                "url": f"https://note.com/{urlname}/n/{key}" if urlname and key else None,
            }
        )
    return results


def _collect_trend_seeds(queries: list[str], seed_count: int) -> tuple[list[dict], list[str]]:
    all_notes = []
    warnings = []
    for query in queries:
        try:
            all_notes.extend(_fetch_popular_notes(query))
        except Exception as exc:
            warnings.append(f"{query}: {exc}")
            logging.error("[note_article_ideas] fetch failed for query '%s': %s", query, exc)

    seen_titles = set()
    deduped = []
    for note in all_notes:
        title = note["title"]
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        deduped.append(note)

    deduped.sort(key=lambda n: n["like_count"], reverse=True)
    return deduped[:seed_count], warnings


_RANKING_ROW_RE = re.compile(
    r'RankingTable__rank__2fAZ">(?P<rank>\d+)</th>.*?href="https://finance\.yahoo\.co\.jp/quote/'
    r'(?P<ticker>[0-9A-Za-z]+\.T)"[^>]*>(?P<name>[^<]+)</a>',
    re.S,
)


def _fetch_ranking_page(page: int) -> list[dict]:
    """Yahoo Finance's 掲示板投稿数ランキング (BBS post-count ranking) - the
    only ranking view checked (2026-08-20) with a stable, single-table
    markup; the general /stocks/ top page mixes several differently-marked-up
    ranking widgets and was skipped as too fragile to parse reliably."""
    params = urllib.parse.urlencode({"market": "all", "term": "daily", "page": page})
    url = f"{YAHOO_RANKING_URL}?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read().decode("utf-8", errors="replace")

    results = []
    for match in _RANKING_ROW_RE.finditer(body):
        ticker = match.group("ticker").strip()
        name = html.unescape(match.group("name")).strip()
        if ticker:
            results.append({"ticker": ticker, "name": name, "rank": int(match.group("rank"))})
    return results


def _load_stock_seen_log(root: Path) -> dict[str, str]:
    path = root / STOCK_SEEN_LOG_PATH
    if not path.exists():
        return {}
    data = _load_json(path)
    return data if isinstance(data, dict) else {}


def _save_stock_seen_log(root: Path, seen: dict[str, str]) -> None:
    path = root / STOCK_SEEN_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    cutoff_label = (datetime.now(JST) - timedelta(days=STOCK_SEEN_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
    trimmed = {ticker: date for ticker, date in seen.items() if date >= cutoff_label}
    path.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")


def _collect_new_ranking_tickers(root: Path) -> tuple[list[dict], list[str]]:
    """Returns tickers that entered the BBS post-count ranking for the first
    time since we started tracking - not just "currently ranked", which
    would be most of the same names every day. On the very first run ever
    (no seen-log file yet), today's ranking is used only to seed that log;
    nothing is reported as "new" this run, per the 2026-08-20 request to
    skip whatever's already on the board today and only react to tickers
    that show up from tomorrow onward."""
    warnings = []
    all_entries: list[dict] = []
    for page in YAHOO_RANKING_PAGES:
        try:
            all_entries.extend(_fetch_ranking_page(page))
        except Exception as exc:
            warnings.append(f"ranking page {page}: {exc}")
            logging.error("[note_article_ideas] ranking fetch failed for page %d: %s", page, exc)

    seen = _load_stock_seen_log(root)
    is_bootstrap = not seen
    today_label = datetime.now(JST).strftime("%Y-%m-%d")

    new_tickers = []
    seen_this_run = set()
    for entry in all_entries:
        ticker = entry["ticker"]
        if ticker in seen_this_run:
            continue
        seen_this_run.add(ticker)
        if not is_bootstrap and ticker not in seen:
            new_tickers.append(entry)
        seen[ticker] = today_label

    _save_stock_seen_log(root, seen)
    return new_tickers, warnings


def _own_article_titles(root: Path) -> list[str]:
    """Union of both known "already posted" sources so a proposed concept
    doesn't duplicate anything the account has already published, whichever
    file happens to have the more complete list at the time."""
    titles: list[str] = []
    for filename in ("note_articles.json", "note_articles_cache.json"):
        payload = _load_json(root / "state" / filename)
        if isinstance(payload, dict):
            entries = payload.get("notes") or []
        elif isinstance(payload, list):
            entries = payload
        else:
            entries = []
        for entry in entries:
            if isinstance(entry, dict):
                title = str(entry.get("title") or entry.get("name") or "").strip()
                if title:
                    titles.append(title)
    return titles


def _load_ideas_log(root: Path) -> list[dict]:
    path = root / IDEAS_LOG_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _save_ideas_log(root: Path, records: list[dict]) -> None:
    path = root / IDEAS_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_json(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```json\s*", "", cleaned)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, re.S)
    if match:
        payload = json.loads(match.group(0))
        if isinstance(payload, dict):
            return payload
    raise ValueError("Gemini response did not contain valid JSON.")


def _normalize_for_compare(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"[\s\W_]+", "", text)


def _char_bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _title_similarity(a: str, b: str) -> float:
    """Character-bigram Jaccard similarity - works without a tokenizer for
    Japanese, which has no whitespace between words. This is the code-level
    dedup check: the prompt already asks Gemini to avoid the same list, but
    an LLM instruction isn't a guarantee, so titles that slip through get
    caught and dropped here instead of being trusted on the model's word.

    Calibrated (2026-08-20) against this account's real title style (long,
    specific - ticker codes, distinct descriptive phrases): genuine
    near-duplicate rewordings scored 0.18-0.44, while different articles
    that happen to share this account's "A vs B" template scored ~0.07
    thanks to the differing company names/tickers carrying most of the
    text. DEFAULT_SIMILARITY_THRESHOLD=0.2 sits between those. This only
    catches literal/near-literal reuse, not a fully different wording of
    the same underlying idea - it's a coarse safety net, not a semantic
    duplicate detector."""
    set_a, set_b = _char_bigrams(_normalize_for_compare(a)), _char_bigrams(_normalize_for_compare(b))
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union else 0.0


def _is_duplicate(title: str, avoid_titles: list[str], threshold: float) -> tuple[bool, str | None]:
    for existing in avoid_titles:
        if _title_similarity(title, existing) >= threshold:
            return True, existing
    return False, None


def _build_prompt(
    trend_seeds: list[dict], new_tickers: list[dict], avoid_titles: list[str], candidate_count: int
) -> str:
    seeds_text = "\n".join(
        f"- 「{seed['title']}」(いいね{seed['like_count']}, 検索語:{seed['query']})" for seed in trend_seeds
    ) or "(取得できた参考記事なし)"
    # Avoid list can get long (hundreds of titles); Gemini only needs enough
    # to reliably avoid near-duplicates, not the full corpus verbatim.
    avoid_text = "\n".join(f"- {title}" for title in avoid_titles[:250]) or "(なし)"
    categories_text = "\n".join(f"- {c}" for c in CATEGORIES)
    ticker_text = "\n".join(
        f"- {t['name']}({t['ticker'].replace('.T', '')}) 投稿数ランキング{t['rank']}位に新規登場"
        for t in new_tickers
    )
    ticker_block = (
        f"""
Yahoo!ファイナンス掲示板の投稿数ランキングに新しく入ってきた銘柄(個別レビュー記事の題材候補、無理に使わなくてよい):
{ticker_text}
"""
        if new_tickers
        else ""
    )

    return f"""
あなたは「高配当株投資・FIRE」をテーマにしたnoteクリエイターの構想アシスタントです。
次に書くべき記事の"構想"(タイトル案・切り口・カテゴリ)を{candidate_count}件、提案してください。
これは候補案なので、後段で重複チェックにかけて絞り込みます。それぞれ別の切り口にしてください。

参考(note.com「投資」トピックで今伸びている記事。真似ではなく、切り口のヒントとして使う):
{seeds_text}
{ticker_block}
このアカウントの記事カテゴリ(いずれかに寄せる):
{categories_text}

絶対に避けること:
- 以下は既に投稿済み、または過去に提案済みのタイトル。内容が重複するテーマ・切り口は避ける
{avoid_text}

出力はJSONのみ。説明文やコードフェンスは不要です。

JSON形式:
{{
  "ideas": [
    {{
      "title": "記事タイトル案",
      "angle": "切り口・フックを一文で",
      "category": "上記カテゴリのいずれか",
      "inspired_by": "参考にした伸びている記事のタイトル、または「独自」"
    }}
  ]
}}
""".strip()


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "note_article_ideas.json"
    generated_at = datetime.now(JST).isoformat()

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        payload = {
            "module": "note_article_ideas",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "GEMINI_API_KEY is not set.",
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[note_article_ideas] skipped: GEMINI_API_KEY is not set")
        return

    config = _load_config(root)
    queries = [str(q).strip() for q in (config.get("queries") or DEFAULT_QUERIES) if str(q).strip()]
    daily_count = int(config.get("daily_count", DEFAULT_DAILY_COUNT))
    candidate_count = int(config.get("candidate_count", DEFAULT_CANDIDATE_COUNT))
    similarity_threshold = float(config.get("similarity_threshold", DEFAULT_SIMILARITY_THRESHOLD))
    seed_count = int(config.get("trend_seed_count", DEFAULT_TREND_SEED_COUNT))
    model = str(config.get("model") or DEFAULT_MODEL)

    trend_seeds, fetch_warnings = _collect_trend_seeds(queries, seed_count)
    new_tickers, ranking_warnings = _collect_new_ranking_tickers(root)
    fetch_warnings += ranking_warnings

    own_titles = _own_article_titles(root)
    ideas_log = _load_ideas_log(root)
    proposed_titles = [r.get("title", "") for r in ideas_log]
    avoid_titles = own_titles + proposed_titles

    prompt = _build_prompt(trend_seeds, new_tickers, avoid_titles, candidate_count)
    usage_tracker = GeminiUsageTracker(model)

    try:
        text, usage = _call_gemini(api_key=api_key, model=model, prompt=prompt)
        usage_tracker.add(usage)
        data = _extract_json(text)
        candidates = data.get("ideas") or []
        candidates = [
            {
                "title": str(idea.get("title") or "").strip(),
                "angle": str(idea.get("angle") or "").strip(),
                "category": str(idea.get("category") or "").strip(),
                "inspired_by": str(idea.get("inspired_by") or "").strip(),
            }
            for idea in candidates
            if str(idea.get("title") or "").strip()
        ]
    except Exception as exc:
        payload = {
            "module": "note_article_ideas",
            "generated_at": generated_at,
            "status": "error",
            "error": str(exc),
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[note_article_ideas] Gemini call failed: %s", exc)
        return

    # Code-level dedup pass: the prompt already asked Gemini to avoid
    # avoid_titles, but that's a request, not a guarantee - each candidate
    # (and each idea already kept this run, since Gemini can also propose
    # near-duplicates of each other) gets checked against the growing avoid
    # list before being kept. Whatever survives is the day's result, which
    # may land anywhere from 0 up to daily_count - it isn't padded back up.
    ideas = []
    dropped = []
    rolling_avoid = list(avoid_titles)
    for candidate in candidates:
        if len(ideas) >= daily_count:
            break
        is_dup, matched = _is_duplicate(candidate["title"], rolling_avoid, similarity_threshold)
        if is_dup:
            dropped.append({**candidate, "duplicate_of": matched})
            continue
        ideas.append(candidate)
        rolling_avoid.append(candidate["title"])

    today_label = datetime.now(JST).strftime("%Y-%m-%d")
    for idea in ideas:
        ideas_log.append({"logged_date": today_label, "title": idea["title"]})
    cutoff_label = (datetime.now(JST) - timedelta(days=IDEAS_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
    ideas_log = [r for r in ideas_log if r.get("logged_date", "9999-99-99") >= cutoff_label]
    _save_ideas_log(root, ideas_log)

    payload = {
        "module": "note_article_ideas",
        "generated_at": generated_at,
        "status": "ok",
        "model": model,
        "cost_jpy": round(usage_tracker.cost_jpy, 2),
        "trend_seeds": trend_seeds,
        "new_ranking_tickers": new_tickers,
        "candidate_count": len(candidates),
        "dropped_as_duplicate": dropped,
        "ideas": ideas,
    }
    if fetch_warnings:
        payload["warnings"] = fetch_warnings
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info(
        "[note_article_ideas] %d candidate(s) -> %d kept, %d dropped as duplicate (from %d trend seed(s))",
        len(candidates), len(ideas), len(dropped), len(trend_seeds),
    )


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
