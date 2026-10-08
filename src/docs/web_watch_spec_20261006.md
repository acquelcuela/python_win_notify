# web_watch(Webページ監視)仕様書・申し送り

最終更新: 2026-10-06

指定した Web ページをこのPCの Chrome で読み、商品・価格・キャンペーンを抽出して
前回と比較し、結果をメールする仕組み。第1弾は OPPO 公式ショップ。
監視先は今後追加していく予定(ユーザー談)。

---

## 1. 全体の仕組み

```
main.py(23:00 枠)
  └─ modules/web_watch.py  run()
       └─ web_watch_config.json の targets ごとに:
            ├─ claude -p --chrome を1回起動(指示文は標準入力、--json-schema で構造化出力)
            │    └─ Claude が対象の全ページを新しいタブで開いて読み、商品・キャンペーンを抽出 → タブを閉じる
            ├─ 前回の結果 state/web_watch/<id>.json と Python で比較
            ├─ 今回の結果で <id>.json を上書き + <id>_history.jsonl に1行追記
       └─ 全対象をまとめてメール1通(毎回送る)
```

- **AI がやるのは抽出だけ**。比較(値下がり・新着など)は Python で機械的に行う。
  AI に比較させると取得の揺れで誤検知が出るため(ユーザーとも合意済みの方針)。
- Claude in Chrome を選んだのはユーザーの希望(PCも Chrome も常時起動しているため)。
  Playwright + Gemini 案も検討したが不採用。
- `claude -p --chrome` の起動方法と前提条件は grok_web と共通
  (`_find_claude_binary` / `_kill_tree` / `ALLOWED_TOOLS` を grok_web から import)。
  前提条件の詳細は `docs/grok_web_spec_20261005.md` を参照。

## 2. ファイル

| パス | 内容 |
|---|---|
| `modules/web_watch.py` | 本体 |
| `web_watch_config.json` | 監視対象(git 管理) |
| `state/web_watch/<id>.json` | 前回成功時の全データ。比較の基準(gitignore) |
| `state/web_watch/<id>_history.jsonl` | 毎回の全データを1行ずつ追記。日々のログ。削除処理なし(1日約6KB) |
| `output/web_watch.json` | 直近実行の結果(diff・所要時間・コスト含む)。デバッグ用 |

## 3. 設定(web_watch_config.json)

```json
{
  "targets": [
    {
      "id": "oppo",                       // state のファイル名になる。変えると履歴が別扱いになる
      "name": "OPPO公式ショップ",           // メール表示名
      "pages": [
        {"section": "スマートフォン", "url": "..."},
        {"section": "トップ", "url": "...", "items": false}   // 商品は取らず campaigns だけ
      ],
      "instruction": "サイト固有の抽出指示(自然文)",
      "timeout_seconds": 420,             // 省略可(既定 420)
      "enabled": true                     // 省略可
    }
  ]
}
```

監視先の追加 = targets に1件足すだけ(コード変更不要)。追加時は一度
`python -m modules.web_watch --only <id> --no-mail` で抽出結果を確認すること。

## 4. 抽出スキーマと比較ルール

- items: `section` / `name` / `price`(円・整数)/ `regular_price` / `badges`(例 "10%OFF")/ `url`
- campaigns: `title` / `url`
- pages: 各ページを読めたか(`loaded`)

比較(`_diff`):
- 商品のキーは `section|url`(url が無ければ name)。同じ商品がスマホ一覧とアウトレットの
  両方に出るため section を含めている。OPPO の url には `?category_page_id=...` が付くが安定している
- 検出するもの: 値下がり / 値上がり / 新着 / 掲載終了 / badges の変化 / キャンペーン追加・終了
- キャンペーンのキーは url のみ(title は AI の要約で毎回揺れるため比較に使わない)
- 比較相手は「前日」ではなく「前回成功した回」。全ページ失敗時は state を更新しない
- 一部ページだけ読めなかった場合、その section の商品は前回分を引き継ぐ
  (「全部消えた」と誤通知しないため)。`items: false` のページが失敗したらキャンペーンも前回分を引き継ぐ
- 初回(前回ファイルなし)は比較せず「初回取得」

## 5. メール

- 件名: `【変化あり】Web監視 OPPO公式ショップ 変化N件` / `… 変化なし` / `… 初回取得`、失敗があれば `【失敗】`
- 本文: 変化一覧(⬇値下がり ⬆値上がり 🆕新着 ➖掲載終了 🏷表示変更 📣キャンペーン)→ 「現在の掲載一覧」アコーディオン(デフォルト閉、中にセクション別の全商品表とキャンペーン一覧)
- 送信は `mail_gmail.send_html_mail`(.env の GMAIL_ADDRESS / GMAIL_APP_PASSWORD / MAIL_TO)

## 6. スケジュール

- 平日: 23:00 枠の最後(株の mail_gmail の後)に `web_watch`
- 土日: `{"time": "23:00", "days": "weekend", "modules": ["web_watch"]}` を別エントリで追加
- main.py の `MODULE_ORDER` 末尾(mail_gmail の後)、`MODULE_TIMEOUT_OVERRIDES["web_watch"] = 900`
- 同時に main.py の `resolve_modules_for_schedule` を修正: 同じ時刻のエントリが複数ある場合、
  今日の曜日が `days` に含まれるエントリを優先する(修正前は時刻だけで最初のエントリを選び、
  土日にも平日 23:00 の株モジュール一式が走るバグがあった)

## 7. 実測(2026-10-06 テスト)

- OPPO(3ページ): 約 120 秒、API 換算 約 $0.74/回(Claude プランの枠を消費、請求なし)
- 取得: スマートフォン 16件 / アウトレット 2件 / キャンペーン 16件(サイト表示「全16件」「全2件」と一致)
- diff は擬似データで全種類の検出を確認済み。実データでの初比較は 2026-10-06 23:00 実行分

## 8. 注意点・未対応

- **タスクスケジューラ NightlyBatchNotify の ExecutionTimeLimit は 90分**(2026-10-06 に
  10分から変更済み。`files1/setup_task.ps1` も修正済み)。監視先を増やすときは、平日 23:00 枠の
  合計(株モジュール約45秒 + 監視先1件あたり約2分)が90分に収まるかを目安にする。
  設定方法は `new_pc_setup_instructions.md` の「5. タスクスケジューラに登録する」
- get_page_text はトップページで「お知らせ記事」しか返さなかったため、プロンプトで
  javascript_tool(document.body.innerText / a の href・img の src/alt)での確認を指示している
- OPPO トップのスライドは画像のみ。キャンペーン名は画像ファイル名・リンク先からの推測
- 23:30 の note_draft_post も Chrome を使う。23:00 枠が 30 分以上かかると lock で 23:30 枠が飛ぶ
- 今後の拡張案(未実装): 履歴を使った「過去N日最安値更新」「N日前比」通知、
  目標価格(marketplace_watch の target_price のような)指定
- 2026-10-06 時点で web_watch 関連は未コミット(`modules/web_watch.py`, `web_watch_config.json`,
  `config.json`, `main.py`, 本書, `docs/時刻別実行仕様.md`)
