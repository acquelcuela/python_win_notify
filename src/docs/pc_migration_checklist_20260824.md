# 別PCへの移行チェックリスト(2026-08-24)

git pull + Python + タスクスケジューラだけでは不十分。gitignoreされていて
pullでは来ないものが複数あるため、それらは個別にコピーする必要がある。

## 前提

- リポジトリ: このNightlyBatchNotifyプロジェクト一式
- 移行先: 別のWindows PC

## 1. gitでそのまま移行できるもの

- `src/`以下のコード・モジュール一式
- `src/config.json`、各種`*_config.json`(keyword_watch_config.json等)
- `src/data/data_j.csv`、`src/data/data_j_aliases.json`
- `src/docs/`以下のドキュメント一式
- `src/requirements.txt`(yfinance, python-dotenv, beautifulsoup4, certifi。他は標準ライブラリで完結)

## 2. gitignoreされていて個別コピーが必要なもの

| 項目 | パス | 無いとどうなるか |
|---|---|---|
| 環境変数・APIキー | `src/.env` | 全モジュールが即エラー(必須、最優先) |
| 実行履歴・的中率ログ等 | `src/state/` 一式 | 履歴がリセットされる(30日レンジ/Xトレンドの的中率分析が消える、keyword_watch・marketplace_watchの既読管理も初回扱いに戻る) |
| note投稿一覧(ユーザー提供分) | `src/state/note_articles.json` | note構想生成が既出記事と気づかず重複提案する可能性 |
| Xトレンド・大相撲ニュースのアーカイブ | `src/output/history/` | 月次まとめ・答え合わせに使う過去データが消える |
| タスクスケジューラ登録スクリプト本体 | `files1/setup_task.ps1` | これ自体がgitignore対象の「archive」フォルダに入っているため、pullしても来ない |

state/以下は「リセットされてもいい」なら省略してよい(履歴なしで再スタートする形)。

## 3. 移行先で書き換えが必要な設定

- `files1/setup_task.ps1`内の`$projectPath = "C:\batch_stock_files"`
  → 移行先の実際の配置フォルダパスに変更する
- `config.json`・各種`*_config.json`内のOneDriveパス
  (`C:\Users\user\OneDrive - LIFEWORK\send@OneDrive2027`)
  → 移行先PCのWindowsユーザー名・OneDriveフォルダ構成が違う場合は要修正
  (該当箇所: `marketplace_watch_config.json`の`export_dir`相当、
  `note_article_ideas_config.json`の`export_dir`、`onedrive_check`関連の
  デフォルトパス)

## 4. 手順

1. コミット・push(このリポジトリ)
2. 移行先PCでclone(またはpull)
3. 個別コピーが必要なファイル(上記2.)をUSBやOneDrive等で移す
4. Pythonをインストール(3.14想定、`setup_windows.bat`が
   `%LOCALAPPDATA%\Python\...`配下を探す)
5. `src/setup_windows.bat`を実行(venv構築・依存パッケージインストール)
6. `files1/setup_task.ps1`のパスを書き換えて、管理者権限のPowerShellで実行
   (タスクスケジューラ登録。15分おきポーリングの仕組み)
7. OneDriveパス等、環境依存の設定を確認・修正
8. 動作確認: `run.bat`を手動実行するか、`main.py --force --schedule <時刻>`で
   任意のスケジュールを強制実行してログを確認する
