"use strict";

// ============================================================================
// constants & tiny helpers
// ============================================================================

const SVG_NS = "http://www.w3.org/2000/svg";
const ROW_H = 20;
const PAD_TOP = 32;
const PAD_BOTTOM = 12;
const PAD_SIDE = 14;
const PIN_R = 5;
const STUB = 10;
const MIN_W = 170;
const INOUT_GAP = 92;
const INOUT_LABEL_H = 26;
const CHILD_GAP = 18;
const CHILD_TOP_MARGIN = 16;
const SIDE_COL_W = 26;
const TITLE_FONT = "700 12.5px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
const LABEL_FONT = "10px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";

const EMPTY_HIERARCHY = { children: [], wires: [] };

let idCounter = 1;
function newId(prefix) {
  return `${prefix}${idCounter++}_${Math.random().toString(36).slice(2, 6)}`;
}

const measureCanvas = document.createElement("canvas");
const mctx = measureCanvas.getContext("2d");
function textWidth(text, font) {
  mctx.font = font;
  return mctx.measureText(text).width;
}

function svgEl(tag, attrs, text) {
  const el = document.createElementNS(SVG_NS, tag);
  if (attrs) {
    for (const k in attrs) el.setAttribute(k, attrs[k]);
  }
  if (text !== undefined) el.textContent = text;
  return el;
}

function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v));
}

function portLabel(p) {
  return p.width > 1 ? `${p.name}:${p.width}` : p.name;
}

// ============================================================================
// state
// ============================================================================

// A placed block:
// {
//   id, kind:'module'|'custom', name, label?, moduleVersion?, x, y, expanded,
//   ports:[{id,name,dir,width}],
//   hierarchy:{children:[{instName,moduleType,ports,children,wires,expanded}], wires:[...]}
// }
const state = {
  blocks: [],
  wires: [], // {id, from:{blockId,portId}, to:{blockId,portId}, driverEnd:'a'|'b', kind:'output'|'inout'}
};

let registryLatest = []; // from /api/modules
let registryAll = {}; // from /api/modules/all
let selected = null; // {type:'block'|'wire', id}
let dragState = null;
let dirty = false;
let clipboard = null;

const view = { x: 60, y: 60, scale: 1 };

// ============================================================================
// DOM refs
// ============================================================================

const scroller = document.getElementById("scroller");
const svg = document.getElementById("canvas");
const viewport = document.getElementById("viewport");
const wiresLayer = document.getElementById("wires-layer");
const blocksLayer = document.getElementById("blocks-layer");
const previewLayer = document.getElementById("preview-layer");
const gridLayer = document.getElementById("grid-layer");

const moduleListEl = document.getElementById("module-list");
const moduleEmptyEl = document.getElementById("module-empty");
const moduleCountEl = document.getElementById("module-count");
const searchEl = document.getElementById("search");
const connDot = document.getElementById("conn-dot");
const connStatus = document.getElementById("conn-status");
const saveStatusEl = document.getElementById("save-status");
const zoomLevelEl = document.getElementById("zoom-level");
const toastEl = document.getElementById("toast");

// ============================================================================
// connection validation (top-level, user-drawn wires only)
// ============================================================================

function portHasWire(blockId, portId) {
  return state.wires.some(
    (w) =>
      (w.from.blockId === blockId && w.from.portId === portId) ||
      (w.to.blockId === blockId && w.to.portId === portId)
  );
}

/** a and b are {blockId, portId, port:{dir,name,width}} */
function evaluateConnection(a, b) {
  if (a.blockId === b.blockId && a.portId === b.portId) {
    return { ok: false, reason: "Can't connect a pin to itself." };
  }
  const da = a.port.dir;
  const db = b.port.dir;

  if (da === db && da !== "inout") {
    const reason =
      da === "output"
        ? `Two outputs (${a.port.name}, ${b.port.name}) would short together.`
        : `${a.port.name} and ${b.port.name} are both inputs — nothing would drive this wire.`;
    return { ok: false, reason };
  }

  for (const end of [a, b]) {
    if (end.port.dir === "input" && portHasWire(end.blockId, end.portId)) {
      return { ok: false, reason: `${end.port.name} already has a driver.` };
    }
  }

  const bidirectional = da === "inout" && db === "inout";
  let driverEnd;
  if (da === "output" || db === "input") driverEnd = "a";
  else if (db === "output" || da === "input") driverEnd = "b";
  else driverEnd = "a"; // inout <-> inout

  return { ok: true, driverEnd, bidirectional };
}

function findPort(blockId, portId) {
  const block = state.blocks.find((b) => b.id === blockId);
  if (!block) return null;
  const port = block.ports.find((p) => p.id === portId);
  if (!port) return null;
  return { blockId, portId, port };
}

// ============================================================================
// recursive layout — shared by top-level blocks and any expanded child
// ============================================================================

/**
 * Lays out one box: its own ports on left/right/bottom edges, and — if
 * `expanded` and it has children — those children stacked vertically inside,
 * each recursively laid out the same way.
 *
 * Returns { w, h, ports:{name:{x,y,edge}}, childLayouts:[{instName,moduleType,x,y,layout,node}] }
 */
