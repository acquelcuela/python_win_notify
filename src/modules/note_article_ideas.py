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

from modules.llm_client import can_run, generate, send_failure_mail
from modules.gemini_pricing import GeminiUsageTracker
from modules import note_idea_sources as idea_sources


JST = timezone(timedelta(hours=9), "JST")
NOTE_SEARCH_URL = "https://note.com/api/v3/searches"
YAHOO_RANKING_URL = "https://finance.yahoo.co.jp/stocks/ranking/bbs"
YAHOO_RANKING_PAGES = [1, 2]
DEFAULT_QUERIES = ["高配当株", "配当生活", "FIRE 配当"]
DEFAULT_DAILY_COUNT = 2
DEFAULT_CANDIDATE_COUNT = 5
DEFAULT_SIMILARITY_THRESHOLD = 0.35
DEFAULT_TREND_SEED_COUNT = 15
DEFAULT_MODEL = "gemini-3.1-flash-lite"
DEFAULT_SEED_MAX_AGE_DAYS = 21
DEFAULT_FRESH_COUNT = 15
DEFAULT_THEME_COOLDOWN_DAYS = 30
DEFAULT_MAX_TITLE_CHARS = 30
AVOID_TITLES_LIMIT = 400

ARTICLE_TYPES = ["設計図", "本当か", "比較", "個別レビュー", "まとめ"]

# Price tiers per note/rule_posting.md section 5 (user decision 2026-11-10,
# replacing the 10-23 price-by-type rule): the price depends on whether the
# paid part holds something the reader can't get elsewhere. Every sale so
# far came from articles whose paid part had the computed answer, so each
# day's set includes at least one 500円 idea when the model offers one.
PRICE_TIERS = {
    "有料・高め": 500,
    "有料・安め": 300,
    "無料": 0,
}
TOP_TIER = "有料・高め"

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


def _fetch_notes(query: str, sort: str = "popular", size: int = 10) -> list[dict]:
    """note.com's own frontend calls this same search endpoint (reverse
    engineered, no official docs) - there's no dedicated "topic trending"
    listing endpoint that returns article data server-side (the topic page
    itself ships as an empty client-rendered shell, checked 2026-08-20), but
    this search API supports sort=popular / sort=new and returns like_count
    and publish_at directly (checked 2026-11-10)."""
    params = urllib.parse.urlencode({"context": "note", "q": query, "size": size, "sort": sort})
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
                "publish_at": str(note.get("publish_at") or ""),
                "url": f"https://note.com/{urlname}/n/{key}" if urlname and key else None,
            }
        )
    return results


def _dedupe_by_title(notes: list[dict]) -> list[dict]:
    seen_titles = set()
    deduped = []
    for note in notes:
        title = note["title"]
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        deduped.append(note)
    return deduped


def _collect_trend_seeds(
    queries: list[str], seed_count: int, max_age_days: int, fresh_count: int
) -> tuple[list[dict], list[dict], list[str]]:
    """Two views of what other creators are writing:
    - seeds: sort=popular, but only articles published in the last
      max_age_days. Without the age cut the same all-time hits ("女性4人で
      FIRE..." etc.) headlined every day for weeks in October.
    - fresh: sort=new, i.e. what was posted in the last few hours. These
      have almost no likes yet, but they show the topic of the day (e.g. a
      sharp drop in JT on 2026-11-10) that the popular list lags behind."""
    popular: list[dict] = []
    fresh: list[dict] = []
    warnings = []
    for query in queries:
        for sort, bucket in (("popular", popular), ("new", fresh)):
            try:
                bucket.extend(_fetch_notes(query, sort=sort))
            except Exception as exc:
                warnings.append(f"{query} ({sort}): {exc}")
                logging.error("[note_article_ideas] fetch failed for query '%s' (%s): %s", query, sort, exc)

    cutoff = datetime.now(JST) - timedelta(days=max_age_days)

    def _is_recent(note: dict) -> bool:
        try:
            return datetime.fromisoformat(note["publish_at"]) >= cutoff
        except ValueError:
            # Unknown date: keep it rather than silently losing the seed.
            return True

    seeds = [n for n in _dedupe_by_title(popular) if _is_recent(n)]
    seeds.sort(key=lambda n: n["like_count"], reverse=True)

    fresh = _dedupe_by_title(fresh)
    fresh.sort(key=lambda n: n["publish_at"], reverse=True)
    return seeds[:seed_count], fresh[:fresh_count], warnings


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


