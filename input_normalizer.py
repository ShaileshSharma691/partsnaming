# PDF/image → NormalizedPage

"""input_normalizer.py – turn any supported input into a NormalizedPage.

Supported inputs:
  * vector PDF   (has useful native text spans)
  * raster PDF   (little/no native text — will be rendered)
  * image file   (PNG / JPG / JPEG / WebP / BMP / TIFF)

Coordinate system contract
--------------------------
Every downstream component (vector source, vision source, merger, bubble
engine) works in "page points".  For a PDF that is the PDF user-space unit
(1/72 inch).  For an image we treat 1 pixel == 1 point; the final pixel
mapping in pipeline_adapter.py handles the scaling.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from PIL import Image, ImageOps


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


@dataclass
class NormalizedPage:
    source_path: Path
    kind: str                          # "pdf" | "image"
    page_num: int                      # 0-based; 0 for images
    page_count: int
    width_pt: float
    height_pt: float
    image: Image.Image                 # rendered raster for vision
    image_dpi: int                     # effective DPI of `image`
    pdf_doc: "fitz.Document | None" = None
    vector_span_count: int = 0
    has_vector_text: bool = False

    def close(self):
        if self.pdf_doc is not None:
            try:
                self.pdf_doc.close()
            except Exception:
                pass
            self.pdf_doc = None


def _render_pdf_page(doc, page_num: int, dpi: int) -> Image.Image:
    page = doc[page_num]
    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _count_text_spans(doc, page_num: int) -> int:
    try:
        page = doc[page_num]
        raw = page.get_text("dict")
        n = 0
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if (span.get("text") or "").strip():
                        n += 1
        return n
    except Exception:
        return 0


def normalize_pdf(path: Path, page_num: int | None, vision_dpi: int,
                  vector_min_spans: int) -> NormalizedPage:
    doc = fitz.open(str(path))
    if page_num is None:
        page_num = len(doc) - 1
    if page_num < 0 or page_num >= len(doc):
        doc.close()
        raise ValueError(f"Page {page_num} out of range (0..{len(doc)-1})")
    page = doc[page_num]
    rect = page.rect
    spans = _count_text_spans(doc, page_num)
    image = _render_pdf_page(doc, page_num, vision_dpi)
    return NormalizedPage(
        source_path=path,
        kind="pdf",
        page_num=page_num,
        page_count=len(doc),
        width_pt=float(rect.width),
        height_pt=float(rect.height),
        image=image,
        image_dpi=vision_dpi,
        pdf_doc=doc,
        vector_span_count=spans,
        has_vector_text=spans >= vector_min_spans,
    )


def normalize_image(path: Path) -> NormalizedPage:
    img = Image.open(path)
    img.load()
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    w, h = img.size
    return NormalizedPage(
        source_path=path,
        kind="image",
        page_num=0,
        page_count=1,
        width_pt=float(w),
        height_pt=float(h),
        image=img,
        image_dpi=72,
        pdf_doc=None,
        vector_span_count=0,
        has_vector_text=False,
    )


def normalize_input(path: Path, page_num: int | None = None,
                    vision_dpi: int = 150,
                    vector_min_spans: int = 10) -> NormalizedPage:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return normalize_pdf(path, page_num, vision_dpi, vector_min_spans)
    if suffix in IMAGE_EXTS:
        return normalize_image(path)
    raise ValueError(f"Unsupported input format: {suffix}")