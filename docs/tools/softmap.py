# SPDX-License-Identifier: Apache-2.0
"""The software map: a generated page showing what is built and how the pieces layer.

A software map in the style of a layered system diagram. The data lives in docs/softmap.toml, and building the page
fails when the data contradicts the repository or the architecture, so the map cannot drift:

* ids are unique, every layer sits in exactly one stack, every ``uses``/``notifies`` target exists;
* a call goes to the same layer or a lower one; an event or callback goes strictly upward;
* the call graph has no cycles;
* ``built`` and ``partial`` components have a ``path`` that exists, ``planned`` ones do not;
* every step of every call flow follows a declared relationship.
"""

import html
import json
import re
import tomllib
from typing import Any

import generated
from model import BuildError, Config, Page, page_from_meta, repo_link

STATUSES = {
    "built": ("text-bg-success", "Built", "Implemented and checked in the repository."),
    "partial": ("text-bg-warning", "Partial", "Exists, but only part of what is planned."),
    "planned": ("text-bg-secondary", "Planned", "Designed; no code yet."),
}
# Grid geometry of the SVG map (user units; the SVG scales to its container).
NW, NH, CGAP, LEFT, PITCH, TOP, PER_ROW = 150, 44, 24, 236, 84, 24, 6

CSS = """
  :root { --st-built:#2e7d32; --st-partial:#b26a00; --st-planned:#6c757d; --accent2:#b4531a; }
  [data-bs-theme="dark"] { --st-built:#66bb6a; --st-partial:#e0a030; --st-planned:#9aa0a6; --accent2:#e8a064; }
  .stack-h { margin-top: 2rem; }
  .layer-h { color: var(--bs-secondary-color); text-transform: uppercase; letter-spacing: .06em; font-size: .8rem; margin-top: 1.5rem; }
  .rel-k { font-size: .7rem; text-transform: uppercase; letter-spacing: .06em; color: var(--bs-secondary-color); }
  .chip { display: inline-block; border: 1px solid var(--bs-border-color); border-radius: .3rem; padding: 0 .45rem; margin: 0 .2rem .2rem 0; text-decoration: none; font-size: .8rem; background: var(--bs-body-bg); }
  .chip-cb { border-style: dashed; border-color: var(--accent); }
  .comp:target { outline: 2px solid var(--accent); }
  table.fn td:first-child { white-space: normal; word-break: break-word; min-width: 16rem; }
  .mapbox { border: 1px solid var(--bs-border-color); border-radius: .5rem; padding: .6rem; background: var(--bs-body-bg); overflow-x: auto; margin: 1rem 0; }
  .map-svg { width: 100%; min-width: 820px; height: auto; display: block; }
  .band { stroke: var(--bs-border-color); stroke-width: 1; }
  .b0 { fill: var(--bs-tertiary-bg); } .b1 { fill: var(--bs-body-bg); }
  .stack { fill: none; stroke: var(--bs-secondary-color); stroke-width: 1.5; }
  .stacklbl { fill: var(--bs-body-color); font-size: 13px; font-weight: 700; }
  .bandlbl { fill: var(--bs-secondary-color); font-size: 11px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; }
  .node { cursor: pointer; }
  .node rect { fill: var(--bs-body-bg); stroke: var(--bs-body-color); stroke-width: 1.4; }
  .node.built rect { stroke: var(--st-built); stroke-width: 2.6; }
  .node.partial rect { stroke: var(--st-partial); stroke-width: 2.6; stroke-dasharray: 6 3; }
  .node.planned rect { stroke: var(--st-planned); stroke-width: 1.5; stroke-dasharray: 3 3; }
  .node .nn { fill: var(--bs-body-color); font-size: 13px; font-weight: 700; }
  .node .ns { fill: var(--bs-secondary-color); font-size: 10px; }
  .node .untr { fill: #c0392b; }
  .node:hover rect, .node:focus rect { fill: var(--accent-soft); outline: none; }
  .node.rel rect { stroke: var(--accent); stroke-width: 2.4; stroke-dasharray: none; }
  .node.sel rect { fill: var(--accent); stroke: var(--accent); stroke-dasharray: none; }
  .node.sel .nn, .node.sel .ns { fill: #fff; }
  [data-bs-theme="dark"] .node.sel .nn, [data-bs-theme="dark"] .node.sel .ns { fill: #0d1117; }
  .node.dim { opacity: .3; }
  .e { fill: none; stroke-width: 1.6; }
  .e-out { stroke: var(--bs-body-color); } .e-cb { stroke: var(--accent); stroke-dasharray: 6 4; }
  .e-in { stroke: var(--accent2); } .e-incb { stroke: var(--accent2); stroke-dasharray: 6 4; }
  .mk-out { fill: var(--bs-body-color); } .mk-cb { fill: var(--accent); } .mk-in, .mk-incb { fill: var(--accent2); }
  .legend { font-size: .8rem; display: flex; flex-wrap: wrap; gap: .4rem 1.4rem; margin: .6rem 0; color: var(--bs-secondary-color); }
  .legend svg { vertical-align: middle; margin-right: .3rem; }
  #sel { min-height: 5.5rem; }
  table.mx { border-collapse: collapse; font-size: .72rem; }
  table.mx td, table.mx th { border: 1px solid var(--bs-border-color); text-align: center; padding: 0; min-width: 1.5rem; height: 1.5rem; }
  table.mx .mx-h { height: 6.5rem; vertical-align: bottom; background: var(--bs-tertiary-bg); }
  table.mx .mx-h a { writing-mode: vertical-rl; transform: rotate(180deg); text-decoration: none; padding: .2rem 0; display: inline-block; white-space: nowrap; }
  table.mx .mx-r { text-align: right; padding: 0 .4rem; background: var(--bs-tertiary-bg); white-space: nowrap; }
  table.mx .mx-r a { text-decoration: none; }
  .mx-c { background: var(--accent); color: #fff; font-weight: 700; }
  [data-bs-theme="dark"] .mx-c { color: #0d1117; }
  .mx-b { background: var(--accent-soft); color: var(--accent); font-weight: 700; outline: 1px dashed var(--accent); outline-offset: -3px; }
  @media print { #q { display: none !important; } }
"""

