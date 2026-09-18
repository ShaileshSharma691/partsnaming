"""Batch-process every PDF in the project folder, skipping ones already done."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import clockwise_numbering as cn


POPPLER_CANDIDATES = [
    r"C:\Program Files\poppler-26.09.0\Library\bin",
    r"C:\Program Files\poppler\Library\bin",
    r"C:\poppler\Library\bin",
]


def find_poppler_tool(name: str) -> str | None:
    exe = shutil.which(name)
    if exe:
        return exe
    for folder in POPPLER_CANDIDATES:
        candidate = Path(folder) / f"{name}.exe"
        if candidate.exists():
            return str(candidate)
    return None


def get_page_count(pdfinfo: str | None, pdf: Path) -> int:
    if not pdfinfo:
        return 0
    try:
        out = subprocess.run([pdfinfo, str(pdf)], capture_output=True,
                             text=True, check=True)
    except Exception:
        return 0
    for line in out.stdout.splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return 0
    return 0


def process_one(pdf: Path, root: Path, pdftoppm: str, pdfinfo: str | None,
                page_override: int, force: bool) -> str:
    part = pdf.stem
    out_dir = root / "outputs" / part
    annotated = out_dir / "annotated_drawing.png"
    excel = out_dir / "extracted_dimensions.xlsx"
    items_path = root / "reviewed_items" / f"{part}.json"

    if not force and annotated.exists() and excel.exists():
        print(f"[SKIP]  {part}  (outputs exist)")
        return "skip"

    page = page_override if page_override > 0 else (get_page_count(pdfinfo, pdf) or 1)

    with tempfile.TemporaryDirectory(prefix="partsnaming-render-") as tmp:
        image_base = Path(tmp) / part
        proc = subprocess.run(
            [pdftoppm, "-png", "-r", "200",
             "-f", str(page), "-l", str(page),
             str(pdf), str(image_base)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print(f"[FAIL]  {part}: pdftoppm exit {proc.returncode}\n{proc.stderr}")
            return "fail"

        image = Path(f"{image_base}-{page}.png")
        if not image.exists():
            print(f"[FAIL]  {part}: rendered image not found: {image}")
            return "fail"

        out_dir.mkdir(parents=True, exist_ok=True)

        use_ocr = not items_path.exists()
        if use_ocr:
            raw = cn.ocr_draft(image)
            mode_label = "ocr"
        else:
            raw = json.loads(items_path.read_text(encoding="utf-8"))
            mode_label = "items"

        items = cn.number_items(cn.normalise_items(raw))
        cn.validate_items(items, image, allow_identical_marker_target=use_ocr)
        cn.write_excel(items, excel, image)
        cn.annotate(image, items, annotated)

        if use_ocr:
            (out_dir / "ocr_review_items.json").write_text(
                json.dumps(items, indent=2), encoding="utf-8"
            )

        print(f"[OK]    {part}  (page {page}, {mode_label})")
        return "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-process all PDFs, skipping ones already done.")
    parser.add_argument("--input-dir", type=Path,
                        default=Path(__file__).resolve().parent)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--page", type=int, default=0)
    args = parser.parse_args()

    root = args.input_dir.resolve()
    if not root.exists():
        print(f"Input folder not found: {root}", file=sys.stderr)
        return 2

    pdftoppm = find_poppler_tool("pdftoppm")
    pdfinfo = find_poppler_tool("pdfinfo")
    if not pdftoppm:
        print("pdftoppm not found. Add Poppler's bin folder to PATH.", file=sys.stderr)
        return 2
    print(f"Using pdftoppm: {pdftoppm}")

    pdfs = sorted(p for p in root.glob("*.pdf") if p.is_file())
    if not pdfs:
        print(f"No PDF files found in {root}")
        return 0

    print(f"Found {len(pdfs)} PDF(s) in {root}\n")

    stats = {"ok": 0, "skip": 0, "fail": 0}
    for pdf in pdfs:
        stats[process_one(pdf, root, pdftoppm, pdfinfo, args.page, args.force)] += 1

    print()
    print("Summary")
    print(f"  Processed : {stats['ok']}")
    print(f"  Skipped   : {stats['skip']}")
    print(f"  Failed    : {stats['fail']}")
    return 1 if stats["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())