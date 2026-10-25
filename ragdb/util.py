from __future__ import annotations

import html
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator

from . import config

# 実際のHTMLタグのみ（「<腫瘍>」のような医学用語の山括弧は残す）
_HTML_TAGS = "a|b|br|div|em|font|h[1-6]|hr|i|img|li|ol|p|small|span|strong|sub|sup|table|tbody|td|th|thead|tr|u|ul"
_TAG = re.compile(rf"</?(?:{_HTML_TAGS})(?:\s[^<>]*)?/?>", re.IGNORECASE)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"\s+")


def clean(s: object) -> str:
    """前処理共通仕様: 全角スペース→半角 / HTMLタグ・制御文字除去 / 空白・改行を単一スペースに圧縮。"""
    if s is None:
        return ""
    t = str(s)
    t = _TAG.sub(" ", t)
    t = html.unescape(t)
    t = t.replace("　", " ")
    t = _CTRL.sub(" ", t)
    return _SPACES.sub(" ", t).strip()


@lru_cache(maxsize=1)
def _encoder():
    from tiktoken import get_encoding

    return get_encoding("cl100k_base")


def token_len(text: str) -> int:
    """tiktoken cl100k_base によるテキスト長の推定。"""
    return len(_encoder().encode(text))


def truncate_tokens(text: str, max_tokens: int) -> str:
    enc = _encoder()
    ids = enc.encode(text)
    if len(ids) <= max_tokens:
        return text
    # マルチバイト文字の途中で切れた場合の置換文字を落とす
    return enc.decode(ids[:max_tokens]).rstrip("�")


def download(url: str, dest: Path, *, timeout: int = 120) -> Path:
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream(
        "GET", url, timeout=timeout, follow_redirects=True,
        headers={"User-Agent": config.HTTP_USER_AGENT},
    ) as r:
        r.raise_for_status()
        if "text/html" in r.headers.get("Content-Type", ""):
            raise RuntimeError(f"ファイルではなくHTMLが返されました（URL変更の可能性）: {url}")
        with open(tmp, "wb") as f:
            for block in r.iter_bytes(1 << 16):
                f.write(block)
    tmp.replace(dest)
    return dest


def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    n = 0
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    tmp.replace(path)
    return n


def read_jsonl(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)
