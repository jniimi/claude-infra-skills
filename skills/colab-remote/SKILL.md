---
name: colab-remote
description: Run code on Google Colab from outside the browser. Use when driving a remote Colab session with the `colab` CLI (google-colab-cli) — `colab new/exec/run/upload/ssh/sessions/stop`, GPU/TPU/A100 or `--high-mem` runtimes, `--env` variables, pushing local source to the VM, passing credentials without Drive — or when writing the bootstrap cell of a Colab notebook that installs a repo with uv, does an editable install, and pins pandas/numpy/pyarrow to the preinstalled versions. Covers the failure modes that are hard to diagnose: exec exiting 0 on exception, `--env` secrets landing in plaintext history, `colab update --install` downgrading a git install, stale modules after editable install, `${name}` not expanding, drivemount blocking on /dev/tty.
---

# Colab をリモート/ノートブックから動かす

経路は2つある。**先にどちらかを選ぶ**:

| | `colab` CLI(①) | ブラウザのノートブック(②) |
|---|---|---|
| 用途 | バッチ実行、エージェントからの自動化、手を離して回す | 対話的な探索、出力を見ながら書く |
| ブラウザ | 不要 | 必要 |
| `google.colab.userdata` | **使えない**(後述) | 使える |
| 資格情報 | ローカルから `colab upload` で渡す | Colab のシークレット |

