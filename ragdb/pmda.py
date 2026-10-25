"""PMDA 医療用医薬品添付文書 XML の収集（03）とセクション抽出（04）。

PMDA の添付文書XML一括ダウンロードは PMDAメディナビ「マイ医薬品集作成サービス」
（会員登録制）でのみ提供されるため、取得はスクリプト化していない。ダウンロードした
ZIP / XML を data/raw/source/pmda/ に置くと、03 が承認番号ごとに
data/raw/pmda/{shonin_no}.xml（UTF-8）へ整理する。

XMLの要素名は版によって異なるため、セクションは次の2通りで判定する。
  1. 要素名（名前空間・HDR_接頭辞を除いた小文字）が既知のタグ名と一致する
  2. 子要素の見出し（Header/Title 等）が「効能又は効果」等で始まる
旧形式のSGMLはXMLとして読めないためスキップする。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

from . import config
from .util import clean

XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
# 承認番号: 例 22000AMX00123 / 30100AMX00001000
_SHONIN = re.compile(r"\b(\d{5}[A-Z]{3}\d{5,8})\b")
_NUMBERING = re.compile(r"^[\s\d０-９.．\-－()（）【】\[\]〔〕<＜>＞・*＊]+")


def local(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _key(tag: object) -> str:
    t = local(tag).lower()
    return t[4:] if t.startswith("hdr_") else t


# ── 03: 収集 ───────────────────────────────────────────────────
def iter_source_xml(src_dir: Path) -> Iterator[tuple[str, bytes]]:
    for p in sorted(src_dir.rglob("*")):
        if p.suffix.lower() == ".zip":
            with zipfile.ZipFile(p) as z:
                for info in z.infolist():
                    if info.filename.lower().endswith(".xml") and not info.is_dir():
                        yield f"{p.name}:{info.filename}", z.read(info)
        elif p.suffix.lower() == ".xml":
            yield str(p), p.read_bytes()


def find_shonin_no(root: ET.Element) -> str | None:
    for el in root.iter():
        if "approvalno" in local(el.tag).lower() or "shonin" in local(el.tag).lower():
            m = _SHONIN.search("".join(el.itertext()))
            if m:
                return m.group(1)
    m = _SHONIN.search(" ".join(root.itertext()))
    return m.group(1) if m else None


def collect(src_dir: Path, out_dir: Path) -> tuple[int, list[str]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    n, skipped = 0, []
    for name, data in iter_source_xml(src_dir):
        try:
            root = ET.fromstring(data)
        except ET.ParseError as e:
            skipped.append(f"{name}: XMLとして解析できません（旧SGML形式の可能性）: {e}")
            continue
        shonin = find_shonin_no(root) or re.sub(r"[^\w.-]", "_", Path(name.split(":")[-1]).stem)
        ET.ElementTree(root).write(out_dir / f"{shonin}.xml", encoding="utf-8", xml_declaration=True)
        n += 1
    return n, skipped


# ── 04: セクション抽出 ─────────────────────────────────────────
def _header_text(el: ET.Element) -> str:
    for child in el:
        name = local(child.tag).lower()
        if "header" in name or "title" in name or name in ("heading", "caption"):
            return clean("".join(child.itertext()))
    return ""


def _match_section(el: ET.Element) -> str | None:
    key = _key(el.tag)
    for canon, rule in config.PMDA_SECTIONS.items():
        if key in rule["tags"]:
            return canon
    head = _NUMBERING.sub("", _header_text(el))
    if head:
        for canon, rule in config.PMDA_SECTIONS.items():
            if any(head.startswith(h) for h in rule["headers"]):
                return canon
    return None


def _text(el: ET.Element) -> str:
    """要素以下のテキスト。xml:lang が日本語以外の要素（英語併記）は除く。"""
    parts: list[str] = []

    def walk(e: ET.Element) -> None:
        lang = e.get(XML_LANG) or e.get("lang")
        if lang and not lang.lower().startswith("ja"):
            return
        if e.text and e.text.strip():
            parts.append(e.text.strip())
        for c in e:
            walk(c)
            if c.tail and c.tail.strip():
                parts.append(c.tail.strip())

    walk(el)
    return clean(" ".join(parts))


def _title(root: ET.Element, fallback: str) -> str:
    for hint in ("approvalbrandname", "brandname", "hanbaimei", "genericname", "nonproprietaryname"):
        for el in root.iter():
            if hint in local(el.tag).lower():
                t = _text(el)
                if t:
                    return t
    return fallback


def parse_sections(path: Path) -> dict:
    """{shonin_no}.xml → {"code", "title", "sections": [(セクション名, 本文), ...]}"""
    root = ET.parse(path).getroot()
    found: dict[str, list[str]] = {}

    def walk(el: ET.Element) -> None:
        canon = _match_section(el)
        if canon:
            body = _text(el)
            head = _header_text(el)
            if head and body.startswith(head):
                body = body[len(head):].strip()
            if body:
                found.setdefault(canon, []).append(body)
            return  # 下位の小見出し（11.1 重大な副作用 等）は同じセクションに含める
        for c in el:
            walk(c)

    walk(root)
    order = list(config.PMDA_SECTIONS)
    return {
        "code": path.stem,
        "title": _title(root, path.stem),
        "sections": [(s, " ".join(found[s])) for s in order if s in found],
    }
