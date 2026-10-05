# note 下書き自動保存

`modules/note_draft_post.py` が、`queue/` に置いた指示書(`.md`)を1件ずつ
Claude(`claude -p --chrome`)に渡し、note.com に **下書き保存まで** 行います。
価格・有料ライン・ハッシュタグ・画像・マガジン設定と公開は手動です
(指示書に書いた内容は結果メールに「手動で仕上げる項目」として出ます)。

## 使い方

1. `queue/_template.md` をコピーして、`_` で始まらない名前で `queue/` に置く
   (例: `queue/2026-10-05_高配当株まとめ.md`)。ファイル名順に処理されます
2. 毎日23:30に起動し、**週次リミットのリセットまで24時間以内**のときだけ実行
   (= 週1回、リセット前夜)。週次使用率90%以上・セッション80%以上なら実行しない
3. 成功 → `done/`、失敗 → `failed/` に日付付きで移動。失敗分は直して `queue/` に戻せば次回再実行
4. 結果メール「note下書き n/m件」に、下書きURL・1件ごとの使用率の増分が載ります
   (履歴: `state/note_draft_runs.json`)

すぐ試す場合(タイミング判定をスキップ):

    cd src
    .venv\Scripts\python.exe -m modules.note_draft_post --force

設定は `config.json` の `note_draft_post`(1回あたりの最大件数・使用率の上限など)。

指示書の中身は個人コンテンツのため git 管理外です(README と `_template.md` のみ管理)。