def _own_articles(root: Path) -> list[dict]:
    """Union of both known "already posted" sources so a proposed concept
    doesn't duplicate anything the account has already published, whichever
    file happens to have the more complete list at the time. Deduped by
    title (the two files overlap heavily - 208 rows for 147 articles on
    2026-11-10), keeping the highest like count seen."""
    by_title: dict[str, dict] = {}
    for filename in ("note_articles.json", "note_articles_cache.json"):
        payload = _load_json(root / "state" / filename)
        if isinstance(payload, dict):
            entries = payload.get("notes") or []
        elif isinstance(payload, list):
            entries = payload
        else:
            entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or entry.get("name") or "").strip()
            if not title:
                continue
            likes = int(entry.get("likeCount") or entry.get("like_count") or 0)
            current = by_title.get(title)
            if current is None or likes > current["like_count"]:
                by_title[title] = {"title": title, "like_count": likes}
    return list(by_title.values())


_ARCHIVE_NAME_RE = re.compile(r"^(?P<prefix>.*?)[、,]?\s*note_article_ideas_(?P<date>\d{4}-\d{2}-\d{2})\.txt$")
_ARCHIVE_IDEA_RE = re.compile(r"^\[(?P<num>\d+)\]\s*(?P<title>.+?)\s*$")


_ADOPT_WORDS_RE = re.compile(r"採用|さいよう|saiyou", re.I)
_REJECT_WORDS_RE = re.compile(r"不採用|いまいち|削除|没|ボツ")
_SOURCE_HEADING = "## 元ネタ"
# "n_inbox 2026-11-09 ②", "inbox 2026-10-08 ②と 2026-10-10 ①を統合",
# and in article_stock_master.md's table, year-less "10-08 ② + 10-10 ①".
_SOURCE_REF_RE = re.compile(r"(?:(\d{4})-)?(\d{2})-(\d{2})\s*([①-⑨])")
_STOCK_MASTER_ROW_RE = re.compile(r"^\|\s*[A-Z]+\d+\s*\|[^|]*\|(?P<source>[^|]*)\|")


def _source_refs_in(text: str) -> set[tuple[str, int]]:
    refs = set()
    today = datetime.now(JST).date()
    for year, month, day, circled in _SOURCE_REF_RE.findall(text):
        if not year:
            # Year-less dates are recent memos: this year, or last year if
            # the month is still ahead of us.
            year = str(today.year - 1 if int(month) > today.month else today.year)
        refs.add((f"{year}-{month}-{day}", ord(circled) - ord("①") + 1))
    return refs