function layoutNode(ports, children, expanded, title, subtitle) {
  const left = ports.filter((p) => p.dir === "input");
  const right = ports.filter((p) => p.dir === "output");
  const bottom = ports.filter((p) => p.dir === "inout");
  const rows = Math.max(left.length, right.length, 1);

  const leftMax = left.reduce((m, p) => Math.max(m, textWidth(portLabel(p), LABEL_FONT)), 0);
  const rightMax = right.reduce((m, p) => Math.max(m, textWidth(portLabel(p), LABEL_FONT)), 0);

  // Reserve room for the title row: title text, subtitle badge, and the
  // refresh/expand icon buttons (reserved even if not all are drawn for
  // this particular node — simpler than threading render-time flags in).
  const titleW = textWidth(title || "", TITLE_FONT);
  const subtitleW = subtitle ? textWidth(subtitle, LABEL_FONT) + 10 : 0;
  const ICONS_W = 40;
  const titleRowW = PAD_SIDE + titleW + 14 + subtitleW + ICONS_W + PAD_SIDE;

  const hasExpandableChildren = children && children.length > 0;
  const showChildren = expanded && hasExpandableChildren;

  let w, h;
  const childLayouts = [];

  if (!showChildren) {
    w = Math.max(
      MIN_W,
      titleRowW,
      PAD_SIDE * 2 + leftMax + rightMax + 34,
      bottom.length ? bottom.length * INOUT_GAP + PAD_SIDE * 2 : 0
    );
    h = PAD_TOP + rows * ROW_H + PAD_BOTTOM + (bottom.length ? INOUT_LABEL_H : 0);
  } else {
    let cursorY = PAD_TOP + CHILD_TOP_MARGIN;
    let innerW = 0;
    children.forEach((child) => {
      const sub = layoutNode(child.ports, child.children, !!child._expanded, child.instName, child.moduleType);
      childLayouts.push({ instName: child.instName, moduleType: child.moduleType, x: 0, y: cursorY, layout: sub, node: child });
      cursorY += sub.h + CHILD_GAP;
      innerW = Math.max(innerW, sub.w);
    });
    cursorY += CHILD_TOP_MARGIN;

    const leftColW = Math.max(SIDE_COL_W, leftMax + 36);
    const rightColW = Math.max(SIDE_COL_W, rightMax + 36);
    innerW = Math.max(innerW, 140);
    w = Math.max(MIN_W * 1.4, titleRowW, leftColW + innerW + rightColW + 24);

    const centerX = leftColW + 12;
    childLayouts.forEach((cl) => {
      cl.x = centerX + (innerW - cl.layout.w) / 2;
    });

    h = Math.max(PAD_TOP + rows * ROW_H + PAD_BOTTOM, cursorY) + (bottom.length ? INOUT_LABEL_H : 0);
  }

  const positions = {};
  left.forEach((p, i) => {
    positions[p.name] = { x: 0, y: PAD_TOP + i * ROW_H + ROW_H / 2, edge: "left" };
  });
  right.forEach((p, i) => {
    positions[p.name] = { x: w, y: PAD_TOP + i * ROW_H + ROW_H / 2, edge: "right" };
  });
  const bottomY = PAD_TOP + rows * ROW_H + PAD_BOTTOM / 2;
  bottom.forEach((p, i) => {
    const slotW = (w - PAD_SIDE * 2) / bottom.length;
    positions[p.name] = { x: PAD_SIDE + slotW * (i + 0.5), y: bottomY, edge: "bottom" };
  });

  return { w, h, ports: positions, childLayouts, showChildren, hasExpandableChildren };
}

/** Resolves a port-name reference against a layout's port-position map,
 * falling back to fanning out across every indexed sibling (`name_0`,
 * `name_1`, ...) when the exact name isn't there — this is how a
 * whole-Vec connect (`io.res` covering `io_res_0..3`) is represented. */
function resolvePositions(layoutPorts, portName) {
  if (layoutPorts[portName]) return [layoutPorts[portName]];
  const prefix = portName + "_";
  return Object.keys(layoutPorts)
    .filter((k) => k.startsWith(prefix))
    .sort()
    .map((k) => layoutPorts[k]);
}

function computeLayout(block) {
  const children = block.hierarchy && block.hierarchy.children ? block.hierarchy.children : [];
  const subtitle = block.kind === "module" ? `v${block.moduleVersion}` : "custom";
  return layoutNode(block.ports, children, !!block.expanded, block.label || block.name, subtitle);
}

function getPortWorldPos(blockId, portId) {
  const block = state.blocks.find((b) => b.id === blockId);
  if (!block || !block._layout) return null;
  const pos = block._layout.ports[portId];
  if (!pos) return null;
  return { x: block.x + pos.x, y: block.y + pos.y, edge: pos.edge };
}

// ============================================================================
// wire geometry
// ============================================================================

function stubOffset(edge) {
  switch (edge) {
    case "left":
      return { dx: -STUB, dy: 0 };
    case "right":
      return { dx: STUB, dy: 0 };
    case "bottom":
      return { dx: 0, dy: STUB };
    default:
      return { dx: 0, dy: 0 };
  }
}

