from __future__ import annotations

from enum import Enum
from pathlib import Path
import os
import tempfile
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from slide_types import SlideClass


SCHEMA_VERSION = "3.0"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Box(StrictModel):
    """A shape box normalized to the slide dimensions."""

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(ge=0, le=1)
    height: float = Field(ge=0, le=1)


class TableCell(StrictModel):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    text: str


class TableContent(StrictModel):
    rows: int = Field(ge=1)
    columns: int = Field(ge=1)
    cells: list[TableCell] = Field(default_factory=list)


class ChartContent(StrictModel):
    title: str | None = None
    chart_type: str | None = None
    category_labels: list[str] = Field(default_factory=list)
    series_names: list[str] = Field(default_factory=list)


class SlideElement(StrictModel):
    shape_id: int
    name: str
    type: str
    box: Box
    z_order: int = Field(ge=0)
    placeholder_type: str | None = None
    alt_text: str | None = None
    text: str | None = None
    table: TableContent | None = None
    chart: ChartContent | None = None
    children: list[SlideElement] = Field(default_factory=list)


class GridCell(StrictModel):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    shape_ids: list[int] = Field(min_length=1)


class GridStructure(StrictModel):
    kind: Literal["grid"] = "grid"
    structure_id: str = Field(pattern=r"^grid_[0-9]+$")
    rows: int = Field(ge=2)
    columns: int = Field(ge=2)
    header_rows: int = Field(default=1, ge=0)
    cells: list[GridCell] = Field(min_length=4)
    content_shape_ids: list[int] = Field(min_length=4)
    decoration_shape_ids: list[int] = Field(default_factory=list)


class RepeatField(StrictModel):
    field_id: str = Field(pattern=r"^field_[0-9]+$")
    shape_ids: list[int] = Field(min_length=2)


class RepeatItem(StrictModel):
    index: int = Field(ge=0)
    fields: dict[str, int] = Field(min_length=1)


class RepeatStructure(StrictModel):
    kind: Literal["repeat"] = "repeat"
    structure_id: str = Field(pattern=r"^repeat_[0-9]+$")
    items: list[RepeatItem] = Field(min_length=2)
    fields: list[RepeatField] = Field(min_length=1)
    content_shape_ids: list[int] = Field(min_length=2)
    decoration_shape_ids: list[int] = Field(default_factory=list)


class StaticVisualStructure(StrictModel):
    kind: Literal["static_visual"] = "static_visual"
    structure_id: str = Field(pattern=r"^static_visual_[0-9]+$")
    shape_ids: list[int] = Field(min_length=1)


SlideStructure = Annotated[
    GridStructure | RepeatStructure | StaticVisualStructure,
    Field(discriminator="kind"),
]


class VLMStatus(str, Enum):
    DISABLED = "disabled"
    OK = "ok"
    ERROR = "error"


class SlideVLMAnalysis(StrictModel):
    status: VLMStatus
    classification: SlideClass | None = None
    tags: list[str] = Field(default_factory=list)
    description: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    error: str | None = None


class SlideMetadata(StrictModel):
    slide_number: int = Field(ge=1)
    slide_id: int
    layout_name: str | None = None
    preview_path: str | None = None
    elements: list[SlideElement] = Field(default_factory=list)
    structures: list[SlideStructure] = Field(default_factory=list)
    theme_fonts: list[str] = Field(default_factory=list)
    palette: list[str] = Field(default_factory=list)
    vlm: SlideVLMAnalysis
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_structures(self) -> SlideMetadata:
        structure_ids = [item.structure_id for item in self.structures]
        if len(structure_ids) != len(set(structure_ids)):
            raise ValueError("structure_id values must be unique within a slide")

        def flatten(items: list[SlideElement]) -> list[SlideElement]:
            return [child for item in items for child in ([item] + flatten(item.children))]

        element_ids = {item.shape_id for item in flatten(self.elements)}
        owners: dict[int, str] = {}
        for structure in self.structures:
            if isinstance(structure, GridStructure):
                shape_ids = structure.content_shape_ids + structure.decoration_shape_ids
                cell_ids = [shape_id for cell in structure.cells for shape_id in cell.shape_ids]
                if set(cell_ids) != set(structure.content_shape_ids):
                    raise ValueError("grid cells must cover content_shape_ids exactly")
                if structure.header_rows >= structure.rows:
                    raise ValueError("grid header_rows must be less than rows")
            elif isinstance(structure, RepeatStructure):
                shape_ids = structure.content_shape_ids + structure.decoration_shape_ids
                item_ids = [shape_id for item in structure.items for shape_id in item.fields.values()]
                field_ids = {field.field_id for field in structure.fields}
                if any(set(item.fields) != field_ids for item in structure.items):
                    raise ValueError("every repeat item must contain every stable field")
                if set(item_ids) != set(structure.content_shape_ids):
                    raise ValueError("repeat items must cover content_shape_ids exactly")
            else:
                shape_ids = structure.shape_ids
            unknown = set(shape_ids) - element_ids
            if unknown:
                raise ValueError(f"structure references unknown shape_ids: {sorted(unknown)}")
            for shape_id in shape_ids:
                if shape_id in owners:
                    raise ValueError(
                        f"shape_id {shape_id} belongs to both {owners[shape_id]!r} "
                        f"and {structure.structure_id!r}"
                    )
                owners[shape_id] = structure.structure_id
        return self


class SlideManifestEntry(StrictModel):
    slide_number: int = Field(ge=1)
    slide_id: int
    directory: str
    metadata_path: str
    preview_path: str | None = None
    vlm_status: VLMStatus


class PresentationManifest(StrictModel):
    schema_version: str = SCHEMA_VERSION
    source_file: str
    source_sha256: str
    slide_width: int = Field(gt=0)
    slide_height: int = Field(gt=0)
    slide_count: int = Field(ge=0)
    vlm_enabled: bool
    slides: list[SlideManifestEntry] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    output_dir: str | None = Field(default=None, exclude=True)

    def save(self, directory: str | Path) -> None:
        directory = Path(directory)
        target = directory / "presentation.json"
        handle, temporary = tempfile.mkstemp(prefix=".presentation.json.", dir=directory)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(self.model_dump_json(indent=2, exclude={"output_dir"}))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
