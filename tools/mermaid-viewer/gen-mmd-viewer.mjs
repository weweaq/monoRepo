#!/usr/bin/env node
// =============================================================================
// gen-mmd-viewer.mjs
// Turn a Mermaid .mmd file into a self-contained, offline, interactive HTML
// (pan + zoom) viewer. Uses local vendor libs (mermaid.min.js + svg-pan-zoom).
//
// Usage:
//   node gen-mmd-viewer.mjs <input.mmd> [output.html]
//
// Default output: same directory as input, basename + ".viewer.html".
// Keep this script pure ASCII (repo rule R10). The .mmd content (may contain
// CJK) is embedded as an escaped text block inside the generated HTML.
// =============================================================================

import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, basename, extname, join, resolve, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));

// ---- arg parsing -----------------------------------------------------------
const args = process.argv.slice(2);
if (args.length < 1) {
  console.error("[ERROR] missing input .mmd path");
  console.error("usage: node gen-mmd-viewer.mjs <input.mmd> [output.html]");
  process.exit(1);
}
const inputPath = resolve(args[0]);
const outputPath = args[1] ? resolve(args[1]) : join(dirname(inputPath), basename(inputPath, extname(inputPath)) + ".viewer.html");

if (!inputPath.toLowerCase().endsWith(".mmd")) {
  console.error("[ERROR] input must be a .mmd file: " + inputPath);
  process.exit(1);
}

// ---- read mermaid source ---------------------------------------------------
let mmdSource;
try {
  mmdSource = readFileSync(inputPath, "utf8");
} catch (e) {
  console.error("[ERROR] cannot read input: " + e.message);
  process.exit(1);
}

// ---- vendor relative path (from output dir -> vendor dir) -------------------
const vendorDir = join(HERE, "vendor");
const relVendor = relative(dirname(outputPath), vendorDir).split(sep).join("/");
const mermaidSrc = relVendor + "/mermaid.min.js";
const panzoomSrc = relVendor + "/svg-pan-zoom.min.js";

// Embed the .mmd source as a JS string literal via JSON.stringify.
// This keeps every byte intact (incl. <br/>, [IMAGE:] etc.) and avoids the
// browser re-interpreting it as HTML, which corrupted the old <pre> approach.
const injectedMmd = JSON.stringify(mmdSource);

// ---- HTML template ----------------------------------------------------------
const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Architecture Flow Viewer</title>
<style>
  :root {
    --paper: #fbfbfa;
    --ink: #1f2733;
    --muted: #8a919c;
    --accent: #2f6fed;
    --border: #e3e7ee;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: var(--paper); color: var(--ink); font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
  #toolbar {
    position: fixed; top: 0; left: 0; right: 0; z-index: 10;
    display: flex; align-items: center; gap: 8px;
    padding: 8px 14px; background: rgba(255,255,255,.92); backdrop-filter: blur(4px);
    border-bottom: 1px solid var(--border);
  }
  #toolbar .btn {
    border: 1px solid var(--border); background: #fff; color: var(--ink);
    border-radius: 6px; padding: 5px 12px; cursor: pointer; font-size: 13px; line-height: 1.2;
    user-select: none;
  }
  #toolbar .btn:hover { border-color: var(--accent); color: var(--accent); }
  #toolbar .btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  #toolbar .spacer { flex: 1; }
  #toolbar .hint { color: var(--muted); font-size: 12px; white-space: nowrap; }
  #stage {
    position: fixed; inset: 0; top: 45px; overflow: hidden; padding: 0;
  }
  #mmd-canvas {
    position: absolute; inset: 0; overflow: hidden;
  }
  #mmd-host {
    position: absolute; inset: 0;
    display: block;
  }
  #mmd-host svg {
    display: block;
    width: 100%; height: 100%;
    max-width: none; max-height: none;
    background: var(--paper);
  }
  #node-tooltip {
    position: fixed; z-index: 20; pointer-events: none; display: none;
    max-width: 320px; padding: 8px 10px; border-radius: 6px;
    background: rgba(31,39,51,.94); color: #fff; font-size: 12px; line-height: 1.5;
    box-shadow: 0 4px 14px rgba(0,0,0,.18); white-space: pre-wrap;
  }
  #status {
    position: fixed; right: 12px; bottom: 10px; z-index: 10;
    font-size: 11px; color: var(--muted);
    background: rgba(255,255,255,.85); padding: 3px 8px; border-radius: 999px;
    border: 1px solid var(--border);
  }
