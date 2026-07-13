from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import subprocess
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv


JST = timezone(timedelta(hours=9), "JST")
DEFAULT_NOTE_CREATOR = "fukuoka_dividend"
DEFAULT_HISTORY_DAYS = 5
DEFAULT_MODEL = "gemini-3.1-flash-lite"
X_MAX_CHARS = 280
NOTE_API_URL = "https://note.com/api/v2/creators/{creator}/contents"
X_TWEET_URL = "https://api.twitter.com/2/tweets"


def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[post_x_note] invalid JSON ignored: %s", path)
        return None


def _dump_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_config(root: Path) -> dict:
    payload = _load_json(root / "config.json")
    return payload if isinstance(payload, dict) else {}


def _module_config(root: Path) -> dict:
    config = _load_config(root)
    payload = config.get("post_x_note", {})
    return payload if isinstance(payload, dict) else {}


def _is_truthy_env(name: str) -> bool:
    value = os.getenv(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _env_override_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _resolve_path(root: Path, value: str | None, default_relative: str) -> Path:
    candidate = Path(str(value)) if value else Path(default_relative)
    return candidate if candidate.is_absolute() else root / candidate


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    import re

    cleaned = re.sub(r"<[^>]+>", "", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _fetch_note_page_via_powershell(url: str) -> dict:
    script = (
        "$ErrorActionPreference='Stop';"
        "$ProgressPreference='SilentlyContinue';"
        f"$headers=@{{'User-Agent'='NightlyBatchNotify/1.0'}};"
        f"(Invoke-WebRequest -Uri '{url}' -Headers $headers -TimeoutSec 20 -UseBasicParsing).Content"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=45,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(f"PowerShell Invoke-WebRequest failed: {stderr or 'unknown error'}")
    return json.loads(completed.stdout)


def _fetch_note_page_via_curl(url: str) -> dict:
    completed = subprocess.run(
        [
            "curl.exe",
            "-fsSL",
            "-A",
            "NightlyBatchNotify/1.0",
            "--max-time",
            "20",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(f"curl.exe failed: {stderr or 'unknown error'}")
    return json.loads(completed.stdout)


def _fetch_note_page_via_urllib(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "NightlyBatchNotify/1.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_note_page(url: str) -> dict:
    errors: list[str] = []
    for label, fetcher in [
        ("PowerShell Invoke-WebRequest", _fetch_note_page_via_powershell),
        ("curl.exe", _fetch_note_page_via_curl),
        ("urllib", _fetch_note_page_via_urllib),
    ]:
        try:
            return fetcher(url)
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            logging.warning("[post_x_note] note fetch failed via %s: %s", label, exc)
    raise RuntimeError("; ".join(errors))


def _fetch_all_notes(creator: str, max_pages: int = 50) -> list[dict]:
    articles: list[dict] = []
    page = 1

    while page <= max_pages:
        url = NOTE_API_URL.format(creator=urllib.parse.quote(creator, safe=""))
        query = urllib.parse.urlencode({"kind": "note", "page": page})
        try:
            data = _fetch_note_page(f"{url}?{query}")
        except Exception as exc:
            raise RuntimeError(f"note API fetch failed on page {page}: {exc}") from exc

        contents = data.get("data", {}).get("contents", [])
        if not contents:
            break

        for item in contents:
            if item.get("status") != "published":
                continue
            title = str(item.get("name") or "").strip()
            key = str(item.get("key") or "").strip()
            if not title or not key:
                continue
            body_raw = str(item.get("body") or "").strip()
            description_raw = str(item.get("description") or "").strip()
            body_text = _strip_html(body_raw)
            description_text = _strip_html(description_raw)
            content_text = body_text or description_text
            articles.append(
                {
                    "id": item.get("id"),
                    "key": key,
                    "title": title,
                    "url": f"https://note.com/{creator}/n/{key}",
                    "price": item.get("price", 0),
                    "body": body_raw,
                    "description": description_raw,
                    "content_text": content_text,
                    "excerpt": content_text,
                    "publish_at": str(item.get("publishAt") or ""),
                }
            )

        page += 1

    return articles


def _load_cached_articles(path: Path) -> list[dict]:
    payload = _load_json(path)
    return payload if isinstance(payload, list) else []


def _save_cached_articles(path: Path, articles: list[dict]) -> None:
    _dump_json(path, articles)


def _load_text_values(path: Path) -> set[str]:
    values: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if value:
                values.add(value)
    return values


def _save_text_values(path: Path, values: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(sorted(values)) + ("\n" if values else ""), encoding="utf-8")


def _load_history(path: Path) -> list[dict]:
    payload = _load_json(path)
    return payload if isinstance(payload, list) else []


def _save_history(path: Path, history: list[dict]) -> None:
    _dump_json(path, history)


def _load_preview_article(path: Path) -> dict | None:
    payload = _load_json(path)
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                return item
    return None


def _recent_urls(history: list[dict], history_days: int) -> set[str]:
    cutoff = datetime.now(JST) - timedelta(days=history_days)
    recent: set[str] = set()
    for item in history:
        posted_at = str(item.get("posted_at") or "")
        url = str(item.get("url") or "")
        if not posted_at or not url:
            continue
        try:
            posted_dt = datetime.fromisoformat(posted_at)
        except ValueError:
            continue
        if posted_dt.tzinfo is None:
            posted_dt = posted_dt.replace(tzinfo=timezone.utc)
        if posted_dt.astimezone(JST) > cutoff:
            recent.add(url)
    return recent


def _filter_articles(
    articles: list[dict],
    history: list[dict],
    exclude_urls: set[str],
    exclude_keys: set[str],
    history_days: int,
) -> list[dict]:
    recent_urls = _recent_urls(history, history_days)
    filtered = []
    for article in articles:
        url = str(article.get("url") or "")
        key = str(article.get("key") or "")
        if not url:
            continue
        if url in recent_urls:
            continue
        if url in exclude_urls:
            continue
        if key and key in exclude_keys:
            continue
        filtered.append(article)
    filtered.sort(key=lambda item: item.get("publish_at") or "", reverse=True)
    return filtered


def _gemini_prompt(article: dict, attempt: int = 1) -> str:
    title = str(article.get("title") or "")
    excerpt = str(article.get("excerpt") or "")
    url = str(article.get("url") or "")
    target_limit = X_MAX_CHARS - ((attempt - 1) * 20)
    return (
        "縺ゅ↑縺溘・譌･譛ｬ隱槭・X謚慕ｨｿ繧剃ｽ懊ｋ邱ｨ髮・・〒縺吶・n"
        "谺｡縺ｮnote險倅ｺ九ｒ邏ｹ莉九☆繧区兜遞ｿ譁・ｒ1縺､縺縺大・縺励※縺上□縺輔＞縲・n"
        "譚｡莉ｶ:\n"
        "- 譌･譛ｬ隱杤n"
        f"- {target_limit}譁・ｭ嶺ｻ･蜀・n"
        "- 險倅ｺ九・隕∫せ繧堤洒縺上∪縺ｨ繧√ｋ\n"
        "- URL縺ｯ譛ｫ蟆ｾ縺ｫ1蝗槭□縺大・繧後ｋ\n"
        "- 繝上ャ繧ｷ繝･繧ｿ繧ｰ縺ｯ荳崎ｦ―n"
        "- 菴呵ｨ医↑蜑咲ｽｮ縺阪ｄ隱ｬ譏弱・荳崎ｦ―n\n"
        f"繧ｿ繧､繝医Ν: {title}\n"
        f"隕∫ｴ・ {excerpt}\n"
        f"URL: {url}\n"
    )


def _call_gemini(api_key: str, model: str, prompt: str) -> str:
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [
            {
                "parts": [{"text": prompt}],
            }
        ]
    }
    request = urllib.request.Request(
        api_url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini API HTTP {exc.code}: {detail}") from exc

    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini API returned no candidates.")
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(str(part.get("text", "")) for part in parts).strip()
    if not text:
        raise RuntimeError("Gemini API returned empty text.")
    return text


def _build_tweet_text(article: dict, gemini_api_key: str | None, model: str, attempt: int = 1) -> str:
    url = str(article.get("url") or "")
    title = str(article.get("title") or "")
    excerpt = str(article.get("excerpt") or "")

    if gemini_api_key:
        try:
            text = _call_gemini(gemini_api_key, model, _gemini_prompt(article, attempt=attempt))
            if url not in text:
                text = f"{text.rstrip()}\n{url}"
            return text.strip()
        except Exception as exc:
            logging.warning("[post_x_note] Gemini fallback used: %s", exc)

    base = f"note譖ｴ譁ｰ: {title}"
    body = excerpt[:120].strip()
    if body:
        base = f"{base}\n{body}"
    return f"{base}\n{url}".strip()


def _build_thread_text(article: dict, gemini_api_key: str | None, model: str, attempt: int = 1) -> str:
    title = str(article.get("title") or "").strip()
    excerpt = str(article.get("excerpt") or "").strip()
    url = str(article.get("url") or "").strip()
    if gemini_api_key:
        prompt = (
            "あなたは日本語でX投稿のスレッドを作る編集者です。\n"
            "以下のnote記事を、2つの投稿に分けて自然にまとめてください。\n"
            "条件:\n"
            "- 日本語で書く\n"
            "- 1通目は結論や要点を少し長めにまとめる\n"
            "- 1通目の末尾にハッシュタグを3〜6個入れる\n"
            "- 1通目に記事タイトルやURLを入れない\n"
            "- 1通目は見出しなしで始めてもよい\n"
            "- 2通目は理由や補足、読みどころを整理してURLを入れる\n"
            "- 2通目の末尾にURLを置く\n"
            "- 2通目は改行を多めに使い、スクロール中でも目に入りやすくする\n"
            "- 2通目は1〜2文中心で、かなり保守的に短くまとめる\n"
            "- 2通目は短い文章を2〜3段落で書く\n"
            "- 2通目は宣伝調、煽り、強い断定を避ける\n"
            "- 2通目はハッシュタグを入れない\n"
            "- 2通目は余計な記号を入れない\n"
            "- 2通目は箇条書きにしない\n"
            "- 2つの投稿の間には、必ず `---THREAD---` という1行だけを入れる\n"
            "- 記号や見出しは最小限\n"
            "- どちらも自然な文体\n"
            "- 断定しすぎない\n"
            "- 1通目は2〜4文程度で、読者を引き込むまとめにする\n"
            "- 2通目は1通目と同じ表現を繰り返さない\n"
            "- 2通目は1通目と同じ表現や同じ文を繰り返さない\n"
            "- どちらも280字以内\n"
            f"タイトル: {title}\n"
            f"本文材料: {excerpt}\n"
            f"URL: {url}\n"
        )
        try:
            text = _call_gemini(gemini_api_key, model, prompt)
            return text.strip()
        except Exception as exc:
            logging.warning("[post_x_note] Gemini thread text fallback used: %s", exc)

    hook = "気になる人向けに、まずは短くまとめました。"
    if title:
        hook = f"{hook}\n{title}"
    if excerpt:
        hook = f"{hook}\n{excerpt[:85].strip()}"
    details = excerpt[85:170].strip() if len(excerpt) > 85 else excerpt[:85].strip()
    second = "今回の記事では、論点を手短に整理しています。\n\n何が重要かを先に把握したい人向けに、読みどころを短くまとめました。"
    if details:
        second = f"{second}\n\n{details}"
    if url:
        second = f"{second}\n\n{url}"
    hook = f"{hook}\n#高配当株 #配当投資 #NISA #株式投資"
    return f"{hook}\n---THREAD---\n{second}".strip()


def _build_minimal_reply_text(article: dict) -> str:
    title = str(article.get("title") or "").strip()
    url = str(article.get("url") or "").strip()
    parts = ["補足です。", "詳しくは本文で整理しています。"]
    if title:
        parts.insert(1, title)
    if url:
        parts.append(url)
    return "\n\n".join(parts).strip()


def _normalize_tweet_text(text: str) -> str:
    cleaned = str(text).strip()
    replacements = [
        ("【要点】", ""),
        ("【結論】", ""),
        ("要点だけ押さえました。", ""),
        ("要点だけ押さえました", ""),
        ("まずは要点だけ押さえました。", ""),
        ("まずは要点だけ押さえました", ""),
        ("要点を順番に整理しています。", ""),
        ("要点を順番に整理しています", ""),
        ("⚠️ 本記事は情報提供を目的としたものです。投資・資産形成の判断はご自身の責任でお願いします。", ""),
        ("⚠️ 本記事は情報提供を目的としたものです。", ""),
        ("本記事は情報提供を目的としたものです。", ""),
        ("投資・資産形成の判断はご自身の責任でお願いします。", ""),
    ]
    for src, dst in replacements:
        cleaned = cleaned.replace(src, dst)
    cleaned = cleaned.replace("本記事は", "")
    cleaned = cleaned.replace("  ", " ")
    cleaned = cleaned.replace("\n\n\n", "\n\n")
    lines = [line.rstrip() for line in cleaned.splitlines()]
    cleaned = "\n".join(lines).strip()
    return cleaned


def _strip_hashtags(text: str) -> str:
    lines: list[str] = []
    for line in str(text).splitlines():
        if line.strip().startswith("#"):
            continue
        lines.append(line)
    cleaned = "\n".join(lines)
    cleaned = cleaned.replace(" #", " ")
    return cleaned.strip()
def _oauth_percent_encode(value: str) -> str:
    return urllib.parse.quote(value, safe="~-._")


def _oauth1_header(
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    access_token: str,
    access_token_secret: str,
) -> str:
    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": access_token,
        "oauth_version": "1.0",
    }
    params: list[tuple[str, str]] = []
    parsed = urllib.parse.urlsplit(url)
    params.extend(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    params.extend(oauth_params.items())
    normalized = "&".join(
        f"{_oauth_percent_encode(str(key))}={_oauth_percent_encode(str(value))}"
        for key, value in sorted(params)
    )
    base_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    base_string = "&".join(
        [
            method.upper(),
            _oauth_percent_encode(base_url),
            _oauth_percent_encode(normalized),
        ]
    )
    signing_key = f"{_oauth_percent_encode(consumer_secret)}&{_oauth_percent_encode(access_token_secret)}"
    signature = base64.b64encode(
        hmac.new(signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")
    oauth_params["oauth_signature"] = signature
    header = ", ".join(
        f'{key}="{_oauth_percent_encode(str(value))}"'
        for key, value in sorted(oauth_params.items())
    )
    return f"OAuth {header}"


def _post_to_x(text: str, reply_to_tweet_id: str | None = None) -> dict:
    api_key = os.getenv("X_API_KEY", "").strip()
    api_secret = os.getenv("X_API_SECRET", "").strip()
    access_token = os.getenv("X_ACCESS_TOKEN", "").strip()
    access_token_secret = os.getenv("X_ACCESS_TOKEN_SECRET", "").strip()

    missing = [
        name
        for name, value in [
            ("X_API_KEY", api_key),
            ("X_API_SECRET", api_secret),
            ("X_ACCESS_TOKEN", access_token),
            ("X_ACCESS_TOKEN_SECRET", access_token_secret),
        ]
        if not value
    ]
    if missing:
        raise RuntimeError("Missing X credentials: " + ", ".join(missing))

    payload: dict[str, object] = {"text": text}
    if reply_to_tweet_id:
        payload["reply"] = {"in_reply_to_tweet_id": reply_to_tweet_id}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": _oauth1_header(
            "POST",
            X_TWEET_URL,
            api_key,
            api_secret,
            access_token,
            access_token_secret,
        ),
    }
    request = urllib.request.Request(X_TWEET_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"X API HTTP {exc.code}: {detail}") from exc
    return payload


def _build_result_payload(
    generated_at: str,
    status: str,
    *,
    module_name: str = "post_x_note",
    reason: str | None = None,
    article: dict | None = None,
    tweet_text: str | None = None,
    reply_text: str | None = None,
    tweet_parts: list[str] | None = None,
    post_result: dict | None = None,
    warnings: list[str] | None = None,
    preview: bool | None = None,
) -> dict:
    payload: dict[str, object] = {
        "module": module_name,
        "generated_at": generated_at,
        "status": status,
        "data": None,
    }
    if reason:
        payload["reason"] = reason
    if article:
        payload["article"] = article
    if tweet_text:
        payload["tweet_text"] = tweet_text
    if reply_text:
        payload["reply_text"] = reply_text
    if tweet_parts:
        payload["tweet_parts"] = tweet_parts
    if post_result:
        payload["post_result"] = post_result
    if warnings:
        payload["warnings"] = warnings
    if preview is not None:
        payload["preview"] = preview
    return payload


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    generated_at = datetime.now(JST).isoformat()
    output_path = output_dir / "post_x_note.json"

    config = _module_config(root)
    enabled = bool(config.get("enabled", False))
    creator = str(config.get("creator") or DEFAULT_NOTE_CREATOR).strip() or DEFAULT_NOTE_CREATOR
    history_days = int(config.get("history_days", DEFAULT_HISTORY_DAYS))
    cache_path = _resolve_path(root, config.get("cache_file"), "state/post_x_note_cache.json")
    history_path = _resolve_path(root, config.get("history_file"), "state/post_x_note_history.json")
    exclude_path = _resolve_path(root, config.get("exclude_file"), "state/post_x_note_exclude_urls.txt")
    exclude_keys_path = _resolve_path(root, config.get("exclude_keys_file"), "state/post_x_note_exclude_keys.txt")
    max_pages = int(config.get("max_pages", 50))
    model = str(config.get("model") or DEFAULT_MODEL)
    split_post = _env_override_bool("POST_X_NOTE_SPLIT_POST", bool(config.get("split_post", True)))
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip() or None
    preview_mode = _is_truthy_env("POST_X_NOTE_PREVIEW")
    force_local = _is_truthy_env("POST_X_NOTE_FORCE_LOCAL")
    force_minimal_reply = _is_truthy_env("POST_X_NOTE_FORCE_MINIMAL_REPLY")
    preview_article_file = os.getenv("POST_X_NOTE_PREVIEW_ARTICLE_FILE", "").strip()
    source_article_file = os.getenv("POST_X_NOTE_SOURCE_FILE", "").strip()

    if not enabled and not preview_mode and not source_article_file:
        payload = _build_result_payload(
            generated_at,
            "skipped",
            reason="post_x_note is disabled in config.json.",
        )
        _dump_json(output_path, payload)
        logging.info("[post_x_note] skipped: disabled in config.json")
        return

    warnings: list[str] = []
    articles: list[dict]
    if (preview_mode or source_article_file) and (preview_article_file or source_article_file):
        preview_path = Path(preview_article_file or source_article_file)
        article = _load_preview_article(preview_path)
        if article:
            articles = [article]
            logging.info("[post_x_note] preview article loaded: %s", preview_path)
        else:
            payload = _build_result_payload(
                generated_at,
                "error",
                reason=f"Preview article file did not contain a valid article: {preview_path}",
            )
            _dump_json(output_path, payload)
            logging.error("[post_x_note] invalid preview article file: %s", preview_path)
            return
    else:
        try:
            articles = _fetch_all_notes(creator, max_pages=max_pages)
            _save_cached_articles(cache_path, articles)
        except Exception as exc:
            warnings.append(str(exc))
            articles = _load_cached_articles(cache_path)
            if not articles:
                payload = _build_result_payload(
                    generated_at,
                    "error",
                    reason=str(exc),
                    warnings=warnings,
                )
                _dump_json(output_path, payload)
                logging.error("[post_x_note] note fetch failed and cache is empty: %s", exc)
                return

    history = _load_history(history_path)
    exclude_urls = _load_text_values(exclude_path)
    exclude_keys = _load_text_values(exclude_keys_path)
    candidates = _filter_articles(articles, history, exclude_urls, exclude_keys, history_days)

    if not candidates:
        payload = _build_result_payload(
            generated_at,
            "skipped",
            reason="No eligible note article found.",
            warnings=warnings or None,
        )
        _dump_json(output_path, payload)
        logging.info("[post_x_note] skipped: no eligible article")
        return

    article = candidates[0]
    tweet_text = ""
    reply_text = ""
    for attempt in range(1, 4):
        tweet_text = _build_thread_text(
            article,
            None if force_local else gemini_api_key,
            model,
            attempt=attempt,
        )
        tweet_text = _normalize_tweet_text(tweet_text)
        parts = [part.strip() for part in tweet_text.split("---THREAD---") if part.strip()]
        if len(parts) == 2 and all(len(part) <= X_MAX_CHARS for part in parts):
            tweet_text = _normalize_tweet_text(parts[0])
            reply_text = _normalize_tweet_text(parts[1])
            break
        logging.warning(
            "[post_x_note] thread text exceeded %s chars or invalid format on attempt %s/3",
            X_MAX_CHARS,
            attempt,
        )
    else:
        payload = _build_result_payload(
            generated_at,
            "skipped",
            reason=f"Tweet text exceeded {X_MAX_CHARS} chars after 3 attempts.",
            article=article,
            tweet_text=tweet_text,
            warnings=warnings or None,
        )
        _dump_json(output_path, payload)
        logging.warning(
            "[post_x_note] skipped: tweet text exceeded %s chars after 3 attempts for %s",
            X_MAX_CHARS,
            article.get("title"),
        )
        return

    if preview_mode:
        payload = _build_result_payload(
            generated_at,
            "preview",
            reason="Preview mode; note fetch and tweet text generation completed without posting.",
            article=article,
            tweet_text=tweet_text,
            reply_text=reply_text if reply_text else None,
            tweet_parts=[tweet_text, reply_text] if reply_text else None,
            warnings=warnings or None,
            preview=True,
        )
        _dump_json(output_path, payload)
        logging.info("[post_x_note] preview completed: %s", article.get("title"))
        return

    if split_post:
        try:
            post_result = _post_to_x(tweet_text)
            tweet_id = str(post_result.get("data", {}).get("id") or "").strip()
            if not tweet_id:
                raise RuntimeError("X API returned no tweet id for the primary post.")
            if force_minimal_reply:
                reply_text = _build_minimal_reply_text(article)
                reply_result = _post_to_x(reply_text, reply_to_tweet_id=tweet_id)
            else:
                try:
                    reply_result = _post_to_x(reply_text, reply_to_tweet_id=tweet_id)
                except Exception as reply_exc:
                    minimal_reply_text = _build_minimal_reply_text(article)
                    if minimal_reply_text != reply_text:
                        logging.warning(
                            "[post_x_note] reply post failed; retrying with minimal reply: %s",
                            reply_exc,
                        )
                        reply_result = _post_to_x(minimal_reply_text, reply_to_tweet_id=tweet_id)
                        reply_text = minimal_reply_text
                    else:
                        raise
        except Exception as exc:
            payload = _build_result_payload(
                generated_at,
                "error",
                reason=str(exc),
                article=article,
                tweet_text=tweet_text,
                reply_text=reply_text,
                warnings=warnings or None,
            )
            _dump_json(output_path, payload)
            logging.error("[post_x_note] X split post failed: %s", exc)
            return

        history.append(
            {
                "key": article.get("key"),
                "url": article.get("url"),
                "title": article.get("title"),
                "posted_at": generated_at,
            }
        )
        _save_history(history_path, history[-200:])

        payload = _build_result_payload(
            generated_at,
            "ok",
            article=article,
            tweet_text=tweet_text,
            reply_text=reply_text if reply_text else None,
            tweet_parts=[tweet_text, reply_text] if reply_text else None,
            post_result={"primary": post_result, "reply": reply_result},
            warnings=warnings or None,
        )
        _dump_json(output_path, payload)
        logging.info("[post_x_note] posted note article in split mode: %s", article.get("title"))
        return

    try:
        post_result = _post_to_x(tweet_text)
    except Exception as exc:
        payload = _build_result_payload(
            generated_at,
            "error",
            reason=str(exc),
            article=article,
            tweet_text=tweet_text,
            reply_text=reply_text if reply_text else None,
            tweet_parts=[tweet_text, reply_text] if reply_text else None,
            warnings=warnings or None,
        )
        _dump_json(output_path, payload)
        logging.error("[post_x_note] X post failed: %s", exc)
        return

    history.append(
        {
            "key": article.get("key"),
            "url": article.get("url"),
            "title": article.get("title"),
            "posted_at": generated_at,
        }
    )
    _save_history(history_path, history[-200:])

    payload = _build_result_payload(
        generated_at,
        "ok",
        article=article,
        tweet_text=tweet_text,
        reply_text=reply_text if reply_text else None,
        tweet_parts=[tweet_text, reply_text] if reply_text else None,
        post_result=post_result,
        warnings=warnings or None,
    )
    _dump_json(output_path, payload)
    logging.info("[post_x_note] posted note article: %s", article.get("title"))


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)

