# SPDX-License-Identifier: Apache-2.0
"""The look shared by every page: palette, CSS, scripts, pinned CDN assets and the logo files.

Re-theming the site is a two-line change: edit ACCENT_LIGHT/ACCENT_SOFT_LIGHT and
ACCENT_DARK/ACCENT_SOFT_DARK below. Nothing else defines a colour that is not derived from
Bootstrap's own variables.
"""

import tomllib
from pathlib import Path
from typing import Any

from pygments.formatters import HtmlFormatter

HERE = Path(__file__).resolve().parent

ACCENT_LIGHT = "#1f4e79"
ACCENT_SOFT_LIGHT = "#e8f0f8"
ACCENT_DARK = "#7fb2e5"
ACCENT_SOFT_DARK = "#1b2733"

THEME_KEY = "oaken-theme"
PYGMENTS_LIGHT = "default"
PYGMENTS_DARK = "github-dark"


def sri() -> dict[str, dict[str, str]]:
    """The pinned CDN URLs and integrity hashes recorded by compute_sri.py."""
    with (HERE / "sri.toml").open("rb") as fh:
        data: dict[str, dict[str, str]] = tomllib.load(fh)
    return data


def cdn_tags(with_mermaid: bool) -> dict[str, Any]:
    """Attribute values for the CDN tags a page loads (the only external requests it makes)."""
    s = sri()
    out: dict[str, Any] = {"css": s["bootstrap_css"], "js": s["bootstrap_js"]}
    out["mermaid"] = s["mermaid_js"] if with_mermaid else None
    return out


# Logo files (docs/tools/brand/) are copied to assets/brand/ on every build. The favicon and touch
# icon are cut from the logo mark; logo.png is the mark itself, with a transparent background.
BRAND_DIR = HERE / "brand"
BRAND_FILES = ("apple-touch-icon.png", "favicon.png", "logo.png")


def _pygments_css() -> str:
    light = HtmlFormatter(style=PYGMENTS_LIGHT).get_style_defs('[data-bs-theme="light"] .highlight')
    dark = HtmlFormatter(style=PYGMENTS_DARK).get_style_defs('[data-bs-theme="dark"] .highlight')
    return str(light) + "\n" + str(dark) + "\n"


