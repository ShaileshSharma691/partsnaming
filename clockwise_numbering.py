from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
import datetime

BLUE = (32, 91, 177)
TITLE_TYPES = ("material_description", "material_specification")


def clockwise_angle(x: float, y: float, centre_x: float, centre_y: float) -> float:
    """Angle starting at 12 o'clock and increasing clockwise."""
    return (math.atan2(x - centre_x, -(y - centre_y)) + 2 * math.pi) % (2 * math.pi)


def number_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply title-block, clockwise, mass and final-note ordering rules."""
    descriptions = [item for item in items if item["type"] == "material_description"]
    specifications = [item for item in items if item["type"] == "material_specification"]
    features = [item for item in items if item["type"] == "feature"]
    masses = [item for item in items if item["type"] == "mass"]
    notes = [item for item in items if item["type"] == "note"]

    # Complete each view before moving to the next; views are ordered left-to-right.
    views: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for feature in features:
        views[str(feature.get("view", "main"))].append(feature)

    sorted_views = sorted(views.values(), key=lambda view: sum(i["x"] for i in view) / len(view))
    ordered_features: list[dict[str, Any]] = []
    for view in sorted_views:
        if all("order" in item for item in view):
            ordered_features.extend(sorted(view, key=lambda item: item["order"]))
            continue
        centre_x = sum(item["x"] for item in view) / len(view)
        centre_y = sum(item["y"] for item in view) / len(view)
        ordered_features.extend(sorted(view, key=lambda item: clockwise_angle(item["x"], item["y"], centre_x, centre_y)))

    ordered = descriptions[:1] + specifications[:1] + ordered_features + masses[:1] + notes[:1]
    for serial, item in enumerate(ordered, start=1):
        item["serial"] = serial
    return ordered


def normalise_items(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    required = {"type", "parameter", "specification", "x", "y"}
    allowed = {"material_description", "material_specification", "feature", "mass", "note"}
    items = []
    for index, item in enumerate(raw, start=1):
        missing = required - item.keys()
        if missing:
            raise ValueError(f"Item {index} is missing: {', '.join(sorted(missing))}")
        if item["type"] not in allowed:
            raise ValueError(f"Item {index} has unsupported type: {item['type']}")
        result = dict(item)
        result.setdefault("tolerance", "N/A")
        result.setdefault("notes", "")
        result.setdefault("target_x", result["x"])
        result.setdefault("target_y", result["y"])
        items.append(result)
    return items


def validate_items(items: list[dict[str, Any]], image_path: Path,
                   allow_identical_marker_target: bool = False) -> None:
    """Reject invalid annotation coordinates before any output is created."""
    with Image.open(image_path) as image:
        width, height = image.size
    for item in items:
        for key, limit in (("x", width), ("target_x", width),
                           ("y", height), ("target_y", height)):
            value = item[key]
            if not isinstance(value, (int, float)) or not 0 <= value < limit:
                raise ValueError(
                    f"{item['parameter']}: {key}={value!r} is outside image bounds "
                    f"({width} x {height}). Review the JSON marker coordinates."
                )
        if (not allow_identical_marker_target
                and item["x"] == item["target_x"]
                and item["y"] == item["target_y"]):
            raise ValueError(
                f"{item['parameter']}: marker and leader target are identical. "
                "Place the marker in whitespace and target the exact callout."
            )

def _spread_markers(
    items: list[dict[str, Any]],
    min_distance: float = 60.0,
    width: int = 0,
    height: int = 0,
    iterations: int = 80,
) -> None:
    """Relax bubble positions so markers don't overlap.

    Only `x`/`y` (bubble centre) are moved. `target_x`/`target_y`
    stay fixed so each leader still points at the exact callout.
    """
    n = len(items)
    if n < 2:
        return

    for _ in range(iterations):
        moved = False
        for i in range(n):
            a = items[i]
            for j in range(i + 1, n):
                b = items[j]
                dx = a["x"] - b["x"]
                dy = a["y"] - b["y"]
                dist = math.hypot(dx, dy)
                if dist < min_distance:
                    if dist < 1e-6:
                        dx, dy = 1.0, -1.0
                        dist = math.hypot(dx, dy)
                    push = (min_distance - dist) / 2.0
                    ux, uy = dx / dist, dy / dist
                    a["x"] += ux * push
                    a["y"] += uy * push
                    b["x"] -= ux * push
                    b["y"] -= uy * push
                    moved = True
        if not moved:
            break

    if width and height:
        for item in items:
            item["x"] = max(40, min(width - 40, item["x"]))
            item["y"] = max(40, min(height - 40, item["y"]))

# ---------------------------------------------------------------------------
# OCR noise filter — only "important" callouts survive.
# ---------------------------------------------------------------------------

_OCR_REJECT = re.compile(
    r"do not scale|if in doubt|first angle|drawing drawn|"
    r"all dimn|dimensions in mm|"
    r"sheet\s*(no|size)|original drawing|size a[0-9]|"
    r"for (all vehicle|m1|m1 & n1|bar code|numbering|list of)|"
    r"tata motors|sharada industries|erc\s*-?\s*pune|cft sign off|"
    r"class(ification)? of characteristics|"
    r"applies to (upper|lower)|key instruction|"
    r"this drawing is the sole property|"
    r"it should not be copied|"
    r"without the written approval|"
    r"for (explanation of drawing|details of title block)|"
    r"envelope dimensions|"
    r"material description|"
    r"nominal dimension|tolerance in um|"
    r"surface roughness|machining deviation|"
    r"\b(revision|revised|redrawn|updated|feedback|trivalent|fasteners?)\b|"
    r"new release|"
    r"\b(ppm|hmp|rmp|skk)\b|"
    r"deburr|"
    r"drg[/ ]part|opposite hand|reference drg|replaces drg|"
    r"surface protection|"
    r"fe\s*zn|ss\s*:?\s*8451|"
    r"ts\s*(10806|11079|11075|11084|11300|11103|11500|11414|11418|11419|11420)|"
    r"scale\s*:|"
    r"\b(qty|sign|date|chkd|appd|drawn|zone|sr\.?\s*no)\b",
    re.IGNORECASE,
)

# Strong accept patterns — the ONLY shapes that become bubble candidates.
_OCR_ACCEPT = [
    # R7, R12.5, R7 TYP
    (re.compile(r"^r\s*\d+(?:\.\d+)?(?:\s+typ)?$", re.IGNORECASE),
     "feature", "Radius"),
    # Dia 9, Ø13, DIA 10.5 H11, Phi 12
    (re.compile(r"^(?:dia|ø|phi|0|o)\s*\d+(?:\.\d+)?(?:\s*(?:h|js)\d+)?$", re.IGNORECASE),
     "feature", "Hole diameter"),
    # 15 deg, 15°, 90°
    (re.compile(r"^\d+(?:\.\d+)?\s*(?:deg|°|degree)$", re.IGNORECASE),
     "feature", "Angle"),
    # 0.190 kg
    (re.compile(r"^\d+(?:\.\d+)?\s*kg$", re.IGNORECASE),
     "mass", "Final mass"),
    # TACK WELDING AT 3 PLACES
    (re.compile(r"tack\s+weld", re.IGNORECASE),
     "feature", "Weld requirement"),
    # General tolerance note (only this exact style, not the column header)
    (re.compile(r"general tolerance\s+to\s+be\s+maintained|general tolerance\s+as\s+per\s+ts", re.IGNORECASE),
     "note", "General note"),
    # Bare dimensions and reference dims: 42.3, (62.5), 26 ±0.3, 9 +/-0.2, 12
    (re.compile(r"^\(?\d+(?:\.\d+)?\)?(?:\s*[±+\-/]\s*\d+(?:\.\d+)?)?$"),
     "feature", "Dimension"),
    # Material: THICK SHEET 4 mm / SHEET 3.15 THK / 5 mm Thk - T SH HR-Fe410
    (re.compile(r"^\d+(?:\.\d+)?\s*mm\s*thk", re.IGNORECASE),
     "material_description", "Material / thickness"),
    (re.compile(r"\bsheet\b.*?\d+(?:\.\d+)?", re.IGNORECASE),
     "material_description", "Material / thickness"),
    (re.compile(r"^\d+(?:\.\d+)?\s*thk\b", re.IGNORECASE),
     "material_description", "Material / thickness"),
    # Material spec: T SH HR-E46 TS23521 / DD: 1079, SS:4013A / TS23521
    (re.compile(r"\b(?:t\s*sh|hr[-\s]?e?\d|fe\s*\d|ss\s*:?\s*\d|dd\s*:?\s*\d|ts\s*\d{5})\b",
                re.IGNORECASE),
     "material_specification", "Material specification"),
]


def _classify_ocr_line(text: str) -> tuple[str | None, str]:
    """Return (item_type, parameter) or (None, '') if the line is noise."""
    t = text.strip()
    if not t:
        return None, ""

    # Strip trailing/leading decorations before matching
    clean = re.sub(r"[;:,]+$", "", t).strip()

    if _OCR_REJECT.search(clean):
        return None, ""

    for pattern, item_type, parameter in _OCR_ACCEPT:
        if pattern.match(clean):
            return item_type, parameter

    return None, ""


def _spread_markers(items, min_distance=90.0, width=0, height=0, iterations=120):
    """Push overlapping bubbles apart; leaders stay anchored to targets."""
    n = len(items)
    if n < 2:
        return
    for _ in range(iterations):
        moved = False
        for i in range(n):
            a = items[i]
            for j in range(i + 1, n):
                b = items[j]
                dx, dy = a["x"] - b["x"], a["y"] - b["y"]
                dist = math.hypot(dx, dy)
                if dist < min_distance:
                    if dist < 1e-6:
                        dx, dy, dist = 1.0, -1.0, math.hypot(1.0, 1.0)
                    push = (min_distance - dist) / 2.0
                    ux, uy = dx / dist, dy / dist
                    a["x"] += ux * push; a["y"] += uy * push
                    b["x"] -= ux * push; b["y"] -= uy * push
                    moved = True
        if not moved:
            break
    if width and height:
        for item in items:
            item["x"] = max(40, min(width - 40, item["x"]))
            item["y"] = max(40, min(height - 40, item["y"]))


def ocr_draft(image_path: Path) -> list[dict[str, Any]]:
    """OCR draft that mirrors the layout of a hand-curated reviewed JSON.

    Only real dimension callouts, the material call, the mass, and the
    general-tolerance note survive the filter. Everything else is dropped.
    """
    try:
        import pytesseract
        from pytesseract import Output
    except ImportError as exc:
        raise RuntimeError("Install OCR support first: python -m pip install pytesseract") from exc

    try:
        with Image.open(image_path) as img:
            width, height = img.size
            data = pytesseract.image_to_data(img, output_type=Output.DICT, config="--psm 11")
    except pytesseract.TesseractNotFoundError as exc:
        raise RuntimeError("Install Tesseract OCR and add it to PATH before using --ocr.") from exc

    lines: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    for index, text in enumerate(data["text"]):
        if text.strip():
            lines[(data["block_num"][index], data["par_num"][index], data["line_num"][index])].append(index)

    result: list[dict[str, Any]] = []
    for indices in lines.values():
        text = " ".join(data["text"][i] for i in indices).strip()
        item_type, parameter = _classify_ocr_line(text)
        if not item_type:
            continue

        x = min(data["left"][i] for i in indices)
        y = min(data["top"][i] for i in indices)
        right = max(data["left"][i] + data["width"][i] for i in indices)
        bottom = max(data["top"][i] + data["height"][i] for i in indices)
        centre_x, centre_y = (x + right) // 2, (y + bottom) // 2

        # Leader tip on the callout, bubble offset into whitespace.
        # Default: 80 px left / 90 px up; mirror right if too close to left edge.
        offset_x = 80 if centre_x < 240 else -80
        offset_y = -90
        marker_x = max(40, min(width - 40, centre_x + offset_x))
        marker_y = max(40, min(height - 40, centre_y + offset_y))

        result.append({
            "type": item_type,
            "parameter": parameter,
            "specification": text,
            "tolerance": "OCR review required",
            "x": marker_x, "y": marker_y,
            "target_x": centre_x, "target_y": centre_y,
            "notes": "Draft detected by OCR",
        })

    _spread_markers(result, min_distance=90.0, width=width, height=height)
    return result

def safe_write(sheet, row: int, col: int, value) -> None:
    """Write to a cell, safely handling merged cells.

    If the target cell is part of a merged range, the value is written
    to the top-left cell of that range instead.
    """
    cell = sheet.cell(row=row, column=col)
    if isinstance(cell, MergedCell):
        # Find the merged range that contains this cell
        for merged_range in sheet.merged_cells.ranges:
            if cell.coordinate in merged_range:
                top_left = sheet.cell(
                    row=merged_range.min_row,
                    column=merged_range.min_col,
                )
                top_left.value = value
                return
    else:
        cell.value = value


def write_excel(items: list[dict[str, Any]], output_path: Path, image_path: Path) -> None:
    """Load the AOI template and inject extracted data."""
    template_path = Path(__file__).resolve().parent / "554743810147 AOI.xlsx"
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")

    workbook = load_workbook(template_path)
    sheet = workbook.worksheets[0]          # first sheet: 554743810147AOI

    # ---------- header updates ----------
    # Extract part number from the image filename (e.g. "SAMPLE_2-1.png" -> "SAMPLE_2")
    part_no = image_path.stem.split('-')[0]

    safe_write(sheet, 2, 3, part_no)        # C2  – PART NO
    safe_write(sheet, 4, 3, part_no)        # C4  – DRAWING NO

    today = datetime.date.today().strftime("%d.%m.%Y")
    safe_write(sheet, 2, 14, f"DATE:-{today}")  # N2  – DATE

    # ---------- data table (starts at row 10) ----------
    start_row = 10
    for i, item in enumerate(items):
        r = start_row + i
        safe_write(sheet, r, 1, item["serial"])          # A – Sr. No.
        safe_write(sheet, r, 2, item["parameter"])       # B – Parameter
        safe_write(sheet, r, 3, item["specification"])   # C – Specification
        safe_write(sheet, r, 4, item["tolerance"])       # D – Tolerance
        # Columns E and beyond are left untouched (pre-filled template data)

    workbook.save(output_path)


def annotate(image_path: Path, items: list[dict[str, Any]], output_path: Path) -> None:
    image = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("arial.ttf", 24)
    radius = 24
    for item in items:
        x, y = item["x"], item["y"]
        draw.line((x, y, item["target_x"], item["target_y"]), fill=BLUE, width=3)
        # White fill ensures the leader cannot obscure the serial number.
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill="white", outline=BLUE, width=4)
        text = str(item["serial"])
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        draw.text((x - (right - left) / 2, y - (bottom - top) / 2 - top), text, fill=BLUE, font=font)
    image.save(output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create clockwise engineering-drawing labels and table.")
    parser.add_argument("image", type=Path, help="Input drawing image")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--items", type=Path, help="Reviewed item JSON")
    source.add_argument("--ocr", action="store_true", help="Create a reviewable OCR draft")
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    raw = json.loads(args.items.read_text(encoding="utf-8")) if args.items else ocr_draft(args.image)
    items = number_items(normalise_items(raw))
    validate_items(items, args.image, allow_identical_marker_target=args.ocr)
    write_excel(items, args.output_dir / "extracted_dimensions.xlsx", args.image)
    annotate(args.image, items, args.output_dir / "annotated_drawing.png")
    if args.ocr:
        (args.output_dir / "ocr_review_items.json").write_text(json.dumps(items, indent=2), encoding="utf-8")
    print(f"Created {args.output_dir / 'annotated_drawing.png'}")
    print(f"Created {args.output_dir / 'extracted_dimensions.xlsx'}")


if __name__ == "__main__":
    main()
