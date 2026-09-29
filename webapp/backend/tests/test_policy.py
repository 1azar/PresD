from content_planner.compiler import compile_slide
from content_planner.models import (
    CATALOG_SCHEMA_VERSION, CatalogFamily, CatalogSlot, CatalogVariant, DeckInfo,
    PresentationPlan, QualityReport, SlidePlan, SlotAssignment, SourceChunk,
    SourcedText, TemplateCatalog, TemplateReference,
)

from webapp.backend.app.tasks import _disable_image_generation


def test_web_policy_rewrites_generate_and_recompiles(tmp_path):
    slot = CatalogSlot.model_validate({
        "slot_id": "hero", "kind": "image", "role": "hero image",
        "target_shape_ids": [7], "bindings": [{"shape_id": 7, "renderer": "image"}],
    })
    variant = CatalogVariant(slide_number=1, description="Cover", slots=[slot])
    catalog = TemplateCatalog(model="test", source_sha256="a" * 64, families=[CatalogFamily(
        family_id="cover", slide_class="cover", description="Cover", variants=[variant],
    )])
    (tmp_path / "planner_catalog.json").write_text(catalog.model_dump_json(), encoding="utf-8")
    assignment = SlotAssignment.model_validate({
        "slot_id": "hero", "kind": "image", "target_shape_ids": [7], "action": "generate",
        "source_refs": ["brief"], "content": {"kind": "image", "prompt": "A generated image"},
    })
    slide = SlidePlan(
        number=1, purpose="Cover", template_family_id="cover", template_slide_number=1,
        template_class="cover", assignments=[assignment], render_operations=compile_slide(
            type("Slide", (), {"assignments": [assignment]})(), variant
        ),
    )
    plan = PresentationPlan(
        model="test", template=TemplateReference(source_sha256="a" * 64, catalog_version=CATALOG_SCHEMA_VERSION),
        deck=DeckInfo(title=SourcedText(text="Title", source_refs=["brief"]), summary=SourcedText(text="Summary", source_refs=["brief"]), language="ru"),
        sources=[SourceChunk(id="brief", text="Brief")], slides=[slide], quality=QualityReport(status="passed", revision_count=0),
    )
    warnings = _disable_image_generation(plan, tmp_path)
    assert plan.slides[0].assignments[0].action.value == "keep"
    assert plan.slides[0].render_operations[0].action.value == "keep"
    assert warnings and "генерация" in warnings[0].lower()

