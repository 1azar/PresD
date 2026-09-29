from __future__ import annotations

import json
import hashlib
import inspect
import os
import re
import tempfile
import time
import logging
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError, create_model, model_validator
from slide_types import SlideClass

from .catalog import TemplateCatalogBuilder, catalog_prompt_projection, select_chart_canvas
from .client import OpenAICompatibleClient, TextLLM, _error_category
from .compiler import CompilationError, compile_slide
from .models import (
    CATALOG_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    PIPELINE_VERSION,
    BulletListContent,
    CardItem,
    CardsContent,
    ContentAnalysis,
    DatasetAsset,
    DeckOutline,
    ImageSlotContent,
    MetricItem,
    MetricsContent,
    NarrativePlanCandidate,
    NarrativeSlotAssignment,
    NarrativeSlide,
    PlanCandidate,
    PlanCritique,
    CritiqueIssue,
    CritiqueScores,
    PlanningRequest,
    ProcessContent,
    ProcessItem,
    ProfilesContent,
    ProfileItem,
    PresentationPlan,
    QualityReport,
    SemanticDeckOutline,
    SemanticSlideOutline,
    SlotAssignment,
    SlotAction,
    SlotKind,
    SlidePlan,
    SlideDraft,
    SlideOutline,
    SourceChunk,
    StrictModel,
    TemplateCatalog,
    TemplateReference,
    TextSlotContent,
    TimelineContent,
    TimelineItem,
    VisualHint,
    VisualSlotContent,
)
from .structured_assets import (
    StructuredAssetError,
    choose_visual,
    default_visual_columns,
    paginate_asset,
)
from .validator import ValidationIssue, validate_analysis, validate_candidate, validate_candidate_issues


ModelT = TypeVar("ModelT", bound=BaseModel)
logger = logging.getLogger("content_planner.pipeline")


