"""パス・取得元URL・埋め込み設定を一元管理する。

値は環境変数で上書きできる（例: OLLAMA_HOST_URL=http://127.0.0.1:11434 make embed）。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("RAGDB_DATA_DIR", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
DB_PATH = Path(os.environ.get("RAGDB_DB_PATH", DATA_DIR / "rag.db"))

# ── 01〜03 の出力（data/raw/）──────────────────────────────────
ICD10_CSV = RAW_DIR / "icd10.csv"
DPC_CSV = RAW_DIR / "dpc.csv"
PMDA_XML_DIR = RAW_DIR / "pmda"  # {shonin_no}.xml
# 取得した元ファイル（変換前）
ICD10_SOURCE = RAW_DIR / "source" / "estat_icd10_2013.csv"
DPC_SOURCE = RAW_DIR / "source" / "dpc_tensuhyo.xlsx"
# PMDAの一括ダウンロード（ZIP/XML）を手動で置く場所
PMDA_DOWNLOAD_DIR = RAW_DIR / "source" / "pmda"
# 院内医薬品集（院内データ。.gitignore 対象）。CSV または Excel。
FORMULARY_PATH = Path(os.environ.get("FORMULARY_PATH", DATA_DIR / "formulary" / "院内医薬品集.csv"))

# ── 04 の出力 ─────────────────────────────────────────────────
CHUNKS_JSONL = DATA_DIR / "chunks.jsonl"
EMBED_PROGRESS = DATA_DIR / "embed_progress.jsonl"
# 計算済み埋め込みの退避先（chunk_id → BLOB）。05 はここから rag.db を作る。
EMBED_STORE = DATA_DIR / "embeddings.sqlite"

# ── 取得元 ────────────────────────────────────────────────────
# ICD-10（2013年版）基本分類: e-Stat 統計分類のCSVダウンロード（全階層）
ICD10_URL = os.environ.get(
    "ICD10_URL",
    "https://www.e-stat.go.jp/term/download"
    "?bKbn=40&kaiteiCode=03&charset=UTF-8&bom=0&searchMethod=keyword&searchWord="
    "&komokuSearchFlg=1&info1SearchFlg=&info2SearchFlg=&info3SearchFlg="
    "&info4SearchFlg=&info5SearchFlg=&info6SearchFlg=",
)
# DPC電子点数表（令和8年度改定・正式版 令和8年8月12日更新）
# 掲載ページ: https://www.mhlw.go.jp/stf/newpage_67729.html （第6 DPC）
# 更新版が出たらこのURLを差し替える。
DPC_URL = os.environ.get(
    "DPC_URL", "https://www.mhlw.go.jp/content/12400000/001737745.xlsx"
)
HTTP_USER_AGENT = "medical-rag-db/1.0 (build script)"

# ── 埋め込み ──────────────────────────────────────────────────
# 既定はアプリ同梱Ollamaのポート(11435)。開発機のシステムOllamaを使う場合は
# OLLAMA_HOST_URL=http://127.0.0.1:11434 を指定する。
OLLAMA_HOST = os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11435")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "768"))
# nomic-embed-text はタスク接頭辞を前提に学習されている（付けないと検索精度が落ちる）。
# 接頭辞は埋め込み計算時のみ付与し、body には保存しない。rag.js は meta テーブルから読む。
_NOMIC = EMBED_MODEL.startswith("nomic-embed-text")
DOC_PREFIX = os.environ.get("EMBED_DOC_PREFIX", "search_document: " if _NOMIC else "")
QUERY_PREFIX = os.environ.get("EMBED_QUERY_PREFIX", "search_query: " if _NOMIC else "")

EMBED_PARALLEL = int(os.environ.get("EMBED_PARALLEL", "4"))
EMBED_RETRIES = 3
EMBED_RETRY_BASE_SEC = 2.0  # 2s → 4s → 8s
EMBED_TIMEOUT_SEC = 30.0
CHECKPOINT_EVERY = 1000

# ── チャンク ──────────────────────────────────────────────────
MAX_TOKENS = 512
PMDA_OVERLAP_TOKENS = 64
PMDA_MIN_BODY_CHARS = 50

# PMDAで収録するセクション（正規名 → 見出し・タグ名の判定語）
PMDA_SECTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "警告": {"headers": ("警告",), "tags": ("warnings",)},
    "禁忌": {"headers": ("禁忌",), "tags": ("contraindications", "contraindication")},
    "効能又は効果": {
        "headers": ("効能又は効果", "効能・効果", "効能効果", "適応症"),
        "tags": ("indicationsorefficacy", "indications", "efficacy"),
    },
    "用法及び用量": {
        "headers": ("用法及び用量", "用法・用量", "用法用量"),
        "tags": ("infodoseadmin", "dosageandadministration", "dosage"),
    },
    "副作用": {"headers": ("副作用",), "tags": ("adverseevents", "adversereactions", "sideeffects")},
    "薬物動態": {"headers": ("薬物動態",), "tags": ("pharmacokinetics",)},
}

# ── 検証（make test）─────────────────────────────────────────
MIN_COUNTS = {
    "icd10": int(os.environ.get("MIN_ICD10", "20000")),
    "dpc": int(os.environ.get("MIN_DPC", "2800")),
    # PMDA は一括ダウンロードを断念し、院内医薬品集で代替（2026-10-21）。取得できた場合のみ検証する。
    "pmda": int(os.environ.get("MIN_PMDA", "0")),
    "formulary": int(os.environ.get("MIN_FORMULARY", "500")),
}
MAX_DB_BYTES = 500 * 1024 * 1024
MAX_SEARCH_MS = 200

ATTRIBUTION = {
    "icd10": "出典: 総務省 e-Stat「疾病、傷害及び死因の統計分類（ICD-10(2013年版)準拠）」（政府標準利用規約）",
    "dpc": "出典: 厚生労働省「診断群分類（DPC）電子点数表」（政府標準利用規約）",
    "pmda": "出典: 独立行政法人医薬品医療機器総合機構（PMDA）医療用医薬品添付文書情報",
    "formulary": "出典: 院内医薬品集（院内採用薬）",
}
