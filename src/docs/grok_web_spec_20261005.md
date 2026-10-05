# Grok(Web版)連携 仕様書

最終更新: 2026-10-05

grok.com(Web版 Grok)を、このPCの Chrome 経由で Claude に操作させて質問し、
結果を JSON で受け取る仕組みと、それを使っているバッチの現状仕様をまとめたもの。

---

## 1. 全体の仕組み

```
バッチ(main.py / 各モジュール)
  └─ modules/grok_web.py  ask_grok("質問", 保存先)
       ├─ 利用上限チェック(超えていれば Chrome を開かず status="skipped")
       ├─ claude -p --chrome を起動(指示文は標準入力で渡す)
       │    └─ Claude が Claude in Chrome 拡張で Chrome を操作
       │         grok.com を新しいタブで開く → 新しい会話で質問 → 回答を読む → タブを閉じる
       ├─ Claude が結果を JSON Schema どおりに出力(--json-schema)
       └─ 結果を保存先に JSON で書き出し、呼び出し回数を記録
```

- Grok の **API は使わない**(API 課金なし)。代わりに Claude Pro の利用枠を消費する。
  目安は 1 回あたり API 換算で $0.4〜0.6 程度の処理量(実際の請求はなし)。
- 1 回の質問にかかる時間は、短い質問で約 45〜60 秒、X 株トレンドの長い質問で約 2〜4 分。

---

## 2. 動かすための前提条件

| 条件 | 内容 |
|---|---|
| Chrome | 起動していること。Claude in Chrome 拡張がログイン・接続済みであること |
| grok.com | 普段の Chrome プロファイルでログイン済みであること |
| タスクスケジューラ | 「ユーザーがログオンしているときのみ実行」(`InteractiveToken`)。別セッションで動くと Chrome に届かない |
| PC | 実行時刻にスリープしていないこと(ロック中は可) |
| Claude CLI | `claude` が PATH にあり、claude.ai(Pro)でログイン済みであること |

### 本人の操作が必要な画面
grok.com では次のような画面がときどき出る。**Claude は代わりに操作せず**、
`error` に状況を書いて返す(その回は失敗扱い)。本人が Chrome で一度済ませれば解消する。

- 利用規約・許容使用ポリシーの更新への同意(`grok.com/tos-gate`)
- 年齢確認(生まれ年の入力)
- ログイン切れ

---

## 3. modules/grok_web.py

### 3.1 Python から使う

```python
from modules.grok_web import ask_grok, get_usage_status

result = ask_grok(
    "質問文",                      # 改行を含んでよい
    root / "output" / "xxx.json",  # 結果の保存先(必ず書き出される)
    schema={...},                  # 任意: 回答をこの JSON Schema の形に整えて data に入れる
    timeout_seconds=600,           # 任意: 既定 600 秒
    cwd=root,                      # 任意: claude を起動するフォルダ
    caller="my_batch",             # 任意: 利用記録に残す呼び出し元の名前
    enforce_limits=True,           # 任意: False で利用上限チェックをしない
    extra_instructions="...",      # 任意: Claude への追加指示(Grok には送らない)
)
```

### 3.2 コマンドラインから使う

```
python -m modules.grok_web "質問" --out output/xxx.json [--schema schema.json] [--timeout 600] [--caller 名前]
python -m modules.grok_web --usage     # 現在の利用状況を表示
```
終了コードは成功で 0、それ以外で 1。

### 3.3 結果 JSON

| キー | 内容 |
|---|---|
| `status` | `ok` / `error` / `skipped`(利用上限で呼ばなかった) |
| `question` | 送った質問 |
| `logged_in` | grok.com にログインできていたか |
| `answer_text` | Grok の回答本文 |
| `data` | `schema` を指定したとき、回答を整形したもの |
| `error` | 失敗・skipped の理由(未ログイン、年齢確認、時間切れ、上限など) |
| `rate_limited` | Grok 自身が「上限に達しました」等を出したとき true |
| `elapsed_seconds` | かかった秒数 |
| `claude` | `num_turns` / `total_cost_usd`(API 換算の目安) / `session_id` |
| `caller` | 呼び出し元 |
| `usage` | 実行後の利用状況(`get_usage_status()` と同じ内容) |

### 3.4 実装上の注意(変えないこと)

- **指示文は標準入力で渡す。** Windows では `claude` が `claude.cmd` なので、引数で渡すと
  cmd.exe が最初の改行で切ってしまい、`--chrome` などそれ以降のオプションも消える。
- **権限は Chrome の操作だけ許可する。** `--allowedTools mcp__claude-in-chrome`。
  `--dangerously-skip-permissions` は使わない。
