from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.gemini_pricing import USD_TO_JPY
from modules.news_movers import (
    _alias_file_path,
    _data_file_path,
    _load_aliases,
    _load_listed_companies,
)


JST = timezone(timedelta(hours=9), "JST")
DEFAULT_MODEL = "grok-4.3"
GROK_API_URL = "https://api.x.ai/v1/responses"
MAX_COMMON_KEYWORDS = 10
MAX_FINDINGS = 8
HISTORY_DIR_NAME = "history"
HISTORY_RETENTION_DAYS = 30
# Per-run files (output/history/stock_x_trends_runs/) are kept indefinitely.
RUNS_DIR_NAME = "stock_x_trends_runs"

# Written by stock_x_trends_web_fetch.py when stock_x_trends.source ==
# "web" - that module drives an actual browser session (slow, so it runs
# on its own earlier schedule slot) and caches its result here for this
# module to just read.
WEB_CACHE_PATH = Path("state") / "stock_x_trends_web_cache.json"
# The fetch slot runs 30 min before each stock_x_trends slot, so anything
# older than this is a previous cycle's leftover, not this cycle's fetch.
DEFAULT_WEB_CACHE_MAX_AGE_MINUTES = 120

# xAI's own authoritative per-call cost, straight from the API response
# (usage.cost_in_usd_ticks / COST_TICKS_PER_USD) - covers token pricing and
# the x_search tool's per-post/per-profile fees together, so there's no need
# to reimplement xAI's pricing table the way gemini_pricing.py does for
# Gemini. Verified against a live call: 1559 input tokens (128 cached),
# 201 output tokens, 0 sources -> cost_in_usd_ticks=23168500, matching
# (1431*1.25 + 128*0.20 + 201*2.50)/1e6 = $0.00231685 at ticks/1e10.
COST_TICKS_PER_USD = 1e10


class GrokUsageTracker:
    """Accumulates cost across one or more Grok calls within a single
    module run, so the caller can report a total (mirrors
    gemini_pricing.GeminiUsageTracker's role for Gemini calls)."""

    def __init__(self) -> None:
        self.call_count = 0
        self.cost_usd = 0.0
        self.num_sources_used = 0

    def add(self, usage: dict | None) -> None:
        if not usage:
            return
        self.call_count += 1
        self.cost_usd += float(usage.get("cost_in_usd_ticks") or 0) / COST_TICKS_PER_USD
        self.num_sources_used += int(usage.get("num_sources_used") or 0)

    @property
    def cost_jpy(self) -> float:
        return self.cost_usd * USD_TO_JPY


def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[stock_x_trends] invalid JSON ignored: %s", path)
        return None


def _load_config(root: Path) -> dict:
    payload = _load_json(root / "config.json")
    return payload if isinstance(payload, dict) else {}


def _module_config(root: Path) -> dict:
    config = _load_config(root)
    payload = config.get("stock_x_trends", {})
    return payload if isinstance(payload, dict) else {}


def _market_context(root: Path) -> str:
    market_news = _load_json(root / "output" / "market_news.json") or {}
    nikkei = _load_json(root / "output" / "stock_nikkei.json") or {}
    parts: list[str] = []

    if isinstance(nikkei, dict) and nikkei.get("data"):
        data = nikkei["data"]
        indices = data.get("indices") or {"nikkei_futures": data}
        items = []
        for key in ("nikkei_average", "topix", "nikkei_futures"):
            item = indices.get(key)
            if isinstance(item, dict) and item.get("label"):
                change = item.get("change")
                change_pct = item.get("change_pct")
                change_text = "-"
                if change is not None:
                    sign = "+" if change >= 0 else ""
                    change_text = f"{sign}{float(change):,.2f} ({sign}{float(change_pct):.2f}%)"
                items.append(f"{item['label']}: {item.get('current', '-')}, {change_text}")
        if items:
            parts.append("市場データ: " + " / ".join(items))

    if isinstance(market_news, dict) and market_news.get("data"):
        titles = [str(item.get("title") or "").strip() for item in market_news["data"][:10]]
        titles = [title for title in titles if title]
        if titles:
            parts.append("ニュース見出し: " + " / ".join(titles[:8]))

    return "\n".join(parts)


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
    raise ValueError("Grok response did not contain valid JSON.")