function bezierPath(a, b) {
  const ea = stubOffset(a.edge);
  const eb = stubOffset(b.edge);
  const p1 = { x: a.x + ea.dx, y: a.y + ea.dy };
  const p2 = { x: b.x + eb.dx, y: b.y + eb.dy };
  const bend = Math.max(40, Math.abs(p2.x - p1.x) * 0.5);
  const c1 = { x: p1.x + (a.edge === "bottom" ? 0 : ea.dx >= 0 ? bend : -bend), y: p1.y + (a.edge === "bottom" ? bend : 0) };
  const c2 = { x: p2.x + (b.edge === "bottom" ? 0 : eb.dx >= 0 ? bend : -bend), y: p2.y + (b.edge === "bottom" ? bend : 0) };
  return `M${a.x},${a.y} L${p1.x},${p1.y} C${c1.x},${c1.y} ${c2.x},${c2.y} ${p2.x},${p2.y} L${b.x},${b.y}`;
}

function straightPath(a, b) {
  const ea = stubOffset(a.edge);
  const eb = stubOffset(b.edge);
  return `M${a.x},${a.y} L${a.x + ea.dx},${a.y + ea.dy} L${b.x + eb.dx},${b.y + eb.dy} L${b.x},${b.y}`;
}

// ============================================================================
// rendering
// ============================================================================

function applyView() {
  viewport.setAttribute("transform", `translate(${view.x},${view.y}) scale(${view.scale})`);
  zoomLevelEl.textContent = `${Math.round(view.scale * 100)}%`;
}

function renderGrid() {
  gridLayer.innerHTML = "";
  const span = 4000;
  gridLayer.appendChild(
    svgEl("rect", { x: -span, y: -span, width: span * 2, height: span * 2, fill: "url(#grid-pattern)" })
  );
}

function renderAll() {
  state.blocks.forEach((b) => (b._layout = computeLayout(b)));
  wiresLayer.innerHTML = "";
  blocksLayer.innerHTML = "";
  state.wires.forEach(renderWire);
  state.blocks.forEach(renderBlock);
}

function renderWire(wire) {
  const a = getPortWorldPos(wire.from.blockId, wire.from.portId);
  const b = getPortWorldPos(wire.to.blockId, wire.to.portId);
  if (!a || !b) return;
  const d = bezierPath(a, b);
  const isSelected = selected && selected.type === "wire" && selected.id === wire.id;
  const kindClass = wire.kind === "inout" ? "k-inout" : "k-output";
  const marker = isSelected ? "arrow-selected" : wire.kind === "inout" ? "arrow-inout" : "arrow-output";

  const g = svgEl("g", { class: "wire-group", "data-wire-id": wire.id });
  const hit = svgEl("path", { d, class: "wire-hit" });
  const path = svgEl("path", {
    d,
    class: `wire ${kindClass}${isSelected ? " selected" : ""}`,
    "marker-end": `url(#${marker})`,
  });
  hit.addEventListener("mousedown", (e) => {
    e.stopPropagation();
    selected = { type: "wire", id: wire.id };
    renderAll();
  });
  g.appendChild(hit);
  g.appendChild(path);
  wiresLayer.appendChild(g);
}

function renderBlock(block) {
  const g = svgEl("g", {
    class: `block${selected && selected.type === "block" && selected.id === block.id ? " selected" : ""}`,
    "data-block-id": block.id,
    transform: `translate(${block.x},${block.y})`,
  });

  renderNodeBody(g, {
    layout: block._layout,
    ports: block.ports,
    wires: (block.hierarchy && block.hierarchy.wires) || [],
    title: block.label || block.name,
    subtitle: block.kind === "module" ? `v${block.moduleVersion}` : "custom",
    interactivePorts: true,
    blockId: block.id,
    onToggleExpand: block._layout.hasExpandableChildren
      ? () => {
          block.expanded = !block.expanded;
          renderAll();
          markDirty();
        }
      : null,
    onRefresh: block.kind === "module" ? () => updateBlockToLatest(block.id) : null,
    onRename: () => renameBlock(block.id),
    depth: 0,
  });

  g.addEventListener("mousedown", (e) => {
    if (e.target.closest(".port") || e.target.closest(".btn-refresh") || e.target.closest(".btn-expand")) return;
    e.stopPropagation();
    selected = { type: "block", id: block.id };
    renderAll();
    const w0 = toWorld(e.clientX, e.clientY);
    dragState = { type: "block", blockId: block.id, startWorld: w0, origBX: block.x, origBY: block.y };
  });

  blocksLayer.appendChild(g);
}

/** Renders one box (top-level block or nested expanded child) plus, if
 * expanded, its children and the internal wires connecting them — all
 * inside the SVG group `parentG`, in that box's own local coordinates. */