_CSS = """
  :root { --accent: @AL@; --accent-soft: @SL@; --sidebar-w: 280px; }
  [data-bs-theme="dark"] { --accent: @AD@; --accent-soft: @SD@; }
  html { scroll-behavior: smooth; scroll-padding-top: 1.5rem; }
  body { font-size: 0.97rem; line-height: 1.6; }
  a { color: var(--accent); }
  :focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
  main { padding: 0 2.5rem 4rem; max-width: 1100px; }
  main:focus { outline: none; }
  @media (max-width: 991px) { main { padding: 0 1rem 3rem; } }
  .cover { padding: 3rem 0 1.5rem; border-bottom: 3px solid var(--accent); margin-bottom: 1rem; }
  .cover h1 { font-weight: 800; letter-spacing: -.01em; color: var(--accent); }
  .cover .lead { max-width: 46rem; }
  .masthead { display: flex; align-items: center; gap: 1.25rem; margin-bottom: .75rem; }
  .masthead h1 { margin: 0; }
  .cover .logo { width: 7rem; height: auto; flex: none; }
  .brand img { vertical-align: -.4em; margin-right: .15rem; }
  .doc-section { padding-top: 1.5rem; margin-top: 1.5rem; border-top: 1px solid var(--bs-border-color); }
  .section-head { display: flex; align-items: baseline; gap: .8rem; margin-bottom: .8rem; }
  .section-num { font-size: 1.1rem; font-weight: 700; color: #fff; background: var(--accent); border-radius: .4rem; padding: .05rem .6rem; }
  [data-bs-theme="dark"] .section-num { color: #0d1117; }
  .section-head h2 { margin: 0; font-size: 1.6rem; font-weight: 700; }
  h3 { font-size: 1.25rem; margin-top: 1.5rem; }
  h4 { font-size: 1.05rem; margin-top: 1.2rem; }
  table { font-size: .88rem; }
  thead th { background: var(--accent-soft); color: var(--accent); white-space: nowrap; }
  code { overflow-wrap: anywhere; }
  .callout { border-left: 4px solid var(--accent); background: var(--accent-soft); padding: .8rem 1rem; margin: 1rem 0; border-radius: 0 .4rem .4rem 0; }
  .callout > :last-child { margin-bottom: 0; }
  .code { position: relative; margin: 1rem 0; }
  .code pre { margin: 0; padding: .9rem 1rem; border: 1px solid var(--bs-border-color); border-radius: .4rem; background: var(--bs-tertiary-bg); overflow-x: auto; }
  .code pre code { overflow-wrap: normal; }
  .copy-btn { position: absolute; top: .4rem; right: .4rem; font-size: .75rem; padding: .1rem .5rem; }
  html:not(.js) pre.mermaid { display: none; }
  pre.mermaid { background: transparent; border: 0; text-align: center; }
  footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--bs-border-color); color: var(--bs-secondary-color); font-size: .85rem; }
  .home main { margin: 0 auto; max-width: 1000px; padding: 0 1.25rem 4rem; }
  .home h2 { font-size: 1.35rem; font-weight: 700; margin-top: 2.2rem; }
  .doc { display: block; height: 100%; padding: 1rem 1.1rem; border: 1px solid var(--bs-border-color); border-left: 4px solid var(--accent);
    border-radius: .4rem; text-decoration: none; color: var(--bs-body-color); background: var(--bs-body-bg); }
  .doc:hover { background: var(--accent-soft); }
  .doc .t { font-weight: 700; color: var(--accent); }
  .doc .s { font-size: .8rem; color: var(--bs-secondary-color); text-transform: uppercase; letter-spacing: .05em; }
  .doc p { margin: .3rem 0 0; font-size: .92rem; }
  .skip:focus { position: fixed; top: .5rem; left: .5rem; z-index: 2000; padding: .5rem 1rem; background: var(--bs-body-bg); border: 2px solid var(--accent); }

  .topbar { position: sticky; top: 0; z-index: 4; display: flex; align-items: center; gap: .75rem; padding: .5rem 1rem;
    background: var(--bs-tertiary-bg); border-bottom: 1px solid var(--bs-border-color); }
  .topbar .brand { font-weight: 700; letter-spacing: .02em; color: var(--accent); }
  .sidebar { --nav-fg: var(--bs-body-color); --nav-muted: var(--bs-secondary-color); --nav-border: var(--bs-border-color);
    --nav-hover: var(--bs-secondary-bg); font-size: 16px; line-height: 1.5; }
  .sidebar .offcanvas-body { padding: 0; display: block; }
  .sidebar-inner { padding: 1.5rem 1rem; }
  .sidebar .top { display: flex; justify-content: space-between; align-items: center; margin-bottom: .75rem; }
  .sidebar .brand { font-weight: 700; font-size: 1.25rem; letter-spacing: .02em; color: var(--accent); text-decoration: none; }
  .sidebar .theme { font: inherit; font-size: .85rem; padding: .15rem .7rem; color: var(--nav-muted); background: transparent;
    border: 1px solid var(--nav-muted); border-radius: .35rem; cursor: pointer; }
  .sidebar .theme:hover { background: var(--nav-hover); }
  .sidebar .what { color: var(--nav-muted); font-size: .85rem; margin-bottom: 1rem; }
  .sidebar a { text-decoration: none; }
  .docnav { margin-bottom: 1rem; padding-bottom: .8rem; border-bottom: 1px solid var(--nav-border); }
  .docnav .home-link { display: block; padding: .4rem .6rem; border-radius: .35rem; font-weight: 700; font-size: 1.05rem; color: var(--accent); background: var(--accent-soft); }
  .sidebar .label { margin: .8rem .5rem .25rem; font-size: .7rem; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--nav-muted); }
  .docnav a.doc-link { display: block; padding: .2rem .6rem; font-size: .92rem; color: var(--nav-fg); border-radius: .3rem; }
  .docnav a.doc-link:hover { background: var(--nav-hover); }
  .docnav a.doc-link.current { font-weight: 700; color: var(--accent); }
  .sidebar .sections .label { margin-top: 0; }
  .sidebar .nav-link { display: block; padding: .3rem .5rem; border-radius: .35rem; font-size: .9rem; color: var(--nav-fg); }
  .sidebar .nav-link:hover { background: var(--nav-hover); }
  .sidebar .nav-link .num { display: inline-block; width: 2.3rem; color: var(--nav-muted); font-variant-numeric: tabular-nums; }
  .sidebar .nav-link.active { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
  .gh { margin: 0 0 1rem; padding-bottom: .8rem; border-bottom: 1px solid var(--nav-border); }
  .gh a { display: block; padding: .2rem .6rem; font-size: .9rem; color: var(--nav-fg); border-radius: .3rem; }
  .gh a:hover { background: var(--nav-hover); }
  .gh a.star { margin-top: .3rem; font-weight: 600; color: var(--accent); border: 1px solid var(--accent); text-align: center; }
  @media (min-width: 992px) {
    .sidebar.offcanvas-lg { position: fixed !important; top: 0; bottom: 0; left: 0; width: var(--sidebar-w) !important; height: auto !important;
      overflow-y: auto; border-right: 1px solid var(--nav-border) !important; background: var(--bs-tertiary-bg) !important; z-index: 5; }
    body.has-pane main { margin-left: var(--sidebar-w); }
  }
  @media print {
    .sidebar, .topbar, .copy-btn, .skip { display: none !important; }
    body.has-pane main { margin-left: 0; }
    main { max-width: none; padding: 0; }
    table, figure, .code { break-inside: avoid; }
  }
"""