</style>
</head>
<body>
  <div id="toolbar">
    <button class="btn primary" id="btn-fit">Fit</button>
    <button class="btn" id="btn-in">+ Zoom in</button>
    <button class="btn" id="btn-out">- Zoom out</button>
    <button class="btn" id="btn-reset">Reset</button>
    <span class="spacer"></span>
    <span class="hint">Drag to pan &middot; scroll to zoom</span>
  </div>
  <div id="stage">
    <div id="mmd-canvas">
      <div id="mmd-host" class="mermaid"></div>
    </div>
  </div>
  <div id="node-tooltip"></div>
  <div id="status"></div>

  <script src="${mermaidSrc}"></script>
  <script src="${panzoomSrc}"></script>
  <script>
    (function () {
      var raw = ${injectedMmd};
      var host = document.getElementById("mmd-host");
      var tip = document.getElementById("node-tooltip");
      var status = document.getElementById("status");
      var panZoom = null;

      mermaid.initialize({
        startOnLoad: false,
        maxTextSize: 500000,
        maxEdges: 200000,
        securityLevel: "loose",
        flowchart: { curve: "basis", htmlLabels: true }
      });

      function render() {
        try {
          mermaid.render("archFlow", raw).then(function (res) {
            host.innerHTML = res.svg;
            bindPanZoom();
          });
        } catch (e) {
          console.error("mermaid render failed:", e);
          status.textContent = "mermaid render failed: " + e.message;
        }
      }

      function bindPanZoom() {
        var svg = host.querySelector("svg");
        if (!svg) return;
        if (panZoom) panZoom.destroy();
        panZoom = svgPanZoom(svg, {
          zoomEnabled: true,
          panEnabled: true,
          controlIconsEnabled: false,
          fit: true,
          center: true,
          minZoom: 0.1,
          maxZoom: 10,
          beforePan: function () { return true; }
        });
        updateStatus();
        attachTips(svg);
      }

      // Hover -> show node label/desc tooltip (class -> friendly name)
      function attachTips(svg) {
        var nodes = svg.querySelectorAll(".node");
        Array.prototype.forEach.call(nodes, function (n) {
          n.addEventListener("mousemove", function (ev) {
            var id = n.id;
            var g = host.querySelector('g[aria-labelledby="' + id.replace(/graph-/g, "") + '"]');
            var lbl = (g && g.getAttribute("aria-label")) || id;
            tip.textContent = lbl;
            showTip(ev);
          });
          n.addEventListener("mouseleave", function () { tip.style.display = "none"; });
        });
      }

      function showTip(ev) {
        var off = 14;
        var x = ev.clientX + off, y = ev.clientY + off;
        tip.style.left = x + "px";
        tip.style.top = y + "px";
        tip.style.display = "block";
      }

      function updateStatus() {
        if (!panZoom) return;
        var z = Math.round(panZoom.getZoom() * 100);
        status.textContent = "zoom " + z + "%";
      }

      document.getElementById("btn-fit").addEventListener("click", function () { if (panZoom) panZoom.fit(); panZoom.center(); });
      document.getElementById("btn-in").addEventListener("click", function () { if (panZoom) panZoom.zoomIn(); });
      document.getElementById("btn-out").addEventListener("click", function () { if (panZoom) panZoom.zoomOut(); });
      document.getElementById("btn-reset").addEventListener("click", function () { if (panZoom) { panZoom.resetZoom(); panZoom.resetPan(); } });

      render();
    })();
  </script>
</body>
</html>
`;

// ---- write output ------------------------------------------------------------
try {
  mkdirSync(dirname(outputPath), { recursive: true });
  writeFileSync(outputPath, html, "utf8");
} catch (e) {
  console.error("[ERROR] cannot write output: " + e.message);
  process.exit(1);
}
console.log("[OK] wrote: " + outputPath);
console.log("[OK] vendor referenced: " + mermaidSrc + " / " + panzoomSrc);