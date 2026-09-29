from __future__ import annotations

from typing import Any

from .models import (
    BindingMode,
    BindingRenderer,
    CatalogBinding,
    CatalogSlot,
    ImageSlotContent,
    MissingAction,
    RenderOperation,
    SlotAction,
    SlotAssignment,
    StructuredAsset,
    VisualSlotContent,
)
from .structured_assets import asset_hash, choose_visual, materialize_visual


class CompilationError(ValueError):
    pass


def _pointer(value: Any, pointer: str) -> Any:
    current = value
    for raw in pointer.lstrip("/").split("/") if pointer != "/" else []:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError) as exc:
                raise KeyError(pointer) from exc
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            raise KeyError(pointer)
    return current


def _binding_value(binding: CatalogBinding, content: dict[str, Any]) -> tuple[list[str] | None, dict[str, Any] | None]:
    values: list[Any] = []
    for path in binding.value_paths:
        try:
            value = _pointer(content, path)
        except KeyError:
            continue
        if value is not None and value != "":
            values.append(value)

    if binding.mode == BindingMode.NATIVE:
        return None, values[0] if values and isinstance(values[0], dict) else None
    if binding.mode == BindingMode.PARAGRAPHS:
        paragraphs: list[str] = []
        for value in values:
            if isinstance(value, list):
                paragraphs.extend(str(item) for item in value if item is not None and str(item))
            else:
                paragraphs.append(str(value))
        return paragraphs or None, None
    if not values:
        return None, None
    return [binding.separator.join(str(value) for value in values)], None


