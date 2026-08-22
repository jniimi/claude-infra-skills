---
name: pcloud-io
description: Read and write files on pCloud cloud storage from Python — list folders, upload files, and save/load pandas DataFrames as parquet without touching local disk. Use when a task involves pCloud, an rclone pcloud remote, PCLOUD_AUTH / pCloud API authentication (OAuth access token or getdigest login), or moving parquet/data files to and from cloud storage on a Colab or remote VM.
---

# pCloud への読み書き

`pcloud_io.py` は pCloud の HTTP API を直接叩く単体スクリプト。依存は `requests` と
`pandas` だけ(parquet を使うなら `pyarrow`)。SDK は使わない。

## 使い方

**そのまま CLI として叩く**(接続確認と初回設定):

```bash
python "${CLAUDE_SKILL_DIR}/pcloud_io.py"          # 引数なし = 接続確認(アカウントと使用量を表示)
python "${CLAUDE_SKILL_DIR}/pcloud_io.py" login    # メール+パスワードでリージョン判定 → Keychain に保存
python "${CLAUDE_SKILL_DIR}/pcloud_io.py" token    # rclone remote の PCLOUD_AUTH / PCLOUD_ENDPOINT を表示
```

サブコマンドはこの3つだけ(`login` / `token` / 引数なし)。`token` は**トークンを画面に
出す**ので、共有中の端末や録画中には使わない。

**他の repo から使う**場合はファイルを1つコピーして import する。パッケージ相対 import は
無いので、置き場所はどこでもよい:

```python
import pcloud_io as pc

cloud = pc.init()                                  # 資格情報を解決してクライアントを返す
pc.to_parquet(df, "data/all.parquet", cloud=cloud) # 保存(同名は上書き)
df = pc.from_parquet("data/all.parquet", cloud=cloud)
print(pc.ls(cloud, "/data"))
```

依存は `uv run --with requests --with pandas python ...` で足せる。**`pyarrow` を runtime
依存として固定インストールしないこと** — Colab のプリインストール版を別バージョンで
上書きすると `pyarrow.lib.IpcReadOptions size changed` のバイナリ非互換が起き、pandas は
`except ImportError` しか捕まえないので `import pandas` ごと落ちてカーネル再起動になる。
だから parquet 系の関数は `pyarrow` を関数内で遅延 import している。

## 主要 API

| 関数 | 説明 |
| --- | --- |
| `init(verbose=True) -> PCloud` | 資格情報を解決してクライアントを作る。`verbose` で `userinfo` を1回叩いて疎通確認する |
| `ls(client, path, *, missing_ok=True) -> DataFrame` | フォルダ一覧を `name`/`isfolder`/`size`/`fileid` で返す。存在しないフォルダは既定で空の DataFrame |
| `upload(client, local_path, remote_dir, *, name=None, folder_id=None, verbose=False) -> dict` | ローカルのファイルを1つ上げる。`folder_id` を渡すとフォルダ解決を省ける |
| `upload_fileobj(client, fileobj, remote_dir, name, *, folder_id=None) -> dict` | 開いたバッファをそのまま上げる。ディスクを経由しない。ここだけ再試行つき |
| `read_parquet(client, fileid=None, *, path=None, columns=None, missing_ok=True) -> DataFrame` | pCloud 上の parquet をメモリに落として読む。`columns` に無い列があっても実在するものだけ読む |
| `to_parquet(df, path, *, cloud=None, compression="zstd", index=False, verbose=True) -> str` | DataFrame を parquet で保存。書き出しはメモリ上、途中のフォルダは自動生成 |
| `from_parquet(path, *, columns=None, cloud=None) -> DataFrame` | `to_parquet()` で書いたものを読む薄いラッパ |
| `file_link(client, fileid=None, *, path=None) -> str` | ダウンロード用の一時直リンクを作る。`fileid` かパスのどちらかで指定 |
| `ensure_folder(client, path) -> int` | 途中の階層も含めてフォルダを作り `folderid` を返す。あれば何もしない |
| `resolve(path) -> str` | 相対パスを `PROJECT_ROOT` 基準の絶対パスにする。`/` 始まりはそのまま |

`cloud=` を省くと `to_parquet` / `from_parquet` は内部で `init(verbose=False)` を呼ぶ。
ループで何度も呼ぶなら `cloud = init()` を一度作って渡すこと(毎回の資格情報解決が減る)。

エラーは `PCloudError`(`result` が 0 以外)。`method` / `code` / `message` に加えて応答
本体を `data` に持つ。エラー応答にしか入っていない値を後続で使えるようにするため。

## 認証の解決順

**環境変数 > Colab のシークレット > rclone の pcloud remote > 保存済み設定 + Keychain。**

1. **環境変数** (`PCLOUD_AUTH`, または `PCLOUD_EMAIL` + `PCLOUD_PASSWORD`) を最優先に
   するのは、一時的に別の資格情報を差し込めるようにするため。CI もこの経路を通る。