function renderNodeBody(parentG, opts) {
  const { layout, ports, title, subtitle, interactivePorts, blockId, onToggleExpand, onRefresh, onRename, depth } = opts;
  const w = layout.w;
  const h = layout.h;

  parentG.appendChild(svgEl("rect", { class: "body", x: 0, y: 0, width: w, height: h, rx: 8, ry: 8 }));
  parentG.appendChild(
    svgEl("path", {
      class: "titlebar",
      d: `M0,8 A8,8 0 0 1 8,0 L${w - 8},0 A8,8 0 0 1 ${w},8 L${w},${PAD_TOP} L0,${PAD_TOP} Z`,
    })
  );
  const titleEl = svgEl("text", { class: "title", x: PAD_SIDE, y: 21 }, title);
  if (onRename) {
    titleEl.style.cursor = "text";
    titleEl.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      onRename();
    });
  }
  parentG.appendChild(titleEl);

  let rightCursor = w - PAD_SIDE;
  if (onRefresh) {
    rightCursor -= 14;
    parentG.appendChild(buildIconButton(rightCursor, 11, onRefresh, "refresh-glyph", "btn-refresh", "Update to latest registered version"));
  }
  if (onToggleExpand) {
    rightCursor -= 20;
    parentG.appendChild(buildIconButton(rightCursor, 11, onToggleExpand, layout.showChildren ? "collapse-glyph" : "expand-glyph", "btn-expand", layout.showChildren ? "Collapse" : "Expand to show internal wiring"));
  }
  if (subtitle) {
    parentG.appendChild(svgEl("text", { class: "subtitle", x: rightCursor - 8, y: 21, "text-anchor": "end" }, subtitle));
  }

  ports.forEach((p) => {
    const pos = layout.ports[p.name];
    if (!pos) return;
    parentG.appendChild(renderPort(blockId, p, pos, interactivePorts));
  });

  if (layout.showChildren) {
    layout.childLayouts.forEach((cl) => {
      const childG = svgEl("g", { class: "block child-block", transform: `translate(${cl.x},${cl.y})` });
      renderNodeBody(childG, {
        layout: cl.layout,
        ports: cl.node.ports,
        wires: cl.node.wires || [],
        title: cl.instName,
        subtitle: cl.moduleType,
        interactivePorts: false,
        blockId: null,
        onToggleExpand: cl.layout.hasExpandableChildren
          ? () => {
              cl.node._expanded = !cl.node._expanded;
              renderAll();
              markDirty();
            }
          : null,
        onRefresh: null,
        onRename: null,
        depth: depth + 1,
      });
      parentG.appendChild(childG);
    });

    // internal wires, drawn within this same local coordinate space
    const findLayout = (ref) => (ref === "$self" ? { layout, x: 0, y: 0 } : (() => {
      const cl = layout.childLayouts.find((c) => c.instName === ref);
      return cl ? { layout: cl.layout, x: cl.x, y: cl.y } : null;
    })());

    (opts.wires || []).forEach((wire) => {
      const fromCtx = findLayout(wire.from.ref);
      const toCtx = findLayout(wire.to.ref);
      if (!fromCtx || !toCtx) return;
      const fromPositions = resolvePositions(fromCtx.layout.ports, wire.from.port);
      const toPositions = resolvePositions(toCtx.layout.ports, wire.to.port);
      const pairs = fromPositions.length === toPositions.length
        ? fromPositions.map((p, i) => [p, toPositions[i]])
        : fromPositions.length === 1
        ? toPositions.map((tp) => [fromPositions[0], tp])
        : toPositions.length === 1
        ? fromPositions.map((fp) => [fp, toPositions[0]])
        : [];
      pairs.forEach(([fp, tp]) => {
        const a = { x: fromCtx.x + fp.x, y: fromCtx.y + fp.y, edge: fp.edge };
        const b = { x: toCtx.x + tp.x, y: toCtx.y + tp.y, edge: tp.edge };
        parentG.appendChild(
          svgEl("path", { d: straightPath(a, b), class: "internal-wire" })
        );
      });
    });
  }
}

const GLYPHS = {
  "refresh-glyph": "M-4,-1 A4,4 0 1 1 -4.6,1.4 M-4,-4 L-4,-1 L-1,-1",
  "expand-glyph": "M-3,-2 L0,3 L3,-2",
  "collapse-glyph": "M-3,2 L0,-3 L3,2",
};

function buildIconButton(cx, cy, onClick, glyphKey, cls, title) {
  const g = svgEl("g", { class: cls, transform: `translate(${cx},${cy})` });
  g.appendChild(svgEl("circle", { cx: 0, cy: 0, r: 9 }));
  g.appendChild(
    svgEl("path", {
      d: GLYPHS[glyphKey],
      fill: "none",
      "stroke-width": 1.4,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
    })
  );
  g.appendChild(svgEl("title", null, title));
  g.addEventListener("mousedown", (e) => {
    e.stopPropagation();
    onClick();
  });
  return g;
}

