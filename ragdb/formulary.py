"""院内医薬品集（CSV / Excel）の読み込み。

列: No, 区分, 薬効コード, 薬効分類, 薬品名, 規格・単位, 標準用法用量, 成分名, 備考, 先発品, 元CSV行
1行 = 1品目 = 1チャンク。院内データのため data/formulary/ は .gitignore 対象。
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

COLUMNS = ["No", "区分", "薬効コード", "薬効分類", "薬品名", "規格・単位", "標準用法用量", "成分名", "備考", "先発品"]

# PDFから転記した際の改ページ注記
_PAGE_NOTE = re.compile(r"[\(（]\s*(次ページへ続く|前ページより続く|続き)\s*[\)）]")
# 薬品名の剤形・規格より前の部分（「フロセミド錠20mg「NP」」→「フロセミド」）
_NAME_CORE = re.compile(r"^(.+?)(?=(OD|CR|DS|LA|SR)?(錠|散|顆粒|細粒|カプセル|シロップ|ドライシロップ|内用液|液|注|軟膏|クリーム|ローション|テープ|パップ|貼付|坐剤|点眼|点鼻|吸入|ゼリー|\d))")


_LEADING_STRENGTH = re.compile(r"^\d+(\.\d+)?(mg|g|μg|mL|%)?")


def name_core(name: str) -> str:
    name = _LEADING_STRENGTH.sub("", name.strip())  # 「2mgセルシン錠」→「セルシン錠」
    m = _NAME_CORE.match(name)
    return (m.group(1) if m else name).strip()


def _cell(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return _PAGE_NOTE.sub("", s).strip()


def read(path: Path) -> list[dict]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl

        ws = openpyxl.load_workbook(path, read_only=True).worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = [_cell(h) for h in next(it)]
        raw = [dict(zip(header, r)) for r in it]
    else:
        with open(path, encoding="utf-8-sig", newline="") as f:
            raw = list(csv.DictReader(f))

    rows: list[dict] = []
    for r in raw:
        row = {c: _cell(r.get(c)) for c in COLUMNS}
        if not row["薬品名"]:
            continue
        # 同じ薬剤の別規格が直下に並ぶ場合、成分名が空欄になっている
        # （例: アレビアチン散10% → アレビアチン錠100mg）。薬品名の主部が一致するときだけ引き継ぐ。
        if not row["成分名"] and rows and name_core(row["薬品名"]) == name_core(rows[-1]["薬品名"]):
            row["成分名"] = rows[-1]["成分名"]
        rows.append(row)
    return rows


def body(row: dict) -> str:
    """チャンク本文（clean() 前）。rag.js / search.py は「成分名: 」「先発品: 」を正規表現で読むため形式を変えないこと。"""
    multi = lambda s: "、".join(x.strip() for x in s.splitlines() if x.strip())  # noqa: E731
    parts = [f"院内採用薬: {row['薬品名']}（{row['区分']}）"]
    if row["規格・単位"]:
        parts.append(f"規格: {multi(row['規格・単位'])}")
    if row["成分名"]:
        parts.append(f"成分名: {row['成分名']}")
    if row["薬効分類"]:
        parts.append(f"薬効分類: {row['薬効コード']} {row['薬効分類']}".strip())
    parts.append(f"標準用法用量: {multi(row['標準用法用量'])}" if row["標準用法用量"] else "標準用法用量: 記載なし")
    if row["備考"]:
        parts.append(f"備考: {multi(row['備考'])}")
    if row["先発品"]:
        parts.append(f"先発品: {multi(row['先発品'])}")
    return " / ".join(parts)


_SALT = re.compile(r"(塩酸塩|硫酸塩|酒石酸塩|フマル酸塩|マレイン酸塩|メシル酸塩|ベシル酸塩|クエン酸塩|酢酸塩|"
                   r"カリウム|ナトリウム|カルシウム|水和物|塩)$")


def strip_salt(name: str) -> str:
    """「ワルファリンカリウム」→「ワルファリン」、「アマンタジン塩酸塩」→「アマンタジン」"""
    prev = None
    while prev != name:
        prev, name = name, _SALT.sub("", name).strip()
    return name


def keywords(row: dict) -> set[str]:
    """質問文に含まれていれば、その品目を直接ヒットさせる語（薬品名の主部・成分名・先発品名）。"""
    words = {name_core(row["薬品名"])}
    for ing in re.split(r"[/／、,]", row["成分名"]):  # 配合剤
        words.update({ing.strip(), strip_salt(ing.strip())})
    for b in row["先発品"].splitlines():
        words.add(name_core(b.strip()))
    return {w for w in words if len(w) >= 3}
