(function () {
  var root = document.documentElement;
  function store(v) { try { localStorage.setItem("oaken-theme", v); } catch (e) {} }

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
