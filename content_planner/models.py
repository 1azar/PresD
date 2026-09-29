from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from slide_types import SlideClass


PLAN_SCHEMA_VERSION = "5.0"
CATALOG_SCHEMA_VERSION = "5.0"
CATALOG_PROMPT_VERSION = "catalog_v8"
PLANNER_PROMPT_VERSION = "planner_v4"
PIPELINE_VERSION = "semantic_outline_v6_materialized_local_repair"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Scalar = str | int | float | bool | None


class VisualHint(StrictModel):
    mode: Literal["auto", "none", "force"] = "auto"
    kind: Literal["table", "chart", "diagram", "pictogram_grid"] | None = None
    subtype: str | None = None

    @model_validator(mode="after")
    def validate_force(self) -> "VisualHint":
        if self.mode == "force" and self.kind is None:
            raise ValueError("forced visual_hint requires kind")
        if self.mode == "force" and self.kind in {"chart", "diagram"} and not self.subtype:
            raise ValueError("forced chart or diagram visual_hint requires subtype")
        if self.mode == "none" and (self.kind is not None or self.subtype is not None):
            raise ValueError("disabled visual_hint cannot select kind or subtype")
        return self


class DatasetColumn(StrictModel):
    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    type: Literal["text", "number", "date", "boolean"]
    unit: str | None = None


class DatasetAsset(StrictModel):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    kind: Literal["dataset"] = "dataset"
    title: str = Field(min_length=1)
    columns: list[DatasetColumn] = Field(min_length=1)
    rows: list[dict[str, Scalar]] = Field(default_factory=list)
    visual_hint: VisualHint = Field(default_factory=VisualHint)
    source_refs: list[str] = Field(default_factory=list)
    provenance: Literal["provided", "extracted", "inferred"] = "provided"

    @model_validator(mode="after")
    def validate_dataset(self) -> "DatasetAsset":
        keys = [column.key for column in self.columns]
        if len(keys) != len(set(keys)):
            raise ValueError("dataset column keys must be unique")
        expected = set(keys)
        by_key = {column.key: column for column in self.columns}
        for row in self.rows:
            if set(row) != expected:
                raise ValueError("every dataset row must contain exactly the declared columns")
            for key, value in row.items():
                if value is None:
                    continue
                column_type = by_key[key].type
                if column_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
                    raise ValueError(f"dataset value for {key!r} must be a number")
                if column_type == "boolean" and not isinstance(value, bool):
                    raise ValueError(f"dataset value for {key!r} must be a boolean")
                if column_type == "text" and not isinstance(value, str):
                    raise ValueError(f"dataset value for {key!r} must be text")
                if column_type == "date":
                    if not isinstance(value, str):
                        raise ValueError(f"dataset value for {key!r} must be an ISO date string")
                    try:
                        datetime.fromisoformat(value.replace("Z", "+00:00"))
                    except ValueError as exc:
                        raise ValueError(f"dataset value for {key!r} must be an ISO date string") from exc
        if self.visual_hint.mode == "force" and self.visual_hint.kind not in {"table", "chart"}:
            raise ValueError("dataset can only force table or chart visualization")
        return self


class DiagramNode(StrictModel):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str | None = None
    parent_id: str | None = None
    icon_query: str | None = None


class DiagramEdge(StrictModel):
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    label: str | None = None


