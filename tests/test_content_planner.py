from __future__ import annotations

import copy
import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from content_planner.catalog import (
    CatalogBuildError,
    TemplateCatalogBuilder,
    _normalize_repeat_structures,
    catalog_prompt_projection,
)
from content_planner.client import ResponseIncompleteError
from content_planner.demo import DEMO_REQUEST_PATH, demo_request
from content_planner.main import PROJECT_ROOT, _resolve_existing_path, _resolve_output_path
from content_planner.main import main as cli_main
from content_planner.models import (
    CatalogFamily,
    CatalogBatchResult,
    CatalogSlideDescriptor,
    CatalogSlot,
    CatalogVariant,
    ContentAnalysis,
    NarrativePlanCandidate,
    PlanCandidate,
    PlanningRequest,
    ProvidedImage,
    QualityReport,
    SlotAssignment,
    SlotAction,
    SlotKind,
    SourceChunk,
    TemplateCatalog,
    VisualHint,
)
from content_planner.planner import ContentPlanner, PlannerFailure, build_sources
from content_planner.structured_assets import materialize_visual, parse_csv_bytes
from content_planner.validator import validate_candidate
from slide_types import SlideClass


class FakeLLM:
    def __init__(self, *responses: str, model: str = "test-model"):
        self.responses = iter(responses)
        self.prompts: list[str] = []
        self.model = model

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return next(self.responses)


def planning_request(minimum: int = 2, maximum: int = 3) -> PlanningRequest:
    return PlanningRequest.model_validate({
        "brief": "Представить продукт Atlas инженерной команде.",
        "content_package": (
            "- Продукт Atlas помогает инженерам управлять релизами.\n"
            "- Анна Лебедева — руководитель продукта."
        ),
        "slide_count": {"min": minimum, "max": maximum},
    })


def template_catalog() -> TemplateCatalog:
    return TemplateCatalog(
        model="test-model",
        source_sha256="template-hash",
        families=[
            CatalogFamily(
                family_id="cover_basic",
                slide_class=SlideClass.COVER,
                description="Обложка",
                variants=[CatalogVariant(
                    slide_number=1,
                    description="Обложка с заголовком",
                    slots=[
                        CatalogSlot(
                            slot_id="title", kind=SlotKind.TEXT, role="title",
                            target_shape_ids=[101], required=True, max_chars=80,
                            bindings=[{"shape_id": 101, "renderer": "text", "value_paths": ["/text"]}],
                        ),
                        CatalogSlot(
                            slot_id="subtitle", kind=SlotKind.TEXT, role="subtitle",
                            target_shape_ids=[102], required=False, max_chars=120,
                            bindings=[{"shape_id": 102, "renderer": "text", "value_paths": ["/text"]}],
                        ),
                    ],
                    static_shape_ids=[103],
                )],
            ),
            CatalogFamily(
                family_id="closing_basic",
                slide_class=SlideClass.CLOSING,
                description="Финальный слайд",
                variants=[CatalogVariant(
                    slide_number=2,
                    description="Финальный тезис",
                    slots=[CatalogSlot(
                        slot_id="message", kind=SlotKind.TEXT, role="message",
                        target_shape_ids=[201], required=True, max_chars=120,
                        bindings=[{"shape_id": 201, "renderer": "text", "value_paths": ["/text"]}],
                    )],
                    static_shape_ids=[202],
                )],
            ),
            CatalogFamily(
                family_id="metrics_pair",
                slide_class=SlideClass.METRICS,
                description="Две метрики",
                variants=[CatalogVariant(
                    slide_number=3,
                    description="Парные метрики",
                    slots=[CatalogSlot(
                        slot_id="metrics", kind=SlotKind.METRICS, role="metrics",
                        target_shape_ids=[301, 302, 303, 304], required=True, capacity=2,
                        bindings=[
                            {"shape_id": 301, "renderer": "text", "value_paths": ["/items/0/label"]},
                            {"shape_id": 302, "renderer": "text", "value_paths": ["/items/0/value"]},
                            {"shape_id": 303, "renderer": "text", "value_paths": ["/items/1/label"]},
                            {"shape_id": 304, "renderer": "text", "value_paths": ["/items/1/value"]},
                        ],
                    )],
                    static_shape_ids=[305],
                )],
            ),
        ],
    )


def analysis_data() -> dict:
    return {
        "goal": "Представить Atlas",
        "audience": "Инженерная команда",
        "language": "ru",
        "narrative": ["Тема", "Польза и команда"],
        "recommended_slide_count": 2,
        "facts": [
            {
                "id": "product",
                "text": "Atlas помогает управлять релизами",
                "source_refs": ["content_001"],
                "mandatory": True,
            },
            {
                "id": "lead",
                "text": "Анна Лебедева руководит продуктом",
                "source_refs": ["content_002"],
                "mandatory": True,
            },
        ],
        "visual_opportunities": [],
    }


def candidate_data() -> dict:
    return {
        "deck": {
            "title": {"text": "Продукт Atlas", "source_refs": ["brief"]},
            "summary": {
                "text": "Atlas помогает инженерам управлять релизами.",
                "source_refs": ["content_001"],
            },
            "language": "ru",
        },
        "slides": [
            {
                "number": 1,
                "purpose": "Обозначить тему",
                "template_family_id": "cover_basic",
                "template_slide_number": 1,
                "template_class": "cover",
                "assignments": [
                    {
                        "slot_id": "title",
                        "kind": "text",
                        "target_shape_ids": [101],
                        "action": "replace",
                        "source_refs": ["brief"],
                        "content": {"kind": "text", "text": "Продукт Atlas"},
                    },
                    {
                        "slot_id": "subtitle",
                        "kind": "text",
                        "target_shape_ids": [102],
                        "action": "clear",
                        "source_refs": [],
                        "content": None,
                    },
                ],
            },
            {
                "number": 2,
                "purpose": "Зафиксировать пользу и владельца",
                "template_family_id": "closing_basic",
                "template_slide_number": 2,
                "template_class": "closing",
                "assignments": [{
                    "slot_id": "message",
                    "kind": "text",
                    "target_shape_ids": [201],
                    "action": "replace",
                    "source_refs": ["content_001", "content_002"],
                    "content": {
                        "kind": "text",
                        "text": "Atlas помогает инженерам. Продуктом руководит Анна Лебедева.",
                    },
                }],
            },
        ],
    }


def passing_critique() -> dict:
    return {
        "scores": {
            "factuality": 5,
            "coverage": 5,
            "narrative": 4,
            "template_fit": 5,
            "conciseness": 5,
        },
        "issues": [],
    }


