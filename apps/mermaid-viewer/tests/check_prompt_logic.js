// Extracted-logic check: run the viewer's parseSymbols + prompt-building path
// against the real architecture-flow.mmd and the 6 legacy-format reviews the
// user actually exported, to verify locs become real symbol + line numbers.
// Run: node apps/mermaid-viewer/tests/check_prompt_logic.js
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "../../withlanggraph/docs/architecture-flow.mmd"), "utf8");

// --- verbatim port of the viewer logic under test ---
let currentNodeMap = {}, currentEdgeList = [], currentSymLine = {}, currentLines = [], currentEdgeLines = {};

function normKey(s) {
  return String(s).replace(/<br\s*\/?>/gi, "").replace(/["']/g, "").replace(/\s+/g, "");
}

function parseSymbols(src) {
  currentNodeMap = {};
  currentEdgeList = [];
  currentSymLine = {};
  currentLines = src.split("\n");
  currentEdgeLines = {};
  src.split("\n").forEach(function (l, idx) {
    var ln = idx + 1;
    if (/^\s*%%/.test(l)) return;
    var sg = l.match(/\bsubgraph\s+([A-Za-z_][A-Za-z0-9_]*)/);
    if (sg && currentSymLine[sg[1]] === undefined) currentSymLine[sg[1]] = ln;
    var ndRe = /(?:^|[^A-Za-z0-9_\-])([A-Za-z_][A-Za-z0-9_]*)\s*(\[\[|\{\{|\(\(|\[|\{|\()/g;
    var mm;
    while ((mm = ndRe.exec(l)) !== null) {
      if (currentSymLine[mm[1]] === undefined) currentSymLine[mm[1]] = ln;
    }
    if (/(->|---|-\.|==|<-)/.test(l)) {
      var plRe = /\|([^|]+)\|/g, pm;
      while ((pm = plRe.exec(l)) !== null) {
        var k = normKey(pm[1]);
        if (k && currentEdgeLines[k] === undefined) currentEdgeLines[k] = { line: ln, text: l.trim() };
      }
      var dtRe = /-\.\s*([^.]+?)\s*\.-/g, dm;
      while ((dm = dtRe.exec(l)) !== null) {
        var k2 = normKey(dm[1]);
        if (k2 && currentEdgeLines[k2] === undefined) currentEdgeLines[k2] = { line: ln, text: l.trim() };
      }
    }
  });
}

function symNodeId(sym) {
  if (!sym) return null;
  var m = sym.match(/^节点\s+([A-Za-z0-9_\-.]+)/);
  if (!m) return null;
  var f = m[1].match(/^flowchart-([A-Za-z_][A-Za-z0-9_]*)-\d+$/);
  return f ? f[1] : m[1];
}

function oneLine(s) { return s.replace(/\s+/g, " ").trim(); }

function normSymForExport(sym) {
  if (!sym) return sym;
  var id = symNodeId(sym);
  if (/^节点/.test(sym) && id && currentSymLine[id] !== undefined) {
    return "节点 " + id + "（行 " + currentSymLine[id] + "）";
  }
  var c = sym.match(/^容器\s+([A-Za-z0-9_\-.]+)/);
  if (c && currentSymLine[c[1]] !== undefined) {
    return "容器 " + c[1] + "（行 " + currentSymLine[c[1]] + "）";
  }
  if (/^连线/.test(sym) && /行\s*\d+/.test(sym)) return sym;
  var eo = sym.match(/^连线\s+(.+?)\s*（edge label）\s*$/);
  if (eo) {
    var hit = currentEdgeLines[normKey(eo[1])];
    if (hit) return "连线 " + hit.text + "（行 " + hit.line + "）";
  }
  return sym;
}

function srcLineFor(sym) {
  if (!sym) return "";
  if (/^连线/.test(sym)) return "";
  var m = String(sym).match(/行\s*(\d+)/);
  if (m) {
    var ln = parseInt(m[1], 10);
    if (currentLines[ln - 1] !== undefined) return currentLines[ln - 1].trim();
  }
  var id = symNodeId(sym);
  if (id && currentSymLine[id] !== undefined) return currentLines[currentSymLine[id] - 1].trim();
  return "";
}
// --- end port ---

parseSymbols(src);

// the 6 legacy reviews exactly as the user's broken export showed them
const reviews = [
  { type: "review", sym: "节点 flowchart-RC-2（proactive / cli / rerun）", text: "需要解释一下这个 proactive" },
  { type: "review", sym: "节点 flowchart-SC-0（scheduler.run_job）", text: "怎么配置？触发？" },
  { type: "review", sym: "节点 flowchart-FB-42（反馈闭环 feedback.pyedit/confirm/redeliver）", text: "没看懂怎么触发的" },
  { type: "review", sym: "节点 flowchart-MMX-16（memory_maintain对话尾部触发长记忆）", text: "怎么触发的？" },
  { type: "review", sym: "容器 MAIN（② 主图状态机 graph.build_graph() (核心)）", text: "这里面有好几个模块，比较核心，可以划分好，再各个解释。" },
  { type: "review", sym: "连线 edge label（edge label）", text: "没看懂什么叫做恒process" },
];

let fails = 0;
function expect(cond, msg) {
  if (cond) { console.log("PASS  " + msg); } else { fails++; console.log("FAIL  " + msg); }
}

// symbol -> line sanity against the known file
// (line numbers track apps/withlanggraph/docs/architecture-flow.mmd; update when
// the diagram legitimately changes shape — last sync 2026-10-04, v3 反馈闭环改版)
expect(currentSymLine["RC"] === 11, "RC -> 行 11 (real symbol recovered)");
expect(currentSymLine["SC"] === 9, "SC -> 行 9");
expect(currentSymLine["MMX"] === 30, "MMX -> 行 30");
expect(currentSymLine["FB"] === 87, "FB -> 行 87");
expect(currentSymLine["MAIN"] === 14, "subgraph MAIN -> 行 14");
expect(currentSymLine["FLOW"] === 17, "subgraph FLOW -> 行 17");

// edge label lookup by normalized text (what a fresh edge click produces)
const wtLabel = "route_after_wait: 恒进 process(返回值固定为 'process', 无分支)";
const hit = currentEdgeLines[normKey(wtLabel)];
expect(!!hit && hit.line === 21, "WT-->PR edge label resolves to 行 21");
const cuLabel = "日报正文";
const hit2 = currentEdgeLines[normKey(cuLabel)];
expect(!!hit2 && hit2.line === 111, "dotted label 日报正文 resolves to 行 111");

// the 6 legacy reviews: what the fixed export produces
console.log("\n--- exported prompt (per-review part) ---");
reviews.forEach(function (r, i) {
  const loc = normSymForExport(r.sym);
  const srcline = srcLineFor(loc);
  console.log((i + 1) + ". loc: " + oneLine(loc));
  if (srcline) console.log("   源码行: " + srcline.slice(0, 90) + (srcline.length > 90 ? "…" : ""));
  console.log("   类型: " + r.type);
  console.log("   意见: " + r.text);
});
console.log("\n--- checks ---");
expect(normSymForExport(reviews[0].sym) === "节点 RC（行 11）", "review1 loc -> 节点 RC（行 11）");
expect(normSymForExport(reviews[4].sym) === "容器 MAIN（行 14）", "review5 loc -> 容器 MAIN（行 14）");
expect(srcLineFor("节点 RC（行 11）").startsWith('RC["proactive=定时主动推送'), "review1 源码行 = RC def line");
expect(srcLineFor("容器 MAIN（行 14）").startsWith("subgraph MAIN["), "review5 源码行 = subgraph MAIN line");
// legacy junk anchor degrades gracefully (not a crash, no garbage line no)
expect(typeof normSymForExport(reviews[5].sym) === "string", "review6 legacy edge anchor degrades gracefully");

console.log(fails === 0 ? "\nALL PASS" : "\n" + fails + " FAILURES");
process.exit(fails === 0 ? 0 : 1);
