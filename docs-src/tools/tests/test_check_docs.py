# SPDX-License-Identifier: Apache-2.0
"""check_docs.py must pass a good tree and fail on every seeded defect."""

from collections.abc import Callable
from pathlib import Path

import build_site
import check_docs
import pytest
from treekit import Tree


def problems(tree: Tree) -> list[str]:
    return check_docs.check(tree.cfg())


def test_good_tree_passes(tree: Tree) -> None:
    assert problems(tree) == []


def test_cli_exit_codes(tree: Tree) -> None:
    args = ["--docs", str(tree.docs), "--repo-root", str(tree.root)]
    assert check_docs.main(args) == 0
    tree.write("docs/stray.md", "# Stray\n")
    assert check_docs.main(args) == 1


def seed_orphan(tree: Tree) -> None:
    tree.write("docs/stray.md", "# Stray\n\n## A\n")


def seed_excluded_is_not_orphan(tree: Tree) -> None:
    tree.write("docs/private/notes.md", "# private\n")
    tree.write("docs/prompts/p.md", "# prompt\n")
    tree.write("docs/tools/t.md", "# tool\n")


def seed_broken_link(tree: Tree) -> None:
    tree.append("docs/alpha.md", '\n<p><a href="nowhere.html">gone</a></p>\n')


def seed_broken_anchor(tree: Tree) -> None:
    tree.append("docs/alpha.md", "\nSee [x](deep/beta.md#no-such-section).\n")


def seed_broken_local_anchor(tree: Tree) -> None:
    tree.append("docs/alpha.md", "\nSee [x](#no-such-section).\n")


def seed_missing_alt(tree: Tree) -> None:
    tree.write("docs/pic.png", "not really a png")
    tree.append("docs/alpha.md", "\n![](pic.png)\n")


def test_a_decorative_icon_may_have_empty_alt_only_when_hidden(tree: Tree) -> None:
    tree.write("docs/pic.png", "not really a png")
    tree.append("docs/alpha.md", '\n<img src="pic.png" alt="" aria-hidden="true">\n')
    assert not [p for p in problems(tree) if "alt" in p]
    tree.append("docs/alpha.md", '\n<img src="pic.png" alt="">\n')
    assert [p for p in problems(tree) if "image without alt" in p]


def seed_external_host(tree: Tree) -> None:
    tree.append("docs/alpha.md", "\nSee [x](https://example.com/page).\n")


def seed_http(tree: Tree) -> None:
    tree.append("docs/alpha.md", "\nSee [x](http://github.com/example/fixture).\n")


def seed_heading_skip(tree: Tree) -> None:
    tree.append("docs/alpha.md", "\n## Skips\n\n#### Too deep\n")


def seed_duplicate_id(tree: Tree) -> None:
    tree.append("docs/alpha.md", "\n### Same\n\ntext\n\n### Same\n\ntext\n")


def seed_unbalanced_html(tree: Tree) -> None:
    tree.append("docs/alpha.md", "\n<div>never closed\n")


def seed_missing_source(tree: Tree) -> None:
    (tree.docs / "deep" / "beta.md").unlink()


def seed_bad_front_matter(tree: Tree) -> None:
    tree.write("docs/deep/beta.md", '+++\ntitle = "Beta\n+++\n\n## x\n')


def seed_unknown_front_matter_key(tree: Tree) -> None:
    tree.write("docs/deep/beta.md", '+++\ntitle = "Beta"\ngroup = "g2"\nbogus = 1\n+++\n\n## x\n')


def seed_bad_repo_link(tree: Tree) -> None:
    tree.append("docs/alpha.md", "\n[x](repo:does/not/exist)\n")


def seed_unknown_group(tree: Tree) -> None:
    tree.write("docs/deep/beta.md", '+++\ntitle = "Beta"\ngroup = "nope"\n+++\n\n## x\n')


def seed_unregistered_md_link(tree: Tree) -> None:
    tree.write("docs/other.md", "# other\n")
    tree.append("docs/alpha.md", "\n[x](other.md)\n")


