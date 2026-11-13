#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build the Oaken Core website from docs/ into docs/ (committed, served by GitHub Pages).

    uv run --group docs python docs/tools/build_site.py [--out DIR] [--serve] [--strict]

With no --out the generated HTML is written into docs/ next to the Markdown sources, and
GitHub Pages serves it from the main branch's /docs folder. The generated files are listed in
docs/.site-manifest, so a rebuild replaces only those and never touches your sources.

Output is deterministic: no timestamps, sorted iteration, LF newlines, so two builds of the
same tree are byte-identical. All internal links are relative, so the site works under
/oaken-core/, from file://, and from --serve.
"""

import argparse
import contextlib
import functools
import http.server
import re
import shutil
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import generated
import pagebase
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from model import (
    BuildError,
    Config,
    Page,
    load_config,
    page_from_markdown,
    project_version,
)
from render import Renderer, asset_prefix, rel_href

HERE = Path(__file__).resolve().parent
CARDS_MARKER = "<p>{{doc-cards}}</p>"
HOME_ID = "index"
TODO_MARKER = "TODO(user)"
MANIFEST = ".site-manifest"


# The Core console mockup lives beside the core app. It is copied under docs/ with its CDN tags
# pinned.
CORE_DEMO_SRC = Path("apps/core/oakencore-templates")
CORE_DEMO_OUT = "demo-core"
_FONT_LINK = re.compile(r"[ \t]*<link[^>]*(?:fonts\.googleapis|fonts\.gstatic)[^>]*>\n")
_CDN_LINK = re.compile(r'<link href="(https://cdn\.jsdelivr\.net/[^"]+)" rel="stylesheet">')
_CDN_SCRIPT = re.compile(r'<script src="(https://cdn\.jsdelivr\.net/[^"]+)"></script>')


def _pin_cdn(text: str) -> str:
    """Give every bare jsdelivr CDN tag the pinned integrity+crossorigin from sri.toml, and drop
    Google Fonts links (the mockups fall back to system fonts). So a demo page passes the same
    link checks as every other page. Raises if a page names a CDN URL that is not pinned."""
    pinned = {v["url"]: v["integrity"] for v in pagebase.sri().values()}

    def pin(m: re.Match[str]) -> str:
        url = m.group(1)
        if url not in pinned:
            raise BuildError(f"demo uses a CDN URL that is not pinned in sri.toml: {url}")
        return m.group(0).replace(">", f' integrity="{pinned[url]}" crossorigin="anonymous">', 1)

    text = _FONT_LINK.sub("", text)
    return _CDN_LINK.sub(pin, _CDN_SCRIPT.sub(pin, text))


def _demo_tree(cfg: Config, src_rel: Path, out_prefix: str) -> dict[str, bytes]:
    """Copy one demo mockup tree under docs/, pinning CDN tags in every HTML file."""
    src = cfg.repo_root / src_rel
    if not src.is_dir():
        return {}
    out: dict[str, bytes] = {}
    for path in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = f"{out_prefix}/{path.relative_to(src).as_posix()}"
        data = path.read_bytes()
        if path.suffix == ".html":
            data = _pin_cdn(data.decode("utf-8")).encode("utf-8")
        out[rel] = data
    return out


def demo_files(cfg: Config) -> dict[str, bytes]:
    """The clickable Oaken Core console demo, published under docs/demo-core/."""
    return _demo_tree(cfg, CORE_DEMO_SRC, CORE_DEMO_OUT)


def make_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(HERE / "templates")),
        autoescape=select_autoescape(["html"], default=True),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def collect_pages(cfg: Config) -> list[Page]:
    """Every registered page, in registry order. Raises BuildError on any registry problem."""
    generated.load_all()
    group_ids = {g.id for g in cfg.groups}
    pages: list[Page] = []
    seen: set[str] = set()
    for entry in cfg.entries:
        pid = entry["id"]
        if pid in seen or pid == HOME_ID:
            raise BuildError(f"site.toml: duplicate or reserved page id {pid!r}")
        seen.add(pid)
        if entry.get("generated"):
            page = generated.get(pid)(cfg)
            page.id = pid  # the registry decides where a generated page lives
            for key in ("title", "subtitle", "lead", "group", "order", "description"):
                if key in entry:
                    setattr(page, key, entry[key])
        else:
            page = page_from_markdown(cfg, entry)
        if page.group not in group_ids:
            raise BuildError(f"page {pid!r}: group {page.group!r} is not declared in site.toml")
        pages.append(page)
    return pages


def check_todos(pages: list[Page]) -> None:
    hits = [p.id for p in pages if TODO_MARKER in p.body_md or TODO_MARKER in p.lead]
    if hits:
        raise BuildError(f"--strict: {TODO_MARKER} is still present in: {', '.join(sorted(hits))}")


def _nav(cfg: Config, pages: list[Page], current: Page | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in cfg.groups:
        members = sorted((p for p in pages if p.group == g.id), key=lambda p: (p.order, p.title))
        if not members:
            continue
        out.append(
            {
                "name": g.name,
                "pages": [
                    {
                        "title": p.title,
                        "href": rel_href(current.id if current else HOME_ID, p.id),
                        "current": current is not None and p.id == current.id,
                    }
                    for p in members
                ],
            }
        )
    return out


def _css_tokens() -> str:
    return (
        f":root{{--accent:{pagebase.ACCENT_LIGHT};--accent-soft:{pagebase.ACCENT_SOFT_LIGHT}}}"
        f'[data-bs-theme="dark"]{{--accent:{pagebase.ACCENT_DARK};'
        f"--accent-soft:{pagebase.ACCENT_SOFT_DARK}}}"
    )


def _is_safe_out(out: Path) -> bool:
    return not out.exists() or not any(out.iterdir()) or (out / ".nojekyll").exists()


def build(cfg: Config, out: Path, strict: bool = False) -> list[str]:
    """Write the site to ``out`` and return the list of files written (relative, sorted)."""
    pages = collect_pages(cfg)
    home = page_from_markdown(cfg, {"id": HOME_ID, "file": cfg.home})
    if strict:
        check_todos(pages + [home])
    renderer = Renderer(cfg, pages + [home])
    env = make_env()

    publish = out.resolve() == cfg.docs_dir.resolve()
    if not publish:
        if not _is_safe_out(out):
            raise BuildError(
                f"refusing to overwrite {out}: not empty and not a previous site build"
            )
        if out.exists():
            shutil.rmtree(out)
    written: dict[str, bytes] = {}

    version = project_version(cfg.repo_root)
    # No commit SHA or date: the HTML is committed, so it can never name its own commit.
    footer = f"{cfg.title} {version} · Apache-2.0"
    common: dict[str, Any] = {
        "site": cfg,
        "head_js": pagebase.HEAD_JS,
        "footer": footer,
    }

    for page in pages:
        rendered = renderer.render(page)
        prefix = asset_prefix(page.id)
        html_text = env.get_template("page.html").render(
            **common,
            page=page,
            rendered=rendered,
            prefix=prefix,
            home_href=rel_href(page.id, HOME_ID),
            nav=_nav(cfg, pages, page),
            cdn=pagebase.cdn_tags(rendered.has_mermaid),
            doc_title=f"{page.title} · {cfg.title}",
            description=page.description or page.lead or cfg.description,
        )
        written[page.out] = html_text.encode("utf-8")

    home_rendered = renderer.render_plain(home)
    cards = [
        {
            "kicker": next((g.name for g in cfg.groups if g.id == p.group), ""),
            "title": p.title,
            "blurb": p.description or p.lead,
            "href": rel_href(HOME_ID, p.id),
        }
        for g in cfg.groups
        for p in sorted((p for p in pages if p.group == g.id), key=lambda p: (p.order, p.title))
    ]
    cards_html = env.get_template("cards.html").render(cards=cards)
    home_rendered.intro_html = home_rendered.intro_html.replace(CARDS_MARKER, cards_html)
    subtitle = home.subtitle if home.subtitle != home.title else ""
    demo = demo_files(cfg)
    written["index.html"] = (
        env.get_template("home.html")
        .render(
            **common,
            page=home,
            rendered=home_rendered,
            prefix="",
            has_demo=bool(demo),
            cdn=pagebase.cdn_tags(home_rendered.has_mermaid),
            doc_title=f"{cfg.title} · {subtitle}" if subtitle else cfg.title,
            description=home.description or home.lead or cfg.description,
        )
        .encode("utf-8")
    )

    written["404.html"] = (
        env.get_template("404.html")
        .render(
            **common,
            cdn=pagebase.cdn_tags(False),
            css_tokens=_css_tokens(),
            base_path=cfg.site_base_path,
        )
        .encode("utf-8")
    )
    for name in pagebase.BRAND_FILES:
        written[f"assets/brand/{name}"] = (pagebase.BRAND_DIR / name).read_bytes()
    written.update(demo)
    written["assets/site.css"] = pagebase.CSS.encode("utf-8")
    written["assets/site.js"] = pagebase.SITE_JS.encode("utf-8")
    urls = [cfg.site_url] + [cfg.site_url + p.out for p in sorted(pages, key=lambda p: p.id)]
    sitemap = ['<?xml version="1.0" encoding="UTF-8"?>']
    sitemap.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    sitemap += [f"  <url><loc>{escape(u)}</loc></url>" for u in urls]
    sitemap.append("</urlset>\n")
    written["sitemap.xml"] = "\n".join(sitemap).encode("utf-8")
    robots = f"User-agent: *\nAllow: /\nSitemap: {cfg.site_url}sitemap.xml\n"
    written["robots.txt"] = robots.encode()
    written[".nojekyll"] = b""

    if publish:
        _remove_stale(out, set(written))
        written[MANIFEST] = ("\n".join(sorted(written)) + "\n").encode("utf-8")
    for rel in sorted(written):
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(written[rel])
    return sorted(written)


def published_files(docs_dir: Path) -> list[str]:
    """Files a previous publish wrote into docs/ (empty if it was never published)."""
    manifest = docs_dir / MANIFEST
    if not manifest.is_file():
        return []
    return [ln for ln in manifest.read_text(encoding="utf-8").splitlines() if ln]


def _remove_stale(out: Path, keep: set[str]) -> None:
    """Delete files an earlier publish wrote that this build no longer produces."""
    for rel in published_files(out):
        target = (out / rel).resolve()
        if rel in keep or out.resolve() not in target.parents:
            continue
        target.unlink(missing_ok=True)
        with contextlib.suppress(OSError):  # directory not empty: it holds sources
            target.parent.rmdir()


def serve(out: Path, port: int) -> None:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"Serving {out} at http://127.0.0.1:{port}/ (Ctrl+C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


def main(argv: list[str] | None = None) -> int:
    default_docs = HERE.parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--docs", type=Path, default=default_docs, help="docs directory (site.toml)")
    ap.add_argument("--repo-root", type=Path, default=None)
    ap.add_argument(
        "--out", type=Path, default=None, help="output dir (default: docs/, the published site)"
    )
    ap.add_argument("--serve", action="store_true", help="serve the result on 127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--strict", action="store_true", help=f"fail if {TODO_MARKER} remains")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.docs, args.repo_root)
        out = (args.out or cfg.docs_dir).resolve()
        files = build(cfg, out, strict=args.strict)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Built {len(files)} files into {out}")
    if args.serve:
        serve(out, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
