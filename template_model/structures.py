from __future__ import annotations

from statistics import median
from typing import Iterable

from .models import (
    GridCell,
    GridStructure,
    RepeatField,
    RepeatItem,
    RepeatStructure,
    SlideElement,
    SlideStructure,
    StaticVisualStructure,
)


def _flatten(elements: Iterable[SlideElement]) -> list[SlideElement]:
    result: list[SlideElement] = []
    for element in elements:
        if element.type != "group":
            result.append(element)
        result.extend(_flatten(element.children))
    return result


def _center(element: SlideElement, axis: str) -> float:
    if axis == "x":
        return element.box.x + element.box.width / 2
    return element.box.y + element.box.height / 2


def _cluster(elements: list[SlideElement], axis: str, tolerance: float) -> list[list[SlideElement]]:
    groups: list[list[SlideElement]] = []
    for element in sorted(elements, key=lambda item: (_center(item, axis), item.shape_id)):
        if not groups:
            groups.append([element])
            continue
        group_center = sum(_center(item, axis) for item in groups[-1]) / len(groups[-1])
        if abs(_center(element, axis) - group_center) <= tolerance:
            groups[-1].append(element)
        else:
            groups.append([element])
    return groups


def _aligned(first: list[SlideElement], other: list[SlideElement], axis: str) -> bool:
    if len(first) != len(other):
        return False
    first = sorted(first, key=lambda item: _center(item, axis))
    other = sorted(other, key=lambda item: _center(item, axis))
    def anchor(item: SlideElement) -> float:
        return item.box.x if axis == "x" else item.box.y

    return all(abs(anchor(left) - anchor(right)) <= 0.035 for left, right in zip(first, other))


def _regular(values: list[float]) -> bool:
    if len(values) < 3:
        return True
    gaps = [right - left for left, right in zip(values, values[1:])]
    typical = median(gaps)
    return typical > 0.01 and all(abs(gap - typical) <= max(0.018, typical * 0.35) for gap in gaps)


def _grid_decorations(
    elements: list[SlideElement], content_ids: set[int], cells: list[SlideElement]
) -> list[int]:
    left = min(item.box.x for item in cells)
    right = max(item.box.x + item.box.width for item in cells)
    top = min(item.box.y for item in cells)
    bottom = max(item.box.y + item.box.height for item in cells)
    result: list[int] = []
    for element in elements:
        if element.shape_id in content_ids or element.type != "image":
            continue
        box = element.box
        thin = min(box.width, box.height) <= 0.012
        crosses = (
            box.x <= right and box.x + box.width >= left
            and box.y <= bottom and box.y + box.height >= top
        )
        contains = (
            box.x <= left + 0.02 and box.x + box.width >= right - 0.02
            and box.y <= top + 0.02 and box.y + box.height >= bottom - 0.02
        )
        if crosses and (thin or contains):
            result.append(element.shape_id)
    return sorted(result)


def _detect_grids(elements: list[SlideElement], occupied: set[int]) -> list[GridStructure]:
    candidates = [item for item in elements if item.type == "text" and item.shape_id not in occupied]
    if len(candidates) < 6:
        return []
    tolerance = max(0.009, min(0.025, median(item.box.height for item in candidates) * 0.45))
    row_groups = [
        sorted(group, key=lambda item: (_center(item, "x"), item.shape_id))
        for group in _cluster(candidates, "y", tolerance)
        if len(group) >= 2
    ]
    structures: list[GridStructure] = []
    while row_groups:
        best: list[list[SlideElement]] = []
        for anchor in row_groups:
            aligned = [row for row in row_groups if _aligned(anchor, row, "x")]
            aligned.sort(key=lambda row: sum(_center(item, "y") for item in row) / len(row))
            if len(aligned) >= 3 and _regular([
                sum(_center(item, "y") for item in row) / len(row) for row in aligned
            ]):
                if sum(map(len, aligned)) > sum(map(len, best)):
                    best = aligned
        if not best:
            break
        if sum(map(len, best)) < len(candidates) * 0.75:
            # A small regular subset inside a heterogeneous layout is not enough
            # evidence for a table; leave the ambiguous shapes raw.
            break
        columns = len(best[0])
        content = [item for row in best for item in row]
        content_ids = {item.shape_id for item in content}
        decorations = _grid_decorations(elements, content_ids, content)
        cells = [
            GridCell(row=row_index, column=column_index, shape_ids=[item.shape_id])
            for row_index, row in enumerate(best)
            for column_index, item in enumerate(row)
        ]
        structures.append(GridStructure(
            structure_id=f"grid_{len(structures)}",
            rows=len(best),
            columns=columns,
            header_rows=1,
            cells=cells,
            content_shape_ids=[item.shape_id for item in content],
            decoration_shape_ids=decorations,
        ))
        occupied.update(content_ids)
        occupied.update(decorations)
        row_groups = [row for row in row_groups if not content_ids.intersection(item.shape_id for item in row)]
    return structures


