import json
from pathlib import Path
from types import SimpleNamespace

from content_planner.catalog import CatalogBuildError
from content_planner.catalog import TemplateCatalogBuilder as RealTemplateCatalogBuilder
from content_planner.client import ResponseIncompleteError
from webapp.backend.app import tasks
from webapp.backend.app.database import SessionLocal
from webapp.backend.app.models import Job, JobEvent, JobFile, Template, User
from webapp.backend.app.storage import from_data, relative_to_data, user_root
from content_planner.planner import PlannerFailure
from content_planner.models import PlanningRequest, SlotAction


class FakeLLM:
    model = "test-model"

    def __init__(self) -> None:
        self.responses = [
            json.dumps({"slides": [{
                "slide_number": 1,
                "classification": "content_text",
                "description": "Текстовый слайд",
                "family_hint": "content_text",
                "family_description": "Текстовый контент",
                "slots": [{
                    "slot_id": "title",
                    "kind": "text",
                    "role": "title",
                    "target": {"target_type": "shape", "shape_id": 11},
                    "required": True,
                }],
            }]}),
            json.dumps({"groups": [{
                "family_id": "content_text",
                "description": "Текстовый контент",
                "slide_numbers": [1],
            }]}),
        ]

    def complete(self, prompt: str) -> str:
        return self.responses.pop(0)


class FakeTemplateModel:
    use_vlm_values: list[bool] = []

    def __init__(self, *, use_vlm: bool) -> None:
        self.use_vlm = use_vlm
        self.use_vlm_values.append(use_vlm)

    def analyze(self, source: Path, model_dir: Path) -> None:
        slide_dir = model_dir / "slides" / "slide_001"
        slide_dir.mkdir(parents=True)
        (model_dir / "presentation.json").write_text(json.dumps({
            "schema_version": "3.0",
            "source_sha256": "source-hash",
            "slides": [{"metadata_path": "slides/slide_001/metadata.json"}],
        }), encoding="utf-8")
        (slide_dir / "metadata.json").write_text(json.dumps({
            "slide_number": 1,
            "elements": [{
                "shape_id": 11,
                "type": "text",
                "placeholder_type": "title",
                "text": "Заголовок",
                "box": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.2},
                "children": [],
            }],
            "structures": [],
            "vlm": {"status": "disabled"},
        }), encoding="utf-8")


def test_model_failures_use_fallback_only_in_fast_mode():
    failure = PlannerFailure("llm_api_error", "timeout")

    assert tasks._should_use_fallback(failure, "reliable")
    assert not tasks._should_use_fallback(failure, "strict")


def test_provider_failures_never_use_fallback_even_in_fast_mode():
    for code in (
        "provider_permission", "provider_rate_limit", "provider_timeout",
        "provider_unavailable", "planning_budget_exhausted",
    ):
        assert not tasks._should_use_fallback(PlannerFailure(code, "provider failed"), "reliable")


def test_repeated_structured_response_failure_can_fallback_only_in_fast_mode():
    failure = PlannerFailure("invalid_llm_response", "invalid JSON twice")
    assert tasks._should_use_fallback(failure, "reliable")
    assert not tasks._should_use_fallback(failure, "strict")


def test_image_source_description_maps_original_name_to_stored_asset_ref():
    description = tasks._image_source_description(
        "../../irina_krylova.png", "8f2d1c.png",
    )
    assert description == (
        "Доступное изображение: 8f2d1c.png "
        "(исходное имя файла: irina_krylova.png)"
    )


def test_job_files_become_opaque_provided_images_inside_assets():
    root = user_root("provided-images") / "jobs" / "job-1" / "assets"
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for index in range(5):
        path = root / f"hash-{index}.png"
        path.write_bytes(b"png")
        files.append(JobFile(
            id=f"file-{index}", job_id="job-1",
            original_name=f"person-{index}.png", media_type="image/png",
            relative_path=relative_to_data(path), size=3,
        ))

    images = tasks._provided_images(files)

    assert [item.original_name for item in images] == [f"person-{index}.png" for index in range(5)]
    assert [item.asset_ref for item in images] == [f"hash-{index}.png" for index in range(5)]
    assert all("/" not in item.asset_ref for item in images)


def test_delivery_gate_rejects_eight_empty_fallback_slides():
    populated = SimpleNamespace(action=SlotAction.REPLACE, content=SimpleNamespace())
    cleared = SimpleNamespace(action=SlotAction.CLEAR, content=None)
    plan = SimpleNamespace(slides=[
        SimpleNamespace(assignments=[populated, cleared]),
        *[SimpleNamespace(assignments=[cleared, cleared]) for _ in range(8)],
        SimpleNamespace(assignments=[populated, cleared]),
    ])
    request = PlanningRequest(
        brief="Ten slides", content_package="Facts", slide_count={"min": 10, "max": 10},
    )

    issues = tasks._delivery_validation_issues(plan, request)

    assert [item["code"] for item in issues] == ["empty_slide"] * 8