- **時間切れのときはプロセスをまとめて終了する。** `taskkill /F /T` で、cmd.exe の下の node まで終了させる。
- **結果は `--output-format json` の `structured_output` から読む。**

---

## 4. 利用上限(無料版 Grok の枠を超えないための制御)

無料版 Grok のテキストチャットは「2時間ごとにおよそ10〜20回、時間経過で順次回復」
(ローリング方式)。これを超えないよう、全バッチ共通で回数を管理する。

### 4.1 設定(config.json)

```json
"grok_web": {
  "window_limit": 10,
  "window_hours": 2,
  "daily_limit": null
}
```

| 項目 | 意味 |
|---|---|
| `window_limit` / `window_hours` | 直近 `window_hours` 時間で `window_limit` 回まで(既定 2時間に10回) |
| `daily_limit` | 1日(日本時間)の上限。`null` なら上限なし |

### 4.2 動き

- 上限に達していると、Chrome を開かずに `status: "skipped"` を返す。
  `error` には次に呼べる時刻(`next_available_at`)も入る。
- Grok 自身が上限のメッセージを出した場合(`rate_limited`)は、そこから `window_hours`(2時間)の間、
  **全バッチの呼び出しを止める**(`rate_limited_until`)。
- 回数は `claude` が起動した時点で 1 回と数える。途中失敗・時間切れも 1 回(Grok が答えている可能性があるため)。
  `claude` の起動自体に失敗した場合は数えない。
- 本人が手で Grok を使った分は数えられない(本人は使わない前提で 10 回にしている)。
- 同時実行の排他制御はない。Grok を使うバッチは時間帯をずらすこと。

### 4.3 記録ファイル

`state/grok_web_usage.json`(直近30日分を保持)

```json
{
  "days": {
    "2026-10-05": [
      {"started_at": "...", "caller": "stock_x_trends_web_fetch", "question": "先頭200文字",
       "status": "ok", "elapsed_seconds": 224.1}
    ]
  },
  "rate_limited_until": "..."
}
```

### 4.4 関数

| 関数 | 内容 |
|---|---|
| `get_usage_status()` | 上限に対する現在の状況。`can_call` / `reason` / `next_available_at` / `window_calls` / `today_calls` など |
| `get_window_call_count(hours=None)` | 直近の呼び出し回数 |
| `get_today_calls()` / `get_today_call_count()` | 今日の呼び出し一覧 / 回数 |
| `get_limits()` | config.json の設定(未設定の項目は既定値) |

---

## 5. Grok / Chrome を使っているバッチ

| モジュール | 時刻 | 曜日 | Grok 回数 | 内容 |
|---|---|---|---|---|
| `stock_x_trends_web_fetch` | 06:30, 22:30 | 平日 | 各1回 | X の株トレンド検索(6章) |
| `radio_guest_check` | 13:00 | 毎日判定・3日に1回実行 | 最大8回 | ラジオのゲスト出演情報(`radio_guest_check_spec_20261004.txt`) |
| `note_draft_post` | 23:30 | 毎日 | 0回 | Grok は使わない。同じ仕組み(`claude -p --chrome`)で note の下書きを保存(`note_draft_post_spec_20261002.txt`) |

- 2時間に10回の枠に対して、時間帯が重ならないので余裕がある(最大は `radio_guest_check` の8回)。
- Chrome は1つなので、Chrome を使うバッチ同士(上の3つ)は時間帯をずらしてある。
- `radio_guest_check` は `skipped` か `rate_limited` を受け取ったら、残りの質問を打ち切る。

---

## 6. X 株トレンド検索(stock_x_trends)の Web 版

### 6.1 流れ

```
06:30 / 22:30  stock_x_trends_web_fetch
                 ├─ stock_x_trends.source が "web" でなければ何もしない
                 ├─ ask_grok(API版の1つ目の観点「broad」と同じ質問, schema=JSON_SCHEMA)
                 ├─ Grok の結果を実行ごとのファイルに保存
                 └─ 正規化して state/stock_x_trends_web_cache.json に書く
07:00 / 23:00  stock_x_trends
                 ├─ キャッシュを読む(120分より古ければ使わない)
                 ├─ その回の結果を実行ごとのファイルに保存
                 ├─ 07:00 なら前の晩 23:00 の結果と合わせる
                 └─ output/stock_x_trends.json に書く(レポートはこれを読む)
```

### 6.2 設定(config.json)

```json
"stock_x_trends": {
  "enabled": true,
  "model": "grok-4.3",
  "max_tokens": 1000,
  "source": "web",
  "web_cache_max_age_minutes": 120
}
```

