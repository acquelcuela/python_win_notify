"""Text generation through either the Gemini API or Claude (claude -p).

Each module's config section picks the provider (2026-10-06):

    "provider": "gemini" | "claude"      (default "gemini")
    "model": "gemini-3.1-flash-lite"      Gemini model (also the fallback model)
    "claude_model": "sonnet"              optional; claude CLI default if omitted
    "fallback_to_gemini": true            if Claude fails and GEMINI_API_KEY is set

Claude runs as `claude -p` on the Claude Pro subscription, so it costs no API
fees but uses the subscription's quota and is slower (roughly 15-40 s per
call vs a few seconds for Gemini). Tools are disabled (`--tools ""`): these
calls are plain prompt-in, text-out.
"""
from __future__ import annotations

import html
import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from modules.grok_web import _find_claude_binary, _kill_tree

DEFAULT_PROVIDER = "gemini"
API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
CLAUDE_TIMEOUT_SECONDS = 180


@dataclass
class LLMResult:
    text: str
    provider: str  # "gemini" or "claude" - the one that actually answered
    model: str
    gemini_usage: dict = field(default_factory=dict)  # for GeminiUsageTracker; {} for Claude
    fallback_reason: str | None = None  # set when Claude failed and Gemini answered instead


def _call_gemini(api_key: str, model: str, prompt: str) -> tuple[str, dict]:
    """Returns (text, usage_metadata); see modules/gemini_pricing.py."""
    url = API_URL_TEMPLATE.format(model=model)
    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ]
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini API HTTP {exc.code}: {detail}") from exc

    candidates = result.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini API returned no candidates.")
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(str(part.get("text", "")) for part in parts).strip()
    if not text:
        raise RuntimeError("Gemini API returned empty text.")
    return text, (result.get("usageMetadata") or {})


def send_failure_mail(title: str, error: str, settings: dict) -> None:
    """Mails "【失敗】<title>" with the error - used by modules whose only output
    is otherwise silent on failure (no report section to show it in)."""
    from modules.mail_gmail import send_html_mail

    gmail_address = os.getenv("GMAIL_ADDRESS", "").strip()
    app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    mail_to = os.getenv("MAIL_TO", "").strip()
    if not (gmail_address and app_password and mail_to):
        logging.warning("[llm_client] failure mail skipped: missing Gmail settings")
        return
    provider = "Claude(Claude Pro)" if provider_of(settings) == "claude" else "Gemini API"
    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>【失敗】{html.escape(title)}</h2>
        <div>{provider} での文章生成に失敗したため、今回の結果はありません。</div>
        <pre style="white-space:pre-wrap;background:#fef2f2;border:1px solid #fca5a5;padding:8px;border-radius:6px;">{html.escape(error[:2000])}</pre>
      </body>
    </html>
    """
    try:
        send_html_mail(gmail_address, app_password, mail_to, f"【失敗】{title}", body)
    except Exception as exc:
        logging.error("[llm_client] failure mail send failed: %s", exc)


def provider_of(settings: dict) -> str:
    provider = str(settings.get("provider") or DEFAULT_PROVIDER).strip().lower()
    return provider if provider in ("gemini", "claude") else DEFAULT_PROVIDER


def can_run(settings: dict) -> bool:
    """False when the configured provider can't be used at all (Gemini without an API key)."""
    return provider_of(settings) == "claude" or bool(os.getenv("GEMINI_API_KEY"))


def _call_claude(prompt: str, model: str | None, cwd: Path | None) -> str:
    claude_bin = _find_claude_binary()
    if not claude_bin:
        raise RuntimeError("claude CLI was not found on PATH.")
    cmd = [claude_bin, "-p", "--output-format", "json", "--no-session-persistence", "--tools", ""]
    if model:
        cmd += ["--model", model]
    # Prompt via stdin: claude is claude.cmd on Windows and cmd.exe cuts
    # batch-file arguments at the first newline (see grok_web).
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=str(cwd) if cwd else None,
    )
    try:
        stdout, stderr = proc.communicate(prompt, timeout=CLAUDE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.communicate()
        raise RuntimeError(f"claude CLI timed out after {CLAUDE_TIMEOUT_SECONDS}s.")
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI exited {proc.returncode}: {(stderr or stdout)[:500]}")
    envelope = json.loads(stdout)
    text = envelope.get("result")
    if envelope.get("is_error") or not isinstance(text, str) or not text.strip():
        raise RuntimeError(f"claude CLI returned no text: {str(text)[:500]}")
    return text.strip()


def generate(prompt: str, settings: dict, default_gemini_model: str, cwd: Path | None = None) -> LLMResult:
    """Generates text with the provider in `settings` (a module's config section)."""
    gemini_model = str(settings.get("model") or default_gemini_model)
    fallback_reason = None
    if provider_of(settings) == "claude":
        claude_model = settings.get("claude_model") or None
        try:
            return LLMResult(_call_claude(prompt, claude_model, cwd), "claude", claude_model or "claude (CLI default)")
        except Exception as exc:
            fallback_reason = str(exc)
            if not (settings.get("fallback_to_gemini", True) and os.getenv("GEMINI_API_KEY")):
                raise
            logging.warning("[llm_client] Claude failed, falling back to Gemini: %s", exc)
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set.")
    text, usage = _call_gemini(api_key=api_key, model=gemini_model, prompt=prompt)
    return LLMResult(text, "gemini", gemini_model, usage or {}, fallback_reason)
