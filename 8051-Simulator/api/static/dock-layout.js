import {
    PANEL_LAYOUT_KEY,
    clampFloatingBounds,
    clampValue,
    MIN_FLOAT_HEIGHT,
    MIN_FLOAT_WIDTH,
} from "./layout-utils.js";
import { rafThrottle } from "./animation-utils.js";

const STORAGE_KEY = `${PANEL_LAYOUT_KEY}-dock`;
const LAYOUT_VERSION = 2;
const ROOT_EDGE_THRESHOLD = 104;
const STACK_EDGE_THRESHOLD = 76;
const FLOAT_MARGIN = 18;
const SPLIT_GAP = 12;
const SPLITTER_SIZE = 10;
const DEFAULT_FLOAT = { width: 440, height: 300 };
const DEFAULT_EASE = "cubic-bezier(0.2, 0.8, 0.2, 1)";

let stackSequence = 0;
let splitSequence = 0;

export function createStack(panels = [], active = panels[0] || null, id = `stack-${++stackSequence}`) {
    return {
        type: "stack",
        id,
        panels: [...new Set(panels)].filter(Boolean),
        active: active && panels.includes(active) ? active : panels[0] || null,
    };
}

export function createSplit(axis = "row", children = [], sizes = null, id = `split-${++splitSequence}`) {
    const nextChildren = children.filter(Boolean);
    return {
        type: "split",
        id,
        axis,
        children: nextChildren,
        sizes: normalizeSizes(sizes, nextChildren.length),
    };
}

export function normalizeSizes(sizes, count) {
    if (!count) {
        return [];
    }
    const fallback = Array.from({ length: count }, () => 1 / count);
    if (!Array.isArray(sizes) || sizes.length !== count) {
        return fallback;
    }
    const sanitized = sizes.map((value) => Number(value) || 0).map((value) => Math.max(0.01, value));
    const total = sanitized.reduce((sum, value) => sum + value, 0);
    if (!total) {
        return fallback;
    }
    return sanitized.map((value) => value / total);
}

export function normalizeLayoutTree(node) {
    if (!node) {
        return null;
    }
    if (node.type === "stack") {
        const panels = [...new Set((node.panels || []).filter(Boolean))];
        if (!panels.length) {
            return null;
        }
        return createStack(panels, node.active, node.id);
    }
    if (node.type !== "split") {
        return null;
    }
    const children = (node.children || []).map((child) => normalizeLayoutTree(child)).filter(Boolean);
    if (!children.length) {
        return null;
    }
    if (children.length === 1) {
        return children[0];
    }
    return createSplit(node.axis === "column" ? "column" : "row", children, node.sizes, node.id);
}

export function findNodeById(node, id, parent = null, index = -1) {
    if (!node) {
        return null;
    }
    if (node.id === id) {
        return { node, parent, index };
    }
    if (node.type === "split") {
        for (let childIndex = 0; childIndex < node.children.length; childIndex += 1) {
            const found = findNodeById(node.children[childIndex], id, node, childIndex);
            if (found) {
                return found;
            }
        }
    }
    return null;
}

export function findPanelLocation(node, panelId, parent = null, index = -1) {
    if (!node) {
        return null;
    }
    if (node.type === "stack" && node.panels.includes(panelId)) {
        return { stack: node, parent, index };
    }
    if (node.type === "split") {
        for (let childIndex = 0; childIndex < node.children.length; childIndex += 1) {
            const found = findPanelLocation(node.children[childIndex], panelId, node, childIndex);
            if (found) {
                return found;
            }
        }
    }
    return null;
}

export function removePanelFromLayout(root, panelId) {
    const location = findPanelLocation(root, panelId);
    if (!location) {
        return root;
    }
    const { stack } = location;
    stack.panels = stack.panels.filter((value) => value !== panelId);
    if (stack.active === panelId) {
        stack.active = stack.panels[0] || null;
    }
    return normalizeLayoutTree(root);
}

export function insertPanelIntoStack(root, stackId, panelId) {
    const target = findNodeById(root, stackId);
    if (!target || target.node.type !== "stack") {
        return root;
    }
    if (!target.node.panels.includes(panelId)) {
        target.node.panels.push(panelId);
    }
    target.node.active = panelId;
    return normalizeLayoutTree(root);
}

function replaceNode(root, targetId, replacement) {
    const hit = findNodeById(root, targetId);
    if (!hit) {
        return root;
    }
    if (!hit.parent) {
        return replacement;
    }
    hit.parent.children.splice(hit.index, 1, replacement);
    hit.parent.sizes = normalizeSizes(hit.parent.sizes, hit.parent.children.length);
    return normalizeLayoutTree(root);
}