- `source` を `"api"` に戻すと、API 版に戻る。同時に `stock_x_trends_web_fetch` も何もしなくなる。
- `model` / `max_tokens` は API 版のときだけ使う。
- `stock_x_trends_web_fetch` の時間切れまでの時間は、grok_web が 600 秒、main.py の監視が 650 秒(`MODULE_TIMEOUT_OVERRIDES`)。

### 6.3 07:00 の合わせ方

- 前回の 07:00 より後に成功した **23:00 の実行ファイル** と、朝の結果を合わせる
  (月曜の朝は金曜の夜と合わせる)。
- 重複(銘柄コード+名前が同じもの、同じキーワード)だけを除き、**件数の上限なし** で全件残す。
- 07:00 をもう一度実行しても結果は変わらない。

| 夜 23:00 | 朝 07:00 | 07:00 のレポート |
|---|---|---|
| 成功 | 成功 | 夜+朝(重複除去) |
| 成功 | 失敗 | 夜の分だけ(`morning_status` に朝の失敗理由) |
| 失敗 | 成功 | 朝の分だけ |
| 失敗 | 失敗 | skipped(X トレンドなし) |

- 前の晩が失敗していた場合、さらに前の古い結果は使わない。
- 09:30 / 12:15 / 17:30 は検索せず、07:00 の `output/stock_x_trends.json` をそのまま使う。

### 6.4 ファイル

| ファイル | 内容 | 保存期間 |
|---|---|---|
| `output/history/stock_x_trends_web_fetch_runs/stock_x_trends_web_fetch_YYYYMMDD_HHMM.json` | Grok の結果(ask_grok の結果 JSON)を実行ごとに保存 | 無期限 |
| `state/stock_x_trends_web_cache.json` | 正規化した最新の結果(stock_x_trends が読む) | 毎回上書き |
| `output/stock_x_trends_web_fetch.json` | 取得モジュールの実行結果(件数、所要時間、raw ファイル名) | 毎回上書き |
| `output/history/stock_x_trends_runs/stock_x_trends_YYYYMMDD_HHMM.json` | stock_x_trends のその回だけの結果(失敗・skipped も保存) | 無期限 |
| `output/stock_x_trends.json` | レポートが読む結果(07:00 は夜と合わせたもの) | 毎回上書き |
| `output/history/stock_x_trends_YYYYMMDD.json` | 日付ごとの履歴(同じ日の 07:00 は 23:00 で上書き) | 30日 |

ファイル名の `HHMM` は実行した時間帯(0700, 2300 など)。手で実行したときは `HHMMSS_manual`。

### 6.5 API 版との違い

| | API 版 | Web 版 |
|---|---|---|
| 費用 | 1回 約20〜50円 | 0円(Claude Pro の枠を使う) |
| 検索回数 | 1回目の結果が少ないときだけ追加で検索(最大3観点) | 1回(broad の観点だけ) |
| 1回の件数 | 銘柄8件まで | 銘柄4件前後(2026-10-02 の実測) |
| 所要時間 | 数十秒 | 約2〜4分 |

Web 版は1回の件数が少ないが、07:00 で夜と朝を上限なしで合わせるので、レポート全体の件数はあまり減らない。

---

## 7. 失敗したときの見え方と対処

| 症状 | 見え方 | 対処 |
|---|---|---|
| Chrome が閉じている・拡張が未接続 | `error` に「claude-in-chrome のツールが使えない」 | Chrome を起動し、拡張の接続を確認 |
| 規約の同意・年齢確認 | `error` にその画面の説明 | 本人が Chrome で grok.com を開いて済ませる |
| ログイン切れ | `logged_in: false` | grok.com に再ログイン |
| 利用上限 | `status: "skipped"`、`next_available_at` | 待つ(自動で回復)。`--usage` で確認 |
| Grok 側の上限 | `rate_limited: true`、2時間停止 | 待つ |
| 時間切れ | `error` に「timed out」 | 頻発するなら `timeout_seconds` を延ばす |
| X トレンドがレポートに出ない | `output/stock_x_trends.json` が skipped、理由が `reason` | `stock_x_trends_runs` と `stock_x_trends_web_fetch_runs` の該当時刻のファイルを見る |

---

## 8. 経緯

- 2026-09-28: Web 版への切り替えを検討したが、当時は「claude-in-chrome は `-p` では動かない」
  「ログインなしの grok.com は回答しない」と結論し、見送り(`progress_notes.md`)。
- 2026-10-02: `-p` で動かなかった原因が、指示文の改行で引数が切れていたことだと判明。
  標準入力で渡す形にして動作を確認し、grok_web.py を作成。X 株トレンドを Web 版に切り替え
  (コミット `23ccc15`)。初回の本番(10/2 22:30 → 23:00)は成功。
- 2026-10-04: `radio_guest_check` が grok_web を利用開始。`extra_instructions` 引数を追加。
