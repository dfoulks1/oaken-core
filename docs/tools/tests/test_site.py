# SPDX-License-Identifier: Apache-2.0
"""What the generated site contains: nav, numbering, scroll-spy, theme, SRI, mermaid, links."""

import re
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import build_site
import generated
import pagebase
import pytest
from model import BuildError, Config
from treekit import Tree


def read(out: Path, rel: str) -> str:
    return (out / rel).read_text(encoding="utf-8")


def test_nav_groups_and_current_page(built: Callable[[], Path]) -> None:
    out = built()
    html = read(out, "alpha.html")
    assert html.index("First group") < html.index("Second group")
    assert 'class="doc-link current" href="alpha.html" aria-current="page"' in html
    assert 'class="doc-link" href="deep/beta.html"' in html
    nested = read(out, "deep/beta.html")
    assert 'class="doc-link" href="../alpha.html"' in nested
    assert 'href="../assets/site.css"' in nested


def test_sections_are_numbered(built: Callable[[], Path]) -> None:
    html = read(built(), "alpha.html")
    assert '<span class="section-num">1</span><h2>One</h2>' in html
    assert '<span class="section-num">2</span><h2>Two</h2>' in html
    assert '<span class="num">1</span>One' in html
    assert '<span class="num">2</span>Two' in html


def test_scroll_spy_markup(built: Callable[[], Path]) -> None:
    html = read(built(), "alpha.html")
    assert 'data-bs-spy="scroll"' in html
    assert 'data-bs-target="#toc"' in html
    assert 'id="toc"' in html
    assert 'href="#one"' in html
    assert 'id="one"' in html
    # below 992 px the pane is an offcanvas opened from a top-bar button
    assert "offcanvas-lg" in html
    assert 'data-bs-toggle="offcanvas"' in html


def test_theme_attribute_and_storage_guard(built: Callable[[], Path]) -> None:
    out = built()
    for rel in ("index.html", "alpha.html", "404.html"):
        html = read(out, rel)
        assert 'data-bs-theme="light"' in html
        assert "prefers-color-scheme: dark" in html
        assert "oaken-theme" in html
        # every localStorage access in the head script is inside try/catch
        head = re.search(r"<script>(.*?)</script>", html, re.S)
        assert head is not None
        script = head.group(1)
        assert script.count("localStorage") == 1
        assert re.search(r"try\{[^}]*localStorage[^}]*\}catch\(e\)\{\}", script)
    js = read(out, "assets/site.js")
    assert re.search(r"try \{ localStorage\.setItem", js)


def test_pygments_styles_scoped_to_theme(built: Callable[[], Path]) -> None:
    css = read(built(), "assets/site.css")
    assert '[data-bs-theme="light"] .highlight' in css
    assert '[data-bs-theme="dark"] .highlight' in css
    assert "--accent: #1f4e79" in css
    assert "--accent: #7fb2e5" in css


class Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.cdn: list[tuple[str, dict[str, str | None]]] = []
        self.urls: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        for key in ("href", "src"):
            val = a.get(key)
            if val and tag in {"a", "link", "script", "img"}:
                self.urls.append((tag, val))
                if "cdn.jsdelivr.net" in val:
                    self.cdn.append((tag, a))


def scan(html: str) -> Tags:
    t = Tags()
    t.feed(html)
    return t


def test_every_cdn_tag_has_sri(built: Callable[[], Path]) -> None:
    out = built()
    pinned = {v["url"]: v["integrity"] for v in pagebase.sri().values()}
    assert all(i.startswith("sha384-") for i in pinned.values())
    seen = 0
    for page in out.rglob("*.html"):
        for _tag, a in scan(page.read_text(encoding="utf-8")).cdn:
            seen += 1
            url = a.get("href") or a.get("src")
            assert url in pinned
            assert a["integrity"] == pinned[str(url)]
            assert a["crossorigin"] == "anonymous"
    assert seen >= 4


def test_mermaid_script_only_on_pages_with_a_diagram(built: Callable[[], Path]) -> None:
    out = built()
    assert "mermaid" in read(out, "alpha.html").lower()
    assert "mermaid@" in read(out, "alpha.html")
    for rel in ("index.html", "deep/beta.html", "404.html"):
        assert "mermaid@" not in read(out, rel)
    html = read(out, "alpha.html")
    assert '<pre class="mermaid">flowchart TD' in html
    assert "<noscript><pre><code>flowchart TD" in html
    assert "--&gt;" in html  # source is escaped, never executed as markup