DEFECTS: list[tuple[str, Callable[[Tree], None], str]] = [
    ("orphan", seed_orphan, "orphan page: docs/stray.md"),
    ("broken-link", seed_broken_link, "broken link nowhere.html"),
    ("broken-anchor", seed_broken_anchor, "broken anchor"),
    ("broken-local-anchor", seed_broken_local_anchor, "broken anchor #no-such-section"),
    ("missing-alt", seed_missing_alt, "image without alt"),
    ("external-host", seed_external_host, "external host 'example.com'"),
    ("http-url", seed_http, "http://"),
    ("heading-skip", seed_heading_skip, "heading level skips"),
    ("duplicate-id", seed_duplicate_id, "duplicate id 'same'"),
    ("unbalanced-html", seed_unbalanced_html, "unbalanced HTML"),
    ("missing-source", seed_missing_source, "build failed"),
    ("bad-front-matter", seed_bad_front_matter, "front matter is not valid TOML"),
    ("unknown-front-matter-key", seed_unknown_front_matter_key, "unknown front matter keys"),
    ("bad-repo-link", seed_bad_repo_link, "repo:does/not/exist does not exist"),
    ("unknown-group", seed_unknown_group, "is not declared in site.toml"),
    ("unregistered-md-link", seed_unregistered_md_link, "not a registered page"),
]


@pytest.mark.parametrize(("name", "seed", "expect"), DEFECTS, ids=[d[0] for d in DEFECTS])
def test_check_fails_on_seeded_defect(
    tree: Tree, name: str, seed: Callable[[Tree], None], expect: str
) -> None:
    seed(tree)
    found = problems(tree)
    assert any(expect in p for p in found), f"{name}: expected {expect!r} in {found}"


def test_loopback_http_is_allowed_but_external_http_is_not(tree: Tree) -> None:
    # Loopback http:// (Oaken's own API and Ollama examples) is allowed; a real host still fails.
    tree.append(
        "docs/alpha.md", "\nRun Ollama at `http://127.0.0.1:11434` or `http://localhost:11434`.\n"
    )
    assert not [p for p in problems(tree) if "http://" in p]
    tree.append("docs/alpha.md", "\nSee [x](http://example.org/page).\n")
    assert any("http://" in p for p in problems(tree))


def test_excluded_paths_are_not_orphans(tree: Tree) -> None:
    seed_excluded_is_not_orphan(tree)
    assert problems(tree) == []


def test_extra_allowed_host_for_links_only(tree: Tree) -> None:
    tree.write(
        "docs/site.toml",
        (tree.docs / "site.toml").read_text() + '\n[check]\nallow_hosts = ["example.com"]\n',
    )
    tree.append("docs/alpha.md", "\nSee [x](https://example.com/page).\n")
    assert problems(tree) == []
    tree.append("docs/alpha.md", '\n<script src="https://example.com/x.js"></script>\n')
    assert any("example.com" in p for p in problems(tree))


def test_cdn_tag_without_integrity_fails(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    import pagebase

    real = pagebase.cdn_tags

    def broken(with_mermaid: bool) -> dict[str, object]:
        tags = real(with_mermaid)
        tags["js"] = {**tags["js"], "integrity": "sha384-wrong"}
        return tags

    monkeypatch.setattr(pagebase, "cdn_tags", broken)
    assert any("pinned integrity" in p for p in problems(tree))


def test_nondeterministic_output_fails(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    counter = iter(range(1000))
    monkeypatch.setattr(build_site, "project_version", lambda _root: f"v{next(counter)}")
    assert any("non-deterministic" in p for p in problems(tree))


def test_extra_check_hook(tree: Tree, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hook = tmp_path / "check_commands.py"
    hook.write_text("def check(cfg):\n    return ['command block failed']\n")
    monkeypatch.setattr(check_docs, "HERE", tmp_path)
    assert "command block failed" in problems(tree)
