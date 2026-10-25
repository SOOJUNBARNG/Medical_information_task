"""Ollama /api/embeddings による埋め込み計算（再開可能）。

- 1チャンク = 1リクエスト、並列4、リトライ3回（2s→4s→8s）
- 1,000チャンクごとにチェックポイント:
    1) data/embeddings.sqlite に BLOB を保存して commit
    2) data/embed_progress.jsonl に完了 chunk_id を追記
  の順で書くため、progress に載っている ID は必ず保存済み。
- 再実行時は progress 記録済み（かつ同じモデル）の ID をスキップする。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import struct
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import httpx

from . import config

log = logging.getLogger(__name__)


def pack(vec: list[float]) -> bytes:
    """Float32 little-endian BLOB。"""
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: bytes) -> tuple[float, ...]:
    return struct.unpack(f"<{len(blob) // 4}f", blob)


class Embedder:
    def __init__(self, host: str = config.OLLAMA_HOST, model: str = config.EMBED_MODEL,
                 dim: int = config.EMBED_DIM):
        self.url = host.rstrip("/") + "/api/embeddings"
        self.model = model
        self.dim = dim
        self.client = httpx.Client(timeout=config.EMBED_TIMEOUT_SEC)

    def embed_text(self, text: str) -> bytes:
        last: Exception | None = None
        for attempt in range(config.EMBED_RETRIES + 1):
            try:
                resp = self.client.post(self.url, json={"model": self.model, "prompt": text})
                resp.raise_for_status()
                vec = resp.json()["embedding"]
                if len(vec) != self.dim:
                    raise ValueError(f"埋め込み次元が {len(vec)}（期待値 {self.dim}）")
                return pack(vec)
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last = e
                if attempt < config.EMBED_RETRIES:
                    time.sleep(config.EMBED_RETRY_BASE_SEC * (2 ** attempt))
        raise RuntimeError(f"埋め込みに失敗しました（{config.EMBED_RETRIES}回リトライ後）: {last}")

    def embed_document(self, text: str) -> bytes:
        return self.embed_text(config.DOC_PREFIX + text)

    def embed_query(self, text: str, prefix: str = config.QUERY_PREFIX) -> bytes:
        return self.embed_text(prefix + text)

    def check(self) -> None:
        try:
            self.embed_text("test")
        except Exception as e:
            raise SystemExit(
                f"Ollama に接続できないか、モデル {self.model} がありません: {e}\n"
                f"  接続先: {self.url}\n"
                f"  対処: ollama pull {self.model} / OLLAMA_HOST_URL を確認"
            ) from e


# ── 保存先・進捗 ───────────────────────────────────────────────
def open_store(path: Path = config.EMBED_STORE) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(
        "CREATE TABLE IF NOT EXISTS embeddings ("
        " chunk_id TEXT NOT NULL, model TEXT NOT NULL, embedding BLOB NOT NULL,"
        " PRIMARY KEY (chunk_id, model))"
    )
    con.commit()
    return con


def load_done_ids(progress: Path = config.EMBED_PROGRESS, model: str = config.EMBED_MODEL) -> set[str]:
    done: set[str] = set()
    if progress.exists():
        with open(progress, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 書き込み途中で中断された最終行
                if rec.get("model", model) == model:
                    done.add(rec["chunk_id"])
    return done


def _checkpoint(con: sqlite3.Connection, progress: Path, model: str, batch: list[tuple[str, bytes]]) -> None:
    if not batch:
        return
    con.executemany(
        "INSERT OR REPLACE INTO embeddings (chunk_id, model, embedding) VALUES (?, ?, ?)",
        [(cid, model, blob) for cid, blob in batch],
    )
    con.commit()
    with open(progress, "a", encoding="utf-8") as f:
        for cid, _ in batch:
            f.write(json.dumps({"chunk_id": cid, "model": model}) + "\n")
    batch.clear()


def embed_all(chunks: list[dict], embedder: Embedder, *,
              progress: Path = config.EMBED_PROGRESS, store: Path = config.EMBED_STORE,
              parallel: int = config.EMBED_PARALLEL, every: int = config.CHECKPOINT_EVERY,
              show_progress: bool = True) -> dict:
    done_ids = load_done_ids(progress, embedder.model)
    todo = [c for c in chunks if c["id"] not in done_ids]
    stats = {"total": len(chunks), "skipped": len(chunks) - len(todo), "embedded": 0}
    if not todo:
        return stats

    con = open_store(store)
    batch: list[tuple[str, bytes]] = []
    bar = None
    if show_progress:
        from tqdm import tqdm
        bar = tqdm(total=len(todo), desc="embedding", unit="chunk")

    it = iter(todo)
    try:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            pending = {}
            # 投入数を並列数の数倍に抑え、中断時に捨てる計算を少なくする
            for c in it:
                pending[pool.submit(embedder.embed_document, c["embed_text"])] = c["id"]
                if len(pending) >= parallel * 2:
                    break
            while pending:
                finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                for fut in finished:
                    cid = pending.pop(fut)
                    batch.append((cid, fut.result()))
                    stats["embedded"] += 1
                    if bar:
                        bar.update(1)
                    if len(batch) >= every:
                        _checkpoint(con, progress, embedder.model, batch)
                    nxt = next(it, None)
                    if nxt is not None:
                        pending[pool.submit(embedder.embed_document, nxt["embed_text"])] = nxt["id"]
    finally:
        # 中断・例外時も計算済みの分は保存する
        _checkpoint(con, progress, embedder.model, batch)
        con.close()
        if bar:
            bar.close()
    return stats