class DiagramAsset(StrictModel):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    kind: Literal["diagram"] = "diagram"
    title: str = Field(min_length=1)
    diagram_type: Literal["process", "timeline", "cycle", "hierarchy", "comparison"]
    nodes: list[DiagramNode] = Field(min_length=1)
    edges: list[DiagramEdge] = Field(default_factory=list)
    order: list[str] = Field(default_factory=list)
    visual_hint: VisualHint = Field(default_factory=VisualHint)
    source_refs: list[str] = Field(default_factory=list)
    provenance: Literal["provided", "extracted", "inferred"] = "provided"

    @model_validator(mode="after")
    def validate_graph(self) -> "DiagramAsset":
        ids = [node.id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("diagram node ids must be unique")
        known = set(ids)
        if self.order and (set(self.order) != known or len(self.order) != len(known)):
            raise ValueError("diagram order must contain every node exactly once")
        for edge in self.edges:
            if edge.source not in known or edge.target not in known:
                raise ValueError("diagram edge references an unknown node")
        if any(node.parent_id is not None and node.parent_id not in known for node in self.nodes):
            raise ValueError("diagram parent_id references an unknown node")
        if self.visual_hint.mode == "force" and self.visual_hint.kind != "diagram":
            raise ValueError("diagram asset can only force diagram visualization")
        return self


class PictogramItem(StrictModel):
    label: str = Field(min_length=1)
    value: Scalar = None
    icon_query: str = Field(min_length=1)


class PictogramGridAsset(StrictModel):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    kind: Literal["pictogram_grid"] = "pictogram_grid"
    title: str = Field(min_length=1)
    items: list[PictogramItem] = Field(min_length=1)
    visual_hint: VisualHint = Field(default_factory=VisualHint)
    source_refs: list[str] = Field(default_factory=list)
    provenance: Literal["provided", "extracted", "inferred"] = "provided"

    @model_validator(mode="after")
    def validate_hint(self) -> "PictogramGridAsset":
        if self.visual_hint.mode == "force" and self.visual_hint.kind != "pictogram_grid":
            raise ValueError("pictogram_grid asset can only force pictogram_grid visualization")
        return self


StructuredAsset = Annotated[
    DatasetAsset | DiagramAsset | PictogramGridAsset,
    Field(discriminator="kind"),
]


class SlideCountRange(StrictModel):
    min: int = Field(ge=1, le=30)
    max: int = Field(ge=1, le=30)

    @model_validator(mode="after")
    def validate_range(self) -> SlideCountRange:
        if self.max < self.min:
            raise ValueError("slide_count.max must be greater than or equal to slide_count.min")
        return self


class ProvidedImage(StrictModel):
    """An uploaded image available to the planner under an opaque asset reference."""

    id: str = Field(min_length=1)
    original_name: str = Field(min_length=1)
    asset_ref: str = Field(min_length=1)


class PlanningRequest(StrictModel):
    brief: str = Field(min_length=1)
    content_package: str = ""
    slide_count: SlideCountRange
    structured_assets: list["StructuredAsset"] = Field(default_factory=list)
    provided_images: list[ProvidedImage] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self) -> "PlanningRequest":
        if not self.content_package.strip() and not self.structured_assets:
            raise ValueError("content_package or structured_assets is required")
        ids = [asset.id for asset in self.structured_assets]
        if len(ids) != len(set(ids)):
            raise ValueError("structured asset ids must be unique")
        image_ids = [image.id for image in self.provided_images]
        if len(image_ids) != len(set(image_ids)):
            raise ValueError("provided image ids must be unique")
        asset_refs = [image.asset_ref for image in self.provided_images]
        if len(asset_refs) != len(set(asset_refs)):
            raise ValueError("provided image asset_ref values must be unique")
        return self