def compile_assignment(
    slot: CatalogSlot,
    assignment: SlotAssignment,
    assets: dict[str, StructuredAsset] | None = None,
) -> list[RenderOperation]:
    if slot.chart_canvas is not None:
        if assignment.action == SlotAction.CLEAR:
            return []
        if not isinstance(assignment.content, VisualSlotContent):
            raise CompilationError(f"chart canvas slot {slot.slot_id!r} requires visual content")
        asset = (assets or {}).get(assignment.content.asset_id)
        if asset is None:
            raise CompilationError(
                f"slot {slot.slot_id!r} references unknown asset {assignment.content.asset_id!r}"
            )
        if assignment.content.visual_kind != "chart" or not assignment.content.subtype:
            raise CompilationError("chart canvas requires a chart subtype")
        expected = choose_visual(
            asset, slot.visual_capabilities, assignment.content.selected_columns or None,
        )
        if expected != ("chart", assignment.content.subtype):
            raise CompilationError(
                f"asset {asset.id!r} requires visual {expected!r}, not chart/{assignment.content.subtype}"
            )
        if assignment.content.subtype not in slot.chart_canvas.supported_subtypes:
            raise CompilationError(
                f"chart canvas does not support subtype {assignment.content.subtype!r}"
            )
        data = materialize_visual(asset, assignment.content, slot.visual_capabilities)
        return [RenderOperation(
            shape_id=None,
            renderer=BindingRenderer.CANVAS,
            action=SlotAction.REPLACE,
            data=data,
            asset_id=asset.id,
            asset_hash=asset_hash(asset),
            visual_kind="chart",
            visual_subtype=assignment.content.subtype,
            canvas_bounds=slot.chart_canvas.bounds,
            preserve_shape_ids=list(slot.chart_canvas.preserve_shape_ids),
        )]
    if not slot.bindings:
        return []
    operations: list[RenderOperation] = []
    for binding in slot.bindings:
        if assignment.action in {SlotAction.CLEAR, SlotAction.KEEP}:
            operations.append(RenderOperation(
                shape_id=binding.shape_id,
                renderer=binding.renderer,
                action=assignment.action,
                image_fit=binding.image_fit,
            ))
            continue

        if isinstance(assignment.content, ImageSlotContent):
            operations.append(RenderOperation(
                shape_id=binding.shape_id,
                renderer=binding.renderer,
                action=assignment.action,
                asset_ref=assignment.content.asset_ref,
                prompt=assignment.content.prompt,
                alt_text=assignment.content.alt_text,
                image_fit=binding.image_fit,
            ))
            continue

        if isinstance(assignment.content, VisualSlotContent):
            asset = (assets or {}).get(assignment.content.asset_id)
            if asset is None:
                raise CompilationError(
                    f"slot {slot.slot_id!r} references unknown asset {assignment.content.asset_id!r}"
                )
            expected = choose_visual(
                asset, slot.visual_capabilities,
                assignment.content.selected_columns or None,
            )
            if expected is None:
                raise CompilationError(f"asset {asset.id!r} disables visualization")
            expected_kind, expected_subtype = expected
            if assignment.content.visual_kind != expected_kind:
                raise CompilationError(
                    f"asset {asset.id!r} requires visual kind {expected_kind!r}"
                )
            if expected_subtype and assignment.content.subtype != expected_subtype:
                raise CompilationError(
                    f"asset {asset.id!r} requires visual subtype {expected_subtype!r}"
                )
            capabilities = slot.visual_capabilities
            if capabilities and assignment.content.visual_kind not in capabilities.kinds:
                raise CompilationError(
                    f"slot {slot.slot_id!r} does not support {assignment.content.visual_kind!r}"
                )
            if (
                capabilities and capabilities.subtypes and assignment.content.subtype
                and assignment.content.subtype not in capabilities.subtypes
            ):
                raise CompilationError(
                    f"slot {slot.slot_id!r} does not support subtype {assignment.content.subtype!r}"
                )
            data = materialize_visual(asset, assignment.content, slot.visual_capabilities)
            renderer = binding.renderer
            if renderer == BindingRenderer.CHART and assignment.content.visual_kind != "chart":
                raise CompilationError("native chart binding requires chart visualization")
            if renderer == BindingRenderer.TABLE and assignment.content.visual_kind != "table":
                raise CompilationError("native table binding requires table visualization")
            if renderer == BindingRenderer.TEXT:
                text, _ = _binding_value(binding, data)
                action = SlotAction.REPLACE if text is not None else (
                    SlotAction.CLEAR if binding.missing_action == MissingAction.CLEAR else SlotAction.KEEP
                )
                operations.append(RenderOperation(
                    shape_id=binding.shape_id,
                    renderer=renderer,
                    action=action,
                    text=text,
                    asset_id=asset.id if action == SlotAction.REPLACE else None,
                    asset_hash=asset_hash(asset) if action == SlotAction.REPLACE else None,
                    visual_kind=assignment.content.visual_kind if action == SlotAction.REPLACE else None,
                    visual_subtype=assignment.content.subtype if action == SlotAction.REPLACE else None,
                ))
                continue
            operations.append(RenderOperation(
                shape_id=binding.shape_id,
                renderer=renderer,
                action=SlotAction.REPLACE,
                data=data,
                asset_id=asset.id,
                asset_hash=asset_hash(asset),
                visual_kind=assignment.content.visual_kind,
                visual_subtype=assignment.content.subtype,
            ))
            continue

        if assignment.content is None:
            raise CompilationError(f"slot {slot.slot_id!r} has no content")
        content = assignment.content.model_dump(mode="json")
        text, data = _binding_value(binding, content)
        if text is None and data is None:
            action = SlotAction.CLEAR if binding.missing_action == MissingAction.CLEAR else SlotAction.KEEP
            operations.append(RenderOperation(
                shape_id=binding.shape_id,
                renderer=binding.renderer,
                action=action,
                image_fit=binding.image_fit,
            ))
            continue
        operations.append(RenderOperation(
            shape_id=binding.shape_id,
            renderer=binding.renderer,
            action=SlotAction.REPLACE,
            text=text,
            data=data,
            image_fit=binding.image_fit,
        ))
    return operations


def compile_slide(
    slide: Any,
    variant: Any,
    assets: list[StructuredAsset] | dict[str, StructuredAsset] | None = None,
) -> list[RenderOperation]:
    asset_map = assets if isinstance(assets, dict) else {asset.id: asset for asset in (assets or [])}
    slots = {slot.slot_id: slot for slot in variant.slots}
    operations: list[RenderOperation] = []
    for assignment in slide.assignments:
        slot = slots.get(assignment.slot_id)
        if slot is None:
            raise CompilationError(f"unknown slot {assignment.slot_id!r}")
        operations.extend(compile_assignment(slot, assignment, asset_map))
    ids = [operation.shape_id for operation in operations if operation.shape_id is not None]
    if len(ids) != len(set(ids)):
        raise CompilationError("compiled render operations contain duplicate shape_id values")
    # Canvas cleanup happens before shape-bound operations are applied. Preserve every
    # operation target (including CLEAR targets) so preflight and rendering can still
    # resolve them; their requested action will remove or empty them afterwards.
    for operation in operations:
        if operation.renderer == BindingRenderer.CANVAS:
            operation.preserve_shape_ids = sorted(set(operation.preserve_shape_ids) | set(ids))
    return operations
