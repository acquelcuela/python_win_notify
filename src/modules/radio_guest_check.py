"""radiko / NHKラジオ(らじる★らじる) / 音泉 のゲスト出演情報を Grok で調べ、
テキストにまとめてメールする(inbox/archive/cowork_radio_guest_check.md の手順を
バッチ化したもの、2026-10-04)。

Cowork版からの変更点:
  - 対象期間は「実行日から5日分」(本日〜4日後)。Cowork版は10日後まで
  - 3日に1回(前回成功から interval_days 日以上)、11:00 に実行
  - Grok は grok_web(grok.com、毎回新しい会話)経由。「同じスレッドで言い換え
    質問」はできないため、言い換え検索は元の質問+別の情報源を指示した
    独立した質問として送る
  - マージ(radiko Stage2 の2セット統合・Stage1との重複除去・整形)は最後に
    claude -p を1回呼んで行う。失敗時は取れた内容を機械的に並べる
  - 結果はメールのみ(テキストファイルは保存しない)。Grok が全部失敗した
    場合も、失敗したことが分かるようにメールする

Grok への質問は最大8問(優先順: radiko Stage1 本体・言い換え → NHK → 音泉 →
radiko Stage2 セットA 本体・言い換え → セットB 本体・言い換え)。Grok の
上限に達したらそこで打ち切り、それまでの結果でまとめる。

    python -m modules.radio_guest_check            # バッチと同じ判定
    python -m modules.radio_guest_check --force    # 間隔チェックをスキップ
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from modules.grok_web import _find_claude_binary, _kill_tree, ask_grok
from modules.local_config import load_config
from modules.mail_gmail import send_html_mail


JST = timezone(timedelta(hours=9), "JST")
STATE_PATH = Path("state") / "radio_guest_check_state.json"
DEFAULT_INTERVAL_DAYS = 3
DEFAULT_DAYS_AHEAD = 4
MERGE_TIMEOUT_SECONDS = 300
WEEKDAYS_JP = "月火水木金土日"

ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "program": {"type": "string", "description": "番組名"},
                    "station": {"type": "string", "description": "放送局(NHKはNHK第1/NHK-FM等、音泉は音泉)"},
                    "date": {"type": "string", "description": "放送日 YYYY-MM-DD。不明なら空文字"},
                    "time": {"type": "string", "description": "放送時間(例 21:45、25:00)。不明なら空文字"},
                    "guest": {"type": "string", "description": "ゲスト名"},
                    "url": {"type": "string", "description": "実際に聴けるURL。不明なら空文字"},
                },
                "required": ["program", "guest"],
            },
        }
    },
    "required": ["entries"],
}

# Claude (not Grok) reads these - rules 4 of the Cowork instructions.
EXTRACT_INSTRUCTIONS = (
    "【data の作り方】Grokの回答から、ゲスト出演が明記されているものだけを entries に入れてください。"
    "「ゲスト情報なし」「該当なし」の番組は入れないこと。1つの番組で放送日が複数ある場合は放送日ごとに分けること。"
    "放送日は回答に書かれた日付をそのまま使い(本日に決め打ちしない)、不明なら空文字にすること。\n"
    "url は「実際にラジオが聴けるURL」を優先: radikoはライブ(https://radiko.jp/#!/live/局ID)/"
    "タイムフリー(https://radiko.jp/#!/ts/局ID/YYYYMMDDHHMMSS)、NHKは"
    "https://www.nhk.or.jp/radio/player/?ch=r1|r2|fm や聴き逃しURL、音泉は onsen.ag の番組ページ。"
    "局サイトの番組紹介記事やnhk.jpの番組情報ページは採用しない。回答本文にサイト名しか出ていない場合は、"
    "read_page や find でリンク要素の href を確認して実URLを使うこと。聴けるURLが取れなければ"
    "サービスのトップ(https://radiko.jp/ 、https://www.nhk.or.jp/radio/ 、https://www.onsen.ag/ )、"
    "それも無理なら空文字。\n"
    "回答は get_page_text で全文を取得し、スクロールして続きが無いか確認してから読み取ること。"
)


def _load_settings(root: Path) -> dict:
    # followed_programs is a personal list, kept in config.local.json (gitignored).
    section = load_config(root).get("radio_guest_check") or {}
    return {
        "interval_days": int(section.get("interval_days", DEFAULT_INTERVAL_DAYS)),
        "days_ahead": int(section.get("days_ahead", DEFAULT_DAYS_AHEAD)),
        "followed_programs": [str(p) for p in section.get("followed_programs") or [] if str(p).strip()],
    }


def _load_state(root: Path) -> dict:
    try:
        data = json.loads((root / STATE_PATH).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(root: Path, state: dict) -> None:
    path = root / STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _jp_date(d: date) -> str:
    return f"{d.year}年{d.month}月{d.day}日"


def _queries(start: date, end: date, programs: list[str]) -> list[dict]:
    period = f"本日({_jp_date(start)})から{(end - start).days}日後({_jp_date(end)})まで"
    radiko_url = (
        "radikoで実際に聴けるURL(ライブURL: https://radiko.jp/#!/live/[放送局ID]、またはタイムフリーURL: "
        "https://radiko.jp/#!/ts/[放送局ID]/[YYYYMMDDHHMMSS])"
    )
    program_list = "\n".join(f"- {p}" for p in programs)
    period_only = (
        f"※放送日が{_jp_date(start)}〜{_jp_date(end)}の回だけを答えてください。{_jp_date(start)}より前に"
        "放送済みの回(先週・先月の回など)は、ゲスト情報があっても含めないでください。"
        "期間内の回のゲストが未発表なら『ゲスト情報なし』としてください。"
    )
    stage1 = (
        f"以下のradiko番組について、{period}の間にゲスト出演の発表があるか、番組ごとに教えてください。"
        f"ゲストがいる場合は放送日・放送時間・ゲスト名・{radiko_url}を、いない場合は『ゲスト情報なし』とだけ、"
        f"番組ごとに教えてください。\n{period_only}\n{program_list}"
    )
    stage2 = (
        f"radiko {period}の番組表・告知で、ゲスト出演が発表されている番組を教えて。それぞれ放送日・放送時間を"
        f"明記した上で、{radiko_url}も分かれば教えて。番組の紹介記事URLではなく再生用URLが欲しい"
    )
    stage2_para = (
        f"radikoで{period}に放送される番組について、各局公式X(Twitter)やアーティスト本人・事務所の投稿など、"
        f"番組表とは別の情報源から分かるゲスト出演告知を教えて。それぞれ放送日・放送時間・ゲスト名と、"
        f"{radiko_url}も分かれば教えて"
    )
    queries = []
    if programs:
        queries += [
            {"key": "stage1", "label": "radiko フォロー中番組", "question": stage1},
            {
                "key": "stage1",
                "label": "radiko フォロー中番組(言い換え)",
                # The 2026-10-04 test run: phrased as "check official X posts",
                # Grok answered with mostly already-aired episodes (8 of 9
                # before the period), so the period is pinned down harder here.
                "question": (
                    f"以下のradiko番組について、各番組公式X(Twitter)やパーソナリティ・ゲスト本人・事務所の投稿も"
                    f"確認して、{period}に放送される回(これから放送される回)のゲスト出演の予告・告知を"
                    f"教えてください。番組表だけでは分からない情報も含めて、放送日・放送時間・ゲスト名・{radiko_url}を"
                    f"番組ごとに。\n{period_only}\n{program_list}"
                ),
            },
        ]
    queries += [
        {
            "key": "nhk",
            "label": "NHKラジオ",
            "question": (
                f"NHKラジオ(らじる★らじるで配信されているNHK第1・NHK第2・NHK-FM)の{period}の音楽番組・"
                "バラエティ番組で、ゲスト出演が発表されている番組を教えて。それぞれ放送日・放送時間を明記した上で、"
                "らじる★らじるで実際に聴けるURL(ライブURL: https://www.nhk.or.jp/radio/player/?ch=r1(第1)/"
                "?ch=r2(第2)/?ch=fm(FM)、または聴き逃しURL)も分かれば教えて。nhk.jpの番組紹介ページではなく"
                "再生用URLが欲しい。\n特にNHK-FM「ミュージックライン」(月〜金 21:45〜の本放送と、翌朝10:15〜の"
                "前日分の再放送があり、ゲストが異なる)は、期間内の各日について本放送・再放送それぞれのゲストを"
                "教えて(公式番組ページ https://www.nhk.jp/p/ml/rs/Z9WGYY3GP5/ の放送予定も確認して)。"
                "NHKラジオ第1『らじらー！サンデー』のような定期特番のゲストも確認して。"
            ),
        },
        {
            "key": "onsen",
            "label": "音泉",
            "question": (
                f"音泉(インターネットラジオ)の{period}に配信される番組で、ゲスト出演が発表されている番組を教えて。"
                "それぞれ配信日を明記した上で、その番組が聴けるonsen.agの番組ページURLも分かれば教えて"
            ),
        },
    ]
    for set_name in ("A", "B"):
        queries += [
            {"key": "stage2", "label": f"radiko リスト外 セット{set_name}", "question": stage2},
            {"key": "stage2", "label": f"radiko リスト外 セット{set_name}(言い換え)", "question": stage2_para},
        ]
    return queries


def _run_queries(root: Path, queries: list[dict]) -> list[dict]:
    # ask_grok always writes its result file; only the latest one is kept,
    # for debugging - the mail is the actual output.
    scratch_path = root / "output" / "radio_guest_check_grok_last.json"
    results = []
    for query in queries:
        result = ask_grok(
            query["question"],
            scratch_path,
            schema=ENTRY_SCHEMA,
            caller="radio_guest_check",
            extra_instructions=EXTRACT_INSTRUCTIONS,
            cwd=root,
        )
        entries = ((result.get("data") or {}).get("entries") or []) if result["status"] == "ok" else []
        results.append({**query, "status": result["status"], "error": result.get("error"), "entries": entries})
        logging.info("[radio_guest_check] %s: %s (%d entries)", query["label"], result["status"], len(entries))
        if result["status"] == "skipped" or result.get("rate_limited"):
            logging.warning("[radio_guest_check] Grok limit reached; stopping after %s", query["label"])
            break
    return results


def _merge_prompt(results: list[dict], start: date, end: date) -> str:
    grouped = {"stage1": [], "stage2": [], "nhk": [], "onsen": []}
    for r in results:
        grouped[r["key"]].append({"query": r["label"], "entries": r["entries"]})
    return (
        "以下は、ラジオ番組のゲスト出演情報を Grok で調べた結果(JSON)です。これを1つのテキストにまとめてください。"
        f"対象期間は{_jp_date(start)}〜{_jp_date(end)}です。\n\n"
        "まとめ方:\n"
        "- 見出しは次の順: 【radiko】の下に「■ フォロー中番組」(stage1)と「■ リスト外」(stage2)、"
        "続いて【NHKラジオ】(nhk)、【音泉】(onsen)\n"
        "- stage2 はセットA/Bと言い換えの結果を統合する。番組名+放送日が同じものは1つにまとめ、詳細が食い違えば"
        "より具体的な方を採用(判断が難しければ併記)。片方にしか無い番組もそのまま採用\n"
        "- stage1 と stage2 に同じ番組+放送日があれば「フォロー中番組」側にだけ載せる(stage2 側の詳細は補ってよい)\n"
        "- 1件ごとに、先頭行「M/D(曜) 番組名／放送局 放送時間」、続けて「ゲスト: ○○」「URL: ○○」(URL不明なら"
        "「URL: 不明」)。空行で区切る。放送日が複数あれば日ごとに分ける。放送日不明は「日付不明」とする\n"
        "- 各見出しの中は放送日の早い順\n"
        "- 対象期間外の放送日のものは除く\n"
        "- 0件の見出しは見出しごと省く。「該当なし」「ゲスト情報なし」などは書かない。情報源の注記も書かない\n"
        "- ゲスト出演が1件も無ければ「今回の対象期間にゲスト出演情報は見つかりませんでした」の1行だけ\n"
        "- 前置き・後書き・Markdown記法(**など)は不要。まとめたテキストだけを出力する\n\n"
        f"{json.dumps(grouped, ensure_ascii=False, indent=1)}"
    )


def _merge_with_claude(prompt: str, root: Path) -> str | None:
    claude_bin = _find_claude_binary()
    if not claude_bin:
        return None
    try:
        proc = subprocess.Popen(
            [claude_bin, "-p", "--output-format", "json", "--no-session-persistence"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", cwd=str(root),
        )
        stdout, _ = proc.communicate(prompt, timeout=MERGE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.communicate()
        logging.error("[radio_guest_check] merge timed out")
        return None
    except Exception as exc:
        logging.error("[radio_guest_check] merge failed: %s", exc)
        return None
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    text = envelope.get("result")
    return text.strip() if isinstance(text, str) and not envelope.get("is_error") and text.strip() else None


def _fallback_text(results: list[dict]) -> str:
    sections = [("stage1", "【radiko】フォロー中番組"), ("stage2", "【radiko】リスト外"), ("nhk", "【NHKラジオ】"), ("onsen", "【音泉】")]
    blocks = []
    # Same rule as the Claude merge: a followed program found again in the
    # wide search is listed only under フォロー中番組.
    followed = {(e.get("program"), e.get("date")) for r in results if r["key"] == "stage1" for e in r["entries"]}
    for key, heading in sections:
        seen = set(followed) if key == "stage2" else set()
        lines = []
        entries = [e for r in results if r["key"] == key for e in r["entries"]]
        for e in sorted(entries, key=lambda e: e.get("date") or "9999"):
            ident = (e.get("program"), e.get("date"))
            if ident in seen:
                continue
            seen.add(ident)
            try:
                d = date.fromisoformat(e.get("date") or "")
                day = f"{d.month}/{d.day}({WEEKDAYS_JP[d.weekday()]})"
            except ValueError:
                day = "日付不明"
            head = " ".join(x for x in [day, "／".join(x for x in [e.get("program", ""), e.get("station", "")] if x), e.get("time", "")] if x)
            lines.append(f"{head}\nゲスト: {e.get('guest', '')}\nURL: {e.get('url') or '不明'}")
        if lines:
            blocks.append(heading + "\n\n" + "\n\n".join(lines))
    return "\n\n".join(blocks) or "今回の対象期間にゲスト出演情報は見つかりませんでした"


def _send_mail(text: str | None, results: list[dict], merged_by_claude: bool, start: date, end: date, now: datetime) -> None:
    gmail_address = os.getenv("GMAIL_ADDRESS", "").strip()
    app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    mail_to = os.getenv("MAIL_TO", "").strip()
    if not (gmail_address and app_password and mail_to):
        logging.warning("[radio_guest_check] mail skipped: missing Gmail settings")
        return
    ok_count = sum(1 for r in results if r["status"] == "ok")
    if text is None:
        summary_html = (
            '<div style="padding:8px 10px;background:#fef2f2;border:1px solid #fca5a5;border-radius:6px;'
            'color:#b91c1c;font-weight:bold;">❌ Grokでの検索がすべて失敗しました。下の検索結果のエラーを確認してください'
            '(Chromeが起動していない・grok.comからログアウトしている・Grokの利用上限など)。</div>'
        )
    elif ok_count < len(results):
        summary_html = (
            f'<div style="color:#b45309;font-size:13px;">⚠ 一部の検索が失敗しました({ok_count}/{len(results)}件成功)。'
            '成功した分だけでまとめています。</div>'
        )
    else:
        summary_html = ""
    if text is not None and not merged_by_claude:
        summary_html += '<div style="color:#b45309;font-size:13px;">⚠ Claudeでのまとめに失敗したため、取得結果をそのまま並べています。</div>'
    text_html = (
        f'<pre style="white-space:pre-wrap;font-family:inherit;font-size:14px;margin-top:10px;">{html.escape(text)}</pre>'
        if text is not None
        else ""
    )
    status_rows = "".join(
        f'<li>{"✅" if r["status"] == "ok" else "❌"} {html.escape(r["label"])}: '
        + (f'{len(r["entries"])}件' if r["status"] == "ok" else html.escape(f'{r["status"]} - {r.get("error") or ""}'[:300]))
        + "</li>"
        for r in results
    )
    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>ラジオ ゲスト情報</h2>
        <div style="color:#6b7280;font-size:12px;">対象: {start.month}/{start.day}〜{end.month}/{end.day} / {now.strftime('%Y-%m-%d %H:%M')} JST 実行</div>
        {summary_html}
        {text_html}
        <div style="margin-top:16px;color:#6b7280;font-size:12px;">検索結果(Grok {ok_count}/{len(results)}件成功)<ul>{status_rows}</ul></div>
      </body>
    </html>
    """
    prefix = "【失敗】" if text is None else ("【一部失敗】" if ok_count < len(results) else "")
    subject = f"{prefix}ラジオ ゲスト情報 {start.month}/{start.day}〜{end.month}/{end.day}"
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[radio_guest_check] sent mail")
    except Exception as exc:
        logging.error("[radio_guest_check] mail send failed: %s", exc)


