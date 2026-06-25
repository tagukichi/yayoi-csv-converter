"""変換エンジンの中間データモデル。

OCR結果を解析した結果も、弥生CSVを書き出す手前のデータも、すべてこの
JournalEntry（仕訳1行）で表現する。書類タイプ（通帳・領収書など）や
出力先（弥生・MF・freee）が変わっても、この中間表現は共通で使う。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class JournalEntry:
    """複式簿記の仕訳1行（借方／貸方が揃った状態）。

    例（通帳の出金: 消耗品をXX円で購入）:
        借方 = 消耗品費 / 貸方 = 普通預金

    例（通帳の入金: 売掛金XX円の回収）:
        借方 = 普通預金 / 貸方 = 売掛金
    """

    date: date  # 取引日付
    debit_account: str  # 借方勘定科目
    credit_account: str  # 貸方勘定科目
    amount: int  # 金額（借方・貸方は同額前提。円・整数）
    description: str = ""  # 摘要

    # 補助科目・部門・税区分（弥生の列に対応。未確定なら空 or 既定値）
    debit_sub: str = ""  # 借方補助科目
    credit_sub: str = ""  # 貸方補助科目
    debit_dept: str = ""  # 借方部門
    credit_dept: str = ""  # 貸方部門
    debit_tax: str = "対象外"  # 借方税区分
    credit_tax: str = "対象外"  # 貸方税区分

    # 解析の確からしさ。勘定科目を自動推定できず人の確認が要る行に True を立て、
    # UI で目立たせたり「要確認」フォルダに振り分けたりするのに使う。
    needs_review: bool = False
    # 推定根拠などのメモ（UI 表示・デバッグ用。CSV には出さない）
    note: str = ""

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError("amount は0以上で指定してください（入出金区分は科目で表現する）")


@dataclass
class ParseResult:
    """1ファイル分の解析結果。仕訳の一覧と、警告メッセージをまとめて持つ。"""

    entries: list[JournalEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def needs_review_count(self) -> int:
        return sum(1 for e in self.entries if e.needs_review)
