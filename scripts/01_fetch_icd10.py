"""ICD-10（2013年版）基本分類を e-Stat から取得し data/raw/icd10.csv を生成する。

  python scripts/01_fetch_icd10.py            # ダウンロードして変換
  python scripts/01_fetch_icd10.py --offline  # 取得済みの data/raw/source/ を変換のみ
"""
import argparse

import _bootstrap  # noqa: F401

from ragdb import config, icd10
from ragdb.util import download


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="ダウンロードせず既存ファイルを使う")
    args = ap.parse_args()

    if not args.offline or not config.ICD10_SOURCE.exists():
        print(f"取得: {config.ICD10_URL}")
        download(config.ICD10_URL, config.ICD10_SOURCE)
    rows = icd10.to_rows(icd10.read_estat(config.ICD10_SOURCE))
    if len(rows) < 1000:
        raise SystemExit(f"行数が少なすぎます（{len(rows)}行）。取得URLを確認してください。")
    icd10.write_csv(rows, config.ICD10_CSV)
    codes = sum(1 for r in rows if "." in r["code"])
    print(f"出力: {config.ICD10_CSV}（{len(rows)}行 / うち4桁以上の細分類 {codes}件）")


if __name__ == "__main__":
    main()