export function splitStackInLayout(root, stackId, edge, panelId) {
    const target = findNodeById(root, stackId);
    if (!target || target.node.type !== "stack") {
        return root;
    }
    const axis = edge === "left" || edge === "right" ? "row" : "column";
    const insertFirst = edge === "left" || edge === "top";
    const fresh = createStack([panelId]);
    const next = createSplit(axis, insertFirst ? [fresh, target.node] : [target.node, fresh], [0.38, 0.62]);
    return replaceNode(root, stackId, next);
}

export function splitRootWithPanel(root, edge, panelId) {
    const axis = edge === "left" || edge === "right" ? "row" : "column";
    const insertFirst = edge === "left" || edge === "top";
    const fresh = createStack([panelId]);
    if (!root) {
        return fresh;
    }
    return createSplit(axis, insertFirst ? [fresh, root] : [root, fresh], [0.28, 0.72]);
}

export function distanceToRect(rect, x, y) {
    const dx = Math.max(rect.left - x, 0, x - rect.right);
    const dy = Math.max(rect.top - y, 0, y - rect.bottom);
    return Math.hypot(dx, dy);
}

export function resolveStackDropZone(rect, x, y, threshold = STACK_EDGE_THRESHOLD) {
    const expanded = {
        left: rect.left - threshold,
        top: rect.top - threshold,
        right: rect.right + threshold,
        bottom: rect.bottom + threshold,
    };
    if (x < expanded.left || x > expanded.right || y < expanded.top || y > expanded.bottom) {
        return null;
    }

    const inside = x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
    const edgeX = Math.min(threshold, rect.width * 0.22);
    const edgeY = Math.min(threshold, rect.height * 0.22);
    const localX = clampValue(x, rect.left, rect.right) - rect.left;
    const localY = clampValue(y, rect.top, rect.bottom) - rect.top;
    const distLeft = localX;
    const distRight = rect.width - localX;
    const distTop = localY;
    const distBottom = rect.height - localY;
    const edgeDistance = Math.min(distLeft, distRight, distTop, distBottom);

    let zone = "center";
    if (inside && edgeDistance <= Math.max(18, Math.min(edgeX, edgeY))) {
        const nearest = Math.min(distLeft, distRight, distTop, distBottom);
        if (nearest === distLeft) {
            zone = "left";
        } else if (nearest === distRight) {
            zone = "right";
        } else if (nearest === distTop) {
            zone = "top";
        } else {
            zone = "bottom";
        }
    }

    if (!inside) {
        const clampedX = clampValue(x, rect.left, rect.right);
        const clampedY = clampValue(y, rect.top, rect.bottom);
        const dx = x - clampedX;
        const dy = y - clampedY;
        if (Math.abs(dx) >= Math.abs(dy)) {
            zone = dx < 0 ? "left" : "right";
        } else {
            zone = dy < 0 ? "top" : "bottom";
        }
    }

    return {
        zone,
        inside,
        distance: distanceToRect(rect, x, y),
    };
}

function createDefaultLayout() {
    return {
        version: LAYOUT_VERSION,
        root: createSplit("row", [
            createSplit("column", [
                createStack(["target"]),
                createStack(["execution"]),
                createStack(["registers"]),
                createStack(["callstack"]),
            ], [0.28, 0.22, 0.3, 0.2]),
            createSplit("column", [
                createStack(["editor"]),
                createStack(["assembler"]),
                createStack(["debugger"]),
                createStack(["trace"]),
            ], [0.42, 0.18, 0.18, 0.22]),
            createSplit("column", [
                createStack(["memory"]),
                createStack(["metrics"]),
            ], [0.78, 0.22]),
        ], [0.22, 0.48, 0.3]),
        floating: {},
        activePanelId: "editor",
    };
}

function getPanelTitle(panel) {
    const title = panel.querySelector(".panel-title");
    if (!title) {
        return panel.dataset.panel || "Panel";
    }
    return Array.from(title.childNodes)
        .map((node) => node.textContent || "")
        .join(" ")
        .replace(/\s+/g, " ")
        .trim() || panel.dataset.panel || "Panel";
}

function collectPanelIds(node, bucket = new Set()) {
    if (!node) {
        return bucket;
    }
    if (node.type === "stack") {
        node.panels.forEach((panelId) => bucket.add(panelId));
        return bucket;
    }
    node.children.forEach((child) => collectPanelIds(child, bucket));
    return bucket;
}

function withDefaultTransition(node) {
    if (!node) {
        return;
    }
    node.style.transition = `transform 200ms ${DEFAULT_EASE}, width 200ms ${DEFAULT_EASE}, height 200ms ${DEFAULT_EASE}, opacity 180ms ease`;
}

function setTransformRect(node, rect, { round = true } = {}) {
    if (!node || !rect) {
        return;
    }
    const left = round ? Math.round(rect.left) : rect.left;
    const top = round ? Math.round(rect.top) : rect.top;
    const width = round ? Math.round(rect.width) : rect.width;
    const height = round ? Math.round(rect.height) : rect.height;
    node.style.width = `${width}px`;
    node.style.height = `${height}px`;
    node.style.transform = `translate3d(${left}px, ${top}px, 0)`;
}

