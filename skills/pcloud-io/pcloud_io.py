"""pCloud の HTTP API クライアント(単体で動くスクリプト)。

認証・フォルダ作成・一覧・アップロード・parquet の読み書きだけを扱う。SDK を足さない
のは、必要な操作がこれだけで、依存を増やすと Colab のプリインストール環境と噛み合わせる
手間が増えるため。

依存は `requests` と `pandas` のみ。parquet を使う場合は `pyarrow` も要るが、**runtime
依存として固定インストールしてはいけない** — Colab のプリインストール版を別バージョンで
上書きすると `pyarrow.lib.IpcReadOptions size changed` のバイナリ非互換が起き、pandas 側は
`except ImportError` しか捕まえないので `import pandas` ごと落ちてカーネル再起動になる。
そのため `pyarrow` は `to_parquet()` / `read_parquet()` の中で遅延 import している。

**パラメータは必ずクエリ文字列で渡す。** POST のボディ(form-encoded)に入れると、
認証は通って `result=0` が返るのにフラグが無視され、期待したキーが欠ける。

**アカウントは US / EU のどちらかのリージョンにしか存在しない。** 相手を間違えると
`result=2000` になるので、初回に両方試して当たった方を保存する。

認証は**リクエストごとのダイジェスト認証**。2026-08-20 の実測で:

- `username` + `digest` + `passworddigest` … `listfolder` まで通る
- `username` + `password`(平文) … `result=1022 Please provide 'code'` で操作ごと拒否
- `getauth=1` … ダイジェスト経路では無視され、auth トークンは返らない

アカウントによっては pCloud 側が平文パスワード認証を止めており、API 単体では**失効可能な
トークンを発行できない**(自前アプリの登録は承認待ちになる)。

そこで既定は **rclone の pCloud remote が持つ OAuth アクセストークン**を使う。rclone は
登録済みの client_id を同梱しているので、`rclone config` のブラウザ認可だけでトークンが
得られ、pCloud の設定画面からいつでも失効させられる。VM へ渡すのもこのトークンで、
パスワードは渡らない。

    rclone config    # n) pcloud を作る(ブラウザで許可するだけ)

トークンが無い場合の代替としてダイジェスト認証も残してある(パスワードが必要)。
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import requests

# アカウントの所属リージョン。初回だけ両方試す。
API_ENDPOINTS = ("https://api.pcloud.com", "https://eapi.pcloud.com")

TIMEOUT = 120

# 一時的なエラー(タイムアウト・切断・5xx)は待って再試行する。パネルを数千件
# 走査するときに並列度を上げると、pCloud 側が応答しきれず ReadTimeout になるため。
RETRIES = 4
BACKOFF = 2.0

# 相対パスの基準。既定は pCloud のルート。
PROJECT_ROOT = os.environ.get("PCLOUD_ROOT", "/")

KEYCHAIN_SERVICE = os.environ.get("PCLOUD_KEYCHAIN_SERVICE", "pcloud-io")

# rclone の remote 名。複数の pcloud remote がある場合に選ぶ。
RCLONE_REMOTE_ENV = "PCLOUD_RCLONE_REMOTE"

# ダイジェストの有効期間は短い(実測で数分)。使い回しつつ切れる前に取り直す。
DIGEST_TTL = 60.0


class PCloudError(RuntimeError):
    """API がエラーを返した(`result` が 0 以外)。

    レスポンス本体を `data` に持たせる。エラー応答にしか入っていない値を
    後続で使えるようにするため。
    """

    def __init__(self, method: str, code: int, message: str, data: dict | None = None):
        self.method, self.code, self.message = method, code, message
        self.data = data or {}
        super().__init__(f"pCloud {method} が失敗 (result={code}): {message}")


def token_store_path() -> Path:
    """メールアドレスとリージョンの保存先。`PCLOUD_TOKEN_STORE` で変更できる。"""
    path = Path(os.environ.get(
        "PCLOUD_TOKEN_STORE", Path.home() / ".config" / "pcloud-io" / "pcloud.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _request(http_method: str, url: str, **kwargs) -> requests.Response:
    """再試行つきの HTTP 呼び出し。ファイル本体を送る POST は再試行しない
    (ファイルハンドルを読み切った後なので、そのままでは再送できない)。"""
    last = None
    for attempt in range(RETRIES):
        try:
            response = requests.request(http_method, url, **kwargs)
        except requests.RequestException as e:
            last = e
            if "files" in kwargs or attempt == RETRIES - 1:
                raise
        else:
            if response.status_code < 500 or attempt == RETRIES - 1:
                return response
            last = response
        time.sleep(BACKOFF ** attempt)
    raise RuntimeError(f"pCloud への接続に失敗しました: {last}")


def _call(endpoint: str, method: str, *, files=None, **params) -> dict:
    params = {k: v for k, v in params.items() if v is not None}
    url = f"{endpoint}/{method}"
    if files is None:
        response = _request("get", url, params=params, timeout=TIMEOUT)
    else:
        response = _request("post", url, params=params, files=files, timeout=TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if data.get("result", 0) != 0:
        raise PCloudError(method, data.get("result"), data.get("error", ""), data)
    return data


def rclone_credentials(remote: str | None = None) -> dict | None:
    """rclone の設定から pCloud の OAuth トークンを取り出す。無ければ None。

    `rclone config dump`(JSON)を読む。INI を直接パースしないのは、設定が暗号化
    されている場合や値がエスケープされている場合に rclone 側の解釈と食い違うため。

    返すのは `{"access_token": ..., "endpoint": ...}`。rclone の `hostname` が EU
    (`eapi.pcloud.com`)を指していればそれに従う。
    """
    try:
        out = subprocess.run(["rclone", "config", "dump"],
                             capture_output=True, text=True, check=True, timeout=30)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None

    try:
        config = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None

    remotes = {name: cfg for name, cfg in config.items() if cfg.get("type") == "pcloud"}
    remote = remote or os.environ.get(RCLONE_REMOTE_ENV)
    if remote:
        remotes = {k: v for k, v in remotes.items() if k == remote}
    if not remotes:
        return None
    if len(remotes) > 1:
        raise RuntimeError(
            f"rclone に pcloud remote が複数あります: {sorted(remotes)}。"
            f"{RCLONE_REMOTE_ENV} でどれを使うか指定してください。"
        )

    name, cfg = next(iter(remotes.items()))
    token = json.loads(cfg["token"])["access_token"]
    hostname = cfg.get("hostname") or "api.pcloud.com"
    return {"access_token": token, "endpoint": f"https://{hostname}", "remote": name}


def _keychain_get(email: str) -> str | None:
    """macOS Keychain からパスワードを読む。無ければ None。"""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-a", email, "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def _keychain_set(email: str, password: str) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-a", email, "-s", KEYCHAIN_SERVICE,
         "-U", "-w", password],
        check=True, capture_output=True)


def _load_colab_secrets(names: tuple[str, ...] = ("PCLOUD_AUTH", "PCLOUD_ENDPOINT")) -> None:
    """Colab のシークレットを環境変数に載せる。既存の環境変数は上書きしない。

    `google.colab` への依存はこの関数だけに閉じ込める。「どのマシンか」ではなく
    「`userdata` が使えるか」を直接判定する(hostname による分岐は新しいマシン・VM・CI で
    誤判定するため使わない)。Colab 以外では ImportError になるので黙って何もしない。
    """
    try:
        from google.colab import userdata  # Colab 以外では ImportError
    except ImportError:
        return
    for name in names:
        if os.environ.get(name):
            continue
        try:
            os.environ[name] = userdata.get(name)
        except Exception:
            pass  # 任意なので、無ければ黙って諦める


@dataclass
class PCloud:
    """クライアント。OAuth トークンがあればそれを使い、無ければダイジェスト認証。

    ダイジェストは短命なので、リクエストのたびに作り直す(取得は TTL の間だけ使い回す)。
    """

    endpoint: str
    access_token: str | None = field(default=None, repr=False)
    email: str | None = None
    password: str = field(default="", repr=False)
    _digest: tuple[str, float] | None = field(default=None, repr=False)

    def _auth(self) -> dict:
        if self.access_token:
            return {"access_token": self.access_token}

        if not (self.email and self.password):
            raise RuntimeError("pCloud の資格情報がありません(トークンもパスワードも無い)。")

        now = time.monotonic()
        if self._digest is None or now - self._digest[1] > DIGEST_TTL:
            self._digest = (_call(self.endpoint, "getdigest")["digest"], now)
        digest = self._digest[0]
        hashed = hashlib.sha1(self.email.lower().encode()).hexdigest()
        return {
            "username": self.email,
            "digest": digest,
            "passworddigest": hashlib.sha1((self.password + hashed + digest).encode()).hexdigest(),
        }

    def call(self, method: str, *, files=None, **params) -> dict:
        return _call(self.endpoint, method, files=files, **self._auth(), **params)


def login(email: str | None = None, password: str | None = None) -> Path:
    """初回設定。リージョンを判定し、パスワードを Keychain に入れる。

    `security` コマンドの引数に一瞬だけパスワードが載るので、共有マシンでは
    代わりに環境変数 `PCLOUD_PASSWORD` を使うこと。
    """
    email = email or input("pCloud のメールアドレス: ").strip()
    password = password or getpass.getpass("pCloud のパスワード: ")

    errors = []
    for endpoint in API_ENDPOINTS:
        client = PCloud(endpoint=endpoint, email=email, password=password)
        try:
            info = client.call("userinfo")
        except PCloudError as e:
            errors.append(f"{endpoint}: {e}")
            continue

        store = token_store_path()
        store.write_text(json.dumps({"email": email, "endpoint": endpoint}))
        store.chmod(0o600)
        try:
            _keychain_set(email, password)
            where = "macOS Keychain"
        except Exception as e:
            where = f"保存できず ({type(e).__name__}) — 環境変数 PCLOUD_PASSWORD で渡す"
        print(f"ログイン成功 ({info.get('email', email)} @ {endpoint})")
        print(f"  設定: {store} / パスワード: {where}")
        return store

    raise RuntimeError("pCloud にログインできませんでした:\n  " + "\n  ".join(errors))


def init(verbose: bool = True) -> PCloud:
    """クライアントを返す。

    解決順は **環境変数 > Colab のシークレット > rclone の pcloud remote >
    保存済みの設定 + Keychain**。環境変数を最優先にするのは、一時的に別の資格情報を
    差し込めるようにするため。

    Colab のシークレットを見るのは、ブラウザのノートブックから使う場合に
    資格情報の出どころがそこしか無いため。`google.colab` への依存は
    `_load_colab_secrets()` に閉じている(任意扱いなので、無くても失敗しないし、
    userdata が使えない環境では黙って飛ぶ)。
    """
    if not os.environ.get("PCLOUD_AUTH"):
        _load_colab_secrets()

    endpoint = os.environ.get("PCLOUD_ENDPOINT")
    token = os.environ.get("PCLOUD_AUTH")
    email = os.environ.get("PCLOUD_EMAIL")
    password = os.environ.get("PCLOUD_PASSWORD")
    source = "環境変数"

    if not token and not (email and password):
        found = rclone_credentials()
        if found:
            token = found["access_token"]
            endpoint = endpoint or found["endpoint"]
            source = f"rclone ({found['remote']}:)"
        else:
            store = token_store_path()
            if not store.is_file():
                raise RuntimeError(
                    "pCloud の資格情報がありません。次のどちらかを一度だけ:\n"
                    "  rclone config                      # pcloud remote を作る(推奨)\n"
                    "  python pcloud_io.py login          # パスワードを Keychain に入れる\n"
                    f"(環境変数 PCLOUD_AUTH でも渡せる / 設定の保存先: {store})"
                )
            data = json.loads(store.read_text())
            email = email or data["email"]
            endpoint = endpoint or data.get("endpoint")
            password = password or _keychain_get(email)
            source = "Keychain"
            if not password:
                raise RuntimeError(
                    f"{email} のパスワードが Keychain にありません。"
                    "`python pcloud_io.py login` で入れ直すか、"
                    "環境変数 PCLOUD_PASSWORD で渡してください。"
                )

    client = PCloud(endpoint=endpoint or API_ENDPOINTS[0], access_token=token,
                    email=email, password=password or "")
    if verbose:
        info = client.call("userinfo")
        used, quota = info.get("usedquota", 0) / 1e9, info.get("quota", 0) / 1e9
        print(f"pCloud: {info.get('email', email)} ({used:.1f}/{quota:.0f} GB 使用) [{source}]")
    return client


def ensure_folder(client: PCloud, path: str) -> int:
    """フォルダを作って folderid を返す。途中の階層も作る。既にあれば何もしない。"""
    folder_id = 0  # 0 = ルート
    for part in [p for p in path.strip("/").split("/") if p]:
        folder_id = client.call(
            "createfolderifnotexists", folderid=folder_id, name=part)["metadata"]["folderid"]
    return folder_id


def ls(client: PCloud, path: str, *, missing_ok: bool = True) -> pd.DataFrame:
    """フォルダの中身を DataFrame (`name`/`isfolder`/`size`/`fileid`) で返す。

    処理済みを飛ばして再開するために使う(`name` の集合を見る)。
    """
    try:
        contents = client.call("listfolder", path=path)["metadata"].get("contents", [])
    except PCloudError as e:
        if missing_ok and e.code in (2005, 2055):  # directory does not exist
            return pd.DataFrame(columns=["name", "isfolder", "size", "fileid"])
        raise
    rows = [{"name": c["name"], "isfolder": c["isfolder"],
             "size": c.get("size"), "fileid": c.get("fileid")} for c in contents]
    return pd.DataFrame(rows, columns=["name", "isfolder", "size", "fileid"]).sort_values(
        "name", ignore_index=True)


def upload(
    client: PCloud,
    local_path: str | Path,
    remote_dir: str,
    *,
    name: str | None = None,
    folder_id: int | None = None,
    verbose: bool = False,
) -> dict:
    """ファイルを1つ上げる。同名があれば**上書き**する(再実行しても増えない)。

    `folder_id` を渡すとフォルダ解決を省く(ループで毎回作らせないため)。
    """
    local_path = Path(local_path)
    if folder_id is None:
        folder_id = ensure_folder(client, remote_dir)
    with open(local_path, "rb") as f:
        data = client.call(
            "uploadfile",
            folderid=folder_id,
            # renameifexists=0 で同名を置き換える。1 だと "file (1)" が増えていく。
            renameifexists=0,
            # nopartial=1: 途中で切れたアップロードを保存しない
            nopartial=1,
            files={"file": (name or local_path.name, f)},
        )
    meta = (data.get("metadata") or [{}])[0]
    if verbose:
        print(f"アップロード: {remote_dir}/{meta.get('name', local_path.name)}")
    return meta


def resolve(path: str) -> str:
    """相対パスを `PROJECT_ROOT` 基準の絶対パスにする。`/` 始まりはそのまま。

        "data/all.parquet" -> "/data/all.parquet"   (PROJECT_ROOT が "/" のとき)
    """
    # rstrip("/") は PROJECT_ROOT が "/" のときに "//data/..." にしないため。
    return path if path.startswith("/") else f"{PROJECT_ROOT.rstrip('/')}/{path.lstrip('/')}"


def file_link(client: PCloud, fileid: int | None = None, *, path: str | None = None) -> str:
    """ダウンロード用の直リンクを作る(有効期間つきの一時 URL)。

    `fileid` かパスのどちらかで指定する。パスなら一覧を引かずに済む。
    """
    if fileid is None and path is None:
        raise ValueError("fileid かパスのどちらかが要ります。")
    link = client.call("getfilelink", **({"fileid": int(fileid)} if fileid is not None
                                         else {"path": resolve(path)}))
    return f"https://{link['hosts'][0]}{link['path']}"


def read_parquet(
    client: PCloud,
    fileid: int | None = None,
    *,
    path: str | None = None,
    columns: list[str] | None = None,
    missing_ok: bool = True,
) -> pd.DataFrame:
    """pCloud 上の parquet を読む。ディスクには落とさない。

    メモリに載せてから読むので、大きすぎるファイルには向かない。ファイルごとに列構成が
    違う場合に備え、`columns` に無い列が含まれていても `missing_ok` なら実在する
    ものだけ読む(pyarrow は存在しない列を渡すと落ちる)。
    """
    import io

    import pyarrow.parquet as pq

    url = file_link(client, fileid, path=path)
    buffer = io.BytesIO(_request("get", url, timeout=TIMEOUT).content)
    if columns is None:
        return pd.read_parquet(buffer)

    available = set(pq.ParquetFile(buffer).schema_arrow.names)
    wanted = [c for c in columns if c in available]
    if not wanted and not missing_ok:
        raise KeyError(f"どの列も見つかりません: {columns}")
    buffer.seek(0)
    return pd.read_parquet(buffer, columns=wanted)


def upload_fileobj(
    client: PCloud,
    fileobj,
    remote_dir: str,
    name: str,
    *,
    folder_id: int | None = None,
) -> dict:
    """開いたファイル/バッファをそのまま上げる。ディスクを経由しない。

    **ここで再試行する。** `_request` は multipart を再送しない(ファイルハンドルを
    読み切った後なので、そのままでは送れない)が、巻き戻せば送り直せる。数千件を
    連続で上げると `ReadTimeout` が現れ、後半のファイルが大きくなるほど増えるため
    (実測で1ラウンド300件中21件)、seek し直して数回試す。`renameifexists=0` で
    上書きなので、サーバ側が受け取っていた場合に二重登録にはならない。
    """
    if folder_id is None:
        folder_id = ensure_folder(client, remote_dir)

    for attempt in range(RETRIES):
        try:
            data = client.call("uploadfile", folderid=folder_id, renameifexists=0,
                               nopartial=1, files={"file": (name, fileobj)})
        except requests.RequestException:
            if attempt == RETRIES - 1 or not fileobj.seekable():
                raise
            fileobj.seek(0)
            time.sleep(BACKOFF ** attempt)
        else:
            return (data.get("metadata") or [{}])[0]


def to_parquet(
    df: pd.DataFrame,
    path: str,
    *,
    cloud: PCloud | None = None,
    compression: str = "zstd",
    index: bool = False,
    verbose: bool = True,
) -> str:
    """DataFrame を pCloud に parquet で保存する。

        to_parquet(df, "data/all.parquet")

    パスは `PROJECT_ROOT`(既定 `/` = pCloud のルート)からの相対。`/` 始まりなら
    絶対パスとして扱う。途中のフォルダは作る。**同名は上書き**する。

    書き出しはメモリ上で行い、ローカルのディスクを使わない(Colab の VM でも、
    容量を気にせず大きな DataFrame を上げられる)。
    """
    import io

    client = cloud or init(verbose=False)
    full = resolve(path)
    remote_dir, name = full.rsplit("/", 1)

    buffer = io.BytesIO()
    df.to_parquet(buffer, compression=compression, index=index)
    buffer.seek(0)
    meta = upload_fileobj(client, buffer, remote_dir, name)
    if verbose:
        size = meta.get("size", buffer.getbuffer().nbytes)
        print(f"保存: {full} ({len(df):,} 行 / {size / 1e6:.1f} MB)")
    return full


def from_parquet(
    path: str,
    *,
    columns: list[str] | None = None,
    cloud: PCloud | None = None,
) -> pd.DataFrame:
    """pCloud に保存した parquet を読む。

        df = from_parquet("data/all.parquet")

    パスの扱いは `to_parquet()` と同じ。列構成が違うファイルにも使えるよう、
    `columns` に無い列が含まれていても実在するものだけ読む。
    """
    return read_parquet(cloud or init(verbose=False), path=path, columns=columns)


def main() -> None:
    """`python pcloud_io.py login` で初回設定、引数なしで接続確認。"""
    import sys

    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "login":
        login()
    elif command == "token":
        # Colab のシークレットに貼るため。画面に出るので、共有中の端末では使わない。
        found = rclone_credentials()
        if not found:
            raise SystemExit("rclone に pcloud remote がありません(`rclone config`)。")
        print(f"PCLOUD_AUTH={found['access_token']}")
        print(f"PCLOUD_ENDPOINT={found['endpoint']}")
    else:
        init()


if __name__ == "__main__":
    main()
