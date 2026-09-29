from __future__ import annotations

import json
import re
import threading
import time
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from content_planner.planner import ContentPlanner, PlannerFailure, build_sources
from content_planner.structured_assets import parse_json_assets
from tests.test_content_planner import analysis_data, passing_critique, template_catalog
from content_planner.models import (
    PlanningRequest, ProvidedImage, SemanticDeckOutline, TemplateCatalog,
    VisualSlotContent,
)
from scripts.live_soak import TEMPLATE as NOVACORE_TEMPLATE, request as novacore_request


def request_with_slides(count: int) -> PlanningRequest:
    return PlanningRequest(
        brief="Представить продукт Atlas инженерной команде.",
        content_package=(
            "- Продукт Atlas помогает инженерам управлять релизами.\n"
            "- Анна Лебедева — руководитель продукта."
        ),
        slide_count={"min": count, "max": count},
    )


def outline(count: int) -> dict:
    slides = []
    for number in range(1, count + 1):
        cover = number == 1
        slides.append({
            "number": number,
            "purpose": "Обозначить тему" if cover else f"Раскрыть тезис {number}",
            "template_family_id": "cover_basic" if cover else "closing_basic",
            "template_slide_number": 1 if cover else 2,
            "source_refs": ["brief"] if cover else ["content_001", "content_002"],
            "asset_ids": [], "image_ids": [], "chart_asset_id": None,
        })
    return {
        "deck": {
            "title": {"text": "Продукт Atlas", "source_refs": ["brief"]},
            "summary": {"text": "Atlas помогает инженерам.", "source_refs": ["content_001"]},
            "language": "ru",
        },
        "slides": slides,
    }


def prompt_slide_number(prompt: str) -> int:
    match = re.search(r"SLIDE OUTLINE:\n(\{[^\n]+\})", prompt)
    assert match
    return int(json.loads(match.group(1))["number"])


def draft(number: int) -> dict:
    if number == 1:
        return {"number": 1, "assignments": [
            {"slot_id": "title", "action": "replace", "source_refs": ["brief"],
             "content": {"kind": "text", "text": "Продукт Atlas"}},
            {"slot_id": "subtitle", "action": "clear", "source_refs": [], "content": None},
        ]}
    return {"number": number, "assignments": [{
        "slot_id": "message", "action": "replace",
        "source_refs": ["content_001", "content_002"],
        "content": {"kind": "text", "text": "Atlas помогает инженерам; продуктом руководит Анна Лебедева."},
    }]}


class ParallelLLM:
    model = "strict-outline-test"
    structured_output = "native"

    def __init__(self, count: int, *, fail_once: int | None = None, delay: float = 0.005):
        self.count = count
        self.fail_once = fail_once
        self.delay = delay
        self.calls: dict[int, int] = {}
        self.schemas: dict[str, list[dict]] = {}
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()

    def complete(self, prompt: str, **kwargs) -> str:
        stage = kwargs["stage"]
        self.schemas.setdefault(stage, []).append(kwargs["json_schema"])
        if stage == "asset materialization":
            requested = re.findall(r'"id":\s*"([A-Za-z0-9_.-]+)"', prompt.split("SOURCES:", 1)[0])
            return json.dumps({"datasets": [{
                "id": asset_id, "title": asset_id,
                "columns": [
                    {"key": "category", "label": "Категория", "type": "text"},
                    {"key": "value", "label": "Значение", "type": "number"},
                ],
                "rows": [
                    {"category": "A", "value": 60},
                    {"category": "B", "value": 40},
                ],
                "visual_hint": {"mode": "auto"},
                "source_refs": ["brief"], "provenance": "inferred",
            } for asset_id in dict.fromkeys(requested)]})
        if stage == "content analysis":
            value = analysis_data()
            value["recommended_slide_count"] = self.count
            return json.dumps(value)
        if stage == "deck outline":
            return json.dumps(outline(self.count))
        if stage == "slide draft":
            number = prompt_slide_number(prompt)
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                self.calls[number] = self.calls.get(number, 0) + 1
                call = self.calls[number]
            try:
                time.sleep(self.delay * (1 + number % 3))
                if number == self.fail_once and call == 1:
                    raise TimeoutError("one transient slide failure")
                return json.dumps(draft(number))
            finally:
                with self.lock:
                    self.active -= 1
        if stage == "plan critique":
            return json.dumps(passing_critique())
        raise AssertionError(stage)


