# claude-infra-skills

Claude Code 用の plugin。自分の実行環境への「接続」の知識を、プロジェクト横断で
使えるスキルとしてまとめたもの。特定のプロジェクトの README に埋もれると
他の repo から参照できないので、ここに切り出してある。

## 収録スキル

| skill | 内容 |
| --- | --- |
| `colab-remote` | Google Colab を使う2経路 — ローカルから `colab` CLI でセッションを立てて流す / ブラウザのノートブックで対話的に使う。uv での環境統一と、踏むと原因が分かりにくい落とし穴。 |
| `pcloud-io` | pCloud への読み書き。単体で動く `pcloud_io.py` を同梱。認証の解決順(環境変数 > Colab userdata > rclone > Keychain)と pCloud API の癖。 |

どちらも description にマッチしたときだけ読み込まれるので、入れておいても
普段のコンテクストは増えない。

## インストール

```
/plugin marketplace add jniimi/claude-skills-marketplace
/plugin install infra-skills@jniimi-claude-skills
```

マシンごとに1回でよい。以降どの repo でも効く。

## 方針

- **どの repo でも同じように成り立つ手順と制約だけを書く。** 個別のプロジェクトの
  事情は持ち込まない。
- **落とし穴の密度を優先する。** 素直に書けば動くことは書かない。一度踏んで
  原因が分かりにくかったことを書く。