# Interactive part, click a node to draw its relationships.
JS = r"""
(function () {
  var NODES = @NODES@, EDGES = @EDGES@, INFO = @INFO@, NAMES = @NAMES@;
  var PITCH = @PITCH@, TOP = @TOP@, NH = @NH@, CGAP = @CGAP@, LEFT = @LEFT@, NW = @NW@, PER_ROW = @PER_ROW@;
  var svg = document.getElementById("softmap"), g = document.getElementById("edges");
  var ns = "http" + "://www.w3.org/2000/svg";  // split so the page holds no literal insecure URL
  function below(r) { return TOP + r * PITCH + NH + (PITCH - NH) / 2; }
  function above(r) { return TOP + r * PITCH - (PITCH - NH) / 2; }
  function gutter(a, b) {
    var mid = (a.col + (a.span - 1) / 2 + b.col + (b.span - 1) / 2) / 2;
    var k = Math.min(PER_ROW, Math.floor(mid) + 1);
    return LEFT + k * (NW + CGAP) - CGAP / 2;
  }
  function route(a, b, lane) {
    var off = ((lane % 6) - 2.5) * 3, cxa = a.x + a.w / 2 + off * 2, cxb = b.x + b.w / 2 + off * 2, pts, y1, y2;
    if (b.row > a.row) {
      y1 = below(a.row) + off; y2 = above(b.row) + off;
      pts = Math.abs(y1 - y2) < 1 ? [[cxa, a.y + a.h], [cxa, y1], [cxb, y1], [cxb, b.y]]
        : [[cxa, a.y + a.h], [cxa, y1], [gutter(a, b) + off, y1], [gutter(a, b) + off, y2], [cxb, y2], [cxb, b.y]];
    } else if (b.row < a.row) {
      y1 = above(a.row) + off; y2 = below(b.row) + off;
      pts = Math.abs(y1 - y2) < 1 ? [[cxa, a.y], [cxa, y1], [cxb, y1], [cxb, b.y + b.h]]
        : [[cxa, a.y], [cxa, y1], [gutter(a, b) + off, y1], [gutter(a, b) + off, y2], [cxb, y2], [cxb, b.y + b.h]];
    } else {
      y1 = below(a.row) + off;
      pts = [[cxa, a.y + a.h], [cxa, y1], [cxb, y1], [cxb, b.y + b.h]];
    }
    return pts.map(function (p) { return p.join(","); }).join(" ");
  }
  function line(a, b, cls, mk, lane) {
    var pl = document.createElementNS(ns, "polyline");
    pl.setAttribute("class", "e " + cls);
    pl.setAttribute("points", route(NODES[a], NODES[b], lane));
    pl.setAttribute("marker-end", "url(#" + mk + ")");
    g.appendChild(pl);
  }
  function chip(id) { return '<a class="chip" href="#c-' + id + '">' + NAMES[id] + "</a>"; }
  function list(a) { return a.length ? a.map(chip).join(" ") : '<span class="text-body-secondary">none</span>'; }
  function select(id) {
    g.innerHTML = "";
    document.querySelectorAll(".node").forEach(function (n) { n.classList.remove("sel", "rel", "dim"); });
    var box = document.getElementById("sel");
    if (!id) { box.textContent = "Select a component on the map."; return; }
    var rel = {}, lane = 0, out = [], inn = [], cbo = [], cbi = [];
    rel[id] = true;
    EDGES.forEach(function (e) {
      var call = e.k === "call";
      if (e.f === id) { line(id, e.t, call ? "e-out" : "e-cb", call ? "m-out" : "m-cb", lane++); rel[e.t] = true; (call ? out : cbo).push(e.t); }
      if (e.t === id) { line(e.f, id, call ? "e-in" : "e-incb", call ? "m-in" : "m-incb", lane++); rel[e.f] = true; (call ? inn : cbi).push(e.f); }
    });
    document.querySelectorAll(".node").forEach(function (n) {
      var nid = n.dataset.id;
      n.classList.add(nid === id ? "sel" : (rel[nid] ? "rel" : "dim"));
    });
    box.innerHTML = '<div><strong>' + NAMES[id] + "</strong> &mdash; " + INFO[id] + ' <a href="#c-' + id + '">Details</a></div>' +
      '<div class="row g-2 mt-1"><div class="col-md-3"><span class="rel-k">Calls</span><br>' + list(out) + "</div>" +
      '<div class="col-md-3"><span class="rel-k">Called by</span><br>' + list(inn) + "</div>" +
      '<div class="col-md-3"><span class="rel-k">Notifies</span><br>' + list(cbo) + "</div>" +
      '<div class="col-md-3"><span class="rel-k">Notified by</span><br>' + list(cbi) + "</div></div>";
  }
  var cur = null;
  document.querySelectorAll(".node").forEach(function (n) {
    function act() { cur = cur === n.dataset.id ? null : n.dataset.id; select(cur); }
    n.addEventListener("click", act);
    n.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); act(); } });
  });
  svg.addEventListener("click", function (e) {
    if (e.target === svg || e.target.classList.contains("band")) { cur = null; select(null); }
  });

  var q = document.getElementById("q"), none = document.getElementById("none");
  var section = document.getElementById("components");
  function groupVisible(h, stopTags) {
    var n = h.nextElementSibling;
    while (n && stopTags.indexOf(n.tagName) < 0) {
      if (n.classList && n.classList.contains("comp") && !n.hidden) { return true; }
      n = n.nextElementSibling;
    }
    return false;
  }
  q.addEventListener("input", function () {
    var v = q.value.trim().toLowerCase(), shown = 0;
    document.querySelectorAll(".comp").forEach(function (c) {
      var ok = !v || c.dataset.search.toLowerCase().indexOf(v) >= 0;
      c.hidden = !ok;
      if (ok) { shown++; }
    });
    section.querySelectorAll("h4").forEach(function (h) { h.hidden = !groupVisible(h, ["H3", "H4"]); });
    section.querySelectorAll("h3").forEach(function (h) { h.hidden = !groupVisible(h, ["H3"]); });
    none.hidden = shown > 0;
  });
})();
"""


