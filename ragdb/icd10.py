"""ICD-10（2013年版）基本分類CSV（e-Stat）→ raw/icd10.csv への変換。

e-Stat の CSV は「分類コード,項目名」の2列で、章（I〜XXII）→中間分類（A00-A09）→
3桁分類（A00）→4桁細分類（A00.0）の順に並ぶ。英語名・定義・包含/除外は
提供されていないため空欄で出力する（列は要件の形式に合わせて残す）。

項目名だけでは「その他」のように意味を持たない行があるため、上位分類を
`hierarchy` 列に保持し、チャンク本文に含める。
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

COLUMNS = ["code", "name_ja", "name_en", "definition", "includes", "excludes", "hierarchy"]

_CHAPTER = re.compile(r"^[IVX]+$")
_BLOCK = re.compile(r"^([A-Z]\d\d)-([A-Z]\d\d)$")


def strip_marks(code: str) -> str:
    """剣印(†)・星印(*)を除いた検索用コード。"""
    return code.rstrip("†*")


def read_estat(path: Path) -> list[tuple[str, str]]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"文字コードを判定できません: {path}")
    rows = []
    for rec in csv.reader(text.splitlines()):
        if len(rec) < 2:
            continue
        code, name = rec[0].strip(), rec[1].strip()
        if code and code != "分類コード":
            rows.append((code, name))
    return rows


def to_rows(estat_rows: list[tuple[str, str]]) -> list[dict]:
    chapter: tuple[str, str] | None = None
    blocks: list[tuple[str, str, str]] = []  # 入れ子（C00-C97 ⊃ C00-C14）に対応するスタック
    names: dict[str, tuple[str, str]] = {}  # 正規化コード → (表示コード, 名称)
    parent_of: dict[str, str] = {}
    out = []

    for code, name in estat_rows:
        hier: list[str] = []
        if _CHAPTER.match(code):
            chapter, blocks = (code, name), []
        else:
            m = _BLOCK.match(code)
            if m:
                start, end = m.groups()
                while blocks and not (blocks[-1][0] <= start and end <= blocks[-1][1]):
                    blocks.pop()
                block_parents = list(blocks)
                blocks.append((start, end, name))
            else:
                key = strip_marks(code)
                # A00.0 → A00、A08.5a → A08.5 のように最長一致する既出コードを親とする
                for cut in range(len(key) - 1, 2, -1):
                    cand = key[:cut].rstrip(".")
                    if cand in names and cand != key:
                        parent_of[key] = cand
                        break
                names[key] = (code, name)
                p = parent_of.get(key)
                while p:
                    hier.append(f"{names[p][0]} {names[p][1]}")
                    p = parent_of.get(p)
                block_parents = list(blocks)
            for s, e, n in reversed(block_parents):
                hier.append(f"{s}-{e} {n}")
            if chapter:
                hier.append(f"第{chapter[0]}章 {chapter[1]}")
        out.append({
            "code": code, "name_ja": name, "name_en": "", "definition": "",
            "includes": "", "excludes": "", "hierarchy": " > ".join(hier),
        })
    return out


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
