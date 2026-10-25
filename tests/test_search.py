"""生成済み data/rag.db の検証（make test）。

1. データ量検証   … カテゴリ別チャンク数・BLOBサイズ
2. 検索精度検証   … tests/eval_queries.json の標本クエリで上位5件を確認（Ollama必要）
3. パフォーマンス … DBサイズ < 500MB、類似度計算 < 200ms

rag.db が無い場合はスキップする。データが未取得のカテゴリ（PMDA等）は
件数検証を失敗として報告する。
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import time
from pathlib import Path

import numpy as np
import pytest

from ragdb import config
from ragdb.embed import Embedder
from ragdb.search import Index

DB = config.DB_PATH
EVAL = json.loads((Path(__file__).parent / "eval_queries.json").read_text(encoding="utf-8"))

pytestmark = pytest.mark.skipif(not DB.exists(), reason=f"{DB} がありません（make build を先に実行）")


@pytest.fixture(scope="module")
def con():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    yield c
    c.close()


@pytest.fixture(scope="module")
def index():
    return Index(DB)


@pytest.fixture(scope="module")
def embedder(index):
    e = Embedder(model=index.meta.get("embed_model", config.EMBED_MODEL), dim=index.dim)
    try:
        e.embed_text("接続確認")
    except Exception as ex:  # noqa: BLE001
        pytest.skip(f"Ollama に接続できないため検索精度検証をスキップ: {ex}")
    return e


# ── 1. データ量 ───────────────────────────────────────────────
@pytest.mark.parametrize("category", list(config.MIN_COUNTS))
def test_chunk_count(con, category):
    n = con.execute("SELECT COUNT(*) FROM chunks WHERE category = ?", (category,)).fetchone()[0]
    assert n >= config.MIN_COUNTS[category], f"{category}: {n}件（期待 >= {config.MIN_COUNTS[category]}）"


def test_embedding_blob_size(con):
    dim = int(dict(con.execute("SELECT key, value FROM meta")).get("embed_dim", "768"))
    bad = con.execute("SELECT COUNT(*) FROM chunks WHERE length(embedding) != ?", (dim * 4,)).fetchone()[0]
    assert bad == 0


def test_no_empty_body(con):
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE trim(body) = ''").fetchone()[0] == 0


# ── 2. 検索精度 ───────────────────────────────────────────────
def _available(con, category):
    return con.execute("SELECT 1 FROM chunks WHERE category = ? LIMIT 1", (category,)).fetchone()


@pytest.mark.parametrize("q", EVAL["queries"], ids=[q["query"] for q in EVAL["queries"]])
def test_search_accuracy(con, index, embedder, q):
    if not _available(con, q["category"]):
        pytest.skip(f"{q['category']} のデータが rag.db にありません")
    hits, _ = index.search(embedder, q["query"], top_k=EVAL["top_k"])
    summary = " | ".join(f"{h.score:.3f} {h.category}:{h.code} {h.title[:20]}" for h in hits)
    matched = [
        h for h in hits
        if h.category == q["category"]
        and (not q.get("expect_code_prefix") or (h.code or "").startswith(q["expect_code_prefix"]))
        and (not q.get("expect_title_contains") or q["expect_title_contains"] in h.title)
    ]
    assert matched, f"期待チャンクが上位{EVAL['top_k']}件にありません: {summary}"
    best = max(h.score for h in matched)
    assert best >= q["min_score"], f"score {best:.3f} < {q['min_score']}: {summary}"


# ── 3. パフォーマンス ─────────────────────────────────────────
def test_db_size():
    size = DB.stat().st_size
    assert size < config.MAX_DB_BYTES, f"{size / 1024 / 1024:.1f} MB"


def test_similarity_latency(index):
    rng = np.random.default_rng(0)
    times = []
    for _ in range(10):
        q = rng.standard_normal(index.dim).astype(np.float32)
        t0 = time.perf_counter()
        index.search_vector(q, top_k=5)
        times.append((time.perf_counter() - t0) * 1000)
    med = statistics.median(times)
    assert med < config.MAX_SEARCH_MS, f"中央値 {med:.1f} ms"