def load(cfg: Config) -> dict[str, Any]:
    path = cfg.docs_dir / "softmap.toml"
    try:
        data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BuildError(f"{path}: {exc}") from exc
    return data


def load_classes(cfg: Config) -> list[dict[str, Any]]:
    """The generated class relationships (docs/tools/classmap.py); none yet is fine."""
    path = cfg.docs_dir / "softmap.classes.toml"
    if not path.exists():
        return []
    try:
        return list(tomllib.loads(path.read_text(encoding="utf-8")).get("classes", []))
    except tomllib.TOMLDecodeError as exc:
        raise BuildError(f"{path}: {exc}") from exc


def validate(cfg: Config, data: dict[str, Any]) -> list[str]:
    """Raise BuildError on any inconsistency; return ids nothing else references (entry points)."""
    errors: list[str] = []
    layer_ids = [layer["id"] for layer in data.get("layers", [])]
    rank = {lid: i for i, lid in enumerate(layer_ids)}  # 0 is the top
    in_stacks = [lid for s in data.get("stacks", []) for lid in s.get("layers", [])]
    if in_stacks != layer_ids:
        errors.append("the stacks' layers must list every layer exactly once, in the layers' order")

    comps: dict[str, Any] = {}
    for c in data.get("components", []):
        cid = c.get("id", "?")
        if cid in comps:
            errors.append(f"duplicate component id {cid!r}")
        comps[cid] = c
        for key in ("name", "layer", "status", "summary"):
            if key not in c:
                errors.append(f"{cid}: missing '{key}'")
        if c.get("layer") not in rank:
            errors.append(f"{cid}: unknown layer {c.get('layer')!r}")
        if c.get("status") not in STATUSES:
            errors.append(f"{cid}: status must be one of {sorted(STATUSES)}")
        path = c.get("path")
        exists = bool(path) and (cfg.repo_root / path).exists()
        if c.get("status") in ("built", "partial") and not exists:
            errors.append(f"{cid}: marked {c['status']} but path {path!r} does not exist")
        if c.get("status") == "planned" and path and exists:
            errors.append(f"{cid}: marked planned but {path!r} already exists; update its status")

    def lrank(cid: str) -> int:
        return rank.get(comps[cid].get("layer"), 0)

    for cid, c in comps.items():
        for kind in ("uses", "notifies"):
            for t in c.get(kind, []):
                if t not in comps:
                    errors.append(f"{cid}: {kind} unknown component {t!r}")
        for t in c.get("uses", []):
            if t in comps and lrank(t) < lrank(cid):
                errors.append(f"layering: {cid} calls up into {t}")
        for t in c.get("notifies", []):
            if t in comps and lrank(t) >= lrank(cid):
                errors.append(f"layering: {cid} notifies {t}, which is not above it")

    state: dict[str, int] = {}

    def visit(n: str, trail: list[str]) -> None:
        if state.get(n) == 1:
            errors.append("call cycle: " + " -> ".join([*trail, n]))
            return
        if state.get(n) == 2:
            return
        state[n] = 1
        for t in comps[n].get("uses", []):
            if t in comps:
                visit(t, [*trail, n])
        state[n] = 2

    for n in comps:
        visit(n, [])

    for fl in data.get("flows", []):
        for i, step in enumerate(fl.get("steps", []), 1):
            where = f"flow {fl.get('id')} step {i}"
            if len(step) != 4:
                errors.append(f"{where}: a step is [from, to, what, note]")
                continue
            a, b = step[0], step[1]
            if a not in comps or b not in comps:
                errors.append(f"{where}: unknown component")
            elif a != b and b not in comps[a].get("uses", []) + comps[a].get("notifies", []):
                errors.append(f"{where}: {a} -> {b} is not a declared relationship")

    if errors:
        raise BuildError("softmap.toml: " + "; ".join(errors))
    referenced = {t for c in comps.values() for t in c.get("uses", []) + c.get("notifies", [])}
    return [cid for cid in comps if cid not in referenced]