def test_delivery_gate_allows_content_complete_needs_review_fallback():
    populated = SimpleNamespace(action=SlotAction.REPLACE, content=SimpleNamespace())
    plan = SimpleNamespace(slides=[
        SimpleNamespace(assignments=[populated, populated]) for _ in range(10)
    ])
    request = PlanningRequest(
        brief="Ten slides", content_package="Facts", slide_count={"min": 10, "max": 10},
    )

    assert tasks._delivery_validation_issues(plan, request) == []

def test_objective_planning_failures_do_not_use_fallback():
    for code in tasks.NON_FALLBACK_PLANNER_ERRORS:
        assert not tasks._should_use_fallback(PlannerFailure(code, "invalid request"))


def test_incomplete_catalog_response_has_safe_public_error():
    failure = CatalogBuildError("LLM failed during catalog slide reduction")
    failure.__cause__ = ResponseIncompleteError(
        "LLM response incomplete (reason='max_output_tokens')"
    )

    assert tasks._public_error(failure) == (
        "Не удалось завершить анализ шаблона. Повторите анализ шаблона."
    )


def test_internal_catalog_failure_has_safe_public_error():
    failure = CatalogBuildError(
        "failed to build catalog variant for slide 38: shape_id 2107 is both editable and static"
    )

    assert tasks._public_error(failure) == (
        "Не удалось завершить анализ шаблона. Повторите анализ шаблона."
    )


def test_canceled_job_cannot_transition_or_be_marked_failed():
    with SessionLocal() as db:
        user = User(username="canceled-user", password_hash="unused")
        db.add(user); db.flush()
        job = Job(
            owner_id=user.id, type="generation", status="canceled",
            stage="canceled", progress=42,
        )
        db.add(job); db.commit(); job_id = job.id

    try:
        tasks.transition(job_id, "building", 75, "Собираем PPTX")
    except tasks.JobCanceled:
        pass
    else:
        raise AssertionError("canceled job accepted a new transition")
    tasks._fail(job_id, RuntimeError("late worker error"))

    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert (job.status, job.stage, job.progress) == ("canceled", "canceled", 42)
        assert db.query(JobEvent).filter_by(job_id=job_id).count() == 0


def test_template_task_without_vlm_reaches_ready(monkeypatch):
    FakeTemplateModel.use_vlm_values = []
    monkeypatch.setattr(tasks, "TemplateModel", FakeTemplateModel)
    monkeypatch.setattr(
        tasks,
        "TemplateCatalogBuilder",
        lambda model_dir, max_workers: RealTemplateCatalogBuilder(
            model_dir, max_workers=max_workers, llm=FakeLLM()
        ),
    )
    with SessionLocal() as db:
        user = User(username="worker-user", password_hash="unused")
        db.add(user)
        db.flush()
        root = user_root(user.id) / "templates" / "worker-template"
        root.mkdir(parents=True, exist_ok=True)
        (root / "source.pptx").write_bytes(b"fake")
        template = Template(
            owner_id=user.id,
            name="brand.pptx",
            sha256="d" * 64,
            model_path=relative_to_data(root / "model"),
        )
        db.add(template)
        db.flush()
        job = Job(
            owner_id=user.id,
            template_id=template.id,
            type="template_analysis",
            brief_excerpt=template.name,
        )
        db.add(job)
        db.commit()
        job_id, template_id = job.id, template.id

    tasks.analyze_template(job_id, use_vlm=False, catalog_workers=2)

    with SessionLocal() as db:
        job = db.get(Job, job_id)
        template = db.get(Template, template_id)
        assert job is not None and job.status == "ready" and job.progress == 100
        assert template is not None and template.status == "ready" and template.slide_count == 1
        assert (from_data(template.model_path) / "planner_catalog.json").is_file()
    assert FakeTemplateModel.use_vlm_values == [False]


def test_long_enrichment_error_keeps_base_template_ready_and_event_bounded():
    with SessionLocal() as db:
        user = User(username="enrichment-user", password_hash="unused")
        db.add(user)
        db.flush()
        template = Template(
            owner_id=user.id, name="brand.pptx", sha256="e" * 64,
            status="ready",
        )
        db.add(template)
        db.flush()
        job = Job(
            owner_id=user.id, template_id=template.id,
            type="template_analysis", status="processing", progress=95,
        )
        db.add(job)
        db.commit()
        job_id, template_id = job.id, template.id

    tasks._enrichment_failed(job_id, RuntimeError("x" * 900))

    with SessionLocal() as db:
        job = db.get(Job, job_id)
        template = db.get(Template, template_id)
        event = db.query(JobEvent).filter_by(job_id=job_id).one()
        assert template is not None and template.status == "ready"
        assert job is not None and job.status == "ready"
        assert job.stage == "enrichment_failed"
        assert len(job.warnings[0]) > 500
        assert event.stage == "enrichment_failed"
        assert len(event.message) == 500
