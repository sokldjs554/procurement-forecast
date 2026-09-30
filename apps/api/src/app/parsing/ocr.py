"""OCR for scanned budget books.

Tesseract (``kor+eng``) runs locally in the worker image; a cloud OCR (Naver CLOVA, Upstage
Document Parse, Google Document AI) can be plugged in behind the same interface when accuracy on
tables matters more than cost. Preprocessing is tuned for 행정 문서 scans: grayscale, autocontrast,
light denoise, then Otsu binarisation.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageFilter, ImageOps


@dataclass(frozen=True, slots=True)
class OCRResult:
    text: str
    confidence: float  # mean word confidence in [0, 1]
    engine: str


class OCREngine(Protocol):
    name: str

    async def recognize(self, image: Image.Image) -> OCRResult: ...


def _otsu_threshold(img: Image.Image) -> int:
    hist = img.histogram()
    total = sum(hist)
    sum_all = sum(i * h for i, h in enumerate(hist))
    sum_b = 0.0
    w_b = 0
    best, threshold = 0.0, 127
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        if between > best:
            best, threshold = between, t
    return threshold


def preprocess(image: Image.Image) -> Image.Image:
    gray = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=1)
    gray = gray.filter(ImageFilter.MedianFilter(size=3))
    threshold = _otsu_threshold(gray)
    return gray.point(lambda p: 255 if p > threshold else 0, mode="L")


class TesseractOCR:
    name = "tesseract"

    def __init__(
        self,
        languages: str = "kor+eng",
        psm: int = 6,
        *,
        binary: str = "tesseract",
        timeout: float = 120.0,
    ) -> None:
        self._languages = languages
        self._config = ["--oem", "1", "--psm", str(psm), "-c", "preserve_interword_spaces=1"]
        self._binary = shutil.which(binary) or binary
        self._timeout = timeout

    @property
    def cache_key(self) -> str:
        return json.dumps([self.name, self._languages, self._config, self._binary])

    def _run(self, image: Image.Image) -> OCRResult:
        prepared = preprocess(image)
        # One tesseract run, two renderers: plain text (its own word spacing handles Korean far
        # better than re-joining TSV words — measured CER 0.055 vs 0.114 on budget scans) and
        # TSV for per-word confidences.
        with tempfile.TemporaryDirectory(prefix="app-ocr-") as tmp:
            src = Path(tmp) / "page.png"
            prepared.save(src)
            out = Path(tmp) / "out"
            subprocess.run(  # noqa: S603 - fixed binary, arguments are not user input
                [
                    self._binary,
                    str(src),
                    str(out),
                    "-l",
                    self._languages,
                    *self._config,
                    "txt",
                    "tsv",
                ],
                check=True,
                capture_output=True,
                timeout=self._timeout,
            )
            text = out.with_suffix(".txt").read_text(encoding="utf-8")
            tsv = out.with_suffix(".tsv").read_text(encoding="utf-8")
        confidences: list[float] = []
        for row in tsv.splitlines()[1:]:
            cols = row.split("\t")
            if len(cols) == 12 and cols[11].strip():
                try:
                    conf = float(cols[10])
                except ValueError:
                    continue
                if conf >= 0:
                    confidences.append(conf / 100)
        mean_conf = sum(confidences) / len(confidences) if confidences else 0.0
        clean = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
        return OCRResult(clean, round(mean_conf, 3), self.name)

    async def recognize(self, image: Image.Image) -> OCRResult:
        # CPU-bound: keep the event loop (and the other jobs on this worker) responsive.
        return await asyncio.to_thread(self._run, image)