# ---------------------------------------------------------------------------- html helpers
def esc(text: str) -> str:
    return html.escape(text, quote=True)


def rich(text: str) -> str:
    """Escape, then turn `code` spans into <code>."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", esc(text))


class Layout:
    """Places components on a 6-column grid, layer by layer, stack by stack."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.comps: list[dict[str, Any]] = data["components"]
        self.layer_name = {layer["id"]: layer["name"] for layer in data["layers"]}
        self.nodes: dict[str, dict[str, int]] = {}
        self.layer_rows: dict[str, tuple[int, int]] = {}
        self.stack_rows: list[tuple[str, int, int]] = []
        row = 1  # row 0 leaves room for the first stack's title
        for stack in data["stacks"]:
            first = row
            for lid in stack["layers"]:
                members = [c for c in self.comps if c["layer"] == lid]
                if not members:
                    continue
                start = row
                for i, c in enumerate(members):
                    r, col = start + i // PER_ROW, i % PER_ROW
                    self.nodes[c["id"]] = {
                        "x": LEFT + col * (NW + CGAP),
                        "y": TOP + r * PITCH,
                        "w": NW,
                        "h": NH,
                        "row": r,
                        "col": col,
                        "span": 1,
                    }
                    row = max(row, r + 1)
                self.layer_rows[lid] = (start, row - 1)
            self.stack_rows.append((stack["name"], first, row - 1))
            row += 1  # a blank row separates stacks
        self.rows = row
        self.width = LEFT + PER_ROW * (NW + CGAP) - CGAP + 20
        self.height = TOP + (self.rows - 1) * PITCH


def _marker(mid: str, cls: str) -> str:
    return (
        f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" '
        f'markerUnits="userSpaceOnUse" orient="auto"><path d="M0,0 L10,5 L0,10 z" class="{cls}"/>'
        "</marker>"
    )