function buildSplitRects(rect, axis, sizes) {
    const count = sizes.length;
    if (!count) {
        return [];
    }
    const gapTotal = SPLIT_GAP * Math.max(0, count - 1);
    const extent = axis === "row" ? rect.width : rect.height;
    const usable = Math.max(0, extent - gapTotal);
    const normalized = normalizeSizes(sizes, count);
    const rects = [];
    let cursor = axis === "row" ? rect.left : rect.top;

    for (let index = 0; index < count; index += 1) {
        const isLast = index === count - 1;
        const chunk = isLast
            ? (axis === "row" ? rect.left + rect.width : rect.top + rect.height) - cursor
            : usable * normalized[index];
        if (axis === "row") {
            rects.push({ left: cursor, top: rect.top, width: chunk, height: rect.height });
            cursor += chunk + SPLIT_GAP;
        } else {
            rects.push({ left: rect.left, top: cursor, width: rect.width, height: chunk });
            cursor += chunk + SPLIT_GAP;
        }
    }
    return rects;
}

export class DockLayoutManager {
    constructor({ workspace, onLayoutChange = null }) {
        this.workspace = workspace;
        this.onLayoutChange = onLayoutChange;
        this.rootEl = null;
        this.snapPreview = null;
        this.dragLayer = null;
        this.floatingLayer = null;
        this.panels = new Map();
        this.panelTitles = new Map();
        this.stackRefs = new Map();
        this.splitRefs = new Map();
        this.layout = null;
        this.activePanelId = null;
        this.previewTarget = null;
        this.zCounter = 20;
        this.dragState = null;
        this.resizeState = null;
        this.onResize = rafThrottle(() => {
            this.applyDockGeometry();
            this.clampFloatingPanels();
            this.renderSnapPreview(this.previewTarget);
        });
    }

    bootstrap() {
        if (!this.workspace) {
            return;
        }
        const sourcePanels = Array.from(this.workspace.querySelectorAll(".dock-panel"));
        sourcePanels.forEach((panel) => {
            const panelId = panel.dataset.panel;
            if (!panelId) {
                return;
            }
            this.panels.set(panelId, panel);
            this.panelTitles.set(panelId, getPanelTitle(panel));
            this.bindPanelChrome(panelId, panel);
        });

        this.workspace.innerHTML = "";
        this.workspace.classList.add("workspace-docking-ready");

        this.rootEl = document.createElement("div");
        this.rootEl.className = "dock-root";
        this.snapPreview = document.createElement("div");
        this.snapPreview.id = "snap-preview";
        this.snapPreview.className = "snap-preview";
        this.snapPreview.hidden = true;
        this.dragLayer = document.createElement("div");
        this.dragLayer.className = "drag-preview-layer";
        this.floatingLayer = document.createElement("div");
        this.floatingLayer.id = "floating-layer";
        this.floatingLayer.className = "floating-layer";
        this.workspace.append(this.rootEl, this.snapPreview, this.dragLayer, this.floatingLayer);

        this.layout = this.restoreLayout();
        this.activePanelId = this.layout.activePanelId || "editor";
        this.render();
        window.addEventListener("resize", this.onResize);
    }

    restoreLayout() {
        const raw = window.localStorage.getItem(STORAGE_KEY);
        if (!raw) {
            return this.reconcileLayout(createDefaultLayout());
        }
        try {
            const parsed = JSON.parse(raw);
            if (parsed?.version !== LAYOUT_VERSION) {
                return this.reconcileLayout(createDefaultLayout());
            }
            return this.reconcileLayout({
                version: LAYOUT_VERSION,
                root: normalizeLayoutTree(parsed.root) || createDefaultLayout().root,
                floating: typeof parsed.floating === "object" && parsed.floating ? parsed.floating : {},
                activePanelId: parsed.activePanelId || "editor",
            });
        } catch {
            return this.reconcileLayout(createDefaultLayout());
        }
    }

    reconcileLayout(layout) {
        const knownPanels = new Set(this.panels.keys());
        const floating = {};
        Object.entries(layout.floating || {}).forEach(([panelId, bounds]) => {
            if (knownPanels.has(panelId)) {
                floating[panelId] = bounds;
            }
        });

        const present = collectPanelIds(layout.root);
        Object.keys(floating).forEach((panelId) => present.add(panelId));
        const missing = Array.from(knownPanels).filter((panelId) => !present.has(panelId));
        if (missing.length) {
            const recovery = createStack(missing, missing[0], "stack-recovery");
            layout.root = layout.root ? createSplit("column", [layout.root, recovery], [0.84, 0.16]) : recovery;
        }

        return {
            version: LAYOUT_VERSION,
            root: normalizeLayoutTree(layout.root),
            floating,
            activePanelId: knownPanels.has(layout.activePanelId) ? layout.activePanelId : (Array.from(knownPanels)[0] || null),
        };
    }

