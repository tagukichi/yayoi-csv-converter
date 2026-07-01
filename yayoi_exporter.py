"""弥生会計（デスクトップ版）の「仕訳データ」インポート形式CSVを書き出す。

弥生会計の取込形式は1行 = 1仕訳の固定列フォーマット（識別フラグ "2000" から
始まる25列）。文字コードは Shift-JIS(cp932)、改行は CR+LF が前提。

重要: 列構成・税区分の文字列・日付書式は弥生のバージョンや設定で細部が異なる
場合がある。ここでは一般的な仕様に合わせて実装しているが、実運用前に必ず
「実際の弥生会計での取込テスト」で確定すること（会計事務所の確認と合わせて行う）。
"""

from __future__ import annotations

import csv
import io

from models import JournalEntry

# 弥生「仕訳データ」取込の識別フラグ。仕訳明細行は "2000"。
JOURNAL_FLAG = "2000"

# 弥生の取込ファイルは Shift-JIS。UTF-8 で書き出すと文字化け・取込エラーになる。
ENCODING = "cp932"

# 25列のヘッダ（弥生の取込仕様順）。弥生のインポートはヘッダ行を自動で
# 読み飛ばさないため、取込用CSVにはヘッダを付けない（include_header=False が既定）。
# ヘッダ付きは人がExcel等で中身を確認する用途向け。
HEADER = [
    "識別フラグ", "伝票No", "決算", "取引日付",
    "借方勘定科目", "借方補助科目", "借方部門", "借方税区分", "借方金額", "借方税金額",
    "貸方勘定科目", "貸方補助科目", "貸方部門", "貸方税区分", "貸方金額", "貸方税金額",
    "摘要", "番号", "期日", "タイプ", "生成元", "仕訳メモ", "付箋1", "付箋2", "調整",
]


def _row(entry: JournalEntry, denpyo_no: int) -> list[str]:
    date_str = entry.date.strftime("%Y/%m/%d")
    amount = str(entry.amount)
    return [
        JOURNAL_FLAG,            # 識別フラグ
        str(denpyo_no),          # 伝票No
        "",                      # 決算（通常仕訳は空欄）
        date_str,                # 取引日付
        entry.debit_account,     # 借方勘定科目
        entry.debit_sub,         # 借方補助科目
        entry.debit_dept,        # 借方部門
        entry.debit_tax,         # 借方税区分
        amount,                  # 借方金額
        "",                      # 借方税金額（税区分「〜込」なら弥生側で自動計算）
        entry.credit_account,    # 貸方勘定科目
        entry.credit_sub,        # 貸方補助科目
        entry.credit_dept,       # 貸方部門
        entry.credit_tax,        # 貸方税区分
        amount,                  # 貸方金額
        "",                      # 貸方税金額
        entry.description,       # 摘要
        "",                      # 番号
        "",                      # 期日
        "0",                     # タイプ（0=仕訳データ）
        "",                      # 生成元
        "",                      # 仕訳メモ
        "0",                     # 付箋1
        "0",                     # 付箋2
        "no",                    # 調整
    ]


def to_yayoi_csv(entries: list[JournalEntry], *, include_header: bool = False) -> bytes:
    """仕訳の一覧を弥生インポート形式CSV（Shift-JISバイト列）に変換する。

    Streamlit の download_button にそのまま渡せるよう bytes を返す。
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    if include_header:
        writer.writerow(HEADER)
    for i, entry in enumerate(entries, start=1):
        writer.writerow(_row(entry, denpyo_no=i))
    # cp932 で表現できない文字（一部の旧字・絵文字など）は "?" に置換して
    # 取込エラーを避ける。元データに含まれていれば warnings 側で拾う想定。
    return buffer.getvalue().encode(ENCODING, errors="replace")