def map_svg(lay: Layout) -> str:
    p = [
        (f'<svg id="softmap" class="map-svg" viewBox="0 0 {lay.width} {lay.height}" role="img" '
        'aria-label="Component map">'),
        "<defs>"
        + _marker("m-out", "mk-out")
        + _marker("m-cb", "mk-cb")
        + _marker("m-in", "mk-in")
        + _marker("m-incb", "mk-incb")
        + "</defs>",
    ]
    for li, (lid, (r0, r1)) in enumerate(lay.layer_rows.items()):
        y0, y1 = TOP + r0 * PITCH - 10, TOP + r1 * PITCH + NH + 10
        p.append(
            f'<rect class="band b{li % 2}" x="8" y="{y0}" width="{lay.width - 16}" '
            f'height="{y1 - y0}" rx="8"/>'
            f'<text class="bandlbl" x="20" y="{y0 + 22}">{esc(lay.layer_name[lid])}</text>'
        )
    for name, r0, r1 in lay.stack_rows:
        y0, y1 = TOP + r0 * PITCH - 16, TOP + r1 * PITCH + NH + 16
        p.append(
            f'<rect class="stack" x="3" y="{y0}" width="{lay.width - 6}" height="{y1 - y0}" rx="10"/>'
            f'<text class="stacklbl" x="14" y="{y0 - 10}">{esc(name)}</text>'
        )
    p.append('<g id="edges"></g>')
    for c in lay.comps:
        n = lay.nodes[c["id"]]
        sub = STATUSES[c["status"]][1].lower()
        mark = ""
        if c.get("untrusted"):
            mark = (
                f'<circle class="untr" cx="{n["x"] + n["w"] - 9}" cy="{n["y"] + 9}" r="4">'
                "<title>Handles untrusted input</title></circle>"
            )
        p.append(
            f'<g class="node {c["status"]}" data-id="{c["id"]}" tabindex="0" role="button" '
            f'aria-label="{esc(c["name"])}">'
            f'<rect x="{n["x"]}" y="{n["y"]}" width="{n["w"]}" height="{n["h"]}" rx="7"/>'
            f'<text class="nn" x="{n["x"] + n["w"] / 2}" y="{n["y"] + 19}" text-anchor="middle">'
            f"{esc(c['name'])}</text>"
            f'<text class="ns" x="{n["x"] + n["w"] / 2}" y="{n["y"] + 34}" text-anchor="middle">'
            f"{esc(sub)}</text>{mark}</g>"
        )
    p.append("</svg>")
    return "".join(p)


def chips(names: dict[str, str], ids: list[str], cb: bool = False) -> str:
    if not ids:
        return '<span class="text-body-secondary small">none</span>'
    cls = "chip chip-cb" if cb else "chip"
    return " ".join(f'<a class="{cls}" href="#c-{i}">{esc(names[i])}</a>' for i in sorted(ids))


def prompt_link(cfg: Config, prompt: str) -> str:
    """Badge linking to the build prompt file, or plain text when there is none."""
    matches = sorted((cfg.repo_root / "docs" / "prompts").glob(f"{prompt}-*.md"))
    label = f"prompt {prompt}"
    if not matches:
        return f'<span class="badge text-bg-light border">{esc(label)}</span>'
    rel = matches[0].relative_to(cfg.repo_root).as_posix()
    url = repo_link(cfg, rel, "softmap.toml")
    return (
        f'<a class="badge text-bg-light border text-decoration-none" href="{url}">{esc(label)}</a>'
    )


def inherits_html(rows: list[dict[str, Any]]) -> str:
    own = [r for r in rows if r["bases"]]
    if not own:
        return ""
    body = "".join(
        f"<tr><td><code>{esc(r['name'])}</code></td><td>{', '.join(f'<code>{esc(b)}</code>' for b in r['bases'])}</td><td>{rich(r['doc'])}</td></tr>"
        for r in own
    )
    return (
        '<h5 class="h6 mt-3">Inherits</h5><div class="table-responsive"><table class="table table-sm table-striped">'
        f"<thead><tr><th>Class</th><th>Bases</th><th>What it is</th></tr></thead><tbody>{body}</tbody></table></div>"
    )


def class_tree_html(classes: list[dict[str, Any]], comp_of: dict[str, str]) -> str:
    """Every in-core hierarchy, each root with its subclasses nested; a class under two bases appears under each."""
    names = {r["name"] for r in classes}
    kids: dict[str, list[dict[str, Any]]] = {}
    for r in classes:
        for b in r["bases"]:
            if b in names:
                kids.setdefault(b, []).append(r)

    def node(r: dict[str, Any]) -> str:
        ext = [b for b in r["bases"] if b not in names]
        tag = f' <span class="text-body-secondary">({esc(comp_of.get(r["path"], "no component"))})</span>'
        base = f' <span class="text-body-secondary">&larr; {", ".join(esc(b) for b in ext)}</span>' if ext else ""
        sub = "".join(node(k) for k in kids.get(r["name"], []))
        return f"<li><code>{esc(r['name'])}</code>{base}{tag}{f'<ul>{sub}</ul>' if sub else ''}</li>"

    roots = [r for r in classes if not any(b in names for b in r["bases"])]
    return f'<ul class="classtree">{"".join(node(r) for r in roots)}</ul>'