def _read_memo_ideas(path: Path) -> dict[int, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    ideas = {}
    for line in lines:
        idea = _ARCHIVE_IDEA_RE.match(line)
        if idea:
            ideas[int(idea.group("num"))] = idea.group("title")
    return ideas


def _brief_source_refs(note_dir: Path) -> set[tuple[str, int]]:
    """(date, idea number) pairs recording which memo idea an article came
    from, written by hand since 2026-10: the "## 元ネタ" section of any
    brief or draft below note/ (e.g. "n_inbox 2026-10-24 ②"), and the
    元ネタ column of the article table in note/article_stock_master.md
    (e.g. "| 10-21 ① |")."""
    refs: set[tuple[str, int]] = set()
    if not note_dir.is_dir():
        return refs
    for path in note_dir.rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _SOURCE_HEADING in text:
            section = text.split(_SOURCE_HEADING, 1)[1].split("\n## ", 1)[0]
            refs |= _source_refs_in(section)
        if path.name == "article_stock_master.md":
            for line in text.splitlines():
                row = _STOCK_MASTER_ROW_RE.match(line)
                if row:
                    refs |= _source_refs_in(row.group("source"))
    return refs


def _load_adoption_history(memo_dirs: list[Path], note_dir: Path) -> tuple[list[dict], list[dict]]:
    """Two signals, both already part of the user's routine (no extra step):
    1. The memo's file name when it's filed away, e.g. "2を採用、...",
       "123saiyoude,...", "1wo saiyou, ...", "いまいち_...". Numbers before
       the adopt word are the picked ideas (single digits, since a memo has
       at most a handful); the rest of that memo counts as passed over. A
       reject word marks the whole memo as passed over.
    2. "## 元ネタ" references in briefs/drafts, which also catch ideas used
       from a memo whose file name carries no decision."""
    memos: dict[str, dict[int, str]] = {}
    adopted_keys: set[tuple[str, int]] = set()
    rejected_keys: set[tuple[str, int]] = set()
    for memo_dir in memo_dirs:
        if not memo_dir.is_dir():
            continue
        for path in sorted(memo_dir.glob("*note_article_ideas_*.txt")):
            match = _ARCHIVE_NAME_RE.match(path.name)
            if not match:
                continue
            date, prefix = match.group("date"), match.group("prefix")
            ideas = _read_memo_ideas(path)
            memos.setdefault(date, {}).update(ideas)
            if _REJECT_WORDS_RE.search(prefix):
                rejected_keys.update((date, n) for n in ideas)
            elif _ADOPT_WORDS_RE.search(prefix):
                head = _ADOPT_WORDS_RE.split(prefix, 1)[0]
                picked = {int(d) for d in re.findall(r"[1-9]", head)}
                for n in ideas:
                    (adopted_keys if n in picked else rejected_keys).add((date, n))

    adopted_keys |= _brief_source_refs(note_dir)
    rejected_keys -= adopted_keys

    def _records(keys: set[tuple[str, int]]) -> list[dict]:
        records = [
            {"date": date, "title": memos[date][n]}
            for date, n in keys
            if date in memos and n in memos[date]
        ]
        records.sort(key=lambda r: r["date"], reverse=True)
        return records

    return _records(adopted_keys), _records(rejected_keys)


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
    duplicate detector.

    Recalibrated 2026-11-10: with titles capped at 30 chars, unrelated
    titles sharing "高配当株" scored 0.23 while true same-theme pairs scored
    0.11-0.15, so this can't tell themes apart anymore. The threshold went
    up to 0.35 (literal rewording only) and theme-level repeats are caught
    by the model-assigned theme tag plus the cooldown check instead."""
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


def _normalize_theme(theme: str) -> str:
    return _normalize_for_compare(theme).lower()


def _bullets(lines: list[str], empty: str = "(なし)") -> str:
    return "\n".join(f"- {line}" for line in lines) or empty


def _build_prompt(
    trend_seeds: list[dict],
    fresh_notes: list[dict],
    new_tickers: list[dict],
    avoid_titles: list[str],
    recent_ideas: list[dict],
    top_own: list[dict],
    sold_titles: list[str],
    adopted: list[dict],
    rejected: list[dict],
    candidate_count: int,
    max_title_chars: int,
    theme_cooldown_days: int,
    tdnet_news: list[dict],
    market: dict,
    season: list[str],
) -> str:
    tdnet_text = _bullets(
        [f"{d['name']}({d['code']}) {d['pubdate'][:10]}「{d['title']}」" for d in tdnet_news], "(該当なし)"
    )
    headlines_text = _bullets(market.get("headlines") or [], "(取得できず)")
    movers_text = _bullets(
        [
            f"{m['name']}({m['code']}) {m['change_pct']:+.1f}%「{m['headline']}」"
            if m.get("change_pct") is not None
            else f"{m['name']}({m['code']})「{m['headline']}」"
            for m in (market.get("movers") or [])
        ]
    )
    season_text = _bullets(season)
    seeds_text = _bullets(
        [f"「{seed['title']}」(いいね{seed['like_count']}, 検索語:{seed['query']})" for seed in trend_seeds],
        "(取得できた参考記事なし)",
    )
    fresh_text = _bullets([f"「{n['title']}」" for n in fresh_notes], "(取得できず)")
    # Newest proposals come first in avoid_titles, so the cap only ever
    # trims the oldest own articles - before 2026-11-10 the cap cut the
    # newest ~100 proposals instead, and themes repeated every few days.
    avoid_text = _bullets(avoid_titles[:AVOID_TITLES_LIMIT])
    recent_text = _bullets(
        [f"{r['logged_date']} {r['title']}" + (f"(テーマ:{r['theme']})" if r.get("theme") else "") for r in recent_ideas]
    )
    top_own_text = _bullets([f"「{a['title']}」(いいね{a['like_count']})" for a in top_own])
    sold_text = _bullets([f"「{t}」" for t in sold_titles])
    adopted_text = _bullets([f"「{r['title']}」" for r in adopted])
    rejected_text = _bullets([f"「{r['title']}」" for r in rejected])
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

## このアカウントで結果が出たもの
実際に売れた有料記事(購入につながったのは「本当のところ・設計図・仕組み」型。有料部分に、他で無料では読めない試算や判断基準がある):
{sold_text}

いいねが多かった自分の記事(PVは「A vs B」の比較記事で集まるが、比較記事はほぼ売れていない):
{top_own_text}

筆者が過去に採用した構想:
{adopted_text}

筆者が過去に採用しなかった構想:
{rejected_text}

## 外部の参考
note.comで直近{DEFAULT_SEED_MAX_AGE_DAYS}日に伸びている記事(真似ではなく、切り口のヒントとして使う):
{seeds_text}

今まさに投稿されている記事(いいねはまだ少ないが、今日の話題がわかる):
{fresh_text}

## 一次情報(他のnote記事より先に書ける題材)
適時開示(TDnet)の配当関連。減配・無配・配当方針の変更が上に並ぶ:
{tdnet_text}

今日の市場ニュースの見出し:
{headlines_text}

ニュースで大きく動いた銘柄:
{movers_text}

今月・来月の高配当投資家の行事(書いてから公開するまでの時間を考え、来月の行事は今から仕込む):
{season_text}
{ticker_block}
時事性のある題材(適時開示・ニュース・note新着の話題・季節の行事)が使えるなら、{candidate_count}件のうち1〜2件はそれを使う。
個別銘柄を題材にする場合は、高配当投資家が配当の観点で判断できる銘柄に限る(配当のない銘柄、値動きだけの話題は使わない)。

## 記事の型(いずれか)
- 設計図: 配当生活・ポートフォリオの組み方を、試算と手順で示す
- 本当か: よく言われる通説や数字を検証して結論を出す
- 比較: A vs B の2択を数字で比べる
- 個別レビュー: 1銘柄を配当の持続性で評価する(タイトルに銘柄名と証券コード)
- まとめ: 制度・時期の情報を整理する

## 価格の区分(型ではなく「有料パートにしかないものがあるか」で決める)
- 有料・高め(500円): 自分で計算しないと出ない数字(試算・早見表)があり、無料パートだけではタイトルの問いに答えきれない
- 有料・安め(300円): 個別株・比較記事で、有料パートに「悪い年の配当」の試算や「買う・売る線」がある
- 無料(0円): 制度・時期のまとめなど、調べればわかる情報が中心。集客用にして有料記事へリンクする
有料パートにしかないものを1行で書けない案は「無料」にする。タイトルの問いへの答えの数字は有料パートに置く前提で考える。
{candidate_count}件のうち、少なくとも2件は「有料・高め」にする(これまで売れたのはこの区分だけ)。

このアカウントの記事カテゴリ(いずれかに寄せる):
{categories_text}

## 守るルール
- タイトルは{max_title_chars}字以内。言い切りか問いかけにする。「——」で始まるサブタイトルは付けない
- 筆者の保有銘柄・保有銘柄数・保有株数・資産配分を明かす、または前提にする案は出さない
- 筆者の実体験を前提にした案(「〜したら驚いた」「私の〜を公開」「〜した話」)は出さない。試算・制度・仕組み・通説の検証で書ける切り口にする
- 有料部分に何を置くか(読者が無料では手に入らないもの)を具体的に書ける案にする

## 絶対に避けること
直近{theme_cooldown_days}日に出した構想。テーマが同じものは、言い回しや数字を変えても出さない(例: 「入金をやめる時期」を年齢や金額だけ変えて出し直すのは不可):
{recent_text}

以下は既に投稿済み、または過去に提案済みのタイトル(新しい順)。内容が重複するテーマ・切り口は避ける:
{avoid_text}

出力はJSONのみ。説明文やコードフェンスは不要です。

JSON形式:
{{
  "ideas": [
    {{
      "title": "記事タイトル案({max_title_chars}字以内)",
      "article_type": "設計図 / 本当か / 比較 / 個別レビュー / まとめ のいずれか",
      "price_tier": "有料・高め / 有料・安め / 無料 のいずれか",
      "theme": "テーマを表す短い名詞句(例: 入金停止の時期、配当控除、JT減配リスク)",
      "angle": "切り口・フックを一文で",
      "paid_value": "有料パートにしかないものを一文で(無料なら、リンク先にする有料記事の方向性)",
      "category": "上記カテゴリのいずれか",
      "inspired_by": "参考にした記事のタイトル、または「独自」"
    }}
  ]
}}
""".strip()


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "note_article_ideas.json"
    generated_at = datetime.now(JST).isoformat()

    config = _load_config(root)
    if not can_run(config):
        payload = {
            "module": "note_article_ideas",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "GEMINI_API_KEY is not set.",
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[note_article_ideas] skipped: GEMINI_API_KEY is not set")
        return

    queries = [str(q).strip() for q in (config.get("queries") or DEFAULT_QUERIES) if str(q).strip()]
    daily_count = int(config.get("daily_count", DEFAULT_DAILY_COUNT))
    candidate_count = int(config.get("candidate_count", DEFAULT_CANDIDATE_COUNT))
    similarity_threshold = float(config.get("similarity_threshold", DEFAULT_SIMILARITY_THRESHOLD))
    seed_count = int(config.get("trend_seed_count", DEFAULT_TREND_SEED_COUNT))
    model = str(config.get("model") or DEFAULT_MODEL)
    seed_max_age_days = int(config.get("trend_seed_max_age_days", DEFAULT_SEED_MAX_AGE_DAYS))
    fresh_count = int(config.get("fresh_count", DEFAULT_FRESH_COUNT))
    theme_cooldown_days = int(config.get("theme_cooldown_days", DEFAULT_THEME_COOLDOWN_DAYS))
    max_title_chars = int(config.get("max_title_chars", DEFAULT_MAX_TITLE_CHARS))
    sold_titles = [str(t).strip() for t in (config.get("sold_titles") or []) if str(t).strip()]
    note_dir = Path(config.get("note_dir") or (root.parent / "note"))
    memo_dirs = [Path(p) for p in (config.get("memo_dirs") or [])] or [
        note_dir / "n_inbox" / "archive",
        note_dir / "n_inbox",
        root.parent / "inbox" / "archive",
    ]

    trend_seeds, fresh_notes, fetch_warnings = _collect_trend_seeds(
        queries, seed_count, seed_max_age_days, fresh_count
    )
    new_tickers, ranking_warnings = _collect_new_ranking_tickers(root)
    fetch_warnings += ranking_warnings
    tdnet_news, tdnet_warnings = idea_sources.collect_tdnet_dividend_news(
        int(config.get("tdnet_days", 2)), int(config.get("tdnet_limit", 25))
    )
    fetch_warnings += tdnet_warnings
    market = idea_sources.collect_market_context(
        root, int(config.get("market_max_age_hours", 36)), int(config.get("headline_limit", 15))
    )
    season = idea_sources.season_events(config.get("season_events"))
    refresh_candidates, refresh_warnings = idea_sources.collect_refresh_candidates(
        root, config.get("refresh_targets"), int(config.get("refresh_window_days", 30))
    )
    fetch_warnings += refresh_warnings

    own_articles = _own_articles(root)
    top_own = sorted(own_articles, key=lambda a: a["like_count"], reverse=True)[:10]
    adopted, rejected = _load_adoption_history(memo_dirs, note_dir)
    ideas_log = _load_ideas_log(root)
    proposed_newest_first = sorted(ideas_log, key=lambda r: r.get("logged_date", ""), reverse=True)
    avoid_titles = list(
        dict.fromkeys(
            [r.get("title", "") for r in proposed_newest_first if r.get("title")]
            + [a["title"] for a in own_articles]
        )
    )
    theme_cutoff = (datetime.now(JST) - timedelta(days=theme_cooldown_days)).strftime("%Y-%m-%d")
    recent_ideas = [r for r in proposed_newest_first if r.get("logged_date", "") >= theme_cutoff]
    recent_themes = {_normalize_theme(r["theme"]) for r in recent_ideas if r.get("theme")}

    prompt = _build_prompt(
        trend_seeds,
        fresh_notes,
        new_tickers,
        avoid_titles,
        recent_ideas,
        top_own,
        sold_titles,
        adopted[:10],
        rejected[:10],
        candidate_count,
        max_title_chars,
        theme_cooldown_days,
        tdnet_news,
        market,
        season,
    )
    usage_tracker = GeminiUsageTracker(model)

    try:
        llm = generate(prompt, config, DEFAULT_MODEL, cwd=root)
        text = llm.text
        usage_tracker.add(llm.gemini_usage)
        data = _extract_json(text)
        candidates = data.get("ideas") or []
        candidates = [
            {
                "title": str(idea.get("title") or "").strip(),
                "article_type": str(idea.get("article_type") or "").strip(),
                "price_tier": str(idea.get("price_tier") or "").strip(),
                "theme": str(idea.get("theme") or "").strip(),
                "angle": str(idea.get("angle") or "").strip(),
                "paid_value": str(idea.get("paid_value") or "").strip(),
                "category": str(idea.get("category") or "").strip(),
                "inspired_by": str(idea.get("inspired_by") or "").strip(),
            }
            for idea in candidates
            if isinstance(idea, dict) and str(idea.get("title") or "").strip()
        ]
        for candidate in candidates:
            candidate["price"] = PRICE_TIERS.get(candidate["price_tier"])
    except Exception as exc:
        payload = {
            "module": "note_article_ideas",
            "generated_at": generated_at,
            "status": "error",
            "error": str(exc),
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[note_article_ideas] LLM call failed: %s", exc)
        send_failure_mail("note記事アイデア", str(exc), config)
        return

    # Code-level checks: the prompt already asks for all of this, but that's
    # a request, not a guarantee. Each candidate (and each one already
    # accepted this run, since the model can also repeat itself) is checked
    # for title length, near-duplicate title, and a theme reused within the
    # cooldown. Whatever survives is the pool; the day's result may land
    # anywhere from 0 up to daily_count - it isn't padded back up.
    valid = []
    dropped = []
    rolling_avoid = list(avoid_titles)
    rolling_themes = set(recent_themes)
    for candidate in candidates:
        if len(candidate["title"]) > max_title_chars:
            dropped.append({**candidate, "reason": f"title over {max_title_chars} chars"})
            continue
        is_dup, matched = _is_duplicate(candidate["title"], rolling_avoid, similarity_threshold)
        if is_dup:
            dropped.append({**candidate, "reason": "duplicate title", "duplicate_of": matched})
            continue
        theme_key = _normalize_theme(candidate["theme"])
        if theme_key and theme_key in rolling_themes:
            dropped.append({**candidate, "reason": "theme used recently"})
            continue
        valid.append(candidate)
        rolling_avoid.append(candidate["title"])
        if theme_key:
            rolling_themes.add(theme_key)

    # Make sure at least one idea of the tier that actually sells is in the
    # day's set, if the model offered any that passed the checks.
    ideas = valid[:daily_count]
    if ideas and not any(i["price_tier"] == TOP_TIER for i in ideas):
        spare = next((c for c in valid[daily_count:] if c["price_tier"] == TOP_TIER), None)
        if spare:
            ideas[-1] = spare

    today_label = datetime.now(JST).strftime("%Y-%m-%d")
    for idea in ideas:
        ideas_log.append(
            {
                "logged_date": today_label,
                "title": idea["title"],
                "theme": idea["theme"],
                "article_type": idea["article_type"],
                "price_tier": idea["price_tier"],
            }
        )
    cutoff_label = (datetime.now(JST) - timedelta(days=IDEAS_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
    ideas_log = [r for r in ideas_log if r.get("logged_date", "9999-99-99") >= cutoff_label]
    _save_ideas_log(root, ideas_log)
    idea_sources.mark_refresh_notified(root, refresh_candidates)

    payload = {
        "module": "note_article_ideas",
        "generated_at": generated_at,
        "status": "ok",
        "model": model if llm.provider == "gemini" else llm.model,
        "provider": llm.provider,
        "cost_jpy": round(usage_tracker.cost_jpy, 2),
        "trend_seeds": trend_seeds,
        "fresh_notes": fresh_notes,
        "new_ranking_tickers": new_tickers,
        "adoption_history": {"adopted": len(adopted), "rejected": len(rejected)},
        "tdnet_dividend_news": tdnet_news,
        "market_context": market,
        "season_events": season,
        "refresh_candidates": refresh_candidates,
        "candidate_count": len(candidates),
        "dropped": dropped,
        "ideas": ideas,
    }
    if fetch_warnings:
        payload["warnings"] = fetch_warnings
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info(
        "[note_article_ideas] %d candidate(s) -> %d kept, %d dropped (from %d trend seed(s))",
        len(candidates), len(ideas), len(dropped), len(trend_seeds),
    )


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
