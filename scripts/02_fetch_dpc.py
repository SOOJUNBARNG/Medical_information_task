"""DPC電子点数表（厚生労働省 Excel）を取得し data/raw/dpc.csv を生成する。

  python scripts/02_fetch_dpc.py
  python scripts/02_fetch_dpc.py --offline
  DPC_URL=https://www.mhlw.go.jp/content/....xlsx python scripts/02_fetch_dpc.py  # 新しい版
"""
import argparse

import _bootstrap  # noqa: F401

from ragdb import config, dpc
from ragdb.util import download


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="ダウンロードせず既存ファイルを使う")
    args = ap.parse_args()

    if not args.offline or not config.DPC_SOURCE.exists():
        print(f"取得: {config.DPC_URL}")
        download(config.DPC_URL, config.DPC_SOURCE)
    rows = dpc.parse_xlsx(config.DPC_SOURCE)
    if len(rows) < 1000:
        raise SystemExit(f"行数が少なすぎます（{len(rows)}行）。Excelの形式が変わった可能性があります。")
    dpc.write_csv(rows, config.DPC_CSV)
    print(f"出力: {config.DPC_CSV}（{len(rows)}分類 / MDC {len({r['mdc_code'] for r in rows})}種）")


if __name__ == "__main__":
    main()
