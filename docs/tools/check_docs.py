#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Guard for the documentation site. Exit status 1 if anything below fails.

    uv run --group docs python docs/tools/check_docs.py

Builds the site to a temporary directory (twice) and fails on:

* a Markdown file under docs/ that is neither registered in site.toml nor excluded (orphan),
  a registered page with no source, bad front matter, a bad ``repo:`` link;
* broken internal links and anchors, duplicate ids, heading-level skips, images without alt;
* any ``http://`` URL (except loopback: 127.0.0.1/localhost/[::1], used by Oaken's own API and
  Ollama examples), any external host outside the allowlist, a CDN tag without an
  integrity hash, unbalanced HTML;
* output that differs between the two builds (non-determinism);
* committed HTML in docs/ that differs from a fresh build (run build_site.py and commit).

Extension hook: if ``docs/tools/check_commands.py`` exists it is imported and its
``check(cfg) -> list[str]`` is called; every string returned is reported as a problem.
Prompt 20 adds that module, which runs the fenced ``<!-- check -->`` shell blocks in the docs.
"""

import argparse
import importlib.util
import posixpath
import re
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pagebase
from build_site import build, published_files
from model import BuildError, Config, is_excluded, load_config

HERE = Path(__file__).resolve().parent
CDN_HOST = "cdn.jsdelivr.net"
VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
    "track", "wbr",
}  # fmt: skip
ALLOWED_SCHEMES = {"mailto"}


class PageScan(HTMLParser):
    """Collects what the checks need from one generated page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.links: list[tuple[str, str, dict[str, str | None]]] = []  # tag, url, attrs
        self.images_without_alt: list[str] = []
        self.headings: list[int] = []
        self.problems: list[str] = []
        self._stack: list[str] = []
        self._in_main = False
        self.text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if a.get("id"):
            self.ids.append(str(a["id"]))
        if tag == "main":
            self._in_main = True
        if self._in_main and re.fullmatch(r"h[1-6]", tag):
            self.headings.append(int(tag[1]))
        decorative = a.get("alt") == "" and a.get("aria-hidden") == "true"  # icons beside text
        if tag == "img" and not (a.get("alt") or "").strip() and not decorative:
            self.images_without_alt.append(str(a.get("src")))
        for attr in ("href", "src"):
            if a.get(attr) is not None and tag in {"a", "link", "script", "img"}:
                self.links.append((tag, str(a[attr]), a))
        if tag not in VOID:
            self._stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Self-closing syntax (<br/>, <img/>): record it, but nothing to balance.
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID:
            return
        if not self._stack or self._stack[-1] != tag:
            top = self._stack[-1] if self._stack else "nothing"
            self.problems.append(f"unbalanced HTML: </{tag}> where {top} is open")
            if tag in self._stack:
                while self._stack and self._stack.pop() != tag:
                    pass
            return
        self._stack.pop()
        if tag == "main":
            self._in_main = False

    def close(self) -> None:
        super().close()
        if self._stack:
            self.problems.append(f"unbalanced HTML: unclosed <{self._stack[-1]}>")


def _scan(path: Path) -> PageScan:
    scan = PageScan()
    scan.text = path.read_text(encoding="utf-8")
    scan.feed(scan.text)
    scan.close()
    return scan


def orphans(cfg: Config) -> list[str]:
    registered = {cfg.home} | {e["file"] for e in cfg.entries if "file" in e}
    out = []
    for path in sorted(cfg.docs_dir.rglob("*.md")):
        rel = path.relative_to(cfg.docs_dir).as_posix()
        if rel in registered or is_excluded(cfg, rel):
            continue
        out.append(f"orphan page: docs/{rel} is neither registered in site.toml nor excluded")
    return out


def _check_page(cfg: Config, site: Path, page: Path, scans: dict[Path, PageScan]) -> list[str]:
    rel = page.relative_to(site).as_posix()
    scan = scans[page]
    probs = [f"{rel}: {p}" for p in scan.problems]
    # Any ``http://`` URL is refused except loopback ones, which are legitimate in the config and
    # Ollama examples (Oaken's own API and model backend run on 127.0.0.1); loopback http carries no
    # exfiltration or downgrade risk. A real external http:// host still fails here.
    for m in re.finditer(r"http://([^\s\"'<>/]+)", scan.text):
        host = m.group(1).rsplit(":", 1)[0].lower()
        if host not in {"127.0.0.1", "localhost", "[::1]", "::1"}:
            probs.append(f"{rel}: contains an http:// URL")
            break
    dups = sorted({i for i in scan.ids if scan.ids.count(i) > 1})
    probs += [f"{rel}: duplicate id {i!r}" for i in dups]
    probs += [f"{rel}: image without alt text ({src})" for src in scan.images_without_alt]
    prev = 0
    for level in scan.headings:
        if level > prev + 1:
            probs.append(f"{rel}: heading level skips from h{prev} to h{level}")
        prev = level
    pinned = {v["url"]: v["integrity"] for v in pagebase.sri().values()}
    for tag, url, attrs in scan.links:
        probs += _check_link(cfg, site, page, scans, tag, url, attrs, pinned)
    return probs


