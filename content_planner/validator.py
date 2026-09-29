from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from pydantic import BaseModel

from .models import (
    CardsContent,
    ChartContent,
    ContentAnalysis,
    DatasetAsset,
    DiagramAsset,
    ImageSlotContent,
    MetricsContent,
    PlanCandidate,
    PlanningRequest,
    ProcessContent,
    ProfilesContent,
    SlotAction,
    SourceChunk,
    TableContent,
    TemplateCatalog,
    TimelineContent,
    BulletListContent,
    VisualSlotContent,
)
from .catalog import select_chart_canvas
from .structured_assets import StructuredAssetError, choose_visual, paginate_asset


_NUMBER = re.compile(
    r"(?<![\w])[-+]?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)"
    r"(?:[.,]\d+)?(?:[ \u00a0\u202f]?%)?(?![\w])"
)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    path: str
    message: str

    def render(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message

    def as_prompt_dict(self) -> dict[str, str]:
        return {
            "severity": "blocking",
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


def _numbers(value: str) -> list[Decimal]:
    numbers: list[Decimal] = []
    for match in _NUMBER.findall(value):
        normalized = (
            match.replace("%", "")
            .replace(" ", "")
            .replace("\u00a0", "")
            .replace("\u202f", "")
            .replace(",", ".")
        )
        try:
            number = Decimal(normalized)
        except InvalidOperation:
            continue
        if number.is_finite():
            numbers.append(number)
    return numbers


def _same_number(left: Decimal, right: Decimal) -> bool:
    return left == right


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, BaseModel):
        yield from _strings(value.model_dump(mode="python"))
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _content_count(content: BaseModel) -> int:
    if isinstance(content, BulletListContent):
        return len(content.items)
    if isinstance(content, (CardsContent, ProfilesContent, MetricsContent, TimelineContent, ProcessContent)):
        return len(content.items)
    if isinstance(content, TableContent):
        return len(content.rows)
    if isinstance(content, ChartContent):
        return len(content.categories)
    return 1


def validate_analysis(
    request: PlanningRequest,
    sources: list[SourceChunk],
    analysis: ContentAnalysis,
) -> list[str]:
    errors: list[str] = []
    if not request.slide_count.min <= analysis.recommended_slide_count <= request.slide_count.max:
        errors.append(
            "recommended_slide_count must be inside "
            f"[{request.slide_count.min}, {request.slide_count.max}]"
        )
    known = {source.id for source in sources}
    fact_ids = [fact.id for fact in analysis.facts]
    if len(fact_ids) != len(set(fact_ids)):
        errors.append("analysis fact IDs must be unique")
    for index, fact in enumerate(analysis.facts):
        unknown = sorted(set(fact.source_refs) - known)
        if unknown:
            errors.append(f"facts[{index}] has unknown source_refs {unknown}")
    source_map = {source.id: source.text for source in sources}
    for index, asset in enumerate(analysis.visual_candidates):
        unknown = sorted(set(asset.source_refs) - known)
        if not asset.source_refs:
            errors.append(f"visual_candidates[{index}] requires source_refs")
        if unknown:
            errors.append(f"visual_candidates[{index}] has unknown source_refs {unknown}")
        if isinstance(asset, DatasetAsset):
            grounded_value: Any = asset.rows
        elif isinstance(asset, DiagramAsset):
            grounded_value = [
                {"label": node.label, "description": node.description}
                for node in asset.nodes
            ]
        else:
            grounded_value = [
                {"label": item.label, "value": item.value}
                for item in asset.items
            ]
        serialized = json.dumps(grounded_value, ensure_ascii=False)
        errors.extend(
            issue.render() for issue in _validate_numbers(
                serialized, asset.source_refs, source_map, f"visual_candidates[{index}]"
            )
        )
    return errors


def validate_candidate(
    request: PlanningRequest,
    sources: list[SourceChunk],
    analysis: ContentAnalysis,
    catalog: TemplateCatalog,
    candidate: PlanCandidate,
) -> list[str]:
    return [
        issue.render()
        for issue in validate_candidate_issues(request, sources, analysis, catalog, candidate)
    ]


def validate_candidate_issues(
    request: PlanningRequest,
    sources: list[SourceChunk],
    analysis: ContentAnalysis,
    catalog: TemplateCatalog,
    candidate: PlanCandidate,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    def add(code: str, path: str, message: str) -> None:
        issues.append(ValidationIssue(code=code, path=path, message=message))

    count = len(candidate.slides)
    if not request.slide_count.min <= count <= request.slide_count.max:
        add(
            "slide_count",
            "slides",
            f"expected {request.slide_count.min}..{request.slide_count.max}, got {count}",
        )
    expected_numbers = list(range(1, count + 1))
    actual_numbers = [slide.number for slide in candidate.slides]
    if actual_numbers != expected_numbers:
        add(
            "slide_numbering",
            "slides",
            f"numbers must be sequential {expected_numbers}, got {actual_numbers}",
        )

    source_map = {source.id: source.text for source in sources}
    known_sources = set(source_map)
    used_sources: set[str] = set()
    families = {family.family_id: family for family in catalog.families}
    assets = {asset.id: asset for asset in request.structured_assets}
    globally_available: dict[str, tuple[str, str | None] | None] = {}
    all_visual_slots = [
        slot for family in catalog.families for variant in family.variants for slot in variant.slots
        if slot.visual_capabilities is not None
    ]
    for asset in request.structured_assets:
        try:
            preferred = choose_visual(asset)
            globally_available[asset.id] = preferred if preferred and any(
                choose_visual(asset, slot.visual_capabilities) == preferred
                for slot in all_visual_slots
            ) else None
        except StructuredAssetError:
            globally_available[asset.id] = None
    visual_occurrences: dict[str, list[tuple[VisualSlotContent, Any]]] = {}
    chart_asset_occurrences: dict[str, list[int]] = {}
    used_image_refs: set[str] = set()

    for field_name, sourced in (("deck.title", candidate.deck.title), ("deck.summary", candidate.deck.summary)):
        unknown = sorted(set(sourced.source_refs) - known_sources)
        if unknown:
            add("unknown_source_refs", field_name, f"unknown source_refs {unknown}")
        used_sources.update(sourced.source_refs)
        issues.extend(_validate_numbers(sourced.text, sourced.source_refs, source_map, field_name))

    for slide_index, slide in enumerate(candidate.slides):
        path = f"slides[{slide_index}]"
        meaningful = any(
            assignment.action in {SlotAction.REPLACE, SlotAction.GENERATE}
            and assignment.content is not None
            for assignment in slide.assignments
        )
        # A one-slot visual-only template may intentionally retain only static
        # template content after an invented optional asset is removed. Decks
        # with no assignments, or ordinary multi-slot slides with everything
        # cleared, are genuinely empty.
        if not meaningful and (not slide.assignments or len(slide.assignments) > 1):
            add("empty_slide", path, "slide has no authored text, visual, or image content")
        family = families.get(slide.template_family_id)
        if slide.chart_asset_id is not None:
            is_base_page = any(
                isinstance(assignment.content, VisualSlotContent)
                and assignment.content.asset_id == slide.chart_asset_id
                and assignment.content.visual_kind == "chart"
                and assignment.content.page == 1
                for assignment in slide.assignments
            )
            if is_base_page:
                chart_asset_occurrences.setdefault(slide.chart_asset_id, []).append(slide_index)
            chart_asset = assets.get(slide.chart_asset_id)
            if chart_asset is None:
                add("unknown_chart_asset", path, "chart_asset_id references an unknown asset")
            elif not isinstance(chart_asset, DatasetAsset):
                add("chart_asset_not_dataset", path, "chart_asset_id must reference a dataset")
            else:
                try:
                    chart_choice = choose_visual(chart_asset)
                except StructuredAssetError as exc:
                    chart_choice = None
                    add("invalid_visual_hint", path, str(exc))
                if chart_choice is None or chart_choice[0] != "chart":
                    add("chart_asset_not_chart", path, "chart_asset_id must reference a chart dataset")
        if family is None:
            add(
                "unknown_template_family",
                path,
                f"unknown template_family_id {slide.template_family_id!r}",
            )
            continue
        if slide.template_class != family.slide_class:
            add(
                "template_class_mismatch",
                path,
                f"template_class {slide.template_class.value!r} does not match "
                f"family class {family.slide_class.value!r}",
            )
        variant = next(
            (item for item in family.variants if item.slide_number == slide.template_slide_number),
            None,
        )
        if variant is None:
            add(
                "unknown_template_variant",
                path,
                f"slide {slide.template_slide_number} is not a variant of "
                f"{slide.template_family_id}",
            )
            continue

        canvas_slots = [slot for slot in variant.slots if slot.chart_canvas is not None]
        if slide.chart_asset_id is not None:
            chart_asset = assets.get(slide.chart_asset_id)
            selected = None
            if isinstance(chart_asset, DatasetAsset):
                try:
                    choice = choose_visual(chart_asset)
                    selected = select_chart_canvas(catalog, choice[1] or "column") if choice and choice[0] == "chart" else None
                except StructuredAssetError:
                    selected = None
            if selected is not None and (
                family.family_id != selected[0].family_id
                or variant.slide_number != selected[1].slide_number
            ):
                add("chart_canvas_mismatch", path, "slide does not use the deterministic chart canvas")
            if len(canvas_slots) != 1:
                add("chart_canvas_missing", path, "chart slide must contain exactly one chart canvas")
        elif canvas_slots and any(
            assignment.action == SlotAction.REPLACE and assignment.slot_id in {slot.slot_id for slot in canvas_slots}
            for assignment in slide.assignments
        ):
            add("chart_asset_id_missing", path, "a populated chart canvas requires chart_asset_id")

        slots = {slot.slot_id: slot for slot in variant.slots}
        assignment_ids = [assignment.slot_id for assignment in slide.assignments]
        if len(assignment_ids) != len(set(assignment_ids)):
            add("duplicate_slot_assignment", path, "slot assignments must be unique")
        missing = sorted(set(slots) - set(assignment_ids))
        extra = sorted(set(assignment_ids) - set(slots))
        if missing:
            add(
                "missing_slot_assignment",
                path,
                f"every editable slot needs an assignment; missing {missing}",
            )
        if extra:
            add("unknown_slot_assignment", path, f"assignments reference unknown slots {extra}")

        for assignment_index, assignment in enumerate(slide.assignments):
            assignment_path = f"{path}.assignments[{assignment_index}]"
            slot = slots.get(assignment.slot_id)
            if slot is None:
                continue
            if assignment.kind != slot.kind:
                add(
                    "slot_kind_mismatch",
                    assignment_path,
                    f"kind {assignment.kind.value!r} does not match catalog kind {slot.kind.value!r}",
                )
            if assignment.target_shape_ids != slot.target_shape_ids:
                add(
                    "target_shape_ids_mismatch",
                    assignment_path,
                    f"target_shape_ids must exactly match {slot.target_shape_ids}",
                )
            if slot.required and assignment.action == SlotAction.CLEAR:
                add("required_slot_cleared", assignment_path, "required slot cannot be cleared")
            if (
                assignment.action in {SlotAction.KEEP, SlotAction.GENERATE}
                and slot.kind.value != "image"
            ):
                add(
                    "invalid_slot_action", assignment_path,
                    "non-image slots only support replace or clear",
                )
            unknown = sorted(set(assignment.source_refs) - known_sources)
            if unknown:
                add("unknown_source_refs", assignment_path, f"unknown source_refs {unknown}")
            used_sources.update(assignment.source_refs)
            if assignment.content is None:
                continue
            if slot.capacity is not None and _content_count(assignment.content) > slot.capacity:
                add(
                    "slot_capacity_exceeded",
                    assignment_path,
                    f"content exceeds slot capacity {slot.capacity}",
                )
            if slot.max_chars is not None:
                too_long = [text for text in _strings(assignment.content) if len(text) > slot.max_chars]
                if too_long:
                    add(
                        "slot_text_too_long",
                        assignment_path,
                        f"text exceeds max_chars {slot.max_chars}",
                    )
            if isinstance(assignment.content, VisualSlotContent):
                visual_occurrences.setdefault(assignment.content.asset_id, []).append(
                    (assignment.content, slot.visual_capabilities)
                )
                asset = assets.get(assignment.content.asset_id)
                if asset is None:
                    add("unknown_structured_asset", assignment_path, "visual references an unknown asset_id")
                else:
                    try:
                        expected = globally_available.get(asset.id) or choose_visual(
                            asset, slot.visual_capabilities, assignment.content.selected_columns or None,
                        )
                    except StructuredAssetError as exc:
                        add("invalid_visual_hint", assignment_path, str(exc))
                        expected = None
                    if expected is None:
                        add("visualization_disabled", assignment_path, "asset visual_hint forbids visualization")
                    elif (
                        assignment.content.visual_kind != expected[0]
                        or (expected[1] is not None and assignment.content.subtype != expected[1])
                    ):
                        add(
                            "visual_selection_mismatch", assignment_path,
                            f"expected {expected[0]}/{expected[1]}, got {assignment.content.visual_kind}/{assignment.content.subtype}",
                        )
                capabilities = slot.visual_capabilities
                if capabilities and assignment.content.visual_kind not in capabilities.kinds:
                    add(
                        "unsupported_visual_kind",
                        assignment_path,
                        f"slot does not support {assignment.content.visual_kind!r}",
                    )
                if (
                    capabilities and capabilities.subtypes and assignment.content.subtype
                    and assignment.content.subtype not in capabilities.subtypes
                ):
                    add(
                        "unsupported_visual_subtype", assignment_path,
                        f"slot does not support subtype {assignment.content.subtype!r}",
                    )
                expected_ref = f"asset:{assignment.content.asset_id}"
                if expected_ref not in assignment.source_refs:
                    add("asset_ref_not_grounded", assignment_path, f"source_refs must include {expected_ref!r}")
                continue
            serialized = json.dumps(
                assignment.content.model_dump(mode="json"), ensure_ascii=False
            )
            issues.extend(
                _validate_numbers(serialized, assignment.source_refs, source_map, assignment_path)
            )
            if (
                assignment.action == SlotAction.REPLACE
                and isinstance(assignment.content, ImageSlotContent)
                and assignment.content.asset_ref
            ):
                haystack = "\n".join(source_map.get(ref, "") for ref in assignment.source_refs)
                if assignment.content.asset_ref not in haystack:
                    add(
                        "asset_ref_not_grounded",
                        assignment_path,
                        "asset_ref must occur verbatim in a referenced source",
                    )
                used_image_refs.add(assignment.content.asset_ref)

    for asset_index, asset in enumerate(request.structured_assets):
        if asset.visual_hint.mode == "none":
            continue
        try:
            asset_choice = choose_visual(asset)
        except StructuredAssetError:
            asset_choice = None
        if isinstance(asset, DatasetAsset) and asset_choice and asset_choice[0] == "chart":
            chart_slides = chart_asset_occurrences.get(asset.id, [])
            if not chart_slides:
                add(
                    "chart_asset_uncovered", f"structured_assets[{asset_index}]",
                    f"chart asset {asset.id!r} needs exactly one base chart slide",
                )
            elif len(chart_slides) > 1:
                add(
                    "chart_asset_reused", f"structured_assets[{asset_index}]",
                    f"chart asset {asset.id!r} is used by multiple base slides",
                )
        occurrences = visual_occurrences.get(asset.id, [])
        if not occurrences:
            add(
                "structured_asset_uncovered", f"structured_assets[{asset_index}]",
                f"asset {asset.id!r} needs one base visualization",
            )
            continue
        first, capabilities = occurrences[0]
        selected_columns = first.selected_columns or None
        if isinstance(asset, DatasetAsset):
            selected_set = {
                key for item, _ in occurrences for key in item.selected_columns
            }
            selected_columns = [
                column.key for column in asset.columns if column.key in selected_set
            ] or None
        try:
            expected_pages = paginate_asset(
                asset, first.visual_kind, first.subtype, capabilities,
                selected_columns=selected_columns,
            )
        except StructuredAssetError as exc:
            add("invalid_visual_pagination", f"structured_assets[{asset_index}]", str(exc))
            continue
        actual_keys = [
            (
                item.visual_kind, item.subtype, tuple(item.selected_columns),
                tuple(item.selected_nodes), item.page,
            )
            for item, _ in occurrences
        ]
        expected_keys = [
            (
                item.visual_kind, item.subtype, tuple(item.selected_columns),
                tuple(item.selected_nodes), item.page,
            )
            for item in expected_pages
        ]
        if len(actual_keys) != len(set(actual_keys)):
            add(
                "duplicate_visual_page", f"structured_assets[{asset_index}]",
                "visual pagination contains duplicate pages",
            )
        if actual_keys != expected_keys:
            add(
                "incomplete_visual_coverage", f"structured_assets[{asset_index}]",
                f"expected pages {expected_keys}, got {actual_keys}",
            )

    for fact in analysis.facts:
        if (
            fact.mandatory
            and fact.fact_type == "content"
            and not set(fact.source_refs).issubset(used_sources)
        ):
            missing = sorted(set(fact.source_refs) - used_sources)
            add(
                "mandatory_fact_uncovered",
                "",
                f"mandatory fact {fact.id!r} is missing source coverage {missing}",
            )
    mentioned_text = f"{request.brief}\n{request.content_package}".casefold()
    for index, image in enumerate(request.provided_images):
        if image.original_name.casefold() in mentioned_text and image.asset_ref not in used_image_refs:
            add(
                "referenced_image_uncovered",
                f"provided_images[{index}]",
                f"referenced image {image.original_name!r} is not assigned to an image slot",
            )
    return issues


def _validate_numbers(
    value: str,
    refs: list[str],
    source_map: dict[str, str],
    path: str,
) -> list[ValidationIssue]:
    available = [number for ref in refs for number in _numbers(source_map.get(ref, ""))]
    issues: list[ValidationIssue] = []
    for number in _numbers(value):
        if not any(_same_number(number, source) for source in available):
            issues.append(
                ValidationIssue(
                    code="number_not_grounded",
                    path=path,
                    message=f"number {number:g} is absent from referenced sources {refs}",
                )
            )
    return issues
