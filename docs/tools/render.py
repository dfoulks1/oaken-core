# SPDX-License-Identifier: Apache-2.0
"""Markdown to HTML for the site: sections, heading ids, repo: links, code and mermaid blocks."""

import html
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict
from mdit_py_plugins.container import container_plugin
from model import BuildError, Config, Page, repo_link
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.util import ClassNotFound

SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


@dataclass
class Section:
    id: str
    title_html: str
    title_text: str
    body_html: str


@dataclass
class Rendered:
    intro_html: str = ""
    sections: list[Section] = field(default_factory=list)
    has_mermaid: bool = False


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower(), flags=re.ASCII)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s or "section"


def rel_href(from_page_id: str, to_page_id: str) -> str:
    """Relative URL from one page's output file to another's."""
    start = posixpath.dirname(from_page_id + ".html") or "."
    return posixpath.relpath(to_page_id + ".html", start=start)


def asset_prefix(page_id: str) -> str:
    depth = page_id.count("/")
    return "../" * depth


class Renderer:
    def __init__(self, cfg: Config, pages: list[Page]) -> None:
        self.cfg = cfg
        self.pages = pages
        self.by_origin: dict[Path, Page] = {p.origin.resolve(): p for p in pages if p.origin}
        self.md = self._make_md()

    # -- markdown-it setup ------------------------------------------------------------------
    def _make_md(self) -> MarkdownIt:
        md = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])
        md.use(container_plugin, name="callout")
        formatter = HtmlFormatter(nowrap=True)

        # Render rules are bound to the renderer, so they receive it as the first argument.
        def fence(_r: Any, tokens: Any, idx: int, options: OptionsDict, env: EnvType) -> str:
            tok = tokens[idx]
            lang = tok.info.strip().split()[0] if tok.info.strip() else ""
            if lang == "mermaid":
                env["has_mermaid"] = True
                src = html.escape(tok.content, quote=False)
                return (
                    f'<pre class="mermaid">{src}</pre>'
                    f"<noscript><pre><code>{src}</code></pre></noscript>\n"
                )
            try:
                lexer: Any = get_lexer_by_name(lang) if lang else TextLexer()
            except ClassNotFound:
                lexer = TextLexer()
            body = highlight(tok.content, lexer, formatter)
            cls = f' class="language-{html.escape(lang)}"' if lang else ""
            return (
                f'<div class="code"><pre class="highlight"><code{cls}>{body}</code></pre></div>\n'
            )

        def table_open(_r: Any, tokens: Any, idx: int, options: OptionsDict, env: EnvType) -> str:
            cls = "table table-sm table-striped align-middle"
            return f'<div class="table-responsive"><table class="{cls}">\n'

        def table_close(_r: Any, tokens: Any, idx: int, options: OptionsDict, env: EnvType) -> str:
            return "</table></div>\n"

        md.add_render_rule("fence", fence)
        md.add_render_rule("table_open", table_open)
        md.add_render_rule("table_close", table_close)
        return md

    # -- links ------------------------------------------------------------------------------
    def _repo_url(self, path: str, where: str) -> str:
        return repo_link(self.cfg, path, where)

    def rewrite_href(self, href: str, page: Page) -> str:
        where = str(page.origin or page.id)
        if href.startswith("repo:"):
            return self._repo_url(href[5:], where)
        if href.startswith("#") or SCHEME.match(href) or href.startswith("//"):
            return href
        path, _, frag = href.partition("#")
        base = page.origin.parent if page.origin else self.cfg.docs_dir
        target = (base / path).resolve()
        if path.endswith(".md"):
            dest = self.by_origin.get(target)
            if dest is not None:
                return rel_href(page.id, dest.id) + (f"#{frag}" if frag else "")
            if page.origin_root and target.exists():
                return self._repo_url(target.relative_to(self.cfg.repo_root).as_posix(), where)
            raise BuildError(f"{where}: link {href!r} is not a registered page (or use repo:)")
        if page.origin_root and path and target.exists():
            try:
                rel = target.relative_to(self.cfg.repo_root).as_posix()
            except ValueError:
                return href
            return self._repo_url(rel, where)
        return href

    # -- rendering --------------------------------------------------------------------------
    def _prepare(self, tokens: list[Token], page: Page) -> None:
        for tok in tokens:
            if tok.type == "heading_open":
                continue
            if tok.type == "inline" and tok.children:
                for child in tok.children:
                    if child.type == "link_open":
                        child.attrSet("href", self.rewrite_href(str(child.attrGet("href")), page))
        # heading ids from the text of the following inline token
        for i, tok in enumerate(tokens):
            if tok.type == "heading_open" and tokens[i + 1].type == "inline":
                tok.attrSet("id", slugify(tokens[i + 1].content))

    def _inline(self, tok: Token, env: EnvType) -> str:
        return str(
            cast(RendererHTML, self.md.renderer).renderInline(
                tok.children or [], self.md.options, env
            )
        )

    def render_plain(self, page: Page) -> Rendered:
        """The page body as one HTML string (used by the home page); no numbered sections."""
        env: EnvType = {}
        tokens = self.md.parse(page.body_md, env)
        self._prepare(tokens, page)
        out = Rendered(intro_html=str(self.md.renderer.render(tokens, self.md.options, env)))
        out.has_mermaid = bool(env.get("has_mermaid"))
        return out

    def render(self, page: Page) -> Rendered:
        env: EnvType = {}
        tokens = self.md.parse(page.body_md, env)
        self._prepare(tokens, page)
        # split into groups at each level-2 heading
        groups: list[list[Token]] = [[]]
        for tok in tokens:
            if tok.type == "heading_open" and tok.tag == "h2":
                groups.append([])
            groups[-1].append(tok)
        out = Rendered()
        rr = cast(RendererHTML, self.md.renderer)
        out.intro_html = str(rr.render(groups[0], self.md.options, env))
        for g in groups[1:]:
            head_inline = g[1]
            sid = str(g[0].attrGet("id"))
            out.sections.append(
                Section(
                    id=sid,
                    title_html=self._inline(head_inline, env),
                    title_text=head_inline.content,
                    body_html=str(rr.render(g[3:], self.md.options, env)),
                )
            )
        out.has_mermaid = bool(env.get("has_mermaid"))
        return out