function renderPort(blockId, port, pos, interactive) {
  const wired = interactive && portHasWire(blockId, port.id);
  const g = svgEl("g", {
    class: `port dir-${port.dir}${wired ? " has-wire" : ""}${interactive ? "" : " readonly"}`,
    "data-port-id": port.id,
    transform: `translate(${pos.x},${pos.y})`,
  });

  const stub = stubOffset(pos.edge);
  g.appendChild(svgEl("line", { class: "stub", x1: 0, y1: 0, x2: stub.dx, y2: stub.dy }));
  g.appendChild(svgEl("circle", { class: `pin ${port.dir}`, cx: stub.dx, cy: stub.dy, r: interactive ? PIN_R : PIN_R - 1.5 }));

  let labelX = 0;
  let anchor = "start";
  if (pos.edge === "left") {
    labelX = PIN_R + 4;
    anchor = "start";
  } else if (pos.edge === "right") {
    labelX = -PIN_R - 4;
    anchor = "end";
  }
  if (pos.edge === "bottom") {
    g.appendChild(svgEl("text", { class: "label", x: 0, y: STUB + 13, "text-anchor": "middle" }, portLabel(port)));
  } else {
    g.appendChild(svgEl("text", { class: "label", x: labelX, y: 3.5, "text-anchor": anchor }, portLabel(port)));
  }

  if (interactive) {
    g.addEventListener("mousedown", (e) => {
      e.stopPropagation();
      dragState = { type: "wire", from: { blockId, portId: port.id, port } };
      ensurePreviewPath();
    });
  }
  return g;
}

// ============================================================================
// wire-drawing preview
// ============================================================================

let previewPathEl = null;

function ensurePreviewPath() {
  previewLayer.innerHTML = "";
  previewPathEl = svgEl("path", { class: "preview-wire" });
  previewLayer.appendChild(previewPathEl);
}

function updatePreview(fromRef, worldPt) {
  if (!previewPathEl) return;
  const a = getPortWorldPos(fromRef.blockId, fromRef.portId);
  if (!a) return;
  previewPathEl.setAttribute("d", bezierPath(a, { x: worldPt.x, y: worldPt.y, edge: "left" }));
}

function clearPreview() {
  previewLayer.innerHTML = "";
  previewPathEl = null;
}

// ============================================================================
// pan / zoom / drag
// ============================================================================

function toWorld(clientX, clientY) {
  const rect = scroller.getBoundingClientRect();
  const sx = clientX - rect.left;
  const sy = clientY - rect.top;
  return { x: (sx - view.x) / view.scale, y: (sy - view.y) / view.scale };
}

function setZoom(newScale, anchorClientX, anchorClientY) {
  const rect = scroller.getBoundingClientRect();
  const ax = anchorClientX !== undefined ? anchorClientX - rect.left : rect.width / 2;
  const ay = anchorClientY !== undefined ? anchorClientY - rect.top : rect.height / 2;
  const wx = (ax - view.x) / view.scale;
  const wy = (ay - view.y) / view.scale;
  view.scale = clamp(newScale, 0.3, 2.5);
  view.x = ax - wx * view.scale;
  view.y = ay - wy * view.scale;
  applyView();
}

scroller.addEventListener(
  "wheel",
  (e) => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
    setZoom(view.scale * factor, e.clientX, e.clientY);
  },
  { passive: false }
);

scroller.addEventListener("mousedown", (e) => {
  if (e.button !== 0) return;
  dragState = { type: "pan", startX: e.clientX, startY: e.clientY, origX: view.x, origY: view.y };
  scroller.classList.add("panning");
  selected = null;
  renderAll();
});

window.addEventListener("mousemove", (e) => {
  if (!dragState) return;
  if (dragState.type === "pan") {
    view.x = dragState.origX + (e.clientX - dragState.startX);
    view.y = dragState.origY + (e.clientY - dragState.startY);
    applyView();
  } else if (dragState.type === "block") {
    const w = toWorld(e.clientX, e.clientY);
    const block = state.blocks.find((b) => b.id === dragState.blockId);
    if (block) {
      block.x = dragState.origBX + (w.x - dragState.startWorld.x);
      block.y = dragState.origBY + (w.y - dragState.startWorld.y);
      renderAll();
    }
  } else if (dragState.type === "wire") {
    updatePreview(dragState.from, toWorld(e.clientX, e.clientY));
  }
});

window.addEventListener("mouseup", (e) => {
  if (!dragState) return;
  if (dragState.type === "pan") scroller.classList.remove("panning");
  if (dragState.type === "block") markDirty();
  if (dragState.type === "wire") {
    const portEl = e.target.closest(".port:not(.readonly)");
    if (portEl) {
      const blockEl = portEl.closest(".block[data-block-id]");
      const toRef = blockEl && findPort(blockEl.dataset.blockId, portEl.dataset.portId);
      if (toRef) {
        const result = evaluateConnection(dragState.from, toRef);
        if (result.ok) {
          state.wires.push({
            id: newId("w"),
            from: { blockId: dragState.from.blockId, portId: dragState.from.portId },
            to: { blockId: toRef.blockId, portId: toRef.portId },
            driverEnd: result.driverEnd,
            kind: result.bidirectional ? "inout" : "output",
          });
          markDirty();
        } else {
          showToast(result.reason, false);
        }
      }
    }
    clearPreview();
    renderAll();
  }
  dragState = null;
});

document.getElementById("zoom-in").addEventListener("click", () => setZoom(view.scale * 1.2));
document.getElementById("zoom-out").addEventListener("click", () => setZoom(view.scale / 1.2));
document.getElementById("zoom-reset").addEventListener("click", () => {
  view.x = 60;
  view.y = 60;
  view.scale = 1;
  applyView();
});

