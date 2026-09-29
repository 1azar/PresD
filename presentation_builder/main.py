from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from presentation_builder.core import PresentationBuilder
    from presentation_builder.models import BuildFailure
else:
    from .core import PresentationBuilder
    from .models import BuildFailure


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Assemble a PPTX from a template model and plan")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="Build the presentation")
    build.add_argument("--template-dir", required=True, type=Path)
    build.add_argument("--plan", required=True, type=Path)
    build.add_argument("--assets-dir", type=Path)
    build.add_argument("--output", required=True, type=Path)
    build.add_argument("--pdf-output", type=Path)
    build.add_argument("--qa-dir", type=Path)
    build.add_argument("--libreoffice", default="libreoffice")
    build.add_argument("--pdftoppm", default="pdftoppm")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = PresentationBuilder(
            libreoffice_path=args.libreoffice,
            pdftoppm_path=args.pdftoppm,
        ).build(
            args.template_dir,
            args.plan,
            args.output,
            assets_dir=args.assets_dir,
            qa_dir=args.qa_dir,
            pdf_output_path=args.pdf_output,
        )
        print(report.model_dump_json(indent=2))
        return 0
    except BuildFailure as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except ValidationError as exc:
        print(json.dumps({
            "status": "error",
            "code": "invalid_plan",
            "message": "plan JSON does not match schema",
            "validation_errors": exc.errors(include_url=False, include_input=False),
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "code": "io_error", "message": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
