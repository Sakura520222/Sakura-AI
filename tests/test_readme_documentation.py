"""README 与文档站入口契约 / README and documentation entry contracts."""

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).parents[1]
SITE = "https://sakura520222.github.io/Sakura-AI/"
READMES = ("README.md", "README_EN.md")
GUIDES = ("features", "issue-analysis", "development")


def _site_pages(text: str) -> set[str]:
    links = re.findall(re.escape(SITE) + r'[^\s)"<>]*', text)
    return {
        urlsplit(link).path.removeprefix("/Sakura-AI/").removesuffix(".html")
        for link in links
    }


@pytest.mark.parametrize("filename", READMES)
def test_readme_documentation_links_resolve_to_site_sources(filename: str) -> None:
    text = (ROOT / filename).read_text(encoding="utf-8")
    pages = _site_pages(text)
    assert {"quick-start", "DEPLOYMENT", *GUIDES} <= pages
    for page in pages:
        source = ROOT / "docs" / f"{page or 'index'}.md"
        assert source.is_file(), f"{filename}: {page}"

    assets = re.findall(r'(?:src|href)="((?:res|docs/images)/[^"#]+)"', text)
    assert assets
    for asset in assets:
        assert (ROOT / asset).is_file(), f"{filename}: {asset}"


def test_readmes_share_documentation_destinations() -> None:
    chinese, english = (
        (ROOT / filename).read_text(encoding="utf-8") for filename in READMES
    )
    assert _site_pages(chinese) == _site_pages(english)


def test_python_version_policy_is_preserved_in_both_languages() -> None:
    guide = (ROOT / "docs" / "development.md").read_text(encoding="utf-8")
    chinese, english = guide.split("## English summary", 1)
    for text in (chinese, english):
        assert "Dependabot" in text
        assert "3.14" in text
        assert "3.15" in text
        assert "`/docker`" in text
        assert "digest" in text


@pytest.mark.parametrize("guide", GUIDES)
def test_migrated_guides_are_in_documentation_navigation(guide: str) -> None:
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    config = (ROOT / "docs" / ".vitepress" / "config.mjs").read_text(encoding="utf-8")
    assert f"]({guide}.md)" in index
    assert f"link: '/{guide}'" in config
    text = (ROOT / "docs" / f"{guide}.md").read_text(encoding="utf-8")
    assert "## English summary" in text