// ============================================================================
// copy / paste / rename
// ============================================================================

function renameBlock(blockId) {
  const block = state.blocks.find((b) => b.id === blockId);
  if (!block) return;
  const next = prompt("Instance label:", block.label || block.name);
  if (next === null) return;
  block.label = next.trim() || null;
  renderAll();
  markDirty();
}

function copySelected() {
  if (!selected || selected.type !== "block") return;
  const block = state.blocks.find((b) => b.id === selected.id);
  if (!block) return;
  clipboard = JSON.parse(
    JSON.stringify({
      kind: block.kind,
      name: block.name,
      moduleVersion: block.moduleVersion,
      ports: block.ports,
      hierarchy: block.hierarchy || EMPTY_HIERARCHY,
    })
  );
  showToast(`Copied '${block.label || block.name}'. Ctrl/Cmd+V to paste.`, true);
}

function pasteClipboard() {
  if (!clipboard) return;
  const instanceCount = state.blocks.filter((b) => b.name === clipboard.name).length;
  const cascade = state.blocks.length * 18;
  const block = {
    id: newId("b"),
    kind: clipboard.kind,
    name: clipboard.name,
    label: `${clipboard.name} #${instanceCount + 1}`,
    moduleVersion: clipboard.moduleVersion,
    x: 100 + (cascade % 300),
    y: 100 + (cascade % 260),
    expanded: false,
    ports: clipboard.ports.map((p) => ({ ...p })),
    hierarchy: clipboard.hierarchy,
  };
  state.blocks.push(block);
  selected = { type: "block", id: block.id };
  renderAll();
  markDirty();
  showToast(`Pasted another '${clipboard.name}'.`, true);
}

window.addEventListener("keydown", (e) => {
  const tag = document.activeElement && document.activeElement.tagName;
  if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;

  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === "c") {
    e.preventDefault();
    copySelected();
    return;
  }
  if (mod && e.key.toLowerCase() === "v") {
    e.preventDefault();
    pasteClipboard();
    return;
  }
  if ((e.key === "Delete" || e.key === "Backspace") && selected) {
    if (selected.type === "block") {
      state.blocks = state.blocks.filter((b) => b.id !== selected.id);
      state.wires = state.wires.filter((w) => w.from.blockId !== selected.id && w.to.blockId !== selected.id);
    } else if (selected.type === "wire") {
      state.wires = state.wires.filter((w) => w.id !== selected.id);
    }
    selected = null;
    renderAll();
    markDirty();
  }
  if (e.key === "Escape") {
    closeModal();
    dragState = null;
    clearPreview();
  }
});

// ============================================================================
// toast / dirty tracking
// ============================================================================

let toastTimer = null;
function showToast(message, ok) {
  toastEl.textContent = message;
  toastEl.className = ok ? "ok" : "";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.add("hidden"), 3200);
}

function markDirty() {
  dirty = true;
  saveStatusEl.textContent = "unsaved changes";
}

// ============================================================================
// palette / registry polling
// ============================================================================

function placeModuleBlock(mod) {
  const ports = mod.ports.map((p) => ({ id: p.name, name: p.name, dir: p.dir, width: p.width }));
  const cascade = state.blocks.length * 18;
  const block = {
    id: newId("b"),
    kind: "module",
    name: mod.name,
    label: null,
    moduleVersion: mod.version,
    x: 60 + (cascade % 260),
    y: 60 + (cascade % 220),
    expanded: false,
    ports,
    hierarchy: mod.hierarchy || EMPTY_HIERARCHY,
  };
  state.blocks.push(block);
  selected = { type: "block", id: block.id };
  renderAll();
  markDirty();
}

function placeModuleVersion(name, version) {
  const entry = registryAll[name];
  if (!entry) return;
  const record = entry.versions.find((v) => v.version === version);
  if (!record) return;
  placeModuleBlock({ name, ...record });
}

function updateBlockToLatest(blockId) {
  const block = state.blocks.find((b) => b.id === blockId);
  if (!block) return;
  const latest = registryLatest.find((m) => m.name === block.name);
  if (!latest) {
    showToast(`No registered versions of '${block.name}' found.`, false);
    return;
  }
  if (latest.version === block.moduleVersion) {
    showToast(`'${block.name}' is already at v${latest.version}.`, true);
    return;
  }
  block.moduleVersion = latest.version;
  block.ports = latest.ports.map((p) => ({ id: p.name, name: p.name, dir: p.dir, width: p.width }));
  block.hierarchy = latest.hierarchy || EMPTY_HIERARCHY;
  const validIds = new Set(block.ports.map((p) => p.id));
  state.wires = state.wires.filter((w) => {
    const touchesBlock = w.from.blockId === block.id || w.to.blockId === block.id;
    if (!touchesBlock) return true;
    const pid = w.from.blockId === block.id ? w.from.portId : w.to.portId;
    return validIds.has(pid);
  });
  renderAll();
  markDirty();
  showToast(`'${block.name}' updated to v${latest.version}.`, true);
}