class SourceChunk(StrictModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class SlotKind(str, Enum):
    TEXT = "text"
    BULLET_LIST = "bullet_list"
    CARDS = "cards"
    PROFILES = "profiles"
    METRICS = "metrics"
    TABLE = "table"
    CHART = "chart"
    TIMELINE = "timeline"
    PROCESS = "process"
    IMAGE = "image"
    VISUAL = "visual"


class SlotAction(str, Enum):
    REPLACE = "replace"
    GENERATE = "generate"
    KEEP = "keep"
    CLEAR = "clear"


class BindingRenderer(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    IMAGE_BOX = "image_box"
    TABLE = "table"
    CHART = "chart"
    VISUAL = "visual"
    CANVAS = "canvas"


class BindingMode(str, Enum):
    SCALAR = "scalar"
    PARAGRAPHS = "paragraphs"
    NATIVE = "native"


class MissingAction(str, Enum):
    CLEAR = "clear"
    KEEP = "keep"


class CatalogBinding(StrictModel):
    """Declarative mapping from logical slot content to one concrete shape."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    shape_id: int
    renderer: BindingRenderer
    value_paths: list[str] = Field(default_factory=list)
    mode: BindingMode = BindingMode.SCALAR
    separator: str = "\n"
    missing_action: MissingAction = MissingAction.CLEAR
    image_fit: Literal["cover", "contain", "stretch"] = "cover"

    @model_validator(mode="after")
    def validate_binding(self) -> CatalogBinding:
        if self.renderer in {BindingRenderer.IMAGE, BindingRenderer.IMAGE_BOX, BindingRenderer.VISUAL}:
            if self.mode != BindingMode.SCALAR:
                raise ValueError("image and visual bindings must use scalar mode")
        elif self.renderer in {BindingRenderer.TABLE, BindingRenderer.CHART} and not self.value_paths:
            # A visual slot can target an existing native PowerPoint object.
            # Its complete materialized dataset is supplied by the compiler,
            # so there is no JSON-pointer binding to declare.
            if self.mode != BindingMode.SCALAR:
                raise ValueError("pathless native bindings must use scalar mode")
        elif not self.value_paths:
            raise ValueError("non-image bindings require value_paths")
        if self.mode == BindingMode.NATIVE and len(self.value_paths) != 1:
            raise ValueError("native bindings require exactly one value_path")
        if any(not path.startswith("/") for path in self.value_paths):
            raise ValueError("binding value_paths must be JSON pointers")
        return self


class CanvasBounds(StrictModel):
    """A rectangle normalized to the slide dimensions."""

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def validate_extent(self) -> "CanvasBounds":
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("canvas bounds must fit inside the slide")
        return self


class ChartCanvas(StrictModel):
    bounds: CanvasBounds
    preserve_shape_ids: list[int] = Field(default_factory=list)
    supported_subtypes: list[
        Literal["bar", "column", "line", "area", "pie", "donut", "scatter"]
    ] = Field(min_length=1)
    preferred_subtypes: list[
        Literal["bar", "column", "line", "area", "pie", "donut", "scatter"]
    ] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_canvas(self) -> "ChartCanvas":
        if len(self.preserve_shape_ids) != len(set(self.preserve_shape_ids)):
            raise ValueError("chart canvas preserve_shape_ids must be unique")
        if len(self.supported_subtypes) != len(set(self.supported_subtypes)):
            raise ValueError("chart canvas supported_subtypes must be unique")
        if len(self.preferred_subtypes) != len(set(self.preferred_subtypes)):
            raise ValueError("chart canvas preferred_subtypes must be unique")
        if not set(self.preferred_subtypes) <= set(self.supported_subtypes):
            raise ValueError("preferred chart subtypes must also be supported")
        return self


class CatalogSlot(StrictModel):
    slot_id: str = Field(min_length=1)
    kind: SlotKind
    role: str = Field(min_length=1)
    target_shape_ids: list[int] = Field(default_factory=list)
    required: bool = True
    capacity: int | None = Field(default=None, ge=1)
    max_chars: int | None = Field(default=None, ge=1)
    bindings: list[CatalogBinding] = Field(default_factory=list)
    visual_capabilities: "VisualCapabilities | None" = None
    chart_canvas: ChartCanvas | None = None

    @model_validator(mode="after")
    def validate_bindings(self) -> CatalogSlot:
        if self.chart_canvas is not None:
            if self.kind != SlotKind.VISUAL or self.slot_id != "chart_visual":
                raise ValueError("chart_canvas is only valid on the chart_visual slot")
            if self.target_shape_ids or self.bindings:
                raise ValueError("chart canvas slots must not have shape anchors or bindings")
            return self
        if not self.target_shape_ids or not self.bindings:
            raise ValueError("non-canvas slots require target_shape_ids and bindings")
        if len(self.target_shape_ids) != len(set(self.target_shape_ids)):
            raise ValueError("target_shape_ids must be unique within a slot")
        binding_ids = [binding.shape_id for binding in self.bindings]
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("binding shape_id values must be unique within a slot")
        if set(binding_ids) != set(self.target_shape_ids):
            raise ValueError("bindings must cover target_shape_ids exactly")
        return self


class VisualCapabilities(StrictModel):
    kinds: list[Literal["table", "chart", "diagram", "pictogram_grid"]] = Field(min_length=1)
    subtypes: list[str] = Field(default_factory=list)
    render_mode: Literal["existing_object", "pseudo_table", "container"]
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    aspect_ratio: float = Field(gt=0)
    max_items: int | None = Field(default=None, ge=1)
    max_rows: int | None = Field(default=None, ge=1)
    max_columns: int | None = Field(default=None, ge=1)


class CatalogVariant(StrictModel):
    slide_number: int = Field(ge=1)
    description: str = Field(min_length=1)
    slots: list[CatalogSlot] = Field(default_factory=list)
    static_shape_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_shape_ownership(self) -> CatalogVariant:
        slot_ids = [slot.slot_id for slot in self.slots]
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("slot_id values must be unique within a variant")
        owned = [shape_id for slot in self.slots for shape_id in slot.target_shape_ids]
        if len(owned) != len(set(owned)):
            raise ValueError("a shape_id cannot belong to multiple slots")
        overlap = set(owned) & set(self.static_shape_ids)
        if overlap:
            raise ValueError(f"shape_id values cannot be both editable and static: {sorted(overlap)}")
        return self


class CatalogFamily(StrictModel):
    family_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    slide_class: SlideClass
    description: str = Field(min_length=1)
    variants: list[CatalogVariant] = Field(min_length=1)


class TemplateCatalog(StrictModel):
    schema_version: str = CATALOG_SCHEMA_VERSION
    prompt_version: str = CATALOG_PROMPT_VERSION
    model: str = Field(min_length=1)
    source_sha256: str = Field(min_length=1)
    families: list[CatalogFamily] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_catalog(self) -> TemplateCatalog:
        if self.schema_version != CATALOG_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported catalog schema {self.schema_version!r}; expected {CATALOG_SCHEMA_VERSION!r}"
            )
        if self.prompt_version != CATALOG_PROMPT_VERSION:
            raise ValueError(
                f"unsupported catalog prompt {self.prompt_version!r}; expected {CATALOG_PROMPT_VERSION!r}"
            )
        family_ids = [family.family_id for family in self.families]
        if len(family_ids) != len(set(family_ids)):
            raise ValueError("catalog family_id values must be unique")
        slides = [variant.slide_number for family in self.families for variant in family.variants]
        if len(slides) != len(set(slides)):
            raise ValueError("each template slide must belong to exactly one family")
        return self


class ShapeSlotTarget(StrictModel):
    target_type: Literal["shape"] = "shape"
    shape_id: int


class StructureSlotTarget(StrictModel):
    target_type: Literal["structure"] = "structure"
    structure_id: str = Field(min_length=1)


SlotTarget = Annotated[
    ShapeSlotTarget | StructureSlotTarget,
    Field(discriminator="target_type"),
]


class CatalogSlotDescriptor(StrictModel):
    """Compact LLM contract; concrete shape bindings are expanded locally."""

    slot_id: str = Field(min_length=1)
    kind: SlotKind
    role: str = Field(min_length=1)
    target: SlotTarget
    required: bool = True
    field_roles: dict[str, str] = Field(default_factory=dict)
    max_chars: int | None = Field(default=None, ge=1)


class CatalogSlideDescriptor(StrictModel):
    slide_number: int = Field(ge=1)
    classification: SlideClass
    description: str = Field(min_length=1)
    family_hint: str = Field(min_length=1)
    family_description: str = Field(min_length=1)
    slots: list[CatalogSlotDescriptor] = Field(default_factory=list)


class CatalogBatchResult(StrictModel):
    slides: list[CatalogSlideDescriptor] = Field(min_length=1)


class CatalogGroup(StrictModel):
    family_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    description: str = Field(min_length=1)
    slide_numbers: list[int] = Field(min_length=1)


class CatalogGroupingResult(StrictModel):
    groups: list[CatalogGroup] = Field(min_length=1)


class SourcedText(StrictModel):
    text: str = Field(min_length=1)
    source_refs: list[str] = Field(min_length=1)


class TextSlotContent(StrictModel):
    kind: Literal["text"] = "text"
    text: str | None = Field(default=None, min_length=1)
    fields: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_text(self) -> TextSlotContent:
        if self.text is None and not self.fields:
            raise ValueError("text content requires text or named fields")
        return self


class BulletListContent(StrictModel):
    kind: Literal["bullet_list"] = "bullet_list"
    items: list[str] = Field(min_length=1)


class CardItem(StrictModel):
    title: str = Field(min_length=1)
    body: str | None = None
    value: Scalar = None


class CardsContent(StrictModel):
    kind: Literal["cards"] = "cards"
    items: list[CardItem] = Field(min_length=1)


class ProfileItem(StrictModel):
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    details: list[str] = Field(default_factory=list)


class ProfilesContent(StrictModel):
    kind: Literal["profiles"] = "profiles"
    items: list[ProfileItem] = Field(min_length=1)


class MetricItem(StrictModel):
    label: str = Field(min_length=1)
    value: Scalar
    unit: str | None = None


class MetricsContent(StrictModel):
    kind: Literal["metrics"] = "metrics"
    items: list[MetricItem] = Field(min_length=1)


class TableContent(StrictModel):
    kind: Literal["table"] = "table"
    columns: list[str] = Field(min_length=1)
    rows: list[list[Scalar]] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_rows(self) -> TableContent:
        width = len(self.columns)
        if any(len(row) != width for row in self.rows):
            raise ValueError("every table row must have the same width as columns")
        return self


class ChartSeries(StrictModel):
    name: str = Field(min_length=1)
    values: list[float] = Field(min_length=1)


class ChartContent(StrictModel):
    kind: Literal["chart"] = "chart"
    chart_type: Literal["bar", "column", "line", "area", "pie", "donut", "scatter"]
    categories: list[Scalar] = Field(min_length=1)
    series: list[ChartSeries] = Field(min_length=1)
    unit: str | None = None

    @model_validator(mode="after")
    def validate_series(self) -> ChartContent:
        width = len(self.categories)
        if any(len(series.values) != width for series in self.series):
            raise ValueError("every chart series must match the category count")
        return self


class TimelineItem(StrictModel):
    date: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str | None = None


class TimelineContent(StrictModel):
    kind: Literal["timeline"] = "timeline"
    items: list[TimelineItem] = Field(min_length=1)


class ProcessItem(StrictModel):
    title: str = Field(min_length=1)
    description: str | None = None


class ProcessContent(StrictModel):
    kind: Literal["process"] = "process"
    items: list[ProcessItem] = Field(min_length=1)


class ImageSlotContent(StrictModel):
    kind: Literal["image"] = "image"
    prompt: str | None = None
    asset_ref: str | None = None
    alt_text: str | None = None


class VisualSlotContent(StrictModel):
    kind: Literal["visual"] = "visual"
    asset_id: str = Field(min_length=1)
    visual_kind: Literal["table", "chart", "diagram", "pictogram_grid"]
    subtype: str | None = None
    selected_columns: list[str] = Field(default_factory=list)
    selected_nodes: list[str] = Field(default_factory=list)
    page: int = Field(default=1, ge=1)


SlotContent = Annotated[
    TextSlotContent
    | BulletListContent
    | CardsContent
    | ProfilesContent
    | MetricsContent
    | TableContent
    | ChartContent
    | TimelineContent
    | ProcessContent
    | ImageSlotContent
    | VisualSlotContent,
    Field(discriminator="kind"),
]


class SlotAssignment(StrictModel):
    slot_id: str = Field(min_length=1)
    kind: SlotKind
    target_shape_ids: list[int] = Field(default_factory=list)
    action: SlotAction
    source_refs: list[str] = Field(default_factory=list)
    content: SlotContent | None = None

    @model_validator(mode="after")
    def validate_action(self) -> SlotAssignment:
        # Accept invalid KEEP/GENERATE actions here so an imperfect LLM
        # response reaches deterministic catalog canonicalization. Optional
        # non-image slots are normalized to CLEAR there; required ones become
        # a precise validation issue and can be revised.
        if self.action in {SlotAction.KEEP, SlotAction.CLEAR}:
            if self.content is not None:
                raise ValueError("keep and clear assignments must not contain content")
            return self
        if self.content is None:
            raise ValueError("replace and generate assignments require content")
        if self.content.kind != self.kind.value:
            raise ValueError("assignment kind must match content kind")
        if not self.source_refs:
            raise ValueError("replace and generate assignments require source_refs")
        if self.action == SlotAction.GENERATE:
            if self.kind != SlotKind.IMAGE:
                return self
            if not isinstance(self.content, ImageSlotContent) or not self.content.prompt:
                raise ValueError("generate requires image content with a prompt")
            if self.content.asset_ref:
                raise ValueError("generate must not contain asset_ref")
        if self.action == SlotAction.REPLACE and isinstance(self.content, ImageSlotContent):
            if not self.content.asset_ref:
                raise ValueError("replacing an image requires asset_ref")
        return self


class RenderOperation(StrictModel):
    shape_id: int | None = None
    renderer: BindingRenderer
    action: SlotAction
    text: list[str] | None = None
    asset_ref: str | None = None
    prompt: str | None = None
    alt_text: str | None = None
    data: dict[str, Any] | None = None
    asset_id: str | None = None
    asset_hash: str | None = None
    visual_kind: Literal["table", "chart", "diagram", "pictogram_grid"] | None = None
    visual_subtype: str | None = None
    image_fit: Literal["cover", "contain", "stretch"] = "cover"
    canvas_bounds: CanvasBounds | None = None
    preserve_shape_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_operation(self) -> RenderOperation:
        if self.renderer == BindingRenderer.CANVAS:
            if self.shape_id is not None:
                raise ValueError("canvas operation must not have a shape_id")
            if self.action != SlotAction.REPLACE:
                raise ValueError("canvas operation must replace its content")
            if (
                self.canvas_bounds is None or self.data is None or not self.asset_id
                or not self.asset_hash or self.visual_kind != "chart"
            ):
                raise ValueError("canvas operation requires bounds and materialized chart data")
            return self
        if self.shape_id is None:
            raise ValueError("non-canvas operation requires shape_id")
        if self.text is not None and not self.text:
            raise ValueError("text operation cannot contain an empty paragraph list")
        if self.action in {SlotAction.CLEAR, SlotAction.KEEP}:
            if any(value is not None for value in (self.text, self.asset_ref, self.prompt, self.data, self.asset_id, self.asset_hash)):
                raise ValueError("clear and keep operations cannot contain a value")
            return self
        if self.renderer == BindingRenderer.TEXT and self.text is None:
            raise ValueError("text replacement requires text")
        if self.renderer in {BindingRenderer.IMAGE, BindingRenderer.IMAGE_BOX}:
            if self.action == SlotAction.REPLACE and not self.asset_ref:
                raise ValueError("image replacement requires asset_ref")
            if self.action == SlotAction.GENERATE and not self.prompt:
                raise ValueError("image generation requires prompt")
        if self.renderer in {BindingRenderer.TABLE, BindingRenderer.CHART} and self.data is None:
            raise ValueError("native renderer requires data")
        if self.renderer == BindingRenderer.VISUAL:
            if self.data is None or not self.asset_id or not self.asset_hash or not self.visual_kind:
                raise ValueError("visual renderer requires materialized asset data and hash")
        return self


class DeckInfo(StrictModel):
    title: SourcedText
    summary: SourcedText
    language: str = Field(min_length=2)


class SlidePlan(StrictModel):
    number: int = Field(ge=1)
    purpose: str = Field(min_length=1)
    template_family_id: str = Field(min_length=1)
    template_slide_number: int = Field(ge=1)
    template_class: SlideClass
    chart_asset_id: str | None = None
    assignments: list[SlotAssignment] = Field(default_factory=list)
    render_operations: list[RenderOperation] = Field(default_factory=list)


class PlanCandidate(StrictModel):
    deck: DeckInfo
    slides: list[SlidePlan] = Field(min_length=1)


class NarrativeSlotAssignment(StrictModel):
    """LLM-owned assignment fields; catalog-owned shape metadata is added later."""

    slot_id: str = Field(min_length=1)
    action: SlotAction
    source_refs: list[str] = Field(default_factory=list)
    content: SlotContent | None = None


class NarrativeSlide(StrictModel):
    purpose: str = Field(min_length=1)
    template_family_id: str = Field(min_length=1)
    template_slide_number: int = Field(ge=1)
    chart_asset_id: str | None = None
    assignments: list[NarrativeSlotAssignment] = Field(default_factory=list)


class NarrativePlanCandidate(StrictModel):
    """Compact model contract: narrative, variant selection, and authored content."""

    deck: DeckInfo
    slides: list[NarrativeSlide] = Field(min_length=1)


class SlideOutline(StrictModel):
    """Model-owned narrative choice without catalog-owned shape bindings."""

    number: int = Field(ge=1)
    purpose: str = Field(min_length=1)
    template_family_id: str = Field(min_length=1)
    template_slide_number: int = Field(ge=1)
    source_refs: list[str] = Field(min_length=1)
    asset_ids: list[str] = Field(default_factory=list)
    image_ids: list[str] = Field(default_factory=list)
    chart_asset_id: str | None = None


class DeckOutline(StrictModel):
    """One compact, ordered scenario shared by all per-slide requests."""

    deck: DeckInfo
    slides: list[SlideOutline] = Field(min_length=1)


class SemanticSlideOutline(StrictModel):
    """The complete model-owned part of an outline.

    Template identities, slot metadata, assets and renderer choices are
    deliberately absent.  They are resolved from the material registry and
    catalog after this response has been validated.
    """

    number: int = Field(ge=1)
    purpose: str = Field(min_length=1)
    content_type: Literal[
        "cover", "text", "bullets", "cards", "profiles", "metrics",
        "table", "chart", "diagram", "pictogram_grid", "process",
        "timeline", "image", "closing",
    ]
    source_refs: list[str] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def read_pre_semantic_outline(cls, value: Any) -> Any:
        """Accept checkpoints/test doubles from the immediately previous format.

        The generated JSON Schema still exposes only the semantic contract.
        """
        if not isinstance(value, dict) or "content_type" in value:
            return value
        technical = {
            "template_family_id", "template_slide_number", "asset_ids",
            "image_ids", "chart_asset_id",
        }
        if not technical.intersection(value):
            return value
        family = str(value.get("template_family_id") or "").casefold()
        content_type = "cover" if "cover" in family or value.get("number") == 1 else "text"
        if "closing" in family:
            content_type = "closing"
        elif "profile" in family:
            content_type = "profiles"
        elif "metric" in family:
            content_type = "metrics"
        if value.get("chart_asset_id"):
            content_type = "chart"
        elif value.get("image_ids"):
            content_type = "image"
        return {
            "number": value.get("number"), "purpose": value.get("purpose"),
            "content_type": content_type, "source_refs": value.get("source_refs"),
        }


class SemanticDeckOutline(StrictModel):
    deck: DeckInfo
    slides: list[SemanticSlideOutline] = Field(min_length=1)


class SlideDraft(StrictModel):
    """Only authored slot values; template selection stays owned by the outline."""

    number: int = Field(ge=1)
    assignments: list[NarrativeSlotAssignment] = Field(default_factory=list)


class QualityReport(StrictModel):
    status: Literal["passed", "needs_review"]
    revision_count: int = Field(ge=0, le=4)
    warnings: list[str] = Field(default_factory=list)
    result_kind: Literal["primary", "fallback"] = "primary"

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_fallback(cls, value: Any) -> Any:
        """Read old plans while keeping new fallback detection explicit."""
        if isinstance(value, dict) and "result_kind" not in value:
            warnings = value.get("warnings") or []
            legacy = any(
                "упрощ" in str(warning).casefold() or "fallback" in str(warning).casefold()
                for warning in warnings
            )
            value = {**value, "result_kind": "fallback" if legacy else "primary"}
        return value


class TemplateReference(StrictModel):
    source_sha256: str
    catalog_version: str


class PresentationPlan(StrictModel):
    status: Literal["ok"] = "ok"
    schema_version: str = PLAN_SCHEMA_VERSION
    model: str
    template: TemplateReference
    deck: DeckInfo
    sources: list[SourceChunk]
    structured_assets: list[StructuredAsset] = Field(default_factory=list)
    slides: list[SlidePlan]
    quality: QualityReport

    @model_validator(mode="after")
    def validate_schema_version(self) -> PresentationPlan:
        if self.schema_version != PLAN_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported plan schema {self.schema_version!r}; expected {PLAN_SCHEMA_VERSION!r}"
            )
        ids = [asset.id for asset in self.structured_assets]
        if len(ids) != len(set(ids)):
            raise ValueError("structured asset ids must be unique")
        return self


class AnalyzedFact(StrictModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    source_refs: list[str] = Field(min_length=1)
    mandatory: bool = True
    fact_type: Literal["content", "constraint"] = "content"


class ContentAnalysis(StrictModel):
    goal: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    language: str = Field(min_length=2)
    narrative: list[str] = Field(min_length=1)
    recommended_slide_count: int = Field(ge=1, le=30)
    facts: list[AnalyzedFact] = Field(min_length=1)
    visual_opportunities: list[str] = Field(default_factory=list)
    visual_candidates: list[StructuredAsset] = Field(default_factory=list)


class CritiqueScores(StrictModel):
    factuality: int = Field(ge=1, le=5)
    coverage: int = Field(ge=1, le=5)
    narrative: int = Field(ge=1, le=5)
    template_fit: int = Field(ge=1, le=5)
    conciseness: int = Field(ge=1, le=5)


class CritiqueIssue(StrictModel):
    severity: Literal["blocking", "warning"]
    message: str = Field(min_length=1)
    slide_number: int | None = Field(default=None, ge=1)
    code: str = Field(default="semantic_quality", min_length=1)
    dimension: Literal[
        "factuality", "coverage", "narrative", "conciseness", "template_fit", "global",
    ] = "global"


class PlanCritique(StrictModel):
    scores: CritiqueScores
    issues: list[CritiqueIssue] = Field(default_factory=list)

    def passes(self) -> bool:
        return (
            self.scores.factuality == 5
            and self.scores.coverage >= 4
            and self.scores.narrative >= 4
            and self.scores.template_fit >= 4
            and self.scores.conciseness >= 4
            and not any(issue.severity == "blocking" for issue in self.issues)
        )


PLAN_CANDIDATE_ADAPTER = TypeAdapter(PlanCandidate)


def compact_schema(model: type[BaseModel] | TypeAdapter[Any]) -> dict[str, Any]:
    if isinstance(model, TypeAdapter):
        return model.json_schema()
    return model.model_json_schema()
