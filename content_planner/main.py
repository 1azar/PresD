from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from content_planner.catalog import CatalogBuildError, TemplateCatalogBuilder
    from content_planner.client import OpenAICompatibleClient
    from content_planner.demo import demo_request
    from content_planner.models import PlanningRequest, PresentationPlan, TemplateCatalog
    from content_planner.planner import ContentPlanner, PlannerFailure
else:
    from .catalog import CatalogBuildError, TemplateCatalogBuilder
    from .client import OpenAICompatibleClient
    from .demo import demo_request
    from .models import PlanningRequest, PresentationPlan, TemplateCatalog
    from .planner import ContentPlanner, PlannerFailure


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_existing_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    from_cwd = path.resolve()
    if from_cwd.exists():
        return from_cwd
    return (PROJECT_ROOT / path).resolve()


def _resolve_output_path(path: Path) -> Path:
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a grounded presentation plan from text and a decomposed PPTX template"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    catalog = subparsers.add_parser("build-catalog", help="Build or refresh the semantic template catalog")
    catalog.add_argument("--template-dir", required=True, type=Path)
    catalog.add_argument("--output", type=Path)
    catalog.add_argument("--force", action="store_true")
    catalog.add_argument("--workers", type=int, default=4, help="Parallel catalog reduction calls")

    generate = subparsers.add_parser("generate", help="Generate a plan from a JSON request")
    generate.add_argument("input_path", nargs="?", type=Path, help="Path to the request JSON")
    generate.add_argument("--input", dest="input_option", type=Path, help="Path to the request JSON")
    generate.add_argument("--template-dir", required=True, type=Path)
    generate.add_argument("--output", required=True, type=Path)
    generate.add_argument(
        "--fast", action="store_true",
        help="Skip the separate semantic critique call after deterministic validation",
    )

    demo = subparsers.add_parser("demo", help="Run the built-in IT team and product example")
    demo.add_argument("--template-dir", required=True, type=Path)
    demo.add_argument("--output", required=True, type=Path)
    demo.add_argument(
        "--fast", action="store_true",
        help="Skip the separate semantic critique call after deterministic validation",
    )
    return parser


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"status": "error", "code": code, "message": message, **details}


def _write_atomic_json(value: PresentationPlan | TemplateCatalog, output: Path) -> None:
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Output file already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(value.model_dump_json(indent=2))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    except Exception:
        if temporary and temporary.exists():
            temporary.unlink()
        raise


def _generate(
    request: PlanningRequest, template_dir: Path, *, fast: bool = False
) -> PresentationPlan:
    llm = OpenAICompatibleClient()
    return ContentPlanner(
        template_dir, llm=llm, enable_critique=not fast,
        max_revisions=1 if fast else 2,
        planning_budget=135 if fast else 300,
    ).generate(request)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build-catalog":
            template_dir = _resolve_existing_path(args.template_dir)
            cache_path = (
                _resolve_output_path(args.output)
                if args.output else template_dir / "planner_catalog.json"
            )
            if args.output and cache_path.exists() and not args.force:
                raise FileExistsError(f"Output file already exists: {cache_path}")
            catalog = TemplateCatalogBuilder(
                template_dir, cache_path=cache_path,
                max_workers=args.workers,
            ).build(force=args.force)
            print(json.dumps({
                "status": "ok",
                "catalog": str(Path(cache_path).resolve()),
                "families": len(catalog.families),
                "slides": sum(len(family.variants) for family in catalog.families),
                "model": catalog.model,
            }, ensure_ascii=False, indent=2))
            return 0

        output_path = _resolve_output_path(args.output)
        template_dir = _resolve_existing_path(args.template_dir)
        if output_path.exists():
            raise FileExistsError(f"Output file already exists: {output_path}")
        if args.command == "demo":
            request = demo_request()
        else:
            input_path = args.input_option or args.input_path
            if input_path is None:
                raise ValueError("generate requires an input JSON path or --input")
            input_path = _resolve_existing_path(input_path)
            request = PlanningRequest.model_validate_json(input_path.read_text(encoding="utf-8"))
        plan = _generate(request, template_dir, fast=args.fast)
        _write_atomic_json(plan, output_path)
        print(json.dumps({
            "status": "ok",
            "output": str(output_path),
            "slides": len(plan.slides),
            "quality": plan.quality.status,
            "revisions": plan.quality.revision_count,
            "model": plan.model,
        }, ensure_ascii=False, indent=2))
        return 0
    except PlannerFailure as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except CatalogBuildError as exc:
        print(json.dumps(_error("catalog_build_failed", str(exc)), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except ValidationError as exc:
        print(json.dumps(_error(
            "invalid_input",
            "Input JSON does not match the planning request schema",
            validation_errors=exc.errors(include_url=False, include_input=False),
        ), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(json.dumps(_error("io_error", str(exc)), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