def _repeat_from_bands(
    elements: list[SlideElement], occupied: set[int], band_axis: str
) -> list[RepeatStructure]:
    available = [
        item for item in elements
        if item.shape_id not in occupied
        and item.type == "text"
        and min(item.box.width, item.box.height) > 0.012
    ]
    if len(available) < 4:
        return []
    dimensions = [item.box.height if band_axis == "y" else item.box.width for item in available]
    tolerance = max(0.012, min(0.035, median(dimensions) * 0.55))
    item_axis = "x" if band_axis == "y" else "y"
    bands = [
        sorted(group, key=lambda item: (_center(item, item_axis), item.shape_id))
        for group in _cluster(available, band_axis, tolerance)
        if len(group) >= 2
    ]
    result: list[RepeatStructure] = []
    while bands:
        best: list[list[SlideElement]] = []
        for anchor in bands:
            aligned = [band for band in bands if _aligned(anchor, band, item_axis)]
            if len(aligned) < 2:
                continue
            aligned.sort(key=lambda band: sum(_center(item, band_axis) for item in band) / len(band))
            # A repeated component must have the same primitive type at each item position.
            aligned = [band for band in aligned if len({item.type for item in band}) == 1]
            if len(aligned) >= 2 and sum(map(len, aligned)) > sum(map(len, best)):
                best = aligned
        if not best:
            break
        if sum(map(len, best)) < len(available) * 0.75:
            break
        item_count = len(best[0])
        if item_count < 2:
            break
        fields = [
            RepeatField(
                field_id=f"field_{field_index}",
                shape_ids=[item.shape_id for item in band],
            )
            for field_index, band in enumerate(best)
        ]
        items = [
            RepeatItem(
                index=item_index,
                fields={
                    f"field_{field_index}": best[field_index][item_index].shape_id
                    for field_index in range(len(best))
                },
            )
            for item_index in range(item_count)
        ]
        content_ids = [item.shape_id for band in best for item in band]
        result.append(RepeatStructure(
            structure_id=f"repeat_{len(result)}",
            items=items,
            fields=fields,
            content_shape_ids=content_ids,
        ))
        occupied.update(content_ids)
        used = set(content_ids)
        bands = [band for band in bands if not used.intersection(item.shape_id for item in band)]
    return result


def _detect_static_visuals(
    elements: list[SlideElement], occupied: set[int], classification: str | None
) -> list[StaticVisualStructure]:
    available = [item for item in elements if item.shape_id not in occupied]
    images = [
        item for item in available
        if item.type == "image" and min(item.box.width, item.box.height) > 0.012
    ]
    groups: list[set[int]] = []
    for image in images:
        box = image.box
        contained = {
            item.shape_id for item in available
            if box.x - 0.015 <= _center(item, "x") <= box.x + box.width + 0.015
            and box.y - 0.015 <= _center(item, "y") <= box.y + box.height + 0.015
        }
        if len(contained) >= 2:
            groups.append(contained)

    visual_classes = {"chart", "timeline", "process", "comparison", "content_visual"}
    if classification in visual_classes and len(images) >= 2:
        visual_top = min(item.box.y for item in images)
        composite = {
            item.shape_id for item in available
            if item.box.y + item.box.height / 2 >= visual_top - 0.02
        }
        if len(composite) >= 2:
            groups.append(composite)

    merged: list[set[int]] = []
    for group in groups:
        intersections = [current for current in merged if current & group]
        if intersections:
            combined = set(group)
            for current in intersections:
                combined.update(current)
                merged.remove(current)
            merged.append(combined)
        else:
            merged.append(set(group))
    structures = []
    for group in sorted(merged, key=lambda ids: min(ids)):
        remaining = sorted(group - occupied)
        if len(remaining) < 2:
            continue
        structures.append(StaticVisualStructure(
            structure_id=f"static_visual_{len(structures)}",
            shape_ids=remaining,
        ))
        occupied.update(remaining)
    return structures


def detect_structures(
    elements: Iterable[SlideElement], classification: str | None = None
) -> list[SlideStructure]:
    """Detect only high-confidence structures; ambiguous shapes remain raw elements."""

    flat = _flatten(elements)
    occupied: set[int] = set()
    structures: list[SlideStructure] = []
    structures.extend(_detect_grids(flat, occupied))
    repeats = _repeat_from_bands(flat, occupied, "y")
    repeats.extend(_repeat_from_bands(flat, occupied, "x"))
    for index, repeat in enumerate(repeats):
        repeat.structure_id = f"repeat_{index}"
    structures.extend(repeats)
    structures.extend(_detect_static_visuals(flat, occupied, classification))
    return structures
