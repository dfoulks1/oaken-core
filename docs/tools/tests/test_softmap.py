# SPDX-License-Identifier: Apache-2.0
"""The software map must reject data that contradicts the repository or the layering."""

from pathlib import Path

import build_site
import pytest
from model import BuildError
from treekit import Tree

MAP = """\
[[stacks]]
id = "s"
name = "Stack"
layers = ["top", "bottom"]

[[layers]]
id = "top"
name = "Top"

[[layers]]
id = "bottom"
name = "Bottom"

[[components]]
id = "a"
name = "Alpha"
layer = "top"
status = "built"
prompt = "01"
path = "src/x.txt"
summary = "The top piece."
uses = ["b"]

[[components]]
id = "b"
name = "Beta | pipe"
layer = "bottom"
status = "planned"
prompt = "02"
summary = "The bottom piece."
uses = []
"""


def with_map(tree: Tree, text: str = MAP) -> Tree:
    tree.write("docs/softmap.toml", text)
    site = (tree.docs / "site.toml").read_text()
    if 'id = "map"' not in site:
        tree.write(
            "docs/site.toml", site + '\n[[pages]]\nid = "map"\ngenerated = true\ngroup = "g1"\n'
        )
    import generated

    if "map" not in generated._REGISTRY:  # register the real builder under the fixture's id
        generated.register("map", generated._REGISTRY["architecture/software-map"])
    return tree


def test_map_renders_counts_status_and_svg(tree: Tree, tmp_path: Path) -> None:
    with_map(tree)
    out = tmp_path / "out"
    build_site.build(tree.cfg(), out)
    html = (out / "map.html").read_text(encoding="utf-8")
    assert "1 built" in html
    assert "1 planned" in html
    assert "badge text-bg-success" in html
    assert 'id="softmap"' in html
    assert 'class="node built" data-id="a"' in html
    assert 'class="node planned" data-id="b"' in html
    assert "Beta | pipe" in html
    assert "blob/main/src/x.txt" in html
    assert 'id="c-a"' in html and 'id="c-b"' in html
    assert "http://" not in html  # the SVG namespace is not written out
    assert "mermaid@" not in html


def test_map_has_interactive_script_and_cards(tree: Tree, tmp_path: Path) -> None:
    with_map(tree)
    out = tmp_path / "out"
    build_site.build(tree.cfg(), out)
    html = (out / "map.html").read_text(encoding="utf-8")
    assert '"f": "a", "t": "b", "k": "call"' in html
    assert 'id="q"' in html
    assert "Dependency matrix" in html
    assert 'class="mx-c"' in html
    assert "Entry points" in html


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        ('uses = ["b"]', 'uses = ["zzz"]', "uses unknown component"),
        (
            'layer = "bottom"\nstatus = "planned"',
            'layer = "nowhere"\nstatus = "planned"',
            "unknown layer",
        ),
        ('status = "planned"', 'status = "done"', "status must be one of"),
        ('path = "src/x.txt"', 'path = "src/missing.txt"', "does not exist"),
        (
            'summary = "The bottom piece."',
            'summary = "The bottom piece."\npath = "src/x.txt"',
            "marked planned",
        ),
        ('id = "b"', 'id = "a"', "duplicate component id"),
        ('summary = "The top piece."\n', "", "missing 'summary'"),
    ],
)
def test_map_validation_failures(
    tree: Tree, tmp_path: Path, old: str, new: str, expect: str
) -> None:
    with_map(tree, MAP.replace(old, new, 1))
    with pytest.raises(BuildError, match=expect):
        build_site.build(tree.cfg(), tmp_path / "out")


def test_calls_may_not_point_up(tree: Tree, tmp_path: Path) -> None:
    bad = MAP.replace("uses = []", 'uses = ["a"]', 1)
    with_map(tree, bad)
    with pytest.raises(BuildError, match="layering: b calls up into a"):
        build_site.build(tree.cfg(), tmp_path / "out")


def test_call_cycle_is_rejected(tree: Tree, tmp_path: Path) -> None:
    bad = MAP.replace(
        'summary = "The bottom piece."\nuses = []', 'summary = "The bottom piece."\nuses = ["b2"]'
    )
    bad += """
[[components]]
id = "b2"
name = "Gamma"
layer = "bottom"
status = "planned"
prompt = "03"
summary = "Same layer."
uses = ["b"]
"""
    with_map(tree, bad)
    with pytest.raises(BuildError, match="call cycle"):
        build_site.build(tree.cfg(), tmp_path / "out")


def test_callbacks_must_go_up(tree: Tree, tmp_path: Path) -> None:
    with_map(tree, MAP.replace('uses = ["b"]', 'uses = ["b"]\nnotifies = ["b"]', 1))
    with pytest.raises(BuildError, match="not above it"):
        build_site.build(tree.cfg(), tmp_path / "out")


def test_flow_steps_must_follow_relationships(tree: Tree, tmp_path: Path) -> None:
    flow = '\n[[flows]]\nid = "f"\ntitle = "F"\nsteps = [["b", "a", "go", ""]]\n'
    with_map(tree, MAP + flow)
    with pytest.raises(BuildError, match="not a declared relationship"):
        build_site.build(tree.cfg(), tmp_path / "out")
    ok = '\n[[flows]]\nid = "f"\ntitle = "F"\nsteps = [["a", "b", "go", "n"]]\n'
    with_map(tree, MAP + ok)
    build_site.build(tree.cfg(), tmp_path / "out2")


def test_stacks_must_cover_layers_in_order(tree: Tree, tmp_path: Path) -> None:
    with_map(tree, MAP.replace('layers = ["top", "bottom"]', 'layers = ["bottom", "top"]'))
    with pytest.raises(BuildError, match="every layer exactly once"):
        build_site.build(tree.cfg(), tmp_path / "out")


def test_untrusted_marker_and_prompt_link(tree: Tree, tmp_path: Path) -> None:
    tree.write("docs/prompts/01-bootstrap.md", "# p\n")
    with_map(tree, MAP.replace('prompt = "01"', 'prompt = "01"\nuntrusted = true', 1))
    out = tmp_path / "out"
    build_site.build(tree.cfg(), out)
    html = (out / "map.html").read_text(encoding="utf-8")
    assert "untrusted input" in html
    assert 'class="untr"' in html
    assert "blob/main/docs/prompts/01-bootstrap.md" in html
