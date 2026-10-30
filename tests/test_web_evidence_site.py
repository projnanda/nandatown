from pathlib import Path


SITE_ROOT = Path(__file__).parents[1] / "examples" / "web_evidence_site"
PAGES = ("index.html", "projects.html", "careers.html")


def page_text(name: str) -> str:
    return (SITE_ROOT / name).read_text(encoding="utf-8")


def test_site_has_three_connected_static_pages() -> None:
    assert (SITE_ROOT / "styles.css").is_file()

    for page_name in PAGES:
        html = page_text(page_name)
        assert 'href="styles.css"' in html
        for target in PAGES:
            assert f'href="{target}"' in html


def test_ground_truth_facts_are_stable_and_visible() -> None:
    index = page_text("index.html")
    projects = page_text("projects.html")
    careers = page_text("careers.html")

    assert "Paris, France" in index
    assert "Aurora Reasoning Engine" in projects
    assert '<time datetime="2024-03-14">March 14, 2024</time>' in projects
    assert "Camille Durand" in projects
    assert "Senior Research Engineer" in careers
    assert "€92,000–€118,000" in careers
