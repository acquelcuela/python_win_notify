# 新しいPCでのセットアップ指示(Claude Code向け)

これは、別のWindows PCで動いていた「NightlyBatchNotify」というPythonバッチ処理
一式(このリポジトリ)を、このPCに移行するための作業指示です。あなた(Claude
Code)がこのPC上で、この指示に従って作業を進めてください。

## 前提状況

このPCには次の2つが用意されています。

1. このリポジトリを`git clone`(または`git pull`)したディレクトリ
   - トラッキング対象のコード・設定ファイルのみが入っている
2. 元PCの作業ディレクトリ全体を`tar.gz`で固めたアーカイブファイル
   - こちらには`.gitignore`で除外されているファイル(APIキー・実行履歴・
     ユーザー提供データなど)も含まれている

まず最初に、この2つが実際にどこに配置されているか(gitでcloneしたディレクトリの
パス、tar.gzファイルのパス)をユーザーに確認してください。見当たらない場合は
デスクトップ・ダウンロードフォルダ・ユーザーのホームディレクトリ等を探してよい
ですが、不明な場合は必ず質問してください。

## 作業手順

### 1. tar.gzを展開する

適当な一時フォルダ(例: `%TEMP%\nightly_batch_restore`)に展開する。中身は
元PCでの`python_win_notify`リポジトリ全体(gitignore対象ファイルも含む)。

### 2. gitignore対象ファイルをコピーする

git cloneしたディレクトリに向けて、以下を展開したアーカイブからコピーする
(コード自体はgit側が最新なので上書きしない、データ・秘密情報だけを移す):

| コピー元(展開したtar.gz内) | コピー先(git cloneしたディレクトリ) | 内容 |
|---|---|---|
| `src/.env` | `src/.env` | Gmail・Gemini・Grok等のAPIキー・認証情報(最優先・必須) |
| `src/state/` 一式 | `src/state/` | 実行履歴・的中率ログ・既読管理など |
| `src/state/note_articles.json` | `src/state/note_articles.json` | (↑に含まれるはずだが念のため個別確認) ユーザー提供のnote投稿一覧 |
| `src/output/history/` | `src/output/history/` | Xトレンド・大相撲ニュースのアーカイブ |
| `files1/setup_task.ps1` | `files1/setup_task.ps1` | タスクスケジューラ登録スクリプト本体。このフォルダごとgitignore対象なので、git側には存在しない |

このリポジトリの`src/docs/pc_migration_checklist_20260824.md`にも同じ内容の
チェックリストがあるので、あわせて参照してよい。

### 3. Python環境をセットアップする

git cloneしたディレクトリの`src/setup_windows.bat`を実行する。これが
`.venv`を作成し、`requirements.txt`(yfinance, python-dotenv, beautifulsoup4,
certifi。他は標準ライブラリ)をインストールする。Python本体(3.14想定)が
`%LOCALAPPDATA%\Python\...`配下に見つからない場合は、先にPythonの
インストールが必要になる旨をユーザーに伝える。

### 4. 環境依存パスを確認・修正する

以下は元PC(ユーザー名`user`)のパスがハードコードされている可能性があるため、
このPCのWindowsユーザー名・OneDriveフォルダ構成に合わせて確認・修正する:

- `files1/setup_task.ps1`内の`$projectPath = "C:\batch_stock_files"`
  → このPCでの実際の配置先フォルダパスに書き換える
- `config.json`・`marketplace_watch_config.json`・`note_article_ideas_config.json`
  等の中にある`C:\Users\user\OneDrive - LIFEWORK\send@OneDrive2027`
  (OneDriveの送信先フォルダパス)
  → このPCの実際のOneDriveパスと一致しているか確認し、違えば修正する

### 5. タスクスケジューラに登録する

`files1/setup_task.ps1`を(パス修正後)管理者権限のPowerShellで実行する。
15分おきにポーリングして、`config.json`の`batch_schedule`に一致する時刻だけ
実処理を行う仕組みなので、この1本を登録すれば十分。

### 6. 動作確認

- `src/run.bat`を手動実行し、`src/logs/task_runner_*.log`と
  `src/logs/batch_*.log`にエラーが出ていないか確認する
- 任意のスケジュールを強制実行して確認したい場合:
  `.venv\Scripts\python.exe main.py --force --schedule <HH:MM>`
  (例: `--force --schedule 22:00`)
- 実際にメールが届くか、`C:\Users\user\OneDrive - LIFEWORK\send@OneDrive2027`
  相当のフォルダにファイルが配置されるかも確認する

## 注意

- `.env`の中身(APIキー等)は絶対に外部に出力・共有しないこと
- state/以下を古いPCと同期させたくない(履歴をリセットして新規スタートしたい)
  場合は、手順2のstate/コピーを省略してよい
