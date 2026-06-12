"""Azure Computer Vision (Read API v3.2) を使ったOCR処理。"""

import os
import time

import requests

OCR_API_VERSION = "v3.2"

# 無料プラン(F0)は 4MB まで・先頭2ページのみ処理される
MAX_FILE_SIZE_MB = 50


class AzureOCRError(Exception):
    """OCR処理で発生したエラー。"""


def get_credentials() -> tuple[str, str]:
    endpoint = os.getenv("AZURE_VISION_ENDPOINT", "").rstrip("/")
    key = os.getenv("AZURE_VISION_KEY", "")
    return endpoint, key


def credentials_available() -> bool:
    endpoint, key = get_credentials()
    return bool(endpoint and key)


def run_ocr(file_bytes: bytes, *, language: str = "ja", timeout_sec: int = 120) -> list[str]:
    """OCRを実行し、読み取った行テキストのリストを返す。

    PDF / PNG / JPG をそのまま渡せる。Read API は非同期なので、
    リクエスト送信後に Operation-Location をポーリングして結果を取得する。
    """
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
            break
        if status == "failed":
            raise AzureOCRError("OCR処理が失敗しました。ファイル形式・内容を確認してください。")
        if time.time() > deadline:
            raise AzureOCRError("OCR処理がタイムアウトしました。")
        time.sleep(1)

    lines: list[str] = []
    for page in result["analyzeResult"]["readResults"]:
        for line in page["lines"]:
            lines.append(line["text"])
    return lines
