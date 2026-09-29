from __future__ import annotations

import json
from pathlib import Path


ICON_ROOT = Path(__file__).parent / "assets" / "lucide"
_MANIFEST = json.loads((ICON_ROOT / "manifest.json").read_text(encoding="utf-8"))


def resolve_icon(query: str) -> tuple[Path, bool]:
    normalized = " ".join(query.lower().strip().split())
    for name, aliases in _MANIFEST["icons"].items():
        if normalized == name or normalized in aliases:
            return ICON_ROOT / f"{name}.svg", True
    return ICON_ROOT / "circle-help.svg", False

