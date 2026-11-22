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


# Logo files (docs-src/tools/brand/) are copied to assets/brand/ on every build. The favicon and touch
# icon are cut from the logo mark; logo.png is the mark itself, with a transparent background.
BRAND_DIR = HERE / "brand"
BRAND_FILES = ("apple-touch-icon.png", "favicon.png", "logo.png")


def _pygments_css() -> str:
    light = HtmlFormatter(style=PYGMENTS_LIGHT).get_style_defs('[data-bs-theme="light"] .highlight')
    dark = HtmlFormatter(style=PYGMENTS_DARK).get_style_defs('[data-bs-theme="dark"] .highlight')
    return str(light) + "\n" + str(dark) + "\n"


_CSS = """
  :root { --accent: @AL@; --accent-soft: @SL@; }
  [data-bs-theme="dark"] { --accent: @AD@; --accent-soft: @SD@; }
  html { scroll-behavior: smooth; scroll-padding-top: 1.5rem; }
  body { font-size: 0.97rem; line-height: 1.6; }
  a { color: var(--accent); }
  :focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
  main { padding: 0 0 4rem; min-width: 0; }
  main:focus { outline: none; }
  .cover { padding: 3rem 0 1.5rem; border-bottom: 3px solid var(--accent); margin-bottom: 1rem; }
  .cover h1 { font-weight: 800; letter-spacing: -.01em; color: var(--accent); }
  .cover .lead { max-width: 46rem; }
  .masthead { display: flex; align-items: center; gap: 1.25rem; margin-bottom: .75rem; }
  .masthead h1 { margin: 0; }
  .cover .logo { width: 7rem; height: 7rem; flex: none; }
  .mark { display: inline-block; flex: none; vertical-align: -.45em; margin-right: .2rem; background-color: var(--accent);
          -webkit-mask: url(brand/logo.png) center / contain no-repeat; mask: url(brand/logo.png) center / contain no-repeat; }  /* the logo's shape, in the theme's accent: right in light and dark */
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
  .cover { margin-top: 1rem; }
  .home h2 { font-size: 1.35rem; font-weight: 700; margin-top: 2.2rem; }
  .doc { display: block; height: 100%; padding: 1rem 1.1rem; border: 1px solid var(--bs-border-color); border-left: 4px solid var(--accent);
    border-radius: .4rem; text-decoration: none; color: var(--bs-body-color); background: var(--bs-body-bg); }
  .doc:hover { background: var(--accent-soft); }
  .doc .t { font-weight: 700; color: var(--accent); }
  .doc .s { font-size: .8rem; color: var(--bs-secondary-color); text-transform: uppercase; letter-spacing: .05em; }
  .doc p { margin: .3rem 0 0; font-size: .92rem; }
  .skip:focus { position: fixed; top: .5rem; left: .5rem; z-index: 2000; padding: .5rem 1rem; background: var(--bs-body-bg); border: 2px solid var(--accent); }

  .site-nav { position: sticky; top: 0; z-index: 10; background: color-mix(in srgb, var(--bs-body-bg) 88%, transparent);
    backdrop-filter: blur(8px); border-bottom: 1px solid var(--bs-border-color); }
  .site-nav .brand { font-weight: 700; letter-spacing: .02em; color: var(--accent); text-decoration: none; }
  .site-nav .nav-link { font-weight: 500; color: var(--bs-body-color); }
  .site-nav .nav-link:hover, .site-nav .nav-link.show, .site-nav .nav-link.active { color: var(--accent); }
  .site-nav .dropdown-menu { min-width: 15rem; padding: .35rem; border-radius: .6rem; }
  .site-nav a.doc-link { display: block; padding: .35rem .7rem; border-radius: .4rem; font-size: .92rem; color: var(--bs-body-color); text-decoration: none; }
  .site-nav a.doc-link:hover { background: var(--bs-secondary-bg); }
  .site-nav a.doc-link.current { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
  .docs-grid { display: grid; grid-template-columns: minmax(0, 1fr); }
  @media (min-width: 992px) { .docs-grid.with-toc { grid-template-columns: 14rem minmax(0, 66rem); column-gap: 3.5rem; } }
  .docs-toc .sticky { position: sticky; top: 3.5rem; max-height: calc(100vh - 3.5rem); overflow-y: auto; padding: 3rem 0 2rem; }
  .docs-toc .label { margin-bottom: .75rem; font-size: .7rem; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--bs-secondary-color); }
  .docs-toc a.nav-link { display: block; padding: .2rem 0; font-size: .9rem; color: var(--bs-secondary-color); text-decoration: none; }
  .docs-toc a.nav-link:hover, .docs-toc a.nav-link.active { color: var(--accent); }
  .docs-toc a.nav-link.active { font-weight: 600; }
  .docs-toc .num { display: inline-block; width: 1.6rem; font-variant-numeric: tabular-nums; opacity: .7; }
  .crumbs { padding-top: 1.5rem; font-size: .85rem; color: var(--bs-secondary-color); }
  .crumbs a { color: var(--bs-secondary-color); }
  .prevnext { margin-top: 3rem; padding-top: 1.5rem; border-top: 1px solid var(--bs-border-color); }
  .hero { text-align: center; padding: 3rem 0 2rem; border-bottom: 0; }
  .hero .masthead { justify-content: center; flex-direction: column; gap: .5rem; }
  .hero .lead { max-width: 38rem; margin: 0 auto 1.5rem; color: var(--bs-secondary-color); }
  .hero .btn { padding: .6rem 1.2rem; font-weight: 600; border-radius: .6rem; }
  .btn-brand { --bs-btn-bg: var(--accent); --bs-btn-border-color: var(--accent); --bs-btn-color: #fff;
    --bs-btn-hover-bg: var(--accent); --bs-btn-hover-border-color: var(--accent); --bs-btn-hover-color: #fff; }
  [data-bs-theme="dark"] .btn-brand { --bs-btn-color: #0d1117; --bs-btn-hover-color: #0d1117; }
  @media print {
    .site-nav, .docs-toc, .prevnext, .copy-btn, .skip { display: none !important; }
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