function renderPalette() {
  const query = searchEl.value.trim().toLowerCase();
  const items = registryLatest.filter((m) => m.name.toLowerCase().includes(query));
  moduleCountEl.textContent = registryLatest.length;
  moduleListEl.innerHTML = "";
  moduleEmptyEl.classList.toggle("hidden", registryLatest.length !== 0);

  items.forEach((mod) => {
    const row = document.createElement("div");
    row.className = "module-row";

    const top = document.createElement("div");
    top.className = "row-top";
    const name = document.createElement("span");
    name.className = "m-name";
    name.textContent = mod.name;
    const badge = document.createElement("span");
    badge.className = "m-badge";
    badge.textContent = `latest v${mod.version}`;
    top.appendChild(name);
    top.appendChild(badge);

    const meta = document.createElement("div");
    meta.className = "m-meta";
    const nIn = mod.ports.filter((p) => p.dir === "input").length;
    const nOut = mod.ports.filter((p) => p.dir === "output").length;
    const nIO = mod.ports.filter((p) => p.dir === "inout").length;
    const nChildren = mod.hierarchy && mod.hierarchy.children ? mod.hierarchy.children.length : 0;
    meta.textContent = `${nIn} in · ${nOut} out${nIO ? ` · ${nIO} inout` : ""}${nChildren ? ` · ${nChildren} internal` : ""}`;

    row.appendChild(top);
    row.appendChild(meta);

    if (mod.versionCount > 1) {
      const select = document.createElement("select");
      const entry = registryAll[mod.name];
      (entry ? entry.versions : []).slice().reverse().forEach((v) => {
        const opt = document.createElement("option");
        opt.value = v.version;
        opt.textContent = `place v${v.version} (${new Date(v.timestamp).toLocaleTimeString()})`;
        select.appendChild(opt);
      });
      select.addEventListener("click", (e) => e.stopPropagation());
      select.addEventListener("change", () => {
        placeModuleVersion(mod.name, Number(select.value));
        select.value = "";
      });
      row.appendChild(select);
      row.addEventListener("click", (e) => {
        if (e.target === select) return;
        placeModuleBlock(mod);
      });
    } else {
      row.addEventListener("click", () => placeModuleBlock(mod));
    }

    moduleListEl.appendChild(row);
  });
}

searchEl.addEventListener("input", renderPalette);

async function pollRegistry() {
  try {
    const [latestRes, allRes] = await Promise.all([fetch("/api/modules"), fetch("/api/modules/all")]);
    registryLatest = await latestRes.json();
    registryAll = await allRes.json();
    connDot.className = "dot live";
    connStatus.textContent = `connected — ${registryLatest.length} module(s)`;
    renderPalette();
  } catch (e) {
    connDot.className = "dot down";
    connStatus.textContent = "server unreachable";
  }
}

// ============================================================================
// custom block modal
// ============================================================================

const overlay = document.getElementById("modal-overlay");
const cbName = document.getElementById("cb-name");
const cbPorts = document.getElementById("cb-ports");

function openModal() {
  overlay.classList.remove("hidden");
  cbName.value = "";
  cbPorts.innerHTML = "";
  addPortRow();
  cbName.focus();
}
function closeModal() {
  overlay.classList.add("hidden");
}

function addPortRow(defaults) {
  const row = document.createElement("div");
  row.className = "port-row";

  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.placeholder = "port name";
  nameInput.value = (defaults && defaults.name) || "";

  const dirSelect = document.createElement("select");
  ["input", "output", "inout"].forEach((d) => {
    const opt = document.createElement("option");
    opt.value = d;
    opt.textContent = d;
    dirSelect.appendChild(opt);
  });
  dirSelect.value = (defaults && defaults.dir) || "input";

  const widthInput = document.createElement("input");
  widthInput.type = "number";
  widthInput.min = "1";
  widthInput.value = (defaults && defaults.width) || 1;

  const rm = document.createElement("button");
  rm.className = "rm";
  rm.type = "button";
  rm.textContent = "✕";
  rm.addEventListener("click", () => row.remove());

  row.appendChild(nameInput);
  row.appendChild(dirSelect);
  row.appendChild(widthInput);
  row.appendChild(rm);
  cbPorts.appendChild(row);
}

document.getElementById("btn-custom").addEventListener("click", openModal);
document.getElementById("cb-cancel").addEventListener("click", closeModal);
document.getElementById("cb-add-port").addEventListener("click", () => addPortRow());
overlay.addEventListener("mousedown", (e) => {
  if (e.target === overlay) closeModal();
});

document.getElementById("cb-create").addEventListener("click", () => {
  const name = cbName.value.trim() || "Custom block";
  const ports = [];
  const seen = new Set();
  let droppedDupes = 0;
  cbPorts.querySelectorAll(".port-row").forEach((row) => {
    const [nameInput, dirSelect, widthInput] = row.querySelectorAll("input,select");
    const pname = nameInput.value.trim();
    if (!pname) return;
    if (seen.has(pname)) {
      droppedDupes++;
      return;
    }
    seen.add(pname);
    ports.push({
      id: pname,
      name: pname,
      dir: dirSelect.value,
      width: clamp(parseInt(widthInput.value, 10) || 1, 1, 4096),
    });
  });
  const cascade = state.blocks.length * 18;
  const block = {
    id: newId("b"),
    kind: "custom",
    name,
    label: null,
    x: 60 + (cascade % 260),
    y: 60 + (cascade % 220),
    expanded: false,
    ports,
    hierarchy: EMPTY_HIERARCHY,
  };
  state.blocks.push(block);
  selected = { type: "block", id: block.id };
  renderAll();
  markDirty();
  closeModal();
  if (droppedDupes) showToast(`Dropped ${droppedDupes} port(s) with duplicate names.`, false);
});

