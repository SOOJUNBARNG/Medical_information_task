"""rag.db（配布用SQLite）の生成。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    code TEXT,
    title TEXT NOT NULL,
    section TEXT,
    body TEXT NOT NULL,
    embedding BLOB NOT NULL
)
"""


def init_db(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(SCHEMA)
    con.execute("CREATE INDEX IF NOT EXISTS idx_category ON chunks (category)")
    # rag.js が検索時に読む埋め込みモデル情報など
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    # 質問文に含まれていればそのチャンクを直接ヒットさせる語（院内医薬品集の薬品名・成分名・先発品名）
    con.execute("CREATE TABLE IF NOT EXISTS keywords (chunk_id INTEGER NOT NULL, word TEXT NOT NULL)")
    con.commit()
    return con


def insert_batch(con: sqlite3.Connection, rows: list[dict]) -> None:
    con.executemany(
        "INSERT INTO chunks (category,code,title,section,body,embedding) "
        "VALUES (:category,:code,:title,:section,:body,:embedding)",
        rows,
    )
    con.commit()


def _flush(con: sqlite3.Connection, batch: list[dict], kw: list[list[str]]) -> None:
    insert_batch(con, batch)
    # 新規DBへの単一書き込みなので、直前の executemany の id は連番で末尾に並ぶ
    last = con.execute("SELECT max(id) FROM chunks").fetchone()[0]
    first = last - len(batch) + 1
    pairs = [(first + i, w) for i, words in enumerate(kw) for w in words]
    if pairs:
        con.executemany("INSERT INTO keywords (chunk_id, word) VALUES (?, ?)", pairs)
        con.commit()


def build(db_path: Path, chunks: Iterable[dict], embeddings: dict[str, bytes], *,
          model: str = config.EMBED_MODEL, dim: int = config.EMBED_DIM,
          batch_size: int = config.CHECKPOINT_EVERY, progress=None) -> dict[str, int]:
    """必ず新しいファイルとして作る（呼び出し側で既存ファイルの確認・削除を済ませること）。"""
    if db_path.exists():
        raise FileExistsError(db_path)
    tmp = db_path.with_suffix(".db.building")
    for p in (tmp, Path(f"{tmp}-wal"), Path(f"{tmp}-shm")):
        p.unlink(missing_ok=True)

    con = init_db(tmp)
    counts: dict[str, int] = {}
    batch: list[dict] = []
    kw: list[list[str]] = []
    for c in chunks:
        blob = embeddings.get(c["id"])
        if blob is None:
            raise KeyError(f"埋め込みが未計算のチャンクがあります: {c['id']}（make embed を再実行）")
        if len(blob) != dim * 4:
            raise ValueError(f"埋め込みサイズ不正: {c['id']} {len(blob)} bytes")
        batch.append({k: c[k] for k in ("category", "code", "title", "section", "body")} | {"embedding": blob})
        kw.append(c.get("keywords") or [])
        counts[c["category"]] = counts.get(c["category"], 0) + 1
        if len(batch) >= batch_size:
            _flush(con, batch, kw)
            if progress:
                progress(len(batch))
            batch.clear()
            kw.clear()
    if batch:
        _flush(con, batch, kw)
        if progress:
            progress(len(batch))

    con.execute("CREATE INDEX IF NOT EXISTS idx_code ON chunks (code)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_keywords_word ON keywords (word)")
    meta = {
        "schema_version": "1",
        "embed_model": model,
        "embed_dim": str(dim),
        "embed_dtype": "float32-le",
        "doc_prefix": config.DOC_PREFIX,
        "query_prefix": config.QUERY_PREFIX,
        "tokenizer": "tiktoken cl100k_base",
        "max_tokens": str(config.MAX_TOKENS),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": json.dumps(counts, ensure_ascii=False),
        "attribution": json.dumps(config.ATTRIBUTION, ensure_ascii=False),
    }
    con.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", meta.items())
    con.commit()
    # 配布物は単一ファイルにする（-wal/-shm を残さない）。アプリ側は読み取り専用で開く。
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("ANALYZE")
    con.close()
    tmp.replace(db_path)
    return counts


def read_meta(con: sqlite3.Connection) -> dict[str, str]:
    try:
        return dict(con.execute("SELECT key, value FROM meta"))
    except sqlite3.OperationalError:
        return {}