def _build_prompt(focus: str, search_terms: list[str], context: str) -> str:
    search_line = " / ".join(search_terms)
    return f"""
日本株に関するX上の投稿を調査してください。
出力は JSON のみです。説明文やコードフェンスは不要です。
朝の寄り付き前から前場中の投稿を優先してください。

調査方針:
- 日本株に関係する投稿だけを対象にする
- 似た表現はまとめる
- 一般的な相場ワードは common_keywords に入れる
- 具体的な銘柄、材料、イベント、需給の変化、決算、レーティング、テーマ変化は discovery_findings に入れる
- discovery_findings は銘柄名・銘柄コード・理由を優先する
- sentiment は strong_positive / positive / neutral / negative のいずれか
- common_keywords は 5〜10件
- discovery_findings は 5〜8件

今回の重点:
{focus}

参考にする検索語:
{search_line}

JSON形式:
{{
  "common_keywords": ["...", "..."],
  "discovery_findings": [
    {{
      "ticker": "銘柄コードまたは空文字",
      "name": "銘柄名またはテーマ名",
      "reason": "なぜ注目されているかを一文で",
      "sentiment": "strong_positive | positive | neutral | negative",
      "source": "X上の投稿要約または見出し",
      "detail": "補足があれば短く"
    }}
  ]
}}

補足メモ:
{context}
""".strip()


