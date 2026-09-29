from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pydantic import ValidationError
from PIL import Image, ImageDraw
from sqlalchemy import select

from content_planner.catalog import CatalogBuildError, TemplateCatalogBuilder
from content_planner.client import ResponseIncompleteError
from content_planner.compiler import compile_slide
from content_planner.models import ImageSlotContent, PlanningRequest, ProvidedImage, SlotAction, StructuredAsset, TemplateCatalog, TextSlotContent, VisualSlotContent
from content_planner.planner import ContentPlanner, PlannerFailure
from presentation_builder import PresentationBuilder
from template_model import TemplateModel
from template_model.models import PresentationManifest, SlideMetadata, SlideVLMAnalysis, VLMStatus
from template_model.structures import detect_structures
from template_model.vlm import VLMAnalyzer

from .config import settings
from .database import SessionLocal
from .extractors import IMAGE_EXTENSIONS, extract_document, extract_structured_assets
from .models import Job, JobEvent, JobFile, Template
from .storage import from_data, relative_to_data


logger = logging.getLogger("webapp.worker")


class JobCanceled(Exception):
    """Raised at cooperative checkpoints after a user cancels a job."""


NON_FALLBACK_PLANNER_ERRORS = {
    "invalid_input",
    "content_overflow",
    "incompatible_template",
    "forced_visual_incompatible",
    "visual_slot_unavailable",
    "missing_input_asset",
    "outline_generation_failed",
    "slide_generation_failed",
    "planning_budget_exhausted",
    "provider_permission",
    "provider_rate_limit",
    "provider_timeout",
    "provider_unavailable",
    "provider_error",
}


def _should_use_fallback(exc: PlannerFailure, generation_mode: str = "reliable") -> bool:
    """Fallback is an explicitly fast-mode-only degraded delivery path."""
    if generation_mode not in {"reliable", "fast"}:
        return False
    # llm_api_error is retained only for third-party/legacy planner clients.
    # The production client emits the specific provider_* categories above.
    if exc.code in {"invalid_llm_response", "quality_gate_failed", "llm_api_error"}:
        return True
    if exc.code == "slide_generation_failed":
        causes = [
            code for values in (exc.details.get("cause_codes") or {}).values()
            for code in values
        ]
        return bool(causes) and set(causes) == {"invalid_llm_response"}
    return False


def _image_source_description(original_name: str, asset_ref: str) -> str:
    """Expose the human filename while keeping the stored name as the asset ref."""
    return (
        f"Доступное изображение: {asset_ref} "
        f"(исходное имя файла: {Path(original_name).name})"
    )


def _provided_images(files: list[JobFile]) -> list[ProvidedImage]:
    images: list[ProvidedImage] = []
    for item in files:
        if Path(item.original_name).suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        path = from_data(item.relative_path)
        if path.parent.name != "assets":
            raise ValueError("Загруженное изображение находится вне каталога assets задания")
        images.append(ProvidedImage(
            id=f"image:{item.id}",
            original_name=Path(item.original_name).name,
            asset_ref=path.name,
        ))
    return images


