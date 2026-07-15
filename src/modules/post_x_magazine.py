from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from modules.mail_gmail import send_html_mail
from modules.post_x_note import (
    _build_result_payload,
    _call_gemini,
    _dump_json,
    _fetch_all_notes,
    _filter_articles,
    _load_cached_articles,
    _load_history,
    _load_json,
    _load_text_values,
    _normalize_tweet_text,
    _post_to_x,
    _resolve_path,
    _save_cached_articles,
    _save_history,
    _strip_html,
    _upload_media_to_x,
)


JST = timezone(timedelta(hours=9), "JST")
DEFAULT_NOTE_CREATOR = "fukuoka_dividend"
DEFAULT_MODEL = "gemini-3.1-flash-lite"
X_MAX_CHARS = 280
THREAD_PARTS_COUNT = 5  # 1 primary post + 4 replies (last reply promotes other magazines)


def _is_truthy_env(name: str) -> bool:
    value = os.getenv(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _render_notification_block(title: str, body_lines: list[str], *, error: str | None = None, reason: str | None = None) -> str:
    error_html = ""
    if error:
        reason_html = f"<div style='color:#b00020;margin-top:4px;'>Reason: {reason}</div>" if reason else ""
        error_html = f"<div style='color:#b00020;font-weight:700;margin-top:6px;'>ERROR</div>{reason_html}"
    lines_html = "".join(f"<div style='margin:2px 0;'>{line}</div>" for line in body_lines if line)
    return f"""
      <div style="border:1px solid #ddd;background:#fafafa;padding:10px;margin:8px 0;">
        <div style="font-weight:700;margin-bottom:6px;">{title}</div>
        {lines_html}
        {error_html}
      </div>
    """


def _send_post_notification(
    root: Path,
    generated_at: str,
    magazine: dict,
    article: dict | None,
    tweet_parts: list[str],
    *,
    status: str,
    reason: str | None = None,
    failed_part_index: int | None = None,
    total_parts: int | None = None,
) -> None:
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
    if missing:
        logging.warning("[post_x_magazine] failure mail skipped: missing Gmail settings: %s", ", ".join(missing))
        return

    subject = f"[NightlyBatchNotify] X post {status}: {magazine.get('name') or magazine.get('id') or 'post_x_magazine'}"
    article_title = str(article.get("title") or "").strip() if article else "-"
    part_label = "-"
    if failed_part_index is not None:
        if total_parts and total_parts > 0:
            part_label = f"{failed_part_index}/{total_parts}"
        else:
            part_label = str(failed_part_index)
    rows = [
        _render_notification_block(
            "Summary",
            [
                f"Time: {generated_at}",
                f"Magazine: {magazine.get('name') or magazine.get('id') or '-'}",
                f"Article: {article_title}",
                f"Status: {status}",
                f"Failed part: {part_label}" if part_label != "-" else "",
            ],
            error="yes" if reason else None,
            reason=reason,
        )
    ]
    for index in range(THREAD_PARTS_COUNT):
        part = tweet_parts[index] if index < len(tweet_parts) else ""
        rows.append(
            _render_notification_block(
                f"Post {index + 1}",
                [part or "(empty)"],
                error="yes" if failed_part_index == index + 1 and reason else None,
                reason=reason if failed_part_index == index + 1 else None,
            )
        )
    body = f"""
    <html>
      <body>
        <h2>X post {status}</h2>
        {''.join(rows)}
      </body>
    </html>
    """
    send_html_mail(gmail_address, app_password, mail_to, subject, body)


def _load_config(root: Path) -> dict:
    payload = _load_json(root / "config.json")
    return payload if isinstance(payload, dict) else {}


def _module_config(root: Path) -> dict:
    config = _load_config(root)
    payload = config.get("post_x_magazine", {})
    return payload if isinstance(payload, dict) else {}


def _load_articles_from_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        logging.warning("[post_x_magazine] article source ignored: %s (%s)", path, exc)
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _load_magazines(raw_magazines: object) -> list[dict]:
    magazines: list[dict] = []
    if not isinstance(raw_magazines, list):
        return magazines
    for item in raw_magazines:
        if not isinstance(item, dict):
            continue
        magazine_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or magazine_id).strip()
        tweet_id = str(item.get("tweet_id") or "").strip()
        tweet_url = str(item.get("tweet_url") or "").strip()
        article_keys_raw = item.get("article_keys") or []
        article_keys = [
            str(value).strip()
            for value in article_keys_raw
            if str(value).strip()
        ] if isinstance(article_keys_raw, list) else []
        magazines.append(
            {
                "id": magazine_id,
                "name": name or magazine_id,
                "tweet_id": tweet_id,
                "tweet_url": tweet_url,
                "article_keys": article_keys,
                "enabled": bool(item.get("enabled", False)),
            }
        )
    return magazines


