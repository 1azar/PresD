from __future__ import annotations

import inspect
import json
import logging
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable

from pydantic import BaseModel, ValidationError

from slide_types import SlideClass

from .client import OpenAICompatibleClient, ResponseIncompleteError, TextLLM
from .models import (
    BindingMode,
    BindingRenderer,
    CanvasBounds,
    CATALOG_PROMPT_VERSION,
    CATALOG_SCHEMA_VERSION,
    CatalogBatchResult,
    CatalogBinding,
    CatalogFamily,
    CatalogGroup,
    CatalogGroupingResult,
    CatalogSlot,
    CatalogSlotDescriptor,
    CatalogSlideDescriptor,
    CatalogVariant,
    ChartCanvas,
    SlotKind,
    TemplateCatalog,
    VisualCapabilities,
)


CHART_SUBTYPES = ("bar", "column", "line", "area", "pie", "donut", "scatter")
logger = logging.getLogger(__name__)
_CHART_FAMILIES = {
    "bar": "cartesian", "column": "cartesian", "line": "cartesian",
    "area": "cartesian", "scatter": "cartesian", "pie": "radial", "donut": "radial",
}


def select_chart_canvas(
    catalog: TemplateCatalog, subtype: str,
) -> tuple[CatalogFamily, CatalogVariant, CatalogSlot] | None:
    """Choose a chart canvas deterministically from semantic fit and geometry."""
    candidates: list[tuple[int, float, int, CatalogFamily, CatalogVariant, CatalogSlot]] = []
    family = _CHART_FAMILIES.get(subtype)
    for catalog_family in catalog.families:
        for variant in catalog_family.variants:
            for slot in variant.slots:
                canvas = slot.chart_canvas
                if canvas is None or subtype not in canvas.supported_subtypes:
                    continue
                if subtype in canvas.preferred_subtypes:
                    match = 0
                elif family and any(_CHART_FAMILIES.get(item) == family for item in canvas.preferred_subtypes):
                    match = 1
                else:
                    match = 2
                area = canvas.bounds.width * canvas.bounds.height
                candidates.append((match, -area, variant.slide_number, catalog_family, variant, slot))
    if not candidates:
        return None
    _, _, _, family_item, variant, slot = min(candidates, key=lambda item: item[:3])
    return family_item, variant, slot


class CatalogBuildError(RuntimeError):
    pass


def _clean_json(value: str) -> str:
    value = value.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            value = "\n".join(lines[1:-1])
    return value.strip()


def _validation_errors(exc: ValidationError) -> list[str]:
    return [
        f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
        for item in exc.errors(include_url=False)
    ]