class PlannerFailure(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        issues = list(self.details.get("validation_issues", []))
        if not issues:
            for error in self.details.get("validation_errors", []):
                path, separator, issue_message = str(error).partition(": ")
                issues.append({
                    "code": "validation_error",
                    "path": path if separator else "",
                    "message": issue_message if separator else str(error),
                })
            if issues:
                self.details["validation_issues"] = issues
        self.validation_issues = issues

    def as_dict(self) -> dict[str, Any]:
        return {"status": "error", "code": self.code, "message": self.message, **self.details}


class MaterializedDatasets(StrictModel):
    datasets: list[DatasetAsset]


def _clean_json(value: str) -> str:
    value = value.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            value = "\n".join(lines[1:-1])
    return value.strip()


def _normalize_assignment_actions(value: Any) -> Any:
    """Remove payloads that structured-output models attach to no-op actions."""
    if isinstance(value, dict):
        normalized = {
            key: _normalize_assignment_actions(child) for key, child in value.items()
        }
        if normalized.get("action") in {"clear", "keep"}:
            normalized["content"] = None
            normalized["source_refs"] = []
        return normalized
    if isinstance(value, list):
        return [_normalize_assignment_actions(child) for child in value]
    return value


def _validation_errors(exc: ValidationError) -> list[str]:
    return [
        f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
        for item in exc.errors(include_url=False)
    ]


def _validation_codes(exc: ValidationError) -> list[str]:
    return list(dict.fromkeys(str(item.get("type") or "validation_error") for item in exc.errors()))


def _safe_error_summary(errors: list[Any], *, limit: int = 240) -> str:
    """Return a bounded, single-line schema error summary for operational logs."""
    summary = "; ".join(" ".join(str(error).split()) for error in errors[:3])
    if len(errors) > 3:
        summary += f"; +{len(errors) - 3} more"
    return summary[:limit] or "unknown schema validation error"


def build_sources(request: PlanningRequest) -> list[SourceChunk]:
    sources = [SourceChunk(id="brief", text=request.brief)]
    lines = [line.strip() for line in request.content_package.replace("\r\n", "\n").split("\n")]
    chunks: list[str] = []
    current: list[str] = []
    for line in lines:
        if not line:
            if current:
                chunks.append(" ".join(current))
                current = []
            continue
        if re.match(r"^(?:[-*•]|\d+[.)])\s+", line):
            if current:
                chunks.append(" ".join(current))
                current = []
            chunks.append(line)
        else:
            current.append(line)
    if current:
        chunks.append(" ".join(current))
    sources.extend(
        SourceChunk(id=f"content_{index:03d}", text=text)
        for index, text in enumerate(chunks, 1)
    )
    for asset in request.structured_assets:
        sources.append(SourceChunk(
            id=f"asset:{asset.id}",
            text=json.dumps(asset.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")),
        ))
    for image in request.provided_images:
        sources.append(SourceChunk(
            id=image.id,
            text=(
                f"Available image: {image.asset_ref} "
                f"(original filename: {image.original_name})"
            ),
        ))
    return sources


class ContentPlanner:
    def __init__(
        self,
        template_dir: str | Path | None = None,
        *,
        llm: TextLLM | None = None,
        catalog: TemplateCatalog | None = None,
        prompt_dir: str | Path | None = None,
        catalog_builder: TemplateCatalogBuilder | None = None,
        max_revisions: int = 2,
        enable_critique: bool = True,
        planning_budget: float = 300.0,
        outline_wave_budget: float = 105.0,
        slide_wave_budget: float = 105.0,
        critique_wave_budget: float = 75.0,
        checkpoint_dir: str | Path | None = None,
        job_id: str | None = None,
        progress: Callable[[str], None] | None = None,
        single_critique: bool = False,
        allow_model_assets: bool = True,
        max_workers: int | None = None,
        semantic_pipeline: bool | None = None,
    ) -> None:
        self.llm = llm or OpenAICompatibleClient()
        self.catalog = catalog
        self.template_dir = Path(template_dir).resolve() if template_dir else None
        prompts = Path(prompt_dir) if prompt_dir else Path(__file__).parent / "prompts"
        self.analysis_prompt = (prompts / "content_analysis_v1.txt").read_text(encoding="utf-8")
        self.draft_prompt = (prompts / "plan_draft_v1.txt").read_text(encoding="utf-8")
        self.outline_prompt = (prompts / "deck_outline_v1.txt").read_text(encoding="utf-8")
        self.slide_prompt = (prompts / "slide_draft_v1.txt").read_text(encoding="utf-8")
        self.critique_prompt = (prompts / "plan_critique_v1.txt").read_text(encoding="utf-8")
        self.revision_prompt = (prompts / "plan_revision_v1.txt").read_text(encoding="utf-8")
        self.repair_prompt = (prompts / "json_repair_v1.txt").read_text(encoding="utf-8")
        self.catalog_builder = catalog_builder
        if not 0 <= max_revisions <= 2:
            raise ValueError("max_revisions must be between 0 and 2")
        self.max_revisions = max_revisions
        self.enable_critique = enable_critique
        self.prompt_dir = prompts
        self.planning_budget = planning_budget
        self.outline_wave_budget = outline_wave_budget
        self.slide_wave_budget = slide_wave_budget
        self.critique_wave_budget = critique_wave_budget
        self.checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir else None
        self.job_id = job_id
        self.progress = progress
        self.single_critique = single_critique
        self.allow_model_assets = allow_model_assets
        self.semantic_pipeline = semantic_pipeline
        configured_workers = max_workers if max_workers is not None else int(
            os.getenv("WEBAPP_PLANNER_MAX_WORKERS", "4")
        )
        if not 1 <= configured_workers <= 8:
            raise ValueError("max_workers must be between 1 and 8")
        self.max_workers = configured_workers
        self._deadline: float | None = None
        self._task_state = threading.local()
        self._checkpoint_key = ""
        self._runtime_warnings: list[str] = []
        self._text_fallback_assets: set[str] = set()

    def generate(self, request: PlanningRequest) -> PresentationPlan:
        self._runtime_warnings = []
        self._text_fallback_assets = set()
        # Production structured-output clients use one semantic pipeline in
        # both modes. Fast mode differs only by its critique/revision budget.
        use_semantic = self.enable_critique if self.semantic_pipeline is None else self.semantic_pipeline
        if use_semantic and self._supports_native_schema():
            return self._generate_strict(request)
        return self._generate_reliable(request)

    def _generate_reliable(self, request: PlanningRequest) -> PresentationPlan:
        catalog = self._catalog()
        self._deadline = time.monotonic() + self.planning_budget
        self._checkpoint_key = self._input_hash(request, catalog)
        self._materialize_missing_datasets(request)
        restored_plan = self._load_checkpoint("validated_plan", PresentationPlan)
        if restored_plan is not None:
            return restored_plan
        asset_guidance = self._asset_guidance(request, catalog)
        sources = build_sources(request)
        analysis = self._load_checkpoint("analysis", ContentAnalysis)
        if analysis is None:
            self._notify("content_analysis")
            analysis = self._analyze(request, sources, asset_guidance)
            self._save_checkpoint("analysis", analysis)
        assets_by_id = {asset.id: asset for asset in request.structured_assets}
        for asset in analysis.visual_candidates:
            if not self.allow_model_assets:
                continue
            previous = assets_by_id.get(asset.id)
            # Request assets are authoritative.  An analysis model may still
            # echo one with altered metadata despite the prompt; never let
            # that overwrite or invalidate uploaded rows.
            if previous is not None:
                continue
            asset.provenance = "inferred"
            request.structured_assets.append(asset)
            assets_by_id[asset.id] = asset
            sources.append(SourceChunk(
                id=f"asset:{asset.id}",
                text=json.dumps(asset.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")),
            ))
        asset_guidance = self._asset_guidance(request, catalog)
        candidate = self._load_checkpoint("draft", PlanCandidate)
        if candidate is None:
            self._notify("drafting")
            candidate = self._draft(request, sources, analysis, catalog, asset_guidance)
            self._save_checkpoint("draft", candidate)
        candidate = self._normalize_candidate(request, catalog, candidate)
        structural_revisions = 0
        semantic_revisions = 0
        last_critique: PlanCritique | None = None

        while True:
            issues = validate_candidate_issues(request, sources, analysis, catalog, candidate)
            if issues:
                self._log_structural_issues(0, "reliable", issues)
                errors = [issue.render() for issue in issues]
                if structural_revisions >= self.max_revisions:
                    overflow = any(issue.code == "mandatory_fact_uncovered" for issue in issues)
                    raise PlannerFailure(
                        "content_overflow" if overflow else "quality_gate_failed",
                        (
                            "mandatory content does not fit the requested slide range"
                            if overflow else
                            "plan remains structurally invalid after the revision limit"
                        ),
                        {
                            "validation_errors": errors,
                            "validation_issues": [issue.as_dict() for issue in issues],
                            "revision_counts": {
                                "structural": structural_revisions,
                                "semantic": semantic_revisions,
                            },
                        },
                    )
                self._notify("repairing")
                candidate = self._revise(
                    request, sources, analysis, catalog, candidate,
                    [issue.as_prompt_dict() for issue in issues], asset_guidance,
                )
                candidate = self._normalize_candidate(request, catalog, candidate)
                structural_revisions += 1
                continue

            if not self.enable_critique:
                quality = QualityReport(
                    status="passed",
                    revision_count=structural_revisions + semantic_revisions,
                    warnings=["LLM semantic critique was skipped; deterministic checks passed"],
                )
                break

            if self.single_critique and semantic_revisions >= 1:
                quality = QualityReport(
                    status="needs_review",
                    revision_count=structural_revisions + semantic_revisions,
                    warnings=[
                        issue.message for issue in (last_critique.issues if last_critique else [])
                    ] or ["Plan was corrected once and requires manual review"],
                )
                break

            critique = self._critique(request, sources, analysis, catalog, candidate)
            last_critique = critique
            if critique.passes():
                quality = QualityReport(
                    status="passed",
                    revision_count=structural_revisions + semantic_revisions,
                    warnings=[],
                )
                break
            if semantic_revisions >= self.max_revisions:
                quality = QualityReport(
                    status="needs_review",
                    revision_count=structural_revisions + semantic_revisions,
                    warnings=[issue.message for issue in critique.issues],
                )
                break
            issues = [issue.model_dump(mode="json") for issue in critique.issues]
            if not issues:
                issues = [{
                    "severity": "warning",
                    "message": "Raise every quality score to the required threshold.",
                    "scores": critique.scores.model_dump(mode="json"),
                }]
            self._notify("repairing")
            candidate = self._revise(request, sources, analysis, catalog, candidate, issues, asset_guidance)
            candidate = self._normalize_candidate(request, catalog, candidate)
            semantic_revisions += 1

        final_issues = validate_candidate_issues(request, sources, analysis, catalog, candidate)
        if final_issues:
            self._log_structural_issues(0, "final", final_issues)
            raise PlannerFailure(
                "quality_gate_failed",
                "final plan failed deterministic validation",
                {
                    "validation_errors": [issue.render() for issue in final_issues],
                    "validation_issues": [issue.as_dict() for issue in final_issues],
                },
            )
        if quality.status == "needs_review" and last_critique and not quality.warnings:
            quality.warnings.append(
                "Quality thresholds were not reached after the semantic revision limit"
            )
        quality.warnings = list(dict.fromkeys([*self._runtime_warnings, *quality.warnings]))
        families = {family.family_id: family for family in catalog.families}
        for slide in candidate.slides:
            family = families[slide.template_family_id]
            variant = next(
                item for item in family.variants
                if item.slide_number == slide.template_slide_number
            )
            try:
                slide.render_operations = compile_slide(slide, variant, request.structured_assets)
            except CompilationError as exc:
                raise PlannerFailure(
                    "render_compilation_failed",
                    f"slide {slide.number}: {exc}",
                ) from exc
        result = PresentationPlan(
            schema_version=PLAN_SCHEMA_VERSION,
            model=self.llm.model,
            template=TemplateReference(
                source_sha256=catalog.source_sha256,
                catalog_version=CATALOG_SCHEMA_VERSION,
            ),
            deck=candidate.deck,
            sources=sources,
            structured_assets=request.structured_assets,
            slides=candidate.slides,
            quality=quality,
        )
        self._save_checkpoint("validated_plan", result)
        return result

    def _generate_strict(self, request: PlanningRequest) -> PresentationPlan:
        """Run the semantic-only, fail-closed planning pipeline."""
        catalog = self._catalog()
        self._deadline = time.monotonic() + self.planning_budget
        self._checkpoint_key = self._input_hash(request, catalog)
        self._materialize_missing_datasets(request)
        restored_plan = self._load_checkpoint("validated_plan", PresentationPlan)
        if restored_plan is not None and restored_plan.quality.result_kind == "primary":
            return restored_plan

        asset_guidance = self._asset_guidance(request, catalog)
        sources = build_sources(request)
        semantic = self._load_checkpoint("semantic_outline", SemanticDeckOutline)
        if semantic is None:
            self._notify("planning_outline")
            results = self._run_wave("outline", {
                "outline": lambda: self._generate_semantic_outline(request, sources),
            })
            value = results.get("outline")
            if not isinstance(value, SemanticDeckOutline):
                failure = value if isinstance(value, PlannerFailure) else None
                if failure:
                    raise failure
                raise PlannerFailure(
                    "outline_generation_failed", "failed to generate a valid deck outline",
                )
            semantic = value
            self._save_checkpoint("semantic_outline", semantic)

        outline = self._resolve_outline(request, sources, catalog, semantic, asset_guidance)
        analysis = self._analysis_from_semantic_outline(request, sources, semantic)

        drafts = self._generate_slides_parallel(
            request, sources, analysis, catalog, outline, asset_guidance,
        )
        candidate = self._assemble_outline(outline, drafts, catalog)
        try:
            candidate = self._normalize_candidate(
                request, catalog, candidate, ground_assets=False,
            )
        except PlannerFailure as exc:
            raise PlannerFailure(
                "slide_generation_failed", "generated slides could not be assembled",
                exc.details,
            ) from exc
        structural = validate_candidate_issues(request, sources, analysis, catalog, candidate)
        if structural:
            blocking_codes = {
                "unknown_template_family", "unknown_template_variant",
                "duplicate_slot_assignment", "missing_slot_assignment",
                "unknown_slot_assignment", "slot_kind_mismatch",
                "target_shape_ids_mismatch", "required_slot_cleared",
                "invalid_slot_action", "unknown_structured_asset",
                "invalid_visual_hint", "visualization_disabled",
                "unsupported_visual_kind", "unsupported_visual_subtype",
                "unknown_chart_asset", "chart_asset_not_dataset",
                "chart_asset_not_chart", "chart_canvas_mismatch",
                "chart_canvas_missing", "chart_asset_id_missing",
            }
            blocking = [item for item in structural if item.code in blocking_codes]
            if blocking:
                raise PlannerFailure(
                    "slide_generation_failed",
                    "generated slides contain non-renderable assignments",
                    {"validation_issues": [item.as_dict() for item in blocking]},
                )
            self._runtime_warnings.extend(
                f"Слайд сохранён с замечанием: {item.message}" for item in structural
            )

        if self.enable_critique:
            self._notify("evaluating_quality")
            critique = self._evaluate_quality_once(
                request, sources, analysis, catalog, candidate,
            )
            quality = QualityReport(
                status="passed" if critique.passes() else "needs_review",
                revision_count=0,
                warnings=([] if critique.passes() else self._critique_warnings(critique)),
                result_kind="primary",
            )
        else:
            quality = QualityReport(
                status="passed", revision_count=0, result_kind="primary",
                warnings=["LLM semantic critique was skipped; deterministic checks passed"],
            )
        quality.warnings = list(dict.fromkeys([*self._runtime_warnings, *quality.warnings]))
        return self._build_result(request, catalog, sources, candidate, quality)

    @staticmethod
    def _safe_fallback_reason(exc: PlannerFailure) -> str:
        reasons = {
            "planning_budget_exhausted": "исчерпан бюджет времени планирования",
            "llm_api_error": "модель временно недоступна",
            "invalid_llm_response": "модель вернула некорректную структуру",
        }
        return reasons.get(exc.code, "качественный вариант не был получен")

    @staticmethod
    def _explicit_asset_references(request: PlanningRequest) -> tuple[list[str], list[str]]:
        text = f"{request.brief}\n{request.content_package}"
        dataset_patterns = (
            r"(?:из\s+)?(?:набор(?:а|е)?\s+данных|таблиц[ауы])\s+(?:id\s*)?[:=]?\s*[`\"']?([A-Za-z0-9][A-Za-z0-9_.-]*)[`\"']?",
            r"dataset(?:\s+id)?\s*[:=]?\s*[`\"']?([A-Za-z0-9][A-Za-z0-9_.-]*)[`\"']?",
        )
        referenced_datasets = {
            match.group(1)
            for pattern in dataset_patterns
            for match in re.finditer(pattern, text, re.IGNORECASE)
        }
        known_datasets = {asset.id for asset in request.structured_assets}
        missing_datasets = sorted(referenced_datasets - known_datasets)

        filename_pattern = re.compile(
            r"[`\"']([^`\"'\r\n]+\.(?:png|jpe?g|webp|gif|bmp|tiff?))[`\"']|"
            r"(?<![\w.-])([\w@()+.-]+\.(?:png|jpe?g|webp|gif|bmp|tiff?))(?![\w.-])",
            re.IGNORECASE,
        )
        referenced_images = {
            (match.group(1) or match.group(2)).strip() for match in filename_pattern.finditer(text)
        }
        known_images = {
            value.casefold()
            for image in request.provided_images
            for value in (Path(image.original_name).name, Path(image.asset_ref).name)
        }
        missing_images = sorted(
            name for name in referenced_images if Path(name).name.casefold() not in known_images
        )
        return missing_datasets, missing_images

    @classmethod
    def _validate_explicit_assets(cls, request: PlanningRequest) -> None:
        """Legacy explicit validation hook; generation now materializes these inputs."""
        missing_datasets, missing_images = cls._explicit_asset_references(request)
        if not missing_datasets and not missing_images:
            return
        raise PlannerFailure(
            "missing_input_asset", "explicitly referenced input assets are missing",
            {
                "missing_asset_ids": missing_datasets,
                "missing_filenames": missing_images,
                "validation_issues": [
                    *({
                        "code": "missing_input_asset", "path": "structured_assets",
                        "message": f"Отсутствует набор данных {asset_id!r}.",
                    } for asset_id in missing_datasets),
                    *({
                        "code": "missing_input_asset", "path": "provided_images",
                        "message": f"Отсутствует изображение {name!r}.",
                    } for name in missing_images),
                ],
            },
        )

    @staticmethod
    def _requested_visual(text: str, asset_id: str) -> tuple[str, str | None]:
        line = next(
            (item for item in text.splitlines() if asset_id.casefold() in item.casefold()),
            "",
        )
        position = text.casefold().find(asset_id.casefold())
        window = (line or text[max(0, position - 120):position + len(asset_id) + 120]).casefold()
        subtype = next((item for item in (
            "donut", "pie", "line", "area", "column", "bar", "scatter",
        ) if item in window), None)
        if "кольцев" in window:
            subtype = "donut"
        elif "кругов" in window:
            subtype = "pie"
        elif "линейн" in window:
            subtype = "line"
        if any(token in window for token in ("table", "таблиц")):
            return "table", None
        if any(token in window for token in ("trend", "динамик", "тренд")) and subtype is None:
            subtype = "line"
        return "chart", subtype or "column"

    def _materialize_missing_datasets(self, request: PlanningRequest) -> None:
        """Create explicitly requested, absent datasets without replacing supplied data."""
        missing, _ = self._explicit_asset_references(request)
        if not missing:
            return
        cached = self._load_checkpoint("materialized_assets", MaterializedDatasets)
        if cached is not None and {item.id for item in cached.datasets} == set(missing):
            datasets = cached.datasets
        else:
            self._notify("materializing_assets")
            text = f"{request.brief}\n{request.content_package}"
            base_sources = build_sources(request)
            specs = []
            for asset_id in missing:
                kind, subtype = self._requested_visual(text, asset_id)
                refs = [source.id for source in base_sources if asset_id.casefold() in source.text.casefold()]
                specs.append({
                    "id": asset_id, "visual_kind": kind, "subtype": subtype,
                    "source_refs": refs or ["brief"],
                })
            prompt = (
                "Create only the missing datasets listed below. Keep every explicit fact and number "
                "from the supplied text unchanged; fill absent values with plausible values. IDs must "
                "match exactly. Set provenance to inferred and source_refs to the supplied source IDs. "
                "For pie/donut use non-negative shares summing to 100; for a trend use ordered categories; "
                "for a table derive rows from the corresponding text section. Return JSON only.\n\n"
                f"MISSING DATASETS:\n{json.dumps(specs, ensure_ascii=False)}\n\n"
                f"SOURCES:\n{json.dumps([item.model_dump(mode='json') for item in base_sources], ensure_ascii=False)}"
            )
            datasets = []
            last: PlannerFailure | None = None
            for attempt in range(2):
                try:
                    value = self._complete_model(
                        prompt + ("\nRETRY: correct all constraints." if attempt else ""),
                        MaterializedDatasets, "asset materialization",
                    )
                    if {item.id for item in value.datasets} != set(missing):
                        raise PlannerFailure("invalid_llm_response", "materialized dataset IDs do not match")
                    datasets = value.datasets
                    self._validate_materialized_datasets(datasets, specs)
                    break
                except PlannerFailure as exc:
                    datasets = []
                    last = exc
                    if exc.code != "invalid_llm_response":
                        raise
            if not datasets:
                # A malformed dataset response is local to these assets. Keep the
                # deck alive with a small, deterministic and explicitly inferred set.
                datasets = [self._deterministic_dataset(spec) for spec in specs]
                if last:
                    logger.warning("dataset_materialization_repaired job=%s error=%s", self.job_id or "-", last.code)
            value = MaterializedDatasets(datasets=datasets)
            self._save_checkpoint("materialized_assets", value)
        known = {asset.id for asset in request.structured_assets}
        for asset in datasets:
            if asset.id not in known:
                request.structured_assets.append(asset)
        self._runtime_warnings.append(
            "Синтезированы недостающие наборы данных: " + ", ".join(sorted(missing))
        )

    @staticmethod
    def _validate_materialized_datasets(
        datasets: list[DatasetAsset], specs: list[dict[str, Any]],
    ) -> None:
        spec_by_id = {item["id"]: item for item in specs}
        for dataset in datasets:
            spec = spec_by_id.get(dataset.id)
            if spec is None or dataset.provenance != "inferred":
                raise PlannerFailure("invalid_llm_response", "invalid inferred dataset provenance")
            if not dataset.source_refs:
                raise PlannerFailure("invalid_llm_response", "inferred dataset has no source refs")
            if spec["visual_kind"] == "table":
                dataset.visual_hint = VisualHint(mode="force", kind="table")
            else:
                dataset.visual_hint = VisualHint(
                    mode="force", kind="chart", subtype=spec["subtype"],
                )
            if spec["subtype"] in {"pie", "donut"}:
                number_keys = [column.key for column in dataset.columns if column.type == "number"]
                if not number_keys:
                    raise PlannerFailure("invalid_llm_response", "share dataset has no numeric column")
                values = [float(row[number_keys[0]]) for row in dataset.rows if row[number_keys[0]] is not None]
                if any(value < 0 for value in values) or abs(sum(values) - 100) > 0.01:
                    raise PlannerFailure("invalid_llm_response", "pie/donut shares must sum to 100")

    @staticmethod
    def _deterministic_dataset(spec: dict[str, Any]) -> DatasetAsset:
        subtype = spec.get("subtype")
        if subtype in {"pie", "donut"}:
            rows = [
                {"category": "Основное", "value": 55},
                {"category": "Дополнительное", "value": 30},
                {"category": "Прочее", "value": 15},
            ]
        else:
            rows = [
                {"category": "Период 1", "value": 35},
                {"category": "Период 2", "value": 50},
                {"category": "Период 3", "value": 65},
            ]
        return DatasetAsset.model_validate({
            "id": spec["id"], "title": spec["id"].replace("_", " ").title(),
            "columns": [
                {"key": "category", "label": "Категория", "type": "text"},
                {"key": "value", "label": "Значение", "type": "number", "unit": "%" if subtype in {"pie", "donut"} else None},
            ],
            "rows": rows, "provenance": "inferred", "source_refs": spec["source_refs"],
            "visual_hint": {"mode": "force", "kind": spec["visual_kind"], "subtype": subtype} if spec["visual_kind"] == "chart" else {"mode": "force", "kind": "table"},
        })

    def _generate_semantic_outline(
        self, request: PlanningRequest, sources: list[SourceChunk],
    ) -> SemanticDeckOutline:
        schema = self._dynamic_schema(
            SemanticDeckOutline, sources=[source.id for source in sources],
        )
        slides_schema = schema.get("properties", {}).get("slides", {})
        slides_schema["minItems"] = request.slide_count.min
        slides_schema["maxItems"] = request.slide_count.max
        prompt = self._request(self.outline_prompt, SemanticDeckOutline, {
            "REQUEST": {
                "brief": request.brief,
                "slide_count": request.slide_count.model_dump(mode="json"),
            },
            "SOURCES": [source.model_dump(mode="json") for source in sources],
            "INSTRUCTION": (
                "Return only deck metadata and semantic slide items: number, purpose, "
                "content_type and source_refs. Do not return template/family/variant IDs, "
                "slot or shape metadata, actions, asset paths, or renderer data."
            ),
        })
        last: PlannerFailure | None = None
        for attempt in range(2):
            try:
                value = self._complete_model(
                    prompt + ("\n\nRETRY: fix the semantic outline and return JSON only." if attempt else ""),
                    SemanticDeckOutline, "deck outline", schema=schema,
                )
                self._validate_semantic_outline(request, sources, value)
                return value
            except PlannerFailure as exc:
                last = exc
                if exc.code != "invalid_llm_response" or attempt:
                    raise
        assert last is not None
        raise last

    @staticmethod
    def _validate_semantic_outline(
        request: PlanningRequest, sources: list[SourceChunk], outline: SemanticDeckOutline,
    ) -> None:
        errors: list[str] = []
        count = len(outline.slides)
        if not request.slide_count.min <= count <= request.slide_count.max:
            errors.append(f"slide count {count} is outside the requested range")
        if [slide.number for slide in outline.slides] != list(range(1, count + 1)):
            errors.append("slide numbers must be consecutive")
        known = {source.id for source in sources}
        for slide in outline.slides:
            unknown = sorted(set(slide.source_refs) - known)
            if unknown:
                errors.append(f"slide {slide.number} has unknown source_refs {unknown}")
        if errors:
            raise PlannerFailure(
                "invalid_llm_response", "semantic outline violates planning constraints",
                {"validation_errors": errors},
            )

    @staticmethod
    def _required_image_ids(request: PlanningRequest) -> list[str]:
        mentioned = f"{request.brief}\n{request.content_package}".casefold()
        return [
            image.id for image in request.provided_images
            if Path(image.original_name).name.casefold() in mentioned
            or Path(image.asset_ref).name.casefold() in mentioned
        ]

    def _resolve_outline(
        self, request: PlanningRequest, sources: list[SourceChunk], catalog: TemplateCatalog,
        semantic: SemanticDeckOutline, asset_guidance: list[dict[str, Any]],
    ) -> DeckOutline:
        """Bind semantic slides to materials and catalog variants deterministically."""
        resolved = semantic.model_copy(deep=True)
        source_map = {source.id: source.text for source in sources}
        semantic_refs = {
            slide.number: list(slide.source_refs) for slide in resolved.slides
        }
        slide_assets: dict[int, list[str]] = {item.number: [] for item in resolved.slides}
        slide_images: dict[int, list[str]] = {item.number: [] for item in resolved.slides}
        guidance = {item["asset_id"]: item for item in asset_guidance}

        # Markdown headings are split into their own source chunks.  When the
        # semantic outline selects a heading, keep its immediately following
        # body with the same slide so structured text slots (cards/profiles)
        # receive the facts, not only the section label.
        ordered_content = [
            source.id for source in sources if re.fullmatch(r"content_\d+", source.id)
        ]
        next_content = dict(zip(ordered_content, ordered_content[1:]))
        for slide in resolved.slides:
            expanded = list(slide.source_refs)
            for ref in slide.source_refs:
                following = next_content.get(ref)
                if (
                    following is not None
                    and source_map.get(ref, "").lstrip().startswith("#")
                    and not source_map.get(following, "").lstrip().startswith("#")
                    and following not in expanded
                ):
                    expanded.append(following)
            slide.source_refs = expanded

        # Every textual source is grounded even when the model omitted it.  This
        # registry step is stable and prevents silent loss of supplied material.
        claimed_refs = {ref for slide in resolved.slides for ref in slide.source_refs}
        registry_refs = [
            source.id for source in sources
            if not source.id.startswith("asset:")
            and source.id not in {image.id for image in request.provided_images}
        ]
        for index, ref in enumerate(item for item in registry_refs if item not in claimed_refs):
            resolved.slides[index % len(resolved.slides)].source_refs.append(ref)

        def slide_score(slide: SemanticSlideOutline, needle: str, preferred: set[str]) -> tuple[Any, ...]:
            selected = "\n".join(
                source_map.get(ref, "") for ref in semantic_refs[slide.number]
            ).casefold()
            words = {word for word in re.findall(r"[\w-]{3,}", needle.casefold())}
            overlap = sum(word in selected or word in slide.purpose.casefold() for word in words)
            return (0 if slide.content_type in preferred else 1, -overlap, len(slide_assets[slide.number]), slide.number)

        required_assets = [
            asset for asset in request.structured_assets
            if asset.visual_hint.mode != "none" and asset.id not in self._text_fallback_assets
        ]
        if len(required_assets) > len(resolved.slides):
            raise PlannerFailure("content_overflow", "structured assets require more slides than available")
        for asset in required_assets:
            item = guidance.get(asset.id)
            if not item or not item.get("compatible_variants"):
                raise PlannerFailure(
                    "incompatible_template", f"no compatible template for asset {asset.id!r}",
                )
            visual_kind = item["compatible_variants"][0]["visual_kind"]
            eligible = [
                slide for slide in resolved.slides if not slide_assets[slide.number]
            ]
            explicit = [
                slide for slide in eligible
                if f"asset:{asset.id}" in semantic_refs[slide.number]
            ]
            target = min(
                explicit or eligible,
                key=lambda slide: slide_score(slide, asset.title, {visual_kind, "table", "chart", "diagram", "pictogram_grid"}),
            )
            slide_assets[target.number].append(asset.id)
            ref = f"asset:{asset.id}"
            if ref not in target.source_refs:
                target.source_refs.append(ref)

        technical_types = {"chart", "table", "diagram", "pictogram_grid"}
        missing_material_slides = [
            slide for slide in resolved.slides
            if slide.content_type in technical_types and not slide_assets[slide.number]
        ]
        if missing_material_slides:
            for slide in missing_material_slides:
                slide.content_type = "text"
            self._runtime_warnings.append(
                "Для несовместимых визуальных слайдов использован текстовый макет."
            )

        image_by_id = {image.id: image for image in request.provided_images}
        material_text = f"{request.brief}\n{request.content_package}".casefold()
        for image_id in self._required_image_ids(request):
            image = image_by_id[image_id]
            filename_refs = [
                ref for ref, text in source_map.items()
                if image.original_name.casefold() in text.casefold()
                or image.asset_ref.casefold() in text.casefold()
            ]

            def content_index(ref: str) -> int | None:
                match = re.fullmatch(r"content_(\d+)", ref)
                return int(match.group(1)) if match else None

            filename_indexes = [
                value for ref in filename_refs if (value := content_index(ref)) is not None
            ]
            filename = image.original_name.casefold()
            occurrences = [match.start() for match in re.finditer(re.escape(filename), material_text)]

            def stems(value: str) -> set[str]:
                return {
                    token[:3] for token in re.findall(r"[a-zа-яё]{3,}", value.casefold())
                    if token[:3] not in {"про", "пор", "wit", "ima"}
                }
            def image_score(slide: SemanticSlideOutline) -> tuple[Any, ...]:
                refs = semantic_refs[slide.number]
                indexes = [
                    value for ref in refs if (value := content_index(ref)) is not None
                ]
                distance = min(
                    (abs(left - right) for left in indexes for right in filename_indexes),
                    default=10_000,
                )
                exact = 0 if set(refs) & set(filename_refs) else 1
                identity = stems(slide.purpose)
                identity_distance = sum(
                    min(
                        (abs(match.start() - position)
                         for match in re.finditer(rf"\b{re.escape(stem)}[a-zа-яё]*", material_text)
                         for position in occurrences),
                        default=100_000,
                    )
                    for stem in identity
                )
                return (
                    exact, identity_distance, distance,
                    0 if slide.content_type in {"image", "profiles"} else 1,
                    len(slide_images[slide.number]), slide.number,
                )

            eligible = [
                slide for slide in resolved.slides
                if slide.content_type in {"image", "profiles"}
                and not slide_images[slide.number]
            ]
            if not eligible:
                eligible = [
                    slide for slide in resolved.slides if not slide_images[slide.number]
                ] or list(resolved.slides)
            explicit = [
                slide for slide in eligible
                if image_id in semantic_refs[slide.number]
            ]
            if explicit:
                eligible = explicit
            target = min(eligible, key=image_score)
            slide_images[target.number].append(image_id)
            if image_id not in target.source_refs:
                target.source_refs.append(image_id)

        class_by_type = {
            "cover": SlideClass.COVER, "text": SlideClass.CONTENT_TEXT,
            "bullets": SlideClass.CONTENT_TEXT, "cards": SlideClass.CONTENT_TEXT,
            "profiles": SlideClass.PROFILE, "metrics": SlideClass.METRICS,
            "table": SlideClass.TABLE, "chart": SlideClass.CHART,
            "diagram": SlideClass.CONTENT_VISUAL, "pictogram_grid": SlideClass.CONTENT_VISUAL,
            "process": SlideClass.PROCESS, "timeline": SlideClass.TIMELINE,
            "image": SlideClass.CONTENT_VISUAL, "closing": SlideClass.CLOSING,
        }
        preferred_slot = {
            "bullets": SlotKind.BULLET_LIST, "cards": SlotKind.CARDS,
            "profiles": SlotKind.PROFILES, "metrics": SlotKind.METRICS,
            "process": SlotKind.PROCESS, "timeline": SlotKind.TIMELINE,
        }
        families = {family.family_id: family for family in catalog.families}
        outline_slides: list[SlideOutline] = []
        for slide in resolved.slides:
            assets = slide_assets[slide.number]
            images = slide_images[slide.number]
            candidates: list[tuple[Any, Any, dict[str, Any] | None]] = []
            if assets:
                allowed = guidance[assets[0]]["compatible_variants"]
                for choice in allowed:
                    family = families[choice["family_id"]]
                    variant = next(v for v in family.variants if v.slide_number == choice["slide_number"])
                    if sum(slot.kind == SlotKind.IMAGE for slot in variant.slots) >= len(images):
                        candidates.append((family, variant, choice))
            else:
                for family in catalog.families:
                    for variant in family.variants:
                        if not variant.slots:
                            continue
                        if any(
                            slot.required and slot.kind in {
                                SlotKind.VISUAL, SlotKind.TABLE, SlotKind.CHART,
                            }
                            for slot in variant.slots
                        ):
                            continue
                        if sum(slot.kind == SlotKind.IMAGE for slot in variant.slots) < len(images):
                            continue
                        candidates.append((family, variant, None))
            if not candidates:
                code = "content_overflow" if images else "incompatible_template"
                raise PlannerFailure(code, f"no template can place slide {slide.number} materials")
            desired_class = class_by_type[slide.content_type]
            desired_slot = preferred_slot.get(slide.content_type, SlotKind.TEXT)
            family, variant, choice = min(candidates, key=lambda item: (
                0 if item[0].slide_class == desired_class else 1,
                item[2].get("expected_pages", 1) if item[2] is not None else 1,
                0 if any(slot.kind == desired_slot for slot in item[1].slots) else 1,
                abs(sum(slot.kind == SlotKind.IMAGE for slot in item[1].slots) - len(images)),
                sum(slot.required for slot in item[1].slots),
                item[1].slide_number,
            ))
            chart_id = assets[0] if choice and choice["visual_kind"] == "chart" else None
            outline_slides.append(SlideOutline(
                number=slide.number, purpose=slide.purpose,
                template_family_id=family.family_id,
                template_slide_number=variant.slide_number,
                source_refs=list(dict.fromkeys(slide.source_refs)),
                asset_ids=assets, image_ids=images, chart_asset_id=chart_id,
            ))
        return DeckOutline(deck=resolved.deck, slides=outline_slides)

    @staticmethod
    def _analysis_from_semantic_outline(
        request: PlanningRequest, sources: list[SourceChunk], outline: SemanticDeckOutline,
    ) -> ContentAnalysis:
        image_ids = {image.id for image in request.provided_images}
        facts = [
            {
                "id": f"source_{index}", "text": source.text,
                "source_refs": [source.id], "mandatory": True,
            }
            for index, source in enumerate(sources, 1)
            if source.id not in image_ids
        ]
        return ContentAnalysis.model_validate({
            "goal": request.brief, "audience": "presentation audience",
            "language": outline.deck.language,
            "narrative": [slide.purpose for slide in outline.slides],
            "recommended_slide_count": len(outline.slides), "facts": facts,
        })

    def _evaluate_quality_once(
        self, request: PlanningRequest, sources: list[SourceChunk], analysis: ContentAnalysis,
        catalog: TemplateCatalog, candidate: PlanCandidate,
    ) -> PlanCritique:
        cached = self._load_checkpoint("quality_critique", PlanCritique)
        if cached is not None:
            return cached
        last: PlannerFailure | None = None
        for attempt in range(2):
            try:
                value = self._critique(
                    request, sources, analysis, catalog, candidate,
                    instruction="Evaluate every quality dimension in one compact response.",
                )
                self._save_checkpoint("quality_critique", value)
                return value
            except PlannerFailure as exc:
                last = exc
                if exc.code != "invalid_llm_response":
                    raise
        raise PlannerFailure(
            "quality_evaluation_failed", "critic returned invalid structured responses twice",
            last.details if last else None,
        )

    def _generate_outline(
        self, request: PlanningRequest, sources: list[SourceChunk],
        analysis: ContentAnalysis, catalog: TemplateCatalog,
        asset_guidance: list[dict[str, Any]],
    ) -> DeckOutline:
        schema = self._dynamic_schema(
            DeckOutline,
            sources=[source.id for source in sources],
            assets=[asset.id for asset in request.structured_assets],
            **self._catalog_selection_enums(catalog),
        )
        slides_schema = schema.get("properties", {}).get("slides", {})
        slides_schema["minItems"] = request.slide_count.min
        slides_schema["maxItems"] = request.slide_count.max
        base = self._request(self.outline_prompt, DeckOutline, {
            "REQUEST": request.model_dump(mode="json"),
            "CONTENT ANALYSIS": analysis.model_dump(mode="json"),
            "TEMPLATE CATALOG": catalog_prompt_projection(catalog),
            "STRUCTURED ASSET VISUAL GUIDANCE": asset_guidance,
            "AVAILABLE SOURCE IDS": [source.id for source in sources],
            "AVAILABLE IMAGES": [image.model_dump(mode="json") for image in request.provided_images],
        })
        last: PlannerFailure | None = None
        for attempt in range(2):
            try:
                outline = self._complete_model(
                    base + ("\n\nRETRY: correct the outline and return JSON only." if attempt else ""),
                    DeckOutline, "deck outline", schema=schema,
                )
                self._validate_outline(request, sources, catalog, outline)
                return outline
            except PlannerFailure as exc:
                last = exc
                if attempt or exc.code == "planning_budget_exhausted":
                    raise
        assert last is not None
        raise last

    @staticmethod
    def _validate_outline(
        request: PlanningRequest, sources: list[SourceChunk], catalog: TemplateCatalog,
        outline: DeckOutline,
    ) -> None:
        errors: list[str] = []
        count = len(outline.slides)
        if not request.slide_count.min <= count <= request.slide_count.max:
            errors.append(f"slide count {count} is outside the form range")
        if [slide.number for slide in outline.slides] != list(range(1, count + 1)):
            errors.append("slide numbers must be consecutive")
        source_ids = {source.id for source in sources}
        asset_ids = {asset.id for asset in request.structured_assets}
        required_asset_ids = {
            asset.id for asset in request.structured_assets if asset.visual_hint.mode != "none"
        }
        image_ids = {image.id for image in request.provided_images}
        variants = {
            (family.family_id, variant.slide_number)
            for family in catalog.families for variant in family.variants
        }
        for slide in outline.slides:
            if (slide.template_family_id, slide.template_slide_number) not in variants:
                errors.append(f"slide {slide.number} selects an unknown template variant")
            if not set(slide.source_refs) <= source_ids:
                errors.append(f"slide {slide.number} references an unknown source")
            if not set(slide.asset_ids) <= asset_ids:
                errors.append(f"slide {slide.number} references an unknown dataset")
            if not set(slide.image_ids) <= image_ids:
                errors.append(f"slide {slide.number} references an unknown image")
            if slide.chart_asset_id is not None and slide.chart_asset_id not in slide.asset_ids:
                errors.append(f"slide {slide.number} chart_asset_id is not assigned to the slide")
        assigned_assets = [asset for slide in outline.slides for asset in slide.asset_ids]
        if (
            set(assigned_assets) != required_asset_ids
            or len(assigned_assets) != len(set(assigned_assets))
        ):
            errors.append("every structured asset must be assigned exactly once")
        mentioned = f"{request.brief}\n{request.content_package}".casefold()
        required_images = {
            image.id for image in request.provided_images
            if image.original_name.casefold() in mentioned
        }
        assigned_images = {image for slide in outline.slides for image in slide.image_ids}
        if not required_images <= assigned_images:
            errors.append("an explicitly referenced image is missing from the outline")
        if errors:
            raise PlannerFailure(
                "invalid_llm_response", "deck outline violates planning constraints",
                {"validation_errors": errors},
            )

    def _generate_slide_draft(
        self, request: PlanningRequest, sources: list[SourceChunk],
        analysis: ContentAnalysis, catalog: TemplateCatalog,
        outline: DeckOutline, asset_guidance: list[dict[str, Any]], index: int,
    ) -> SlideDraft:
        slide = outline.slides[index]
        family = next(item for item in catalog.families if item.family_id == slide.template_family_id)
        variant = next(item for item in family.variants if item.slide_number == slide.template_slide_number)
        source_map = {source.id: source for source in sources}
        selected_sources = [source_map[source_id] for source_id in slide.source_refs]
        image_map = {image.id: image for image in request.provided_images}
        assets = {asset.id: asset for asset in request.structured_assets}
        guidance = {item["asset_id"]: item for item in asset_guidance}
        textual_kinds = {
            SlotKind.TEXT, SlotKind.BULLET_LIST, SlotKind.CARDS, SlotKind.PROFILES,
            SlotKind.METRICS, SlotKind.TIMELINE, SlotKind.PROCESS,
        }
        authored_slots = [slot for slot in variant.slots if slot.kind in textual_kinds]

        class TextDraftBase(StrictModel):
            @model_validator(mode="before")
            @classmethod
            def translate_previous_contract(cls, value: Any) -> Any:
                if not isinstance(value, dict) or "assignments" not in value:
                    return value
                translated: dict[str, Any] = {}
                for assignment in value.get("assignments") or []:
                    if not isinstance(assignment, dict) or not assignment.get("slot_id"):
                        continue
                    slot_id = assignment["slot_id"]
                    content = assignment.get("content")
                    if assignment.get("action") in {"clear", "keep"} or content is None:
                        translated[slot_id] = None
                    elif content.get("kind") == "text":
                        translated[slot_id] = content.get("text") or content.get("fields")
                    else:
                        translated[slot_id] = content.get("items")
                return translated

        value_types: dict[SlotKind, Any] = {
            SlotKind.TEXT: str | dict[str, str],
            SlotKind.BULLET_LIST: list[str],
            SlotKind.CARDS: list[CardItem],
            SlotKind.PROFILES: list[ProfileItem],
            SlotKind.METRICS: list[MetricItem],
            SlotKind.TIMELINE: list[TimelineItem],
            SlotKind.PROCESS: list[ProcessItem],
        }
        fields = {
            slot.slot_id: (value_types[slot.kind] | None, None)
            for slot in authored_slots
        }
        draft_model = create_model(
            f"Slide{slide.number}TextDraft", __base__=TextDraftBase, **fields,
        )
        schema = draft_model.model_json_schema()
        neighbors = outline.slides[max(0, index - 1):min(len(outline.slides), index + 2)]
        prompt = self._request(self.slide_prompt, draft_model, {
            "SLIDE OUTLINE": slide.model_dump(mode="json"),
            "NEIGHBORING OUTLINE ITEMS": [item.model_dump(mode="json") for item in neighbors],
            "SLIDE SOURCES": [source.model_dump(mode="json") for source in selected_sources],
            "TEXT SLOT CONTRACT": [{
                "name": slot.slot_id, "role": slot.role, "required": slot.required,
                "capacity": slot.capacity, "max_chars": slot.max_chars,
            } for slot in authored_slots],
            "CONTENT ANALYSIS SUMMARY": {
                "goal": analysis.goal, "audience": analysis.audience, "language": analysis.language,
            },
            "INSTRUCTION": (
                "Return one top-level property per TEXT SLOT CONTRACT item. Return null for an "
                "unused optional slot. Do not return number, assignments, source_refs, actions, "
                "template metadata, images, visuals, asset paths, or renderer fields."
            ),
        })
        authored = self._complete_model(prompt, draft_model, "slide draft", schema=schema)
        values = authored.model_dump()
        refs = [ref for ref in slide.source_refs if ref not in image_map]
        refs = refs or ["brief"]
        assignments: list[NarrativeSlotAssignment] = []
        errors: list[str] = []
        for slot in authored_slots:
            value = values.get(slot.slot_id)
            if value is None:
                is_title = bool(
                    set(re.findall(r"[a-zа-яё]+", f"{slot.slot_id} {slot.role}".casefold()))
                    & {"title", "heading", "header", "headline", "заголовок"}
                )
                if slot.required and is_title:
                    value = slide.purpose[:slot.max_chars] if slot.max_chars else slide.purpose
                elif slot.required:
                    errors.append(f"required slot {slot.slot_id!r} is missing")
                    continue
                else:
                    assignments.append(NarrativeSlotAssignment(
                        slot_id=slot.slot_id, action=SlotAction.CLEAR,
                    ))
                    continue
            if slot.kind == SlotKind.TEXT:
                content = TextSlotContent(
                    text=(
                        value[:slot.max_chars] if isinstance(value, str) and slot.max_chars
                        else value if isinstance(value, str) else None
                    ),
                    fields=value if isinstance(value, dict) else {},
                )
            elif slot.kind == SlotKind.BULLET_LIST:
                content = BulletListContent(items=value)
            elif slot.kind == SlotKind.CARDS:
                content = CardsContent(items=value)
            elif slot.kind == SlotKind.PROFILES:
                content = ProfilesContent(items=value)
            elif slot.kind == SlotKind.METRICS:
                content = MetricsContent(items=value)
            elif slot.kind == SlotKind.TIMELINE:
                content = TimelineContent(items=value)
            else:
                content = ProcessContent(items=value)
            assignments.append(NarrativeSlotAssignment(
                slot_id=slot.slot_id, action=SlotAction.REPLACE,
                source_refs=refs, content=content,
            ))

        image_ids = iter(slide.image_ids)
        for slot in variant.slots:
            if slot.kind in textual_kinds:
                continue
            if slot.kind == SlotKind.VISUAL:
                selected: tuple[Any, dict[str, Any]] | None = next((
                    (assets[asset_id], choice)
                    for asset_id in slide.asset_ids
                    for choice in guidance.get(asset_id, {}).get("compatible_variants", [])
                    if choice["family_id"] == slide.template_family_id
                    and choice["slide_number"] == slide.template_slide_number
                    and choice["slot_id"] == slot.slot_id
                ), None)
                if selected is not None:
                    asset, choice = selected
                    assignments.append(NarrativeSlotAssignment(
                        slot_id=slot.slot_id,
                        action=SlotAction.REPLACE,
                        source_refs=[f"asset:{asset.id}"],
                        content=VisualSlotContent(
                            asset_id=asset.id,
                            visual_kind=choice["visual_kind"],
                            subtype=choice.get("subtype"),
                            selected_columns=choice.get("selected_columns", []),
                        ),
                    ))
                elif slot.required:
                    code = "incompatible_template" if slide.asset_ids else "missing_input_asset"
                    raise PlannerFailure(
                        code,
                        f"required visual slot {slot.slot_id!r} has no structured material",
                        {"validation_issues": [{
                            "code": code,
                            "path": f"slides[{slide.number - 1}].assignments",
                            "message": f"Обязательный слот {slot.slot_id!r} не обеспечен материалом.",
                        }]},
                    )
                else:
                    assignments.append(NarrativeSlotAssignment(
                        slot_id=slot.slot_id, action=SlotAction.CLEAR,
                    ))
                continue
            if slot.kind == SlotKind.IMAGE:
                image_id = next(image_ids, None)
                if image_id is not None:
                    image = image_map[image_id]
                    assignments.append(NarrativeSlotAssignment(
                        slot_id=slot.slot_id, action=SlotAction.REPLACE,
                        source_refs=[image.id], content=ImageSlotContent(
                            asset_ref=image.asset_ref, alt_text=Path(image.original_name).stem,
                        ),
                    ))
                else:
                    assignments.append(NarrativeSlotAssignment(
                        slot_id=slot.slot_id,
                        action=SlotAction.KEEP if slot.required else SlotAction.CLEAR,
                    ))
            elif not slot.required:
                assignments.append(NarrativeSlotAssignment(
                    slot_id=slot.slot_id, action=SlotAction.CLEAR,
                ))
            else:
                errors.append(f"required technical slot {slot.slot_id!r} has no grounded material")
        assignment_ids = [assignment.slot_id for assignment in assignments]
        expected_ids = [slot.slot_id for slot in variant.slots]
        if (
            len(assignment_ids) != len(set(assignment_ids))
            or len(expected_ids) != len(set(expected_ids))
            or set(assignment_ids) != set(expected_ids)
        ):
            errors.append(
                "every selected variant slot must have exactly one assignment; "
                f"expected {expected_ids}, got {assignment_ids}"
            )
        if errors:
            raise PlannerFailure(
                "invalid_llm_response", "slide draft violates the selected template",
                {"validation_errors": errors},
            )
        return SlideDraft(number=slide.number, assignments=assignments)

    def _revise_outline(
        self, request: PlanningRequest, sources: list[SourceChunk],
        analysis: ContentAnalysis, catalog: TemplateCatalog, outline: DeckOutline,
        asset_guidance: list[dict[str, Any]], issues: list[dict[str, Any]],
        round_number: int,
    ) -> DeckOutline:
        checkpoint = f"outline_revision_{round_number}"
        cached = self._load_checkpoint(checkpoint, DeckOutline)
        if cached is not None:
            self._validate_outline(request, sources, catalog, cached)
            return cached
        schema = self._dynamic_schema(
            DeckOutline,
            sources=[source.id for source in sources],
            assets=[asset.id for asset in request.structured_assets],
            **self._catalog_selection_enums(catalog),
        )
        slides_schema = schema.get("properties", {}).get("slides", {})
        slides_schema["minItems"] = request.slide_count.min
        slides_schema["maxItems"] = request.slide_count.max
        revised = self._complete_model(
            self._request(self.outline_prompt, DeckOutline, {
                "REQUEST": request.model_dump(mode="json"),
                "CONTENT ANALYSIS": analysis.model_dump(mode="json"),
                "TEMPLATE CATALOG": catalog_prompt_projection(catalog),
                "STRUCTURED ASSET VISUAL GUIDANCE": asset_guidance,
                "CURRENT OUTLINE": outline.model_dump(mode="json"),
                "GLOBAL ISSUES TO FIX": issues,
                "INSTRUCTION": (
                    "Return the complete corrected outline. Keep every unaffected item byte-for-byte "
                    "equivalent so only changed slides need regeneration."
                ),
            }),
            DeckOutline, "outline revision", schema=schema,
        )
        self._validate_outline(request, sources, catalog, revised)
        self._save_checkpoint(checkpoint, revised)
        return revised

    def _generate_slides_parallel(
        self, request: PlanningRequest, sources: list[SourceChunk],
        analysis: ContentAnalysis, catalog: TemplateCatalog, outline: DeckOutline,
        asset_guidance: list[dict[str, Any]], *, checkpoint_prefix: str = "slide",
        existing: dict[int, SlideDraft] | None = None,
    ) -> dict[int, SlideDraft]:
        drafts: dict[int, SlideDraft] = dict(existing or {})
        pending: dict[str, Callable[[], SlideDraft]] = {}
        for index, slide in enumerate(outline.slides):
            if slide.number in drafts:
                continue
            cached = self._load_checkpoint(f"{checkpoint_prefix}_{slide.number}", SlideDraft)
            if cached is not None:
                drafts[slide.number] = cached
            else:
                pending[str(slide.number)] = lambda index=index: self._generate_slide_draft(
                    request, sources, analysis, catalog, outline, asset_guidance, index,
                )
        total = len(outline.slides)
        self._slide_progress_completed = len(drafts)
        self._slide_progress_total = total
        self._notify(f"generating_slides:{len(drafts)}:{total}")
        failed: dict[str, Callable[[], SlideDraft]] = {}
        failure_codes: dict[str, list[str]] = {}
        pending_items = list(pending.items())
        for offset in range(0, len(pending_items), self.max_workers):
            batch = dict(pending_items[offset:offset + self.max_workers])
            self._slide_progress_completed = len(drafts)
            for key, value in self._run_wave("slides", batch).items():
                if isinstance(value, SlideDraft):
                    number = int(key)
                    drafts[number] = value
                    self._save_checkpoint(f"{checkpoint_prefix}_{number}", value)
                else:
                    if isinstance(value, PlannerFailure):
                        failure_codes.setdefault(key, []).append(value.code)
                    index = int(key) - 1
                    failed[key] = lambda index=index: self._generate_slide_draft(
                        request, sources, analysis, catalog, outline, asset_guidance, index,
                    )
        if failed:
            failed_items = list(failed.items())
            for offset in range(0, len(failed_items), self.max_workers):
                batch = dict(failed_items[offset:offset + self.max_workers])
                self._slide_progress_completed = len(drafts)
                for key, value in self._run_wave("slides_retry", batch).items():
                    if isinstance(value, SlideDraft):
                        number = int(key)
                        drafts[number] = value
                        self._save_checkpoint(f"{checkpoint_prefix}_{number}", value)
                    elif isinstance(value, PlannerFailure):
                        failure_codes.setdefault(key, []).append(value.code)
        if len(drafts) != total:
            missing = sorted(set(range(1, total + 1)) - set(drafts))
            non_local = {
                code for number in missing for code in failure_codes.get(str(number), [])
                if code != "invalid_llm_response"
            }
            if non_local:
                code = next(iter(non_local)) if len(non_local) == 1 else "slide_generation_failed"
                raise PlannerFailure(
                    code, "slide generation failed outside local draft repair",
                    {"failed_slides": missing, "cause_codes": failure_codes},
                )
            for number in missing:
                drafts[number] = self._repair_slide_locally(
                    request, sources, catalog, outline, asset_guidance, number - 1,
                )
                self._save_checkpoint(f"{checkpoint_prefix}_{number}", drafts[number])
            self._runtime_warnings.append(
                "Локально восстановлены слайды: " + ", ".join(map(str, missing))
            )
        return drafts

    def _repair_slide_locally(
        self, request: PlanningRequest, sources: list[SourceChunk], catalog: TemplateCatalog,
        outline: DeckOutline, asset_guidance: list[dict[str, Any]], index: int,
    ) -> SlideDraft:
        """Fill one failed draft from its fixed sources and material assignments."""
        slide = outline.slides[index]
        family = next(item for item in catalog.families if item.family_id == slide.template_family_id)
        variant = next(item for item in family.variants if item.slide_number == slide.template_slide_number)
        source_map = {source.id: source.text for source in sources}
        raw_parts = [
            source_map[ref] for ref in slide.source_refs
            if ref in source_map and not ref.startswith("asset:") and not ref.startswith("image:")
        ]
        raw = " ".join(raw_parts).strip() or slide.purpose
        fragments = [
            item.strip(" -•\t") for item in re.split(r"(?:\n+|(?<=[.!?;])\s+)", raw)
            if item.strip(" -•\t")
        ] or [slide.purpose]
        refs = [ref for ref in slide.source_refs if ref in source_map and not ref.startswith("image:")] or ["brief"]
        assets = {asset.id: asset for asset in request.structured_assets}
        images = {image.id: image for image in request.provided_images}
        guidance = {item["asset_id"]: item for item in asset_guidance}
        image_ids = iter(slide.image_ids)
        assignments: list[NarrativeSlotAssignment] = []

        def clipped(value: str, limit: int | None) -> str:
            value = " ".join(value.split())
            if not limit or len(value) <= limit:
                return value
            return value[:max(1, limit - 1)].rstrip() + "…"

        for slot in variant.slots:
            content: Any | None = None
            if slot.kind == SlotKind.TEXT:
                value = slide.purpose if re.search(r"title|head|заголов", f"{slot.slot_id} {slot.role}", re.I) else fragments[0]
                content = TextSlotContent(text=clipped(value, slot.max_chars))
            elif slot.kind == SlotKind.BULLET_LIST:
                content = BulletListContent(items=[clipped(item, slot.max_chars) for item in fragments[:slot.capacity or 4]])
            elif slot.kind == SlotKind.CARDS:
                content = CardsContent(items=[
                    CardItem(title=clipped(item.split("—", 1)[0], slot.max_chars), body=clipped(item, slot.max_chars))
                    for item in fragments[:slot.capacity or 3]
                ])
            elif slot.kind == SlotKind.PROFILES:
                items = []
                for item in fragments[:slot.capacity or 3]:
                    parts = re.split(r"\s+[—-]\s+|:\s+", item, maxsplit=1)
                    items.append(ProfileItem(
                        name=clipped(parts[0], slot.max_chars),
                        role=clipped(parts[1] if len(parts) > 1 else "Участник команды", slot.max_chars),
                    ))
                content = ProfilesContent(items=items)
            elif slot.kind == SlotKind.METRICS:
                matches = re.findall(r"([^.;]{0,60}?)\b([-+]?\d+(?:[.,]\d+)?)\s*(%|[A-Za-zА-Яа-яЁё]+)?", raw)
                items = [MetricItem(label=clipped(label.strip(" :-") or "Показатель", slot.max_chars), value=float(value.replace(",", ".")), unit=unit or None) for label, value, unit in matches[:slot.capacity or 3]]
                content = MetricsContent(items=items or [MetricItem(label="Показатель", value="—")])
            elif slot.kind == SlotKind.TIMELINE:
                content = TimelineContent(items=[TimelineItem(date=str(i + 1), title=clipped(item, slot.max_chars)) for i, item in enumerate(fragments[:slot.capacity or 4])])
            elif slot.kind == SlotKind.PROCESS:
                content = ProcessContent(items=[ProcessItem(title=clipped(item, slot.max_chars)) for item in fragments[:slot.capacity or 4]])
            elif slot.kind == SlotKind.VISUAL:
                selected = next((
                    (assets[asset_id], choice) for asset_id in slide.asset_ids
                    for choice in guidance.get(asset_id, {}).get("compatible_variants", [])
                    if choice["family_id"] == slide.template_family_id
                    and choice["slide_number"] == slide.template_slide_number
                    and choice["slot_id"] == slot.slot_id
                ), None)
                if selected:
                    asset, choice = selected
                    assignments.append(NarrativeSlotAssignment(
                        slot_id=slot.slot_id, action=SlotAction.REPLACE,
                        source_refs=[f"asset:{asset.id}"], content=VisualSlotContent(
                            asset_id=asset.id, visual_kind=choice["visual_kind"],
                            subtype=choice.get("subtype"), selected_columns=choice.get("selected_columns", []),
                        ),
                    ))
                    continue
            elif slot.kind == SlotKind.IMAGE:
                image_id = next(image_ids, None)
                if image_id and image_id in images:
                    image = images[image_id]
                    assignments.append(NarrativeSlotAssignment(
                        slot_id=slot.slot_id, action=SlotAction.REPLACE,
                        source_refs=[image.id], content=ImageSlotContent(
                            asset_ref=image.asset_ref, alt_text=Path(image.original_name).stem,
                        ),
                    ))
                    continue
            if content is not None:
                assignments.append(NarrativeSlotAssignment(
                    slot_id=slot.slot_id, action=SlotAction.REPLACE,
                    source_refs=refs, content=content,
                ))
            else:
                assignments.append(NarrativeSlotAssignment(
                    slot_id=slot.slot_id,
                    action=SlotAction.KEEP if slot.required else SlotAction.CLEAR,
                ))
        return SlideDraft(number=slide.number, assignments=assignments)

    @classmethod
    def _assemble_outline(
        cls, outline: DeckOutline, drafts: dict[int, SlideDraft], catalog: TemplateCatalog,
    ) -> PlanCandidate:
        slides: list[SlidePlan] = []
        for item in outline.slides:
            draft = drafts[item.number]
            narrative = NarrativeSlide(
                purpose=item.purpose,
                template_family_id=item.template_family_id,
                template_slide_number=item.template_slide_number,
                chart_asset_id=item.chart_asset_id,
                assignments=draft.assignments,
            )
            slides.append(cls._expand_narrative_slide(narrative, catalog, item.number))
        return PlanCandidate(deck=outline.deck, slides=slides)

    def _normalize_candidate(
        self, request: PlanningRequest, catalog: TemplateCatalog, candidate: PlanCandidate,
        *, ground_assets: bool = True,
    ) -> PlanCandidate:
        candidate = self._canonicalize_candidate(catalog, candidate, request)
        if ground_assets:
            candidate = self._ground_candidate(request, catalog, candidate)
        candidate = self._canonicalize_candidate(catalog, candidate, request)
        return self._paginate_candidate(request, catalog, candidate)

    def _ground_candidate(
        self, request: PlanningRequest, catalog: TemplateCatalog, candidate: PlanCandidate,
    ) -> PlanCandidate:
        """Apply facts that are mechanical consequences of uploaded assets.

        Models choose the narrative. They do not get to change an uploaded dataset's
        renderer or discard an explicitly named image.
        """
        result = candidate.model_copy(deep=True)
        families = {family.family_id: family for family in catalog.families}
        guidance = {
            item["asset_id"]: item["compatible_variants"][0]
            for item in self._asset_guidance(request, catalog)
            if item.get("compatible_variants")
        }
        claimed_slides: set[int] = set()

        for asset in request.structured_assets:
            selected = guidance.get(asset.id)
            if selected is None:
                continue
            ref = f"asset:{asset.id}"
            target_index = next((
                index for index, slide in enumerate(result.slides)
                if index not in claimed_slides and (
                    slide.chart_asset_id == asset.id
                    or any(
                        isinstance(assignment.content, VisualSlotContent)
                        and assignment.content.asset_id == asset.id
                        for assignment in slide.assignments
                    )
                )
            ), None)
            if target_index is None:
                target_index = next((
                    index for index, slide in enumerate(result.slides)
                    if index not in claimed_slides
                    and any(ref in assignment.source_refs for assignment in slide.assignments)
                ), None)
            if target_index is None:
                title = asset.title.casefold()
                target_index = next((
                    index for index, slide in enumerate(result.slides)
                    if index not in claimed_slides
                    and title in json.dumps(slide.model_dump(mode="json"), ensure_ascii=False).casefold()
                ), None)
            if target_index is None:
                target_index = next((
                    index for index in range(1, len(result.slides))
                    if index not in claimed_slides
                ), None)
            if target_index is None:
                continue
            claimed_slides.add(target_index)
            slide = result.slides[target_index]
            family = families[selected["family_id"]]
            variant = next(
                item for item in family.variants
                if item.slide_number == selected["slide_number"]
            )
            authored_text = [
                assignment.model_copy(deep=True) for assignment in slide.assignments
                if isinstance(assignment.content, TextSlotContent)
            ]
            normalized: list[SlotAssignment] = []
            for slot in variant.slots:
                if slot.slot_id == selected["slot_id"]:
                    normalized.append(SlotAssignment(
                        slot_id=slot.slot_id, kind=SlotKind.VISUAL,
                        target_shape_ids=list(slot.target_shape_ids),
                        action=SlotAction.REPLACE, source_refs=[ref],
                        content=VisualSlotContent(
                            asset_id=asset.id,
                            visual_kind=selected["visual_kind"],
                            subtype=selected.get("subtype"),
                            selected_columns=selected.get("selected_columns", []),
                        ),
                    ))
                elif slot.kind == SlotKind.TEXT:
                    authored = authored_text.pop(0) if authored_text else None
                    if authored is not None:
                        authored.slot_id = slot.slot_id
                        authored.kind = slot.kind
                        authored.target_shape_ids = list(slot.target_shape_ids)
                        normalized.append(authored)
                    elif slot.required:
                        normalized.append(SlotAssignment(
                            slot_id=slot.slot_id, kind=slot.kind,
                            target_shape_ids=list(slot.target_shape_ids),
                            action=SlotAction.REPLACE, source_refs=[ref],
                            content=TextSlotContent(text=asset.title[:slot.max_chars] if slot.max_chars else asset.title),
                        ))
                    else:
                        normalized.append(SlotAssignment(
                            slot_id=slot.slot_id, kind=slot.kind,
                            target_shape_ids=list(slot.target_shape_ids), action=SlotAction.CLEAR,
                        ))
                elif slot.kind == SlotKind.IMAGE and slot.required:
                    normalized.append(SlotAssignment(
                        slot_id=slot.slot_id, kind=slot.kind,
                        target_shape_ids=list(slot.target_shape_ids), action=SlotAction.KEEP,
                    ))
                elif not slot.required:
                    normalized.append(SlotAssignment(
                        slot_id=slot.slot_id, kind=slot.kind,
                        target_shape_ids=list(slot.target_shape_ids), action=SlotAction.CLEAR,
                    ))
            slide.template_family_id = family.family_id
            slide.template_slide_number = variant.slide_number
            slide.template_class = family.slide_class
            slide.chart_asset_id = asset.id if selected["visual_kind"] == "chart" else None
            slide.assignments = normalized

        sources = build_sources(request)
        source_map = {source.id: source.text for source in sources}
        used_image_refs: set[str] = set()
        image_targets: list[tuple[Any, Any, dict[str, Any], str]] = []
        for slide in result.slides:
            slide_refs = {
                ref for assignment in slide.assignments for ref in assignment.source_refs
            }
            serialized = (
                json.dumps(slide.model_dump(mode="json"), ensure_ascii=False)
                + "\n" + "\n".join(source_map.get(ref, "") for ref in slide_refs)
            ).casefold()
            family = families.get(slide.template_family_id)
            variant = next((
                item for item in family.variants
                if item.slide_number == slide.template_slide_number
            ), None) if family else None
            slots = {slot.slot_id: slot for slot in variant.slots} if variant else {}
            for assignment in slide.assignments:
                slot = slots.get(assignment.slot_id)
                if slot is not None and slot.kind == SlotKind.IMAGE:
                    image_targets.append((slide, assignment, slots, serialized))
                if isinstance(assignment.content, ImageSlotContent) and assignment.content.asset_ref:
                    used_image_refs.add(assignment.content.asset_ref)
            for image in request.provided_images:
                if image.asset_ref in used_image_refs:
                    continue
                names = {image.original_name.casefold(), Path(image.original_name).stem.casefold()}
                if not any(name and name in serialized for name in names):
                    continue
                assignment = next((
                    item for item in slide.assignments
                    if slots.get(item.slot_id) is not None
                    and slots[item.slot_id].kind == SlotKind.IMAGE
                    and not (
                        isinstance(item.content, ImageSlotContent)
                        and item.content.asset_ref
                    )
                ), None)
                if assignment is not None:
                    assignment.kind = SlotKind.IMAGE
                    assignment.action = SlotAction.REPLACE
                    assignment.source_refs = [image.id]
                    assignment.content = ImageSlotContent(
                        asset_ref=image.asset_ref, alt_text=Path(image.original_name).stem,
                    )
                    used_image_refs.add(image.asset_ref)
                    break
            for assignment in slide.assignments:
                slot = slots.get(assignment.slot_id)
                slot_text = f"{assignment.slot_id} {slot.role if slot else ''}".casefold()
                related_sources: list[SourceChunk] = []
                content_indexes = sorted(
                    int(match.group(1)) for ref in slide_refs
                    if (match := re.fullmatch(r"content_(\d+)", ref))
                )
                if content_indexes:
                    # A revision may add an outline source (for example content_013)
                    # next to the actual profile block (content_026). The later
                    # reference is the concrete block containing role/details.
                    start = content_indexes[-1]
                    related_sources = [
                        source for source in sources
                        if (match := re.fullmatch(r"content_(\d+)", source.id))
                        and start <= int(match.group(1)) <= start + 4
                    ]
                detail_sources = [
                    source for source in related_sources
                    if re.search(r"опыт|ответствен|experience|responsib", source.text, re.IGNORECASE)
                ]
                if (
                    assignment.action == SlotAction.CLEAR
                    and slot is not None and slot.kind == SlotKind.BULLET_LIST
                    and re.search(r"detail|bullet|опыт|ответ", slot_text)
                    and detail_sources
                ):
                    items = [
                        source.text[:slot.max_chars] if slot.max_chars else source.text
                        for source in detail_sources
                    ]
                    assignment.kind = SlotKind.BULLET_LIST
                    assignment.action = SlotAction.REPLACE
                    assignment.source_refs = [source.id for source in detail_sources]
                    assignment.content = BulletListContent(items=items)
                if not isinstance(assignment.content, ProfilesContent):
                    continue
                for profile in assignment.content.items:
                    if profile.details:
                        continue
                    referenced = [
                        source for source in sources
                        if source.id in assignment.source_refs and re.search(
                            r"опыт|ответствен|experience|responsib", source.text, re.IGNORECASE,
                        )
                    ]
                    matching = referenced or [
                        source for source in sources
                        if profile.name.casefold() in source.text.casefold()
                    ]
                    fragments: list[str] = []
                    for source in matching:
                        parts = re.split(r"(?:[.;]|\n|\s+[—-]\s+)", source.text)
                        fragments.extend(
                            part.strip() for part in parts
                            if re.search(r"опыт|ответствен|experience|responsib", part, re.IGNORECASE)
                        )
                        if source.id not in assignment.source_refs:
                            assignment.source_refs.append(source.id)
                    if fragments:
                        limit = slots.get(assignment.slot_id).max_chars if slots.get(assignment.slot_id) else None
                        profile.details = [part[:limit] if limit else part for part in fragments[:2]]
        remaining_images = [
            image for image in request.provided_images if image.asset_ref not in used_image_refs
        ]
        remaining_targets = [
            target for target in image_targets
            if not (
                isinstance(target[1].content, ImageSlotContent)
                and target[1].content.asset_ref
            )
        ]
        remaining_targets.sort(key=lambda target: (
            0 if re.search(
                r"photo|portrait|avatar|портрет|profile|профил",
                f"{target[0].purpose} {target[1].slot_id}", re.IGNORECASE,
            ) else 1,
            target[0].number,
        ))
        for image, (_, assignment, _, _) in zip(remaining_images, remaining_targets):
            assignment.kind = SlotKind.IMAGE
            assignment.action = SlotAction.REPLACE
            assignment.source_refs = [image.id]
            assignment.content = ImageSlotContent(
                asset_ref=image.asset_ref, alt_text=Path(image.original_name).stem,
            )
        return result

    def _log_structural_issues(
        self, candidate_index: int, phase: str, issues: list[ValidationIssue],
    ) -> None:
        summary = [
            {"code": issue.code, "message": issue.message[:160]}
            for issue in issues[:12]
        ]
        logger.warning(
            "candidate_structural_invalid job=%s candidate=%d phase=%s issues=%s",
            self.job_id or "-", candidate_index, phase,
            json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
        )

    def _run_wave(
        self, name: str, tasks: dict[str, Callable[[], ModelT]],
    ) -> dict[str, ModelT | PlannerFailure]:
        if not tasks:
            return {}
        started = time.monotonic()
        results: dict[str, ModelT | PlannerFailure] = {}
        if name.startswith("critique"):
            wave_limit = self.critique_wave_budget
        elif name == "outline":
            wave_limit = self.outline_wave_budget
        else:
            wave_limit = self.slide_wave_budget
        remaining = (
            max(0.0, self._deadline - started) if self._deadline is not None else wave_limit
        )
        timeout = min(wave_limit, remaining)
        wave_deadline = started + timeout

        def run(task: Callable[[], ModelT]) -> ModelT:
            self._task_state.deadline = wave_deadline
            try:
                return task()
            finally:
                self._task_state.deadline = None

        executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="planner")
        futures = {executor.submit(run, task): key for key, task in tasks.items()}
        pending = set(futures)
        done: set[Any] = set()
        while pending:
            remaining_wait = max(0.0, wave_deadline - time.monotonic())
            if remaining_wait <= 0:
                break
            completed, pending = wait(
                pending, timeout=remaining_wait, return_when=FIRST_COMPLETED,
            )
            if not completed:
                break
            done.update(completed)
            if name.startswith("slides"):
                wave_completed = sum(
                    future.done() and not future.cancelled() for future in futures
                )
                completed_count = getattr(self, "_slide_progress_completed", 0) + wave_completed
                total_count = getattr(self, "_slide_progress_total", len(tasks))
                self._notify(f"generating_slides:{completed_count}:{total_count}")
        unfinished = pending
        try:
            for future in done:
                key = futures[future]
                try:
                    results[key] = future.result()
                except PlannerFailure as exc:
                    results[key] = exc
                    details = (
                        exc.details.get("validation_issues")
                        or exc.details.get("validation_errors")
                        or []
                    )
                    logger.warning(
                        "planner_wave_failure job=%s wave=%s task=%s code=%s summary=%s",
                        self.job_id or "-", name, key, exc.code,
                        _safe_error_summary([str(item) for item in details]),
                    )
                except Exception as exc:
                    results[key] = PlannerFailure(
                        "internal_pipeline_error", f"{name} failed internally",
                    )
                    logger.warning(
                        "planner_wave_failure job=%s wave=%s task=%s code=internal_pipeline_error summary=%s",
                        self.job_id or "-", name, key, _safe_error_summary([str(exc)]),
                    )
            for future in unfinished:
                key = futures[future]
                future.cancel()
                results[key] = PlannerFailure(
                    "planning_budget_exhausted", f"{name} exceeded its {timeout:g}s wave limit",
                    {"validation_issues": [{
                        "code": "planning_budget_exhausted", "path": name,
                        "message": f"Этап {name!r} превысил лимит {timeout:g} с.",
                    }]},
                )
        finally:
            # Running HTTP calls receive wave_deadline and must be joined before
            # returning so no orphan request can mutate checkpoints later.
            executor.shutdown(wait=True, cancel_futures=True)
        failures = sum(isinstance(value, PlannerFailure) for value in results.values())
        logger.info(
            "planner_wave job=%s wave=%s requests=%d failures=%d duration=%.3f",
            self.job_id or "-", name, len(tasks), failures, time.monotonic() - started,
        )
        return results

    def _evaluate_candidates(
        self, request: PlanningRequest, sources: list[SourceChunk], analysis: ContentAnalysis,
        catalog: TemplateCatalog, candidates: dict[int, PlanCandidate], *, round_number: int,
    ) -> dict[int, PlanCritique]:
        profiles = {
            "facts": "Evaluate only factuality and mandatory-fact coverage; still fill every score.",
            "narrative": "Evaluate only narrative coherence and conciseness; still fill every score.",
            "visual": "Evaluate only template fit and visual suitability; still fill every score.",
        }
        by_candidate: dict[int, dict[str, PlanCritique]] = {index: {} for index in candidates}
        tasks: dict[str, Callable[[], PlanCritique]] = {}
        for index, candidate in candidates.items():
            for profile, instruction in profiles.items():
                checkpoint = f"critique_r{round_number}_c{index}_{profile}"
                cached = self._load_checkpoint(checkpoint, PlanCritique)
                if cached is not None:
                    by_candidate[index][profile] = cached
                    continue
                key = f"{index}:{profile}"
                tasks[key] = lambda candidate=candidate, profile=profile, instruction=instruction: self._critique(
                    request, sources, analysis, catalog, candidate,
                    profile=profile, instruction=instruction,
                )
        for key, value in self._run_wave(f"critique_round_{round_number}", tasks).items():
            index_text, profile = key.split(":", 1)
            index = int(index_text)
            if isinstance(value, PlanCritique):
                by_candidate[index][profile] = value
                self._save_checkpoint(f"critique_r{round_number}_c{index}_{profile}", value)
            else:
                by_candidate[index][profile] = self._failed_critique(profile)
        return {
            index: self._merge_critiques(values or {"facts": self._failed_critique("all")})
            for index, values in by_candidate.items()
        }

    @staticmethod
    def _merge_critiques(critiques: dict[str, PlanCritique]) -> PlanCritique:
        fallback = next(iter(critiques.values()))
        facts = critiques.get("facts", fallback)
        narrative = critiques.get("narrative", fallback)
        visual = critiques.get("visual", fallback)
        scores = CritiqueScores(
            factuality=facts.scores.factuality,
            coverage=facts.scores.coverage,
            narrative=narrative.scores.narrative,
            conciseness=narrative.scores.conciseness,
            template_fit=visual.scores.template_fit,
        )
        issues: list[CritiqueIssue] = []
        seen: set[tuple[str, str, int | None, str, str]] = set()
        for critique in critiques.values():
            for issue in critique.issues:
                key = (issue.severity, issue.message, issue.slide_number, issue.code, issue.dimension)
                if key not in seen:
                    seen.add(key)
                    issues.append(issue)
        return PlanCritique(scores=scores, issues=issues)

    @staticmethod
    def _failed_critique(profile: str) -> PlanCritique:
        return PlanCritique(
            scores=CritiqueScores(
                factuality=1, coverage=1, narrative=1, template_fit=1, conciseness=1,
            ),
            issues=[CritiqueIssue(
                severity="warning", code="critic_unavailable", dimension="global",
                message=f"Не удалось завершить смысловую проверку ({profile})",
            )],
        )

    @staticmethod
    def _critique_from_validation(issues: list[ValidationIssue]) -> PlanCritique:
        return PlanCritique(
            scores=CritiqueScores(
                factuality=1, coverage=1, narrative=1, template_fit=1, conciseness=1,
            ),
            issues=[CritiqueIssue(
                severity="blocking", code=issue.code, dimension="global",
                message=issue.message,
            ) for issue in issues],
        )

    @staticmethod
    def _candidate_rank(critique: PlanCritique, index: int) -> tuple[int, int, int, int, int, int]:
        blocking = sum(issue.severity == "blocking" for issue in critique.issues)
        warning_count = len(critique.issues)
        scores = critique.scores
        other = [scores.coverage, scores.narrative, scores.template_fit, scores.conciseness]
        return (
            int(blocking == 0), int(scores.factuality == 5), min(other),
            scores.factuality + sum(other), -warning_count, -index,
        )

    @staticmethod
    def _critique_warnings(critique: PlanCritique) -> list[str]:
        warnings = list(dict.fromkeys(issue.message for issue in critique.issues))
        if not warnings:
            warnings.append("Смысловые пороги качества не достигнуты; проверьте презентацию")
        return warnings

    def _build_result(
        self, request: PlanningRequest, catalog: TemplateCatalog, sources: list[SourceChunk],
        candidate: PlanCandidate, quality: QualityReport,
    ) -> PresentationPlan:
        families = {family.family_id: family for family in catalog.families}
        for slide in candidate.slides:
            family = families[slide.template_family_id]
            variant = next(
                item for item in family.variants
                if item.slide_number == slide.template_slide_number
            )
            try:
                slide.render_operations = compile_slide(slide, variant, request.structured_assets)
            except CompilationError as exc:
                raise PlannerFailure(
                    "render_compilation_failed", f"slide {slide.number}: {exc}",
                ) from exc
        result = PresentationPlan(
            schema_version=PLAN_SCHEMA_VERSION, model=self.llm.model,
            template=TemplateReference(
                source_sha256=catalog.source_sha256,
                catalog_version=CATALOG_SCHEMA_VERSION,
            ),
            deck=candidate.deck, sources=sources,
            structured_assets=request.structured_assets,
            slides=candidate.slides, quality=quality,
        )
        self._save_checkpoint("validated_plan", result)
        return result

    def _notify(self, stage: str) -> None:
        if self.progress is not None:
            self.progress(stage)

    def _input_hash(self, request: PlanningRequest, catalog: TemplateCatalog) -> str:
        payload = {
            "request": request.model_dump(mode="json"),
            "catalog": catalog.model_dump(mode="json"),
            "model": self.llm.model,
            "pipeline": {
                "version": PIPELINE_VERSION,
                "strict": self.enable_critique and self._supports_native_schema(),
                "max_revisions": self.max_revisions,
                "allow_model_assets": self.allow_model_assets,
                "semantic_pipeline": self.semantic_pipeline,
            },
            "prompts": {
                "analysis": hashlib.sha256(self.analysis_prompt.encode()).hexdigest(),
                "draft": hashlib.sha256(self.draft_prompt.encode()).hexdigest(),
                "outline": hashlib.sha256(self.outline_prompt.encode()).hexdigest(),
                "slide": hashlib.sha256(self.slide_prompt.encode()).hexdigest(),
                "critique": hashlib.sha256(self.critique_prompt.encode()).hexdigest(),
                "revision": hashlib.sha256(self.revision_prompt.encode()).hexdigest(),
            },
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    def _load_checkpoint(self, stage: str, model: type[ModelT]) -> ModelT | None:
        if self.checkpoint_dir is None:
            return None
        path = self.checkpoint_dir / f"checkpoint_{stage}.json"
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if envelope.get("key") != self._checkpoint_key or envelope.get("stage") != stage:
                return None
            return model.model_validate(envelope["payload"])
        except (OSError, KeyError, TypeError, ValueError, ValidationError):
            return None

    def _save_checkpoint(self, stage: str, value: BaseModel) -> None:
        if self.checkpoint_dir is None:
            return
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        envelope = {
            "stage": stage,
            "key": self._checkpoint_key,
            "payload": value.model_dump(mode="json"),
        }
        handle, temporary = tempfile.mkstemp(
            prefix=f".{stage}.", suffix=".json", dir=self.checkpoint_dir,
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(envelope, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.checkpoint_dir / f"checkpoint_{stage}.json")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def generate_fallback(
        self, request: PlanningRequest, *, reason: str = "model output required fallback",
    ) -> PresentationPlan:
        """Build a grounded, deterministic deck without another model call."""
        catalog = self._catalog()
        sources = build_sources(request)
        source_ids = [source.id for source in sources]
        image_source_ids = {image.id for image in request.provided_images}
        text_sources = [
            source for source in sources
            if not source.id.startswith("asset:") and source.id not in image_source_ids
        ]
        if not text_sources:
            text_sources = sources

        def usable(family: Any, variant: Any) -> bool:
            return bool(variant.slots) and all(
                not slot.required or slot.kind in {SlotKind.TEXT, SlotKind.IMAGE}
                for slot in variant.slots
            )

        choices = [
            (family, variant)
            for family in catalog.families for variant in family.variants
            if usable(family, variant)
        ]
        if not choices:
            raise PlannerFailure(
                "incompatible_template", "template has no deterministic text-capable variant",
            )

        def preferred(class_name: str | None = None) -> tuple[Any, Any]:
            candidates = choices
            if class_name:
                matched = [item for item in choices if item[0].slide_class.value == class_name]
                if matched:
                    candidates = matched
            return min(
                candidates,
                key=lambda item: (
                    sum(slot.required for slot in item[1].slots), item[1].slide_number,
                ),
            )

        def safe_text(text: str, limit: int | None) -> str:
            value = " ".join(text.split()) or request.brief.strip()
            if not limit or len(value) <= limit:
                return value
            words: list[str] = []
            for word in value.split():
                candidate = " ".join([*words, word])
                if len(candidate) > limit:
                    break
                words.append(word)
            return " ".join(words) or value[:limit].rstrip()

        def text_slide(family: Any, variant: Any, source: SourceChunk, refs: list[str]) -> Any:
            assignments: list[dict[str, Any]] = []
            used_text = False
            for slot in variant.slots:
                if slot.kind == SlotKind.TEXT and (slot.required or not used_text):
                    assignments.append({
                        "slot_id": slot.slot_id,
                        "kind": slot.kind.value,
                        "target_shape_ids": slot.target_shape_ids,
                        "action": "replace",
                        "source_refs": refs,
                        "content": {"kind": "text", "text": safe_text(source.text, slot.max_chars)},
                    })
                    used_text = True
                elif slot.kind == SlotKind.IMAGE and slot.required:
                    assignments.append({
                        "slot_id": slot.slot_id, "kind": slot.kind.value,
                        "target_shape_ids": slot.target_shape_ids, "action": "keep",
                    })
                else:
                    assignments.append({
                        "slot_id": slot.slot_id, "kind": slot.kind.value,
                        "target_shape_ids": slot.target_shape_ids, "action": "clear",
                    })
            return {
                "number": 1,
                "purpose": safe_text(source.text, 160),
                "template_family_id": family.family_id,
                "template_slide_number": variant.slide_number,
                "template_class": family.slide_class.value,
                "assignments": assignments,
            }

        slides: list[dict[str, Any]] = []
        cover = preferred("cover")
        slides.append(text_slide(*cover, text_sources[0], source_ids))

        # Structured assets are assigned by code, exactly once and in request order.
        guidance = self._asset_guidance(request, catalog)
        guidance_by_id = {item["asset_id"]: item for item in guidance}
        families = {family.family_id: family for family in catalog.families}
        for asset in request.structured_assets:
            if asset.visual_hint.mode == "none":
                continue
            variants = guidance_by_id.get(asset.id, {}).get("compatible_variants", [])
            if not variants:
                continue
            selected = variants[0]
            family = families[selected["family_id"]]
            variant = next(
                item for item in family.variants
                if item.slide_number == selected["slide_number"]
            )
            assignments: list[dict[str, Any]] = []
            for slot in variant.slots:
                if slot.slot_id == selected["slot_id"]:
                    assignments.append({
                        "slot_id": slot.slot_id, "kind": "visual",
                        "target_shape_ids": slot.target_shape_ids, "action": "replace",
                        "source_refs": [f"asset:{asset.id}"],
                        "content": {
                            "kind": "visual", "asset_id": asset.id,
                            "visual_kind": selected["visual_kind"],
                            "subtype": selected.get("subtype"),
                            "selected_columns": selected.get("selected_columns", []),
                        },
                    })
                elif slot.kind == SlotKind.TEXT:
                    assignments.append({
                        "slot_id": slot.slot_id, "kind": "text",
                        "target_shape_ids": slot.target_shape_ids, "action": "replace",
                        "source_refs": [f"asset:{asset.id}"],
                        "content": {"kind": "text", "text": safe_text(asset.title, slot.max_chars)},
                    })
                elif slot.kind == SlotKind.IMAGE and slot.required:
                    assignments.append({
                        "slot_id": slot.slot_id, "kind": "image",
                        "target_shape_ids": slot.target_shape_ids, "action": "keep",
                    })
                else:
                    assignments.append({
                        "slot_id": slot.slot_id, "kind": slot.kind.value,
                        "target_shape_ids": slot.target_shape_ids, "action": "clear",
                    })
            slides.append({
                "number": 1, "purpose": asset.title,
                "template_family_id": family.family_id,
                "template_slide_number": variant.slide_number,
                "template_class": family.slide_class.value,
                "chart_asset_id": asset.id if selected["visual_kind"] == "chart" else None,
                "assignments": assignments,
            })

        image_variants = [
            (family, variant)
            for family, variant in choices
            if any(slot.kind == SlotKind.IMAGE for slot in variant.slots)
        ]
        for image in request.provided_images:
            if not image_variants or len(slides) >= request.slide_count.max:
                break
            family, variant = min(
                image_variants,
                key=lambda item: (sum(slot.required for slot in item[1].slots), item[1].slide_number),
            )
            source = next((
                item for item in text_sources
                if image.original_name.casefold() in item.text.casefold()
            ), text_sources[min(len(slides), len(text_sources) - 1)])
            slide = text_slide(family, variant, source, [source.id, image.id])
            image_slot = next(slot for slot in variant.slots if slot.kind == SlotKind.IMAGE)
            assignment = next(item for item in slide["assignments"] if item["slot_id"] == image_slot.slot_id)
            assignment.update({
                "kind": "image", "action": "replace", "source_refs": [image.id],
                "content": {
                    "kind": "image", "asset_ref": image.asset_ref,
                    "alt_text": Path(image.original_name).stem,
                },
            })
            slides.append(slide)

        body = preferred("content_text")
        while len(slides) < max(1, request.slide_count.min - 1):
            source = text_sources[(len(slides) - 1) % len(text_sources)]
            slides.append(text_slide(*body, source, source_ids))
        if len(slides) < request.slide_count.max:
            closing = preferred("closing")
            slides.append(text_slide(*closing, text_sources[-1], source_ids))
        if len(slides) > request.slide_count.max:
            raise PlannerFailure("content_overflow", "deterministic fallback exceeds slide limit")

        for number, slide in enumerate(slides, 1):
            slide["number"] = number
        first = text_sources[0]
        candidate = PlanCandidate.model_validate({
            "deck": {
                "title": {"text": safe_text(request.brief, 160), "source_refs": ["brief"]},
                "summary": {"text": safe_text(first.text, 300), "source_refs": [first.id]},
                "language": "ru",
            },
            "slides": slides,
        })
        candidate = self._normalize_candidate(request, catalog, candidate)
        if len(candidate.slides) < request.slide_count.min:
            raise PlannerFailure("content_overflow", "fallback could not meet minimum slide count")
        analysis = ContentAnalysis.model_validate({
            "goal": request.brief,
            "audience": "presentation audience",
            "language": "ru",
            "narrative": [slide.purpose for slide in candidate.slides],
            "recommended_slide_count": len(candidate.slides),
            "facts": [{
                "id": f"source_{index}", "text": source.text,
                "source_refs": [source.id], "mandatory": True,
            } for index, source in enumerate(sources, 1)],
        })
        issues = validate_candidate_issues(request, sources, analysis, catalog, candidate)
        if issues:
            raise PlannerFailure(
                "content_overflow" if any(item.code == "content_overflow" for item in issues)
                else "quality_gate_failed",
                "deterministic fallback did not pass validation",
                {"validation_issues": [item.as_dict() for item in issues]},
            )
        for slide in candidate.slides:
            family = families[slide.template_family_id]
            variant = next(
                item for item in family.variants
                if item.slide_number == slide.template_slide_number
            )
            slide.render_operations = compile_slide(slide, variant, request.structured_assets)
        result = PresentationPlan(
            model=self.llm.model,
            template=TemplateReference(
                source_sha256=catalog.source_sha256,
                catalog_version=CATALOG_SCHEMA_VERSION,
            ),
            deck=candidate.deck, sources=sources,
            structured_assets=request.structured_assets,
            slides=candidate.slides,
            quality=QualityReport(
                status="needs_review", revision_count=0,
                warnings=["Использован упрощённый вариант", reason],
                result_kind="fallback",
            ),
        )
        self._save_checkpoint("validated_plan", result)
        return result

    @staticmethod
    def _canonicalize_candidate(
        catalog: TemplateCatalog, candidate: PlanCandidate, request: PlanningRequest | None = None,
    ) -> PlanCandidate:
        """Fill catalog-owned fields without spending a revision on the LLM.

        Template class, shape IDs, optional empty slots, ordering, and removal of
        unknown/duplicate assignments are mechanical consequences of the chosen
        variant.  The model remains responsible for selecting a variant and for
        all actual content.
        """
        families = {family.family_id: family for family in catalog.families}
        assets = {asset.id: asset for asset in (request.structured_assets if request else [])}
        for number, slide in enumerate(candidate.slides, start=1):
            slide.number = number
            selected_family = families.get(slide.template_family_id)
            selected_variant = next((
                item for item in selected_family.variants
                if item.slide_number == slide.template_slide_number
            ), None) if selected_family else None
            if selected_family is not None and selected_variant is None:
                matches = [
                    (family, variant)
                    for family in catalog.families for variant in family.variants
                    if variant.slide_number == slide.template_slide_number
                ]
                if len(matches) == 1:
                    selected_family, selected_variant = matches[0]
                    slide.template_family_id = selected_family.family_id
            if slide.chart_asset_id is None:
                referenced_chart_ids = [
                    assignment.content.asset_id
                    for assignment in slide.assignments
                    if isinstance(assignment.content, VisualSlotContent)
                    and assignment.content.visual_kind == "chart"
                ]
                if len(set(referenced_chart_ids)) == 1:
                    slide.chart_asset_id = referenced_chart_ids[0]
            chart_asset = assets.get(slide.chart_asset_id or "")
            chart_choice = None
            if isinstance(chart_asset, DatasetAsset):
                try:
                    chart_choice = choose_visual(chart_asset)
                except StructuredAssetError:
                    chart_choice = None
            if chart_choice and chart_choice[0] == "chart":
                selected = select_chart_canvas(catalog, chart_choice[1] or "column")
                if selected is not None:
                    chosen_family, chosen_variant, _ = selected
                    # Keep only narrative-authored title content. Dataset rows,
                    # categories and values are always synthesized below.
                    text_assignments = [
                        assignment for assignment in slide.assignments
                        if isinstance(assignment.content, TextSlotContent)
                    ]
                    title_assignment = next((
                        assignment for assignment in text_assignments
                        if set(re.findall(r"[a-zа-яё]+", assignment.slot_id.casefold()))
                        & {"title", "heading", "header", "заголовок"}
                    ), text_assignments[0] if text_assignments else None)
                    slide.template_family_id = chosen_family.family_id
                    slide.template_slide_number = chosen_variant.slide_number
                    slide.template_class = chosen_family.slide_class
                    normalized_chart: list[SlotAssignment] = []
                    for slot in chosen_variant.slots:
                        if slot.chart_canvas is not None:
                            columns = default_visual_columns(chart_asset, *chart_choice)
                            normalized_chart.append(SlotAssignment(
                                slot_id=slot.slot_id,
                                kind=SlotKind.VISUAL,
                                target_shape_ids=[],
                                action=SlotAction.REPLACE,
                                source_refs=[f"asset:{chart_asset.id}"],
                                content=VisualSlotContent(
                                    asset_id=chart_asset.id,
                                    visual_kind="chart",
                                    subtype=chart_choice[1],
                                    selected_columns=columns,
                                    page=1,
                                ),
                            ))
                        elif slot.kind == SlotKind.TEXT and title_assignment is not None:
                            replacement = title_assignment.model_copy(deep=True)
                            replacement.slot_id = slot.slot_id
                            replacement.kind = slot.kind
                            replacement.target_shape_ids = list(slot.target_shape_ids)
                            normalized_chart.append(replacement)
                        elif not slot.required:
                            normalized_chart.append(SlotAssignment(
                                slot_id=slot.slot_id, kind=slot.kind,
                                target_shape_ids=list(slot.target_shape_ids),
                                action=SlotAction.CLEAR,
                            ))
                    slide.assignments = normalized_chart
                    continue
            family = families.get(slide.template_family_id)
            if family is None:
                continue
            variant = next(
                (item for item in family.variants
                 if item.slide_number == slide.template_slide_number),
                None,
            )
            if variant is None:
                continue
            slide.template_class = family.slide_class
            assignments = {}
            for assignment in slide.assignments:
                assignments.setdefault(assignment.slot_id, assignment)
            normalized: list[SlotAssignment] = []
            for slot in variant.slots:
                assignment = assignments.get(slot.slot_id)
                if assignment is None:
                    if not slot.required:
                        normalized.append(SlotAssignment(
                            slot_id=slot.slot_id,
                            kind=slot.kind,
                            target_shape_ids=slot.target_shape_ids,
                            action=SlotAction.CLEAR,
                        ))
                    continue
                assignment.target_shape_ids = list(slot.target_shape_ids)
                if (
                    assignment.action in {SlotAction.KEEP, SlotAction.GENERATE}
                    and slot.kind != SlotKind.IMAGE
                ):
                    if not slot.required:
                        normalized.append(SlotAssignment(
                            slot_id=slot.slot_id,
                            kind=slot.kind,
                            target_shape_ids=list(slot.target_shape_ids),
                            action=SlotAction.CLEAR,
                        ))
                        continue
                if isinstance(assignment.content, VisualSlotContent):
                    asset = assets.get(assignment.content.asset_id)
                    if asset is None and not slot.required:
                        normalized.append(SlotAssignment(
                            slot_id=slot.slot_id,
                            kind=slot.kind,
                            target_shape_ids=slot.target_shape_ids,
                            action=SlotAction.CLEAR,
                        ))
                        continue
                    if asset is None:
                        normalized.append(assignment)
                        continue
                    try:
                        choice = choose_visual(
                            asset, slot.visual_capabilities,
                            assignment.content.selected_columns or None,
                        )
                    except StructuredAssetError:
                        choice = None
                    if choice is not None:
                        assignment.content.visual_kind, assignment.content.subtype = choice
                        if not assignment.content.selected_columns:
                            assignment.content.selected_columns = default_visual_columns(asset, *choice)
                normalized.append(assignment)
            slide.assignments = normalized
        return candidate

    @staticmethod
    def _visual_slots(catalog: TemplateCatalog):
        for family in catalog.families:
            for variant in family.variants:
                for slot in variant.slots:
                    if slot.kind.value == "visual" and slot.visual_capabilities is not None:
                        yield family, variant, slot

    def _asset_guidance(self, request: PlanningRequest, catalog: TemplateCatalog) -> list[dict[str, Any]]:
        guidance: list[dict[str, Any]] = []
        issues: list[ValidationIssue] = []
        slots = list(self._visual_slots(catalog))
        for index, asset in enumerate(request.structured_assets):
            if asset.visual_hint.mode == "none":
                continue
            compatible: list[dict[str, Any]] = []
            errors: list[str] = []
            try:
                unconstrained = choose_visual(asset)
            except StructuredAssetError as exc:
                unconstrained = None
                errors.append(str(exc))
            if isinstance(asset, DatasetAsset) and unconstrained and unconstrained[0] == "chart":
                selected = select_chart_canvas(catalog, unconstrained[1] or "column")
                if selected is None:
                    self._text_fallback_assets.add(asset.id)
                    self._runtime_warnings.append(
                        f"Набор {asset.id!r} изложен текстом: шаблон не поддерживает требуемую диаграмму."
                    )
                    continue
                family, variant, slot = selected
                columns = default_visual_columns(asset, *unconstrained)
                pages = len(paginate_asset(
                    asset, *unconstrained, slot.visual_capabilities,
                    max_slides=10_000, selected_columns=columns,
                ))
                compatible.append({
                    "family_id": family.family_id,
                    "slide_number": variant.slide_number,
                    "slot_id": slot.slot_id,
                    "visual_kind": "chart",
                    "subtype": unconstrained[1],
                    "selected_columns": columns,
                    "expected_pages": pages,
                })
                guidance.append({
                    "asset_id": asset.id,
                    "preferred": unconstrained,
                    "compatible_variants": compatible,
                    "instruction": "Create exactly one base slide and set chart_asset_id; write only its title.",
                })
                continue
            for family, variant, slot in slots:
                if slot.chart_canvas is not None:
                    continue
                try:
                    choice = choose_visual(asset, slot.visual_capabilities)
                except StructuredAssetError as exc:
                    errors.append(str(exc))
                    continue
                if choice is None:
                    continue
                columns = default_visual_columns(asset, *choice)
                try:
                    pages = len(paginate_asset(
                        asset, *choice, slot.visual_capabilities,
                        max_slides=10_000,
                        selected_columns=columns or None,
                    ))
                except StructuredAssetError as exc:
                    errors.append(str(exc))
                    continue
                compatible.append({
                    "family_id": family.family_id,
                    "slide_number": variant.slide_number,
                    "slot_id": slot.slot_id,
                    "visual_kind": choice[0],
                    "subtype": choice[1],
                    "selected_columns": columns,
                    "expected_pages": pages,
                })
            if not compatible:
                self._text_fallback_assets.add(asset.id)
                self._runtime_warnings.append(
                    f"Набор {asset.id!r} изложен текстом: в шаблоне нет совместимого visual-макета."
                )
            else:
                preferred = choose_visual(asset)
                preferred_variants = [
                    item for item in compatible
                    if (item["visual_kind"], item["subtype"]) == preferred
                ]
                if preferred_variants:
                    compatible = preferred_variants
                guidance.append({
                    "asset_id": asset.id,
                    "preferred": preferred,
                    "compatible_variants": compatible,
                })
        return guidance

    @staticmethod
    def _paginate_candidate(
        request: PlanningRequest, catalog: TemplateCatalog, candidate: PlanCandidate,
    ) -> PlanCandidate:
        families = {family.family_id: family for family in catalog.families}
        assets = {asset.id: asset for asset in request.structured_assets}
        result = candidate.model_copy(deep=True)
        chart_slide_counts: dict[str, int] = {}
        for item in result.slides:
            if item.chart_asset_id:
                chart_slide_counts[item.chart_asset_id] = chart_slide_counts.get(item.chart_asset_id, 0) + 1
        occurrences_by_asset: dict[str, list[tuple[SlotAssignment, Any]]] = {}
        for slide in result.slides:
            family = families.get(slide.template_family_id)
            variant = next(
                (
                    item for item in family.variants
                    if item.slide_number == slide.template_slide_number
                ),
                None,
            ) if family else None
            slots = {slot.slot_id: slot for slot in variant.slots} if variant else {}
            for assignment in slide.assignments:
                if not isinstance(assignment.content, VisualSlotContent):
                    continue
                slot = slots.get(assignment.slot_id)
                if slot is not None:
                    occurrences_by_asset.setdefault(
                        assignment.content.asset_id, [],
                    ).append((assignment, slot))

        # The model sometimes pre-expands an asset and may duplicate or reorder pages. When
        # enough compatible optional slots already exist, reuse them in slide order and clear
        # extras. This preserves every slide's non-visual content and avoids an LLM revision.
        for asset_id, occurrences in occurrences_by_asset.items():
            if len(occurrences) < 2:
                continue
            # Chart slides have an explicit base-slide identity. Multiple base
            # slides must remain visible to validation as reuse; only an
            # already-paginated chart (distinct page numbers) is idempotent.
            if asset_id in chart_slide_counts:
                continue
            asset = assets.get(asset_id)
            if asset is None:
                continue
            first, first_slot = occurrences[0]
            selected_columns = first.content.selected_columns or None
            if isinstance(asset, DatasetAsset):
                selected_set = {
                    key
                    for assignment, _ in occurrences
                    for key in assignment.content.selected_columns
                }
                selected_columns = [
                    column.key for column in asset.columns if column.key in selected_set
                ] or None
            pages = paginate_asset(
                asset,
                first.content.visual_kind,
                first.content.subtype,
                first_slot.visual_capabilities,
                max_slides=10_000,
                selected_columns=selected_columns,
            )
            if len(occurrences) < len(pages):
                continue
            used = occurrences[:len(pages)]
            extras = occurrences[len(pages):]
            compatible = all(
                page.visual_kind in slot.visual_capabilities.kinds
                and (
                    not page.subtype
                    or not slot.visual_capabilities.subtypes
                    or page.subtype in slot.visual_capabilities.subtypes
                )
                for page, (_, slot) in zip(pages, used)
                if slot.visual_capabilities is not None
            )
            if not compatible or any(slot.required for _, slot in extras):
                continue
            for page, (assignment, _) in zip(pages, used):
                assignment.content = page
            for assignment, _ in extras:
                assignment.action = SlotAction.CLEAR
                assignment.source_refs = []
                assignment.content = None

        index = 0
        while index < len(result.slides):
            slide = result.slides[index]
            visual_assignments = [
                assignment for assignment in slide.assignments
                if isinstance(assignment.content, VisualSlotContent)
            ]
            if not visual_assignments:
                index += 1
                continue
            if len(visual_assignments) > 1:
                index += 1
                continue
            assignment = visual_assignments[0]
            occurrences = sum(
                1 for item in result.slides for other in item.assignments
                if isinstance(other.content, VisualSlotContent)
                and other.content.asset_id == assignment.content.asset_id
            )
            # A lone occurrence is always the base visualization, even if the model assigned
            # an arbitrary page number. Already-expanded plans are left untouched.
            if occurrences > 1:
                index += 1
                continue
            asset = assets.get(assignment.content.asset_id)
            family = families.get(slide.template_family_id)
            variant = next((item for item in family.variants if item.slide_number == slide.template_slide_number), None) if family else None
            slot = next((item for item in variant.slots if item.slot_id == assignment.slot_id), None) if variant else None
            if asset is None or slot is None:
                index += 1
                continue
            pages = paginate_asset(
                asset,
                assignment.content.visual_kind,
                assignment.content.subtype,
                slot.visual_capabilities,
                max_slides=10_000,
                selected_columns=assignment.content.selected_columns or None,
            )
            required = len(result.slides) + len(pages) - 1
            if required > request.slide_count.max:
                issue = ValidationIssue(
                    code="content_overflow", path=f"structured_assets[{asset.id}]",
                    message=f"visual pagination requires {required} slides; maximum is {request.slide_count.max}",
                )
                raise PlannerFailure(
                    "content_overflow", "structured data does not fit the requested slide range",
                    {
                        "validation_errors": [issue.render()],
                        "validation_issues": [issue.as_dict()],
                        "required_slides": required,
                    },
                )
            clones = []
            for page in pages:
                clone = slide.model_copy(deep=True)
                target = next(item for item in clone.assignments if item.slot_id == assignment.slot_id)
                target.content = page
                if page.page > 1 and clone.chart_asset_id:
                    ContentPlanner._mark_chart_continuation(clone)
                clones.append(clone)
            result.slides[index:index + 1] = clones
            index += len(clones)
        for number, slide in enumerate(result.slides, 1):
            slide.number = number
            if slide.chart_asset_id and any(
                isinstance(assignment.content, VisualSlotContent)
                and assignment.content.visual_kind == "chart"
                and assignment.content.page > 1
                for assignment in slide.assignments
            ):
                ContentPlanner._mark_chart_continuation(slide)
        return result

    @staticmethod
    def _mark_chart_continuation(slide: Any) -> None:
        suffix = " (продолжение)"
        for assignment in slide.assignments:
            content = assignment.content
            if not isinstance(content, TextSlotContent):
                continue
            if content.text:
                if not content.text.endswith(suffix):
                    content.text += suffix
                return
            if content.fields:
                key = next((name for name in content.fields if "title" in name.casefold()), None)
                key = key or next(iter(content.fields))
                if not content.fields[key].endswith(suffix):
                    content.fields[key] += suffix
                return

    def _catalog(self) -> TemplateCatalog:
        if self.catalog is not None:
            return self.catalog
        if self.catalog_builder is not None:
            return self.catalog_builder.build()
        if self.template_dir is None:
            raise PlannerFailure("invalid_input", "template_dir is required when catalog is not injected")
        try:
            return TemplateCatalogBuilder(
                self.template_dir, llm=self.llm, prompt_dir=self.prompt_dir
            ).build()
        except Exception as exc:
            raise PlannerFailure("catalog_build_failed", str(exc)) from exc

    def _analyze(
        self, request: PlanningRequest, sources: list[SourceChunk],
        asset_guidance: list[dict[str, Any]] | None = None,
    ) -> ContentAnalysis:
        payload = {
            "slide_count": request.slide_count.model_dump(mode="json"),
            "sources": [source.model_dump(mode="json") for source in sources],
        }
        schema = self._dynamic_schema(
            ContentAnalysis, sources=[source.id for source in sources],
        )
        material_count = max(1, len(sources))
        properties = schema.get("properties", {})
        properties.get("facts", {})["maxItems"] = material_count
        properties.get("narrative", {})["maxItems"] = request.slide_count.max
        properties.get("visual_opportunities", {})["maxItems"] = min(
            material_count, request.slide_count.max,
        )
        recommended = properties.get("recommended_slide_count", {})
        recommended["minimum"] = request.slide_count.min
        recommended["maximum"] = request.slide_count.max
        if not self.allow_model_assets:
            properties.get("visual_candidates", {})["maxItems"] = 0

        instruction = self.analysis_prompt
        if not self.allow_model_assets:
            model_asset_instruction = (
                "When prose or PDF text explicitly contains a complete small dataset or diagram, "
                "you may also emit a normalized visual_candidate. It must cite source_refs, "
                "preserve source order, and copy every label and value exactly. Never calculate, "
                "aggregate, interpolate, sort, or infer missing values. Do not duplicate structured "
                "assets already present in the request."
            )
            data_policy = (
                "DATA POLICY: Return visual_candidates exactly as []. Do not create, "
                "reconstruct, or normalize tables, charts, diagrams, or datasets. Structured "
                "visuals may only come from assets already extracted from uploaded files."
            )
            instruction = instruction.replace(model_asset_instruction, data_policy)
            if data_policy not in instruction:
                instruction += f"\n\n{data_policy}"

        def analysis_errors(value: ContentAnalysis) -> list[str]:
            errors = validate_analysis(request, sources, value)
            if not self.allow_model_assets and value.visual_candidates:
                errors.insert(0, "visual_candidates: must be an empty array")
            bounded_arrays = (
                ("facts", len(value.facts), material_count),
                ("narrative", len(value.narrative), request.slide_count.max),
                (
                    "visual_opportunities", len(value.visual_opportunities),
                    min(material_count, request.slide_count.max),
                ),
            )
            errors.extend(
                f"{name}: contains {actual} items; maximum is {maximum}"
                for name, actual, maximum in bounded_arrays
                if actual > maximum
            )
            return errors

        for attempt in range(2):
            retry_instruction = instruction
            if attempt:
                retry_instruction += (
                    "\n\nRETRY: Return one concise JSON object only. Obey every array limit "
                    "in the schema and include no commentary, markdown, or extra fields."
                )
            try:
                analysis = self._complete_model(
                    self._request(retry_instruction, ContentAnalysis, {
                        "PLANNING INPUT": payload,
                        "STRUCTURED ASSET VISUAL GUIDANCE": asset_guidance or [],
                    }),
                    ContentAnalysis,
                    "content analysis",
                    schema=schema,
                )
                source_map = {source.id: source.text for source in sources}
                constraint_pattern = re.compile(
                    r"(?:ровно|exactly).{0,30}(?:слайд|slide)|"
                    r"(?:презентац|presentation).{0,30}(?:должн|must).{0,30}(?:состо|contain)|"
                    r"(?:не добавлять|do not add).{0,30}(?:слайд|slide|roadmap|appendix)",
                    re.IGNORECASE,
                )
                for fact in analysis.facts:
                    referenced = "\n".join(source_map.get(ref, "") for ref in fact.source_refs)
                    if constraint_pattern.search(f"{fact.text}\n{referenced}"):
                        fact.fact_type = "constraint"
                errors = analysis_errors(analysis)
                if errors and not self._supports_native_schema():
                    analysis = self._repair_model(
                        analysis.model_dump_json(), errors, ContentAnalysis, "content analysis",
                    )
                    errors = analysis_errors(analysis)
                if errors:
                    raise PlannerFailure(
                        "invalid_llm_response",
                        "content analysis violates planning constraints",
                        {"validation_errors": errors},
                    )
                return analysis
            except PlannerFailure as exc:
                if exc.code != "invalid_llm_response" or attempt:
                    raise
                logger.warning(
                    "planner_retry job=%s stage=content_analysis attempt=2 reason=%s",
                    self.job_id or "-",
                    _safe_error_summary(exc.details.get("validation_errors", [])),
                )

        # The loop either returns a valid model or raises on its second attempt.
        raise AssertionError("unreachable content analysis retry state")

    def _draft(
        self,
        request: PlanningRequest,
        sources: list[SourceChunk],
        analysis: ContentAnalysis,
        catalog: TemplateCatalog,
        asset_guidance: list[dict[str, Any]] | None = None,
        *,
        focus: str | None = None,
    ) -> PlanCandidate:
        if self._supports_native_schema():
            schema = self._dynamic_schema(
                NarrativePlanCandidate,
                sources=[source.id for source in sources],
                assets=[asset.id for asset in request.structured_assets],
                **self._catalog_selection_enums(catalog),
            )
            base_prompt = self._request(self.draft_prompt, NarrativePlanCandidate, {
                    "REQUEST": request.model_dump(mode="json"),
                    "SOURCES": [source.model_dump(mode="json") for source in sources],
                    "CONTENT ANALYSIS": analysis.model_dump(mode="json"),
                    "TEMPLATE CATALOG": catalog_prompt_projection(catalog),
                    "STRUCTURED ASSET VISUAL GUIDANCE": asset_guidance or [],
                    "CANDIDATE EMPHASIS": focus or "Balanced presentation quality.",
                })
            retry_errors: list[str] = []
            for attempt in range(2):
                prompt = base_prompt
                if attempt:
                    prompt += (
                        "\n\nRETRY: Return a complete corrected plan. Fix every validation "
                        "error below without weakening replace content or source_refs.\n"
                        + json.dumps(retry_errors, ensure_ascii=False)
                    )
                try:
                    narrative = self._complete_model(
                        prompt, NarrativePlanCandidate, "plan draft", schema=schema,
                    )
                    return self._expand_narrative_plan(narrative, catalog)
                except ValidationError as exc:
                    retry_errors = _validation_errors(exc)
                    failure = PlannerFailure(
                        "invalid_llm_response", "draft content violates slot action rules",
                        {"validation_errors": retry_errors},
                    )
                except PlannerFailure as exc:
                    if exc.code != "invalid_llm_response":
                        raise
                    retry_errors = list(exc.details.get("validation_errors", []))
                    failure = exc
                if attempt:
                    raise failure
                logger.warning(
                    "planner_retry job=%s stage=plan_draft attempt=2 reason=%s",
                    self.job_id or "-", _safe_error_summary(retry_errors),
                )
            raise AssertionError("unreachable plan draft retry state")
        return self._complete_model(
            self._request(self.draft_prompt, PlanCandidate, {
                "REQUEST": request.model_dump(mode="json"),
                "SOURCES": [source.model_dump(mode="json") for source in sources],
                "CONTENT ANALYSIS": analysis.model_dump(mode="json"),
                "TEMPLATE CATALOG": catalog_prompt_projection(catalog),
                "STRUCTURED ASSET VISUAL GUIDANCE": asset_guidance or [],
                "CANDIDATE EMPHASIS": focus or "Balanced presentation quality.",
            }),
            PlanCandidate,
            "plan draft",
            schema=self._dynamic_schema(
                PlanCandidate,
                sources=[source.id for source in sources],
                assets=[asset.id for asset in request.structured_assets],
                **self._catalog_selection_enums(catalog),
            ),
        )

    def _critique(
        self,
        request: PlanningRequest,
        sources: list[SourceChunk],
        analysis: ContentAnalysis,
        catalog: TemplateCatalog,
        candidate: PlanCandidate,
        *,
        profile: str = "general",
        instruction: str = "Evaluate every quality dimension.",
    ) -> PlanCritique:
        return self._complete_model(
            self._request(self.critique_prompt, PlanCritique, {
                "REQUEST": request.model_dump(mode="json"),
                "SOURCES": [source.model_dump(mode="json") for source in sources],
                "CONTENT ANALYSIS": analysis.model_dump(mode="json"),
                "SELECTED TEMPLATE CATALOG": self._selected_catalog(catalog, candidate),
                "PLAN": candidate.model_dump(mode="json"),
                "CRITIC PROFILE": {"name": profile, "instruction": instruction},
            }),
            PlanCritique,
            "plan critique",
            schema=self._dynamic_schema(PlanCritique),
        )

    def _revise(
        self,
        request: PlanningRequest,
        sources: list[SourceChunk],
        analysis: ContentAnalysis,
        catalog: TemplateCatalog,
        candidate: PlanCandidate,
        issues: list[dict[str, Any]],
        asset_guidance: list[dict[str, Any]] | None = None,
    ) -> PlanCandidate:
        if self._supports_native_schema():
            slide_index = self._issue_slide_index(issues, len(candidate.slides))
            current = candidate.slides[slide_index]
            narrative = self._complete_model(
                self._request(self.revision_prompt, NarrativeSlide, {
                    "REQUEST": request.model_dump(mode="json"),
                    "SOURCES": [source.model_dump(mode="json") for source in sources],
                    "CONTENT ANALYSIS": analysis.model_dump(mode="json"),
                    "SELECTED TEMPLATE CATALOG": self._selected_catalog(catalog, candidate),
                    "CURRENT SLIDE": current.model_dump(mode="json"),
                    "ISSUES TO FIX": issues,
                    "STRUCTURED ASSET VISUAL GUIDANCE": asset_guidance or [],
                    "INSTRUCTION": "Return only the corrected CURRENT SLIDE; do not rewrite other slides.",
                }),
                NarrativeSlide,
                "slide repair",
                schema=self._dynamic_schema(
                    NarrativeSlide,
                    sources=[source.id for source in sources],
                    assets=[asset.id for asset in request.structured_assets],
                    **self._catalog_selection_enums(catalog),
                ),
            )
            try:
                repaired = self._expand_narrative_slide(narrative, catalog, current.number)
            except ValidationError as exc:
                raise PlannerFailure(
                    "invalid_llm_response", "slide repair violates slot action rules",
                    {"validation_errors": _validation_errors(exc)},
                ) from exc
            result = candidate.model_copy(deep=True)
            repaired.number = current.number
            result.slides[slide_index] = repaired
            return result
        return self._complete_model(
            self._request(self.revision_prompt, PlanCandidate, {
                "REQUEST": request.model_dump(mode="json"),
                "SOURCES": [source.model_dump(mode="json") for source in sources],
                "CONTENT ANALYSIS": analysis.model_dump(mode="json"),
                "TEMPLATE CATALOG": catalog_prompt_projection(catalog),
                "CURRENT PLAN": candidate.model_dump(mode="json"),
                "ISSUES TO FIX": issues,
                "STRUCTURED ASSET VISUAL GUIDANCE": asset_guidance or [],
            }),
            PlanCandidate,
            "plan revision",
            schema=self._dynamic_schema(
                PlanCandidate,
                sources=[source.id for source in sources],
                assets=[asset.id for asset in request.structured_assets],
                **self._catalog_selection_enums(catalog),
            ),
        )

    def _revise_plan(
        self,
        request: PlanningRequest,
        sources: list[SourceChunk],
        analysis: ContentAnalysis,
        catalog: TemplateCatalog,
        candidate: PlanCandidate,
        issues: list[dict[str, Any]],
        asset_guidance: list[dict[str, Any]] | None = None,
        *,
        stage: str = "plan revision",
    ) -> PlanCandidate:
        """Revise deck structure as one operation, never mixed with slide repairs."""
        if not self._supports_native_schema():
            return self._revise(
                request, sources, analysis, catalog, candidate, issues, asset_guidance,
            )
        narrative = self._complete_model(
            self._request(self.revision_prompt, NarrativePlanCandidate, {
                "REQUEST": request.model_dump(mode="json"),
                "SOURCES": [source.model_dump(mode="json") for source in sources],
                "CONTENT ANALYSIS": analysis.model_dump(mode="json"),
                "TEMPLATE CATALOG": catalog_prompt_projection(catalog),
                "CURRENT PLAN": candidate.model_dump(mode="json"),
                "ISSUES TO FIX": issues,
                "STRUCTURED ASSET VISUAL GUIDANCE": asset_guidance or [],
                "INSTRUCTION": "Return the complete corrected plan because these are global or structural issues.",
            }),
            NarrativePlanCandidate,
            stage,
            schema=self._dynamic_schema(
                NarrativePlanCandidate,
                sources=[source.id for source in sources],
                assets=[asset.id for asset in request.structured_assets],
                **self._catalog_selection_enums(catalog),
            ),
        )
        try:
            return self._expand_narrative_plan(narrative, catalog)
        except ValidationError as exc:
            raise PlannerFailure(
                "invalid_llm_response", "plan revision violates slot action rules",
                {"validation_errors": _validation_errors(exc)},
            ) from exc

    def _revise_slides_parallel(
        self,
        request: PlanningRequest,
        sources: list[SourceChunk],
        analysis: ContentAnalysis,
        catalog: TemplateCatalog,
        candidate: PlanCandidate,
        issues: list[dict[str, Any]],
        asset_guidance: list[dict[str, Any]] | None,
        round_number: int,
    ) -> PlanCandidate:
        grouped: dict[int, list[dict[str, Any]]] = {}
        for issue in issues:
            number = issue.get("slide_number")
            if not isinstance(number, int):
                match = re.search(r"slides\[(\d+)\]", str(issue.get("path") or ""))
                if match:
                    number = int(match.group(1)) + 1
            if isinstance(number, int) and 1 <= number <= len(candidate.slides):
                grouped.setdefault(number, []).append(issue)
        if not grouped:
            return candidate

        tasks: dict[str, Callable[[], PlanCandidate]] = {}
        completed: dict[int, PlanCandidate] = {}
        for number, slide_issues in grouped.items():
            checkpoint = f"slide_revision_r{round_number}_s{number}"
            cached = self._load_checkpoint(checkpoint, PlanCandidate)
            if cached is not None:
                completed[number] = cached
                continue
            tasks[str(number)] = lambda slide_issues=slide_issues: self._revise(
                request, sources, analysis, catalog, candidate, slide_issues, asset_guidance,
            )
        for key, value in self._run_wave(f"slide_repairs_{round_number}", tasks).items():
            if isinstance(value, PlanCandidate):
                number = int(key)
                completed[number] = value
                self._save_checkpoint(f"slide_revision_r{round_number}_s{number}", value)
        if len(completed) != len(grouped):
            raise PlannerFailure("llm_api_error", "one or more slide repairs failed")
        result = candidate.model_copy(deep=True)
        for number in sorted(completed):
            repaired_plan = completed[number]
            if len(repaired_plan.slides) != len(candidate.slides):
                raise PlannerFailure("invalid_llm_response", "slide repair changed plan length")
            result.slides[number - 1] = repaired_plan.slides[number - 1]
        return result

    @staticmethod
    def _expand_narrative_slide(
        narrative: NarrativeSlide, catalog: TemplateCatalog, number: int,
    ) -> SlidePlan:
        family = next(
            (item for item in catalog.families if item.family_id == narrative.template_family_id),
            None,
        )
        variant = next(
            (item for item in family.variants if item.slide_number == narrative.template_slide_number),
            None,
        ) if family else None
        if family is not None and variant is None:
            # The response schema can enumerate valid family IDs and valid
            # slide numbers, but JSON Schema cannot cheaply express their
            # catalog pairing. Slide numbers are globally unique in analyzed
            # presentations, so repair an unambiguous crossed pair here.
            matches = [
                (candidate_family, candidate_variant)
                for candidate_family in catalog.families
                for candidate_variant in candidate_family.variants
                if candidate_variant.slide_number == narrative.template_slide_number
            ]
            if len(matches) == 1:
                family, variant = matches[0]
        slots = {slot.slot_id: slot for slot in variant.slots} if variant else {}
        assignments: list[dict[str, Any]] = []
        for assignment in narrative.assignments:
            slot = slots.get(assignment.slot_id)
            content = assignment.content
            content_kind = SlotKind(
                content.kind if content is not None and content.kind in SlotKind._value2member_map_
                else "text"
            )
            if (
                slot is not None
                and content is not None
                and content_kind != slot.kind
                and not slot.required
            ):
                assignments.append({
                    "slot_id": assignment.slot_id,
                    "kind": slot.kind,
                    "target_shape_ids": list(slot.target_shape_ids),
                    "action": SlotAction.CLEAR,
                    "source_refs": [],
                    "content": None,
                })
                continue
            # Preserve a required-slot mismatch as a structurally invalid but
            # parseable assignment. The validator can then report it to the
            # normal candidate repair pass instead of discarding the draft.
            inferred_kind = (
                content_kind
                if slot is not None and content is not None and content_kind != slot.kind
                else slot.kind if slot else content_kind
            )
            assignments.append({
                "slot_id": assignment.slot_id,
                "kind": inferred_kind,
                "target_shape_ids": list(slot.target_shape_ids) if slot else [],
                "action": assignment.action,
                "source_refs": assignment.source_refs,
                "content": content,
            })
        return SlidePlan(
            number=number,
            purpose=narrative.purpose,
            template_family_id=family.family_id if family else narrative.template_family_id,
            template_slide_number=narrative.template_slide_number,
            template_class=family.slide_class if family else SlideClass.CONTENT_TEXT,
            chart_asset_id=narrative.chart_asset_id,
            assignments=assignments,
        )

    @classmethod
    def _expand_narrative_plan(
        cls, narrative: NarrativePlanCandidate, catalog: TemplateCatalog,
    ) -> PlanCandidate:
        return PlanCandidate(
            deck=narrative.deck,
            slides=[
                cls._expand_narrative_slide(slide, catalog, index)
                for index, slide in enumerate(narrative.slides, 1)
            ],
        )

    @staticmethod
    def _issue_slide_index(issues: list[dict[str, Any]], count: int) -> int:
        for issue in issues:
            path = str(issue.get("path") or "")
            match = re.search(r"slides\[(\d+)\]", path)
            if match:
                return min(int(match.group(1)), count - 1)
            number = issue.get("slide_number")
            if isinstance(number, int) and 1 <= number <= count:
                return number - 1
        return count - 1

    @staticmethod
    def _selected_catalog(catalog: TemplateCatalog, candidate: PlanCandidate) -> dict[str, Any]:
        selected = {(slide.template_family_id, slide.template_slide_number) for slide in candidate.slides}
        projection = catalog_prompt_projection(catalog)
        for family in projection["families"]:
            family["variants"] = [
                variant for variant in family["variants"]
                if (family["family_id"], variant["slide_number"]) in selected
            ]
        projection["families"] = [family for family in projection["families"] if family["variants"]]
        return projection

    @staticmethod
    def _catalog_selection_enums(catalog: TemplateCatalog) -> dict[str, list[Any]]:
        editable = [
            (family, variant)
            for family in catalog.families
            for variant in family.variants
            if variant.slots
        ]
        return {
            "families": list(dict.fromkeys(family.family_id for family, _ in editable)),
            "variants": list(dict.fromkeys(variant.slide_number for _, variant in editable)),
            "slots": list(dict.fromkeys(
                slot.slot_id for _, variant in editable for slot in variant.slots
            )),
        }

    @staticmethod
    def _request(instruction: str, model: type[BaseModel], sections: dict[str, Any]) -> str:
        parts = [instruction]
        for label, payload in sections.items():
            parts.append(
                f"{label}:\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
            )
        return "\n\n".join(parts)

    @staticmethod
    def _dynamic_schema(
        model: type[BaseModel], *, sources: list[str] | None = None,
        assets: list[str] | None = None, families: list[str] | None = None,
        variants: list[int] | None = None, slots: list[str] | None = None,
    ) -> dict[str, Any]:
        schema = model.model_json_schema()
        enum_by_name: dict[str, list[Any]] = {}
        if sources:
            enum_by_name["source_refs"] = sources
        if assets:
            enum_by_name["asset_id"] = assets
            enum_by_name["chart_asset_id"] = assets
        if families:
            enum_by_name["template_family_id"] = families
        if variants:
            enum_by_name["template_slide_number"] = variants
        if slots:
            enum_by_name["slot_id"] = slots

        def visit(node: Any) -> None:
            if isinstance(node, dict):
                properties = node.get("properties", {})
                for name, child in properties.items():
                    values = enum_by_name.get(name)
                    if values:
                        target = child.get("items") if name == "source_refs" else child
                        if isinstance(target, dict):
                            branches = target.get("anyOf")
                            if isinstance(branches, list):
                                string_branch = next(
                                    (
                                        branch for branch in branches
                                        if isinstance(branch, dict)
                                        and branch.get("type") == "string"
                                    ),
                                    None,
                                )
                                if string_branch is not None:
                                    string_branch["enum"] = values
                                else:
                                    target["enum"] = values
                            else:
                                target["enum"] = values
                    visit(child)
                for key, child in node.items():
                    if key != "properties":
                        visit(child)
            elif isinstance(node, list):
                for child in node:
                    visit(child)

        visit(schema)
        return schema

    def _complete_model(
        self, prompt: str, model: type[ModelT], stage: str,
        *, schema: dict[str, Any] | None = None,
    ) -> ModelT:
        task_deadline = getattr(self._task_state, "deadline", None)
        deadlines = [value for value in (self._deadline, task_deadline) if value is not None]
        effective_deadline = min(deadlines) if deadlines else None
        if effective_deadline is not None and time.monotonic() >= effective_deadline:
            raise PlannerFailure("planning_budget_exhausted", "planning budget exhausted")
        accepts_schema = self._accepts_schema()
        uses_native_schema = self._supports_native_schema()
        try:
            if accepts_schema:
                raw = self.llm.complete(
                    prompt,
                    json_schema=schema or model.model_json_schema(),
                    schema_name=stage,
                    stage=stage,
                    job_id=self.job_id,
                    deadline=effective_deadline,
                )
            else:
                raw = self.llm.complete(prompt)
        except Exception as exc:
            if effective_deadline is not None and time.monotonic() >= effective_deadline:
                raise PlannerFailure("planning_budget_exhausted", "planning budget exhausted") from exc
            category, _, _ = _error_category(exc)
            if category in {"http_401", "http_403"}:
                code = "provider_permission"
            elif category == "rate_limit":
                code = "provider_rate_limit"
            elif category == "timeout":
                code = "provider_timeout"
            elif category in {"server", "connection", "incomplete", "empty_response"}:
                code = "provider_unavailable"
            else:
                code = "provider_error"
            raise PlannerFailure(
                code, f"LLM failed during {stage}", {"error_category": category},
            ) from exc
        try:
            decoded = json.loads(_clean_json(raw))
            return model.model_validate(_normalize_assignment_actions(decoded))
        except (ValidationError, ValueError) as exc:
            errors = _validation_errors(exc) if isinstance(exc, ValidationError) else [str(exc)]
            codes = _validation_codes(exc) if isinstance(exc, ValidationError) else ["invalid_json"]
            logger.warning(
                "llm_schema_error job=%s stage=%s validation_codes=%s summary=%s",
                self.job_id or "-", stage, ",".join(codes), _safe_error_summary(errors),
            )
            if uses_native_schema:
                raise PlannerFailure(
                    "invalid_llm_response",
                    f"structured response was invalid during {stage}",
                    {"validation_errors": errors},
                ) from exc
            return self._repair_model(raw, errors, model, stage)

    def _supports_native_schema(self) -> bool:
        if getattr(self.llm, "structured_output", "native") != "native":
            return False
        return self._accepts_schema()

    def _accepts_schema(self) -> bool:
        parameters = inspect.signature(self.llm.complete).parameters
        return "json_schema" in parameters or any(
            item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()
        )

    def _repair_model(
        self,
        raw: str,
        errors: list[str],
        model: type[ModelT],
        stage: str,
    ) -> ModelT:
        schema = json.dumps(model.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
        prompt = (
            f"{self.repair_prompt}\n\nOUTPUT JSON SCHEMA:\n{schema}\n\n"
            f"INVALID RESPONSE:\n{raw}\n\nVALIDATION ERRORS:\n"
            f"{json.dumps(errors, ensure_ascii=False)}"
        )
        try:
            repaired = self.llm.complete(prompt)
            decoded = json.loads(_clean_json(repaired))
            return model.model_validate(_normalize_assignment_actions(decoded))
        except Exception as exc:
            raise PlannerFailure(
                "invalid_llm_response",
                f"invalid LLM response during {stage} after one repair",
                {"validation_errors": errors},
            ) from exc
