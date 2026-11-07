# SPDX-License-Identifier: Apache-2.0
"""Site registry (site.toml), page front matter and the Page structure every builder returns."""

import fnmatch
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_EXCLUDES = ["prompts/**", "tools/**"]
DEFAULT_ALLOW_HOSTS = ["github.com"]
FRONT_KEYS = {"title", "subtitle", "lead", "badges", "group", "order", "description"}
PAGE_KEYS = FRONT_KEYS | {"id", "file", "source", "generated"}


class BuildError(Exception):
    """A problem that makes the site unbuildable (front matter, missing source, repo: link)."""


@dataclass
class Group:
    id: str
    name: str


@dataclass
class Page:
    """One page of the site. Markdown files and generated pages both produce this."""

    id: str  # output path without ".html", e.g. "architecture/overview"
    title: str
    body_md: str
    subtitle: str = ""
    lead: str = ""
    badges: list[tuple[str, str]] = field(default_factory=list)
    group: str = ""
    order: int = 100
    description: str = ""
    origin: Path | None = None  # file the Markdown came from; None for generated pages
    origin_root: bool = False  # True when the file lives at the repo root (README-style)
    extra_css: str = ""  # page-specific CSS, emitted in <style> (generated pages only)
    extra_js: str = ""  # page-specific script, emitted inline at the end of <body>

    @property
    def out(self) -> str:
        return self.id + ".html"


@dataclass
class Config:
    docs_dir: Path
    repo_root: Path
    title: str
    description: str
    repo_url: str
    site_url: str
    branch: str
    home: str
    groups: list[Group]
    entries: list[dict[str, Any]]
    excludes: list[str]
    allow_hosts: list[str]

    @property
    def site_base_path(self) -> str:
        """The URL path the site is served under, e.g. "/oaken-core/"."""
        m = re.match(r"https?://[^/]+(/.*)?$", self.site_url)
        path = (m.group(1) if m else None) or "/"
        return path if path.endswith("/") else path + "/"


def load_config(docs_dir: Path, repo_root: Path | None = None) -> Config:
    docs_dir = docs_dir.resolve()
    repo_root = (repo_root or docs_dir.parent).resolve()
    toml_path = docs_dir / "site.toml"
    if not toml_path.is_file():
        raise BuildError(f"{toml_path}: site registry not found")
    try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise BuildError(f"{toml_path}: {exc}") from exc
    site = data.get("site", {})
    for key in ("title", "repo", "url", "home"):
        if key not in site:
            raise BuildError(f"site.toml: [site] is missing '{key}'")
    groups = [Group(g["id"], g["name"]) for g in data.get("groups", [])]
    entries = list(data.get("pages", []))
    for e in entries:
        unknown = set(e) - PAGE_KEYS
        if unknown:
            raise BuildError(f"site.toml: page {e.get('id')!r} has unknown keys {sorted(unknown)}")
        if "id" not in e:
            raise BuildError("site.toml: a [[pages]] entry has no 'id'")
    return Config(
        docs_dir=docs_dir,
        repo_root=repo_root,
        title=site["title"],
        description=site.get("description", ""),
        repo_url=site["repo"].rstrip("/"),
        site_url=site["url"].rstrip("/") + "/",
        branch=site.get("branch", "main"),
        home=site["home"],
        groups=groups,
        entries=entries,
        excludes=DEFAULT_EXCLUDES + list(data.get("exclude", [])),
        allow_hosts=DEFAULT_ALLOW_HOSTS + list(data.get("check", {}).get("allow_hosts", [])),
    )


def is_excluded(cfg: Config, rel: str) -> bool:
    """rel is a posix path relative to docs/. '*' crosses '/': 'tools/**' is everything below."""
    return any(fnmatch.fnmatch(rel, pat) for pat in cfg.excludes)


