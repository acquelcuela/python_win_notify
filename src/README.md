# NightlyBatchNotify

Windows のタスクスケジューラが `run.bat` を呼び出します。このバージョンは
`src\.venv` の中の Windows Python で動作し、Docker は不要です。

## クイックスタート

1. `config.json` を確認します。
2. `.env.example` から `.env` を作成し、Gmail設定を編集します。
3. 仮想環境を作成します:

```cmd
setup_windows.bat
```

4. 手動でテスト実行します:

```cmd
.venv\Scripts\python.exe main.py --force
```

5. ダブルクリックでスケジュールタスクを登録します:

```text
scheduler\install_scheduled_task.bat
```

6. X投稿マガジンモジュールだけを手動で実行します(07:30/21:00の時刻チェックを
   スキップしますが、実際にXへ投稿する本番実行です):

```text
run_post_x_magazine.bat
```

7. 旧世代のnote→X投稿ランナー(現在`config.json`でOFF):

```text
run_post_x_note.bat
```

8. プレビュー専用のnote→X投稿ランナー:

```text
run_post_x_note_preview.bat
```

## スケジューラ関連

- 登録: `scheduler\install_scheduled_task.bat`
- 削除: `scheduler\uninstall_scheduled_task.bat`
- 確認: `scheduler\check_scheduled_task.bat`

登録したタスクは1日中15分おきに起動します。実際の処理は`config.json`の
`batch_schedule`で定義されたスケジュール枠(時刻・曜日・実行モジュールを1つに
まとめたエントリ)の中でのみ実行されます。現在は`07:00`/`09:30`/`12:15`/`22:45`が
平日のみ・全モジュール、`07:30`/`21:00`が毎日・`post_x_magazine`のみです。詳細は
`docs\時刻別実行仕様.md`を参照してください(`stock_x_trends`が07:00にしか検索
しないのにその日のメール全てに結果が表示される、といったモジュールごとの
自己制限についても記載しています)。

`run_post_x_note.bat`はメインのスケジューラとは別系統で、`config.json`により
常時OFFです。ライブのX投稿モジュールとしては`post_x_magazine`がこれを
置き換えました。

`run_post_x_note_preview.bat`は`state\post_x_note_preview_article.json`を
読み込み、下書きテキストを作成しますが、Xへの投稿は行いません。

## ドキュメント

- 現行仕様: `docs\時刻別実行仕様.md`
- 進捗と意思決定の記録: `docs\progress_notes.md`