def _materialize_missing_image_placeholders(
    request: PlanningRequest, assets_dir: Path,
) -> list[str]:
    """Register neutral local images for filenames explicitly named in the brief."""
    _, missing = ContentPlanner._explicit_asset_references(request)
    if not missing:
        return []
    assets_dir.mkdir(parents=True, exist_ok=True)
    for original_name in missing:
        digest = hashlib.sha256(original_name.encode("utf-8")).hexdigest()[:12]
        asset_ref = f"placeholder-{digest}.png"
        target = assets_dir / asset_ref
        image = Image.new("RGB", (1200, 800), "#E9EDF2")
        draw = ImageDraw.Draw(image)
        draw.rectangle((70, 70, 1130, 730), outline="#AAB4C0", width=5)
        draw.line((420, 500, 560, 350, 690, 475, 800, 300), fill="#AAB4C0", width=10)
        image.save(target, format="PNG")
        request.provided_images.append(ProvidedImage(
            id=f"image:placeholder:{digest}",
            original_name=Path(original_name).name,
            asset_ref=asset_ref,
        ))
        request.content_package += "\n\n" + _image_source_description(original_name, asset_ref)
    return [
        "Для отсутствующих изображений созданы нейтральные placeholders: "
        + ", ".join(missing)
    ]


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _public_error(exc: Exception) -> str:
    if isinstance(exc, PlannerFailure):
        summaries = {
            "visual_slot_unavailable": "В шаблоне нет подходящего контейнера для визуализации данных.",
            "forced_visual_incompatible": "Заданный тип визуализации не поддерживается выбранным шаблоном.",
            "content_overflow": "Данные не помещаются в заданный диапазон слайдов.",
            "quality_gate_failed": "План не прошёл проверку структуры и полноты данных.",
            "render_compilation_failed": "Не удалось подготовить визуализацию для сборки презентации.",
            "invalid_structured_assets": "Табличные данные имеют некорректную структуру.",
            "missing_input_asset": "Не найдены явно указанные входные данные или изображения.",
            "outline_generation_failed": "Модель не смогла построить структуру презентации.",
            "slide_generation_failed": "Модель не смогла подготовить один или несколько слайдов.",
            "planning_budget_exhausted": "Превышен бюджет времени планирования.",
            "provider_permission": "Нет разрешения на вызов модели. Проверьте роль сервисного аккаунта.",
            "provider_rate_limit": "Лимит запросов к модели временно исчерпан. Повторите позже.",
            "provider_timeout": "Модель не ответила за отведённое время.",
            "provider_unavailable": "Сервис модели временно недоступен.",
            "provider_error": "Провайдер модели отклонил запрос.",
            "invalid_llm_response": "Модель вернула ответ в неподдерживаемом формате.",
            "quality_evaluation_failed": "Не удалось получить корректную оценку качества презентации.",
            "incompatible_template": "Материалы несовместимы с доступными макетами шаблона.",
        }
        return summaries.get(exc.code, "Не удалось построить проверенный план презентации.")
    if isinstance(exc, (CatalogBuildError, ValidationError)):
        return "Не удалось завершить анализ шаблона. Повторите анализ шаблона."
    cause: BaseException | None = exc
    seen_causes: set[int] = set()
    while cause is not None and id(cause) not in seen_causes:
        seen_causes.add(id(cause))
        if isinstance(cause, ResponseIncompleteError):
            return (
                "Модель не смогла завершить анализ части шаблона из-за ограничения "
                "размера ответа. Повторите анализ шаблона."
            )
        cause = cause.__cause__
    message = str(exc)[:1000] or type(exc).__name__
    for private_root, label in (
        (str(settings.data_root.resolve()), "[storage]"),
        (str(Path.home()), "[home]"),
        (str(Path.cwd().resolve()), "[app]"),
    ):
        message = message.replace(private_root, label)
    return message


def _safe_validation_issues(exc: Exception) -> list[dict[str, str]]:
    if not isinstance(exc, PlannerFailure):
        return []
    root = str(settings.data_root.resolve())
    issues: list[dict[str, str]] = []
    for raw in exc.validation_issues:
        if not isinstance(raw, dict):
            continue
        issues.append({
            "code": str(raw.get("code") or "validation_error")[:100],
            "path": str(raw.get("path") or "").replace(root, "[storage]")[:500],
            "message": str(raw.get("message") or "").replace(root, "[storage]")[:1000],
        })
    return issues


def transition(job_id: str, stage: str, progress: int, message: str, *, status: str = "processing") -> None:
    with SessionLocal() as db:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job:
            return
        if job.status == "canceled":
            raise JobCanceled(job_id)
        job.stage, job.progress, job.status = stage, progress, status
        db.add(JobEvent(job_id=job.id, stage=stage, progress=progress, message=message))
        db.commit()


