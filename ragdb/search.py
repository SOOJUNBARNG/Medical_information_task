"""rag.db に対するコサイン類似度検索（テスト・CLI確認用。アプリ側は rag.js が同じ処理を行う）。"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .db import read_meta


@dataclass
class Hit:
    id: int
    category: str
    code: str | None
    title: str
    section: str | None
    body: str
    score: float


class Index:
    """全チャンクの埋め込みを (N, dim) 行列として保持し、行列積で類似度を計算する。"""

    def __init__(self, db_path: Path):
        self.con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.meta = read_meta(self.con)
        self.dim = int(self.meta.get("embed_dim", "768"))
        ids, cats, blobs = [], [], []
        for rid, cat, blob in self.con.execute("SELECT id, category, embedding FROM chunks ORDER BY id"):
            ids.append(rid)
            cats.append(cat)
            blobs.append(blob)
        self.ids = np.asarray(ids, dtype=np.int64)
        self.categories = np.asarray(cats)
        mat = np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(ids), self.dim)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self.matrix = (mat / norms).astype(np.float32)
        try:
            self.keywords = list(self.con.execute("SELECT word, chunk_id FROM keywords"))
        except sqlite3.OperationalError:
            self.keywords = []

    def keyword_hits(self, query: str, category: str | None = None) -> list[Hit]:
        """質問文に薬品名・成分名・先発品名が含まれる院内採用薬を返す（長い語の一致を優先）。"""
        ids: list[int] = []
        for word, cid in sorted(self.keywords, key=lambda x: -len(x[0])):
            if word in query and cid not in ids:
                ids.append(cid)
        hits = []
        for rid in ids:
            cat, code, title, section, body = self.con.execute(
                "SELECT category, code, title, section, body FROM chunks WHERE id = ?", (rid,)
            ).fetchone()
            if not category or cat == category:
                hits.append(Hit(rid, cat, code, title, section, body, 1.0))
        return hits

    def search_vector(self, qvec: np.ndarray, top_k: int = 5, category: str | None = None) -> list[Hit]:
        q = np.asarray(qvec, dtype=np.float32)
        q = q / (np.linalg.norm(q) or 1.0)
        if category:
            mask = self.categories == category
            idx = np.nonzero(mask)[0]
            scores = self.matrix[idx] @ q
        else:
            idx = None
            scores = self.matrix @ q
        k = min(top_k, len(scores))
        if k == 0:
            return []
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        hits = []
        for t in top:
            row_i = idx[t] if idx is not None else t
            rid = int(self.ids[row_i])
            cat, code, title, section, body = self.con.execute(
                "SELECT category, code, title, section, body FROM chunks WHERE id = ?", (rid,)
            ).fetchone()
            hits.append(Hit(rid, cat, code, title, section, body, float(scores[t])))
        return hits

    def search(self, embedder, query: str, top_k: int = 5, category: str | None = None) -> tuple[list[Hit], dict]:
        t0 = time.perf_counter()
        blob = embedder.embed_query(query, prefix=self.meta.get("query_prefix", ""))
        t1 = time.perf_counter()
        qvec = np.frombuffer(blob, dtype="<f4")
        exact = self.keyword_hits(query, category)
        seen = {h.id for h in exact}
        hits = (exact + [h for h in self.search_vector(qvec, top_k + len(exact), category) if h.id not in seen])[:top_k]
        t2 = time.perf_counter()
        return hits, {"embed_ms": (t1 - t0) * 1000, "search_ms": (t2 - t1) * 1000}
