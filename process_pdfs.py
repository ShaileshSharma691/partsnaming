"""Batch-process every PDF; native PDF extraction first, OCR only as fallback."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pymupdf as fitz
from PIL import Image

import clockwise_numbering as cn


POPPLER_CANDIDATES = [
    r"C:\Program Files\poppler-26.09.0\Library\bin",
    r"C:\Program Files\poppler\Library\bin",
    r"C:\poppler\Library\bin",
]


def find_poppler_tool(name: str):
    exe = shutil.which(name)
    if exe:
        return exe
    for folder in POPPLER_CANDIDATES:
        candidate = Path(folder) / f"{name}.exe"
        if candidate.exists():
            return str(candidate)
    return None


def get_page_count_fitz(pdf: Path) -> int:
    """Use PyMuPDF to count pages — no subprocess needed."""
    try:
        doc = fitz.open(str(pdf))
        n = len(doc)
        doc.close()
        return n
    except Exception:
        return 0


def render_page_fitz(pdf: Path, page_num: int, out_png: Path, dpi: int = 300) -> bool:
    """
    Render one PDF page to a PNG using PyMuPDF.

    Replaces the pdftoppm subprocess call that fails with
    STATUS_DLL_INIT_FAILED (0xC0000142) when spawned from Python on Windows.
    PyMuPDF is already imported for extraction, so this adds zero new deps.
    """
    try:
        doc = fitz.open(str(pdf))
        page = doc[page_num]          # 0-based
        scale = dpi / 72.0
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        pix.save(str(out_png))
        doc.close()
        return True
    except Exception as e:
        print(f"  [render_page_fitz] {e}")
        return False


def process_one(pdf, root, page_override, force):
    part = pdf.stem
    out_dir = root / "outputs" / part
    annotated = out_dir / "annotated_drawing.png"
    excel = out_dir / "extracted_dimensions.xlsx"
    reviewed_path = root / "reviewed_items" / f"{part}.json"

    if not force and annotated.exists() and excel.exists():
        print(f"[SKIP]  {part}  (outputs exist)")
        return "skip"

    # Determine which 1-based page to render
    total_pages = get_page_count_fitz(pdf)
    if page_override > 0:
        page_1based = page_override
    else:
        page_1based = total_pages if total_pages > 0 else 1
    fitz_page = max(0, page_1based - 1)   # fitz is 0-based

    with tempfile.TemporaryDirectory(prefix="partsnaming-render-") as tmp:
        image = Path(tmp) / f"{part}.png"

        ok = render_page_fitz(pdf, fitz_page, image, dpi=300)
        if not ok or not image.exists():
            print(f"[FAIL]  {part}: PyMuPDF render failed for page {page_1based}")
            return "fail"

        out_dir.mkdir(parents=True, exist_ok=True)

        with Image.open(image) as im:
            png_size = im.size

        if reviewed_path.exists():
            raw = json.loads(reviewed_path.read_text(encoding="utf-8"))
            mode_label = "items"
        else:
            try:
                raw, _ = cn.pdf_draft(pdf, fitz_page, target_size=png_size)
                mode_label = "pdf-native"
            except Exception as e:
                print(f"[WARN] {part}: PDF extraction failed ({e}); falling back to OCR")
                raw = cn.ocr_draft(image)
                mode_label = "ocr"

        items = cn.number_items(cn.normalise_items(raw))
        cn.validate_items(items, image, allow_identical_marker_target=True)
        cn.write_excel(items, excel, image)
        cn.annotate(image, items, annotated)

        if mode_label == "ocr":
            (out_dir / "ocr_review_items.json").write_text(
                json.dumps(items, indent=2), encoding="utf-8"
            )
        else:
            (out_dir / "extracted_candidates.json").write_text(
                json.dumps(items, indent=2), encoding="utf-8"
            )

        print(f"[OK]    {part}  (page {page_1based}, {mode_label}, {len(items)} items)")
        return "ok"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--page", type=int, default=0)
    args = parser.parse_args()

    root = args.input_dir.resolve()
    if not root.exists():
        print(f"Input folder not found: {root}", file=sys.stderr)
        return 2

    pdfs = sorted(p for p in root.glob("*.pdf") if p.is_file())
    if not pdfs:
        print(f"No PDF files found in {root}")
        return 0
    print(f"Found {len(pdfs)} PDF(s)\n")

    stats = {"ok": 0, "skip": 0, "fail": 0}
    for pdf in pdfs:
        stats[process_one(pdf, root, args.page, args.force)] += 1

    print()
    print("Summary")
    print(f"  Processed : {stats['ok']}")
    print(f"  Skipped   : {stats['skip']}")
    print(f"  Failed    : {stats['fail']}")
    return 1 if stats["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())