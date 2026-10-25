"""data/raw/ をチャンク化し、Ollama（nomic-embed-text）で埋め込みを計算する。

中断（Ctrl+C）しても、再実行すれば data/embed_progress.jsonl に記録済みの
チャンクはスキップして続きから計算する。

  python scripts/04_chunk_and_embed.py
  python scripts/04_chunk_and_embed.py --chunk-only   # チャンク化のみ（Ollama不要）
  OLLAMA_HOST_URL=http://127.0.0.1:11434 python scripts/04_chunk_and_embed.py
"""
import argparse
import time
from collections import Counter

import _bootstrap  # noqa: F401

from ragdb import chunking, config
from ragdb.embed import Embedder, embed_all
from ragdb.util import write_jsonl


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk-only", action="store_true")
    args = ap.parse_args()

    chunks = list(chunking.all_chunks())
    if not chunks:
        raise SystemExit("チャンクが0件です。先に make fetch を実行してください。")
    write_jsonl(config.CHUNKS_JSONL, chunks)
    by_cat = Counter(c["category"] for c in chunks)
    print(f"チャンク: {len(chunks)}件 {dict(by_cat)} → {config.CHUNKS_JSONL}")
    if args.chunk_only:
        return

    embedder = Embedder()
    embedder.check()
    print(f"埋め込み: {embedder.model} @ {embedder.url}（並列{config.EMBED_PARALLEL}）")
    t0 = time.time()
    try:
        stats = embed_all(chunks, embedder)
    except KeyboardInterrupt:
        raise SystemExit("\n中断しました。計算済みの分は保存済みです。再実行すると続きから再開します。")
    dt = time.time() - t0
    rate = stats["embedded"] / dt if dt > 0 and stats["embedded"] else 0
    print(f"完了: 新規 {stats['embedded']}件 / スキップ {stats['skipped']}件 / {dt:.0f}秒"
          + (f"（{rate:.1f}件/秒）" if rate else ""))


if __name__ == "__main__":
    main()
