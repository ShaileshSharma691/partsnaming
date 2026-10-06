"""candidate_merger.py – deduplicate + merge candidates from multiple sources."""
from __future__ import annotations

import math
import re
from difflib import SequenceMatcher

_SYMBOL_MAP = str.maketrans({
    "ϕ": "Ø", "φ": "Ø", "⌀": "Ø", "Φ": "Ø", "∅": "Ø",
    "º": "°", "×": "x",
})

# TB field types that should merge on text match alone (position may
# be imprecise from Gemini).
_TB_MERGE_TYPES = {
    "material_description", "material_specification", "part_number",
    "envelope", "mass", "surface_protection",
}


def _normalize_text(t: str) -> str:
    t = (t or "").strip().upper().translate(_SYMBOL_MAP)
    t = re.sub(r"\s+", "", t)
    return t.replace(",", ".")


def _similar(a: str, b: str) -> float:
    a_n, b_n = _normalize_text(a), _normalize_text(b)
    if not a_n or not b_n:
        return 0.0
    if a_n == b_n:
        return 1.0
    return SequenceMatcher(None, a_n, b_n).ratio()


def _center(bbox):
    return ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)


def _spatial_close(a, b, radius):
    ax, ay = _center(a["bbox"])
    bx, by = _center(b["bbox"])
    return math.hypot(ax - bx, ay - by) <= radius


def _source_rank(c):
    s = (c.get("source") or "vector").lower()
    return {"vector": 0, "pymupdf": 0, "gemini": 1, "ocr": 2}.get(s, 3)


def _combine(weaker: dict, stronger: dict) -> dict:
    out = dict(stronger)
    for k in ("tolerance", "parameter", "specification", "type"):
        if not out.get(k) and weaker.get(k):
            out[k] = weaker[k]
    out["merged_sources"] = sorted({
        *(stronger.get("merged_sources")
          or [stronger.get("source", "vector")]),
        *(weaker.get("merged_sources")
          or [weaker.get("source", "vision")]),
    })
    return out


def merge_candidates(vector_cands, vision_cands,
                     spatial_radius_pt: float = 28.0,
                     text_similarity: float = 0.82):
    """Three-tier merge:

    Tier A: bbox close + fuzzy text match       → same feature
    Tier B: same TB type + exact text match     → same TB field
            (position-agnostic — Gemini places TB fields imprecisely)
    """
    vlist = list(vector_cands or [])
    glist = list(vision_cands or [])
    merged = list(vlist)
    added = duplicates = replaced = 0

    for g in glist:
        match_idx = -1

        # --- Tier A: spatial + fuzzy text ---
        for i, v in enumerate(merged):
            if not _spatial_close(v, g, spatial_radius_pt):
                continue
            if _similar(v.get("text", ""), g.get("text", "")) >= text_similarity:
                match_idx = i
                break

        # --- Tier B: TB type + exact text (position-agnostic) ---
        if match_idx < 0:
            g_type = (g.get("type") or "").lower()
            g_norm = _normalize_text(g.get("text", ""))
            if g_type in _TB_MERGE_TYPES and len(g_norm) >= 3:
                for i, v in enumerate(merged):
                    if (v.get("type") or "").lower() != g_type:
                        continue
                    if _normalize_text(v.get("text", "")) == g_norm:
                        match_idx = i
                        break

        if match_idx < 0:
            merged.append(g)
            added += 1
            continue

        duplicates += 1
        existing = merged[match_idx]
        if _source_rank(g) < _source_rank(existing):
            merged[match_idx] = _combine(existing, g)
            replaced += 1
        else:
            merged[match_idx] = _combine(g, existing)

    stats = {
        "vector_in": len(vlist),
        "vision_in": len(glist),
        "merged_total": len(merged),
        "vision_added": added,
        "duplicates_removed": duplicates,
        "replaced_by_vision": replaced,
    }
    return merged, stats