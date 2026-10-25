"""raw/ の各ファイル → チャンク（chunks.jsonl の1行）。

チャンクのフィールド:
  id          … 内容から決まる安定ID（本文が変われば変わる → 再埋め込み対象になる）
  category, code, title, section, body … rag.db の chunks テーブルにそのまま入る
  embed_text  … 埋め込みに渡すテキスト。PMDAは本文に薬剤名が含まれないことが多いため
                「【販売名／セクション】本文」とする（body 自体は要件どおりセクション本文のまま）
"""
from __future__ import annotations

import csv
import hashlib
import logging
import re
from pathlib import Path
from typing import Iterator

from . import config, formulary, pmda
from .util import clean, token_len, truncate_tokens, _encoder

log = logging.getLogger(__name__)

_SENTENCE = re.compile(r"(?<=[。！？!?])\s*")


def chunk_id(category: str, code: str | None, section: str | None, part: int, body: str) -> str:
    h = hashlib.sha1(f"{category}|{code}|{section}|{part}|{body}".encode("utf-8")).hexdigest()
    return h[:20]


def _make(category, code, title, section, body, part=0, embed_text=None) -> dict:
    return {
        "id": chunk_id(category, code, section, part, body),
        "category": category,
        "code": code or None,
        "title": title,
        "section": section,
        "body": body,
        "embed_text": embed_text or body,
    }


def _fit(body: str, where: str) -> str:
    """分割しないデータ（ICD-10・DPC）が512トークンを超えた場合は切り詰める。"""
    if token_len(body) > config.MAX_TOKENS:
        log.warning("512トークン超のため切り詰め: %s", where)
        return truncate_tokens(body, config.MAX_TOKENS)
    return body


# ── ICD-10 ─────────────────────────────────────────────────────
def icd10_chunks(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            code = clean(row.get("code"))
            name = clean(row.get("name_ja"))
            definition = clean(row.get("definition"))
            includes = clean(row.get("includes"))
            excludes = clean(row.get("excludes"))
            hierarchy = clean(row.get("hierarchy"))
            if not name:
                continue
            # 要件の body 構造: {name_ja}\n{definition}\n\n包含: {includes}\n\n除外: {excludes}
            # 空の項目は出力しない。e-Statに定義が無いため、上位分類を「分類:」行として補う。
            head = "\n".join(x for x in (name, definition) if x)
            blocks = [head]
            if hierarchy:
                blocks.append(f"分類: {hierarchy}")
            if includes:
                blocks.append(f"包含: {includes}")
            if excludes:
                blocks.append(f"除外: {excludes}")
            body = _fit("\n\n".join(blocks), f"icd10 {code}")
            yield _make("icd10", code, name, None, body, embed_text=f"ICD-10 {code} {body}")


# ── DPC ────────────────────────────────────────────────────────
def dpc_chunks(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            r = {k: clean(v) for k, v in row.items()}
            if not r.get("dpc_code") or not r.get("dpc_name"):
                continue
            # 要件の body 構造: MDC{mdc_code} {mdc_name} / {dpc_code} {dpc_name}
            # 検索に有用な入院期間・点数・対象ICDを、512トークンに収まる範囲で後ろに足す。
            body = _fit(f"MDC{r['mdc_code']} {r['mdc_name']} / {r['dpc_code']} {r['dpc_name']}",
                        f"dpc {r['dpc_code']}")
            extras = [
                f"入院期間I/II/III(日): {r['days']}" if r.get("days") else "",
                f"点数I/II/III: {r['points']}" if r.get("points") else "",
                f"対象ICD-10: {r['icd_codes']}" if r.get("icd_codes") else "",
            ]
            for extra in filter(None, extras):
                if token_len(f"{body} / {extra}") <= config.MAX_TOKENS:
                    body = f"{body} / {extra}"
            title = r.get("disease_name") or r["dpc_name"]
            yield _make("dpc", r["dpc_code"], title, None, body, embed_text=f"DPC {body}")


# ── PMDA ───────────────────────────────────────────────────────
def split_with_overlap(text: str, max_tokens: int, overlap: int) -> list[str]:
    """文境界で max_tokens 以下に分割し、隣接チャンクで overlap トークンを共有する。"""
    if token_len(text) <= max_tokens:
        return [text]
    enc = _encoder()
    # 1文が上限を超える場合はトークン境界で強制分割する
    sentences: list[str] = []
    for s in _SENTENCE.split(text):
        if not s:
            continue
        ids = enc.encode(s)
        step = max_tokens - overlap
        if len(ids) <= step:
            sentences.append(s)
        else:
            for i in range(0, len(ids), step):
                sentences.append(enc.decode(ids[i:i + step]).strip("�"))

    chunks: list[str] = []
    cur: list[str] = []
    prefix = ""  # 直前チャンク末尾 overlap トークン
    for s in sentences:
        # 連結するとBPEの結合でトークン数が変わるため、実際に連結した長さで判定する
        if cur and token_len((prefix + "".join(cur) + s).strip()) > max_tokens:
            body = (prefix + "".join(cur)).strip()
            chunks.append(body)
            prefix = enc.decode(enc.encode(body)[-overlap:]).lstrip("�")
            cur = []
        cur.append(s)
    if cur:
        chunks.append((prefix + "".join(cur)).strip())
    # 強制分割した文 + overlap がデコード誤差で1〜2トークン超える場合の保険
    return [truncate_tokens(c, max_tokens) for c in chunks]


def pmda_chunks(xml_dir: Path) -> Iterator[dict]:
    for path in sorted(xml_dir.glob("*.xml")):
        try:
            doc = pmda.parse_sections(path)
        except Exception as e:  # 壊れたファイル1件で全体を止めない
            log.warning("PMDA XMLの解析に失敗: %s (%s)", path.name, e)
            continue
        for section, text in doc["sections"]:
            text = clean(text)
            if len(text) < config.PMDA_MIN_BODY_CHARS:
                continue
            parts = split_with_overlap(text, config.MAX_TOKENS, config.PMDA_OVERLAP_TOKENS)
            for i, body in enumerate(parts):
                label = section if len(parts) == 1 else f"{section}({i + 1}/{len(parts)})"
                yield _make(
                    "pmda", doc["code"], doc["title"], section, body, part=i,
                    embed_text=f"【{doc['title']}／{label}】{body}",
                )


# ── 院内医薬品集 ───────────────────────────────────────────────
def formulary_chunks(path: Path) -> Iterator[dict]:
    for row in formulary.read(path):
        body = _fit(clean(formulary.body(row)), f"formulary {row['薬品名']}")
        c = _make("formulary", row["薬効コード"] or None, clean(row["薬品名"]), row["区分"] or None, body,
                  part=int(row["No"]) if row["No"].isdigit() else 0,
                  embed_text=f"院内医薬品集 {body}")
        c["keywords"] = sorted(clean(k) for k in formulary.keywords(row))
        yield c


def all_chunks() -> Iterator[dict]:
    seen: set[str] = set()
    sources = [
        (config.ICD10_CSV, icd10_chunks),
        (config.DPC_CSV, dpc_chunks),
        (config.PMDA_XML_DIR, pmda_chunks),
        (config.FORMULARY_PATH, formulary_chunks),
    ]
    for path, fn in sources:
        if not path.exists():
            log.warning("入力がありません（スキップ）: %s", path)
            continue
        for c in fn(path):
            if not c["body"] or c["id"] in seen:
                continue
            seen.add(c["id"])
            yield c