def split_front_matter(text: str, where: str) -> tuple[dict[str, Any], str]:
    """Split a '+++'-fenced TOML front matter block off the top of a Markdown file."""
    if not text.startswith("+++"):
        return {}, text
    lines = text.split("\n")
    try:
        end = lines.index("+++", 1)
    except ValueError as exc:
        raise BuildError(f"{where}: front matter opened with +++ is never closed") from exc
    try:
        meta = tomllib.loads("\n".join(lines[1:end]))
    except tomllib.TOMLDecodeError as exc:
        raise BuildError(f"{where}: front matter is not valid TOML: {exc}") from exc
    unknown = set(meta) - FRONT_KEYS
    if unknown:
        raise BuildError(f"{where}: unknown front matter keys {sorted(unknown)}")
    return meta, "\n".join(lines[end + 1 :])


def _badges(raw: Any, where: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for b in raw or []:
        if not (isinstance(b, list) and len(b) == 2 and all(isinstance(x, str) for x in b)):
            raise BuildError(f"{where}: each badge must be [class, text]")
        out.append((b[0], b[1]))
    return out


def page_from_markdown(cfg: Config, entry: dict[str, Any]) -> Page:
    """Read the Markdown file named by a registry entry; entry keys override front matter."""
    pid = entry["id"]
    if "source" in entry:
        path, root = cfg.repo_root / entry["source"], True
    elif "file" in entry:
        path, root = cfg.docs_dir / entry["file"], False
    else:
        raise BuildError(f"page {pid!r}: needs 'file', 'source' or generated = true")
    if not path.is_file():
        raise BuildError(f"page {pid!r}: source file {path} does not exist")
    meta, body = split_front_matter(path.read_text(encoding="utf-8"), str(path))
    meta.update({k: v for k, v in entry.items() if k in FRONT_KEYS})
    title = meta.get("title")
    m = re.match(r"\s*# (.+)\n", body)
    if m:  # the page title comes from front matter/registry; a leading H1 is only a fallback
        title = title or m.group(1).strip()
        body = body[m.end() :]
    if not title:
        raise BuildError(f"{path}: no title (front matter 'title' or a leading '# ' heading)")
    return page_from_meta(pid, str(title), body, meta, path, root)


def page_from_meta(
    pid: str, title: str, body: str, meta: dict[str, Any], origin: Path | None, root: bool
) -> Page:
    where = str(origin or pid)
    order = meta.get("order", 100)
    if not isinstance(order, int):
        raise BuildError(f"{where}: 'order' must be an integer")
    return Page(
        id=pid,
        title=title,
        body_md=body,
        subtitle=str(meta.get("subtitle", title)),
        lead=str(meta.get("lead", "")),
        badges=_badges(meta.get("badges"), where),
        group=str(meta.get("group", "")),
        order=order,
        description=str(meta.get("description", "")),
        origin=origin,
        origin_root=root,
    )


def git_info(repo_root: Path) -> str:
    """'<short sha> · <commit date>' or 'dev'. Never wall-clock time, so builds are reproducible."""
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "log", "-1", "--format=%h %cs"],  # noqa: S607
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "dev"
    if not out:
        return "dev"
    sha, date = out.split()
    return f"{sha} · {date}"


def project_version(repo_root: Path) -> str:
    init = repo_root / "src" / "oaken" / "__init__.py"
    try:
        m = re.search(r'__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"))
    except OSError:
        return "dev"
    return m.group(1) if m else "dev"


def repo_link(cfg: Config, path: str, where: str) -> str:
    """GitHub URL for a repository path; BuildError if it is outside the repo or missing."""
    target = (cfg.repo_root / path).resolve()
    try:
        target.relative_to(cfg.repo_root)
    except ValueError:
        raise BuildError(f"{where}: repo:{path} points outside the repository") from None
    if not target.exists():
        raise BuildError(f"{where}: repo:{path} does not exist")
    kind = "tree" if target.is_dir() else "blob"
    return f"{cfg.repo_url}/{kind}/{cfg.branch}/{path.rstrip('/')}"
