# run_sheet_c

スプレッドシート (Google フォームの回答など) に書かれた **C 言語の回答をまとめてコンパイル・実行し、期待出力と比べて採点する** ツールです。
学生のコードは、ネットワークのない隔離コンテナで実行します。

## 構成

| サービス | 役割 |
|---|---|
| `web` | 画面。ファイルのアップロード、結果表示、点数の手動修正、前回との差分表示。**コードは実行しない** |
| `worker` | 提出コードのコンパイルと実行 (gcc 11.3.0)。`network_mode: none`、メモリ 1GB、権限なしユーザー |

web と worker は、共有ボリューム `work` を介してジョブをやり取りします (web が `jobs/<id>/` に置き、worker が拾って結果を書く)。

## 使い方

```bash
cp .env.example .env     # ROOT_PASSWORD などを編集 (必須)
docker compose up -d --build
```

ブラウザで http://localhost:8000 を開きます (Basic 認証)。

| ユーザー | 権限 |
|---|---|
| `ROOT_USER` (既定 `root`) | 全操作 |
| `SA_USER` (既定 `sa`) | 閲覧と点数の修正のみ。`SA_PASSWORD` が空なら無効 |

### 入力ファイル

- **学生の回答** (`.xlsx` / `.csv` / `.tsv`)
  - 1 行目: E 列以降に問題番号
  - B 列: メール (あれば識別に使う) / C 列: 識別番号 / D 列: 氏名
  - E 列以降: 各問題の C ソース
  - A 列が「タイムスタンプ」なら、同じタグの前回の実行との比較に使う
- **問題の入出力** (`.json` / `.xlsx` / `.csv` / `.tsv`)
  - JSON: `{"1": [{"in": "3\n", "out": "27\n"}], ...}`
  - CSV: `問題番号,入力,期待出力` (1 行 1 ケース)
  - 画面の `/template.xlsx`、`/template.csv` からテンプレートを取得できます

### 採点

| 点 | 条件 |
|---|---|
| 1 | 全ケースで出力が期待出力と完全一致 |
| 0.5 | コンパイルできたが出力が違う (出力なし・異常終了・時間切れも含む) |
| 0 | コンパイルエラー |
| なし | 期待出力がなく判定できない |

改行コード (CRLF / LF) と、出力末尾の改行の有無だけは区別しません。空白や桁数などは区別します。
点数は画面から手動で修正できます。

### 実行を同じタグで繰り返す

アップロード時にタグを付けると、同じタグの直前の実行と比べて「追加 / 更新 / 削除」を表示します。
各タグの最新 2 件より古い実行は自動でゴミ箱に移ります (ファイルは残り、復元できます)。

## データの保存先

| 場所 | 内容 |
|---|---|
| Docker ボリューム `run_sheet_c_work` | `jobs/<id>/` (回答・入出力・結果)、`overrides.json` (手動修正した点数)、`tags.json` (タグのコメント) |
| `./output/jobs/<タグ>/<id>/` | 実行が完了するたびに worker が書き出す `meta.json` / `result.json` / `result.csv` |

ボリュームを丸ごとローカルに控えるには:

```bash
mkdir -p output/volume-snapshot
docker run --rm --entrypoint sh --user root \
  -v run_sheet_c_work:/work:ro -v "$PWD/output/volume-snapshot":/dst \
  run-sheet-c-web -c 'cp -a /work/. /dst/ && chown -R '"$(id -u):$(id -g)"' /dst'
```

> `overrides.json` と `tags.json` は自動では書き出されません。定期的に上の方法で控えてください。

## ライブラリの追加

- web (Python): [docker/requirements-web.txt](docker/requirements-web.txt)
- worker (apt): [docker/apt-worker.txt](docker/apt-worker.txt)

編集後は `docker compose up -d --build` で反映します。

## ファイル

| ファイル | 内容 |
|---|---|
| `app.py` | Web 画面 (Flask) |
| `worker.py` | ジョブの監視と実行 |
| `run_sheet_c.py` | コンパイル・実行のエンジン |
| `scoring.py` | 点数づけ、学生の並べ方 |
| `diffview.py` | 前回との差分 |

## 公開しないもの

`.env` (パスワード)、`data/`、`output/` (学生の回答と成績) は `.gitignore` で除外しています。**リポジトリに入れないでください。**
