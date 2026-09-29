from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from template_model.core import TemplateModel
else:
    from .core import TemplateModel


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Decompose a PPTX into per-slide metadata")
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze = subparsers.add_parser("analyze", help="Analyze a presentation template")
    analyze.add_argument("--template", required=True, type=Path)
    analyze.add_argument("--output", required=True, type=Path)
    analyze.add_argument("--vlm", action="store_true", help="Enable visual classification")
    analyze.add_argument("--libreoffice", default="libreoffice")
    analyze.add_argument("--pdftoppm", default="pdftoppm")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        model = TemplateModel(
            use_vlm=args.vlm,
            libreoffice_path=args.libreoffice,
            pdftoppm_path=args.pdftoppm,
        )
        manifest = model.analyze(args.template, args.output)
        vlm_errors = sum(slide.vlm_status.value == "error" for slide in manifest.slides)
        print(json.dumps({
            "output": str(args.output.resolve()),
            "slides": manifest.slide_count,
            "previews": sum(slide.preview_path is not None for slide in manifest.slides),
            "vlm_enabled": manifest.vlm_enabled,
            "vlm_errors": vlm_errors,
            "source_sha256": manifest.source_sha256,
        }, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