CSS = (
    _CSS.replace("@AL@", ACCENT_LIGHT)
    .replace("@SL@", ACCENT_SOFT_LIGHT)
    .replace("@AD@", ACCENT_DARK)
    .replace("@SD@", ACCENT_SOFT_DARK)
    + _pygments_css()
)

# Runs in <head> before first paint: no flash of the wrong theme, and it must survive blocked
# storage (localStorage can throw), so every access is wrapped.
HEAD_JS = (
    "(function(){var r=document.documentElement;r.classList.add('js');var t=null;"
    "try{t=localStorage.getItem('" + THEME_KEY + "');}catch(e){}"
    "if(t!=='light'&&t!=='dark'){t=window.matchMedia&&window.matchMedia("
    "'(prefers-color-scheme: dark)').matches?'dark':'light';}"
    "r.setAttribute('data-bs-theme',t);})();"
)

SITE_JS = """\
(function () {
  var root = document.documentElement;
  function store(v) { try { localStorage.setItem("@KEY@", v); } catch (e) {} }

  // Mermaid would otherwise render on load with its own theme; keep the source and take over.
  function mermaidTheme() { return root.getAttribute("data-bs-theme") === "dark" ? "dark" : "default"; }
  if (window.mermaid) {
    window.mermaid.initialize({ startOnLoad: false, theme: mermaidTheme(), securityLevel: "strict" });
    document.querySelectorAll("pre.mermaid").forEach(function (el) {
      el.setAttribute("data-src", el.textContent);
    });
  }

  function renderMermaid() {
    var blocks = document.querySelectorAll("pre.mermaid");
    if (!blocks.length || !window.mermaid) { return; }
    window.mermaid.initialize({ startOnLoad: false, theme: mermaidTheme(), securityLevel: "strict" });
    blocks.forEach(function (el) {
      el.removeAttribute("data-processed");
      el.textContent = el.getAttribute("data-src");
    });
    window.mermaid.run({ nodes: blocks });
  }

  var btn = document.getElementById("themeToggle");
  if (btn) {
    btn.addEventListener("click", function () {
      var next = root.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-bs-theme", next);
      store(next);
      renderMermaid();
    });
  }

  document.querySelectorAll(".code").forEach(function (box) {
    var pre = box.querySelector("pre");
    if (!pre || !navigator.clipboard) { return; }
    var b = document.createElement("button");
    b.type = "button";
    b.className = "copy-btn btn btn-sm btn-outline-secondary";
    b.textContent = "Copy";
    b.setAttribute("aria-label", "Copy code to clipboard");
    b.addEventListener("click", function () {
      navigator.clipboard.writeText(pre.innerText).then(function () {
        b.textContent = "Copied";
        setTimeout(function () { b.textContent = "Copy"; }, 1500);
      });
    });
    box.appendChild(b);
  });

  window.addEventListener("load", renderMermaid);
})();
""".replace("@KEY@", THEME_KEY)
