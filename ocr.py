"""Azure Computer Vision (Read API v3.2) を使ったOCR処理。

run_ocr() はテキスト行のみ、run_ocr_lines() は座標付きの行を返す。
通帳・カード明細のような表形式の書類は、座標を使って行・列を復元する
（group_rows() 参照）。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import requests

OCR_API_VERSION = "v3.2"

# 無料プラン(F0)は 4MB まで・先頭2ページのみ処理される
MAX_FILE_SIZE_MB = 50


class AzureOCRError(Exception):
    """OCR処理で発生したエラー。"""


@dataclass
class OcrLine:
    """OCRで読み取った1行（座標付き）。

    x, y は行の左上付近の座標、height は行の高さ。単位は PDF なら inch、
    画像なら pixel（同一ファイル内で一貫しているため相対比較にのみ使う）。
    """

    text: str
    x: float
    y: float
    height: float
    page: int


def get_credentials() -> tuple[str, str]:
    endpoint = os.getenv("AZURE_VISION_ENDPOINT", "").rstrip("/")
    key = os.getenv("AZURE_VISION_KEY", "")
    return endpoint, key


def credentials_available() -> bool:
    endpoint, key = get_credentials()
    return bool(endpoint and key)


def _analyze(file_bytes: bytes, language: str, timeout_sec: int) -> dict:
    """Read API に投げてポーリングし、analyzeResult を返す。"""
    endpoint, key = get_credentials()
    if not endpoint or not key:
        raise AzureOCRError(
            "AZURE_VISION_ENDPOINT / AZURE_VISION_KEY が設定されていません。"
            ".env を確認してください。"
        )

    if len(file_bytes) > MAX_FILE_SIZE_MB * 1024 * 1024:
        raise AzureOCRError(f"ファイルサイズが上限({MAX_FILE_SIZE_MB}MB)を超えています。")

    analyze_url = f"{endpoint}/vision/{OCR_API_VERSION}/read/analyze"
    res = requests.post(
        analyze_url,
        params={"language": language},
        headers={
            "Ocp-Apim-Subscription-Key": key,
            "Content-Type": "application/octet-stream",
        },
        data=file_bytes,
        timeout=30,
    )
    if res.status_code != 202:
        raise AzureOCRError(f"OCRリクエストに失敗しました: HTTP {res.status_code} {res.text}")

    operation_url = res.headers["Operation-Location"]
    deadline = time.time() + timeout_sec

    while True:
        poll = requests.get(
            operation_url,
            headers={"Ocp-Apim-Subscription-Key": key},
            timeout=30,
        )
        poll.raise_for_status()
        result = poll.json()
        status = result.get("status")

        if status == "succeeded":
            return result["analyzeResult"]
        if status == "failed":
            raise AzureOCRError("OCR処理が失敗しました。ファイル形式・内容を確認してください。")
        if time.time() > deadline:
            raise AzureOCRError("OCR処理がタイムアウトしました。")
        time.sleep(1)


def run_ocr(file_bytes: bytes, *, language: str = "ja", timeout_sec: int = 120) -> list[str]:
    """OCRを実行し、読み取った行テキストのリストを返す。"""
    return [line.text for line in run_ocr_lines(file_bytes, language=language, timeout_sec=timeout_sec)]


def run_ocr_lines(
    file_bytes: bytes, *, language: str = "ja", timeout_sec: int = 120
) -> list[OcrLine]:
    """OCRを実行し、座標付きの行リストを返す。"""
    analyze_result = _analyze(file_bytes, language, timeout_sec)
    lines: list[OcrLine] = []
    for page_no, page in enumerate(analyze_result["readResults"], start=1):
        for line in page["lines"]:
            # boundingBox は四隅の [x1,y1, x2,y2, x3,y3, x4,y4]
            box = line["boundingBox"]
            xs, ys = box[0::2], box[1::2]
            lines.append(
                OcrLine(
                    text=line["text"],
                    x=min(xs),
                    y=(min(ys) + max(ys)) / 2,
                    height=max(ys) - min(ys),
                    page=page_no,
                )
            )
    return lines


def group_rows(lines: list[OcrLine]) -> list[list[OcrLine]]:
    """座標を使って行をグループ化し、表の「行」を復元する。

    同一ページ内で Y 座標が近い行を1つの行とみなし、行内は X 座標順
    （左→右）に並べる。しきい値は行高の中央値から自動決定するので、
    PDF(inch)・画像(pixel) どちらの単位でも機能する。
    """
    if not lines:
        return []

    rows: list[list[OcrLine]] = []
    pages = sorted({ln.page for ln in lines})
    for page in pages:
        page_lines = sorted(
            (ln for ln in lines if ln.page == page), key=lambda ln: ln.y
        )
        heights = sorted(ln.height for ln in page_lines)
        median_height = heights[len(heights) // 2]
        tol = median_height * 0.6

        current: list[OcrLine] = []
        current_y = None
        for ln in page_lines:
            if current_y is None or abs(ln.y - current_y) <= tol:
                current.append(ln)
                # 行の代表Yは所属行の平均（傾きスキャンでもずれにくくする）
                current_y = sum(c.y for c in current) / len(current)
            else:
                rows.append(sorted(current, key=lambda c: c.x))
                current = [ln]
                current_y = ln.y
        if current:
            rows.append(sorted(current, key=lambda c: c.x))
    return rows