def _load_rotation_state(path: Path) -> dict:
    payload = _load_json(path)
    if isinstance(payload, dict):
        return payload
    return {"last_index": -1, "last_magazine_id": "", "updated_at": None}


def _save_rotation_state(path: Path, state: dict) -> None:
    _dump_json(path, state)


def _next_index(current_index: int, length: int) -> int:
    if length <= 0:
        return 0
    return (current_index + 1) % length


def _base_hashtags_for_magazine(magazine: dict) -> list[str]:
    magazine_id = str(magazine.get("id") or "").strip()
    names = {
        "high_dividend_review": ["#高配当株", "#配当投資", "#資産運用", "#長期投資", "#NISA"],
        "etf_index": ["#ETF", "#インデックス投資", "#資産運用", "#NISA", "#分散投資"],
        "fire_living": ["#FIRE", "#配当生活", "#高配当株", "#資産運用", "#NISA"],
        "tax_inheritance": ["#税金", "#相続", "#制度", "#NISA", "#資産運用"],
        "portfolio_rules": ["#高配当株", "#ポートフォリオ", "#資産運用", "#配当投資", "#長期投資"],
    }
    return names.get(magazine_id, ["#高配当株", "#資産運用", "#投資", "#NISA", "#長期投資"])


def _closing_question_for_magazine(magazine: dict) -> str:
    magazine_id = str(magazine.get("id") or "").strip()
    questions = {
        "high_dividend_review": "あなたなら、最初の1株に何を選びますか？",
        "etf_index": "あなたは個別株派、それともETF派ですか？",
        "fire_living": "配当だけで生活費を賄うなら、目標は月いくらにしますか？",
        "tax_inheritance": "この制度、あなたの家族にも当てはまりそうですか？",
        "portfolio_rules": "あなたのポートフォリオ、今のルールで納得できていますか？",
    }
    return questions.get(magazine_id, "あなたはどう考えますか？")


def _magazine_promo_line(magazine: dict) -> str:
    magazine_name = str(magazine.get("name") or "").strip()
    if magazine_name:
        return (
            f"この投稿は「{magazine_name}」マガジンの1本です。\n"
            "他のテーマ別マガジンはプロフィール固定ツイートにまとめています。\n"
            "よければそちらもチェックしてみてください。"
        )
    return "他のテーマ別マガジンはプロフィール固定ツイートにまとめています。よければそちらもチェックしてみてください。"


def _article_hashtags(article: dict | None, magazine: dict) -> list[str]:
    text = " ".join(
        part for part in [
            str(article.get("title") or "").strip() if article else "",
            str(article.get("excerpt") or "").strip() if article else "",
            str(magazine.get("name") or "").strip(),
        ]
        if part
    )
    tags = list(_base_hashtags_for_magazine(magazine))

    keyword_map = [
        ("FIRE", "#FIRE"),
        ("iDeCo", "#iDeCo"),
        ("NISA", "#NISA"),
        ("ETF", "#ETF"),
        ("インデックス", "#インデックス投資"),
        ("配当", "#配当投資"),
        ("高配当", "#高配当株"),
        ("相続", "#相続"),
        ("税", "#税金"),
        ("株主還元", "#株主還元"),
        ("ポートフォリオ", "#ポートフォリオ"),
    ]
    for keyword, tag in keyword_map:
        if keyword in text and tag not in tags:
            tags.append(tag)

    unique: list[str] = []
    for tag in tags:
        if tag not in unique:
            unique.append(tag)
    while len(unique) < 4:
        unique.append("#高配当株")
    return unique[:6]


def _article_content_text(article: dict | None) -> str:
    if not article:
        return ""
    for key in ("content_text", "excerpt", "description", "body"):
        value = str(article.get(key) or "").strip()
        if value:
            return _strip_html(value)
    return ""