class InputAndValidationTests(unittest.TestCase):
    def test_constraint_fact_does_not_require_slide_coverage(self):
        request = planning_request(2, 2)
        analysis = ContentAnalysis.model_validate(analysis_data())
        analysis = ContentAnalysis.model_validate({
            **analysis.model_dump(mode="json"),
            "facts": [*analysis.model_dump(mode="json")["facts"], {
                "id": "exact_count", "text": "Exactly two slides",
                "source_refs": ["content_002"], "mandatory": True,
                "fact_type": "constraint",
            }],
        })
        candidate = PlanCandidate.model_validate(candidate_data())
        candidate.slides[1].assignments[0].source_refs = ["content_001"]

        errors = validate_candidate(request, build_sources(request), analysis, template_catalog(), candidate)

        self.assertFalse(any("exact_count" in error for error in errors))

    def test_content_fact_requires_every_source_ref(self):
        request = planning_request(2, 2)
        analysis = ContentAnalysis.model_validate(analysis_data())
        analysis.facts[0].source_refs = ["content_001", "content_002"]
        candidate = PlanCandidate.model_validate(candidate_data())
        candidate.slides[1].assignments[0].source_refs = ["content_001"]

        errors = validate_candidate(request, build_sources(request), analysis, template_catalog(), candidate)

        self.assertTrue(any("mandatory fact 'product'" in error and "content_002" in error for error in errors))

    def test_multi_slot_slide_with_all_content_cleared_is_empty(self):
        request = planning_request(2, 2)
        analysis = ContentAnalysis.model_validate(analysis_data())
        candidate = PlanCandidate.model_validate(candidate_data())
        candidate.slides[0].assignments[0].action = SlotAction.CLEAR
        candidate.slides[0].assignments[0].content = None
        candidate.slides[0].assignments[0].source_refs = []

        errors = validate_candidate(request, build_sources(request), analysis, template_catalog(), candidate)

        self.assertTrue(any("slide has no authored" in error for error in errors))

    def test_explicitly_named_provided_image_must_be_used(self):
        request = planning_request(2, 2).model_copy(update={
            "content_package": planning_request(2, 2).content_package + "\nПортрет anna.png",
            "provided_images": [ProvidedImage(**{
                "id": "image:anna", "original_name": "anna.png",
                "asset_ref": "opaque.png",
            })],
        })
        request = PlanningRequest.model_validate(request.model_dump(mode="json"))
        candidate = PlanCandidate.model_validate(candidate_data())

        errors = validate_candidate(
            request, build_sources(request), ContentAnalysis.model_validate(analysis_data()),
            template_catalog(), candidate,
        )

        self.assertTrue(any("referenced image 'anna.png'" in error for error in errors))

    def test_planner_catalog_projection_omits_compiler_bindings(self):
        projection = catalog_prompt_projection(template_catalog())
        slot = projection["families"][0]["variants"][0]["slots"][0]
        self.assertNotIn("bindings", slot)
        self.assertEqual(slot["target_shape_ids"], [101])

    def test_planner_catalog_projection_omits_variants_without_editable_slots(self):
        catalog = template_catalog()
        catalog.families[0].variants.append(CatalogVariant(
            slide_number=99, description="Static cover", slots=[], static_shape_ids=[999],
        ))

        projection = catalog_prompt_projection(catalog)

        cover = next(item for item in projection["families"] if item["family_id"] == "cover_basic")
        self.assertEqual([item["slide_number"] for item in cover["variants"]], [1])

    def test_example_generate_input_is_valid(self):
        request = PlanningRequest.model_validate_json(
            DEMO_REQUEST_PATH.read_text(encoding="utf-8")
        )
        self.assertEqual(request, demo_request())
        self.assertIn("Анна Лебедева", request.content_package)
        self.assertEqual((request.slide_count.min, request.slide_count.max), (7, 9))

    def test_source_chunking_is_stable_for_paragraphs_and_list_items(self):
        request = PlanningRequest.model_validate({
            "brief": "Цель",
            "content_package": "Первый абзац\nпродолжается.\n\n- Факт A\n- Факт B",
            "slide_count": {"min": 2, "max": 4},
        })
        sources = build_sources(request)
        self.assertEqual([source.id for source in sources], [
            "brief", "content_001", "content_002", "content_003"
        ])
        self.assertEqual(sources[1].text, "Первый абзац продолжается.")

    def test_validator_rejects_wrong_shape_ids_capacity_and_unsupported_numbers(self):
        request = planning_request()
        sources = build_sources(request)
        analysis = ContentAnalysis.model_validate(analysis_data())
        data = candidate_data()
        data["slides"][0]["assignments"][0]["target_shape_ids"] = [999]
        data["slides"][1]["assignments"][0]["content"]["text"] += " 77%."
        candidate = PlanCandidate.model_validate(data)
        errors = validate_candidate(request, sources, analysis, template_catalog(), candidate)
        self.assertTrue(any("exactly match" in error for error in errors))
        self.assertTrue(any("number 77" in error for error in errors))

        metrics = copy.deepcopy(candidate_data())
        metrics["slides"][1] = {
            "number": 2,
            "purpose": "Метрики",
            "template_family_id": "metrics_pair",
            "template_slide_number": 3,
            "template_class": "metrics",
            "assignments": [{
                "slot_id": "metrics", "kind": "metrics",
                "target_shape_ids": [301, 302, 303, 304],
                "action": "replace", "source_refs": ["content_001"],
                "content": {"kind": "metrics", "items": [
                    {"label": "A", "value": "x"},
                    {"label": "B", "value": "y"},
                    {"label": "C", "value": "z"},
                ]},
            }],
        }
        errors = validate_candidate(
            request, sources, analysis, template_catalog(), PlanCandidate.model_validate(metrics)
        )
        self.assertTrue(any("capacity 2" in error for error in errors))

    def test_validator_normalizes_grouped_numbers_and_decimal_commas(self):
        request = planning_request()
        sources = build_sources(request)
        sources[1].text += (
            " Обработано 2 800 релизов, 18\u00a0000 документов и 12\u202f000 запросов; "
            "успешность 99,4%."
        )
        analysis = ContentAnalysis.model_validate(analysis_data())
        supported = candidate_data()
        supported["deck"]["summary"] = {
            "text": "2800 релизов, 18000 документов, 12000 запросов и 99.4% успешности.",
            "source_refs": ["content_001"],
        }
        errors = validate_candidate(
            request,
            sources,
            analysis,
            template_catalog(),
            PlanCandidate.model_validate(supported),
        )
        self.assertFalse(any("number" in error for error in errors))

        unsupported = copy.deepcopy(supported)
        unsupported["deck"]["summary"]["text"] = "2801 релиз"
        errors = validate_candidate(
            request,
            sources,
            analysis,
            template_catalog(),
            PlanCandidate.model_validate(unsupported),
        )
        self.assertTrue(any("number 2801" in error for error in errors))

    def test_non_image_keep_is_parsed_for_deterministic_normalization(self):
        generated = SlotAssignment.model_validate({
            "slot_id": "hero", "kind": "image", "target_shape_ids": [10],
            "action": "generate", "source_refs": ["brief"],
            "content": {"kind": "image", "prompt": "Абстрактная IT-платформа"},
        })
        self.assertEqual(generated.action.value, "generate")
        keep = SlotAssignment.model_validate({
            "slot_id": "title", "kind": "text", "target_shape_ids": [11],
            "action": "keep", "source_refs": [], "content": None,
        })
        self.assertEqual(keep.action, SlotAction.KEEP)
        invalid_generate = SlotAssignment.model_validate({
            "slot_id": "title", "kind": "text", "target_shape_ids": [11],
            "action": "generate", "source_refs": ["brief"],
            "content": {"kind": "text", "text": "Title"},
        })
        self.assertEqual(invalid_generate.action, SlotAction.GENERATE)

        data = candidate_data()
        data["slides"][0]["assignments"][0] = {
            "slot_id": "title", "kind": "text", "target_shape_ids": [101],
            "action": "keep", "source_refs": [], "content": None,
        }
        data["slides"][0]["assignments"][1] = {
            "slot_id": "subtitle", "kind": "text", "target_shape_ids": [102],
            "action": "keep", "source_refs": [], "content": None,
        }
        candidate = ContentPlanner._canonicalize_candidate(
            template_catalog(), PlanCandidate.model_validate(data), planning_request(),
        )
        self.assertEqual(candidate.slides[0].assignments[1].action, SlotAction.CLEAR)
        errors = validate_candidate(
            planning_request(), build_sources(planning_request()),
            ContentAnalysis.model_validate(analysis_data()), template_catalog(), candidate,
        )
        self.assertTrue(any("non-image slots only support replace or clear" in error for error in errors))


class CatalogTests(unittest.TestCase):
    def test_adds_one_fallback_slot_per_unselected_title_placeholder(self):
        slide = {
            "slide_number": 1, "classification": "content_text", "structures": [],
            "elements": [
                {"shape_id": 1, "type": "text", "placeholder_type": "title",
                 "text": "First", "box": {"width": .8, "height": .1}},
                {"shape_id": 2, "type": "text", "placeholder_type": "center_title",
                 "text": "Second", "box": {"width": .8, "height": .1}},
                {"shape_id": 3, "type": "text", "placeholder_type": "title",
                 "text": "Selected", "box": {"width": .8, "height": .1}},
            ],
        }
        descriptor = CatalogSlideDescriptor.model_validate({
            "slide_number": 1, "classification": "content_text", "description": "Text",
            "family_hint": "text", "family_description": "Text", "slots": [{
                "slot_id": "title", "kind": "text", "role": "title",
                "target": {"target_type": "shape", "shape_id": 3},
            }],
        })

        slots, static = TemplateCatalogBuilder(
            Path("."), llm=FakeLLM(),
        )._expand_descriptor(slide, descriptor)

        self.assertEqual(
            [(slot.slot_id, slot.target_shape_ids) for slot in slots],
            [("title", [3]), ("fallback_title", [1]), ("fallback_title_2", [2])],
        )
        self.assertTrue(all(not slot.required for slot in slots[1:]))
        self.assertTrue(all(slot.bindings[0].value_paths == ["/text"] for slot in slots))
        self.assertEqual(static, [])

    def test_native_table_and_chart_visual_bindings_do_not_require_value_paths(self):
        for element_type in ("table", "chart"):
            slide = {
                "slide_number": 1, "classification": "content_visual", "structures": [],
                "elements": [{
                    "shape_id": 889, "type": element_type,
                    "box": {"width": .7, "height": .5},
                    element_type: ({"rows": 4, "columns": 3} if element_type == "table" else {
                        "chart_type": "LINE", "category_labels": [], "series_names": [],
                    }),
                }],
            }
            descriptor = CatalogSlideDescriptor.model_validate({
                "slide_number": 1, "classification": "content_visual",
                "description": "Native visual", "family_hint": "visual",
                "family_description": "Visual", "slots": [],
            })

            slots, _ = TemplateCatalogBuilder(Path("."), llm=FakeLLM())._expand_descriptor(
                slide, descriptor,
            )

            visual = next(slot for slot in slots if slot.kind == SlotKind.VISUAL)
            self.assertEqual(visual.bindings[0].renderer.value, element_type)
            self.assertEqual(visual.bindings[0].value_paths, [])

    def test_promotes_only_largest_optional_visual_image_slot(self):
        slide = {
            "slide_number": 1, "classification": "content_visual", "structures": [],
            "elements": [
                {"shape_id": 1, "type": "image", "box": {"width": .6, "height": .5}},
                {"shape_id": 2, "type": "image", "box": {"width": .3, "height": .3}},
                {"shape_id": 3, "type": "image", "box": {"width": .8, "height": .6}},
            ],
        }
        descriptor = CatalogSlideDescriptor.model_validate({
            "slide_number": 1, "classification": "content_visual", "description": "Visual",
            "family_hint": "visual", "family_description": "Visual",
            "slots": [
                {"slot_id": "main", "kind": "image", "role": "visual", "required": True,
                 "target": {"target_type": "shape", "shape_id": 1}},
                {"slot_id": "small", "kind": "image", "role": "visual", "required": False,
                 "target": {"target_type": "shape", "shape_id": 2}},
                {"slot_id": "photo", "kind": "image", "role": "photo", "required": False,
                 "target": {"target_type": "shape", "shape_id": 3}},
            ],
        })
        slots, _ = TemplateCatalogBuilder(Path("."), llm=FakeLLM())._expand_descriptor(slide, descriptor)
        by_id = {slot.slot_id: slot for slot in slots}
        self.assertEqual(by_id["main"].kind, SlotKind.VISUAL)
        self.assertFalse(by_id["main"].required)
        self.assertEqual(by_id["small"].kind, SlotKind.IMAGE)
        self.assertEqual(by_id["photo"].kind, SlotKind.IMAGE)

    def test_does_not_promote_cover_image(self):
        slide = {
            "slide_number": 1, "classification": "cover", "structures": [],
            "elements": [{"shape_id": 1, "type": "image", "box": {"width": .8, "height": .6}}],
        }
        descriptor = CatalogSlideDescriptor.model_validate({
            "slide_number": 1, "classification": "cover", "description": "Slide",
            "family_hint": "slide", "family_description": "Slide",
            "slots": [{
                "slot_id": "hero", "kind": "image", "role": "visual", "required": False,
                "target": {"target_type": "shape", "shape_id": 1},
            }],
        })
        slots, _ = TemplateCatalogBuilder(Path("."), llm=FakeLLM())._expand_descriptor(slide, descriptor)
        self.assertEqual(slots[0].kind, SlotKind.IMAGE)

    def test_does_not_promote_image_inside_group(self):
        slide = {
            "slide_number": 1, "classification": "content_visual", "structures": [],
            "elements": [{
                "shape_id": 1, "type": "image", "in_group": True,
                "box": {"width": .8, "height": .6},
            }],
        }
        descriptor = CatalogSlideDescriptor.model_validate({
            "slide_number": 1, "classification": "content_visual", "description": "Slide",
            "family_hint": "slide", "family_description": "Slide",
            "slots": [{
                "slot_id": "hero", "kind": "image", "role": "visual", "required": False,
                "target": {"target_type": "shape", "shape_id": 1},
            }],
        })
        slots, _ = TemplateCatalogBuilder(Path("."), llm=FakeLLM())._expand_descriptor(slide, descriptor)
        self.assertEqual(slots[0].kind, SlotKind.IMAGE)

    def test_promotes_selected_static_visual_image_but_not_other_parts(self):
        slide = {
            "slide_number": 1, "classification": "content_visual",
            "elements": [
                {"shape_id": 1, "type": "image", "box": {"width": .8, "height": .6}},
                {"shape_id": 2, "type": "text", "text": "caption", "box": {"width": .3, "height": .1}},
            ],
            "structures": [{
                "kind": "static_visual", "structure_id": "static_visual_0",
                "shape_ids": [1, 2], "content_shape_ids": [1, 2], "decoration_shape_ids": [],
            }],
        }
        descriptor = CatalogSlideDescriptor.model_validate({
            "slide_number": 1, "classification": "content_visual", "description": "Slide",
            "family_hint": "slide", "family_description": "Slide",
            "slots": [{
                "slot_id": "hero", "kind": "image", "role": "visual", "required": False,
                "target": {"target_type": "shape", "shape_id": 1},
            }],
        })
        slots, static = TemplateCatalogBuilder(Path("."), llm=FakeLLM())._expand_descriptor(slide, descriptor)
        self.assertEqual(slots[0].kind, SlotKind.VISUAL)
        self.assertIn(2, static)