    persistLayout() {
        if (!this.layout) {
            return;
        }
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
            version: LAYOUT_VERSION,
            root: this.layout.root,
            floating: this.layout.floating,
            activePanelId: this.activePanelId,
        }));
        if (typeof this.onLayoutChange === "function") {
            this.onLayoutChange(this.layout);
        }
    }

    bindPanelChrome(panelId, panel) {
        if (panel.dataset.dockManagerBound === "true") {
            return;
        }
        panel.dataset.dockManagerBound = "true";
        panel.classList.add("dock-panel-managed");
        this.ensureResizeHandles(panel);
        panel.addEventListener("pointerdown", () => this.focusPanel(panelId));
        const title = panel.querySelector(".panel-title");
        if (title) {
            title.setAttribute("draggable", "false");
            title.addEventListener("pointerdown", (event) => this.startDrag(panelId, event));
        }
        panel.querySelectorAll(".panel-resize").forEach((handle) => {
            handle.addEventListener("pointerdown", (event) => this.startResize(panelId, handle.dataset.resize, event));
        });
    }

    ensureResizeHandles(panel) {
        if (panel.querySelector(".panel-resize")) {
            return;
        }
        ["n", "s", "e", "w", "ne", "nw", "se", "sw"].forEach((direction) => {
            const handle = document.createElement("div");
            handle.className = `panel-resize panel-resize-${direction}`;
            handle.dataset.resize = direction;
            panel.appendChild(handle);
        });
    }

    render() {
        if (!this.rootEl || !this.layout) {
            return;
        }
        this.stackRefs.clear();
        this.splitRefs.clear();
        this.rootEl.replaceChildren();
        this.floatingLayer.replaceChildren();
        this.panels.forEach((panel) => {
            panel.hidden = true;
            panel.classList.remove("is-floating", "is-focused", "is-drag-origin");
            panel.style.position = "";
            panel.style.left = "";
            panel.style.top = "";
            panel.style.width = "";
            panel.style.height = "";
            panel.style.zIndex = "";
            panel.style.transform = "";
            panel.style.transition = "";
        });

        if (this.layout.root) {
            this.buildNode(this.layout.root, this.rootEl);
        }
        this.applyDockGeometry();
        this.renderFloatingPanels();
        this.applyFocusState();
    }

    buildNode(node, container) {
        if (node.type === "split") {
            const shell = document.createElement("div");
            shell.className = `dock-split dock-split-${node.axis}`;
            shell.dataset.splitId = node.id;
            withDefaultTransition(shell);
            container.appendChild(shell);
            const ref = { element: shell, node, splitters: [] };
            this.splitRefs.set(node.id, ref);
            node.children.forEach((child, index) => {
                this.buildNode(child, container);
                if (index < node.children.length - 1) {
                    const splitter = document.createElement("div");
                    splitter.className = `dock-splitter dock-splitter-${node.axis}`;
                    splitter.setAttribute("role", "separator");
                    splitter.setAttribute("aria-orientation", node.axis === "row" ? "vertical" : "horizontal");
                    splitter.dataset.splitId = node.id;
                    splitter.dataset.index = String(index);
                    splitter.addEventListener("pointerdown", (event) => this.startSplitResize(node.id, index, event));
                    withDefaultTransition(splitter);
                    container.appendChild(splitter);
                    ref.splitters.push(splitter);
                }
            });
            return;
        }

        const stack = document.createElement("div");
        stack.className = "dock-stack";
        stack.dataset.stackId = node.id;
        stack.dataset.panelCount = String(node.panels.length);
        withDefaultTransition(stack);

        const tabs = document.createElement("div");
        tabs.className = "dock-stack-tabs";
        if (node.panels.length <= 1) {
            tabs.classList.add("is-hidden");
        }
        node.panels.forEach((panelId) => {
            const tab = document.createElement("button");
            tab.type = "button";
            tab.className = `dock-tab${panelId === node.active ? " is-active" : ""}`;
            tab.textContent = this.panelTitles.get(panelId) || panelId;
            tab.addEventListener("click", () => {
                node.active = panelId;
                this.activePanelId = panelId;
                this.render();
                this.persistLayout();
            });
            tabs.appendChild(tab);
        });

        const body = document.createElement("div");
        body.className = "dock-stack-body";
        node.active = node.active || node.panels[0] || null;
        node.panels.forEach((panelId) => {
            const panel = this.panels.get(panelId);
            if (!panel) {
                return;
            }
            if (panel.parentElement && panel.parentElement !== body) {
                panel.parentElement.removeChild(panel);
            }
            panel.hidden = panelId !== node.active;
            body.appendChild(panel);
        });

        stack.append(tabs, body);
        container.appendChild(stack);
        this.stackRefs.set(node.id, { element: stack, body, node, rect: null });
    }

    applyDockGeometry() {
        if (!this.layout?.root || !this.workspace) {
            return;
        }
        const rect = {
            left: 0,
            top: 0,
            width: this.workspace.clientWidth,
            height: this.workspace.clientHeight,
        };
        this.layoutNode(this.layout.root, rect);
    }

    layoutNode(node, rect) {
        if (!node) {
            return;
        }
        if (node.type === "split") {
            const ref = this.splitRefs.get(node.id);
            if (ref) {
                setTransformRect(ref.element, rect);
            }
            const childRects = buildSplitRects(rect, node.axis, node.sizes);
            node.children.forEach((child, index) => this.layoutNode(child, childRects[index]));
            ref?.splitters.forEach((splitter, index) => {
                const firstRect = childRects[index];
                if (!firstRect) {
                    return;
                }
                if (node.axis === "row") {
                    setTransformRect(splitter, {
                        left: firstRect.left + firstRect.width + ((SPLIT_GAP - SPLITTER_SIZE) / 2),
                        top: rect.top,
                        width: SPLITTER_SIZE,
                        height: rect.height,
                    });
                } else {
                    setTransformRect(splitter, {
                        left: rect.left,
                        top: firstRect.top + firstRect.height + ((SPLIT_GAP - SPLITTER_SIZE) / 2),
                        width: rect.width,
                        height: SPLITTER_SIZE,
                    });
                }
            });
            return;
        }

        const ref = this.stackRefs.get(node.id);
        if (!ref) {
            return;
        }
        ref.rect = rect;
        setTransformRect(ref.element, rect);
        ref.element.dataset.activePanel = node.active || "";
        const tabs = ref.element.querySelector(".dock-stack-tabs");
        if (tabs) {
            tabs.classList.toggle("is-hidden", node.panels.length <= 1);
        }
    }

    renderFloatingPanels() {
        this.floatingLayer.replaceChildren();
        Object.entries(this.layout.floating || {}).forEach(([panelId, bounds]) => {
            const panel = this.panels.get(panelId);
            if (!panel) {
                return;
            }
            panel.hidden = false;
            panel.classList.add("is-floating");
            panel.style.position = "absolute";
            panel.style.left = "0";
            panel.style.top = "0";
            panel.style.width = `${Math.round(bounds.width)}px`;
            panel.style.height = `${Math.round(bounds.height)}px`;
            panel.style.transform = `translate3d(${Math.round(bounds.left)}px, ${Math.round(bounds.top)}px, 0)`;
            panel.style.zIndex = String(bounds.z || ++this.zCounter);
            withDefaultTransition(panel);
            this.floatingLayer.appendChild(panel);
        });
    }

    applyFocusState() {
        this.panels.forEach((panel) => panel.classList.remove("is-focused"));
        const panel = this.panels.get(this.activePanelId);
        if (!panel) {
            return;
        }
        panel.classList.add("is-focused");
        if (this.layout.floating?.[this.activePanelId]) {
            const z = ++this.zCounter;
            this.layout.floating[this.activePanelId].z = z;
            panel.style.zIndex = String(z);
        }
    }

    focusPanel(panelId) {
        if (!panelId) {
            return;
        }
        const location = findPanelLocation(this.layout.root, panelId);
        if (location?.stack) {
            location.stack.active = panelId;
        }
        this.activePanelId = panelId;
        this.render();
        this.persistLayout();
    }

    getPanelBounds(panelId) {
        const floating = this.layout.floating?.[panelId];
        if (floating) {
            return { ...floating };
        }
        const location = findPanelLocation(this.layout.root, panelId);
        if (location?.stack) {
            const ref = this.stackRefs.get(location.stack.id);
            if (ref?.rect) {
                return { ...ref.rect };
            }
        }
        return { left: 40, top: 40, ...DEFAULT_FLOAT };
    }

    createDragPreview(panelId, bounds) {
        const preview = document.createElement("div");
        preview.className = "panel-drag-preview";
        preview.innerHTML = `
            <div class="panel-drag-preview-head">${this.panelTitles.get(panelId) || panelId}</div>
            <div class="panel-drag-preview-body">Drop to dock, split, stack, or float.</div>
        `;
        preview.style.width = `${Math.round(bounds.width)}px`;
        preview.style.height = `${Math.round(bounds.height)}px`;
        this.dragLayer.replaceChildren(preview);
        return preview;
    }

    updatePreviewPosition(preview, bounds) {
        if (!preview) {
            return;
        }
        preview.style.width = `${Math.round(bounds.width)}px`;
        preview.style.height = `${Math.round(bounds.height)}px`;
        preview.style.transform = `translate3d(${Math.round(bounds.left)}px, ${Math.round(bounds.top)}px, 0) scale(0.97)`;
    }

    startDrag(panelId, event) {
        if (event.button !== undefined && event.button !== 0) {
            return;
        }
        const panel = this.panels.get(panelId);
        if (!panel || !this.workspace) {
            return;
        }
        event.preventDefault();
        event.stopPropagation();
        const pointerTarget = event.currentTarget;
        if (pointerTarget?.setPointerCapture && event.pointerId !== undefined) {
            pointerTarget.setPointerCapture(event.pointerId);
        }

        const sourceLocation = findPanelLocation(this.layout.root, panelId);
        const sourceStackId = sourceLocation?.stack?.id || null;
        const sourceStackSize = sourceLocation?.stack?.panels.length || 0;
        const wasFloating = Boolean(this.layout.floating?.[panelId]);
        const workspaceRect = this.workspace.getBoundingClientRect();
        const origin = this.getPanelBounds(panelId);
        const startPoint = this.getPoint(event);
        const offsetX = startPoint.x - workspaceRect.left - origin.left;
        const offsetY = startPoint.y - workspaceRect.top - origin.top;
        const preview = this.createDragPreview(panelId, origin);
        this.updatePreviewPosition(preview, origin);
        panel.classList.add("is-drag-origin");
        this.activePanelId = panelId;
        this.applyFocusState();

        this.dragState = {
            panelId,
            preview,
            sourceStackId,
            sourceStackSize,
            wasFloating,
        };

        const onMove = rafThrottle((moveEvent) => {
            const point = this.getPoint(moveEvent);
            const nextBounds = clampFloatingBounds({
                left: point.x - workspaceRect.left - offsetX,
                top: point.y - workspaceRect.top - offsetY,
                width: origin.width,
                height: origin.height,
                workspaceWidth: this.workspace.clientWidth,
                workspaceHeight: this.workspace.clientHeight,
                minWidth: MIN_FLOAT_WIDTH,
                minHeight: MIN_FLOAT_HEIGHT,
            });
            this.updatePreviewPosition(preview, nextBounds);
            this.previewTarget = this.resolveDropTarget(point.x, point.y, panelId);
            this.renderSnapPreview(this.previewTarget);
        });

        const finish = (upEvent) => {
            window.removeEventListener("pointermove", onMove);
            window.removeEventListener("pointerup", finish);
            if (pointerTarget?.releasePointerCapture && event.pointerId !== undefined) {
                try {
                    pointerTarget.releasePointerCapture(event.pointerId);
                } catch {
                    // ignored
                }
            }
            panel.classList.remove("is-drag-origin");
            this.dragLayer.replaceChildren();
            const point = this.getPoint(upEvent);
            const finalBounds = clampFloatingBounds({
                left: point.x - workspaceRect.left - offsetX,
                top: point.y - workspaceRect.top - offsetY,
                width: origin.width,
                height: origin.height,
                workspaceWidth: this.workspace.clientWidth,
                workspaceHeight: this.workspace.clientHeight,
                minWidth: MIN_FLOAT_WIDTH,
                minHeight: MIN_FLOAT_HEIGHT,
            });
            this.applyDrop(panelId, this.previewTarget, finalBounds, this.dragState);
            this.dragState = null;
            this.previewTarget = null;
            this.renderSnapPreview(null);
        };

        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", finish, { once: true });
    }

    startResize(panelId, direction, event) {
        const panel = this.panels.get(panelId);
        const floating = this.layout.floating?.[panelId];
        if (!panel || !floating) {
            return;
        }
        event.preventDefault();
        event.stopPropagation();
        const pointerTarget = event.currentTarget;
        if (pointerTarget?.setPointerCapture && event.pointerId !== undefined) {
            pointerTarget.setPointerCapture(event.pointerId);
        }
        this.activePanelId = panelId;
        this.applyFocusState();
        panel.classList.add("is-resizing");
        panel.style.transition = "none";

        const start = this.getPoint(event);
        const initial = { ...floating };
        const onMove = rafThrottle((moveEvent) => {
            const point = this.getPoint(moveEvent);
            const deltaX = point.x - start.x;
            const deltaY = point.y - start.y;
            let next = { ...initial };
            if (direction.includes("e")) {
                next.width = initial.width + deltaX;
            }
            if (direction.includes("s")) {
                next.height = initial.height + deltaY;
            }
            if (direction.includes("w")) {
                next.left = initial.left + deltaX;
                next.width = initial.width - deltaX;
            }
            if (direction.includes("n")) {
                next.top = initial.top + deltaY;
                next.height = initial.height - deltaY;
            }
            next = clampFloatingBounds({
                ...next,
                workspaceWidth: this.workspace.clientWidth,
                workspaceHeight: this.workspace.clientHeight,
                minWidth: MIN_FLOAT_WIDTH,
                minHeight: MIN_FLOAT_HEIGHT,
            });
            this.layout.floating[panelId] = { ...next, z: this.layout.floating[panelId].z || panel.style.zIndex || ++this.zCounter };
            panel.style.width = `${Math.round(next.width)}px`;
            panel.style.height = `${Math.round(next.height)}px`;
            panel.style.transform = `translate3d(${Math.round(next.left)}px, ${Math.round(next.top)}px, 0)`;
        });

        const finish = () => {
            window.removeEventListener("pointermove", onMove);
            window.removeEventListener("pointerup", finish);
            if (pointerTarget?.releasePointerCapture && event.pointerId !== undefined) {
                try {
                    pointerTarget.releasePointerCapture(event.pointerId);
                } catch {
                    // ignored
                }
            }
            panel.classList.remove("is-resizing");
            withDefaultTransition(panel);
            this.persistLayout();
        };

        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", finish, { once: true });
    }

    startSplitResize(splitId, index, event) {
        event.preventDefault();
        const ref = this.splitRefs.get(splitId);
        const target = findNodeById(this.layout.root, splitId);
        if (!ref || !target || target.node.type !== "split") {
            return;
        }
        const pointerTarget = event.currentTarget;
        if (pointerTarget?.setPointerCapture && event.pointerId !== undefined) {
            pointerTarget.setPointerCapture(event.pointerId);
        }
        const splitNode = target.node;
        const splitRect = ref.element.getBoundingClientRect();
        const start = this.getPoint(event);
        const axis = splitNode.axis;
        const sizePx = axis === "row" ? splitRect.width : splitRect.height;
        const total = splitNode.sizes[index] + splitNode.sizes[index + 1];
        const minRatio = Math.min(0.45, Math.max(0.08, 180 / Math.max(sizePx, 180)));

        const onMove = rafThrottle((moveEvent) => {
            const point = this.getPoint(moveEvent);
            const delta = axis === "row" ? point.x - start.x : point.y - start.y;
            let first = splitNode.sizes[index] + (delta / Math.max(sizePx, 1));
            first = clampValue(first, minRatio, total - minRatio);
            splitNode.sizes[index] = first;
            splitNode.sizes[index + 1] = total - first;
            this.applyDockGeometry();
        });

        const finish = () => {
            window.removeEventListener("pointermove", onMove);
            window.removeEventListener("pointerup", finish);
            if (pointerTarget?.releasePointerCapture && event.pointerId !== undefined) {
                try {
                    pointerTarget.releasePointerCapture(event.pointerId);
                } catch {
                    // ignored
                }
            }
            this.persistLayout();
        };

        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", finish, { once: true });
    }

    getPoint(event) {
        if (event.touches?.[0]) {
            return { x: event.touches[0].clientX, y: event.touches[0].clientY };
        }
        return { x: event.clientX, y: event.clientY };
    }

    resolveDropTarget(clientX, clientY, panelId) {
        const workspaceRect = this.workspace.getBoundingClientRect();
        if (clientX < workspaceRect.left || clientX > workspaceRect.right || clientY < workspaceRect.top || clientY > workspaceRect.bottom) {
            return null;
        }

        let best = null;
        this.stackRefs.forEach((ref, stackId) => {
            if (!ref?.rect) {
                return;
            }
            const zone = resolveStackDropZone(ref.rect, clientX, clientY);
            if (!zone) {
                return;
            }
            const sameStack = ref.node.panels.includes(panelId) && ref.node.panels.length === 1;
            if (sameStack && zone.zone === "center") {
                return;
            }
            const score = zone.distance + (zone.zone === "center" ? 4 : 0);
            if (!best || score < best.score) {
                best = { type: "stack", stackId, zone: zone.zone, rect: ref.rect, score };
            }
        });

        const leftEdge = clientX - workspaceRect.left;
        const rightEdge = workspaceRect.right - clientX;
        const topEdge = clientY - workspaceRect.top;
        const bottomEdge = workspaceRect.bottom - clientY;
        const rootMin = Math.min(leftEdge, rightEdge, topEdge, bottomEdge);
        if (rootMin <= ROOT_EDGE_THRESHOLD && (!best || best.score > ROOT_EDGE_THRESHOLD * 0.85)) {
            let edge = "left";
            if (rootMin === rightEdge) {
                edge = "right";
            } else if (rootMin === topEdge) {
                edge = "top";
            } else if (rootMin === bottomEdge) {
                edge = "bottom";
            }
            return { type: "root", edge, rect: workspaceRect, score: rootMin };
        }
        return best;
    }

    renderSnapPreview(target) {
        if (!target || !this.snapPreview) {
            this.snapPreview.hidden = true;
            this.snapPreview.classList.remove("is-visible");
            return;
        }
        const workspaceRect = this.workspace.getBoundingClientRect();
        let rect = null;
        if (target.type === "root") {
            if (target.edge === "left") {
                rect = { left: 0, top: 0, width: workspaceRect.width * 0.26, height: workspaceRect.height };
            } else if (target.edge === "right") {
                rect = { left: workspaceRect.width * 0.74, top: 0, width: workspaceRect.width * 0.26, height: workspaceRect.height };
            } else if (target.edge === "top") {
                rect = { left: 0, top: 0, width: workspaceRect.width, height: workspaceRect.height * 0.28 };
            } else {
                rect = { left: 0, top: workspaceRect.height * 0.72, width: workspaceRect.width, height: workspaceRect.height * 0.28 };
            }
        } else if (target.type === "stack") {
            const relative = {
                left: target.rect.left - workspaceRect.left,
                top: target.rect.top - workspaceRect.top,
                width: target.rect.width,
                height: target.rect.height,
            };
            if (target.zone === "center") {
                rect = { ...relative };
            } else if (target.zone === "left") {
                rect = { left: relative.left, top: relative.top, width: relative.width * 0.42, height: relative.height };
            } else if (target.zone === "right") {
                rect = { left: relative.left + (relative.width * 0.58), top: relative.top, width: relative.width * 0.42, height: relative.height };
            } else if (target.zone === "top") {
                rect = { left: relative.left, top: relative.top, width: relative.width, height: relative.height * 0.42 };
            } else {
                rect = { left: relative.left, top: relative.top + (relative.height * 0.58), width: relative.width, height: relative.height * 0.42 };
            }
        }

        if (!rect) {
            this.snapPreview.hidden = true;
            this.snapPreview.classList.remove("is-visible");
            return;
        }
        this.snapPreview.hidden = false;
        this.snapPreview.classList.add("is-visible");
        setTransformRect(this.snapPreview, rect);
    }

    applyDrop(panelId, target, fallbackBounds, dragState = null) {
        const sourceStackId = dragState?.sourceStackId || null;
        const sourceStackSize = dragState?.sourceStackSize || 0;
        const wasFloating = Boolean(dragState?.wasFloating);

        if (target?.type === "stack" && target.stackId === sourceStackId && target.zone === "center" && !wasFloating) {
            this.render();
            this.persistLayout();
            return;
        }
        if (target?.type === "stack" && target.stackId === sourceStackId && sourceStackSize <= 1 && !wasFloating && target.zone !== "center") {
            target = { type: "root", edge: target.zone };
        }

        let nextRoot = removePanelFromLayout(this.layout.root, panelId);
        delete this.layout.floating[panelId];

        if (!target) {
            this.layout.root = nextRoot;
            this.layout.floating[panelId] = { ...fallbackBounds, z: ++this.zCounter };
            this.activePanelId = panelId;
            this.render();
            this.persistLayout();
            return;
        }

        if (target.type === "root") {
            nextRoot = splitRootWithPanel(nextRoot, target.edge, panelId);
        } else {
            const targetExists = findNodeById(nextRoot, target.stackId);
            if (!targetExists) {
                if (target.zone === "center") {
                    this.layout.root = nextRoot;
                    this.layout.floating[panelId] = { ...fallbackBounds, z: ++this.zCounter };
                    this.activePanelId = panelId;
                    this.render();
                    this.persistLayout();
                    return;
                }
                nextRoot = splitRootWithPanel(nextRoot, target.zone, panelId);
            } else if (target.zone === "center") {
                nextRoot = insertPanelIntoStack(nextRoot, target.stackId, panelId);
            } else {
                nextRoot = splitStackInLayout(nextRoot, target.stackId, target.zone, panelId);
            }
        }

        this.layout.root = normalizeLayoutTree(nextRoot);
        this.activePanelId = panelId;
        this.render();
        this.persistLayout();
    }

    clampFloatingPanels() {
        let changed = false;
        Object.entries(this.layout.floating || {}).forEach(([panelId, bounds]) => {
            const clamped = clampFloatingBounds({
                ...bounds,
                workspaceWidth: Math.max(0, this.workspace.clientWidth - FLOAT_MARGIN),
                workspaceHeight: Math.max(0, this.workspace.clientHeight - FLOAT_MARGIN),
                minWidth: MIN_FLOAT_WIDTH,
                minHeight: MIN_FLOAT_HEIGHT,
            });
            if (
                clamped.left !== bounds.left
                || clamped.top !== bounds.top
                || clamped.width !== bounds.width
                || clamped.height !== bounds.height
            ) {
                this.layout.floating[panelId] = { ...bounds, ...clamped };
                changed = true;
            }
        });
        this.renderFloatingPanels();
        if (changed) {
            this.persistLayout();
        }
    }
}
