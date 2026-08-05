"""Azure Computer Vision (Read API v3.2) を使ったOCR処理。

run_ocr() はテキスト行のみ、run_ocr_lines() は座標付きの行を返す。
通帳・カード明細のような表形式の書類は、座標を使って行・列を復元する
（group_rows() 参照）。1枚の写真に複数のレシートが写っている場合は
split_text_clusters() で空間的なかたまりに分割できる。
"""

from __future__ import annotations

import io
import os
import time
from dataclasses import dataclass

import requests

OCR_API_VERSION = "v3.2"

# 無料プラン(F0)は 4MB まで・先頭2ページのみ処理される
MAX_FILE_SIZE_MB = 50

# Azure F0 の画像上限は 4MB。余裕を見て 3.5MB を目標に圧縮する
_MAX_IMAGE_BYTES = int(3.5 * 1024 * 1024)

_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")


def is_image_filename(filename: str) -> bool:
    return filename.lower().endswith(_IMAGE_EXTENSIONS)


def compress_image_if_needed(file_bytes: bytes, filename: str) -> tuple[bytes, str | None]:
    """スマホ写真などの大きな画像をOCRの上限内に自動圧縮する。

    Azure 無料プラン(F0)の画像上限は4MB。スマホ写真は普通に超えるため、
    上限超過時は EXIF の回転を反映したうえで縮小・JPEG再圧縮する。
    戻り値: (送信するバイト列, ユーザー向けメッセージ or None)。
    画像以外や上限内のファイルはそのまま返す。
    """
    if not is_image_filename(filename) or len(file_bytes) <= _MAX_IMAGE_BYTES:
        return file_bytes, None

    from PIL import Image, ImageOps

    original_mb = len(file_bytes) / (1024 * 1024)
    img = Image.open(io.BytesIO(file_bytes))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    max_side = 2600
    result = file_bytes
    while max_side >= 1200:
        w, h = img.size
        scale = min(1.0, max_side / max(w, h))
        work = (
            img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            if scale < 1.0
            else img
        )
        for quality in (85, 75, 65):
            buf = io.BytesIO()
            work.save(buf, "JPEG", quality=quality, optimize=True)
            result = buf.getvalue()
            if len(result) <= _MAX_IMAGE_BYTES:
                return result, (
                    f"画像が大きいため自動圧縮しました"
                    f"（{original_mb:.1f}MB → {len(result) / (1024 * 1024):.1f}MB）。"
                )
        max_side = int(max_side * 0.8)
    return result, "画像を自動圧縮しました（画質を落として送信します）。"


class AzureOCRError(Exception):
    """OCR処理で発生したエラー。"""


@dataclass
class OcrLine:
    """OCRで読み取った1行（座標付き）。

    x, y は行の左上付近の座標、height は行の高さ、width は行の幅。
    単位は PDF なら inch、画像なら pixel（同一ファイル内で一貫している
    ため相対比較にのみ使う）。
    """

    text: str
    x: float
    y: float
    height: float
    page: int
    width: float = 0.0


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
        if "InvalidImageSize" in res.text or "too large" in res.text:
            raise AzureOCRError(
                "ファイルがOCRの上限（無料プランは4MB）を超えています。"
                "画像は自動圧縮されますが、PDFの場合はページを分割するか、"
                "Azureの有料プラン(S1)への切り替えをご検討ください。"
            )
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
                    width=max(xs) - min(xs),
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


def split_text_clusters(lines: list[OcrLine]) -> list[list[OcrLine]]:
    """1枚の画像内で空間的に離れたテキストのかたまりに分割する。

    複数のレシートを1枚の写真に収めた場合に、レシートごとのグループへ
    分ける用途。行同士が縦横とも近ければ同じグループとみなす
    （Union-Find による連結成分）。数行しかない小さなグループは
    誤分割とみなして最寄りのグループに併合する。
    """
    if len(lines) < 2:
        return [lines] if lines else []

    def box(ln: OcrLine) -> tuple[float, float, float, float]:
        w = ln.width if ln.width > 0 else len(ln.text) * ln.height * 0.6
        return (ln.x, ln.y - ln.height / 2, ln.x + w, ln.y + ln.height / 2)

    boxes = [box(ln) for ln in lines]

    # 文字の大きさの目安。横向きに撮影された（テキストが90度回転した）
    # レシートでは外接矩形の高さが「行の長さ」になってしまうため、
    # 短い方の辺を文字サイズとみなす（回転に依存しない尺度）。
    sizes = sorted(min(b[2] - b[0], b[3] - b[1]) for b in boxes)
    scale = sizes[len(sizes) // 2] or 1.0
    tol = 3.0 * scale

    parent = list(range(len(lines)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    # 矩形同士の隙間（重なっていれば 0）で近さを測る。中心間距離では、
    # 回転して細長くなった行同士を誤って遠いと判定してしまうため。
    for i in range(len(lines)):
        ax0, ay0, ax1, ay1 = boxes[i]
        for j in range(i + 1, len(lines)):
            if lines[i].page != lines[j].page:
                continue
            bx0, by0, bx1, by1 = boxes[j]
            dx = max(0.0, max(ax0, bx0) - min(ax1, bx1))
            dy = max(0.0, max(ay0, by0) - min(ay1, by1))
            if dx < tol and dy < tol:
                union(i, j)

    groups: dict[int, list[OcrLine]] = {}
    for i, ln in enumerate(lines):
        groups.setdefault(find(i), []).append(ln)
    clusters = list(groups.values())

    # 小さすぎるグループ（3行未満）は誤分割の可能性が高いので最寄りに併合
    def centroid(cluster: list[OcrLine]) -> tuple[float, float]:
        return (
            sum(l.x for l in cluster) / len(cluster),
            sum(l.y for l in cluster) / len(cluster),
        )

    merged = True
    while merged and len(clusters) > 1:
        merged = False
        for small in clusters:
            if len(small) >= 3:
                continue
            sx, sy = centroid(small)
            others = [c for c in clusters if c is not small]
            nearest = min(
                others,
                key=lambda c: (centroid(c)[0] - sx) ** 2 + (centroid(c)[1] - sy) ** 2,
            )
            nearest.extend(small)
            clusters.remove(small)
            merged = True
            break

    clusters.sort(key=lambda c: (min(l.page for l in c), min(l.y for l in c), min(l.x for l in c)))
    for c in clusters:
        c.sort(key=lambda l: (l.page, l.y, l.x))
    return clusters
