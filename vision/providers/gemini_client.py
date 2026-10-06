"""GeminiVisionClient – Fixes [ymin, xmin, ymax, xmax] order for proper merging."""
from __future__ import annotations
import json
import logging
import random
import re
import time
from PIL import Image
from vision.base import VisionModelClient

log = logging.getLogger(__name__)

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "type": {"type": "string"},
                    "parameter": {"type": "string"},
                    "specification": {"type": "string"},
                    "tolerance": {"type": "string"},
                    "bbox_1000": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Exactly [ymin, xmin, ymax, xmax] mapped to 0-1000 scale"
                    }
                },
                "required": ["text", "bbox_1000"],
            },
        }
    },
    "required": ["candidates"],
}

def _build_prompt() -> str:
    return """\
You are a mechanical-engineering drawing inspector.
Return EVERY visible inspection characteristic as structured JSON.
INCLUDE: linear dimensions, tolerances, holes, radii, angles, part numbers, mass.
EXCLUDE: title/scale text, frame grids, boilerplate notes.

CRITICAL COORDINATE RULE:
You MUST output "bbox_1000" as an array of exactly 4 numbers in this EXACT ORDER:
[ymin, xmin, ymax, xmax]
These coordinates MUST be normalized to a 0-1000 scale where [0,0] is top-left and [1000,1000] is bottom-right.
"""

class GeminiVisionClient(VisionModelClient):
    provider_name = "gemini"

    def __init__(self, api_key: str, model_name: str, timeout_s: int = 120, max_retries: int = 3):
        if not api_key: raise ValueError("Gemini API key missing")
        from google import genai
        from google.genai import types
        self._types = types
        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name
        self.max_retries = max_retries

    def extract_candidates(self, image, page_width_pt, page_height_pt, page_num=0, context=None):
        for attempt in range(self.max_retries + 1):
            try:
                raw = self._call_model(image)
                return self._parse(raw, page_width_pt, page_height_pt)
            except Exception:
                time.sleep(2)
        return []

    def _call_model(self, image: Image.Image) -> str:
        try:
            afc = self._types.AutomaticFunctionCallingConfig(disable=True)
            cfg = self._types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_RESPONSE_SCHEMA,
                temperature=0.1, automatic_function_calling=afc,
            )
        except Exception:
            cfg = self._types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_RESPONSE_SCHEMA, temperature=0.1,
            )
        resp = self.client.models.generate_content(
            model=self.model_name, contents=[_build_prompt(), image], config=cfg
        )
        return getattr(resp, "text", "")

    @staticmethod
    def _parse(raw: str, page_w: float, page_h: float) -> list[dict]:
        raw = raw.strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m: raw = m.group(0)
        try: data = json.loads(raw)
        except Exception: return []

        out = []
        for it in data.get("candidates", []):
            b = it.get("bbox_1000")
            if not b or len(b) != 4: continue

            # GEMINI NATIVE FORMAT is [ymin, xmin, ymax, xmax]. Parsing correctly!
            ymin = max(0.0, min(1000.0, float(b[0])))
            xmin = max(0.0, min(1000.0, float(b[1])))
            ymax = max(0.0, min(1000.0, float(b[2])))
            xmax = max(0.0, min(1000.0, float(b[3])))

            x1 = (xmin / 1000.0) * page_w
            y1 = (ymin / 1000.0) * page_h
            x2 = (xmax / 1000.0) * page_w
            y2 = (ymax / 1000.0) * page_h

            if x2 < x1: x1, x2 = x2, x1
            if y2 < y1: y1, y2 = y2, y1
            if (x2 - x1) < 1 and (y2 - y1) < 1: continue

            text = (it.get("text") or "").strip()
            if not text: continue

            out.append({
                "text": text,
                "type": (it.get("type") or "feature").strip().lower(),
                "parameter": (it.get("parameter") or "Dimn").strip(),
                "specification": (it.get("specification") or text).strip(),
                "tolerance": (it.get("tolerance") or "").strip(),
                "bbox": (x1, y1, x2, y2),
                "anchor_x": (x1 + x2) / 2,
                "anchor_y": (y1 + y2) / 2,
                "source": "gemini"
            })
        return out