def test_pages_only_request_pinned_cdn_and_github(built: Callable[[], Path]) -> None:
    out = built()
    pinned = {v["url"] for v in pagebase.sri().values()}
    for page in out.rglob("*.html"):
        text = page.read_text(encoding="utf-8")
        assert "http://" not in text
        for tag, url in scan(text).urls:
            parts = urlsplit(url)
            if not parts.scheme or parts.scheme == "data":
                continue
            if parts.hostname == "cdn.jsdelivr.net":
                assert url in pinned
            else:
                assert tag == "a"
                assert parts.hostname == "github.com", url
        # no inline event-less network calls either
        assert "fetch(" not in text
        assert "XMLHttpRequest" not in text


def test_repo_links_expand_and_validate(tree: Tree, built: Callable[[], Path]) -> None:
    html = read(built(), "alpha.html")
    assert 'href="https://github.com/example/fixture/blob/main/src/x.txt"' in html
    assert 'href="https://github.com/example/fixture/tree/main/src"' in html
    tree.append("docs/alpha.md", "\nSee [gone](repo:src/missing.txt).\n")
    with pytest.raises(BuildError, match=r"repo:src/missing\.txt does not exist"):
        build_site.build(tree.cfg(), tree.root / "out2")


def test_page_links_are_relative_and_resolved(built: Callable[[], Path]) -> None:
    out = built()
    assert 'href="deep/beta.html"' in read(out, "alpha.html")
    assert 'href="../alpha.html#two"' in read(out, "deep/beta.html")


def test_home_cards_generated_from_registry(built: Callable[[], Path]) -> None:
    html = read(built(), "index.html")
    assert 'href="alpha.html"' in html
    assert 'href="deep/beta.html"' in html
    assert "Alpha blurb." in html
    assert "{{doc-cards}}" not in html
    assert "Star this repo on GitHub" in html
    assert "Pre-alpha" in html


def test_extra_files_and_relative_everywhere(built: Callable[[], Path]) -> None:
    out = built()
    for name in ("404.html", "sitemap.xml", "robots.txt", ".nojekyll", "assets/site.css"):
        assert (out / name).exists(), name
    assert "https://example.github.io/fixture/alpha.html" in read(out, "sitemap.xml")
    assert "Sitemap: https://example.github.io/fixture/sitemap.xml" in read(out, "robots.txt")
    assert 'href="/fixture/"' in read(out, "404.html")


def test_footer_has_no_wall_clock(built: Callable[[], Path]) -> None:
    html = read(built(), "alpha.html")
    footer = re.search(r"<footer>(.*?)</footer>", html)
    assert footer is not None
    assert footer.group(1) == "Fixture dev · Apache-2.0"


def test_two_builds_are_byte_identical(tree: Tree, tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    build_site.build(tree.cfg(), a)
    build_site.build(tree.cfg(), b)
    fa = {p.relative_to(a): p.read_bytes() for p in a.rglob("*") if p.is_file()}
    fb = {p.relative_to(b): p.read_bytes() for p in b.rglob("*") if p.is_file()}
    assert fa == fb


def test_refuses_to_clobber_a_foreign_directory(tree: Tree, tmp_path: Path) -> None:
    out = tmp_path / "precious"
    out.mkdir()
    (out / "notes.txt").write_text("keep me")
    with pytest.raises(BuildError, match="refusing to overwrite"):
        build_site.build(tree.cfg(), out)
    assert (out / "notes.txt").exists()


def test_strict_fails_on_todo_user(tree: Tree, tmp_path: Path) -> None:
    tree.append("docs/alpha.md", "\nTODO(user): fill me in.\n")
    args = ["--docs", str(tree.docs), "--repo-root", str(tree.root)]
    assert build_site.main([*args, "--out", str(tmp_path / "o1")]) == 0
    assert build_site.main([*args, "--out", str(tmp_path / "o2"), "--strict"]) == 1


def test_generated_pages_register_and_render(tree: Tree, tmp_path: Path) -> None:
    tree.write(
        "docs/origin/0001-first.md",
        "# 1. First decision\n\n- Status: accepted\n- Date: 2026-01-02\n",
    )
    tree.write(
        "docs/site.toml",
        (tree.docs / "site.toml").read_text()
        + ('\n[[pages]]\nid = "adr/index"\ngenerated = true\ngroup = "g1"\n'),
    )
    cfg: Config = tree.cfg()
    cfg.excludes.append("origin/0*.md")
    out = tmp_path / "gen"
    build_site.build(cfg, out)
    html = read(out, "adr/index.html")
    assert "First decision" in html
    assert "accepted" in html
    assert "2026-01-02" in html
    assert "blob/main/docs/origin/0001-first.md" in html


def test_unregistered_generator_is_an_error(tree: Tree, tmp_path: Path) -> None:
    tree.write(
        "docs/site.toml",
        (tree.docs / "site.toml").read_text()
        + ('\n[[pages]]\nid = "nope"\ngenerated = true\ngroup = "g1"\n'),
    )
    with pytest.raises(BuildError, match="no generator exists"):
        build_site.build(tree.cfg(), tmp_path / "x")
    assert generated.get("adr/index") is not None
