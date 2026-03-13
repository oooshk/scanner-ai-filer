from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from pathlib import Path

from pypdf import PdfReader

from .config import OCRConfig

logger = logging.getLogger(__name__)


def run_ocr(input_pdf: Path, ocr_cfg: OCRConfig) -> Path:
    if not ocr_cfg.enabled:
        return input_pdf

    if shutil.which(ocr_cfg.command) is None:
        logger.warning("ocrmypdf not found; skipping OCR for %s", input_pdf.name)
        return input_pdf

    output_pdf = input_pdf.with_name(f"ocr_{input_pdf.name}")
    cmd = [
        ocr_cfg.command,
        "--language",
        ocr_cfg.language,
    ]
    if ocr_cfg.rotate_pages:
        cmd.append("--rotate-pages")
    if ocr_cfg.deskew:
        cmd.append("--deskew")
    if ocr_cfg.clean:
        cmd.append("--clean")
    if ocr_cfg.skip_text:
        cmd.append("--skip-text")
    else:
        cmd.append("--redo-ocr")
    if ocr_cfg.extra_args:
        cmd.extend(shlex.split(ocr_cfg.extra_args))
    cmd.extend([str(input_pdf), str(output_pdf)])

    logger.info("Running OCR for %s", input_pdf.name)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("OCR failed for %s: %s", input_pdf.name, result.stderr.strip())
        return input_pdf

    input_pdf.unlink(missing_ok=True)
    output_pdf.rename(input_pdf)
    return input_pdf


def extract_text(pdf_path: Path, max_chars: int) -> str:
    try:
        reader = PdfReader(str(pdf_path))
        parts: list[str] = []
        for page in reader.pages:
            txt = page.extract_text() or ""
            if txt:
                parts.append(txt)
            if sum(len(p) for p in parts) >= max_chars:
                break
        out = "\n".join(parts).strip()
        return out[:max_chars]
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to extract text from %s: %s", pdf_path, exc)
        return ""