Claude Code のセッションから回すなら基本は①。人間が図を見ながら試行錯誤するなら②。
**①と②は合流できない。** `colab url` は空の scratchpad を開くだけで CLI のセッションには繋がらない(upstream issue #24、未修正)。詳細と代替は後述の「ノートブックとして残す」。
どちらも共通の前提として、**依存は `uv.lock` から固定版を入れ、Python/pandas/numpy は Colab のプリインストール版に合わせる**(後述の「uv と Colab のバージョン整合」)。

---

## ① colab CLI でリモートから動かす

Google 公式の [`google-colab-cli`](https://github.com/googlecolab/google-colab-cli)(`colab` コマンド)。ローカルの shell から Colab のセッションを立て、任意のコードをカーネルで実行できる。

### インストール(このスキルの検証済み構成: v0.7.1)

```bash
uv tool install --force "git+https://github.com/googlecolab/google-colab-cli@v0.7.1" \
  --with "jupyter-kernel-client==0.8.0"
colab version    # -> Version: 0.7.1
```

- **PyPI 版は使わない。** タグは v0.7.1 まで出ているが、**PyPI の最新は 0.6.0 のまま**で `--high-mem` も `--env` も `colab ssh` も入っていない(2026-09-21 時点)。PyPI が 0.7 以降を出したら `uv tool install google-colab-cli --with ...` に戻してよい。
- **`jupyter-kernel-client` の上限指定は今も必須。** PyPI の 1.x は `KernelClient` シンボル自体を廃止しており、入れると CLI が起動時に落ちる。`<1` でもよいが、実績があるのは 0.8.0。
  (upstream の `uv.lock` は googlecolab フォークの `ColabKernelClient` を使うが、`uv tool install` では `tool.uv.sources` が効かず PyPI 版が入る。CLI 側は両対応済み。)
- **`colab update --install` を実行しないこと。** 中身は `uv tool install -U google-colab-cli` で、**git 版を PyPI 版に置き換える**(今は 0.6.0 へのダウングレードになり、`--with` のピンも消える)。CLI は起動のたびにバックグラウンドで更新チェックをして1日1回バナーを出すが、無視してよい。

### コマンドの見取り図

| | |
|---|---|
| セッション | `colab new -s <name> [--gpu A100\|T4\|L4\|G4\|H100] [--tpu v5e1\|v6e1] [--high-mem]` / `sessions` / `status -s` / `stop -s` / `restart-kernel -s` |
| 実行 | `colab exec -s <name> [-f file.py\|file.ipynb] [--timeout N] [--env K=V]` / `colab run script.py [args...]` / `repl` / `console` |
| ファイル | `colab upload -s <name> LOCAL REMOTE` / `download` / `ls` / `rm` / `edit` |
| その他 | `colab ssh -s <name>` / `colab install -s <name> [-r req.txt]` / `colab log -s <name> -o out.ipynb` / `colab auth` / `colab drivemount` / `colab whoami`(ヘルプ非表示だが動く) / `colab url`(**壊れている**、後述) |

`colab readme` / `colab skill` で同梱ドキュメントを表示できる。認証は既定が `oauth2`(v0.6.0 からローカルの callback サーバではなく**リモートのコピペ方式**なのでヘッドレスでも通る)。GCP の ADC を使いたいときだけ `--auth adc`。

### ドライバの設計指針

`colab` を subprocess で叩く薄いラッパを1本用意し、`up` / `run` / `status` / `stop` のサブコマンドにまとめると扱いやすい。`up` は**冪等**にする(既存セッションがあれば作り直さない — `colab exec` はカーネルの状態を保つので、作り直すと入れた依存もクライアントも失う)。`up` がやることは概ねこの順:

1. セッションを用意(無ければ `colab new`)
2. **ローカルの作業ツリーを tar で固めて `colab upload`**(`git clone` させない)
3. 短命の資格情報を JSON で `colab upload`
4. ブートストラップスクリプトを `colab exec` で流す(展開 → 依存インストール → クライアント生成)

**git clone ではなく tar を送る**のが要点。GitHub トークンを VM に渡さずに済み、コミットしていない編集もそのまま持っていける。送るファイルは `git ls-files` + `--others --exclude-standard` で拾えば、ignore 済みの巨大データを巻き込まない。

### `colab exec` の罠

- **例外が出ても終了コードは 0**(v0.7.1 で再確認)。outputs を検査していない(終了コードを伝播するのは `colab run` だけ)。そのままだと失敗を成功と報告する。**送るスクリプトの末尾に番兵の print を足し、その文字列が出力に現れたかどうかで完走を判定する**こと。
- **既定タイムアウトは 30 秒。** 依存のインストールは数分かかるので `--timeout` が要る(ブートストラップは 900 秒程度)。subprocess 側のタイムアウトは CLI の値より余裕を持たせる。
- **`sys.argv` を渡さない**(渡すのは `colab run`)。exec で流すスクリプトのパラメータは、引数ではなく**ファイル冒頭の定数**として書く。
- **カーネルの状態は exec をまたいで残る。** ブートストラップで作った変数は後続のスクリプトからそのまま見える。ただしカーネル再起動で消えるので、タスクスクリプトの冒頭は冪等な `client = ensure_client()`(キャッシュ付き)にしておく。
- **exec に渡したコードと出力は、ローカルの `~/.config/colab-cli/history/<session>.jsonl` に平文で残る。** 秘密情報を exec するコードに書かない・print しない。値の受け渡しは `colab upload`(履歴にはパスしか残らない)で行う。

### `--env KEY=VALUE`(v0.7.0 で追加)— 秘密情報には使わない

`colab exec` / `colab run` に付けられるが、実装は**実行するコードの先頭に `os.environ['KEY'] = 'VALUE'` を差し込むだけ**。その合成後のコードがそのまま履歴 jsonl に書かれるので、**渡した値は平文でローカルに残る**(実測で確認済み)。非秘密の設定値(`RUN_ID`, `DATASET=small` など)に限って使い、トークン類は従来どおり `colab upload` した JSON から読ませる。

### `colab run`(使いどころが `exec` とは違う)

```bash
colab run --gpu A100 --high-mem --keep -s trainer train.py --epochs 10
```

- **終了コードを伝播する唯一の経路。** スクリプト側の `sys.exit(n)` は CPython と同じ `n` になり、それ以外の未捕捉例外は非ゼロになる。番兵 print が要らない。
- スクリプト本体はローカルから読んで送られ、`sys.argv` と `__name__ == "__main__"` が正しく立つ。
- **ただし毎回まっさらな VM を確保する。** `-s <name>` を付けても既存セッションの再利用はしない(名前が付くだけ)。`--keep` を付ければ終了後も残り、以降は `colab exec -s <name>` で同じカーネルに繋げる。
- 既定 `--timeout` は exec と同じく 30 秒。

したがって「重いセットアップを1回だけして何度も回す」用途は今も `new` + `upload` + `exec`(+ 番兵)で、「単発のスクリプトを投げて結果と終了コードだけ欲しい」用途は `run` が素直。

### `colab ssh`(v0.7.0 で追加、未検証)

SSH-over-WebSocket で VM に shell が取れる。`--proxy-mode` は OpenSSH の `ProxyCommand` として使えるので、VS Code Remote-SSH などの IDE リモート開発が通る。

```
Host colab
  ProxyCommand /Users/<user>/.local/bin/colab ssh --proxy-mode -s <name> [--gpu T4] [--rm]
  User root
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
```

- **鍵は ed25519 か ecdsa。`ssh-rsa` はサーバ側で拒否される。** 鍵が無いと `no SSH public key found in ~/.ssh/` で止まる(`ssh-keygen -t ed25519`)。
- `ProxyCommand` には `colab` の**絶対パス**を書く(非ログインシェルで PATH が通らない)。
- サーバ側が `/colab/ssh` を提供していないランタイムでは **HTTP 404** になる。その場合は諦めて `exec` 経路に戻る。
- `-s` 無しの `colab ssh` はセッションが無ければ自動で立てる。`--rm` は**自動で立てた場合のみ**終了時に停止する(既存セッションは消さない)。

### ノートブックとして残す(ブラウザで開きたいとき)

**`colab url` は使えない。** 出てくるのは `notebooks/empty.ipynb?dbu=...` で、開いても空の scratchpad がランタイム未接続で出るだけ。「接続」を押すと CLI のセッションではなく**新しい CPU VM を確保してしまう**(upstream issue #24、2026-05-24 起票・未修正、2026-09 時点で fix 待ち)。CLI のセッションはブラウザ側からは「不明なノートブック」としてしか見えない。

代わりに、**実行履歴をノートブックに書き出す**:

```bash
colab log -s <name> -o session.ipynb    # .ipynb / .md / .txt / .jsonl
```

`exec` で流したセルが**出力込みで**順に並んだ `.ipynb` が得られる(先頭にセッション情報の markdown セル付き)。ローカルの Jupyter/VS Code で開けるし、Drive や GitHub に置けば Colab でも開ける — ただしその場合ランタイムは新規になる。

逆向き(`.ipynb` を投げて回す)は `colab exec -s <name> -f nb.ipynb` で、セルごとに実行しつつローカルに `nb_output.ipynb` を書き出す。**「ノートブックを正本にして CLI で回し、結果を ipynb で受け取る」のが現実的な運用**で、ブラウザ UI にライブで合流する経路は今のところ無い。

### セッションが生きているかどうか

- **アイドルのセッションはサーバ側で回収される**(CPU で数十分〜)。ローカルの記録だけが残るので、**送る前に生存確認する**(`colab exec` で `print('__alive__')` を流して返ってくるか、`colab status -s <name>`)。死んだセッションへの `upload` は `File or directory not found` という分かりにくい失敗になる。回収されていれば作り直す。
- **`colab sessions` の `[?]` はローカルに記録が無いサーバ側の割り当て。** 名前が無いので `exec` も `stop` もできない。Web UI から切るか、24時間の keep-alive 上限で回収されるのを待つ。
- **ローカルで Ctrl-C しても、リモートのカーネルは走り続ける。** exec を止めても VM 側の処理は継続し、次の exec が詰まって時間切れになる。止めるには `colab restart-kernel -s <session>`(カーネルの状態は消えるので `up` で入れ直す)。
- **確保に失敗すると "Allocation refused (precondition failed)"**(HTTP 412)。セッションの持ちすぎか、そのアクセラレータの一時的な枠不足。`colab stop` で空けるか、別のアクセラレータにする。

### CLI セッション固有の制約

- **`google.colab.userdata` が使えない。** userdata の解決はフロントエンドへの `colab_request` 経由だが、`colab` CLI がハンドラを持っているのは今も Drive マウントだけで、シークレットには応答が返らない。**CLI 経路の資格情報はローカルから渡す**(`colab upload` で JSON を置き、VM 側は「既存の環境変数 > その JSON」の順で解決する)。ノートブック経路のコードは `userdata` を読む実装のまま残しておけば、同じ関数が両方で動く。
- **`colab drivemount` は毎回ブラウザでの許可を求め、`/dev/tty` から Enter を待つ。** Drive の資格情報は VM ごとの ephemeral 発行なので、前のセッションで許可済みでも新しいセッションでは効かない。**非対話シェルでは待たずに `mount failed` になる**。どうしても要るなら対話的な端末から実行する(Claude Code のセッションなら `! colab drivemount -s <session> /content/drive`)。後述の設計にすれば、そもそも Drive が要らなくなる。
- **`--high-mem`(high-RAM)は v0.7.0 からリリースに入った**が、PyPI は 0.6.0 のままなので上記の git インストールが要る。`colab new -s x --high-mem` → `colab status -s x` が `Shape: High-RAM` になれば効いている。**Colab Pro / Pro+ が必要**で、**単一シェイプしか無いアクセラレータ(L4, TPU v5e1/v6e1)では警告して無視される**。CPU・T4・A100 では有効。

### 資格情報の渡し方(Drive が要らなくなる設計)

プロバイダを問わず効く原則: **VM へ渡すのは短命のアクセストークンだけにし、ローテートする refresh token は渡さない。** 可能ならスコープも落として発行する(読み取り専用 + ダウンロード許可など)。

理由は refresh token が**使うたびにローテートする**方式だと、トークンストアを VM に持ち出した瞬間に「渡した先が更新するとローカルが無効になる」からで、これを回避しようとすると双方から書ける永続領域(= Drive)を共有する羽目になる。アクセストークンなら期限切れになるだけで渡した側は何も壊れない。結果、**VM に永続化するものが無くなり、Drive も毎セッションのブラウザ許可も不要**になる。

- 期限切れは、タスクを流すたびにローカルで発行し直して push することで避ける(API 1往復)。単発の実行が有効期限を超え、その終盤に I/O が残る場合だけ切れるので、そのときだけトークンを押し直すサブコマンドを用意する。
- スコープを絞るときは**一覧用の権限だけでは足りない**ことが多い(メタデータは通るがダウンロードが 403)。必要な操作を実際に一度通してから確定させる。
- トークンストアの中身を運ぶ必要が出たら **JSON で export/import する**。dbm などのバックエンドファイルを直接コピーしない(実装が環境依存で可搬でない)。
- ストレージ側の読み書き自体については、pCloud を使うなら別スキル `pcloud-io` を参照。

### タスクスクリプトの形

```python
# task.py — ドライバの `run task.py` で流す(= colab exec)
from mypkg import colab as pc

client = pc.ensure_client()      # 冪等。2回目以降はキャッシュを返すだけ
...                              # パラメータは argv ではなくここに定数で書く
```

### パッケージを編集したあとの罠

**editable install はファイルを差し替えるだけで、import 済みモジュールは入れ替わらない。** `up` を撃ち直しても、カーネルに残った古いモジュールがそのまま動き続ける(トレースバックの行番号だけ新しくなって混乱する)。ブートストラップは import 前に `sys.modules` から自パッケージを落とす:

```python
for name in [m for m in sys.modules if m == "<pkg>" or m.startswith("<pkg>.")]:
    del sys.modules[name]
```

編集後にタスクを流すときは、ソースを送り直してブートストラップを通す経路(`run --sync` 相当)を使う。

---

## ② ブラウザのノートブックで対話的に使う

実装は `src/<pkg>/` に置き、ノートブックからは import して使う。**`uv sync` が作る venv はノートブックのカーネルからは見えない**ので、Colab では `uv pip install --system` で**カーネル自身の Python** に lock 由来の固定版を入れる。これで対話的な分析と環境の統一が両立し、`sys.path` 操作も `%cd src` も要らない。

ブートストラップは**何も import する前の最初のセル**に置く(後から差し替えるとランタイム再起動が要る)。**GitHub トークンを URL に埋め込まないこと** — 埋め込むと `.git/config` に残り、clone 失敗時のエラー出力にもそのまま出て `.ipynb` に保存される。`GIT_ASKPASS` 経由なら URL・コマンドライン・設定ファイルのいずれにも載らない。

```python
import os, sys, site, importlib
from google.colab import userdata

OWNER, REPO = '<owner>', '<REPO>'
repo_dir = f'/content/{REPO}'
URL = f'https://github.com/{OWNER}/{REPO}'       # トークンを含まない clean URL

# トークンは環境変数にのみ置き、git へは askpass 経由で渡す
os.environ['GITHUB_TOKEN'] = userdata.get('GITHUB_TOKEN')
os.environ['GIT_TERMINAL_PROMPT'] = '0'          # 認証失敗時に入力待ちで固まらず即エラー
askpass = '/content/.git-askpass.sh'             # repo の外に置く(commit されないように)
with open(askpass, 'w') as f:
    f.write('#!/bin/sh\n'
            'case "$1" in\n'
            '  Username*) echo "x-access-token" ;;\n'
            '  Password*) echo "$GITHUB_TOKEN" ;;\n'
            'esac\n')
os.chmod(askpass, 0o700)
os.environ['GIT_ASKPASS'] = askpass

%cd /content
if not os.path.exists(f'{repo_dir}/.git'):
    !git clone $URL
%cd $repo_dir
!git remote set-url origin $URL                  # 以前のトークン入り URL の掃除も兼ねる
!git pull --ff-only origin main

# lock から固定版 requirements を生成し、カーネル自身の Python へ入れる
PY = sys.executable                              # 取り違え防止にカーネルを明示
!pip install -q uv
!uv export --frozen --no-dev --no-hashes --no-emit-project -o /tmp/requirements.txt
!uv pip install --system --python $PY -q -r /tmp/requirements.txt
!uv pip install --system --python $PY -q --no-deps -e .   # 本体を editable で(--no-deps で lock の版を守る)

# editable install が置いた .pth は起動済みカーネルには自動反映されないので取り込む
site.main()
importlib.invalidate_caches()

import <pkg>
print('bootstrap OK:', <pkg>.__file__)           # ここが出れば環境構築は完了
```

以降は普通のセルで書ける。`src/` 以下を編集したら `importlib.reload` かランタイム再起動で反映される。スクリプトとして流したいときは `!uv run python -m <pkg>.<module>`(こちらは venv 側で走るので、カーネルとは別環境になる点に注意)。

資格情報は Colab のシークレットに登録し、`userdata.get()` を読む処理は**1つの関数に閉じ込める**(`google.colab` への import をそこだけにすると、ローカル・CLI セッション・ノートブックで同じコードが動く)。

### ノートブックを書くときの注意

- IPython の変数展開は `$name` と `{name}` は解釈するが、**`${name}` は展開されない**(`/content/${REPO}` は `/content/$REPO` という実在しないパスになる)。magic や `!` に変数を渡すときは `$name`。
- **`%cd` は `if` の外に置く。** 分岐の中に書くと初回 clone のときだけ cwd が変わり、2回目以降と食い違って以降のセルが失敗する。
- **editable install の `.pth` はインタプリタ起動時にしか処理されない。** 起動済みカーネルに反映するには install 後に `site.main()` + `importlib.invalidate_caches()` が要る。飛ばすと、依存は入っているのに `ModuleNotFoundError: No module named '<pkg>'` になる。`%cd <repo>/src` でも import は通るが、cwd を戻すと壊れる場当たりなので使わない。
- 素の `.py`(= `colab exec` で送るスクリプト)では `%cd` や `!` は使えない。逆に `${name}` 事故も起きないので、ブートストラップは `.py` にしておくと安全。

---

## uv と Colab のバージョン整合(両経路に共通)

- **requirements.txt を手書きしない。** 真実は `uv.lock` 一箇所に置き、`uv export --frozen --no-dev --no-hashes --no-emit-project` で生成する。生成物はコミットしない。
- **Python / pandas / numpy は Colab のプリインストール版に完全固定する。** 実測値を確認してから `uv add` し、`.python-version` と `requires-python` も揃える。ずれていると Colab 側で再インストールが走り、同梱ライブラリを壊す。(2026-09 時点の CPU ランタイムは Python 3.13.15 / x86_64。)
- **pyarrow を runtime 依存に入れない**(dev グループへ)。バージョンを上書きすると `pyarrow.lib.IpcReadOptions size changed` のバイナリ非互換が起き、pandas は `except ImportError` しか捕まえないので **`import pandas` ごと落ちてカーネルの再起動が必要になる**。`to_parquet()` などは関数内で遅延 import しておく。ブートストラップを `--no-dev` で回せば dev グループは入らない。
- 大きな依存(torch など)も optional group に切り出し、**Colab のプリインストール版を使う**。毎セッション数 GB の再取得を避けられる。
- GPU は必要なときだけ(`--gpu A100`)。スモークテストは CPU か弱い GPU で十分。CPU のみで RAM が要るケースは `--high-mem`。