def _check_link(
    cfg: Config,
    site: Path,
    page: Path,
    scans: dict[Path, PageScan],
    tag: str,
    url: str,
    attrs: dict[str, str | None],
    pinned: dict[str, str],
) -> list[str]:
    rel = page.relative_to(site).as_posix()
    parts = urlsplit(url)
    if parts.scheme or url.startswith("//"):
        if url.startswith("//"):
            return [f"{rel}: protocol-relative URL {url}"]
        if parts.scheme == "https":
            host = parts.hostname or ""
            if host == CDN_HOST:
                if url not in pinned:
                    return [f"{rel}: CDN URL {url} is not a pinned asset in sri.toml"]
                if attrs.get("integrity") != pinned[url] or attrs.get("crossorigin") != "anonymous":
                    return [f"{rel}: CDN tag for {url} lacks the pinned integrity/crossorigin"]
                return []
            if tag == "a" and host in cfg.allow_hosts:
                return []
            return [f"{rel}: external host {host!r} is not allowed ({url})"]
        if parts.scheme in ALLOWED_SCHEMES and tag == "a":
            return []
        if parts.scheme == "data" and tag == "link" and attrs.get("rel") == "icon":
            return []
        return [f"{rel}: URL scheme {parts.scheme!r} is not allowed ({url[:60]})"]

    path = unquote(parts.path)
    if path.startswith("/"):  # only the 404 page uses a root-relative link
        base = cfg.site_base_path
        if not path.startswith(base):
            return [f"{rel}: root-relative link {url} is outside {base}"]
        target_rel = path[len(base) :]
    elif not path:
        target_rel = rel
    else:
        target_rel = posixpath.normpath(posixpath.join(posixpath.dirname(rel), path))
    if target_rel in ("", "."):
        target_rel = "index.html"
    elif path.endswith("/") or (site / target_rel).is_dir():
        target_rel = posixpath.join(target_rel, "index.html")
    if target_rel.startswith(".."):
        return [f"{rel}: link {url} escapes the site"]
    target = site / target_rel
    if not target.is_file():
        return [f"{rel}: broken link {url} (no {target_rel})"]
    if parts.fragment and target.suffix == ".html":
        ids = scans[target].ids if target in scans else _scan(target).ids
        if parts.fragment not in ids:
            return [f"{rel}: broken anchor {url} (no id {parts.fragment!r} in {target_rel})"]
    return []


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _stale(cfg: Config, fresh: Path) -> list[str]:
    """The committed HTML in docs/ must equal what a build produces now."""
    published = published_files(cfg.docs_dir)
    if not published:
        return []  # never published (e.g. a fixture tree)
    want = _tree(fresh)
    out: list[str] = []
    for rel in sorted(set(want) | set(published)):
        have = cfg.docs_dir / rel
        if rel not in want:
            out.append(f"stale published file docs/{rel} (rebuild the site)")
        elif not have.is_file() or have.read_bytes() != want[rel]:
            out.append(f"docs/{rel} is out of date; run: uv run python docs/tools/build_site.py")
    return out


EXTRA_CHECKS = ("check_commands.py", "check_prompts.py")  # each has ``check(cfg) -> problems``


def _extra_checks(cfg: Config) -> list[str]:
    problems: list[str] = []
    for name in EXTRA_CHECKS:
        path = HERE / name
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            problems.append(f"cannot load {path}")
            continue
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        problems += [str(p) for p in mod.check(cfg)]
    return problems


def check(cfg: Config) -> list[str]:
    """Return every problem found; an empty list means the docs are good."""
    problems = orphans(cfg)
    with tempfile.TemporaryDirectory(prefix="oaken-docs-") as tmp:
        a, b = Path(tmp) / "a", Path(tmp) / "b"
        try:
            build(cfg, a)
            build(cfg, b)
        except BuildError as exc:
            return [*problems, f"build failed: {exc}"]
        ta, tb = _tree(a), _tree(b)
        if ta != tb:
            diff = sorted(k for k in set(ta) | set(tb) if ta.get(k) != tb.get(k))
            problems.append(f"non-deterministic output; differs between builds: {diff}")
        problems += _stale(cfg, a)
        scans: dict[Path, PageScan] = {}
        pages = sorted(a.rglob("*.html"))
        for page in pages:
            scans[page] = _scan(page)
        for page in pages:
            problems += _check_page(cfg, a, page, scans)
    problems += _extra_checks(cfg)
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--docs", type=Path, default=HERE.parent, help="docs directory (site.toml)")
    ap.add_argument("--repo-root", type=Path, default=None)
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.docs, args.repo_root)
    except BuildError as exc:
        print(f"FAIL: {exc}")
        return 1
    problems = check(cfg)
    for p in problems:
        print(f"FAIL: {p}")
    if problems:
        print(f"{len(problems)} problem(s) found.")
        return 1
    print("Docs OK: built twice with identical output; links, anchors and hosts check out.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