def _split_article_text(text: str, parts: int = 3, chunk_size: int = 90) -> list[str]:
    import re

    cleaned = _normalize_tweet_text(text)
    if not cleaned:
        return [""] * parts
    sentences = [segment.strip() for segment in re.split(r"(?<=[。！？!?])", cleaned) if segment.strip()]
    if not sentences:
        sentences = [cleaned]

    def _hard_split(piece: str) -> list[str]:
        # Prefer breaking on the Japanese comma (、) before falling back to a
        # raw character cut, so an oversized run-on sentence doesn't get
        # sliced mid-word. The ASCII "," is deliberately excluded: in these
        # articles it almost always appears as a thousands separator (e.g.
        # "18,000円"), and splitting on it would break numbers apart.
        sub_pieces = [p.strip() for p in re.split(r"(?<=[、])", piece) if p.strip()]
        if not sub_pieces:
            sub_pieces = [piece]
        result: list[str] = []
        sub_current = ""
        for sub in sub_pieces:
            if len(sub) > chunk_size:
                if sub_current:
                    result.append(sub_current.strip())
                    sub_current = ""
                result.extend(
                    seg.strip()
                    for seg in (sub[i : i + chunk_size] for i in range(0, len(sub), chunk_size))
                    if seg.strip()
                )
                continue
            candidate = f"{sub_current}{sub}".strip()
            if not sub_current or len(candidate) <= chunk_size:
                sub_current = candidate
            else:
                result.append(sub_current.strip())
                sub_current = sub
        if sub_current:
            result.append(sub_current.strip())
        return result

    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > chunk_size:
            if current:
                chunks.append(current.strip())
                current = ""
            chunks.extend(_hard_split(sentence))
            continue
        candidate = f"{current}{sentence}".strip()
        if not current or len(candidate) <= chunk_size:
            current = candidate
            continue
        chunks.append(current.strip())
        current = sentence
    if current:
        chunks.append(current.strip())

    if len(chunks) <= 1 and len(cleaned) > chunk_size:
        chunks = [cleaned[i : i + chunk_size].strip() for i in range(0, len(cleaned), chunk_size) if cleaned[i : i + chunk_size].strip()]

    while len(chunks) < parts:
        chunks.append("")
    return chunks[:parts]


def _compose_with_hashtags(body: str, hashtags: list[str], limit: int = 280) -> str:
    body = _normalize_tweet_text(body).strip()
    tags = list(hashtags)
    if not tags:
        return body[:limit].rstrip()

    while tags:
        tag_line = " ".join(tags)
        separator = "\n\n" if body else ""
        candidate = f"{body}{separator}{tag_line}".strip()
        if len(candidate) <= limit:
            return candidate
        if len(tags) > 4:
            tags.pop()
            continue
        break

    tag_line = " ".join(tags)
    separator = "\n\n" if body else ""
    reserved = len(separator) + len(tag_line)
    available = max(0, limit - reserved)
    if len(body) > available:
        body = body[:available].rstrip()
        while body and body[-1] in "、。,.!！?？・ ":
            body = body[:-1].rstrip()
    if body:
        return f"{body}{separator}{tag_line}".strip()
    return tag_line[:limit].rstrip()