// ============================================================================
// save / load / export / clear
// ============================================================================

function serializeCanvas() {
  return {
    blocks: state.blocks.map((b) => ({
      id: b.id,
      kind: b.kind,
      name: b.name,
      label: b.label || null,
      moduleVersion: b.moduleVersion,
      x: b.x,
      y: b.y,
      expanded: !!b.expanded,
      ports: b.ports,
      hierarchy: b.hierarchy || EMPTY_HIERARCHY,
    })),
    wires: state.wires,
  };
}

async function saveCanvas() {
  try {
    const res = await fetch("/api/canvas", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(serializeCanvas()),
    });
    if (!res.ok) throw new Error(await res.text());
    dirty = false;
    saveStatusEl.textContent = `saved · ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    saveStatusEl.textContent = "save failed — is the server running?";
  }
}

async function loadCanvas() {
  try {
    const res = await fetch("/api/canvas");
    const data = await res.json();
    state.blocks = (data.blocks || []).map((b) => ({ ...b, hierarchy: b.hierarchy || EMPTY_HIERARCHY, _layout: null }));
    state.wires = data.wires || [];
    renderAll();
  } catch (e) {
    // no saved canvas yet — start empty
  }
}

document.getElementById("btn-save").addEventListener("click", saveCanvas);
document.getElementById("btn-clear").addEventListener("click", () => {
  if (!confirm("Clear the whole canvas? This can't be undone.")) return;
  state.blocks = [];
  state.wires = [];
  selected = null;
  renderAll();
  markDirty();
});

const EXPORT_STYLE = `
  text{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
  .block .body{fill:#151b1f;stroke:#2a333a;stroke-width:1.4;}
  .block .titlebar{fill:#1b2227;}
  .block .title{fill:#dde6ea;font-size:12.5px;font-weight:700;}
  .block .subtitle{fill:#7e8c93;font-size:9.5px;}
  .btn-refresh,.btn-expand{display:none;}
  .port .pin{stroke-width:1.6;fill:#151b1f;}
  .port .pin.input{stroke:#4da6ff;}
  .port .pin.output{stroke:#ff9f45;}
  .port .pin.inout{stroke:#c084fc;}
  .port.dir-input .stub{stroke:#4da6ff;stroke-width:1.6;}
  .port.dir-output .stub{stroke:#ff9f45;stroke-width:1.6;}
  .port.dir-inout .stub{stroke:#c084fc;stroke-width:1.6;}
  .port .label{fill:#dde6ea;font-size:10px;}
  .wire{fill:none;stroke-width:2;}
  .wire.k-output{stroke:#ff9f45;}
  .wire.k-inout{stroke:#c084fc;stroke-dasharray:5 4;}
  .internal-wire{fill:none;stroke:#5b6b72;stroke-width:1.3;stroke-dasharray:3 3;}
`;

document.getElementById("btn-export").addEventListener("click", () => {
  if (!state.blocks.length) {
    showToast("Nothing to export yet.", false);
    return;
  }
  const clone = svg.cloneNode(true);
  clone.setAttribute("xmlns", SVG_NS);
  clone.querySelector("#grid-layer").remove();
  clone.querySelector("#preview-layer").remove();
  clone.querySelector("#viewport").removeAttribute("transform");

  const bbox = blocksLayer.getBBox();
  const pad = 40;
  const vx = bbox.x - pad, vy = bbox.y - pad, vw = bbox.width + pad * 2, vh = bbox.height + pad * 2;
  clone.setAttribute("width", vw);
  clone.setAttribute("height", vh);
  clone.setAttribute("viewBox", `${vx} ${vy} ${vw} ${vh}`);

  const bg = document.createElementNS(SVG_NS, "rect");
  bg.setAttribute("x", vx);
  bg.setAttribute("y", vy);
  bg.setAttribute("width", vw);
  bg.setAttribute("height", vh);
  bg.setAttribute("fill", "#0f1417");

  const style = document.createElementNS(SVG_NS, "style");
  style.textContent = EXPORT_STYLE;

  clone.insertBefore(bg, clone.firstChild);
  clone.insertBefore(style, clone.firstChild);

  const xml = new XMLSerializer().serializeToString(clone);
  const blob = new Blob([xml], { type: "image/svg+xml" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "module_canvas.svg";
  a.click();
  URL.revokeObjectURL(url);
});

window.addEventListener("beforeunload", (e) => {
  if (dirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});
setInterval(() => {
  if (dirty) saveCanvas();
}, 8000);

// ============================================================================
// init
// ============================================================================

(async function init() {
  renderGrid();
  applyView();
  await loadCanvas();
  await pollRegistry();
  setInterval(pollRegistry, 2500);
})();