def _call_grok(api_key: str, model: str, prompt: str, max_tokens: int) -> tuple[dict, dict]:
    body = {
        "model": model,
        "input": [
            {"role": "system", "content": "Return only valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "max_output_tokens": max_tokens,
        "tools": [{"type": "x_search"}],
    }
    request = urllib.request.Request(
        GROK_API_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Grok API HTTP {exc.code}: {detail}") from exc

    output = payload.get("output") or []
    text = payload.get("output_text") or ""
    if not text and output:
        for item in output:
            for part in item.get("content", []) or []:
                text += str(part.get("text") or part.get("output_text") or "")
    if not text:
        raise RuntimeError("Grok API returned empty content.")
    return _extract_json(text), (payload.get("usage") or {})


def _normalize_payload(data: dict) -> dict:
    keywords: list[str] = []
    source_keywords = data.get("common_keywords") or data.get("trending_keywords") or []
    for item in source_keywords:
        value = str(item).strip()
        if value and value not in keywords:
            keywords.append(value)
    keywords = keywords[:MAX_COMMON_KEYWORDS]

    stock_findings: list[dict] = []
    theme_findings: list[dict] = []
    source_findings = data.get("discovery_findings") or data.get("notable_posts") or []
    for item in source_findings:
        if not isinstance(item, dict):
            continue
        finding = {
            "ticker": str(item.get("ticker") or "").strip(),
            "name": str(item.get("name") or "").strip(),
            "reason": str(item.get("reason") or "").strip(),
            "sentiment": str(item.get("sentiment") or "neutral").strip(),
            "source": str(item.get("source") or "").strip(),
            "detail": str(item.get("detail") or "").strip(),
        }
        if finding["ticker"]:
            stock_findings.append(finding)
        elif finding["name"] or finding["reason"]:
            theme_findings.append(finding)

    return {
        "common_keywords": keywords,
        "stock_findings": stock_findings[:MAX_FINDINGS],
        "theme_findings": theme_findings[:MAX_FINDINGS],
        "discovery_findings": theme_findings[:MAX_FINDINGS],
        "trending_keywords": keywords,
        "notable_posts": theme_findings[:5],
    }


def _merge_payload(base: dict, extra: dict, cap: bool = True) -> dict:
    """Merges two payloads, base first, dropping duplicate keywords and
    duplicate (ticker, name) findings. `cap` trims each list to the per-run
    limits - right for combining one run's search passes, but the 07:00
    overnight+morning merge passes cap=False: with the cap, a full 23:00
    list left no room and every new morning finding was silently dropped."""
    merged_keywords: list[str] = []
    for source in (
        base.get("common_keywords") or [],
        extra.get("common_keywords") or [],
    ):
        for value in source:
            if value and value not in merged_keywords:
                merged_keywords.append(value)

    def _merge_items(sources: list[list[dict]]) -> list[dict]:
        merged_items: list[dict] = []
        seen_keys: set[tuple[str, str]] = set()
        for source in sources:
            for item in source:
                key = (str(item.get("ticker") or "").strip(), str(item.get("name") or "").strip())
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                merged_items.append(item)
        return merged_items

    merged_stock_findings = _merge_items(
        [base.get("stock_findings") or [], extra.get("stock_findings") or []]
    )
    merged_theme_findings = _merge_items(
        [base.get("theme_findings") or [], extra.get("theme_findings") or []]
    )

    if cap:
        merged_keywords = merged_keywords[:MAX_COMMON_KEYWORDS]
        merged_stock_findings = merged_stock_findings[:MAX_FINDINGS]
        merged_theme_findings = merged_theme_findings[:MAX_FINDINGS]

    return {
        "common_keywords": merged_keywords,
        "stock_findings": merged_stock_findings,
        "theme_findings": merged_theme_findings,
        "discovery_findings": merged_theme_findings,
        "trending_keywords": merged_keywords,
        "notable_posts": merged_theme_findings[:5],
    }


def _needs_more_passes(payload: dict) -> bool:
    stock_findings = payload.get("stock_findings") or []
    theme_findings = payload.get("theme_findings") or []
    findings = stock_findings + theme_findings
    if len(findings) < 4:
        return True
    # The broad pass alone tends to return plenty of individual-ticker hits
    # but almost never any theme/discovery findings, since it isn't aimed at
    # that signal. Require some theme coverage too so the momentum/catalyst
    # passes (which target different signal types) actually get a chance to
    # run instead of being skipped every time the broad pass looks "enough".
    if len(theme_findings) < 2:
        return True
    specific_count = 0
    for item in findings:
        ticker = str(item.get("ticker") or "").strip()
        name = str(item.get("name") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if (ticker or name) and len(reason) >= 12:
            specific_count += 1
    return specific_count < 3


def _search_passes() -> list[tuple[str, list[str], str]]:
    return [
        (
            "broad",
            ["急騰", "爆上げ", "仕掛け", "上がりそう"],
            "今日のXで急騰や仕掛けとして話題になっている日本株を、朝の寄り付き前から前場中を優先して広く拾う。",
        ),
        (
            "momentum",
            ["材料出た", "IR", "決算", "上方修正"],
            "今日のXで材料、IR、決算、上方修正をきっかけに話題になっている日本株を、銘柄名・材料の内容・盛り上がり度合いで拾う。",
        ),
        (
            "catalyst",
            ["出来高急増", "板が厚い", "仕込み時", "次の主役"],
            "今日のXで出来高急増、板の厚さ、仕込み時、次の主役として言及されている低位株・小型株を、短期トレーダーの投稿優先で拾う。",
        ),
    ]


def _run_grok_searches(
    api_key: str, model: str, max_tokens: int, context: str, usage_tracker: GrokUsageTracker
) -> tuple[dict, list[dict]]:
    passes_used: list[dict] = []
    merged: dict | None = None

    for index, (name, search_terms, focus) in enumerate(_search_passes(), start=1):
        prompt = _build_prompt(focus, search_terms, context)
        raw, usage = _call_grok(api_key, model, prompt, max_tokens)
        usage_tracker.add(usage)
        data = _normalize_payload(raw)
        passes_used.append(
            {
                "name": name,
                "search_terms": search_terms,
                "common_keywords": len(data["common_keywords"]),
                "discovery_findings": len(data["discovery_findings"]),
            }
        )

        if merged is None:
            merged = data
            if not _needs_more_passes(merged):
                break
            continue

        merged = _merge_payload(merged, data)
        if not _needs_more_passes(merged):
            break

        # Run only the next pass when the current result is still weak.
        if index >= 2 and not _needs_more_passes(merged):
            break

    if merged is None:
        raise RuntimeError("Grok search returned no payload.")
    return merged, passes_used


def _build_name_lookup(root: Path, config: dict) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Returns (code -> official name, code -> alias list) so Grok findings
    can be cross-checked against the same reference data news_movers uses.
    A finding whose ticker/name doesn't resolve here either means data_j.csv
    is stale (the ticker was delisted/renamed) or Grok hallucinated it - both
    are worth surfacing instead of silently trusting the finding."""
    news_movers_config = config.get("news_movers", {})
    data_path = _data_file_path(root, config)
    alias_path = _alias_file_path(root, config)
    try:
        companies = _load_listed_companies(root, data_path)
    except FileNotFoundError:
        return {}, {}
    names_by_code = {company["code"]: company["name"] for company in companies}
    aliases_by_ticker = _load_aliases(alias_path)
    return names_by_code, aliases_by_ticker


def _normalize_name_for_match(text: str) -> str:
    """Collapses full-width/half-width and case differences before name
    comparison - e.g. official "ＩＨＩ" vs a finding's "IHI", or "ソフト９９"
    vs "ソフト99", are the same name and shouldn't need a one-off alias
    entry each time this width/case variant shows up (2026-08-25: this
    pattern accounted for 2 of 5 "unresolved" findings in a single week)."""
    return unicodedata.normalize("NFKC", text).strip().lower()


def _verify_findings(root: Path, data: dict) -> dict:
    names_by_code, aliases_by_ticker = _build_name_lookup(root, _load_config(root))
    if not names_by_code:
        return data

    unresolved: list[str] = []
    for finding in data.get("stock_findings") or []:
        code = str(finding.get("ticker") or "").strip()
        name = str(finding.get("name") or "").strip()
        if not code:
            continue
        official_name = names_by_code.get(code)
        aliases = aliases_by_ticker.get(f"{code}.T", [])
        known_names = {official_name} | set(aliases) if official_name else set(aliases)
        normalized_name = _normalize_name_for_match(name) if name else ""
        normalized_known = {_normalize_name_for_match(n) for n in known_names if n}
        normalized_official = _normalize_name_for_match(official_name) if official_name else ""
        resolved = bool(official_name) and (
            not name
            or normalized_name in normalized_known
            or normalized_official in normalized_name
            or normalized_name in normalized_official
        )
        finding["verified"] = resolved
        if not resolved:
            unresolved.append(f"{code}({name or '-'})")

    if unresolved:
        logging.warning(
            "[stock_x_trends] %d finding(s) did not match data_j.csv/aliases - check for stale listing data or a missing alias: %s",
            len(unresolved),
            ", ".join(unresolved),
        )
    return data


def _archive_history(root: Path, payload: dict) -> None:
    """Keep a dated copy of each run's output so past search content isn't
    lost the moment the next run overwrites output/stock_x_trends.json -
    otherwise the only record of a given day's findings was the report email."""
    history_dir = root / "output" / HISTORY_DIR_NAME
    history_dir.mkdir(parents=True, exist_ok=True)
    generated_at = payload.get("generated_at") or datetime.now(JST).isoformat()
    try:
        date_label = datetime.fromisoformat(str(generated_at)).astimezone(JST).strftime("%Y%m%d")
    except (TypeError, ValueError):
        date_label = datetime.now(JST).strftime("%Y%m%d")
    history_path = history_dir / f"stock_x_trends_{date_label}.json"
    history_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    cutoff = datetime.now(JST) - timedelta(days=HISTORY_RETENTION_DAYS)
    for existing in history_dir.glob("stock_x_trends_*.json"):
        try:
            file_date = datetime.strptime(existing.stem.split("_")[-1], "%Y%m%d").replace(tzinfo=JST)
        except ValueError:
            continue
        if file_date < cutoff:
            existing.unlink(missing_ok=True)


def _web_cache_skip_reason(cache_payload, max_age_minutes: int | None) -> str | None:
    if not isinstance(cache_payload, dict) or not cache_payload.get("data"):
        return f"no cached web data at {WEB_CACHE_PATH} - run stock_x_trends_web_fetch first."
    if max_age_minutes is None:
        return None
    # A failed stock_x_trends_web_fetch run (Chrome closed, Grok logged out or
    # rate-limited, ...) leaves the previous cache in place - without this the
    # report would silently present that older cycle's X trends as current.
    try:
        cached_at = datetime.fromisoformat(str(cache_payload.get("generated_at")))
    except ValueError:
        return f"web cache at {WEB_CACHE_PATH} has no valid generated_at."
    age_minutes = (datetime.now(JST) - cached_at).total_seconds() / 60
    if age_minutes > max_age_minutes:
        return (
            f"web cache is stale ({age_minutes:.0f} min old, limit {max_age_minutes} min; "
            f"generated at {cache_payload.get('generated_at')}) - stock_x_trends_web_fetch likely failed."
        )
    return None


def _from_web_cache(root: Path, generated_at: str, max_age_minutes: int | None) -> dict:
    """stock_x_trends.source == "web": this run's own payload, read from the
    cache stock_x_trends_web_fetch.py left behind instead of calling the Grok
    API - no API key or cost here, but this only has data once that separate,
    slower module has run, and a cache older than max_age_minutes is refused
    rather than reused."""
    cache_payload = _load_json(root / WEB_CACHE_PATH)
    skip_reason = _web_cache_skip_reason(cache_payload, max_age_minutes)
    if skip_reason:
        logging.warning("[stock_x_trends] no data this run (source=web): %s", skip_reason)
        return {
            "module": "stock_x_trends",
            "generated_at": generated_at,
            "status": "skipped",
            "source": "web",
            "reason": skip_reason,
            "data": None,
        }

    data = _verify_findings(root, cache_payload["data"])
    logging.info(
        "[stock_x_trends] (web) collected %s common keywords, %s stock findings and %s theme findings "
        "from cache generated at %s",
        len(data["common_keywords"]),
        len(data["stock_findings"]),
        len(data["theme_findings"]),
        cache_payload.get("generated_at"),
    )
    return {
        "module": "stock_x_trends",
        "generated_at": generated_at,
        "status": "ok",
        "source": "web",
        "web_cache_generated_at": cache_payload.get("generated_at"),
        "cost_jpy": 0.0,
        "data": data,
    }


def _from_api(root: Path, generated_at: str, api_key: str, model: str, max_tokens: int) -> dict:
    """stock_x_trends.source == "api": this run's own payload from the Grok API."""
    context = _market_context(root)
    usage_tracker = GrokUsageTracker()
    try:
        data, passes_used = _run_grok_searches(api_key, model, max_tokens, context, usage_tracker)
        data = _verify_findings(root, data)
    except Exception as exc:
        logging.error("[stock_x_trends] failed: %s", exc)
        return {
            "module": "stock_x_trends",
            "generated_at": generated_at,
            "status": "error",
            "source": "api",
            "model": model,
            "cost_jpy": round(usage_tracker.cost_jpy, 3),
            "error": str(exc),
            "data": None,
        }
    logging.info(
        "[stock_x_trends] collected %s common keywords, %s stock findings and %s theme findings using %s pass(es) "
        "(cost=%.3f JPY, %s X source(s) used)",
        len(data["common_keywords"]),
        len(data["stock_findings"]),
        len(data["theme_findings"]),
        len(passes_used),
        usage_tracker.cost_jpy,
        usage_tracker.num_sources_used,
    )
    return {
        "module": "stock_x_trends",
        "generated_at": generated_at,
        "status": "ok",
        "source": "api",
        "model": model,
        "search_passes": passes_used,
        "cost_jpy": round(usage_tracker.cost_jpy, 3),
        "num_sources_used": usage_tracker.num_sources_used,
        "data": data,
    }


def _runs_dir(root: Path) -> Path:
    return root / "output" / HISTORY_DIR_NAME / RUNS_DIR_NAME


def _save_run(root: Path, payload: dict, schedule_key: str) -> Path:
    """Every run's own (un-merged) result gets its own file, failures
    included - nothing overwrites it, so the 07:00 merge can always find the
    previous night's list, and past cycles stay inspectable."""
    runs_dir = _runs_dir(root)
    runs_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.fromisoformat(payload["generated_at"]).astimezone(JST)
    slot_label = schedule_key.replace(":", "") if schedule_key else f"{started:%H%M%S}_manual"
    path = runs_dir / f"stock_x_trends_{started:%Y%m%d}_{slot_label}.json"
    path.write_text(
        json.dumps({**payload, "schedule_key": schedule_key or None}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _overnight_base(root: Path, current_run: Path) -> tuple[Path, dict] | None:
    """For a 07:00 run: the latest successful 23:00 run since the previous
    07:00 run - so Monday morning still picks up Friday night, but a failed
    23:00 never falls back to an older cycle's list."""
    for path in sorted(_runs_dir(root).glob("stock_x_trends_*.json"), reverse=True):
        if path.name >= current_run.name:
            continue
        run_payload = _load_json(path)
        if not isinstance(run_payload, dict):
            continue
        schedule_key = run_payload.get("schedule_key")
        if schedule_key == "07:00":
            return None
        if schedule_key == "23:00" and run_payload.get("status") == "ok" and run_payload.get("data"):
            return path, run_payload
    return None


def _combine_with_overnight(root: Path, own: dict, run_path: Path) -> dict:
    """Twice-daily cycle: 23:00 starts a fresh list (the overnight picks),
    07:00 adds the morning's findings to that same night's list with
    duplicates removed and no cap, so the 09:30/12:15 reports - which don't
    re-fetch - keep showing the combined set until 23:00 resets it. If the
    morning fetch failed, the night's list is still published on its own."""
    base = _overnight_base(root, run_path)
    if base is None:
        return own
    base_path, base_payload = base
    if own.get("status") == "ok":
        data = _merge_payload(base_payload["data"], own["data"], cap=False)
        extra = {}
    else:
        data = base_payload["data"]
        extra = {
            "morning_status": own.get("status"),
            "morning_reason": own.get("reason") or own.get("error"),
        }
        logging.warning(
            "[stock_x_trends] morning fetch %s - publishing last night's list only (%s)",
            own.get("status"),
            base_path.name,
        )
    return {
        **own,
        "status": "ok",
        "merged_with_previous": True,
        "previous_run": base_path.name,
        **extra,
        "data": data,
    }


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "stock_x_trends.json"
    generated_at = datetime.now(JST).isoformat()

    config = _module_config(root)
    enabled = bool(config.get("enabled", False))
    model = str(config.get("model") or DEFAULT_MODEL)
    max_tokens = int(config.get("max_tokens", 1000))
    source = str(config.get("source") or "api").strip().lower()
    api_key = os.getenv("GROK_API_KEY", "").strip()

    if not enabled:
        payload = {
            "module": "stock_x_trends",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "stock_x_trends is disabled in config.json.",
            "data": None,
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[stock_x_trends] skipped: disabled in config.json")
        return

    if source == "web":
        max_age = config.get("web_cache_max_age_minutes", DEFAULT_WEB_CACHE_MAX_AGE_MINUTES)
        own = _from_web_cache(root, generated_at, int(max_age) if max_age is not None else None)
    elif not api_key:
        logging.info("[stock_x_trends] no data this run: GROK_API_KEY is not set")
        own = {
            "module": "stock_x_trends",
            "generated_at": generated_at,
            "status": "skipped",
            "source": "api",
            "reason": "GROK_API_KEY is not set.",
            "data": None,
        }
    else:
        own = _from_api(root, generated_at, api_key, model, max_tokens)

    schedule_key = os.getenv("BATCH_SCHEDULE_KEY", "").strip()
    run_path = _save_run(root, own, schedule_key)
    payload = _combine_with_overnight(root, own, run_path) if schedule_key == "07:00" else own
    if payload.get("merged_with_previous"):
        logging.info(
            "[stock_x_trends] combined with %s: %s stock findings, %s theme findings",
            payload["previous_run"],
            len(payload["data"]["stock_findings"]),
            len(payload["data"]["theme_findings"]),
        )

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if payload.get("status") == "ok":
        _archive_history(root, payload)


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