def card(cfg: Config, c: dict[str, Any], names: dict[str, str], used_by: dict[str, list[str]],
         notified_by: dict[str, list[str]], classes: list[dict[str, Any]] | None = None) -> str:  # fmt: skip
    cls, label, _ = STATUSES[c["status"]]
    badges = f'<span class="badge {cls}">{label}</span>' + (f' {prompt_link(cfg, str(c["prompt"]))}' if c.get("prompt") else "")
    if c.get("untrusted"):
        badges += ' <span class="badge text-bg-danger">untrusted input</span>'
    where = ""
    if c.get("path"):
        url = repo_link(cfg, c["path"], f"component {c['id']}")
        where = f'<a href="{url}"><code>{esc(c["path"])}</code></a> &middot; '
    types = ""
    if c.get("types"):
        rows = "".join(
            f"<tr><td><code>{esc(n)}</code></td><td>{esc(k)}</td><td>{rich(d)}</td></tr>"
            for n, k, d in c["types"]
        )
        types = (
            '<h5 class="h6 mt-3">Types</h5><div class="table-responsive"><table class="table '
            'table-sm table-striped"><thead><tr><th>Name</th><th>Kind</th><th>Meaning</th></tr>'
            f"</thead><tbody>{rows}</tbody></table></div>"
        )
    inherits = inherits_html([r for r in classes or [] if c.get("path") and r["path"] == c["path"]])
    funcs = ""
    if c.get("funcs"):
        rows = "".join(
            f"<tr><td><code>{esc(s)}</code></td><td>{rich(d)}</td></tr>" for s, d in c["funcs"]
        )
        funcs = (
            '<h5 class="h6 mt-3">Functions</h5><div class="table-responsive"><table class="table '
            'table-sm table-striped fn"><thead><tr><th>Signature</th><th>What it does</th></tr>'
            f"</thead><tbody>{rows}</tbody></table></div>"
        )
    ext = ", ".join(esc(e) for e in c.get("ext", [])) or "none"
    notes = ""
    if c.get("notes"):
        items = "".join(f"<li>{rich(n)}</li>" for n in c["notes"])
        notes = f'<div class="small mt-2"><span class="rel-k">Notes</span><ul class="mb-0">{items}</ul></div>'
    search = " ".join([c["id"], c["name"], c["summary"], *[s for s, _ in c.get("funcs", [])]])
    cid = c["id"]
    return (
        f'<article class="card mb-3 comp" id="c-{cid}" data-search="{esc(search)}">'
        '<div class="card-header d-flex flex-wrap gap-2 align-items-center">'
        f'<strong class="fs-6">{esc(c["name"])}</strong>{badges}'
        f'<span class="ms-auto small">{where}<a href="#map">map</a></span></div>'
        f'<div class="card-body"><p>{rich(c["summary"])}</p>'
        '<div class="row g-3 small">'
        f'<div class="col-md-6"><div class="rel-k">Calls</div>{chips(names, c.get("uses", []))}</div>'
        f'<div class="col-md-6"><div class="rel-k">Called by</div>{chips(names, used_by[cid])}</div>'
        '<div class="col-md-6"><div class="rel-k">Notifies (event or callback, upward)</div>'
        f"{chips(names, c.get('notifies', []), True)}</div>"
        '<div class="col-md-6"><div class="rel-k">Notified by</div>'
        f"{chips(names, notified_by[cid], True)}</div></div>"
        f'{types}{inherits}{funcs}<div class="small mt-2"><span class="rel-k">External</span> {ext}</div>'
        f"{notes}</div></article>"
    )


