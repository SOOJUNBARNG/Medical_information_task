"""PMDA 添付文書XMLを承認番号ごとに data/raw/pmda/{shonin_no}.xml へ整理する。

PMDAの一括ダウンロードは PMDAメディナビ「マイ医薬品集作成サービス」（会員登録制）
からのみ取得できるため、このスクリプトはダウンロードを行わない。
事前に「XML/SGMLのみ」で一括ダウンロードしたZIP（またはXML）を
data/raw/source/pmda/ に置いてから実行する。
"""
import _bootstrap  # noqa: F401

from ragdb import config, pmda


def main() -> None:
    src = config.PMDA_DOWNLOAD_DIR
    src.mkdir(parents=True, exist_ok=True)
    if not any(src.rglob("*.zip")) and not any(src.rglob("*.xml")):
        print(
            f"PMDAの添付文書データがありません: {src}\n"
            "  PMDAメディナビの「添付文書一括ダウンロード」で取得したZIPをここに置いてください。\n"
            "  PMDAをスキップして ICD-10・DPC のみで続行します。"
        )
        return
    n, skipped = pmda.collect(src, config.PMDA_XML_DIR)
    for s in skipped[:20]:
        print("スキップ:", s)
    if len(skipped) > 20:
        print(f"…ほか {len(skipped) - 20} 件スキップ")
    print(f"出力: {config.PMDA_XML_DIR}（{n}ファイル / スキップ {len(skipped)}件）")


if __name__ == "__main__":
    main()