def test_ten_slides_are_parallel_bounded_stable_and_retry_only_failure():
    llm = ParallelLLM(10, fail_once=5)
    planner = ContentPlanner(
        llm=llm, catalog=template_catalog(), max_workers=4, max_revisions=0,
    )
    with patch.object(planner, "_run_wave", wraps=planner._run_wave) as waves:
        plan = planner.generate(request_with_slides(10))

    assert [slide.number for slide in plan.slides] == list(range(1, 11))
    assert llm.maximum == 4
    assert llm.calls[5] == 2
    assert all(calls == 1 for number, calls in llm.calls.items() if number != 5)
    slide_batches = [
        len(call.args[1]) for call in waves.call_args_list if call.args[0] == "slides"
    ]
    assert slide_batches == [4, 4, 2]
    outline_schema = json.dumps(llm.schemas["deck outline"][0])
    assert "content_type" in outline_schema
    assert "template_family_id" not in outline_schema
    assert "template_slide_number" not in outline_schema
    assert "shape_id" not in outline_schema
    for schema in llm.schemas["slide draft"]:
        serialized = json.dumps(schema)
        assert "assignments" not in serialized
        assert '"image"' not in serialized
        assert '"visual"' not in serialized


def test_strict_slide_timeout_has_no_fallback_and_leaves_no_active_request():
    llm = ParallelLLM(2, delay=0.04)
    planner = ContentPlanner(
        llm=llm, catalog=template_catalog(), max_revisions=0,
        slide_wave_budget=0.01, planning_budget=1,
    )

    with pytest.raises(PlannerFailure) as caught:
        planner.generate(request_with_slides(2))

    assert caught.value.code == "planning_budget_exhausted"
    assert llm.active == 0


def test_missing_named_assets_are_materialized_before_outline():
    llm = ParallelLLM(2)
    request = PlanningRequest(
        brief=(
            "Используй набор данных `active_teams_2025`, таблицу `product_usage_share`, "
            "dataset id product_results и portrait.png"
        ),
        content_package="Исходные сведения",
        slide_count={"min": 2, "max": 3},
    )

    planner = ContentPlanner(llm=llm, catalog=template_catalog())
    planner._deadline = time.monotonic() + 5
    planner._checkpoint_key = "materialization-test"
    with tempfile.TemporaryDirectory() as directory:
        planner.checkpoint_dir = Path(directory)
        planner._materialize_missing_datasets(request)

    assert [asset.id for asset in request.structured_assets] == [
        "active_teams_2025", "product_results", "product_usage_share",
    ]
    assert all(asset.provenance == "inferred" for asset in request.structured_assets)
    assert any("active_teams_2025" in warning for warning in planner._runtime_warnings)
    assert len(llm.schemas["asset materialization"]) == 1


def test_russian_dataset_inflections_are_materialized():
    llm = ParallelLLM(2)
    request = PlanningRequest(
        brief=(
            "Покажи динамику из набора данных `active_teams_2025`, доли из "
            "набора данных `product_usage_share` и итоги в наборе данных "
            "`product_results`."
        ),
        content_package="Исходные сведения",
        slide_count={"min": 2, "max": 3},
    )

    planner = ContentPlanner(llm=llm, catalog=template_catalog())
    planner._deadline = time.monotonic() + 5
    planner._checkpoint_key = "materialization-test"
    planner._materialize_missing_datasets(request)

    assert [asset.id for asset in request.structured_assets] == [
        "active_teams_2025", "product_results", "product_usage_share",
    ]


def test_uploaded_image_storage_reference_is_not_reported_missing():
    request = PlanningRequest(
        brief="Профиль с `portrait.png`",
        content_package="Доступное изображение: a1b2c3.png (исходное имя файла: portrait.png)",
        provided_images=[ProvidedImage(
            id="image:1", original_name="portrait.png", asset_ref="a1b2c3.png",
        )],
        slide_count={"min": 1, "max": 2},
    )

    ContentPlanner._validate_explicit_assets(request)


