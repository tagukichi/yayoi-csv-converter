"""弥生会計（デスクトップ版）の「仕訳データ」インポート形式CSVを書き出す。

列構成・書式は、会計事務所からもらった実際の弥生エクスポート（テンプレート
CSV）に完全準拠している:
- 27列（25列 + 借方取引先名・貸方取引先名）
- 日付は和暦形式 R.07/01/01（令和7年1月1日）
- 識別フラグは 2000（1行1仕訳の仕訳日記帳形式）、税金額 0、タイプ 0、
  付箋 0/0、調整 no
- 文字コードは Shift-JIS(cp932)、改行は CR+LF、ヘッダ行なし

実運用前に必ず「実際の弥生会計での取込テスト」で確定すること。
"""

from __future__ import annotations

import csv
import io
from datetime import date

from models import JournalEntry

# 弥生「仕訳データ」取込の識別フラグ。1行1仕訳（仕訳日記帳形式）は "2000"。
JOURNAL_FLAG = "2000"

# 弥生の取込ファイルは Shift-JIS。UTF-8 で書き出すと文字化け・取込エラーになる。
ENCODING = "cp932"

# 27列のヘッダ（会計事務所のテンプレートCSVと同一の並び）。弥生のインポートは
# ヘッダ行を自動で読み飛ばさないため、取込用CSVにはヘッダを付けない
# （include_header=False が既定）。ヘッダ付きは人が確認する用途向け。
HEADER = [
    "識別フラグ", "伝票No", "決算", "取引日付",
    "借方勘定科目", "借方補助科目", "借方部門", "借方税区分", "借方金額", "借方税金額",
    "貸方勘定科目", "貸方補助科目", "貸方部門", "貸方税区分", "貸方金額", "貸方税金額",
    "摘要", "番号", "期日", "タイプ", "生成元", "仕訳メモ", "付箋1", "付箋2", "調整",
    "借方取引先名", "貸方取引先名",
]


def _wareki(d: date) -> str:
    """日付を弥生テンプレートと同じ和暦形式（R.07/01/01）にする。

    令和（2019年5月〜)のみ対応。それ以前の日付は西暦のまま出す
    （弥生は西暦 YYYY/MM/DD も受け付ける）。
    """
    if d >= date(2019, 5, 1):
        return f"R.{d.year - 2018:02d}/{d.month:02d}/{d.day:02d}"
    return d.strftime("%Y/%m/%d")


def _row(entry: JournalEntry, denpyo_no: int) -> list[str]:
    amount = str(entry.amount)
    return [
        JOURNAL_FLAG,            # 識別フラグ
        str(denpyo_no),          # 伝票No
        "",                      # 決算（通常仕訳は空欄）
        _wareki(entry.date),     # 取引日付（和暦 R.07/01/01 形式）
        entry.debit_account,     # 借方勘定科目
        entry.debit_sub,         # 借方補助科目
        entry.debit_dept,        # 借方部門
        entry.debit_tax,         # 借方税区分
        amount,                  # 借方金額
        "0",                     # 借方税金額（税込処理では 0。テンプレート準拠）
        entry.credit_account,    # 貸方勘定科目
        entry.credit_sub,        # 貸方補助科目
        entry.credit_dept,       # 貸方部門
        entry.credit_tax,        # 貸方税区分
        amount,                  # 貸方金額
        "0",                     # 貸方税金額
        entry.description,       # 摘要
        "",                      # 番号
        "",                      # 期日
        "0",                     # タイプ（0=仕訳データ）
        "",                      # 生成元
        "",                      # 仕訳メモ
        "0",                     # 付箋1
        "0",                     # 付箋2
        "no",                    # 調整
        "",                      # 借方取引先名
        "",                      # 貸方取引先名
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