def _build_prompt(article: dict | None, magazine: dict) -> str:
    magazine_name = str(magazine.get("name") or "").strip()
    title = str(article.get("title") or "").strip() if article else ""
    article_text = _article_content_text(article)
    return (
        "あなたは日本語でXの単独投稿文を作成する役割です。\n"
        "次のnote本文を、URLなし・replyなしで、5本の投稿に分けて要約してください。\n"
        "制約:\n"
        "- 日本語で書く\n"
        "- 5つの投稿本文のみを出力する\n"
        "- 5つの投稿の間には、他の記号を一切付けずに ---THREAD--- という行だけを区切りとして入れる\n"
        "- ---THREAD--- 以外の見出しや番号（1本目、Post1など）は付けない\n"
        "- URLは本文に入れない\n"
        "- reply先を示す文言は入れない\n"
        "- 1本目の最初の行は、内容を表す短いキャッチコピーを【】で囲んで書く（「タイトル」という文字列自体は書かない）\n"
        "- 1本目の【】の次の行は、具体的な金額・年数・利回りなどの数字か、体験談ふうの一文から書き始めてタイムラインで目を止めさせる（「〜について解説します」のような説明口調で始めない）\n"
        "- 1本目は導入と主題\n"
        "- 2本目は内容の具体点\n"
        "- 3本目は実用性や気づき\n"
        "- 4本目は読後の持ち帰りで締め、最後の行に記事のテーマに沿った読者への問いかけを1文入れて返信を誘う（紋切り型のテンプレ文をそのまま使わず、内容に合わせた自然な問いにする）\n"
        "- 5本目は、この投稿が「マガジン名」マガジンの1本であることに軽く触れ、他のテーマ別マガジンはプロフィール固定ツイートにまとめてあるので興味があれば見てほしいと1〜2文で自然に誘導する（宣伝口調になりすぎない）\n"
        "- 各投稿は5行で書く\n"
        "- 各行は短く、見出しっぽくしない\n"
        "- 各投稿は140文字前後に収める\n"
        "- 宣伝文句ではなく、有用な情報として自然に書く\n"
        "- 抽象的な感想だけで終わらせず、本文の具体的な内容を含める\n"
        "- 有料部分が読める場合は、無理に入れず品質向上に役立つ範囲だけ使う\n"
        "- 記事という言い方をくどくしない\n"
        "- 断定しすぎず、でも読みどころが伝わるようにする\n"
        "- 文章は平易で、短く、読みやすくする\n"
        "- 1本ごとに最大でも180文字以内\n"
        "- ハッシュタグは4〜6個を最後の投稿にだけ付ける\n"
        f"マガジン名: {magazine_name}\n"
        f"関連記事タイトル: {title}\n"
        f"取得できた本文: {article_text}\n"
    )


def _content_chunks(article: dict | None, title: str, excerpt: str, magazine_name: str) -> list[str]:
    # title/magazine_name are intentionally excluded here: neither has trailing
    # punctuation, so mixing them into the sentence-split source text caused
    # them to fuse onto the adjacent real sentence instead of standing alone
    # (title duplicated into the first chunk, magazine name bleeding into the
    # last one). Only the article body itself gets chunked.
    source = excerpt.strip() or title
    if source and source[-1] not in "。！？!?":
        # Cached note excerpts are often cut off mid-sentence (paywall preview
        # boundary). Drop the trailing incomplete fragment so the last chunk
        # doesn't end mid-word instead of rendering the raw cutoff.
        trim_idx = max(source.rfind(mark) for mark in "。！？!?、")
        if trim_idx > 0:
            source = source[: trim_idx + 1]
    chunks = [chunk.strip() for chunk in _split_article_text(source, parts=16, chunk_size=80) if chunk.strip()]
    if chunks:
        return chunks
    fallback = [part for part in [title.strip(), excerpt.strip(), magazine_name.strip()] if part]
    return fallback or [""]