def _fail(job_id: str, exc: Exception, *, preserve_template: bool = False) -> None:
    message = _public_error(exc)
    validation_issues = _safe_validation_issues(exc)
    with SessionLocal() as db:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job:
            return
        if job.status == "canceled":
            return
        job.status, job.stage, job.error = "failed", "failed", message
        job.warnings = validation_issues
        if job.type == "template_analysis" and job.template_id:
            template = db.get(Template, job.template_id)
            if template:
                model_dir = from_data(template.model_path) if template.model_path else None
                template.status = "ready" if preserve_template and model_dir and model_dir.is_dir() else "failed"
                template.error = message
        db.add(JobEvent(job_id=job.id, stage="failed", progress=job.progress, message="Обработка завершилась с ошибкой"))
        db.commit()
    logger.exception(
        "job_failed job_id=%s stage=failed code=%s validation_issues=%s",
        job_id, getattr(exc, "code", type(exc).__name__),
        getattr(exc, "validation_issues", []),
    )


def _analysis_request(job: Job) -> tuple[dict, Path]:
    artifact = from_data(job.artifact_path or "")
    request_path = artifact / "request.json"
    return json.loads(request_path.read_text(encoding="utf-8")), request_path


def _cleanup_model_versions(template_id: str, models_dir: Path) -> None:
    """Remove only unpublished versions not pinned by an active generation."""
    keep: set[Path] = set()
    with SessionLocal() as db:
        template = db.get(Template, template_id)
        if template and template.model_path:
            keep.add(from_data(template.model_path).resolve())
        active = db.scalars(select(Job).where(
            Job.template_id == template_id,
            Job.type == "generation",
            Job.status.in_(["queued", "processing"]),
        ))
        for job in active:
            try:
                request, _ = _analysis_request(job)
            except (OSError, ValueError):
                try:
                    request = json.loads(
                        (from_data(job.artifact_path or "") / "request.json").read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    continue
            pinned = request.get("model_path")
            if pinned:
                keep.add(from_data(pinned).resolve())
    if not models_dir.is_dir():
        return
    for candidate in models_dir.iterdir():
        is_version = candidate.name.startswith(("base-", "enriched-"))
        if is_version and candidate.resolve() not in keep and candidate.is_dir() and not candidate.name.endswith(".work"):
            shutil.rmtree(candidate, ignore_errors=True)


def analyze_template(
    job_id: str,
    use_vlm: bool = True,
    catalog_workers: int = 4,
    preserve_existing: bool = False,
) -> None:
    started = time.monotonic()
    try:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            template = db.get(Template, job.template_id) if job else None
            if not job or not template:
                return
            try:
                request, _ = _analysis_request(job)
            except (OSError, ValueError):
                # Compatibility for tasks created before analysis request files
                # were introduced. Worker-created jobs always take the branch above.
                legacy_model = from_data(template.model_path or "")
                request = {
                    "source_path": relative_to_data(legacy_model.parent / "source.pptx"),
                    "base_model_path": relative_to_data(legacy_model),
                    "use_vlm": use_vlm,
                    "catalog_workers": catalog_workers,
                    "preserve_existing": preserve_existing,
                }
            source = from_data(request["source_path"])
            model_dir = from_data(request["base_model_path"])
            preserve_existing = bool(request.get("preserve_existing", preserve_existing))
            use_vlm = bool(request.get("use_vlm", use_vlm))
            catalog_workers = int(request.get("catalog_workers", catalog_workers))
        transition(job_id, "validating", 10, "Проверяем структуру PPTX")
        transition(job_id, "analyzing_structure", 25, "Анализируем структуру слайдов")
        if model_dir.exists():
            shutil.rmtree(model_dir)
        # The availability path never waits for visual-language requests.
        TemplateModel(use_vlm=False).analyze(source, model_dir)
        transition(job_id, "building_base_catalog", 50, "Строим базовый семантический каталог")
        catalog = TemplateCatalogBuilder(model_dir, max_workers=catalog_workers).build()
        manifest = json.loads((model_dir / "presentation.json").read_text(encoding="utf-8"))
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            template = db.get(Template, job.template_id) if job else None
            if not job or not template:
                return
            template.status, template.error = "ready", None
            template.model_path = relative_to_data(model_dir)
            template.slide_count = len(manifest.get("slides", []))
            job.warnings = [] if catalog.families else ["Каталог не содержит семейств"]
            if use_vlm:
                job.status, job.stage, job.progress = "processing", "ready_enriching", 60
                message = "Шаблон готов к работе — улучшаем визуальный анализ"
            else:
                job.status, job.stage, job.progress = "ready", "ready", 100
                message = "Шаблон готов"
            db.add(JobEvent(job_id=job.id, stage=job.stage, progress=job.progress, message=message))
            db.commit()
        if use_vlm:
            from .queue import queue
            try:
                queue("template-enrichment").enqueue(
                    enrich_template, job_id,
                    job_id=f"template-enrichment-{job_id}", job_timeout=3600,
                    retry=None,
                )
            except Exception as exc:
                _enrichment_failed(job_id, exc)
        else:
            _cleanup_model_versions(template.id, model_dir.parent)
            logger.info("job_complete job_id=%s stage=ready duration=%.3f", job_id, time.monotonic() - started)
    except Exception as exc:
        _fail(job_id, exc, preserve_template=preserve_existing)
        raise


def _enrichment_failed(job_id: str, exc: Exception) -> None:
    message = f"Визуальное обогащение не завершено: {_public_error(exc)}"
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        template = db.get(Template, job.template_id) if job else None
        if not job or not template:
            return
        template.status, template.error = "ready", None
        job.status, job.stage = "ready", "enrichment_failed"
        job.warnings = [*(item for item in (job.warnings or []) if isinstance(item, str)), message]
        db.add(JobEvent(
            job_id=job.id, stage="enrichment_failed", progress=job.progress,
            message=message,
        ))
        db.commit()


def enrich_template(job_id: str) -> None:
    """Resume-capable, parallel visual analysis followed by atomic publication."""
    try:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            template = db.get(Template, job.template_id) if job else None
            if not job or not template:
                return
            request, _ = _analysis_request(job)
            base_dir = from_data(request["base_model_path"])
            enriched_dir = base_dir.with_name(f"enriched-{job.id}")
            work_dir = enriched_dir.with_name(f"{enriched_dir.name}.work")
            workers = max(1, min(8, int(request.get("enrichment_workers", 4))))
            timeout = float(request.get("vlm_timeout", 90))
        if enriched_dir.is_dir():
            # Publication completed before the worker was interrupted.
            with SessionLocal() as db:
                job = db.get(Job, job_id)
                template = db.get(Template, job.template_id) if job else None
                if job and template:
                    template.model_path = relative_to_data(enriched_dir)
                    job.status, job.stage, job.progress = "ready", "ready", 100
                    db.commit()
            return
        if not work_dir.exists():
            shutil.copytree(base_dir, work_dir)
        manifest_path = work_dir / "presentation.json"
        manifest = PresentationManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        transition(job_id, "enriching_visuals", max(60, min(90, job.progress)), "Улучшаем визуальный анализ")

        pending = []
        for entry in manifest.slides:
            metadata_path = work_dir / entry.metadata_path
            metadata = SlideMetadata.model_validate_json(metadata_path.read_text(encoding="utf-8"))
            entry.vlm_status = metadata.vlm.status
            if metadata.vlm.status == VLMStatus.DISABLED:
                pending.append((entry.slide_number, metadata_path))
        completed_before = len(manifest.slides) - len(pending)

        def process_slide(item: tuple[int, Path]) -> tuple[int, VLMStatus, str | None]:
            number, metadata_path = item
            metadata = SlideMetadata.model_validate_json(metadata_path.read_text(encoding="utf-8"))
            preview = metadata_path.parent / (metadata.preview_path or "preview.png")
            warning: str | None = None
            if not preview.is_file():
                warning = f"Слайд {number}: превью недоступно, визуальный анализ пропущен"
                result = SlideVLMAnalysis(status=VLMStatus.ERROR, error="Preview is unavailable")
            else:
                try:
                    analyzer = VLMAnalyzer(retries=1, timeout=timeout)
                    value = analyzer.analyze(preview, {
                        "slide_number": number,
                        "elements": [element.model_dump(mode="json") for element in metadata.elements],
                    })
                    result = SlideVLMAnalysis(
                        status=VLMStatus.OK, classification=value.classification,
                        tags=value.tags, description=value.description, confidence=value.confidence,
                    )
                    metadata.structures = detect_structures(metadata.elements, value.classification.value)
                except Exception as exc:
                    warning = f"Слайд {number}: {type(exc).__name__}: {exc}"
                    result = SlideVLMAnalysis(status=VLMStatus.ERROR, error=str(exc)[:1000])
            metadata.vlm = result
            if warning:
                metadata.warnings.append(warning)
            _atomic_write_text(metadata_path, metadata.model_dump_json(indent=2) + "\n")
            return number, result.status, warning

        warnings: list[str] = []
        if pending:
            with ThreadPoolExecutor(max_workers=min(workers, len(pending))) as executor:
                futures = [executor.submit(process_slide, item) for item in pending]
                for offset, future in enumerate(as_completed(futures), 1):
                    number, status, warning = future.result()
                    if warning:
                        warnings.append(warning)
                    entry = next(item for item in manifest.slides if item.slide_number == number)
                    entry.vlm_status = status
                    manifest.vlm_enabled = True
                    manifest.save(work_dir)
                    done = completed_before + offset
                    progress = 60 + int(30 * done / max(1, len(manifest.slides)))
                    transition(job_id, "enriching_visuals", progress, f"Визуально обработан слайд {number}")
        manifest.vlm_enabled = True
        manifest.warnings.extend(warnings)
        manifest.save(work_dir)
        transition(job_id, "rebuilding_catalog", 95, "Перестраиваем каталог с визуальными описаниями")
        TemplateCatalogBuilder(
            work_dir, max_workers=int(request.get("catalog_workers", 4)),
        ).build(force=True)
        os.replace(work_dir, enriched_dir)
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            template = db.get(Template, job.template_id) if job else None
            if not job or not template:
                return
            template.model_path = relative_to_data(enriched_dir)
            template.status, template.error = "ready", None
            job.status, job.stage, job.progress, job.error = "ready", "ready", 100, None
            job.warnings = [*(item for item in (job.warnings or []) if isinstance(item, str)), *warnings]
            db.add(JobEvent(job_id=job.id, stage="ready", progress=100, message="Визуальный анализ опубликован"))
            db.commit()
        _cleanup_model_versions(template.id, enriched_dir.parent)
    except Exception as exc:
        _enrichment_failed(job_id, exc)
        logger.exception("template_enrichment_failed job_id=%s", job_id)


def _disable_image_generation(plan, model_dir: Path) -> list[str]:
    changed = 0
    catalog = TemplateCatalog.model_validate_json((model_dir / "planner_catalog.json").read_text(encoding="utf-8"))
    families = {family.family_id: family for family in catalog.families}
    for slide in plan.slides:
        for assignment in slide.assignments:
            if assignment.action == SlotAction.GENERATE:
                assignment.action = SlotAction.KEEP
                assignment.content = None
                assignment.source_refs = []
                changed += 1
        family = families[slide.template_family_id]
        variant = next(item for item in family.variants if item.slide_number == slide.template_slide_number)
        slide.render_operations = compile_slide(slide, variant, plan.structured_assets)
    return [f"Запрещённая генерация изображений заменена на сохранение шаблона ({changed})"] if changed else []


def _delivery_validation_issues(plan, request: PlanningRequest) -> list[dict[str, str]]:
    """Last gate before expensive rendering and publishing a job as ready."""
    issues: list[dict[str, str]] = []
    used_images: set[str] = set()
    used_assets: set[str] = set()
    for index, slide in enumerate(plan.slides):
        meaningful = False
        for assignment in slide.assignments:
            if assignment.action in {SlotAction.REPLACE, SlotAction.GENERATE} and assignment.content is not None:
                meaningful = True
            if isinstance(assignment.content, ImageSlotContent) and assignment.content.asset_ref:
                used_images.add(assignment.content.asset_ref)
            if isinstance(assignment.content, VisualSlotContent):
                used_assets.add(assignment.content.asset_id)
            if isinstance(assignment.content, TextSlotContent):
                values = [assignment.content.text or "", *assignment.content.fields.values()]
                for value in values:
                    if re.search(r"\b(?:Available image|Доступное изображение)\s*:", value, re.I):
                        issues.append({
                            "code": "technical_source_leak", "path": f"slides[{index}]",
                            "message": "Техническое описание изображения попало в текст слайда.",
                        })
                if "title" in assignment.slot_id.casefold():
                    title = next((value.strip() for value in values if value.strip()), "")
                    if title and len(title.split()) == 1 and title.casefold() in {
                        "слайд", "раздел", "итоги", "контент", "slide", "section", "content",
                    }:
                        issues.append({
                            "code": "service_word_title", "path": f"slides[{index}]",
                            "message": "Заголовок состоит только из служебного слова.",
                        })
        if not meaningful:
            issues.append({
                "code": "empty_slide", "path": f"slides[{index}]",
                "message": "Слайд не содержит текста, визуализации или назначенного изображения.",
            })
    mentioned = f"{request.brief}\n{request.content_package}".casefold()
    for index, image in enumerate(request.provided_images):
        if image.original_name.casefold() in mentioned and image.asset_ref not in used_images:
            issues.append({
                "code": "referenced_image_uncovered",
                "path": f"provided_images[{index}]",
                "message": f"Изображение {image.original_name!r} не использовано.",
            })
    for index, asset in enumerate(request.structured_assets):
        if asset.visual_hint.mode != "none" and asset.id not in used_assets:
            issues.append({
                "code": "required_asset_uncovered", "path": f"structured_assets[{index}]",
                "message": f"Набор данных {asset.id!r} не использован.",
            })
    return issues


def generate_presentation(job_id: str) -> None:
    started = time.monotonic()
    try:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            template = db.get(Template, job.template_id) if job else None
            files = list(db.scalars(select(JobFile).where(JobFile.job_id == job_id)))
            if job and job.status == "canceled":
                return
            if not job or not template or template.status != "ready":
                raise ValueError("Шаблон недоступен")
            job_dir = from_data(job.artifact_path or "")
        transition(job_id, "extracting_content", 15, "Извлекаем текст и проверяем вложения")
        request_data = json.loads((job_dir / "request.json").read_text(encoding="utf-8"))
        pinned_model_path = request_data.get("model_path") or template.model_path
        if not pinned_model_path:
            raise ValueError("Версия модели шаблона не закреплена")
        model_dir = from_data(pinned_model_path)
        for stale in (job_dir / "plan.json", job_dir / "presentation.pptx", job_dir / "presentation.pdf"):
            stale.unlink(missing_ok=True)
        shutil.rmtree(job_dir / "qa", ignore_errors=True)
        content_parts = [request_data.get("content_text", "").strip()]
        provided_images = _provided_images(files)
        structured_assets: list[StructuredAsset] = []
        structured_assets.extend(PlanningRequest.model_validate({
            "brief": request_data["brief"],
            "content_package": request_data.get("content_text", ""),
            "structured_assets": request_data.get("structured_assets", []),
            "slide_count": {"min": request_data["slide_min"], "max": request_data["slide_max"]},
        }).structured_assets if request_data.get("structured_assets") else [])
        for item in files:
            path = from_data(item.relative_path)
            extension = Path(item.original_name).suffix.lower()
            if extension in IMAGE_EXTENSIONS:
                asset_ref = path.name
                content_parts.append(
                    _image_source_description(item.original_name, asset_ref)
                )
            else:
                structured_assets.extend(extract_structured_assets(path, item.original_name))
                value = extract_document(path, item.original_name).strip()
                if value:
                    content_parts.append(f"Документ {item.original_name}:\n{value}")
        content = "\n\n".join(part for part in content_parts if part)
        if len(content) > settings.max_extracted_chars:
            raise ValueError("После извлечения получилось больше 50 000 символов; сократите документы")
        if not content:
            content = "Переданные изображения: " + ", ".join(from_data(item.relative_path).name for item in files)
        unique_assets: dict[str, StructuredAsset] = {}
        for asset in structured_assets:
            previous = unique_assets.get(asset.id)
            if previous is not None and previous != asset:
                raise ValueError(f"Конфликт structured asset id: {asset.id}")
            unique_assets[asset.id] = asset
        planning_request = PlanningRequest(
            brief=request_data["brief"], content_package=content,
            structured_assets=list(unique_assets.values()),
            provided_images=provided_images,
            slide_count={"min": request_data["slide_min"], "max": request_data["slide_max"]},
        )
        recovery_warnings = _materialize_missing_image_placeholders(
            planning_request, job_dir / "assets",
        )
        transition(job_id, "content_analysis", 25, "Анализируем содержание")
        mode = request_data.get("generation_mode") or (
            "reliable" if request_data.get("fast_mode") else "strict"
        )
        stage_progress = {
            "materializing_assets": (28, "Восстанавливаем недостающие данные"),
            "content_analysis": (30, "Анализируем содержание"),
            "drafting": (42, "Создаём черновик презентации"),
            "repairing": (55, "Исправляем проблемный слайд"),
            "drafting_candidates": (42, "Создаём варианты презентации"),
            "evaluating_candidates": (52, "Сравниваем варианты и проверяем смысл"),
            "revising": (60, "Исправляем проблемные слайды"),
            "planning_outline": (38, "Строим общий сценарий презентации"),
            "evaluating_quality": (62, "Проверяем факты, сценарий и визуальное соответствие"),
        }

        def planner_progress(stage: str) -> None:
            if stage.startswith("generating_slides:"):
                _, completed, total = stage.split(":", 2)
                done, count = int(completed), max(1, int(total))
                progress = 42 + int(16 * done / count)
                transition(
                    job_id, "generating_slides", progress,
                    f"Готово {done} из {count} слайдов",
                )
                return
            progress, message = stage_progress[stage]
            transition(job_id, stage, progress, message)

        planner = ContentPlanner(
            model_dir,
            enable_critique=mode == "strict",
            max_revisions=2 if mode == "strict" else 1,
            planning_budget=(
                settings.planner_strict_budget if mode == "strict"
                else settings.planner_fast_budget
            ),
            outline_wave_budget=settings.planner_outline_wave_budget,
            slide_wave_budget=settings.planner_slide_wave_budget,
            critique_wave_budget=settings.planner_critique_wave_budget,
            checkpoint_dir=job_dir / "checkpoints",
            job_id=job_id,
            progress=planner_progress,
            single_critique=False,
            allow_model_assets=False,
            max_workers=settings.planner_max_workers,
            semantic_pipeline=True,
        )
        restriction = "\nWEB MODE: image action generate is forbidden. Use keep, or replace only with a provided asset_ref."
        planner.draft_prompt += restriction
        planner.revision_prompt += restriction
        plan = planner.generate(planning_request)
        warnings = [*recovery_warnings, *_disable_image_generation(plan, model_dir)]
        delivery_issues = _delivery_validation_issues(plan, planning_request)
        warnings.extend(item["message"] for item in delivery_issues)
        warnings.extend(plan.quality.warnings)
        plan_path = job_dir / "plan.json"
        _atomic_write_text(plan_path, plan.model_dump_json(indent=2) + "\n")
        transition(job_id, "building", 75, "Собираем PPTX")
        output = job_dir / "presentation.pptx"
        qa = job_dir / "qa"
        report = PresentationBuilder().build(
            model_dir, plan, output, assets_dir=job_dir / "assets", qa_dir=qa,
            pdf_output_path=job_dir / "presentation.pdf",
            progress=lambda stage: transition(
                job_id, stage, 90, "Создаём превью презентации",
            ),
        )
        warnings.extend(issue.message for issue in report.issues if issue.level == "warning")
        with SessionLocal() as db:
            job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if not job:
                return
            if job.status == "canceled":
                return
            job.status, job.stage, job.progress = "ready", "ready", 100
            job.error = None
            job.artifact_path = relative_to_data(job_dir)
            job.qa_path = relative_to_data(qa)
            job.warnings = warnings
            db.add(JobEvent(job_id=job.id, stage="ready", progress=100, message="Презентация готова"))
            db.commit()
        logger.info("job_complete job_id=%s stage=ready duration=%.3f", job_id, time.monotonic() - started)
    except JobCanceled:
        logger.info("job_canceled job_id=%s", job_id)
    except Exception as exc:
        _fail(job_id, exc)
        raise