def run(root: Path, force: bool = False) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "radio_guest_check.json"
    now = datetime.now(JST)
    today = now.date()
    settings = _load_settings(root)
    state = _load_state(root)

    def _write(status: str, **extra) -> None:
        payload = {"module": "radio_guest_check", "generated_at": now.isoformat(), "status": status, **extra}
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    last_run = state.get("last_success_date")
    if not force and last_run:
        try:
            days_since = (today - date.fromisoformat(last_run)).days
        except ValueError:
            days_since = settings["interval_days"]
        if days_since < settings["interval_days"]:
            _write("skipped", reason=f"Last run {last_run} ({days_since} days ago, interval {settings['interval_days']}).")
            logging.info("[radio_guest_check] skipped: last run %s", last_run)
            return

    start, end = today, today + timedelta(days=settings["days_ahead"])
    results = _run_queries(root, _queries(start, end, settings["followed_programs"]))
    if not any(r["status"] == "ok" for r in results):
        # Leave last_success_date alone so tomorrow's 11:00 tries again -
        # but still mail, so the failure itself is visible.
        _write("error", error="No Grok query succeeded.", results=results)
        logging.error("[radio_guest_check] no Grok query succeeded")
        _send_mail(None, results, False, start, end, now)
        return

    merged = _merge_with_claude(_merge_prompt(results, start, end), root)
    text = merged or _fallback_text(results)
    _save_state(root, {"last_success_date": today.isoformat()})
    _write("ok", period=[start.isoformat(), end.isoformat()], text=text,
           merged_by_claude=merged is not None, results=results)
    _send_mail(text, results, merged is not None, start, end, now)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check radio guest appearances via Grok.")
    parser.add_argument("--force", action="store_true", help="skip the every-N-days check")
    args = parser.parse_args()
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(root, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