def _flatten_elements(
    elements: Iterable[dict[str, Any]], *, in_group: bool = False,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for element in elements:
        if element.get("type") != "group":
            result.append({
                "shape_id": element["shape_id"],
                "type": element["type"],
                "placeholder_type": element.get("placeholder_type"),
                "name": element.get("name"),
                "alt_text": element.get("alt_text"),
                "text": element.get("text"),
                "box": element.get("box"),
                "table": element.get("table"),
                "chart": element.get("chart"),
                "in_group": in_group,
            })
        result.extend(_flatten_elements(
            element.get("children", []), in_group=in_group or element.get("type") == "group"
        ))
    return result


def _normalize_repeat_structures(
    elements: list[dict[str, Any]], structures: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Make detected repeats representable by the planner content models.

    The geometry detector can see a two-row card layout as N items with two
    copies of every semantic field.  For example, five columns of profiles in
    two rows becomes five items with fields ``name, role, name, role``.  Split
    exact repeated field cycles into ten items with ``name, role`` instead.

    No supported repeat content model has more than three distinct fields.  If
    a wider structure cannot be split unambiguously, leave its shapes raw so
    the reducer can expose individual slots rather than being forced to invent
    an invalid field role.
    """
    element_by_id = {element["shape_id"]: element for element in elements}
    normalized: list[dict[str, Any]] = []
    for structure in structures:
        if structure.get("kind") != "repeat":
            normalized.append(structure)
            continue

        fields = structure.get("fields", [])
        field_count = len(fields)
        if field_count <= 3:
            normalized.append(structure)
            continue

        signatures = []
        for field in fields:
            signatures.append(tuple(
                " ".join(str(element_by_id.get(shape_id, {}).get("text") or "").lower().split())
                for shape_id in field.get("shape_ids", [])
            ))
        period = next(
            (
                size for size in range(1, min(3, field_count) + 1)
                if field_count % size == 0
                and signatures == signatures[:size] * (field_count // size)
            ),
            None,
        )
        if period is None:
            continue

        source_items = sorted(structure.get("items", []), key=lambda item: item["index"])
        cycle_count = field_count // period
        split_items: list[dict[str, Any]] = []
        for cycle_index in range(cycle_count):
            for source_item in source_items:
                split_items.append({
                    "index": len(split_items),
                    "fields": {
                        f"field_{field_index}": source_item["fields"][
                            fields[cycle_index * period + field_index]["field_id"]
                        ]
                        for field_index in range(period)
                    },
                })
        split_fields = [{
            "field_id": f"field_{field_index}",
            "shape_ids": [item["fields"][f"field_{field_index}"] for item in split_items],
        } for field_index in range(period)]
        normalized.append({
            **structure,
            "items": split_items,
            "fields": split_fields,
        })
    return normalized


class TemplateCatalogBuilder:
    def __init__(
        self,
        template_dir: str | Path,
        *,
        llm: TextLLM | None = None,
        prompt_dir: str | Path | None = None,
        cache_path: str | Path | None = None,
        batch_size: int = 6,
        max_batch_elements: int = 120,
        max_workers: int = 4,
    ) -> None:
        self.template_dir = Path(template_dir).resolve()
        self.llm = llm
        prompts = Path(prompt_dir) if prompt_dir else Path(__file__).parent / "prompts"
        self.reduce_prompt = (prompts / "catalog_reduce_v1.txt").read_text(encoding="utf-8")
        self.group_prompt = (prompts / "catalog_group_v1.txt").read_text(encoding="utf-8")
        self.repair_prompt = (prompts / "json_repair_v1.txt").read_text(encoding="utf-8")
        self.cache_path = Path(cache_path) if cache_path else self.template_dir / "planner_catalog.json"
        self.batch_size = batch_size
        self.max_batch_elements = max_batch_elements
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.max_workers = max_workers

    def build(self, *, force: bool = False) -> TemplateCatalog:
        manifest_path = self.template_dir / "presentation.json"
        if not manifest_path.is_file():
            raise CatalogBuildError(f"presentation.json not found in {self.template_dir}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_hash = str(manifest.get("source_sha256", "")).strip()
        if not source_hash:
            raise CatalogBuildError("presentation.json has no source_sha256")

        if not force:
            cached = self._load_cache(source_hash)
            if cached is not None:
                return cached

        raw_slides = self._load_slides(manifest)
        batches = list(self._slide_batches(raw_slides))

        if self.llm is None:
            self.llm = OpenAICompatibleClient()
        assert self.llm is not None

        # Slide reduction requests are independent.  Keeping their result order
        # deterministic lets a large template use several API calls concurrently
        # without changing the catalog output.
        if len(batches) > 1 and self.max_workers > 1:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(batches))) as executor:
                reduced_batches = list(executor.map(self._reduce_batch, batches))
        else:
            reduced_batches = [self._reduce_batch(batch) for batch in batches]
        descriptors = [descriptor for batch in reduced_batches for descriptor in batch]

        descriptors_by_slide = {item.slide_number: item for item in descriptors}
        resolved_slides = [{
            **slide,
            "classification": slide["classification"]
            or descriptors_by_slide[slide["slide_number"]].classification.value,
            "description": slide["description"]
            or descriptors_by_slide[slide["slide_number"]].description,
        } for slide in raw_slides]
        chart_numbers = {
            slide["slide_number"] for slide in resolved_slides
            if slide["classification"] == SlideClass.CHART.value
        }
        # A content-visual slide is a fallback only when the template has no
        # dedicated chart slide. This keeps ordinary visual layouts unchanged
        # whenever a purpose-built chart layout exists.
        canvas_numbers = chart_numbers or {
            slide["slide_number"] for slide in resolved_slides
            if slide["classification"] == SlideClass.CONTENT_VISUAL.value
        }

        grouping_payload = [{
            "slide_number": descriptor.slide_number,
            "classification": next(
                slide["classification"] for slide in resolved_slides
                if slide["slide_number"] == descriptor.slide_number
            ),
            "family_hint": descriptor.family_hint,
            "family_description": descriptor.family_description,
            "slots": [slot.model_dump(mode="json") for slot in descriptor.slots],
        } for descriptor in descriptors]
        grouping_prompt = self._request(
            self.group_prompt,
            CatalogGroupingResult,
            "SLIDE DESCRIPTORS",
            grouping_payload,
        )
        grouping = self._complete_model(
            grouping_prompt,
            CatalogGroupingResult,
            "catalog grouping",
            validator=lambda value: self._validate_grouping(raw_slides, value),
        )
        grouping = self._split_mixed_class_groups(resolved_slides, grouping)

        raw_by_slide = {item["slide_number"]: item for item in resolved_slides}
        families: list[CatalogFamily] = []
        for group in grouping.groups:
            classes = {raw_by_slide[number]["classification"] for number in group.slide_numbers}
            variants = []
            for number in group.slide_numbers:
                descriptor = descriptors_by_slide[number]
                try:
                    slots, static_shape_ids = self._expand_descriptor(
                        raw_by_slide[number], descriptor
                    )
                    if number in canvas_numbers:
                        slots, static_shape_ids = self._with_chart_canvas(
                            raw_by_slide[number], slots, static_shape_ids,
                            dedicated=bool(chart_numbers),
                        )
                    variant = CatalogVariant(
                        slide_number=number,
                        description=raw_by_slide[number]["description"],
                        slots=slots,
                        static_shape_ids=static_shape_ids,
                    )
                except (CatalogBuildError, ValidationError) as exc:
                    raise CatalogBuildError(
                        f"failed to build catalog variant for slide {number}: {exc}"
                    ) from exc
                variants.append(variant)
            families.append(CatalogFamily(
                family_id=group.family_id,
                slide_class=SlideClass(next(iter(classes))),
                description=group.description,
                variants=variants,
            ))

        catalog = TemplateCatalog(
            schema_version=CATALOG_SCHEMA_VERSION,
            prompt_version=CATALOG_PROMPT_VERSION,
            model=self.llm.model,
            source_sha256=source_hash,
            families=families,
        )
        self._write_cache(catalog)
        return catalog

    def _reduce_batch(
        self, batch: list[dict[str, Any]]
    ) -> list[CatalogSlideDescriptor]:
        payload = [{
            "slide_number": slide["slide_number"],
            "classification_hint": slide["classification"],
            "description_hint": slide["description"],
            "elements": slide["elements"],
            "structures": slide["structures"],
        } for slide in batch]
        prompt = self._request(
            self.reduce_prompt,
            CatalogBatchResult,
            "SLIDES",
            payload,
        )
        try:
            result = self._complete_model(
                prompt,
                CatalogBatchResult,
                "catalog slide reduction",
                validator=lambda value: self._validate_batch(batch, value),
            )
        except Exception as exc:
            cause: BaseException | None = exc
            while cause is not None and not isinstance(cause, ResponseIncompleteError):
                cause = cause.__cause__
            if cause is None or len(batch) == 1:
                raise
            midpoint = len(batch) // 2
            left, right = batch[:midpoint], batch[midpoint:]
            logger.warning(
                "catalog reduction response incomplete for slides %s; retrying as %s and %s",
                [slide["slide_number"] for slide in batch],
                [slide["slide_number"] for slide in left],
                [slide["slide_number"] for slide in right],
            )
            return self._reduce_batch(left) + self._reduce_batch(right)
        descriptors_by_number = {
            descriptor.slide_number: descriptor for descriptor in result.slides
        }
        return [
            descriptors_by_number[slide["slide_number"]]
            for slide in batch
        ]

    def _load_cache(self, source_hash: str) -> TemplateCatalog | None:
        if not self.cache_path.is_file():
            return None
        try:
            catalog = TemplateCatalog.model_validate_json(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            return None
        if (
            catalog.source_sha256 != source_hash
            or catalog.schema_version != CATALOG_SCHEMA_VERSION
            or catalog.prompt_version != CATALOG_PROMPT_VERSION
        ):
            return None
        return catalog

    @staticmethod
    def _split_mixed_class_groups(
        raw_slides: list[dict[str, Any]], grouping: CatalogGroupingResult
    ) -> CatalogGroupingResult:
        """Enforce the class boundary locally instead of trusting the LLM.

        A family class is already known from template analysis, so asking the
        grouping model to repair this mechanical constraint only adds latency
        and another failure mode.  Mixed groups are split in source order and
        receive stable class-qualified IDs.
        """
        class_by_slide = {
            slide["slide_number"]: slide["classification"] for slide in raw_slides
        }
        reserved = {group.family_id for group in grouping.groups}
        used: set[str] = set()
        normalized: list[CatalogGroup] = []

        def unique_id(preferred: str) -> str:
            candidate = preferred
            suffix = 2
            while candidate in used or candidate in reserved:
                candidate = f"{preferred}_{suffix}"
                suffix += 1
            used.add(candidate)
            return candidate

        for group in grouping.groups:
            partitions: dict[str, list[int]] = {}
            for number in group.slide_numbers:
                partitions.setdefault(class_by_slide[number], []).append(number)
            if len(partitions) == 1:
                family_id = group.family_id
                if family_id in used:
                    family_id = unique_id(family_id)
                else:
                    used.add(family_id)
                normalized.append(group.model_copy(update={"family_id": family_id}))
                continue
            for slide_class, numbers in partitions.items():
                normalized.append(CatalogGroup(
                    family_id=unique_id(f"{group.family_id}_{slide_class}"),
                    description=group.description,
                    slide_numbers=numbers,
                ))
        return CatalogGroupingResult(groups=normalized)

    def _load_slides(self, manifest: dict[str, Any]) -> list[dict[str, Any]]:
        schema_version = manifest.get("schema_version")
        if schema_version != "3.0":
            raise CatalogBuildError(
                f"template schema {schema_version!r} is unsupported; rerun template analysis "
                "to create a schema 3.0 template directory"
            )
        slides: list[dict[str, Any]] = []
        for entry in manifest.get("slides", []):
            metadata_path = self.template_dir / entry["metadata_path"]
            if not metadata_path.is_file():
                raise CatalogBuildError(f"slide metadata not found: {metadata_path}")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            vlm = metadata.get("vlm", {})
            classification: str | None = None
            description: str | None = None
            if vlm.get("status") == "ok":
                try:
                    classification = SlideClass(vlm.get("classification")).value
                except (TypeError, ValueError):
                    classification = None
                raw_description = vlm.get("description")
                if isinstance(raw_description, str) and raw_description.strip():
                    description = raw_description.strip()
            elements = _flatten_elements(metadata.get("elements", []))
            slides.append({
                "slide_number": metadata["slide_number"],
                "classification": classification,
                "description": description,
                "tags": [str(tag) for tag in vlm.get("tags", []) if str(tag).strip()],
                "top_level_elements": [
                    {
                        "shape_id": element.get("shape_id"),
                        "name": element.get("name"),
                        "alt_text": element.get("alt_text"),
                        "placeholder_type": element.get("placeholder_type"),
                        "text": element.get("text"),
                        "box": element.get("box") or {},
                        "type": element.get("type"),
                        "chart": element.get("chart"),
                    }
                    for element in metadata.get("elements", [])
                ],
                "elements": elements,
                "structures": _normalize_repeat_structures(
                    elements, metadata.get("structures", [])
                ),
            })
        if not slides:
            raise CatalogBuildError("template contains no slides")
        return slides

    @staticmethod
    def _chart_preferences(slide: dict[str, Any]) -> list[str]:
        text = " ".join([
            *(str(value) for value in slide.get("tags", [])),
            str(slide.get("description") or ""),
            *(
                str(element.get("chart") or {})
                for element in slide.get("top_level_elements", [])
                if element.get("type") == "chart"
            ),
        ]).casefold()
        aliases = {
            "donut": ("donut", "doughnut", "кольцев"),
            "pie": ("pie", "кругов"),
            "scatter": ("scatter", "xy_", "точеч"),
            "area": ("area", "област"),
            "line": ("line", "линейн", "линия"),
            "column": ("column", "столбчат", "вертикальн"),
            "bar": ("bar", "полос", "горизонтальн"),
        }
        return [subtype for subtype, tokens in aliases.items() if any(token in text for token in tokens)]

    @classmethod
    def _with_chart_canvas(
        cls, slide: dict[str, Any], slots: list[CatalogSlot],
        static_shape_ids: list[int], *, dedicated: bool,
    ) -> tuple[list[CatalogSlot], list[int]]:
        top_level = slide.get("top_level_elements", slide.get("elements", []))
        top_by_id = {item["shape_id"]: item for item in top_level}
        title_slots = [
            slot for slot in slots
            if slot.kind == SlotKind.TEXT and (
                bool(
                    set(re.findall(r"[a-zа-яё]+", slot.role.casefold()))
                    & {"title", "heading", "header", "заголовок"}
                )
                or any(
                    str(top_by_id.get(shape_id, {}).get("placeholder_type") or "").casefold()
                    in {"title", "center_title"}
                    for shape_id in slot.target_shape_ids
                )
            )
        ]
        if not title_slots:
            return slots, static_shape_ids
        title_ids = {shape_id for slot in title_slots for shape_id in slot.target_shape_ids}
        title_boxes = [
            top_by_id[shape_id].get("box") or {}
            for shape_id in title_ids if shape_id in top_by_id
        ]
        title_bottom = max(
            (float(box.get("y") or 0) + float(box.get("height") or 0) for box in title_boxes),
            default=0,
        )
        canvas_top = max(0.22, title_bottom + 0.03)
        if canvas_top >= 0.90:
            return slots, static_shape_ids
        preserve = set(title_ids)
        marker = re.compile(r"(?:^|[^a-zа-яё])(logo|brand|логотип|бренд)(?:$|[^a-zа-яё])")
        for element in top_level:
            box = element.get("box") or {}
            semantic = f"{element.get('name') or ''} {element.get('alt_text') or ''}".casefold()
            small_footer = (
                float(box.get("y") or 0) >= 0.90
                and float(box.get("height") or 0) <= 0.10
            )
            if marker.search(semantic) or small_footer:
                preserve.add(element["shape_id"])
        chart_slot = CatalogSlot(
            slot_id="chart_visual",
            kind=SlotKind.VISUAL,
            role="native chart canvas",
            target_shape_ids=[],
            required=dedicated,
            bindings=[],
            visual_capabilities=VisualCapabilities(
                kinds=["chart"], subtypes=list(CHART_SUBTYPES), render_mode="container",
                width=.94, height=.90 - canvas_top, aspect_ratio=.94 / (.90 - canvas_top),
                max_items=12,
            ),
            chart_canvas=ChartCanvas(
                bounds=CanvasBounds(x=.03, y=canvas_top, width=.94, height=.90 - canvas_top),
                preserve_shape_ids=sorted(preserve),
                supported_subtypes=list(CHART_SUBTYPES),
                preferred_subtypes=cls._chart_preferences(slide),
            ),
        )
        retained = title_slots if dedicated else [
            slot if slot in title_slots else slot.model_copy(update={"required": False})
            for slot in slots
        ]
        retained = [slot for slot in retained if slot.slot_id != "chart_visual"]
        retained.append(chart_slot)
        owned = {shape_id for slot in retained for shape_id in slot.target_shape_ids}
        return retained, sorted((set(static_shape_ids) & preserve) - owned)

    def _slide_batches(
        self, slides: list[dict[str, Any]]
    ) -> Iterable[list[dict[str, Any]]]:
        """Keep large, element-heavy slides away from one another in LLM requests."""
        batch: list[dict[str, Any]] = []
        element_count = 0
        for slide in slides:
            slide_elements = len(slide["elements"])
            exceeds_count = len(batch) >= self.batch_size
            exceeds_elements = bool(batch) and element_count + slide_elements > self.max_batch_elements
            if exceeds_count or exceeds_elements:
                yield batch
                batch = []
                element_count = 0
            batch.append(slide)
            element_count += slide_elements
        if batch:
            yield batch

    @staticmethod
    def _request(
        instruction: str,
        model: type[BaseModel],
        payload_label: str,
        payload: Any,
    ) -> str:
        schema = json.dumps(model.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return f"{instruction}\n\nOUTPUT JSON SCHEMA:\n{schema}\n\n{payload_label}:\n{data}"

    def _complete_model(
        self,
        prompt: str,
        model: type[BaseModel],
        stage: str,
        validator: Callable[[Any], None] | None = None,
    ) -> Any:
        try:
            raw = self._call_llm(prompt, stage)
        except Exception as exc:
            raise CatalogBuildError(f"LLM failed during {stage}: {exc}") from exc
        try:
            parsed = model.model_validate_json(_clean_json(raw))
            if validator is not None:
                validator(parsed)
            return parsed
        except (ValidationError, ValueError, CatalogBuildError) as exc:
            errors = _validation_errors(exc) if isinstance(exc, ValidationError) else [str(exc)]
            schema = json.dumps(model.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
            repair = (
                f"{self.repair_prompt}\n\nOUTPUT JSON SCHEMA:\n{schema}\n\n"
                f"ORIGINAL REQUEST:\n{prompt}\n\n"
                f"INVALID RESPONSE:\n{raw}\n\nVALIDATION ERRORS:\n"
                f"{json.dumps(errors, ensure_ascii=False)}"
            )
            try:
                repaired = self._call_llm(repair, f"{stage} repair")
                parsed = model.model_validate_json(_clean_json(repaired))
                if validator is not None:
                    validator(parsed)
                return parsed
            except Exception as repair_exc:
                raise CatalogBuildError(
                    f"invalid LLM response during {stage} after one repair: {repair_exc}"
                ) from repair_exc

    def _call_llm(self, prompt: str, stage: str) -> str:
        """Pass observability metadata when the injected client accepts it."""
        parameters = inspect.signature(self.llm.complete).parameters
        supports_metadata = "stage" in parameters or any(
            item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()
        )
        if supports_metadata:
            return self.llm.complete(prompt, stage=stage)
        return self.llm.complete(prompt)

    @staticmethod
    def _validate_batch(batch: list[dict[str, Any]], result: CatalogBatchResult) -> None:
        expected = [slide["slide_number"] for slide in batch]
        actual = [slide.slide_number for slide in result.slides]
        if sorted(actual) != sorted(expected) or len(actual) != len(set(actual)):
            raise CatalogBuildError(f"catalog batch slides must be exactly {expected}; got {actual}")
        by_number = {slide["slide_number"]: slide for slide in batch}
        for descriptor in result.slides:
            slide = by_number[descriptor.slide_number]
            element_ids = {element["shape_id"] for element in slide["elements"]}
            structures = {item["structure_id"]: item for item in slide["structures"]}
            owners: dict[str, str] = {}
            for slot in descriptor.slots:
                target = slot.target
                if target.target_type == "shape":
                    key = f"shape:{target.shape_id}"
                    if target.shape_id not in element_ids:
                        raise CatalogBuildError(
                            f"slide {descriptor.slide_number} slot {slot.slot_id!r} references "
                            f"unknown shape_id {target.shape_id}"
                        )
                    if slot.field_roles:
                        raise CatalogBuildError("field_roles are only valid for repeat structures")
                else:
                    key = f"structure:{target.structure_id}"
                    structure = structures.get(target.structure_id)
                    if structure is None:
                        raise CatalogBuildError(
                            f"slide {descriptor.slide_number} slot {slot.slot_id!r} references "
                            f"unknown structure {target.structure_id!r}"
                        )
                    TemplateCatalogBuilder._normalize_repeat_slot(slot, structure)
                    TemplateCatalogBuilder._validate_structure_slot(slot, structure)
                if key in owners:
                    raise CatalogBuildError(
                        f"slide {descriptor.slide_number} target {key} belongs to both "
                        f"slot {owners[key]!r} and slot {slot.slot_id!r}"
                    )
                owners[key] = slot.slot_id

    @staticmethod
    def _normalize_repeat_slot(
        slot: CatalogSlotDescriptor, structure: dict[str, Any]
    ) -> None:
        """Recover common unambiguous interpretations of a detected repeat.

        A reducer can reverse the field_roles mapping or describe a whole
        repeat structure as a text or bullet-list slot even after JSON repair.
        Correct only mappings whose values form an exact set of stable field
        IDs.  The structure boundary is deterministic, so scalar repeats with
        up to three fields can be represented as a generic cards slot.  More
        specific kinds still retain their model-selected semantics and strict
        checks.
        """
        if structure.get("kind") != "repeat":
            return
        fields = structure.get("fields", [])
        expected_fields = {field["field_id"] for field in fields}
        if (
            set(slot.field_roles) != expected_fields
            and len(slot.field_roles) == len(expected_fields)
            and set(slot.field_roles.values()) == expected_fields
        ):
            slot.field_roles = {
                field_id: role for role, field_id in slot.field_roles.items()
            }

        if slot.kind not in {SlotKind.TEXT, SlotKind.BULLET_LIST}:
            return
        roles = ("title", "body", "value")
        if not fields or len(fields) > len(roles):
            return
        slot.kind = SlotKind.CARDS
        slot.field_roles = {
            field["field_id"]: roles[index] for index, field in enumerate(fields)
        }

    @staticmethod
    def _validate_structure_slot(
        slot: CatalogSlotDescriptor, structure: dict[str, Any]
    ) -> None:
        structure_kind = structure.get("kind")
        if structure_kind == "static_visual":
            raise CatalogBuildError("static_visual structures cannot be editable slots")
        if structure_kind == "grid":
            if slot.kind != SlotKind.TABLE:
                raise CatalogBuildError("grid structures require slot kind 'table'")
            if slot.field_roles:
                raise CatalogBuildError("grid structures do not accept field_roles")
            return
        if structure_kind != "repeat":
            raise CatalogBuildError(f"unsupported structure kind {structure_kind!r}")
        allowed: dict[SlotKind, set[str]] = {
            SlotKind.CARDS: {"title", "body", "value"},
            SlotKind.PROFILES: {"name", "role", "details"},
            SlotKind.METRICS: {"label", "value", "unit"},
            SlotKind.TIMELINE: {"date", "title", "description"},
            SlotKind.PROCESS: {"title", "description"},
        }
        if slot.kind not in allowed:
            raise CatalogBuildError(
                f"repeat structures do not support slot kind {slot.kind.value!r}"
            )
        expected_fields = {field["field_id"] for field in structure.get("fields", [])}
        actual_fields = set(slot.field_roles)
        if actual_fields != expected_fields:
            raise CatalogBuildError(
                f"repeat field_roles must map exactly {sorted(expected_fields)}; "
                f"got {sorted(actual_fields)}"
            )
        unknown_roles = set(slot.field_roles.values()) - allowed[slot.kind]
        if unknown_roles:
            raise CatalogBuildError(
                f"invalid {slot.kind.value} field roles: {sorted(unknown_roles)}"
            )
        roles = list(slot.field_roles.values())
        if len(roles) != len(set(roles)):
            raise CatalogBuildError("repeat field roles must be unique")

    @staticmethod
    def _single_binding(
        slot: CatalogSlotDescriptor, element: dict[str, Any]
    ) -> CatalogBinding:
        element_type = element["type"]
        if slot.kind == SlotKind.VISUAL:
            renderer = (
                BindingRenderer.CHART if element_type == "chart"
                else BindingRenderer.TABLE if element_type == "table"
                else BindingRenderer.VISUAL
            )
            return CatalogBinding(shape_id=element["shape_id"], renderer=renderer)
        if slot.kind == SlotKind.IMAGE:
            renderer = BindingRenderer.IMAGE if element_type == "image" else BindingRenderer.IMAGE_BOX
            return CatalogBinding(shape_id=element["shape_id"], renderer=renderer)
        if element_type in {"table", "chart"}:
            expected = SlotKind(element_type)
            if slot.kind != expected:
                raise CatalogBuildError(
                    f"native {element_type} shape requires slot kind {element_type!r}"
                )
            return CatalogBinding(
                shape_id=element["shape_id"], renderer=BindingRenderer(element_type),
                value_paths=["/"], mode=BindingMode.NATIVE,
            )
        if element_type != "text":
            raise CatalogBuildError(
                f"shape {element['shape_id']} of type {element_type!r} is not editable as {slot.kind.value!r}"
            )
        paragraphs = slot.kind == SlotKind.BULLET_LIST
        return CatalogBinding(
            shape_id=element["shape_id"], renderer=BindingRenderer.TEXT,
            value_paths=["/items" if paragraphs else "/text"],
            mode=BindingMode.PARAGRAPHS if paragraphs else BindingMode.SCALAR,
        )

    @staticmethod
    def _generic_placeholder(element: dict[str, Any]) -> bool:
        text = " ".join(str(element.get("text") or "").lower().split()).strip(".:*")
        return text in {
            "text", "текст", "title", "заголовок", "subtitle", "подзаголовок",
            "description", "описание", "value", "показатель", "insert text",
        }

    @staticmethod
    def _visual_kinds(element: dict[str, Any]) -> list[str]:
        if element["type"] == "chart":
            return ["chart"]
        if element["type"] == "table":
            return ["table"]
        marker = f"{element.get('name') or ''} {element.get('alt_text') or ''}".lower()
        match = re.search(r"presd:visual(?::([a-z_, -]+))?", marker)
        if match and match.group(1):
            values = [value.strip() for value in re.split(r"[, ]+", match.group(1)) if value.strip()]
            allowed = {"table", "chart", "diagram", "pictogram_grid"}
            selected = [value for value in values if value in allowed]
            if selected:
                return selected
        return ["table", "chart", "diagram", "pictogram_grid"]

    @classmethod
    def _visual_capabilities(cls, element: dict[str, Any], render_mode: str) -> VisualCapabilities:
        box = element.get("box") or {}
        width, height = float(box.get("width") or 0.01), float(box.get("height") or 0.01)
        table = element.get("table") or {}
        subtypes = ["bar", "column", "line", "area", "pie", "donut", "scatter", "process", "timeline", "cycle", "hierarchy", "comparison"]
        if element["type"] == "chart":
            native = str((element.get("chart") or {}).get("chart_type") or "").lower()
            aliases = {
                "bar": "bar", "column": "column", "line": "line", "area": "area",
                "pie": "pie", "doughnut": "donut", "xy_": "scatter", "scatter": "scatter",
            }
            selected = next((value for token, value in aliases.items() if token in native), None)
            subtypes = [selected] if selected else []
        elif element["type"] == "table":
            subtypes = []
        return VisualCapabilities(
            kinds=cls._visual_kinds(element),
            subtypes=subtypes,
            render_mode=render_mode,
            width=width,
            height=height,
            aspect_ratio=width / height,
            max_items=12,
            max_rows=max(1, int(table.get("rows", 1)) - 1) if table else None,
            max_columns=int(table.get("columns", 1)) if table else None,
        )

    def _expand_descriptor(
        self, slide: dict[str, Any], descriptor: CatalogSlideDescriptor
    ) -> tuple[list[CatalogSlot], list[int]]:
        element_by_id = {element["shape_id"]: element for element in slide["elements"]}
        structures = {item["structure_id"]: item for item in slide["structures"]}
        slots: list[CatalogSlot] = []
        editable: set[int] = set()
        structurally_owned = {
            shape_id
            for structure in slide["structures"]
            for shape_id in (
                structure.get("shape_ids", [])
                + structure.get("content_shape_ids", [])
                + structure.get("decoration_shape_ids", [])
            )
        }
        for spec in descriptor.slots:
            target = spec.target
            slot_kind = spec.kind
            visual_capabilities = None
            if target.target_type == "shape":
                element = element_by_id[target.shape_id]
                # Rasterized diagrams are never promoted to native data slots.
                if element["type"] == "image" and spec.kind != SlotKind.IMAGE:
                    continue
                binding = self._single_binding(spec, element)
                ids = [target.shape_id]
                capacity = None
                bindings = [binding]
            else:
                structure = structures[target.structure_id]
                if structure["kind"] == "grid":
                    slot_kind = SlotKind.VISUAL
                    cells = sorted(structure["cells"], key=lambda item: (item["row"], item["column"]))
                    header_rows = structure["header_rows"]
                    bindings = []
                    for cell in cells:
                        for shape_id in cell["shape_ids"]:
                            if cell["row"] < header_rows:
                                path = f"/columns/{cell['column']}"
                            else:
                                path = f"/rows/{cell['row'] - header_rows}/{cell['column']}"
                            bindings.append(CatalogBinding(
                                shape_id=shape_id,
                                renderer=BindingRenderer.TEXT,
                                value_paths=[path],
                            ))
                    ids = [binding.shape_id for binding in bindings]
                    capacity = structure["rows"] - header_rows
                    visual_capabilities = VisualCapabilities(
                        kinds=["table"], render_mode="pseudo_table",
                        width=0.01, height=0.01, aspect_ratio=1,
                        max_rows=capacity, max_columns=structure["columns"],
                    )
                else:
                    bindings = []
                    for item in sorted(structure["items"], key=lambda value: value["index"]):
                        for field_id, shape_id in item["fields"].items():
                            role = spec.field_roles[field_id]
                            element = element_by_id[shape_id]
                            if element["type"] != "text":
                                raise CatalogBuildError(
                                    f"repeat field {field_id!r} shape {shape_id} is not text"
                                )
                            bindings.append(CatalogBinding(
                                shape_id=shape_id,
                                renderer=BindingRenderer.TEXT,
                                value_paths=[f"/items/{item['index']}/{role}"],
                                mode=(
                                    BindingMode.PARAGRAPHS
                                    if role == "details" else BindingMode.SCALAR
                                ),
                            ))
                    ids = [binding.shape_id for binding in bindings]
                    capacity = len(structure["items"])
            overlap = editable.intersection(ids)
            if overlap:
                raise CatalogBuildError(f"expanded slots overlap shape_ids {sorted(overlap)}")
            editable.update(ids)
            slots.append(CatalogSlot(
                slot_id=spec.slot_id,
                kind=slot_kind,
                role=spec.role,
                target_shape_ids=ids,
                required=spec.required,
                capacity=capacity,
                max_chars=spec.max_chars,
                bindings=bindings,
                visual_capabilities=visual_capabilities or (
                    self._visual_capabilities(
                        element_by_id[ids[0]],
                        "existing_object" if element_by_id[ids[0]]["type"] in {"chart", "table"} else "container",
                    )
                    if slot_kind == SlotKind.VISUAL and len(ids) == 1 else None
                ),
            ))

        all_ids = set(element_by_id)
        # Reducers commonly describe a large chart placeholder as an optional
        # image.  On explicitly visual slide classes, image slots whose role
        # says "visual" are optional replacement anchors by definition; promote
        # only the largest one and normalize it to optional.  This excludes
        # cover photography, decorative/static structures and grouped parts.
        if slide.get("classification") in {"chart", "content_visual"}:
            promotable: list[tuple[float, CatalogSlot, dict[str, Any]]] = []
            for slot in slots:
                if slot.kind != SlotKind.IMAGE or len(slot.target_shape_ids) != 1:
                    continue
                shape_id = slot.target_shape_ids[0]
                element = element_by_id[shape_id]
                role_tokens = set(re.findall(r"[\wа-яё]+", slot.role.casefold()))
                if (
                    element["type"] != "image"
                    or element.get("in_group", False)
                    or not role_tokens.intersection({"visual", "визуал", "график", "chart"})
                ):
                    continue
                box = element.get("box") or {}
                promotable.append((float(box.get("width") or 0) * float(box.get("height") or 0), slot, element))
            if promotable:
                _, promoted, element = max(promotable, key=lambda item: item[0])
                promoted.kind = SlotKind.VISUAL
                promoted.role = "structured visual"
                promoted.required = False
                promoted.capacity = 12
                promoted.bindings = [CatalogBinding(
                    shape_id=element["shape_id"], renderer=BindingRenderer.VISUAL,
                )]
                promoted.visual_capabilities = self._visual_capabilities(element, "container")

        # Visual slots are deterministic catalog features, so they do not
        # depend on the semantic reducer noticing a chart or a named anchor.
        # Existing native objects and explicit presd:visual markers are safe
        # even when the reducer omitted them.
        unclaimed = all_ids - editable
        native = [
            element_by_id[shape_id] for shape_id in sorted(unclaimed)
            if element_by_id[shape_id]["type"] in {"chart", "table"}
            and shape_id not in structurally_owned
            and not element_by_id[shape_id].get("in_group", False)
        ]
        marked = [
            element_by_id[shape_id] for shape_id in sorted(unclaimed)
            if "presd:visual" in f"{element_by_id[shape_id].get('name') or ''} {element_by_id[shape_id].get('alt_text') or ''}".lower()
            and shape_id not in structurally_owned
            and not element_by_id[shape_id].get("in_group", False)
            and element_by_id[shape_id] not in native
        ]
        candidates = native + marked
        existing_slot_ids = {slot.slot_id for slot in slots}
        for candidate in candidates:
            if candidate["shape_id"] in editable:
                continue
            base = "visual"
            slot_id, suffix = base, 2
            while slot_id in existing_slot_ids:
                slot_id, suffix = f"{base}_{suffix}", suffix + 1
            existing_slot_ids.add(slot_id)
            render_mode = "existing_object" if candidate["type"] in {"chart", "table"} else "container"
            slots.append(CatalogSlot(
                slot_id=slot_id,
                kind=SlotKind.VISUAL,
                role="structured visual",
                target_shape_ids=[candidate["shape_id"]],
                required=False,
                capacity=12,
                bindings=[CatalogBinding(
                    shape_id=candidate["shape_id"],
                    renderer=(
                        BindingRenderer.CHART if candidate["type"] == "chart"
                        else BindingRenderer.TABLE if candidate["type"] == "table"
                        else BindingRenderer.VISUAL
                    ),
                )],
                visual_capabilities=self._visual_capabilities(candidate, render_mode),
            ))
            editable.add(candidate["shape_id"])

        unclaimed = all_ids - editable
        fallback_title_ids = sorted(
            shape_id for shape_id in unclaimed - structurally_owned
            if element_by_id[shape_id]["type"] == "text"
            and str(element_by_id[shape_id].get("placeholder_type") or "").casefold()
            in {"title", "center_title"}
        )
        existing_slot_ids = {slot.slot_id for slot in slots}
        for index, shape_id in enumerate(fallback_title_ids, 1):
            slot_id = "fallback_title" if index == 1 else f"fallback_title_{index}"
            suffix = index + 1
            while slot_id in existing_slot_ids:
                slot_id = f"fallback_title_{suffix}"
                suffix += 1
            existing_slot_ids.add(slot_id)
            slots.append(CatalogSlot(
                slot_id=slot_id,
                kind=SlotKind.TEXT,
                role="optional title placeholder fallback",
                target_shape_ids=[shape_id],
                required=False,
                bindings=[CatalogBinding(
                    shape_id=shape_id,
                    renderer=BindingRenderer.TEXT,
                    value_paths=["/text"],
                )],
            ))
            editable.add(shape_id)

        unclaimed = all_ids - editable
        fallback_ids = sorted(
            shape_id for shape_id in unclaimed - structurally_owned
            if element_by_id[shape_id]["type"] == "text"
            and self._generic_placeholder(element_by_id[shape_id])
        )
        if fallback_ids:
            existing = {slot.slot_id for slot in slots}
            slot_id = "fallback_text"
            suffix = 2
            while slot_id in existing:
                slot_id = f"fallback_text_{suffix}"
                suffix += 1
            if len(fallback_ids) == 1:
                kind, capacity = SlotKind.TEXT, None
                paths = ["/text"]
            else:
                kind, capacity = SlotKind.BULLET_LIST, len(fallback_ids)
                paths = [f"/items/{index}" for index in range(len(fallback_ids))]
            slots.append(CatalogSlot(
                slot_id=slot_id,
                kind=kind,
                role="optional generic placeholder fallback",
                target_shape_ids=fallback_ids,
                required=False,
                capacity=capacity,
                bindings=[CatalogBinding(
                    shape_id=shape_id,
                    renderer=BindingRenderer.TEXT,
                    value_paths=[paths[index]],
                ) for index, shape_id in enumerate(fallback_ids)],
            ))
            editable.update(fallback_ids)
        static = sorted(all_ids - editable)
        return slots, static

    @staticmethod
    def _validate_grouping(
        raw_slides: list[dict[str, Any]], grouping: CatalogGroupingResult
    ) -> None:
        expected = {slide["slide_number"] for slide in raw_slides}
        grouped = [number for group in grouping.groups for number in group.slide_numbers]
        family_ids = [group.family_id for group in grouping.groups]
        if len(family_ids) != len(set(family_ids)):
            raise CatalogBuildError("grouping contains duplicate family_id values")
        if set(grouped) != expected or len(grouped) != len(set(grouped)):
            raise CatalogBuildError("grouping must contain every slide exactly once")

    def _write_cache(self, catalog: TemplateCatalog) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=f".{self.cache_path.name}.",
                suffix=".tmp",
                dir=self.cache_path.parent,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(catalog.model_dump_json(indent=2))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.cache_path)
        except Exception:
            if temporary and temporary.exists():
                temporary.unlink()
            raise


def catalog_prompt_projection(catalog: TemplateCatalog) -> dict[str, Any]:
    families = []
    for family in catalog.families:
        variants = [variant for variant in family.variants if variant.slots]
        if not variants:
            continue
        families.append({
            "family_id": family.family_id,
            "slide_class": family.slide_class.value,
            "description": family.description,
            "variants": [{
                "slide_number": variant.slide_number,
                "description": variant.description,
                # Bindings are renderer/compiler details.  Omitting them keeps
                # the planning prompt substantially smaller without removing
                # anything the model needs to choose and populate a variant.
                "slots": [
                    slot.model_dump(mode="json", exclude={"bindings"})
                    for slot in variant.slots
                ],
            } for variant in variants],
        })
    return {
        "source_sha256": catalog.source_sha256,
        "families": families,
    }
