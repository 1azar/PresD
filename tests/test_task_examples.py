from pathlib import Path

from PIL import Image

from content_planner.structured_assets import parse_json_assets


EXAMPLE = (
    Path(__file__).resolve().parents[1]
    / "description"
    / "tasks_examples"
    / "it_team_products"
)

TEAM = {
    "irina_krylova.png": ("Ирина Крылова", "Product Lead", "12 лет"),
    "artem_saveliev.png": ("Артём Савельев", "Engineering Manager", "14 лет"),
    "maya_belova.png": ("Майя Белова", "Staff Backend Engineer", "9 лет"),
    "roman_aliev.png": ("Роман Алиев", "ML Lead", "8 лет"),
    "daria_kim.png": ("Дарья Ким", "Product Designer", "7 лет"),
}


def test_it_team_example_has_forced_chart_donut_and_table():
    assets = parse_json_assets((EXAMPLE / "structured_assets.json").read_bytes())

    assert [asset.id for asset in assets] == [
        "active_teams_2025",
        "product_usage_share",
        "product_results",
    ]
    assert [
        (asset.visual_hint.kind, asset.visual_hint.subtype)
        for asset in assets
    ] == [
        ("chart", "line"),
        ("chart", "donut"),
        ("table", None),
    ]
    usage = assets[1]
    assert sum(row["share_percent"] for row in usage.rows) == 100
    assert len(assets[0].rows) == 12
    assert len(assets[2].rows) == 3


def test_it_team_example_references_every_profile_and_valid_portrait():
    content = (EXAMPLE / "content_package.md").read_text(encoding="utf-8")
    portrait_dir = EXAMPLE / "portraits"

    assert set(path.name for path in portrait_dir.glob("*.png")) == set(TEAM)
    for filename, expected_text in TEAM.items():
        assert content.count(f"`{filename}`") >= 2
        assert all(value in content for value in expected_text)
        with Image.open(portrait_dir / filename) as portrait:
            assert portrait.format == "PNG"
            assert portrait.width >= 512
            assert portrait.height >= 512
            assert 0.9 <= portrait.width / portrait.height <= 1.1


def test_it_team_example_web_instructions_are_fixed_to_ten_slides():
    brief = (EXAMPLE / "brief.txt").read_text(encoding="utf-8")
    readme = (EXAMPLE / "README.md").read_text(encoding="utf-8")

    assert "ровно из 10 слайдов" in brief
    assert "10—10" in readme
    assert "structured_assets.json" in readme
    assert "VK Tech шаблон.pptx" in readme
