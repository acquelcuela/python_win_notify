# post_images

X投稿(`post_x_magazine`)に添付する画像を置くディレクトリです。ここには
**まだ使っていない画像**だけを置きます。

- 投稿に使った画像は `../post_images_posted/` へ移動する運用にします。
- ここに置いた画像ファイル自体はgit管理対象外です(`.gitignore`参照)。
  このREADMEだけがgitに残ります。
- 現時点では画像を自動でピックアップして投稿に添付する仕組み、および
  投稿後に`post_images_posted/`へ移動する処理は未実装です。
  画像が揃ったら、`post_x_magazine.py`側の実装に着手します。
