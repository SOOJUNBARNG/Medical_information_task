# medical-rag-db

ICD-10・DPC・院内医薬品集（PMDA添付文書の代替）を **事前に埋め込み済みの SQLite（`rag.db`）** にまとめ、
medical-ai-app v1.2 から完全オフラインで RAG 検索できるようにする医療知識ベースです。

- **ビルド時（開発機・ネット必要）**: データ取得 → チャンク化 → Ollama で埋め込み → `rag.db` 生成
- **実行時（病院PC・オフライン）**: クエリだけを埋め込み、`better-sqlite3` でコサイン類似度検索 → 上位K件をプロンプトに挿入

> [!WARNING]
> **既定の埋め込みモデル `nomic-embed-text`（v1.5）では日本語の検索精度が実用水準に届きません。**
> 2026-10-13 時点の検証では、ICD-10・DPC の標本クエリ7件すべてで期待チャンクが上位5件に入りませんでした
> （例:「腰痛」→ Q06.4 水脊髄、「急性心筋梗塞のICDコード」→ G37.5 同心円性硬化症）。
> パイプライン側の不具合ではないことは確認済みで、チャンク自身の本文で検索すると score 1.0 で1位になります。
> 768次元のまま多言語モデル（`nomic-embed-text-v2-moe` など）へ切り替える場合は、
> `EMBED_MODEL` を指定して再実行するだけで済みます（[モデルの切り替え](#埋め込みモデルの切り替え)）。

## データソース

| データ | 取得元 | 取得方法 | 件数（実績） | ライセンス |
|---|---|---|---|---|
| ICD-10（2013年版）基本分類 | 総務省 e-Stat 統計分類 | `01_fetch_icd10.py` が CSV を自動取得 | 15,339行（章22・中間分類279・コード15,038） | 政府標準利用規約 |
| DPC電子点数表（令和8年度） | 厚生労働省（[掲載ページ](https://www.mhlw.go.jp/stf/newpage_67729.html)） | `02_fetch_dpc.py` が Excel を自動取得 | 3,267分類 | 政府標準利用規約 |
| **院内医薬品集**（院内採用薬） | 院内データ `data/formulary/院内医薬品集.csv`（または .xlsx） | 手動で配置（**.gitignore 対象・コミット禁止**） | 569品目 | 院内限定 |
| PMDA 医療用医薬品添付文書（保留） | PMDAメディナビ「マイ医薬品集作成サービス」 | 手動ダウンロード → `03_fetch_pmda.py` が整理 | 未取得（取得時にエラーが続いたため院内医薬品集で代替） | PMDA利用規約 |

- e-Stat の ICD-10 CSV にあるのは「コード・日本語名」だけです。英語名・定義・包含/除外は空欄で、代わりに上位分類（`hierarchy` 列）を本文に含めます。
  ICD-10 の件数は要件の目安（約22,000）より少なくなります。
- DPC の取得URLは `ragdb/config.py` の `DPC_URL` にあります。新しい版が出たら差し替えてください（環境変数 `DPC_URL` でも指定できます）。
- 院内医薬品集は `data/formulary/院内医薬品集.csv` に置くと `make embed` で自動的に取り込まれます（場所は環境変数 `FORMULARY_PATH` で変更可）。
  **このリポジトリは公開設定のため、院内データは `.gitignore` で除外しています。`git add -f` しないでください。**
- PMDA は保留中です。一括ダウンロードの ZIP を `data/raw/source/pmda/` に置けば、パイプラインはそのまま取り込みます。

## セットアップ

Python 3.10 以上と Ollama が必要です。

```bash
pip install -r requirements.txt
```

```bash
ollama pull nomic-embed-text
```

## ビルド手順

```bash
make fetch
```

```bash
make embed OLLAMA_HOST_URL=http://127.0.0.1:11434
```

```bash
make build
```

```bash
make test
```

```bash
make install-app
```

| ターゲット | 内容 | 出力 |
|---|---|---|
| `make fetch` | 01〜03 を順に実行 | `data/raw/icd10.csv`, `data/raw/dpc.csv`, `data/raw/pmda/{承認番号}.xml` |
| `make embed` | 04: チャンク化と埋め込み（中断しても再実行すれば続きから再開） | `data/chunks.jsonl`, `data/embeddings.sqlite`, `data/embed_progress.jsonl` |
| `make build` | 05: `rag.db` を新規作成（既存ファイルがあれば上書きを確認。`--yes` で省略） | `data/rag.db` |
| `make test` | 単体テストと `rag.db` の検証 | — |
| `make test-js` | `rag.js` の検証（初回は `cd integration && npm install`） | — |

- 使う Python は `PYTHON=` で指定できます（例: `make test PYTHON=~/Desktop/myenv/bin/python`）。
- `OLLAMA_HOST_URL` の既定値はアプリ同梱 Ollama のポート `11435` です。開発機のシステム Ollama を使う場合は `11434` を指定します。

## パイプライン仕様

### 前処理（全データ共通）

全角スペースを半角にし、HTMLタグと制御文字を除去して、連続する空白・改行を1つのスペースに圧縮します。body が空の行はスキップします。
HTMLタグは既知のタグ名だけを除去するため、「悪性新生物<腫瘍>」「ジカ<Zika>ウイルス病」のような山括弧は残ります。

### チャンク分割（トークン数は tiktoken `cl100k_base` で計測）

| データ | 分割単位 | 最大 | オーバーラップ | body |
|---|---|---|---|---|
| ICD-10 | コード1件 | 512 | なし | `{name_ja}\n{definition}\n\n分類: {hierarchy}\n\n包含: …\n\n除外: …`（空の項目は省略） |
| DPC | 分類1件 | 512 | なし | `MDC{mdc_code} {mdc_name} / {dpc_code} {dpc_name}` の後に、512トークンに収まる範囲で入院期間・点数・対象ICD-10を付加 |
| PMDA | XMLセクション | 512 | 64トークン | セクション本文。512トークンを超えるものは文境界で分割し、50文字未満のセクションはスキップ |
| 院内医薬品集 | 1品目 | 512 | なし | `院内採用薬: {薬品名}（{区分}） / 規格 / 成分名 / 薬効分類 / 標準用法用量 / 備考 / 先発品` |

- PMDA の収録セクションは 警告・禁忌・効能又は効果・用法及び用量・副作用・薬物動態 です（`config.PMDA_SECTIONS`）。
  「10.1 併用禁忌」は相互作用の一部なので禁忌には含めません。
- 埋め込みに渡すテキストには、検索の手がかりとしてコードや薬剤名を前に付けます（例: `【販売名／用法及び用量】本文`）。
  `rag.db` の body には付けません。
- 必須部分だけで512トークンを超える DPC 分類（47件）は、末尾の手術名の列挙を切り詰めます。
- 院内医薬品集で成分名が空欄の行は、直前の行と薬品名の主部が同じとき（例: アレビアチン散 → アレビアチン錠）だけ成分名を引き継ぎます。
  PDFから転記した際の「(次ページへ続く)」は除去します。

### 埋め込み

- Ollama の `/api/embeddings` に1チャンクずつ送り、並列4・リトライ3回（2秒→4秒→8秒）で処理します。
- nomic-embed-text の前提に合わせ、文書には `search_document: `、クエリには `search_query: ` を付けます。値は `rag.db` の `meta` テーブルに保存され、`rag.js` が読み取ります。
- 1,000チャンクごとにチェックポイントを書きます。埋め込みを `data/embeddings.sqlite` に commit してから、`data/embed_progress.jsonl` に完了IDを追記します。
- チャンクIDは内容から決まるため、データを更新したときは本文が変わったチャンクだけが再計算されます。

### rag.db

```sql
CREATE TABLE chunks (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  category  TEXT NOT NULL,  -- 'icd10' | 'dpc' | 'pmda'
  code      TEXT,           -- ICDコード / DPCコード / 承認番号
  title     TEXT NOT NULL,  -- 疾病名 / 分類名 / 販売名
  section   TEXT,           -- PMDAセクション名
  body      TEXT NOT NULL,
  embedding BLOB NOT NULL   -- Float32[768] little-endian（3,072 bytes）
);
CREATE INDEX idx_category ON chunks (category);
CREATE INDEX idx_code ON chunks (code);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- 埋め込みモデル・接頭辞・出典など
CREATE TABLE keywords (chunk_id INTEGER NOT NULL, word TEXT NOT NULL);  -- 院内医薬品集の薬品名・成分名・先発品名
```

書き込みは WAL モードで1,000件ごとに commit します。完了時に `journal_mode=DELETE` に戻すので、配布物は `-wal` や `-shm` を伴わない単一ファイルになります。

## 検証（`make test`）

| 区分 | 内容 | 現状（2026-10-21、nomic-embed-text） |
|---|---|---|
| 単体テスト | 前処理・ICD階層・DPC解析・PMDA XML解析・院内医薬品集・分割/オーバーラップ・中断再開・DB生成 | 11件すべて成功 |
| データ量 | icd10 ≥ 20,000 / dpc ≥ 2,800 / formulary ≥ 500 / pmda（取得時のみ）、BLOB = 3,072 bytes | icd10 15,339 ✗ / dpc 3,267 ✓ / formulary 569 ✓ / BLOB ✓ |
| 検索精度 | `tests/eval_queries.json` の12クエリで上位5件・score下限を確認 | 薬品名を含む院内医薬品集の3件 ✓（キーワード一致）／ICD・DPC の7件と、薬品名を含まない院内医薬品集の2件 ✗ |
| 性能 | DB < 500MB、類似度計算 < 200ms | 76.1MB ✓ / 数ms ✓ |

- 件数の閾値は環境変数 `MIN_ICD10` / `MIN_DPC` / `MIN_PMDA` で変更できます。
- 標本クエリは `tests/eval_queries.json` で編集します。要件書の「脇誘痛」は「腰痛」、「脂質肾糧」は「脂質異常症」と解釈しています（**要確認**）。

### 性能の実測（MacBook、CPU）

| 指標 | 目標 | 実測 |
|---|---|---|
| 埋め込み計算（ICD-10＋DPC 18,606件） | — | 650秒（約28件/秒） |
| 埋め込み計算（全件11.5万件の推定） | < 8時間 | 約1〜1.5時間 |
| rag.js 検索（115,000件・クエリ埋め込みを除く） | < 200ms | 中央値 約70〜80ms |
| rag.js 検索（実DB 18,606件・クエリ埋め込み込み） | < 200ms | 93ms |
| rag.js 起動時の読み込み（115,000件） | — | 約1.7〜2.2秒（非同期・UIは止まらない） |

## medical-ai-app への組み込み（v1.2）

`integration/medical-ai-app/src/rag.js` を medical-ai-app の `src/rag.js` にコピーして使います。

```js
const { initRag, searchRag, buildRagPrompt, getAttribution } = require('./rag');

// Ollama 起動後に1回
await initRag({
  dbPath: app.isPackaged ? path.join(process.resourcesPath, 'rag.db')
                         : path.join(__dirname, '..', 'resources', 'rag.db'),
  ollamaBase: OLLAMA_BASE, // http://127.0.0.1:11435
});

// ollama-chat ハンドラ内で、最後のユーザー発話にコンテキストを挿入
const hits = await searchRag(userText, { topK: 5 });
const prompt = buildRagPrompt(userText, hits);
```

- **メモリ**: 埋め込みは int8 に量子化して保持します（115,000件で約88MB。Float32 のままだと約350MB）。上位候補だけを Float32 BLOB で再スコアします。
- **キーワード一致**: 質問に院内採用薬の薬品名・成分名・先発品名（例: 「ソラナックス」「ワルファリン」）や、`I21.0`・`010060xx99x00x` のようなコードが含まれていれば、そのチャンクを score 1.0 で先頭に置きます。
- **推測の抑止**: 院内医薬品集には効能・禁忌・副作用がないため、`buildRagPrompt` は「参考情報にない内容は推測しない」よう指示します。
- **アプリ側で必要な作業**:
  - `npm install better-sqlite3` と `@electron/rebuild`
  - `package.json` の `extraResources` に `resources/rag.db` を追加
  - 埋め込みモデル（nomic-embed-text 約274MB）を `resources/models` に同梱（`bundle-model.sh`）
  - 出典表示（`getAttribution()`）

## リポジトリ構成

```
medical-rag-db/
├── Makefile                     # make fetch / embed / build / test
├── requirements.txt
├── ragdb/                       # パイプライン本体
│   ├── config.py                #   パス・URL・モデル・閾値
│   ├── util.py                  #   前処理・トークン計測・ダウンロード
│   ├── icd10.py / dpc.py / pmda.py   # データソースごとの変換
│   ├── chunking.py              #   チャンク分割
│   ├── embed.py                 #   Ollama 埋め込み（並列・リトライ・再開）
│   ├── db.py                    #   rag.db 生成
│   └── search.py                #   検索（テスト・確認用）
├── scripts/01〜05_*.py          # 各ステップの実行スクリプト
├── tests/                       # pytest（単体テスト・rag.db 検証・標本クエリ）
├── integration/                 # rag.js と Node.js テスト
└── data/                        # 生成物（.gitignore）
```

## 埋め込みモデルの切り替え

```bash
ollama pull nomic-embed-text-v2-moe
```

```bash
EMBED_MODEL=nomic-embed-text-v2-moe make embed build test
```

次元が768以外のモデルは `EMBED_DIM` も指定します（BLOB サイズとテストの期待値が変わります）。
接頭辞を使わないモデルは `EMBED_DOC_PREFIX=` と `EMBED_QUERY_PREFIX=` を空にします。
進捗はモデルごとに管理するため、既存モデルの計算結果は消えません。

## 出典表示

`rag.db` の `meta.attribution` に出典文言を格納しています。PMDA のデータを含める場合は、アプリ上で出典を表示してください。