def _build_content_thread_parts(article: dict | None, magazine: dict) -> list[str]:
    magazine_name = str(magazine.get("name") or "").strip()
    title = str(article.get("title") or "").strip() if article else ""
    excerpt = _article_content_text(article)
    title_line = title[:80] if title else (magazine_name or "要点")
    chunks = _content_chunks(article, title, excerpt, magazine_name)

    # Distribute chunks evenly across the first 4 posts (instead of fixed
    # batches of 4) so a short article doesn't leave later posts empty, which
    # previously fell back to re-slicing the excerpt from its start -
    # duplicating earlier posts and cutting mid-word. The 5th post is not
    # filled from article content at all; it's a dedicated cross-promo for
    # the other magazines (see below).
    bucket_size = max(1, -(-len(chunks) // 4))
    grouped = [chunks[i : i + bucket_size] for i in range(0, len(chunks), bucket_size)]
    while len(grouped) < 4:
        grouped.append([])

    parts: list[str] = []
    used_generic_fallback = False
    for index in range(4):
        group = grouped[index] if index < len(grouped) else []
        lines = list(group[:5])
        if index == 0:
            lines = [f"【{title_line}】"] + lines
        if not lines:
            if index == 0:
                lines = [title_line]
            elif not used_generic_fallback:
                lines = [f"{magazine_name}の視点から要点をまとめました。" if magazine_name else title_line]
                used_generic_fallback = True
            else:
                # A second (or later) empty group would only repeat the same
                # generic line verbatim; leave it blank so the caller's
                # empty-string filter drops it instead of posting a duplicate.
                parts.append("")
                continue
        if index == 3:
            lines = lines + [_closing_question_for_magazine(magazine)]
        parts.append(_format_multiline_tweet("\n".join(lines), max_lines=5)[:220].rstrip())

    parts.append(_format_multiline_tweet(_magazine_promo_line(magazine), max_lines=5)[:220].rstrip())
    return parts[:THREAD_PARTS_COUNT]


def _build_thread_parts(article: dict | None, magazine: dict, gemini_api_key: str | None, model: str) -> list[str]:
    magazine_name = str(magazine.get("name") or "").strip()
    title = str(article.get("title") or "").strip() if article else ""
    excerpt = _article_content_text(article)
    title_line = title[:80] if title else (magazine_name or "要点")
    if gemini_api_key:
        try:
            text = _call_gemini(gemini_api_key, model, _build_prompt(article, magazine))
            raw_parts = [part.strip() for part in text.split("---THREAD---") if part.strip()]
            parts = [
                _format_multiline_tweet(part, max_lines=5)[:220].rstrip()
                for part in raw_parts[:THREAD_PARTS_COUNT]
            ]
            while len(parts) < THREAD_PARTS_COUNT:
                parts.append("")
            if all(parts):
                return parts[:THREAD_PARTS_COUNT]
            logging.warning(
                "[post_x_magazine] Gemini response did not split into %d usable parts (got %d); using content fallback",
                THREAD_PARTS_COUNT,
                sum(1 for part in parts if part),
            )
        except Exception as exc:
            logging.warning("[post_x_magazine] Gemini fallback used: %s", exc)

    return _build_content_thread_parts(article, magazine)


def _build_thread_fallback_parts(article: dict | None, magazine: dict) -> list[str]:
    parts = _build_content_thread_parts(article, magazine)
    return [part[:160].rstrip() for part in parts]


def _format_multiline_tweet(text: str, max_lines: int = 5) -> str:
    import re

    cleaned = _normalize_tweet_text(text).strip()
    if not cleaned:
        return ""
    # Don't break right after sentence-ending punctuation when it's immediately
    # followed by a closing bracket (e.g. "どれ？】") - otherwise the bracket
    # gets pushed onto its own line, separated from the title it closes.
    cleaned = re.sub(r"([。！？!?])(?![】」』\)])\s*", r"\1\n", cleaned)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    return "\n".join(lines).strip()


def _attach_hashtags_to_last_part(parts: list[str], article: dict | None, magazine: dict) -> list[str]:
    if not parts:
        return parts
    tags = _article_hashtags(article, magazine)
    if not tags:
        return parts
    updated = list(parts)
    last_body = _format_multiline_tweet(updated[-1], max_lines=4)
    tag_line = " ".join(tags[:6]).strip()
    if last_body:
        updated[-1] = f"{last_body}\n{tag_line}".strip()
    else:
        updated[-1] = tag_line
    return updated


def _article_source_candidates(root: Path, config: dict) -> list[Path]:
    candidates: list[Path] = []
    source_file = str(config.get("fallback_articles_file") or "").strip()
    if source_file:
        path = Path(source_file)
        candidates.append(path if path.is_absolute() else root / path)

    legacy_path = root.parent / "all_md_files_20260707" / "★tweet_tool" / "tweet_tool" / "note_articles_cache.json"
    candidates.append(legacy_path)
    return candidates


POST_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _pick_post_image(root: Path) -> Path | None:
    image_dir = root / "post_images"
    if not image_dir.is_dir():
        return None
    candidates = [
        path
        for path in image_dir.iterdir()
        if path.is_file() and path.suffix.lower() in POST_IMAGE_EXTENSIONS
    ]
    if not candidates:
        return None
    return random.choice(candidates)


def _mark_image_as_posted(image_path: Path) -> None:
    posted_dir = image_path.parent / "posted"
    posted_dir.mkdir(exist_ok=True)
    destination = posted_dir / image_path.name
    if destination.exists():
        destination = posted_dir / f"{image_path.stem}_{int(time.time())}{image_path.suffix}"
    image_path.rename(destination)


def _select_article(
    articles: list[dict],
    magazine: dict,
    history: list[dict],
    exclude_urls: set[str],
    exclude_keys: set[str],
    history_days: int,
) -> dict | None:
    article_keys = set(str(value).strip() for value in magazine.get("article_keys") or [] if str(value).strip())
    matched_articles = articles
    if article_keys:
        matched_articles = [
            article
            for article in articles
            if str(article.get("key") or "").strip() in article_keys
        ]
    filtered = _filter_articles(matched_articles, history, exclude_urls, exclude_keys, history_days)
    if not filtered:
        return None
    return random.choice(filtered)


def _select_magazine_and_article(
    magazines: list[dict],
    rotation_state: dict,
    articles: list[dict],
    history: list[dict],
    exclude_urls: set[str],
    exclude_keys: set[str],
    history_days: int,
) -> tuple[dict | None, dict | None, int]:
    if not magazines:
        return None, None, -1
    start_index = int(rotation_state.get("last_index", -1))
    start_index = _next_index(start_index, len(magazines))
    for offset in range(len(magazines)):
        index = (start_index + offset) % len(magazines)
        magazine = magazines[index]
        if not magazine.get("enabled", False):
            continue
        article = _select_article(articles, magazine, history, exclude_urls, exclude_keys, history_days)
        if article:
            return magazine, article, index
    return None, None, -1


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    generated_at = datetime.now(JST).isoformat()
    output_path = output_dir / "post_x_magazine.json"

    config = _module_config(root)
    enabled = bool(config.get("enabled", False))
    magazines = _load_magazines(config.get("magazines", []))
    rotation_path = _resolve_path(root, config.get("rotation_state_file"), "state/post_x_magazine_rotation.json")
    history_path = _resolve_path(root, config.get("history_file"), "state/post_x_magazine_history.json")
    cache_path = _resolve_path(root, config.get("cache_file"), "state/post_x_magazine_cache.json")
    exclude_path = _resolve_path(root, config.get("exclude_file"), "state/post_x_magazine_exclude_urls.txt")
    exclude_keys_path = _resolve_path(root, config.get("exclude_keys_file"), "state/post_x_magazine_exclude_keys.txt")
    history_days = int(config.get("history_days", 5))
    max_pages = int(config.get("max_pages", 50))
    model = str(config.get("model") or DEFAULT_MODEL)
    creator = str(config.get("creator") or DEFAULT_NOTE_CREATOR).strip() or DEFAULT_NOTE_CREATOR
    preview_mode = _is_truthy_env("POST_X_MAGAZINE_PREVIEW")
    notify_only_mode = _is_truthy_env("POST_X_MAGAZINE_NOTIFY_ONLY")
    force_overflow_test = _is_truthy_env("POST_X_MAGAZINE_FORCE_OVERFLOW_TEST")
    source_article_file = os.getenv("POST_X_MAGAZINE_SOURCE_FILE", "").strip()
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip() or None

    if not enabled and not preview_mode and not source_article_file and not notify_only_mode:
        payload = _build_result_payload(
            generated_at,
            "skipped",
            module_name="post_x_magazine",
            reason="post_x_magazine is disabled in config.json.",
        )
        _dump_json(output_path, payload)
        logging.info("[post_x_magazine] skipped: disabled in config.json")
        return

    if source_article_file:
        source_path = Path(source_article_file)
        if not source_path.is_absolute():
            source_path = root / source_path
        articles = _load_articles_from_file(source_path)
    else:
        articles = _load_cached_articles(cache_path)
        if not articles:
            for candidate in _article_source_candidates(root, config):
                articles = _load_articles_from_file(candidate)
                if articles:
                    logging.info("[post_x_magazine] using local article source: %s", candidate)
                    break
        if not articles:
            try:
                articles = _fetch_all_notes(creator, max_pages=max_pages)
                _save_cached_articles(cache_path, articles)
            except Exception as exc:
                articles = _load_cached_articles(cache_path)
                if not articles:
                    for candidate in _article_source_candidates(root, config):
                        articles = _load_articles_from_file(candidate)
                        if articles:
                            logging.info("[post_x_magazine] fallback article source after fetch failure: %s", candidate)
                            break
                if not articles:
                    payload = _build_result_payload(
                        generated_at,
                        "error",
                        module_name="post_x_magazine",
                        reason=str(exc),
                    )
                    _dump_json(output_path, payload)
                    logging.error("[post_x_magazine] note fetch failed and cache is empty: %s", exc)
                    return

    if not magazines:
        payload = _build_result_payload(
            generated_at,
            "skipped",
            module_name="post_x_magazine",
            reason="No magazines configured.",
        )
        _dump_json(output_path, payload)
        logging.info("[post_x_magazine] skipped: no magazines configured")
        return

    history = _load_history(history_path)
    exclude_urls = _load_text_values(exclude_path)
    exclude_keys = _load_text_values(exclude_keys_path)
    rotation_state = _load_rotation_state(rotation_path)
    magazine, article, magazine_index = _select_magazine_and_article(
        magazines,
        rotation_state,
        articles,
        history,
        exclude_urls,
        exclude_keys,
        history_days,
    )

    if not magazine:
        payload = _build_result_payload(
            generated_at,
            "skipped",
            module_name="post_x_magazine",
            reason="No eligible magazine/article pair found.",
        )
        _dump_json(output_path, payload)
        logging.info("[post_x_magazine] skipped: no eligible pair")
        return

    thread_parts = _build_thread_parts(article, magazine, gemini_api_key, model)
    thread_parts = [
        _format_multiline_tweet(part, max_lines=5)[:280].rstrip()
        for part in thread_parts
        if str(part).strip()
    ]
    thread_parts = _attach_hashtags_to_last_part(thread_parts, article, magazine)
    if force_overflow_test and thread_parts:
        thread_parts[0] = f"{thread_parts[0]}\n{'X' * 400}"
    too_long_parts = [part for part in thread_parts if len(part) > X_MAX_CHARS]
    if too_long_parts:
        failed_part_index = next((i + 1 for i, part in enumerate(thread_parts) if len(part) > X_MAX_CHARS), None)
        reason = f"Tweet text exceeded {X_MAX_CHARS} chars before posting."
        payload = _build_result_payload(
            generated_at,
            "error",
            module_name="post_x_magazine",
            reason=reason,
            article=article,
            tweet_text="\n---THREAD---\n".join(thread_parts),
            tweet_parts=thread_parts,
        )
        payload["magazine"] = magazine
        _dump_json(output_path, payload)
        try:
            _send_post_notification(
                root,
                generated_at,
                magazine,
                article,
                thread_parts,
                status="error",
                reason=reason,
                failed_part_index=failed_part_index,
                total_parts=len(thread_parts),
            )
        except Exception as mail_exc:
            logging.warning("[post_x_magazine] failure mail skipped: %s", mail_exc)
        logging.error("[post_x_magazine] post failed: %s", reason)
        return
    if not thread_parts:
        payload = _build_result_payload(
            generated_at,
            "error",
            module_name="post_x_magazine",
            reason="No tweet parts could be generated.",
        )
        _dump_json(output_path, payload)
        try:
            _send_post_notification(
                root,
                generated_at,
                magazine,
                article,
                [],
                status="error",
                reason="No tweet parts could be generated.",
                failed_part_index=None,
                total_parts=0,
            )
        except Exception as mail_exc:
            logging.warning("[post_x_magazine] failure mail skipped: %s", mail_exc)
        logging.error("[post_x_magazine] post failed: no tweet parts generated")
        return

    if preview_mode or notify_only_mode:
        payload = _build_result_payload(
            generated_at,
            "notify_only" if notify_only_mode else "preview",
            module_name="post_x_magazine",
            reason=(
                "Notify-only mode; magazine selection and thread text generation completed without posting."
                if notify_only_mode
                else "Preview mode; magazine selection and thread text generation completed without posting."
            ),
            article=article,
            tweet_text="\n---THREAD---\n".join(thread_parts),
            tweet_parts=thread_parts,
            preview=preview_mode,
            warnings=None,
        )
        payload["magazine"] = magazine
        _dump_json(output_path, payload)
        try:
            _send_post_notification(
                root,
                generated_at,
                magazine,
                article,
                thread_parts,
                status="notify_only" if notify_only_mode else "preview",
                reason=None,
                failed_part_index=None,
                total_parts=len(thread_parts),
            )
        except Exception as mail_exc:
            logging.warning("[post_x_magazine] notification mail skipped: %s", mail_exc)
        logging.info(
            "[post_x_magazine] %s completed: %s / %s",
            "notify-only" if notify_only_mode else "preview",
            magazine.get("id"),
            article.get("title") if article else "-",
        )
        return

    fallback_parts = _build_thread_fallback_parts(article, magazine)

    post_image_path = _pick_post_image(root)
    media_id: str | None = None
    if post_image_path:
        try:
            media_id = _upload_media_to_x(post_image_path)
            logging.info("[post_x_magazine] attached image: %s", post_image_path.name)
        except Exception as exc:
            logging.warning("[post_x_magazine] image upload failed, posting without image: %s", exc)
            media_id = None

    posted_parts: list[str] = []
    posted_sources: list[str] = []
    try:
        post_result = None
        reply_results: list[dict] = []
        parent_tweet_id = ""
        candidate_groups = [("gemini", thread_parts), ("fallback", fallback_parts)]
        for index in range(THREAD_PARTS_COUNT):
            if index == 0:
                candidates = [
                    (label, group[0])
                    for label, group in candidate_groups
                    if len(group) > 0 and str(group[0]).strip()
                ]
            else:
                candidates = [
                    (label, group[index])
                    for label, group in candidate_groups
                    if len(group) > index and str(group[index]).strip()
                ]
            if not candidates:
                candidates = [("fallback", fallback_parts[index])]
            last_error: Exception | None = None
            for label, candidate_text in candidates:
                try:
                    if index == 0:
                        post_result = _post_to_x(candidate_text, media_ids=[media_id] if media_id else None)
                        tweet_id = str(post_result.get("data", {}).get("id") or "").strip()
                        if not tweet_id:
                            raise RuntimeError("X API returned no tweet id for the primary post.")
                        parent_tweet_id = tweet_id
                    else:
                        time.sleep(2)
                        reply_result = _post_to_x(candidate_text, reply_to_tweet_id=parent_tweet_id)
                        reply_results.append(reply_result)
                        parent_tweet_id = str(reply_result.get("data", {}).get("id") or "").strip() or parent_tweet_id
                    posted_parts.append(candidate_text)
                    posted_sources.append(label)
                    break
                except Exception as exc:
                    last_error = exc
                    logging.warning(
                        "[post_x_magazine] post %d (%s) failed, trying next candidate: %s",
                        index + 1,
                        label,
                        exc,
                    )
                    continue
            else:
                raise RuntimeError(f"Failed to post thread part {index + 1}: {last_error}") from last_error
        logging.info("[post_x_magazine] posted sources: %s", posted_sources)
    except Exception as exc:
        failed_part_index = None
        match = re.search(r"part (\d+)", str(exc))
        if match:
            failed_part_index = int(match.group(1))
        payload = _build_result_payload(
            generated_at,
            "error",
            module_name="post_x_magazine",
            reason=str(exc),
            article=article,
            tweet_text="\n---THREAD---\n".join(posted_parts or thread_parts),
            tweet_parts=posted_parts or thread_parts,
        )
        payload["magazine"] = magazine
        _dump_json(output_path, payload)
        try:
            _send_post_notification(
                root,
                generated_at,
                magazine,
                article,
                posted_parts or thread_parts,
                status="error",
                reason=str(exc),
                failed_part_index=failed_part_index,
                total_parts=len(thread_parts),
            )
        except Exception as mail_exc:
            logging.warning("[post_x_magazine] failure mail skipped: %s", mail_exc)
        logging.error("[post_x_magazine] post failed: %s", exc)
        return

    history.append(
        {
            "magazine_id": magazine.get("id"),
            "magazine_name": magazine.get("name"),
            "tweet_id": str(post_result.get("data", {}).get("id") or "").strip(),
            "article_key": article.get("key") if article else "",
            "article_url": article.get("url") if article else "",
            "posted_at": generated_at,
        }
    )
    _save_history(history_path, history[-200:])
    if post_image_path and media_id:
        try:
            _mark_image_as_posted(post_image_path)
        except Exception as exc:
            logging.warning("[post_x_magazine] failed to move used image to posted/: %s", exc)
    _save_rotation_state(
        rotation_path,
        {
            "last_index": magazine_index,
            "last_magazine_id": magazine.get("id"),
            "updated_at": generated_at,
        },
    )

    payload = _build_result_payload(
        generated_at,
        "ok",
        module_name="post_x_magazine",
        article=article,
        tweet_text="\n---THREAD---\n".join(posted_parts),
        tweet_parts=posted_parts,
        post_result={"primary": post_result, "replies": reply_results},
    )
    payload["magazine"] = magazine
    _dump_json(output_path, payload)
    try:
        _send_post_notification(
            root,
            generated_at,
            magazine,
            article,
            posted_parts,
            status="ok",
            reason=None,
            failed_part_index=None,
            total_parts=len(posted_parts),
        )
    except Exception as mail_exc:
        logging.warning("[post_x_magazine] success mail skipped: %s", mail_exc)
    logging.info(
        "[post_x_magazine] posted standalone tweet for magazine=%s article=%s",
        magazine.get("id"),
        article.get("title") if article else "-",
    )


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)

