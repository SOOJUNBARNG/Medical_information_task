"""chunks.jsonl と計算済み埋め込みから data/rag.db を新規作成する。

既存の rag.db がある場合は上書き前に確認する（--yes で確認を省略）。
"""
import argparse
import sys

import _bootstrap  # noqa: F401

from ragdb import config, db
from ragdb.embed import open_store
from ragdb.util import read_jsonl


class StoreLookup:
    """埋め込み保存先から1件ずつ引く（全件をメモリに載せない）。"""

    def __init__(self, model: str):
        self.con = open_store()
        self.model = model

    def get(self, chunk_id: str):
        row = self.con.execute(
            "SELECT embedding FROM embeddings WHERE chunk_id = ? AND model = ?", (chunk_id, self.model)
        ).fetchone()
        return row[0] if row else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", "-y", action="store_true", help="既存の rag.db を確認なしで上書きする")
    args = ap.parse_args()

    out = config.DB_PATH
    if not config.CHUNKS_JSONL.exists():
        raise SystemExit("chunks.jsonl がありません。先に make embed を実行してください。")
    if out.exists():
        if not args.yes:
            if not sys.stdin.isatty():
                raise SystemExit(f"{out} が既に存在します。上書きする場合は --yes を付けてください。")
            ans = input(f"{out} は既に存在します。上書きしますか？ [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                raise SystemExit("中止しました。")
        out.unlink()

    from tqdm import tqdm

    chunks = list(read_jsonl(config.CHUNKS_JSONL))
    with tqdm(total=len(chunks), desc="writing", unit="chunk") as bar:
        counts = db.build(out, chunks, StoreLookup(config.EMBED_MODEL), progress=bar.update)
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"出力: {out}（{sum(counts.values())}チャンク {counts} / {size_mb:.1f} MB）")


if __name__ == "__main__":
    main()