class PlannerVisualPaginationTests(unittest.TestCase):
    @staticmethod
    def catalog() -> TemplateCatalog:
        return TemplateCatalog.model_validate({
            "model": "test", "source_sha256": "hash", "families": [{
                "family_id": "table", "slide_class": "content_visual", "description": "Table",
                "variants": [{"slide_number": 1, "description": "Table", "slots": [{
                    "slot_id": "visual", "kind": "visual", "role": "structured visual",
                    "target_shape_ids": [7], "required": False,
                    "bindings": [{"shape_id": 7, "renderer": "visual"}],
                    "visual_capabilities": {
                        "kinds": ["table"], "render_mode": "container", "width": 1,
                        "height": 1, "aspect_ratio": 1, "max_rows": 9, "max_columns": 3,
                    },
                }], "static_shape_ids": []}],
            }],
        })

    @staticmethod
    def candidate(asset_id: str) -> PlanCandidate:
        return PlanCandidate.model_validate({
            "deck": {
                "title": {"text": "Data", "source_refs": ["brief"]},
                "summary": {"text": "Data", "source_refs": ["brief"]}, "language": "en",
            },
            "slides": [{
                "number": 1, "purpose": "data", "template_family_id": "table",
                "template_slide_number": 1, "template_class": "content_visual",
                "assignments": [{
                    "slot_id": "visual", "kind": "visual", "target_shape_ids": [7],
                    "action": "replace", "source_refs": [f"asset:{asset_id}"],
                    "content": {"kind": "visual", "asset_id": asset_id,
                                "visual_kind": "table"},
                }],
            }],
        })

    def test_paginates_twelve_rows_nine_plus_three_without_loss_and_is_idempotent(self):
        asset = parse_csv_bytes(
            ("id,value\n" + "\n".join(f"{index},{index * 10}" for index in range(12))).encode(),
            "Rows",
        )[0].model_copy(update={"visual_hint": VisualHint(mode="force", kind="table")})
        request = PlanningRequest(
            brief="Data", content_package="Data", structured_assets=[asset],
            slide_count={"min": 1, "max": 3},
        )
        candidate = ContentPlanner._canonicalize_candidate(self.catalog(), self.candidate(asset.id), request)
        paged = ContentPlanner._paginate_candidate(request, self.catalog(), candidate)
        self.assertEqual([item.assignments[0].content.page for item in paged.slides], [1, 2])
        caps = self.catalog().families[0].variants[0].slots[0].visual_capabilities
        rows = [
            row for slide in paged.slides
            for row in materialize_visual(asset, slide.assignments[0].content, caps)["rows"]
        ]
        self.assertEqual(len(rows), 12)
        self.assertEqual(ContentPlanner._paginate_candidate(request, self.catalog(), paged), paged)

        request.slide_count.max = 1
        with self.assertRaises(PlannerFailure) as context:
            ContentPlanner._paginate_candidate(request, self.catalog(), candidate)
        self.assertEqual(context.exception.code, "content_overflow")
        self.assertEqual(context.exception.details["required_slides"], 2)

    def test_rebuilds_complete_pagination_from_lone_page_two(self):
        asset = parse_csv_bytes(
            ("id,value\n" + "\n".join(f"{index},{index * 10}" for index in range(12))).encode(),
            "Rows",
        )[0].model_copy(update={"visual_hint": VisualHint(mode="force", kind="table")})
        request = PlanningRequest(
            brief="Data", content_package="Data", structured_assets=[asset],
            slide_count={"min": 1, "max": 3},
        )
        candidate = self.candidate(asset.id)
        candidate.slides[0].assignments[0].content.page = 2
        candidate = ContentPlanner._canonicalize_candidate(self.catalog(), candidate, request)

        paged = ContentPlanner._paginate_candidate(request, self.catalog(), candidate)

        self.assertEqual([item.assignments[0].content.page for item in paged.slides], [1, 2])
        self.assertEqual(
            [item.assignments[0].content.selected_columns for item in paged.slides],
            [["id", "value"], ["id", "value"]],
        )

    def test_normalizes_lone_out_of_range_page_for_single_page_asset(self):
        asset = parse_csv_bytes(b"id,value\n1,10\n2,20\n", "Rows")[0].model_copy(
            update={"visual_hint": VisualHint(mode="force", kind="table")}
        )
        request = PlanningRequest(
            brief="Data", content_package="Data", structured_assets=[asset],
            slide_count={"min": 1, "max": 2},
        )
        candidate = self.candidate(asset.id)
        candidate.slides[0].assignments[0].content.page = 7
        candidate = ContentPlanner._canonicalize_candidate(self.catalog(), candidate, request)

        paged = ContentPlanner._paginate_candidate(request, self.catalog(), candidate)

        self.assertEqual(len(paged.slides), 1)
        self.assertEqual(paged.slides[0].assignments[0].content.page, 1)

    def test_generate_repairs_lone_page_two_without_structural_revision(self):
        asset = parse_csv_bytes(
            ("id,value\n" + "\n".join(f"{index},{index * 10}" for index in range(12))).encode(),
            "Rows",
        )[0].model_copy(update={"visual_hint": VisualHint(mode="force", kind="table")})
        request = PlanningRequest(
            brief="Data", content_package="Data", structured_assets=[asset],
            slide_count={"min": 1, "max": 3},
        )
        candidate = self.candidate(asset.id)
        candidate.slides[0].assignments[0].content.page = 2
        analysis = {
            "goal": "Present data", "audience": "Leaders", "language": "en",
            "narrative": ["Data"], "recommended_slide_count": 1,
            "facts": [{
                "id": "data", "text": "Data", "source_refs": ["brief"],
                "mandatory": True,
            }],
            "visual_opportunities": [], "visual_candidates": [],
        }
        llm = FakeLLM(
            json.dumps(analysis),
            candidate.model_dump_json(),
            json.dumps(passing_critique()),
        )

        plan = ContentPlanner(llm=llm, catalog=self.catalog()).generate(request)

        self.assertEqual([slide.assignments[0].content.page for slide in plan.slides], [1, 2])
        self.assertEqual(plan.quality.revision_count, 0)
        self.assertEqual(len(llm.prompts), 3)

    def test_clears_invented_asset_from_optional_visual_slot(self):
        request = PlanningRequest(
            brief="Data", content_package="Data", slide_count={"min": 1, "max": 2},
        )
        candidate = self.candidate("financials_table")

        normalized = ContentPlanner._canonicalize_candidate(
            self.catalog(), candidate, request,
        )

        assignment = normalized.slides[0].assignments[0]
        self.assertEqual(assignment.action, SlotAction.CLEAR)
        self.assertIsNone(assignment.content)
        self.assertEqual(assignment.source_refs, [])

    def test_preserves_invented_asset_in_required_visual_slot_for_validation(self):
        catalog = self.catalog()
        catalog.families[0].variants[0].slots[0].required = True
        request = PlanningRequest(
            brief="Data", content_package="Data", slide_count={"min": 1, "max": 2},
        )
        candidate = self.candidate("financials_table")

        normalized = ContentPlanner._canonicalize_candidate(catalog, candidate, request)

        assignment = normalized.slides[0].assignments[0]
        self.assertEqual(assignment.action, SlotAction.REPLACE)
        self.assertEqual(assignment.content.asset_id, "financials_table")

    def test_generate_clears_invented_optional_asset_without_revision(self):
        request = PlanningRequest(
            brief="Data", content_package="Data", slide_count={"min": 1, "max": 2},
        )
        candidate = self.candidate("operations_table")
        analysis = {
            "goal": "Present data", "audience": "Leaders", "language": "en",
            "narrative": ["Data"], "recommended_slide_count": 1,
            "facts": [{
                "id": "data", "text": "Data", "source_refs": ["brief"],
                "mandatory": True,
            }],
            "visual_opportunities": [], "visual_candidates": [],
        }
        llm = FakeLLM(
            json.dumps(analysis),
            candidate.model_dump_json(),
            json.dumps(passing_critique()),
        )

        plan = ContentPlanner(llm=llm, catalog=self.catalog()).generate(request)

        assignment = plan.slides[0].assignments[0]
        self.assertEqual(assignment.action, SlotAction.CLEAR)
        self.assertIsNone(assignment.content)
        self.assertEqual(plan.quality.revision_count, 0)
        self.assertEqual(len(llm.prompts), 3)

    def test_normalizes_and_clears_duplicate_preexpanded_pages(self):
        asset = parse_csv_bytes(
            ("id,value\n" + "\n".join(f"{index},{index * 10}" for index in range(12))).encode(),
            "Rows",
        )[0].model_copy(update={"visual_hint": VisualHint(mode="force", kind="table")})
        request = PlanningRequest(
            brief="Data", content_package="Data", structured_assets=[asset],
            slide_count={"min": 1, "max": 3},
        )
        candidate = self.candidate(asset.id)
        candidate.slides = [candidate.slides[0].model_copy(deep=True) for _ in range(3)]
        for number, (slide, page) in enumerate(zip(candidate.slides, [1, 2, 1]), 1):
            slide.number = number
            slide.assignments[0].content.page = page
        candidate = ContentPlanner._canonicalize_candidate(self.catalog(), candidate, request)

        paged = ContentPlanner._paginate_candidate(request, self.catalog(), candidate)

        assignments = [slide.assignments[0] for slide in paged.slides]
        self.assertEqual(
            [item.content.page for item in assignments if item.content is not None],
            [1, 2],
        )
        self.assertEqual(assignments[2].action, SlotAction.CLEAR)
        self.assertEqual(assignments[2].source_refs, [])

    def test_generate_removes_duplicate_visual_page_without_revision(self):
        asset = parse_csv_bytes(
            ("id,value\n" + "\n".join(f"{index},{index * 10}" for index in range(12))).encode(),
            "Rows",
        )[0].model_copy(update={"visual_hint": VisualHint(mode="force", kind="table")})
        request = PlanningRequest(
            brief="Data", content_package="Data", structured_assets=[asset],
            slide_count={"min": 1, "max": 3},
        )
        candidate = self.candidate(asset.id)
        candidate.slides = [candidate.slides[0].model_copy(deep=True) for _ in range(3)]
        for number, (slide, page) in enumerate(zip(candidate.slides, [1, 2, 1]), 1):
            slide.number = number
            slide.assignments[0].content.page = page
        analysis = {
            "goal": "Present data", "audience": "Leaders", "language": "en",
            "narrative": ["Data"], "recommended_slide_count": 3,
            "facts": [{
                "id": "data", "text": "Data", "source_refs": ["brief"],
                "mandatory": True,
            }],
            "visual_opportunities": [], "visual_candidates": [],
        }
        llm = FakeLLM(
            json.dumps(analysis),
            candidate.model_dump_json(),
            json.dumps(passing_critique()),
        )

        plan = ContentPlanner(llm=llm, catalog=self.catalog()).generate(request)

        assignments = [slide.assignments[0] for slide in plan.slides]
        self.assertEqual(
            [item.content.page for item in assignments if item.content is not None],
            [1, 2],
        )
        self.assertEqual(assignments[2].action, SlotAction.CLEAR)
        self.assertEqual(plan.quality.revision_count, 0)
        self.assertEqual(len(llm.prompts), 3)

    def test_splits_repeated_field_cycles_into_distinct_items(self):
        elements = [
            {"shape_id": shape_id, "text": text}
            for shape_id, text in enumerate(
                ["Name", "Name", "Role", "Role", "Name", "Name", "Role", "Role"],
                start=1,
            )
        ]
        structure = {
            "kind": "repeat", "structure_id": "repeat_0",
            "items": [
                {"index": 0, "fields": {
                    "field_0": 1, "field_1": 3, "field_2": 5, "field_3": 7,
                }},
                {"index": 1, "fields": {
                    "field_0": 2, "field_1": 4, "field_2": 6, "field_3": 8,
                }},
            ],
            "fields": [
                {"field_id": "field_0", "shape_ids": [1, 2]},
                {"field_id": "field_1", "shape_ids": [3, 4]},
                {"field_id": "field_2", "shape_ids": [5, 6]},
                {"field_id": "field_3", "shape_ids": [7, 8]},
            ],
            "content_shape_ids": list(range(1, 9)), "decoration_shape_ids": [],
        }

        repeat = _normalize_repeat_structures(elements, [structure])[0]

        self.assertEqual(len(repeat["items"]), 4)
        self.assertEqual([field["field_id"] for field in repeat["fields"]], [
            "field_0", "field_1",
        ])
        self.assertEqual(repeat["fields"][0]["shape_ids"], [1, 2, 5, 6])
        self.assertEqual(repeat["fields"][1]["shape_ids"], [3, 4, 7, 8])

    def test_drops_unrepresentable_wide_repeat_structure(self):
        elements = [
            {"shape_id": shape_id, "text": f"Field {shape_id}"}
            for shape_id in range(1, 9)
        ]
        structure = {
            "kind": "repeat", "structure_id": "repeat_0",
            "items": [
                {"index": item, "fields": {
                    f"field_{field}": item + field * 2 + 1 for field in range(4)
                }}
                for item in range(2)
            ],
            "fields": [
                {"field_id": f"field_{field}", "shape_ids": [field * 2 + 1, field * 2 + 2]}
                for field in range(4)
            ],
            "content_shape_ids": list(range(1, 9)), "decoration_shape_ids": [],
        }

        self.assertEqual(_normalize_repeat_structures(elements, [structure]), [])

    def test_expands_grid_structure_to_every_cell_binding(self):
        builder = TemplateCatalogBuilder(Path("."), llm=FakeLLM())
        cells = [
            {"row": row, "column": column, "shape_ids": [row * 3 + column + 1]}
            for row in range(3) for column in range(3)
        ]
        slide = {
            "slide_number": 1,
            "elements": [
                {"shape_id": index, "type": "text", "text": "Text", "box": {}}
                for index in range(1, 10)
            ],
            "structures": [{
                "kind": "grid", "structure_id": "grid_0", "rows": 3, "columns": 3,
                "header_rows": 1, "cells": cells,
                "content_shape_ids": list(range(1, 10)), "decoration_shape_ids": [],
            }],
        }
        descriptor = CatalogSlideDescriptor.model_validate({
            "slide_number": 1, "classification": "table", "description": "Table slide",
            "family_hint": "table", "family_description": "Table",
            "slots": [{
                "slot_id": "table", "kind": "table", "role": "data", "required": True,
                "target": {"target_type": "structure", "structure_id": "grid_0"},
            }],
        })
        builder._validate_batch([slide], CatalogBatchResult(slides=[descriptor]))
        slots, static = builder._expand_descriptor(slide, descriptor)
        self.assertEqual((len(slots[0].bindings), slots[0].capacity), (9, 2))
        self.assertEqual(slots[0].bindings[0].value_paths, ["/columns/0"])
        self.assertEqual(slots[0].bindings[-1].value_paths, ["/rows/1/2"])
        self.assertEqual(static, [])

    def test_rejects_unknown_repeat_field_role(self):
        slide = {
            "slide_number": 1, "elements": [],
            "structures": [{
                "kind": "repeat", "structure_id": "repeat_0", "items": [],
                "fields": [{"field_id": "field_0", "shape_ids": [1, 2]}],
                "content_shape_ids": [1, 2], "decoration_shape_ids": [],
            }],
        }
        descriptor = CatalogSlideDescriptor.model_validate({
            "slide_number": 1, "classification": "metrics", "description": "Metrics slide",
            "family_hint": "metrics", "family_description": "Metrics",
            "slots": [{
                "slot_id": "metrics", "kind": "metrics", "role": "metrics",
                "target": {"target_type": "structure", "structure_id": "repeat_0"},
                "field_roles": {"field_0": "title"},
            }],
        })
        with self.assertRaisesRegex(CatalogBuildError, "invalid metrics field roles"):
            TemplateCatalogBuilder._validate_batch([slide], CatalogBatchResult(slides=[descriptor]))

    def test_normalizes_inverted_repeat_field_roles(self):
        slide = {
            "slide_number": 1,
            "elements": [
                {"shape_id": shape_id, "type": "text"}
                for shape_id in range(1, 7)
            ],
            "structures": [{
                "kind": "repeat", "structure_id": "repeat_0", "items": [
                    {"index": 0, "fields": {
                        "field_0": 1, "field_1": 2, "field_2": 3,
                    }},
                    {"index": 1, "fields": {
                        "field_0": 4, "field_1": 5, "field_2": 6,
                    }},
                ],
                "fields": [
                    {"field_id": "field_0", "shape_ids": [1, 4]},
                    {"field_id": "field_1", "shape_ids": [2, 5]},
                    {"field_id": "field_2", "shape_ids": [3, 6]},
                ],
                "content_shape_ids": list(range(1, 7)), "decoration_shape_ids": [],
            }],
        }
        descriptor = CatalogSlideDescriptor.model_validate({
            "slide_number": 1, "classification": "timeline",
            "description": "Timeline", "family_hint": "timeline",
            "family_description": "Timeline",
            "slots": [{
                "slot_id": "events", "kind": "timeline", "role": "events",
                "target": {"target_type": "structure", "structure_id": "repeat_0"},
                "field_roles": {
                    "date": "field_0", "title": "field_1",
                    "description": "field_2",
                },
            }],
        })

        TemplateCatalogBuilder._validate_batch(
            [slide], CatalogBatchResult(slides=[descriptor])
        )

        self.assertEqual(descriptor.slots[0].field_roles, {
            "field_0": "date", "field_1": "title", "field_2": "description",
        })
        slots, static = TemplateCatalogBuilder(
            Path("."), llm=FakeLLM()
        )._expand_descriptor(slide, descriptor)
        self.assertEqual(
            [binding.value_paths for binding in slots[0].bindings],
            [["/items/0/date"], ["/items/0/title"], ["/items/0/description"],
             ["/items/1/date"], ["/items/1/title"], ["/items/1/description"]],
        )
        self.assertEqual(static, [])

    def test_rejects_non_bijective_inverted_repeat_field_roles(self):
        slide = {
            "slide_number": 1, "elements": [],
            "structures": [{
                "kind": "repeat", "structure_id": "repeat_0", "items": [],
                "fields": [
                    {"field_id": "field_0", "shape_ids": []},
                    {"field_id": "field_1", "shape_ids": []},
                    {"field_id": "field_2", "shape_ids": []},
                ],
                "content_shape_ids": [], "decoration_shape_ids": [],
            }],
        }
        invalid_mappings = (
            {"date": "field_0", "title": "field_1"},
            {"field_0": "date", "title": "field_1", "description": "field_2"},
            {"date": "field_0", "title": "field_0", "description": "field_2"},
        )
        for field_roles in invalid_mappings:
            with self.subTest(field_roles=field_roles):
                descriptor = CatalogSlideDescriptor.model_validate({
                    "slide_number": 1, "classification": "timeline",
                    "description": "Timeline", "family_hint": "timeline",
                    "family_description": "Timeline",
                    "slots": [{
                        "slot_id": "events", "kind": "timeline", "role": "events",
                        "target": {
                            "target_type": "structure", "structure_id": "repeat_0",
                        },
                        "field_roles": field_roles,
                    }],
                })
                with self.assertRaisesRegex(
                    CatalogBuildError, "repeat field_roles must map exactly"
                ):
                    TemplateCatalogBuilder._validate_batch(
                        [slide], CatalogBatchResult(slides=[descriptor])
                    )

    def test_normalizes_text_slot_targeting_repeat_to_cards(self):
        slide = {
            "slide_number": 1,
            "elements": [
                {"shape_id": 1, "type": "text"},
                {"shape_id": 2, "type": "text"},
                {"shape_id": 3, "type": "text"},
                {"shape_id": 4, "type": "text"},
            ],
            "structures": [{
                "kind": "repeat", "structure_id": "repeat_0", "items": [
                    {"index": 0, "fields": {"field_0": 1, "field_1": 3}},
                    {"index": 1, "fields": {"field_0": 2, "field_1": 4}},
                ],
                "fields": [
                    {"field_id": "field_0", "shape_ids": [1, 2]},
                    {"field_id": "field_1", "shape_ids": [3, 4]},
                ],
                "content_shape_ids": [1, 2, 3, 4], "decoration_shape_ids": [],
            }],
        }
        descriptor = CatalogSlideDescriptor.model_validate({
            "slide_number": 1, "classification": "content_text",
            "description": "Repeated text", "family_hint": "cards",
            "family_description": "Cards",
            "slots": [{
                "slot_id": "items", "kind": "text", "role": "items",
                "target": {"target_type": "structure", "structure_id": "repeat_0"},
            }],
        })

        TemplateCatalogBuilder._validate_batch(
            [slide], CatalogBatchResult(slides=[descriptor])
        )

        slot = descriptor.slots[0]
        self.assertEqual(slot.kind, SlotKind.CARDS)
        self.assertEqual(slot.field_roles, {"field_0": "title", "field_1": "body"})
        slots, static = TemplateCatalogBuilder(
            Path("."), llm=FakeLLM()
        )._expand_descriptor(slide, descriptor)
        self.assertEqual(slots[0].kind, SlotKind.CARDS)
        self.assertEqual(
            [binding.value_paths for binding in slots[0].bindings],
            [["/items/0/title"], ["/items/0/body"],
             ["/items/1/title"], ["/items/1/body"]],
        )
        self.assertEqual(static, [])

    def create_template(self, root: Path) -> None:
        (root / "slides" / "slide_001").mkdir(parents=True)
        (root / "slides" / "slide_002").mkdir(parents=True)
        manifest = {
            "schema_version": "3.0",
            "source_sha256": "source-hash",
            "slides": [
                {"metadata_path": "slides/slide_001/metadata.json"},
                {"metadata_path": "slides/slide_002/metadata.json"},
            ],
        }
        (root / "presentation.json").write_text(json.dumps(manifest), encoding="utf-8")
        for number, shape_id in ((1, 11), (2, 21)):
            metadata = {
                "slide_number": number,
                "elements": [{
                    "shape_id": shape_id,
                    "type": "text",
                    "placeholder_type": "title",
                    "text": "Заголовок",
                    "box": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.2},
                    "children": [],
                }],
                "structures": [],
                "vlm": {
                    "status": "ok", "classification": "cover",
                    "description": f"Обложка {number}",
                },
            }
            path = root / "slides" / f"slide_{number:03d}" / "metadata.json"
            path.write_text(json.dumps(metadata), encoding="utf-8")

    @staticmethod
    def reduce_response() -> str:
        return json.dumps({"slides": [
            {
                "slide_number": 1,
                "classification": "cover",
                "description": "Описание каталога 1",
                "family_hint": "cover",
                "family_description": "Обложка",
                "slots": [{
                    "slot_id": "title", "kind": "text", "role": "title",
                    "target": {"target_type": "shape", "shape_id": 11}, "required": True,
                }],
            },
            {
                "slide_number": 2,
                "classification": "cover",
                "description": "Описание каталога 2",
                "family_hint": "cover",
                "family_description": "Обложка",
                "slots": [{
                    "slot_id": "title", "kind": "text", "role": "title",
                    "target": {"target_type": "shape", "shape_id": 21}, "required": True,
                }],
            },
        ]}, ensure_ascii=False)

    @staticmethod
    def grouping_response() -> str:
        return json.dumps({"groups": [{
            "family_id": "cover_basic",
            "description": "Обложка",
            "slide_numbers": [1, 2],
        }]}, ensure_ascii=False)

    def test_builds_validated_catalog_and_reuses_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_template(root)
            llm = FakeLLM(self.reduce_response(), self.grouping_response())
            catalog = TemplateCatalogBuilder(root, llm=llm).build()
            self.assertEqual(len(llm.prompts), 2)
            self.assertEqual(len(catalog.families[0].variants), 2)
            self.assertEqual(catalog.families[0].slide_class, SlideClass.COVER)
            self.assertEqual(catalog.families[0].variants[0].description, "Обложка 1")
            self.assertTrue((root / "planner_catalog.json").is_file())

            cached_llm = FakeLLM()
            cached = TemplateCatalogBuilder(root, llm=cached_llm).build()
            self.assertEqual(cached.source_sha256, "source-hash")
            self.assertEqual(cached_llm.prompts, [])

            changed_model = FakeLLM(
                self.reduce_response(), self.grouping_response(), model="new-model"
            )
            rebuilt = TemplateCatalogBuilder(root, llm=changed_model).build()
            self.assertEqual(rebuilt.model, "test-model")
            self.assertEqual(changed_model.prompts, [])

    def test_catalog_calls_include_observability_stage(self):
        class StageLLM(FakeLLM):
            def __init__(self, *responses: str):
                super().__init__(*responses)
                self.stages: list[str] = []

            def complete(self, prompt: str, **kwargs) -> str:
                self.prompts.append(prompt)
                self.stages.append(kwargs["stage"])
                return next(self.responses)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_template(root)
            llm = StageLLM(self.reduce_response(), self.grouping_response())

            TemplateCatalogBuilder(root, llm=llm).build()

            self.assertEqual(
                llm.stages,
                ["catalog slide reduction", "catalog grouping"],
            )

    def test_builds_catalog_without_successful_vlm_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_template(root)
            for number, status in ((1, "disabled"), (2, "error")):
                metadata_path = root / "slides" / f"slide_{number:03d}" / "metadata.json"
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata["vlm"] = {"status": status}
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            response = json.loads(self.reduce_response())
            response["slides"][1]["classification"] = "content_text"
            response["slides"][1]["description"] = "Текстовый слайд из структуры"
            llm = FakeLLM(json.dumps(response, ensure_ascii=False), self.grouping_response())

            catalog = TemplateCatalogBuilder(root, llm=llm).build()

            self.assertEqual(
                [(family.slide_class, family.variants[0].description) for family in catalog.families],
                [
                    (SlideClass.COVER, "Описание каталога 1"),
                    (SlideClass.CONTENT_TEXT, "Текстовый слайд из структуры"),
                ],
            )
            self.assertIn('"classification_hint":null', llm.prompts[0])

    def test_successful_vlm_semantics_take_priority_over_catalog_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_template(root)
            response = json.loads(self.reduce_response())
            for slide in response["slides"]:
                slide["classification"] = "other"
                slide["description"] = "Запасное описание"
            llm = FakeLLM(json.dumps(response, ensure_ascii=False), self.grouping_response())

            catalog = TemplateCatalogBuilder(root, llm=llm).build()

            self.assertEqual(catalog.families[0].slide_class, SlideClass.COVER)
            self.assertEqual(
                [variant.description for variant in catalog.families[0].variants],
                ["Обложка 1", "Обложка 2"],
            )

    def test_splits_mixed_class_family_without_another_llm_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_template(root)
            metadata_path = root / "slides" / "slide_002" / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["vlm"]["classification"] = "content_text"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            llm = FakeLLM(self.reduce_response(), self.grouping_response())

            catalog = TemplateCatalogBuilder(root, llm=llm).build()

            self.assertEqual(len(llm.prompts), 2)
            self.assertEqual(
                [(family.family_id, family.slide_class.value) for family in catalog.families],
                [
                    ("cover_basic_cover", "cover"),
                    ("cover_basic_content_text", "content_text"),
                ],
            )

    def test_rejects_unknown_or_unclassified_shape_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_template(root)
            invalid = json.loads(self.reduce_response())
            invalid["slides"][0]["slots"][0]["target"]["shape_id"] = 999
            llm = FakeLLM(json.dumps(invalid), json.dumps(invalid))
            with self.assertRaisesRegex(CatalogBuildError, "after one repair"):
                TemplateCatalogBuilder(root, llm=llm).build()

    def test_repairs_semantically_incomplete_shape_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_template(root)
            invalid = json.loads(self.reduce_response())
            # Use an unknown ID to exercise schema-valid parsing followed by
            # semantic coverage repair.
            invalid["slides"][0]["slots"][0]["target"]["shape_id"] = 999
            llm = FakeLLM(
                json.dumps(invalid), self.reduce_response(), self.grouping_response()
            )
            catalog = TemplateCatalogBuilder(root, llm=llm).build()
            self.assertEqual(catalog.families[0].variants[0].slots[0].target_shape_ids, [11])
            self.assertIn("ORIGINAL REQUEST", llm.prompts[1])
            self.assertIn("unknown shape_id 999", llm.prompts[1])

    def test_exposes_omitted_text_placeholders_as_optional_fallback_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_template(root)
            incomplete = json.loads(self.reduce_response())
            incomplete["slides"][0]["slots"] = []
            llm = FakeLLM(json.dumps(incomplete), self.grouping_response())

            catalog = TemplateCatalogBuilder(root, llm=llm).build()

            fallback = catalog.families[0].variants[0].slots[0]
            self.assertEqual(fallback.slot_id, "fallback_title")
            self.assertEqual(fallback.kind.value, "text")
            self.assertFalse(fallback.required)
            self.assertEqual(fallback.target_shape_ids, [11])
            self.assertEqual(fallback.bindings[0].value_paths, ["/text"])
            self.assertEqual(len(llm.prompts), 2)

    def test_normalizes_unambiguous_duplicate_shape_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_template(root)
            duplicated = json.loads(self.reduce_response())
            first = duplicated["slides"][0]
            first["slots"].append({
                "slot_id": "duplicate", "kind": "text", "role": "duplicate",
                "target": {"target_type": "shape", "shape_id": 11}, "required": False,
            })
            llm = FakeLLM(json.dumps(duplicated), self.grouping_response())

            llm = FakeLLM(json.dumps(duplicated), self.reduce_response(), self.grouping_response())
            catalog = TemplateCatalogBuilder(root, llm=llm).build()

            variant = catalog.families[0].variants[0]
            self.assertEqual(variant.slots[0].target_shape_ids, [11])
            self.assertEqual(variant.static_shape_ids, [])
            self.assertEqual(len(llm.prompts), 3)

    def test_duplicate_slots_produce_actionable_repair_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_template(root)
            duplicated = json.loads(self.reduce_response())
            duplicated["slides"][0]["slots"].append({
                "slot_id": "subtitle", "kind": "text", "role": "subtitle",
                "target": {"target_type": "shape", "shape_id": 11}, "required": False,
            })
            llm = FakeLLM(
                json.dumps(duplicated), self.reduce_response(), self.grouping_response()
            )

            TemplateCatalogBuilder(root, llm=llm).build()

            self.assertIn("shape:11", llm.prompts[1])
            self.assertIn("slot 'title'", llm.prompts[1])
            self.assertIn("slot 'subtitle'", llm.prompts[1])

    def test_moves_thin_decorative_image_from_slot_to_static(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_template(root)
            metadata_path = root / "slides" / "slide_001" / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["elements"].append({
                "shape_id": 12,
                "type": "image",
                "placeholder_type": None,
                "text": None,
                "box": {"x": 0.1, "y": 0.3, "width": 0.8, "height": 0.002},
                "children": [],
            })
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            invalid = json.loads(self.reduce_response())
            llm = FakeLLM(json.dumps(invalid), self.grouping_response())

            catalog = TemplateCatalogBuilder(root, llm=llm).build()

            variant = catalog.families[0].variants[0]
            self.assertEqual(variant.slots[0].target_shape_ids, [11])
            self.assertEqual([item.shape_id for item in variant.slots[0].bindings], [11])
            self.assertEqual(variant.static_shape_ids, [12])
            self.assertEqual(len(llm.prompts), 2)

    def test_normalizes_image_renderer_for_text_image_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_template(root)
            response = json.loads(self.reduce_response())
            slot = response["slides"][0]["slots"][0]
            slot["kind"] = "image"
            llm = FakeLLM(json.dumps(response), self.grouping_response())

            catalog = TemplateCatalogBuilder(root, llm=llm).build()

            binding = catalog.families[0].variants[0].slots[0].bindings[0]
            self.assertEqual(binding.renderer.value, "image_box")
            self.assertEqual(binding.value_paths, [])
            self.assertEqual(len(llm.prompts), 2)

    def test_normalizes_image_renderer_for_regular_text_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_template(root)
            response = json.loads(self.reduce_response())
            llm = FakeLLM(json.dumps(response), self.grouping_response())

            catalog = TemplateCatalogBuilder(root, llm=llm).build()

            binding = catalog.families[0].variants[0].slots[0].bindings[0]
            self.assertEqual(binding.renderer.value, "text")
            self.assertEqual(binding.value_paths, ["/text"])
            self.assertEqual(len(llm.prompts), 2)

    def test_moves_rasterized_native_graphic_from_slot_to_static(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_template(root)
            metadata_path = root / "slides" / "slide_001" / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["elements"].append({
                "shape_id": 12,
                "type": "image",
                "placeholder_type": None,
                "text": None,
                "box": {"x": 0.5, "y": 0.2, "width": 0.4, "height": 0.6},
                "children": [],
            })
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            invalid = json.loads(self.reduce_response())
            invalid["slides"][0]["slots"].append({
                "slot_id": "chart",
                "kind": "chart",
                "role": "donut chart",
                "target": {"target_type": "shape", "shape_id": 12},
                "required": False,
            })
            llm = FakeLLM(json.dumps(invalid), self.grouping_response())

            catalog = TemplateCatalogBuilder(root, llm=llm).build()

            variant = catalog.families[0].variants[0]
            self.assertEqual([slot.slot_id for slot in variant.slots], ["title"])
            self.assertEqual(variant.static_shape_ids, [12])
            self.assertEqual(len(llm.prompts), 2)

    def test_element_budget_separates_large_slides(self):
        builder = TemplateCatalogBuilder(
            Path("template_model/output/run_2"), llm=FakeLLM(),
            batch_size=6, max_batch_elements=5,
        )
        slides = [
            {"slide_number": 1, "elements": [{}, {}, {}]},
            {"slide_number": 2, "elements": [{}, {}, {}]},
            {"slide_number": 3, "elements": [{}]},
        ]
        batches = list(builder._slide_batches(slides))
        self.assertEqual(
            [[slide["slide_number"] for slide in batch] for batch in batches],
            [[1], [2, 3]],
        )

    @staticmethod
    def reduction_slides(count: int) -> list[dict]:
        return [{
            "slide_number": number,
            "classification": "cover",
            "description": f"Обложка {number}",
            "elements": [{
                "shape_id": number * 10 + 1,
                "type": "text",
                "placeholder_type": "title",
                "text": "Заголовок",
                "box": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.2},
                "in_group": False,
            }],
            "structures": [],
        } for number in range(1, count + 1)]

    def test_incomplete_reduction_splits_batch_and_preserves_slide_order(self):
        class SplittingLLM:
            model = "test-model"

            def __init__(self):
                self.calls: list[list[int]] = []

            def complete(self, prompt: str) -> str:
                payload = json.loads(prompt.rsplit("SLIDES:\n", 1)[1])
                numbers = [slide["slide_number"] for slide in payload]
                self.calls.append(numbers)
                if len(numbers) > 2:
                    raise ResponseIncompleteError("max_output_tokens")
                return json.dumps({"slides": [{
                    "slide_number": slide["slide_number"],
                    "classification": "cover",
                    "description": f"Описание {slide['slide_number']}",
                    "family_hint": "cover",
                    "family_description": "Обложка",
                    "slots": [{
                        "slot_id": "title", "kind": "text", "role": "title",
                        "target": {
                            "target_type": "shape",
                            "shape_id": slide["elements"][0]["shape_id"],
                        },
                        "required": True,
                    }],
                } for slide in reversed(payload)]})

        llm = SplittingLLM()
        builder = TemplateCatalogBuilder(Path("."), llm=llm)

        with self.assertLogs("content_planner.catalog", level="WARNING") as logs:
            result = builder._reduce_batch(self.reduction_slides(4))

        self.assertEqual([item.slide_number for item in result], [1, 2, 3, 4])
        self.assertEqual(llm.calls, [[1, 2, 3, 4], [1, 2], [3, 4]])
        self.assertIn("[1, 2, 3, 4]", logs.output[0])
        self.assertIn("[1, 2] and [3, 4]", logs.output[0])

    def test_incomplete_reduction_recursively_splits_to_single_slides(self):
        class SingletonLLM:
            model = "test-model"

            def complete(self, prompt: str) -> str:
                payload = json.loads(prompt.rsplit("SLIDES:\n", 1)[1])
                if len(payload) > 1:
                    raise ResponseIncompleteError("max_output_tokens")
                slide = payload[0]
                return json.dumps({"slides": [{
                    "slide_number": slide["slide_number"],
                    "classification": "cover", "description": "Описание",
                    "family_hint": "cover", "family_description": "Обложка",
                    "slots": [{
                        "slot_id": "title", "kind": "text", "role": "title",
                        "target": {
                            "target_type": "shape",
                            "shape_id": slide["elements"][0]["shape_id"],
                        },
                    }],
                }]})

        result = TemplateCatalogBuilder(
            Path("."), llm=SingletonLLM()
        )._reduce_batch(self.reduction_slides(4))

        self.assertEqual([item.slide_number for item in result], [1, 2, 3, 4])

    def test_reduction_does_not_split_single_slide_or_other_errors(self):
        class FailingLLM:
            model = "test-model"

            def __init__(self, error: Exception):
                self.error = error
                self.calls = 0

            def complete(self, prompt: str) -> str:
                self.calls += 1
                raise self.error

        for slides, error in (
            (self.reduction_slides(1), ResponseIncompleteError("max_output_tokens")),
            (self.reduction_slides(4), RuntimeError("provider unavailable")),
        ):
            with self.subTest(slide_count=len(slides), error=type(error).__name__):
                llm = FailingLLM(error)
                with self.assertRaises(CatalogBuildError) as raised:
                    TemplateCatalogBuilder(Path("."), llm=llm)._reduce_batch(slides)
                self.assertIs(raised.exception.__cause__, error)
                self.assertEqual(llm.calls, 1)

    def test_real_run_2_requires_reanalysis(self):
        root = Path(__file__).resolve().parents[1] / "template_model" / "output" / "run_2"
        manifest = json.loads((root / "presentation.json").read_text(encoding="utf-8"))
        builder = TemplateCatalogBuilder(root, llm=FakeLLM())
        with self.assertRaisesRegex(CatalogBuildError, "rerun template analysis"):
            builder._load_slides(manifest)


class PlannerTests(unittest.TestCase):
    def test_fallback_skips_empty_cover_and_passes_empty_slide_validation(self):
        catalog = template_catalog()
        catalog.families.insert(0, CatalogFamily(
            family_id="cover_standard",
            slide_class=SlideClass.COVER,
            description="Static cover without editable content",
            variants=[CatalogVariant(
                slide_number=99,
                description="Static cover",
                slots=[],
                static_shape_ids=[999],
            )],
        ))
        request = planning_request(1, 1)

        selection_enums = ContentPlanner._catalog_selection_enums(catalog)
        plan = ContentPlanner(llm=FakeLLM(), catalog=catalog).generate_fallback(request)

        self.assertNotIn("cover_standard", selection_enums["families"])
        self.assertNotIn(99, selection_enums["variants"])
        self.assertEqual(plan.slides[0].template_family_id, "cover_basic")
        self.assertTrue(plan.slides[0].assignments)
        self.assertTrue(any(
            assignment.action == SlotAction.REPLACE and assignment.content is not None
            for assignment in plan.slides[0].assignments
        ))

    def test_chart_asset_schema_allows_known_id_or_null_only(self):
        schema = ContentPlanner._dynamic_schema(
            NarrativePlanCandidate, assets=["orders", "regions"],
        )
        chart_schema = schema["$defs"]["NarrativeSlide"]["properties"]["chart_asset_id"]
        string_branch, null_branch = chart_schema["anyOf"]

        self.assertEqual(string_branch["enum"], ["orders", "regions"])
        self.assertNotIn("unknown", string_branch["enum"])
        self.assertEqual(null_branch, {"type": "null"})
        self.assertNotIn(
            "chart_asset_id", schema["$defs"]["NarrativeSlide"].get("required", []),
        )

    def test_clear_assignment_discards_accidental_empty_text_content(self):
        raw = copy.deepcopy(candidate_data())
        for slide in raw["slides"]:
            slide.pop("number")
            slide.pop("template_class")
            for assignment in slide["assignments"]:
                assignment.pop("kind")
                assignment.pop("target_shape_ids")
        cleared = raw["slides"][0]["assignments"][1]
        cleared["source_refs"] = ["brief"]
        cleared["content"] = {"kind": "text", "text": ""}

        class NativeLLM:
            model = "native-test"

            def complete(self, prompt, **kwargs):
                return json.dumps(raw)

        parsed = ContentPlanner(
            llm=NativeLLM(), catalog=template_catalog(),
        )._complete_model("prompt", NarrativePlanCandidate, "plan draft")
        assignment = parsed.slides[0].assignments[1]
        self.assertIsNone(assignment.content)
        self.assertEqual(assignment.source_refs, [])

    def test_empty_replace_content_is_still_rejected(self):
        raw = copy.deepcopy(candidate_data())
        for slide in raw["slides"]:
            slide.pop("number")
            slide.pop("template_class")
            for assignment in slide["assignments"]:
                assignment.pop("kind")
                assignment.pop("target_shape_ids")
        raw["slides"][0]["assignments"][0]["content"] = {
            "kind": "text", "text": "",
        }

        class NativeLLM:
            model = "native-test"

            def complete(self, prompt, **kwargs):
                return json.dumps(raw)

        with self.assertRaises(PlannerFailure) as raised:
            ContentPlanner(
                llm=NativeLLM(), catalog=template_catalog(),
            )._complete_model("prompt", NarrativePlanCandidate, "plan draft")
        self.assertEqual(raised.exception.code, "invalid_llm_response")

    def test_native_schema_uses_compact_model_contract(self):
        compact = copy.deepcopy(candidate_data())
        for slide in compact["slides"]:
            slide.pop("number")
            slide.pop("template_class")
            for assignment in slide["assignments"]:
                assignment.pop("kind")
                assignment.pop("target_shape_ids")

        class NativeLLM:
            model = "native-test"

            def __init__(self):
                self.responses = iter([json.dumps(analysis_data()), json.dumps(compact)])
                self.schemas = []

            def complete(self, prompt, **kwargs):
                self.schemas.append(kwargs["json_schema"])
                return next(self.responses)

        llm = NativeLLM()
        plan = ContentPlanner(
            llm=llm, catalog=template_catalog(), enable_critique=False,
        ).generate(planning_request())
        draft_schema = llm.schemas[1]
        serialized = json.dumps(draft_schema)
        self.assertNotIn('"target_shape_ids"', serialized)
        self.assertNotIn('"template_class"', serialized)
        self.assertEqual(plan.slides[0].assignments[0].target_shape_ids, [101])
        self.assertEqual(plan.slides[0].template_class, SlideClass.COVER)

    def test_resume_uses_analysis_checkpoint_without_repeating_llm_call(self):
        with tempfile.TemporaryDirectory() as directory:
            first = FakeLLM(json.dumps(analysis_data()))
            planner = ContentPlanner(
                llm=first, catalog=template_catalog(), checkpoint_dir=directory,
            )
            with self.assertRaises(PlannerFailure):
                planner.generate(planning_request())
            self.assertEqual(len(first.prompts), 2)

            resumed = FakeLLM(
                json.dumps(candidate_data()), json.dumps(passing_critique()),
            )
            plan = ContentPlanner(
                llm=resumed, catalog=template_catalog(), checkpoint_dir=directory,
            ).generate(planning_request())
            self.assertEqual(plan.quality.status, "passed")
            self.assertEqual(len(resumed.prompts), 2)

            no_calls = FakeLLM()
            restored = ContentPlanner(
                llm=no_calls, catalog=template_catalog(), checkpoint_dir=directory,
            ).generate(planning_request())
            self.assertEqual(restored, plan)
            self.assertEqual(no_calls.prompts, [])

    def test_happy_path_passes_without_revision(self):
        llm = FakeLLM(
            json.dumps(analysis_data(), ensure_ascii=False),
            json.dumps(candidate_data(), ensure_ascii=False),
            json.dumps(passing_critique(), ensure_ascii=False),
        )
        plan = ContentPlanner(llm=llm, catalog=template_catalog()).generate(planning_request())
        self.assertEqual(len(llm.prompts), 3)
        self.assertEqual(plan.quality, QualityReport(status="passed", revision_count=0))
        self.assertEqual(plan.slides[0].assignments[0].target_shape_ids, [101])
        self.assertEqual(plan.sources[1].id, "content_001")

    def test_critique_triggers_revision(self):
        failing = {
            "scores": {
                "factuality": 5, "coverage": 3, "narrative": 4,
                "template_fit": 5, "conciseness": 5,
            },
            "issues": [{
                "severity": "warning", "message": "Усилить покрытие", "slide_number": 2
            }],
        }
        llm = FakeLLM(
            json.dumps(analysis_data()),
            json.dumps(candidate_data()),
            json.dumps(failing),
            json.dumps(candidate_data()),
            json.dumps(passing_critique()),
        )
        plan = ContentPlanner(llm=llm, catalog=template_catalog()).generate(planning_request())
        self.assertEqual(plan.quality.revision_count, 1)
        self.assertEqual(plan.quality.status, "passed")
        self.assertEqual(len(llm.prompts), 5)

    def test_catalog_owned_shape_ids_are_fixed_without_revision(self):
        invalid = candidate_data()
        invalid["slides"][0]["number"] = 9
        invalid["slides"][0]["assignments"][0]["target_shape_ids"] = [999]
        llm = FakeLLM(
            json.dumps(analysis_data()),
            json.dumps(invalid),
            json.dumps(passing_critique()),
        )
        plan = ContentPlanner(llm=llm, catalog=template_catalog()).generate(planning_request())
        self.assertEqual(plan.quality.revision_count, 0)
        self.assertEqual(plan.slides[0].number, 1)
        self.assertEqual(plan.slides[0].assignments[0].target_shape_ids, [101])
        self.assertEqual(len(llm.prompts), 3)

    def test_structural_and_semantic_revisions_have_independent_budgets(self):
        failing_critique = {
            "scores": {
                "factuality": 5, "coverage": 3, "narrative": 4,
                "template_fit": 5, "conciseness": 5,
            },
            "issues": [{
                "severity": "warning", "message": "Усилить покрытие", "slide_number": 2,
            }],
        }
        structurally_invalid = candidate_data()
        structurally_invalid["slides"][0]["template_family_id"] = "unknown_family"
        llm = FakeLLM(
            json.dumps(analysis_data()),
            json.dumps(candidate_data()),
            json.dumps(failing_critique),
            json.dumps(structurally_invalid),
            json.dumps(candidate_data()),
            json.dumps(passing_critique()),
        )

        plan = ContentPlanner(
            llm=llm,
            catalog=template_catalog(),
            max_revisions=1,
        ).generate(planning_request())

        self.assertEqual(plan.quality.status, "passed")
        self.assertEqual(plan.quality.revision_count, 2)
        self.assertEqual(len(llm.prompts), 6)
        self.assertIn('"code":"unknown_template_family"', llm.prompts[4])
        self.assertIn('"path":"slides[0]"', llm.prompts[4])

    def test_invalid_json_is_repaired_once(self):
        llm = FakeLLM(
            "not-json",
            json.dumps(analysis_data()),
            json.dumps(candidate_data()),
            json.dumps(passing_critique()),
        )
        plan = ContentPlanner(llm=llm, catalog=template_catalog()).generate(planning_request())
        self.assertEqual(plan.quality.status, "passed")
        self.assertIn("INVALID RESPONSE", llm.prompts[1])

    def test_invalid_plan_after_revision_limit_fails(self):
        invalid = candidate_data()
        invalid["slides"][0]["template_family_id"] = "unknown_family"
        llm = FakeLLM(
            json.dumps(analysis_data()),
            json.dumps(invalid),
            json.dumps(invalid),
            json.dumps(invalid),
        )
        with self.assertRaises(PlannerFailure) as context:
            ContentPlanner(llm=llm, catalog=template_catalog()).generate(planning_request())
        self.assertEqual(context.exception.code, "quality_gate_failed")

    def test_fast_mode_skips_semantic_critique(self):
        llm = FakeLLM(
            json.dumps(analysis_data()),
            json.dumps(candidate_data()),
        )
        plan = ContentPlanner(
            llm=llm, catalog=template_catalog(), enable_critique=False
        ).generate(planning_request())
        self.assertEqual(len(llm.prompts), 2)
        self.assertEqual(plan.quality.status, "passed")
        self.assertIn("critique was skipped", plan.quality.warnings[0])

    def test_valid_plan_returns_needs_review_after_two_revisions(self):
        failing = {
            "scores": {
                "factuality": 5, "coverage": 3, "narrative": 3,
                "template_fit": 4, "conciseness": 4,
            },
            "issues": [{"severity": "warning", "message": "Усилить повествование"}],
        }
        llm = FakeLLM(
            json.dumps(analysis_data()), json.dumps(candidate_data()),
            json.dumps(failing), json.dumps(candidate_data()),
            json.dumps(failing), json.dumps(candidate_data()),
            json.dumps(failing),
        )
        plan = ContentPlanner(llm=llm, catalog=template_catalog()).generate(planning_request())
        self.assertEqual(plan.quality.status, "needs_review")
        self.assertEqual(plan.quality.revision_count, 2)
        self.assertEqual(plan.quality.warnings, ["Усилить повествование"])


@unittest.skip("legacy three-full-candidate strict pipeline; covered by test_strict_outline_pipeline")
class StrictParallelPlannerTests(unittest.TestCase):
    @staticmethod
    def narrative_candidate(title: str) -> dict:
        value = copy.deepcopy(candidate_data())
        value["deck"]["title"]["text"] = title
        for slide in value["slides"]:
            slide.pop("number")
            slide.pop("template_class")
            for assignment in slide["assignments"]:
                assignment.pop("kind")
                assignment.pop("target_shape_ids")
        return value

    def test_parallel_waves_are_bounded_and_selection_ignores_completion_order(self):
        owner = self

        class SynchronizedLLM:
            model = "parallel-test"

            def __init__(self):
                self.lock = threading.Lock()
                self.active = 0
                self.maximum = 0
                self.stages: list[str] = []

            def complete(self, prompt: str, **kwargs) -> str:
                stage = kwargs["stage"]
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                    self.stages.append(stage)
                try:
                    if stage == "content analysis":
                        return json.dumps(analysis_data())
                    if stage == "plan draft":
                        if "factual accuracy" in prompt:
                            time.sleep(0.03)
                            title = "Facts candidate"
                        elif "coherent audience-centered" in prompt:
                            time.sleep(0.01)
                            title = "Narrative winner"
                        else:
                            time.sleep(0.02)
                            title = "Template candidate"
                        return json.dumps(owner.narrative_candidate(title))
                    if stage == "plan critique":
                        score = 5 if "Narrative winner" in prompt else (
                            4 if "Template candidate" in prompt else 3
                        )
                        return json.dumps({
                            "scores": {
                                "factuality": 5 if score == 5 else score,
                                "coverage": score,
                                "narrative": score,
                                "template_fit": score,
                                "conciseness": score,
                            },
                            "issues": [],
                        })
                    raise AssertionError(stage)
                finally:
                    with self.lock:
                        self.active -= 1

        llm = SynchronizedLLM()
        plan = ContentPlanner(
            llm=llm, catalog=template_catalog(), max_revisions=0, max_workers=2,
        ).generate(planning_request())

        self.assertEqual(plan.deck.title.text, "Narrative winner")
        self.assertEqual(plan.quality.status, "passed")
        self.assertEqual(llm.maximum, 2)
        self.assertEqual(llm.stages.count("plan draft"), 3)
        self.assertEqual(llm.stages.count("plan critique"), 9)

    def test_invalid_analysis_is_retried_once_and_strict_pipeline_continues(self):
        owner = self
        asset = parse_csv_bytes(
            b"month,value\nJan,10\nFeb,12\n", "Monthly orders",
        )[0].model_copy(update={"visual_hint": VisualHint(mode="none")})
        request = planning_request()
        request.structured_assets = [asset]
        valid_analysis = analysis_data()
        valid_analysis["visual_candidates"] = []
        invalid_analysis = copy.deepcopy(valid_analysis)
        invalid_analysis["visual_candidates"] = [asset.model_dump(mode="json")]

        class RetryLLM:
            model = "analysis-retry-test"

            def __init__(self):
                self.analysis_calls = 0
                self.analysis_prompts: list[str] = []
                self.analysis_schemas: list[dict] = []

            def complete(self, prompt: str, **kwargs) -> str:
                stage = kwargs["stage"]
                if stage == "content analysis":
                    self.analysis_calls += 1
                    self.analysis_prompts.append(prompt)
                    self.analysis_schemas.append(kwargs["json_schema"])
                    return json.dumps(
                        invalid_analysis if self.analysis_calls == 1 else valid_analysis
                    )
                if stage == "plan draft":
                    return json.dumps(owner.narrative_candidate("Grounded candidate"))
                if stage == "plan critique":
                    return json.dumps(passing_critique())
                raise AssertionError(stage)

        original_asset = asset.model_dump(mode="json")
        llm = RetryLLM()
        plan = ContentPlanner(
            llm=llm, catalog=template_catalog(), allow_model_assets=False,
            max_revisions=0, max_workers=2,
        ).generate(request)

        self.assertEqual(plan.quality.status, "passed")
        self.assertEqual(llm.analysis_calls, 2)
        self.assertIn("Return visual_candidates exactly as []", llm.analysis_prompts[0])
        self.assertNotIn("you may also emit a normalized visual_candidate", llm.analysis_prompts[0])
        self.assertIn("RETRY: Return one concise JSON object only", llm.analysis_prompts[1])
        self.assertEqual(
            llm.analysis_schemas[0]["properties"]["visual_candidates"]["maxItems"], 0,
        )
        self.assertEqual(
            llm.analysis_schemas[0]["properties"]["facts"]["maxItems"],
            len(build_sources(request)),
        )
        self.assertEqual(plan.structured_assets[0].model_dump(mode="json"), original_asset)

    def test_two_invalid_analysis_attempts_return_needs_review_fallback(self):
        class InvalidAnalysisLLM:
            model = "analysis-fallback-test"

            def __init__(self):
                self.calls = 0

            def complete(self, prompt: str, **kwargs) -> str:
                self.calls += 1
                return "not-json"

        llm = InvalidAnalysisLLM()
        plan = ContentPlanner(
            llm=llm, catalog=template_catalog(), allow_model_assets=False,
            max_revisions=0,
        ).generate(planning_request())

        self.assertEqual(llm.calls, 2)
        self.assertEqual(plan.quality.status, "needs_review")
        self.assertEqual(plan.quality.warnings, [
            "Использован упрощённый вариант",
            "модель вернула некорректную структуру",
        ])

    def test_no_valid_model_candidate_returns_marked_fallback(self):
        class FailingDraftLLM:
            model = "fallback-test"

            def complete(self, prompt: str, **kwargs) -> str:
                if kwargs["stage"] == "content analysis":
                    return json.dumps(analysis_data())
                raise TimeoutError("temporary model timeout")

        plan = ContentPlanner(
            llm=FailingDraftLLM(), catalog=template_catalog(), max_revisions=0,
        ).generate(planning_request())

        self.assertEqual(plan.quality.status, "needs_review")
        self.assertEqual(plan.quality.warnings[0], "Использован упрощённый вариант")


class CLITests(unittest.TestCase):
    def test_project_paths_are_resolved_outside_project_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("pathlib.Path.cwd", return_value=Path(directory)):
                resolved_input = _resolve_existing_path(
                    Path("content_planner/examples/it_team_request.json")
                )
        self.assertEqual(
            resolved_input,
            PROJECT_ROOT / "content_planner/examples/it_team_request.json",
        )
        self.assertEqual(
            _resolve_output_path(Path("content_planner/output/plan.json")),
            PROJECT_ROOT / "content_planner/output/plan.json",
        )

    def test_demo_writes_plan_atomically_and_refuses_overwrite(self):
        request = planning_request()
        sources = build_sources(request)
        candidate = PlanCandidate.model_validate(candidate_data())
        from content_planner.models import PresentationPlan, TemplateReference
        plan = PresentationPlan(
            model="test-model",
            template=TemplateReference(source_sha256="hash", catalog_version="1.0"),
            deck=candidate.deck,
            sources=sources,
            slides=candidate.slides,
            quality=QualityReport(status="passed", revision_count=0),
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "plan.json"
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch("content_planner.main._generate", return_value=plan),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                args = [
                    "demo", "--template-dir", "template_model/output/run_2",
                    "--output", str(output),
                ]
                self.assertEqual(cli_main(args), 0)
                self.assertEqual(cli_main(args), 2)
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], "5.0")
            self.assertIn('"status": "ok"', stdout.getvalue())
            self.assertIn('"code": "io_error"', stderr.getvalue())

    def test_generate_accepts_positional_input_path(self):
        request = planning_request()
        sources = build_sources(request)
        candidate = PlanCandidate.model_validate(candidate_data())
        from content_planner.models import PresentationPlan, TemplateReference
        plan = PresentationPlan(
            model="test-model",
            template=TemplateReference(source_sha256="hash", catalog_version="1.0"),
            deck=candidate.deck,
            sources=sources,
            slides=candidate.slides,
            quality=QualityReport(status="passed", revision_count=0),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "request.json"
            output_path = root / "plan.json"
            input_path.write_text(request.model_dump_json(), encoding="utf-8")
            with patch("content_planner.main._generate", return_value=plan):
                code = cli_main([
                    "generate", str(input_path),
                    "--template-dir", "template_model/output/run_2",
                    "--output", str(output_path),
                ])
            self.assertEqual(code, 0)
            self.assertTrue(output_path.is_file())


if __name__ == "__main__":
    unittest.main()
def test_quality_report_reads_legacy_fallback_warning_and_new_explicit_kind():
    legacy = QualityReport.model_validate({
        "status": "needs_review", "revision_count": 0,
        "warnings": ["Использован упрощённый вариант"],
    })
    primary = QualityReport(status="needs_review", revision_count=0)

    assert legacy.result_kind == "fallback"
    assert primary.result_kind == "primary"