def test_novacore_portraits_are_bound_by_original_name_not_upload_order():
    request = novacore_request()
    request.structured_assets = parse_json_assets(
        (Path(__file__).resolve().parents[1] / "description" / "tasks_examples" / "it_team_products" / "structured_assets.json").read_bytes()
    )
    request.provided_images.reverse()
    sources = build_sources(request)
    profiles = [
        ("Ирина Крылова", "irina_krylova.png"),
        ("Артём Савельев", "artem_saveliev.png"),
        ("Майя Белова", "maya_belova.png"),
        ("Роман Алиев", "roman_aliev.png"),
        ("Дарья Ким", "daria_kim.png"),
    ]
    slides = [
        {"number": 1, "purpose": "NovaCore", "content_type": "cover", "source_refs": ["brief"]},
        {"number": 2, "purpose": "Продукты", "content_type": "cards", "source_refs": ["content_005"]},
        {"number": 3, "purpose": "Рост", "content_type": "chart", "source_refs": ["asset:active_teams_2025"]},
        {"number": 4, "purpose": "Доли", "content_type": "chart", "source_refs": ["asset:product_usage_share"]},
        {"number": 5, "purpose": "Результаты", "content_type": "table", "source_refs": ["asset:product_results"]},
    ]
    for number, (name, _) in enumerate(profiles, 6):
        source = next(item.id for item in sources if name.casefold() in item.text.casefold())
        slides.append({
            "number": number, "purpose": f"Профиль {name}",
            "content_type": "profiles", "source_refs": [source],
        })
    semantic = SemanticDeckOutline.model_validate({
        "deck": {
            "title": {"text": "NovaCore", "source_refs": ["brief"]},
            "summary": {"text": "Продукты и команда", "source_refs": ["brief"]},
            "language": "ru",
        },
        "slides": slides,
    })
    catalog = TemplateCatalog.model_validate_json(
        (NOVACORE_TEMPLATE / "planner_catalog.json").read_text(encoding="utf-8")
    )
    planner = ContentPlanner(llm=ParallelLLM(10), catalog=catalog)
    resolved = planner._resolve_outline(
        request, sources, catalog, semantic, planner._asset_guidance(request, catalog),
    )
    images = {image.id: image.original_name for image in request.provided_images}

    assert [images[slide.image_ids[0]] for slide in resolved.slides[5:]] == [
        filename for _, filename in profiles
    ]

    assert [slide.asset_ids for slide in resolved.slides[2:5]] == [
        ["active_teams_2025"], ["product_usage_share"], ["product_results"],
    ]
    assert resolved.slides[4].template_slide_number in {35, 36, 37}
    guidance = planner._asset_guidance(request, catalog)
    analysis = planner._analysis_from_semantic_outline(request, sources, semantic)
    with patch.object(
        planner, "_complete_model",
        side_effect=lambda _prompt, model, _stage, **_kwargs: model.model_validate({}),
    ):
        drafts = {
            slide.number: planner._generate_slide_draft(
                request, sources, analysis, catalog, resolved, guidance, index,
            )
            for index, slide in enumerate(resolved.slides[2:5], start=2)
        }

    technical = []
    for slide in resolved.slides[2:5]:
        draft = drafts[slide.number]
        visual = [
            assignment for assignment in draft.assignments
            if isinstance(assignment.content, VisualSlotContent)
        ]
        assert len(visual) == 1
        assert visual[0].content.asset_id == slide.asset_ids[0]
        assert visual[0].source_refs == [f"asset:{slide.asset_ids[0]}"]
        technical.extend(item.content.asset_id for item in visual)
    assert technical == [
        "active_teams_2025", "product_usage_share", "product_results",
    ]


def test_semantic_technical_slide_without_assets_uses_text_layout():
    request = request_with_slides(1)
    sources = build_sources(request)
    semantic = SemanticDeckOutline.model_validate({
        "deck": {
            "title": {"text": "Atlas", "source_refs": ["brief"]},
            "summary": {"text": "Динамика", "source_refs": ["brief"]},
            "language": "ru",
        },
        "slides": [{
            "number": 1, "purpose": "Показать динамику",
            "content_type": "chart", "source_refs": ["brief"],
        }],
    })
    planner = ContentPlanner(llm=ParallelLLM(1), catalog=template_catalog())

    resolved = planner._resolve_outline(request, sources, template_catalog(), semantic, [])

    assert resolved.slides[0].asset_ids == []
    assert resolved.slides[0].template_family_id in {"cover_basic", "closing_basic"}
    assert any("текстовый макет" in warning for warning in planner._runtime_warnings)