2. **Colab のシークレット** — `google.colab.userdata` への依存は `_load_colab_secrets()`
   の lazy import 1箇所に閉じ込めてある。Colab 以外では `ImportError` になるので黙って
   何もしない。「どのマシンか」ではなく「`userdata` が使えるか」で判定する(hostname 判定は
   新しいマシン・VM・CI で誤判定する)。既存の環境変数は上書きしない。
3. **rclone の pcloud remote** — `rclone config dump` の JSON から OAuth アクセストークンを
   取る。INI を直接パースしないのは、暗号化・エスケープで rclone 側の解釈と食い違うため。
   pcloud remote が複数あると例外になるので `PCLOUD_RCLONE_REMOTE` で選ぶ。
4. **保存済み設定 + Keychain** — `login` が書いた JSON(メールとリージョン)と macOS
   Keychain のパスワードでダイジェスト認証する。

## pCloud API の癖(2026-08-20 実測)

踏むと原因が分かりにくいものだけ:

- **パラメータは必ずクエリ文字列で渡す。** POST のボディ(form-encoded)に入れても受理
  されて `result=0` が返るが、`getauth` のようなフラグは黙って無視され、応答から期待した
  キーだけが消える。`_call()` が `params=` で渡しているのはこのため。
- **アカウントによっては平文パスワード認証が全メソッドで拒否される** — `listfolder` を
  含め `result=1022 Please provide 'code'`。一方 `getdigest` によるダイジェスト認証は通る。
- **ダイジェスト経路では `getauth` が無視される**ので、**API 単体では失効可能なトークンを
  発行できない**(自前アプリの登録は承認待ちになる)。
- したがって**既定は rclone の pcloud remote が持つ OAuth アクセストークン**。rclone は
  登録済みの client_id を同梱しているので `rclone config` のブラウザ認可だけで取得でき、
  pCloud の Settings → Applications からいつでも失効させられる。
- **リモート VM へ渡すのはこのトークンだけ**で、パスワードは渡さない。失効できない資格情報を
  第三者の VM に置かないため。
- **アカウントは US / EU のどちらかのリージョンにしか存在しない。** 相手を間違えると
  `result=2000`。`login` が両方試して当たった方を保存する。
- **`upload()` は `renameifexists=0` なので同名は上書き。** 再実行してもファイルは増えない
  (実測で fileid も同一)。1 だと `file (1)` が増えていくので、冪等に回したいなら 0 のまま。
- タイムアウト・切断・5xx は `_request()` が指数バックオフで再試行する。ただし multipart の
  POST は再試行しない(ファイルハンドルを読み切った後で再送できない)。巻き戻せる
  バッファを使う `upload_fileobj()` だけが seek し直して再試行する。

## Colab のブラウザノートブックから使う

ノートブックには資格情報をファイルで置く経路が無いので、Colab のシークレットに登録する。

```bash
python "${CLAUDE_SKILL_DIR}/pcloud_io.py" token   # PCLOUD_AUTH=... / PCLOUD_ENDPOINT=... を表示
```

表示された値を Colab 左ペインの鍵アイコンから `PCLOUD_AUTH`(EU アカウントなら
`PCLOUD_ENDPOINT` も)として登録し、**そのノートブックへのアクセスを許可**する。あとは
ノートブックで `pcloud_io.init()` を呼べば `_load_colab_secrets()` が拾う。許可を忘れると
`userdata` が例外を投げるが、任意扱いなので黙って次の候補(rclone → Keychain)に落ちる —
Colab には rclone も Keychain も無いので「資格情報がありません」で止まる。

## 環境変数

| 変数 | 効果 |
| --- | --- |
| `PCLOUD_ROOT` | 相対パスの基準。既定 `/`(= pCloud のルート) |
| `PCLOUD_AUTH` | OAuth アクセストークン。あればこれだけで認証する |
| `PCLOUD_ENDPOINT` | API のエンドポイント。EU アカウントは `https://eapi.pcloud.com` |
| `PCLOUD_EMAIL` | ダイジェスト認証のメールアドレス |
| `PCLOUD_PASSWORD` | ダイジェスト認証のパスワード。`login` は `security` コマンドの引数に一瞬パスワードを載せるので、共有マシンではこちらで渡す |
| `PCLOUD_TOKEN_STORE` | 設定(メール+リージョン)の保存先。既定 `~/.config/pcloud-io/pcloud.json` |
| `PCLOUD_RCLONE_REMOTE` | pcloud remote が複数あるときに使う名前 |
| `PCLOUD_KEYCHAIN_SERVICE` | macOS Keychain のサービス名。既定 `pcloud-io` |

`PCLOUD_ROOT` と `PCLOUD_KEYCHAIN_SERVICE` は **import 時に1回だけ読む**モジュール定数
なので、`os.environ` を後から書き換えても効かない。プロセス起動前に設定すること。