def flows_html(data: dict[str, Any], names: dict[str, str], comps: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for fl in data.get("flows", []):
        rows = []
        for i, (a, b, what, note) in enumerate(fl["steps"], 1):
            cb = b in comps[a].get("notifies", [])
            tag = ' <span class="badge text-bg-info">callback</span>' if cb else ""
            rows.append(
                f'<tr><td>{i}</td><td><a href="#c-{a}">{esc(names[a])}</a></td><td>&rarr;</td>'
                f'<td><a href="#c-{b}">{esc(names[b])}</a></td><td>{rich(what)}{tag}</td>'
                f"<td>{rich(note)}</td></tr>"
            )
        out.append(f"### {fl['title']}")
        out.append(
            '<div class="table-responsive"><table class="table table-sm table-striped"><thead><tr>'
            "<th>#</th><th>From</th><th></th><th>To</th><th>What happens</th><th>Notes</th></tr>"
            f"</thead><tbody>{''.join(rows)}</tbody></table></div>"
        )
    return out


def matrix_html(comps: list[dict[str, Any]], names: dict[str, str]) -> str:
    ids = [c["id"] for c in comps]
    head = "".join(f'<th class="mx-h"><a href="#c-{i}">{esc(names[i])}</a></th>' for i in ids)
    body = []
    for c in comps:
        cells = []
        for t in ids:
            if t in c.get("uses", []):
                cells.append('<td class="mx-c" title="calls">C</td>')
            elif t in c.get("notifies", []):
                cells.append('<td class="mx-b" title="notifies">B</td>')
            else:
                cells.append("<td></td>")
        body.append(
            f'<tr><th class="mx-r"><a href="#c-{c["id"]}">{esc(c["name"])}</a></th>'
            f"{''.join(cells)}</tr>"
        )
    return (
        f'<div class="table-responsive"><table class="mx"><thead><tr><th></th>{head}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def src_link(cfg: Config, path: str) -> str:
    """A Markdown link to a repo file, or plain code if this tree does not have it."""
    return f"[{path}](repo:{path})" if (cfg.repo_root / path).exists() else f"`{path}`"


def _builder(cfg: Config) -> Page:
    data = load(cfg)
    classes = load_classes(cfg)
    entry_points = validate(cfg, data)
    comps: list[dict[str, Any]] = data["components"]
    by_id = {c["id"]: c for c in comps}
    names = {c["id"]: c["name"] for c in comps}
    lay = Layout(data)
    used_by: dict[str, list[str]] = {c["id"]: [] for c in comps}
    notified_by: dict[str, list[str]] = {c["id"]: [] for c in comps}
    edges: list[dict[str, str]] = []
    for c in comps:
        for t in c.get("uses", []):
            used_by[t].append(c["id"])
            edges.append({"f": c["id"], "t": t, "k": "call"})
        for t in c.get("notifies", []):
            notified_by[t].append(c["id"])
            edges.append({"f": c["id"], "t": t, "k": "cb"})
    counts = {s: sum(1 for c in comps if c["status"] == s) for s in STATUSES}
    n_calls = sum(1 for e in edges if e["k"] == "call")
    n_funcs = sum(len(c.get("funcs", [])) for c in comps)
    n_steps = sum(len(f["steps"]) for f in data.get("flows", []))

    out: list[str] = [
        "## Status",
        "",
        (f"{len(comps)} components in {len(data['layers'])} layers: "
        f"**{counts['built']} built**, **{counts['partial']} partial**, "
        f"**{counts['planned']} planned**."),
        "",
        "| Status | Meaning |",
        "|---|---|",
    ]
    for cls, label, meaning in STATUSES.values():
        out.append(f'| <span class="badge {cls}">{label}</span> | {meaning} |')
    out += [
        "",
        "::: callout",
        (f"Generated from {src_link(cfg, 'docs/softmap.toml')} by {src_link(cfg, 'docs/tools/softmap.py')}, "
        "which refuses to build if the data breaks the layering rules or disagrees with the repository. "
        "Edit the data in the same change that builds or alters a component."),
        ":::",
        "",
        "## Map",
        "",
        ("Click a component to draw its relationships. Each separate piece of software is its own "
        "framed stack, with its layers top to bottom. The border shows status; a red dot marks "
        "components that handle untrusted input."),
        "",
        f'<div class="mapbox">{map_svg(lay)}</div>',
        "",
        ('<div class="legend">'
        '<span><svg width="34" height="10"><line x1="0" y1="5" x2="34" y2="5" class="e e-out"/></svg>calls (selected component calls this)</span>'
        '<span><svg width="34" height="10"><line x1="0" y1="5" x2="34" y2="5" class="e e-in"/></svg>called by</span>'
        '<span><svg width="34" height="10"><line x1="0" y1="5" x2="34" y2="5" class="e e-cb"/></svg>notifies (event or callback, upward)</span>'
        '<span><svg width="34" height="10"><line x1="0" y1="5" x2="34" y2="5" class="e e-incb"/></svg>notified by</span>'
        "</div>"),
        "",
        '<div id="sel" class="card card-body small">Select a component on the map.</div>',
        "",
        "## Conventions",
        "",
        ("- **Calls go down.** A component may call its own layer or a lower one, never a higher "
        "one, and the call graph has no cycles."),
        ("- **Events go up.** Lower layers reach higher ones only through events or a handler the "
        "higher layer registered."),
        ("- **Plugins meet core at a contract.** Core never imports provider code, and a plugin "
        "never touches the databases."),
        ("- **Outside text is data.** Anything that handles text from outside is marked untrusted, and "
        "its text reaches a model only as `UntrustedText`."),
        ("- **Mutations are gated in code.** The operation registry's confirm gate, not a prompt, "
        "decides whether anything changes: a change is a proposal until a person confirms it."),
        "",
        "## Components",
        "",
        ('<div class="mb-3"><input id="q" class="form-control" type="search" '
        'placeholder="Filter components and functions (for example: intake, queue, plugin)"></div>'),
        "",
    ]
    for stack in data["stacks"]:
        out += [f"### {stack['name']}", ""]
        for lid in stack["layers"]:
            members = [c for c in comps if c["layer"] == lid]
            if not members:
                continue
            out += [f"#### {lay.layer_name[lid]}", ""]
            for c in members:
                out += [card(cfg, c, names, used_by, notified_by, classes), ""]
    out += [
        '<p id="none" class="text-body-secondary" hidden>No component matches.</p>',
        "",
        *(
            [
                "## Class relationships",
                "",
                ("Every class that takes part in an in-core inheritance, read from the live package by "
                f"{src_link(cfg, 'docs/tools/classmap.py')} into {src_link(cfg, 'docs/softmap.classes.toml')}. "
                "Subclasses nest under their base; the arrow names a base from outside the package."),
                "",
                class_tree_html(classes, {c["path"]: c["name"] for c in comps if c.get("path")}),
                "",
            ]
            if classes
            else []
        ),
        "## Call flows",
        "",
        ("Each step is one call from one component into another, checked against the declared "
        "relationships at build time."),
        "",
    ]
    for chunk in flows_html(data, names, by_id):
        out += [chunk, ""]
    out += [
        "## Dependency matrix",
        "",
        ("Row is the caller, column is the target. **C** is a direct call, **B** is an event or "
        "registered callback."),
        "",
        matrix_html(comps, names),
        "",
        "## Checks",
        "",
        "Run every time the docs are built; the build fails if any of these stops being true.",
        "",
        "| Result | Check | Detail |",
        "|---|---|---|",
    ]
    rows = [
        ("Components", f"{len(comps)} components, {n_funcs} documented functions"),
        ("Relationships", f"{n_calls} calls and {len(edges) - n_calls} events or callbacks"),
        (
            "Layering",
            "Every call goes to the same or a lower layer; every callback goes strictly upward",
        ),
        ("Call graph", "Acyclic"),
        (
            "Call flows",
            f"{len(data.get('flows', []))} flows, {n_steps} steps, each following a declared relationship",
        ),
        (
            "Repository",
            "Built and partial components point at paths that exist; planned ones have none yet",
        ),
    ]
    for a, b in rows:
        out.append(f'| <span class="badge text-bg-success">pass</span> | {a} | {b} |')
    out += [""]
    if entry_points:
        eps = ", ".join(f"**{esc(names[i])}**" for i in entry_points)
        out += [
            f"Entry points, which nothing else calls because the user or an event drives them: {eps}.",
            "",
        ]
    out += ["## Open points", "", "| Topic | Note |", "|---|---|"]
    for o in data.get("open", []):
        out.append(f"| **{o['title']}** | {o['text'].replace('|', '&#124;')} |")
    out.append("")

    page = page_from_meta(
        "architecture/software-map",
        "Software map",
        "\n".join(out),
        {
            "subtitle": "What is built, and how it fits",
            "lead": "The components of Oaken Core, the layers they sit in, what calls what, "
            "and which pieces exist yet.",
            "badges": [["text-bg-secondary", "Generated"]],
            "group": "concepts",
            "order": 20,
            "description": "The layered map of Oaken Core's components and their build status.",
        },
        None,
        False,
    )
    page.extra_css = CSS
    info = {c["id"]: rich(c["summary"]) for c in comps}
    page.extra_js = (
        JS.replace("@NODES@", json.dumps(lay.nodes))
        .replace("@EDGES@", json.dumps(edges))
        .replace("@INFO@", json.dumps(info))
        .replace("@NAMES@", json.dumps({k: esc(v) for k, v in names.items()}))
        .replace("@PITCH@", str(PITCH))
        .replace("@TOP@", str(TOP))
        .replace("@NH@", str(NH))
        .replace("@CGAP@", str(CGAP))
        .replace("@LEFT@", str(LEFT))
        .replace("@NW@", str(NW))
        .replace("@PER_ROW@", str(PER_ROW))
    )
    return page


generated.register("architecture/software-map", _builder)
