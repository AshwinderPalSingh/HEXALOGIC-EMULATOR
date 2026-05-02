import { Sim8051Client, SimulatorApiError } from "./sim8051-client.js";
import {
    MIN_CENTER_WIDTH,
    MIN_LEFT_WIDTH,
    MIN_PANEL_HEIGHT,
    MIN_RIGHT_WIDTH,
    PANEL_LAYOUT_KEY,
    resolveLeftColumnWidth,
    resolveRightColumnWidth,
    resolveVerticalSplit,
} from "./layout-utils.js";
import { animateLoaderProgress, pulseClass, rafThrottle } from "./animation-utils.js";

const client = new Sim8051Client({ baseUrl: window.HEXLOGIC_API_BASE || "/api/v2" });
/** Set to true for temporary hardware sync logging (devices + pins). */
const HEXLOGIC_HW_DEBUG = false;
window.DEBUG_TIMING = window.DEBUG_TIMING ?? true;
window.__HEXLOGIC_APP_READY__ = window.__HEXLOGIC_APP_READY__ ?? false;
const LOADER_MIN_MS = 1200;
const APP_BOOT_TS = Date.now();
const TRACE_LIMIT = 200;
const DEBUG_CONSOLE_MAX_LINES = 240;
const SMART_SCROLL_THRESHOLD_PX = 20;
const NEW_ENTRY_HIGHLIGHT_MS = 2000;
const RUN_FRAME_MAX_STEPS = 100000;
const UI_FRAME_BUDGET_MS = 1000 / 60;
const PORT_BASES = [0x80, 0x90, 0xA0, 0xB0];
const DEFAULT_HARDWARE_DEVICE_TYPES = [
    { type: "led", label: "LED", icon: "💡" },
    { type: "switch", label: "Switch", icon: "⏻" },
    { type: "led_array", label: "LED Array", icon: "▦" },
    { type: "seven_segment", label: "7-Segment", icon: "8" },
    { type: "stepper", label: "Stepper", icon: "⟲" },
];
const DEFAULT_SOURCE = {
    "8051": [
        "ORG 0000H",
        "MOV A,#01H",
        "MOV R0,#20H",
        "MOV @R0,A",
        "INC A",
        "MOV P1,A",
        "SJMP $",
        "END",
    ].join("\n"),
    arm: [
        "ORG 0000H",
        "MOV R0, #4",
        "MOV R1, #12",
        "ADD R2, R0, R1",
        "MOV R3, #0",
        "STR R2, [R3, #0]",
        "LDR R4, [R3, #0]",
        "B DONE",
        "DONE:",
        "MOV R5, R4",
        "END",
    ].join("\n"),
};

const appState = {
    architecture: "8051",
    endian: "little",
    executionMode: "realtime",
    debugMode: false,
    theme: localStorage.getItem("sim-theme") || "light",
    assembled: false,
    running: false,
    paused: false,
    monaco: null,
    editor: null,
    breakpointDecorations: [],
    executionDecorations: [],
    breakpoints: new Set(),
    listingByLine: new Map(),
    listingByAddress: new Map(),
    traceTimeline: [],
    waveformHistory: {},
    snapshot: null,
    lastMetricsFetchMs: 0,
    lastMetricsPayload: null,
    activeExecutionLine: null,
    workspaceMode: localStorage.getItem("sim-workspace") || "code",
    runtimeEventSource: null,
    signalEventSource: null,
    uiTiming: {
        pending: null,
        lastReceiveToPaintMs: null,
        lastSyncRenderMs: null,
        lastRoundTripMs: null,
        lastServerToPaintMs: null,
        lastFrameGapMs: null,
        droppedFrames: 0,
        maxReceiveToPaintMs: 0,
        samples: 0,
        lastChannel: "idle",
        lastServerGeneratedAtMs: null,
        lastFrameTs: null,
    },
};

const domMaps = {
    registers: new Map(),
    flags: new Map(),
    memory: new Map(),
    xram: new Map(),
    code: new Map(),
};

const panelRegistry = {
    panels: new Map(),
    elements: new Map(),
    missingWarnings: new Set(),
};

const uiState = {
    draggedPanel: null,
    activePanelId: null,
    zCounter: 10,
    floating: new Map(),
    dragSession: null,
    resizeSession: null,
    dockManager: null,
};

const debugConsoleState = {
    lastMessage: null,
    lastLevel: null,
    repeatCount: 0,
    lastNode: null,
};
const smartScrollControllers = new Map();

const HARDWARE_GRID = 20;
const HARDWARE_LAYOUT_KEY = "sim-hardware-layout-v2";
const HARDWARE_VIEW_MIN_SCALE = 0.6;
const HARDWARE_VIEW_MAX_SCALE = 1.5;
const HARDWARE_VIEW_MARGIN = 72;
const HARDWARE_MCU_VISIBILITY_INSET = 32;
const HARDWARE_STAGE_WIDTH = 2200;
const HARDWARE_STAGE_HEIGHT = 1400;
const MCU_DEFAULTS = {
    "8051": { width: 940, height: 760, y: 160 },
    arm: { width: 560, height: 420, y: 180 },
};
const savedHardwareLayout = (() => {
    try {
        return JSON.parse(localStorage.getItem(HARDWARE_LAYOUT_KEY) || "{}") || {};
    } catch {
        return {};
    }
})();
const hardwareState = {
    nodes: new Map(),
    connectionNodes: new Map(),
    signalNodes: new Map(),
    wirePaths: new Map(),
    occupancy: new Map(),
    suggestions: [],
    zCounter: 10,
    drag: null,
    pan: null,
    selection: null,
    connectionDrag: null,
    paletteDrag: null,
    hoverConnectorKey: null,
    rafId: 0,
    rafPending: null,
    runningTests: false,
    selectedDeviceId: null,
    selectedIds: new Set(),
    lastTestResult: null,
    localLayout: savedHardwareLayout,
    signalHistory: {},
    signalTokenOrder: [],
    seenSignalTokens: new Set(),
    pendingSignalStream: null,
    view: {
        scale: Number(savedHardwareLayout.__view?.scale || 1),
        panX: Number(savedHardwareLayout.__view?.panX || 32),
        panY: Number(savedHardwareLayout.__view?.panY || 32),
    },
    analyzer: {
        zoom: 1,
        offsetMs: 0,
        cursorX: null,
    },
    mcu: null,
    viewportFitRaf: 0,
    viewportNeedsFit: true,
    lastViewportArchitecture: null,
};

function byId(id) {
    return panelRegistry.elements.get(id) || document.getElementById(id);
}

function registerPanelElement(panel) {
    if (!panel) {
        return;
    }
    const panelId = panel.dataset?.panel;
    if (panelId) {
        panelRegistry.panels.set(panelId, panel);
    }
    if (panel.id) {
        panelRegistry.elements.set(panel.id, panel);
    }
    panel.querySelectorAll?.("[id]").forEach((node) => {
        panelRegistry.elements.set(node.id, node);
    });
}

function refreshPanelRegistry() {
    panelRegistry.panels.clear();
    panelRegistry.elements.clear();
    const panels = uiState.dockManager?.panels
        ? Array.from(uiState.dockManager.panels.values())
        : Array.from(document.querySelectorAll(".dock-panel"));
    panels.forEach((panel) => registerPanelElement(panel));
}

function warnMissingElement(id) {
    if (panelRegistry.missingWarnings.has(id)) {
        return;
    }
    panelRegistry.missingWarnings.add(id);
    console.warn(`Missing element: ${id}`);
}

function safeSetHTML(id, html) {
    const node = byId(id);
    if (!node) {
        warnMissingElement(id);
        return null;
    }
    node.innerHTML = html;
    return node;
}

function safeSetText(id, text) {
    const node = byId(id);
    if (!node) {
        warnMissingElement(id);
        return null;
    }
    node.textContent = text;
    return node;
}

function safeSetHidden(id, hidden) {
    const node = byId(id);
    if (!node) {
        warnMissingElement(id);
        return null;
    }
    node.hidden = hidden;
    return node;
}

function safeSetValue(id, value) {
    const node = byId(id);
    if (!node) {
        warnMissingElement(id);
        return null;
    }
    node.value = value;
    return node;
}

function safeSetChecked(id, checked) {
    const node = byId(id);
    if (!node) {
        warnMissingElement(id);
        return null;
    }
    node.checked = checked;
    return node;
}

function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
}

function safeInvoke(label, fn, fallback = null) {
    try {
        return fn();
    } catch (error) {
        console.error(`[HexaLogic] ${label} failed:`, error);
        return fallback;
    }
}

function isPlainObject(value) {
    return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function normalizeTraceEntry(entry, fallback = {}) {
    const source = isPlainObject(entry) ? entry : {};
    const fallbackObj = isPlainObject(fallback) ? fallback : {};
    const pc = Number(source.pc ?? fallbackObj.pc ?? 0);
    const cycles = Number(source.cycles ?? fallbackObj.cycles ?? 0);
    const opcode = Number(source.opcode ?? fallbackObj.opcode ?? 0);
    const mnemonic = String(source.mnemonic ?? fallbackObj.mnemonic ?? "UNKNOWN");
    const bytes = Array.isArray(source.bytes) ? source.bytes : (Array.isArray(source.bytes_) ? source.bytes_ : []);
    return {
        pc: Number.isFinite(pc) ? pc : 0,
        cycles: Number.isFinite(cycles) ? cycles : 0,
        opcode: Number.isFinite(opcode) ? opcode : 0,
        mnemonic,
        bytes: bytes.filter((b) => Number.isFinite(Number(b))).map((b) => Number(b) & 0xFF),
        line: source.line ?? fallbackObj.line ?? null,
        text: source.text ?? fallbackObj.text ?? null,
        register_diff: isPlainObject(source.register_diff) ? source.register_diff : {},
    };
}

function normalizeTraceEntries(entries, fallback = {}) {
    if (!Array.isArray(entries)) {
        return [];
    }
    return entries
        .filter((item) => item && typeof item === "object")
        .map((item) => normalizeTraceEntry(item, fallback))
        .filter((item) => typeof item.mnemonic === "string");
}

function formatTraceLogEntry(entry, pcWidth = 4) {
    const trace = normalizeTraceEntry(entry);
    return `${trace.mnemonic} @ ${toHex(trace.pc, pcWidth)}`;
}

function formatRunSummary(stepCount, droppedSteps, lastStep, pcWidth = 4) {
    const count = Number(stepCount || 0);
    const dropped = Number(droppedSteps || 0);
    const summary = `Run: ${count} instructions${dropped ? ` (${dropped} summarized)` : ""}`;
    if (!lastStep) {
        return summary;
    }
    const trace = normalizeTraceEntry(lastStep);
    return `${summary}, last ${trace.mnemonic} @ ${toHex(trace.pc, pcWidth)}`;
}

function toHex(value, width = 2) {
    return `0x${Number(value || 0).toString(16).toUpperCase().padStart(width, "0")}`;
}

function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function nextAnimationFrame() {
    return new Promise((resolve) => window.requestAnimationFrame(resolve));
}

function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
}

function getRunSpeedMultiplier() {
    return clamp(Number(byId("run_speed")?.value || 1), 1, 10);
}

function updateRunSpeedLabel() {
    safeSetText("speed_label", `${Math.round(getRunSpeedMultiplier())}x`);
}

function getClockHzInput() {
    const value = Number(byId("clock_input")?.value || 0);
    return Number.isFinite(value) ? Math.max(1, Math.trunc(value)) : 0;
}

function syncClockPreset(clockHz) {
    const preset = byId("clock_preset");
    if (!preset) {
        return;
    }
    const normalized = String(Math.max(1, Math.trunc(Number(clockHz || 0))));
    const hasPreset = Array.from(preset.options || []).some((option) => option.value === normalized);
    preset.value = hasPreset ? normalized : "custom";
}

function applyClockControlValue(clockHz) {
    const normalized = Math.max(1, Math.trunc(Number(clockHz || 0)));
    safeSetValue("clock_input", String(normalized));
    syncClockPreset(normalized);
}

function _effectiveExecutionHz(architecture, clockHz, speedMultiplier) {
    const scaledClock = Math.max(1, Number(clockHz || 0)) * Math.max(0.1, Number(speedMultiplier || 1));
    return architecture === "8051" ? (scaledClock / 12) : scaledClock;
}

function setWorkspaceMode(mode) {
    appState.workspaceMode = mode;
    localStorage.setItem("sim-workspace", mode);
    const root = document.getElementById("ide-root");
    if (root) {
        root.dataset.workspace = mode;
    }
    console.log("Workspace:", mode);
    document.querySelectorAll(".workspace-tab").forEach((btn) => {
        const tabMode = btn.getAttribute("data-mode") || btn.dataset.workspace;
        btn.classList.toggle("is-active", tabMode === mode);
    });
    if (mode === "hardware") {
        hardwareState.viewportNeedsFit = true;
        renderHardware(appState.snapshot);
        scheduleHardwareViewportEnsure({ force: true, reason: "workspace-open" });
    }
}

/**
 * Code | Hardware toggle must never depend on #hardware-canvas existing.
 * Delegated click on #ide-root so clicks still work if direct listeners fail.
 */
function bindWorkspaceModeControls() {
    const root = document.getElementById("ide-root");
    if (!root) {
        console.warn("bindWorkspaceModeControls: #ide-root not found");
        return;
    }
    root.addEventListener("click", (event) => {
        const btn = event.target.closest(".workspace-tab[data-mode]");
        if (!btn || !root.contains(btn)) {
            return;
        }
        const m = btn.getAttribute("data-mode");
        if (m !== "code" && m !== "hardware") {
            return;
        }
        event.preventDefault();
        setWorkspaceMode(m);
    });
    ["workspace_code", "workspace_hardware"].forEach((id) => {
        const el = document.getElementById(id);
        if (el) {
            el.disabled = false;
        }
    });
}

function buildConnectionSelect(device, conn, catalog, hw) {
    const current = device.connections?.[conn.key] ?? "";
    const signalState = current ? (_signalState(hw, current) || {}) : {};
    const status = signalState.contention ? "is-error" : ((signalState.floating || signalState.fault) ? "is-warning" : "");
    const optionGroup = conn.kind === "bus8" ? "bus8" : conn.kind === "bus4" ? "bus4" : "pins";
    const choices = Array.isArray(catalog?.[optionGroup]) ? catalog[optionGroup] : [];
    const options = choices.map((choice) => {
        const choiceId = String(choice?.id || "");
        const choiceLabel = String(choice?.label || choiceId || "");
        return `<option value="${escapeHtml(choiceId)}"${choiceId === current ? " selected" : ""}>${escapeHtml(choiceLabel)}</option>`;
    }).join("");
    const stateLabel = current
        ? `${current} • ${signalState.contention ? "contention" : (signalState.floating ? "floating" : (signalState.state || (signalState.level ? "high" : "low")))}`
        : "Unbound";
    return `
        <label class="vh-conn-summary ${status}">
            <span class="vh-conn-name">${escapeHtml(conn.label)}</span>
            <select class="vh-conn" data-device-id="${escapeHtml(device.id)}" data-conn="${escapeHtml(conn.key)}">
                <option value="">Unbound</option>
                ${options}
            </select>
            <span class="vh-conn-value">${escapeHtml(stateLabel)}</span>
        </label>
    `;
}

function _snap(value) {
    return Math.round(Number(value || 0) / HARDWARE_GRID) * HARDWARE_GRID;
}

function _clampDeviceToCanvas(canvas, x, y, width, height) {
    const maxX = Math.max(0, (canvas.offsetWidth || canvas.clientWidth || 0) - width);
    const maxY = Math.max(0, (canvas.offsetHeight || canvas.clientHeight || 0) - height);
    return {
        x: clamp(x, 0, maxX),
        y: clamp(y, 0, maxY),
    };
}

function _rectOverlap(a, b) {
    const left = Math.max(a.x, b.x);
    const right = Math.min(a.x + a.width, b.x + b.width);
    const top = Math.max(a.y, b.y);
    const bottom = Math.min(a.y + a.height, b.y + b.height);
    return right > left && bottom > top;
}

function _resolveCollision(canvas, deviceId, x, y, width, height) {
    let nx = x;
    let ny = y;
    const tries = 24;
    for (let i = 0; i < tries; i += 1) {
        const hit = _occupancyHit(deviceId, nx, ny, width, height);
        if (!hit) {
            return { x: nx, y: ny };
        }
        nx += HARDWARE_GRID;
        ny += (i % 2) ? HARDWARE_GRID : 0;
        const clamped = _clampDeviceToCanvas(canvas, nx, ny, width, height);
        nx = clamped.x;
        ny = clamped.y;
    }
    return { x, y };
}

function _saveHardwareLayoutLocal() {
    hardwareState.localLayout.__view = {
        scale: hardwareState.view.scale,
        panX: hardwareState.view.panX,
        panY: hardwareState.view.panY,
    };
    localStorage.setItem(HARDWARE_LAYOUT_KEY, JSON.stringify(hardwareState.localLayout));
}

function getHardwareViewport() {
    return byId("vh-board-viewport");
}

function getHardwareStage() {
    return byId("hardware-stage");
}

function getHardwareCanvas() {
    return byId("hardware-canvas");
}

function getHardwareWireLayer() {
    return byId("vh-wire-layer");
}

function createDefaultMcuModel(architecture = appState.architecture) {
    const normalized = architecture === "arm" ? "arm" : "8051";
    const defaults = MCU_DEFAULTS[normalized] || MCU_DEFAULTS["8051"];
    return {
        id: "mcu_main",
        type: normalized,
        x: Math.round((HARDWARE_STAGE_WIDTH - defaults.width) / 2),
        y: defaults.y,
        width: defaults.width,
        height: defaults.height,
        locked: true,
    };
}

function _currentMcuModel() {
    if (!hardwareState.mcu || hardwareState.mcu.type !== appState.architecture) {
        hardwareState.mcu = createDefaultMcuModel(appState.architecture);
    }
    return hardwareState.mcu;
}

function ensureMcuBoardNode() {
    const stage = getHardwareStage();
    if (!stage) {
        return null;
    }
    let node = byId("vh-mcu-board");
    if (!node) {
        node = document.createElement("div");
        node.id = "vh-mcu-board";
        node.className = "vh-mcu-board";
        const canvas = getHardwareCanvas();
        if (canvas?.parentElement === stage) {
            stage.insertBefore(node, canvas);
        } else {
            stage.appendChild(node);
        }
        registerPanelElement(node);
    }
    return node;
}

function _hardwareViewScale() {
    const scale = Number(hardwareState.view.scale);
    return Number.isFinite(scale) && scale > 0 ? scale : 1;
}

function _mcuBoardRect() {
    const node = ensureMcuBoardNode();
    const stage = getHardwareStage();
    if (!node || !stage) {
        return null;
    }
    const rect = node.getBoundingClientRect();
    const stageRect = stage.getBoundingClientRect();
    const scale = _hardwareViewScale();
    if (rect.width > 0 && rect.height > 0 && stageRect.width > 0 && stageRect.height > 0) {
        return {
            x: (rect.left - stageRect.left) / scale,
            y: (rect.top - stageRect.top) / scale,
            width: rect.width / scale,
            height: rect.height / scale,
        };
    }
    const model = _currentMcuModel();
    const width = node.offsetWidth || model.width || 940;
    const height = node.offsetHeight || model.height || 760;
    const stageWidth = stage.offsetWidth || HARDWARE_STAGE_WIDTH;
    return {
        x: Number.isFinite(model.x) ? model.x : Math.round((stageWidth - width) / 2),
        y: Number.isFinite(model.y) ? model.y : 88,
        width,
        height,
    };
}

function _hardwareContentBounds() {
    const mcu = _mcuBoardRect();
    if (!mcu) {
        return null;
    }
    const rects = [mcu];
    hardwareState.nodes.forEach((node) => {
        const rect = getBoardRect(node);
        if (rect.width > 0 && rect.height > 0) {
            rects.push(rect);
        }
    });
    const left = Math.min(...rects.map((rect) => rect.x));
    const top = Math.min(...rects.map((rect) => rect.y));
    const right = Math.max(...rects.map((rect) => rect.x + rect.width));
    const bottom = Math.max(...rects.map((rect) => rect.y + rect.height));
    return {
        mcu,
        hasDevices: rects.length > 1,
        x: left,
        y: top,
        width: Math.max(1, right - left),
        height: Math.max(1, bottom - top),
    };
}

function _clampHardwarePan(panX, panY, scale, bounds = null) {
    const viewport = getHardwareViewport();
    if (!viewport) {
        return { panX, panY };
    }
    const activeBounds = bounds || _hardwareContentBounds();
    if (!activeBounds) {
        return { panX, panY };
    }
    const margin = HARDWARE_VIEW_MARGIN;
    const minPanX = viewport.clientWidth - ((activeBounds.x + activeBounds.width + margin) * scale);
    const maxPanX = margin - (activeBounds.x * scale);
    const minPanY = viewport.clientHeight - ((activeBounds.y + activeBounds.height + margin) * scale);
    const maxPanY = margin - (activeBounds.y * scale);
    let nextPanX = clamp(panX, minPanX, maxPanX);
    let nextPanY = clamp(panY, minPanY, maxPanY);
    const mcu = activeBounds.mcu;
    if (mcu) {
        const inset = HARDWARE_MCU_VISIBILITY_INSET;
        const mcuWidth = mcu.width * scale;
        const mcuHeight = mcu.height * scale;
        if (mcuWidth >= (viewport.clientWidth - (inset * 2))) {
            nextPanX = (viewport.clientWidth / 2) - ((mcu.x + (mcu.width / 2)) * scale);
        } else {
            const mcuMinPanX = inset - (mcu.x * scale);
            const mcuMaxPanX = (viewport.clientWidth - inset) - ((mcu.x + mcu.width) * scale);
            nextPanX = clamp(nextPanX, mcuMinPanX, mcuMaxPanX);
        }
        if (mcuHeight >= (viewport.clientHeight - (inset * 2))) {
            nextPanY = (viewport.clientHeight / 2) - ((mcu.y + (mcu.height / 2)) * scale);
        } else {
            const mcuMinPanY = inset - (mcu.y * scale);
            const mcuMaxPanY = (viewport.clientHeight - inset) - ((mcu.y + mcu.height) * scale);
            nextPanY = clamp(nextPanY, mcuMinPanY, mcuMaxPanY);
        }
    }
    return { panX: nextPanX, panY: nextPanY };
}

function _isMcuVisible() {
    const viewport = getHardwareViewport();
    const bounds = _hardwareContentBounds();
    if (!viewport || !bounds?.mcu) {
        return false;
    }
    const scale = _hardwareViewScale();
    const left = (bounds.mcu.x * scale) + hardwareState.view.panX;
    const top = (bounds.mcu.y * scale) + hardwareState.view.panY;
    const right = left + (bounds.mcu.width * scale);
    const bottom = top + (bounds.mcu.height * scale);
    return right > 32 && bottom > 32 && left < (viewport.clientWidth - 32) && top < (viewport.clientHeight - 32);
}

function fitHardwareViewport({ includeDevices = true, reason = "auto" } = {}) {
    const viewport = getHardwareViewport();
    const stage = getHardwareStage();
    if (!viewport || !stage || viewport.clientWidth <= 0 || viewport.clientHeight <= 0) {
        return false;
    }
    const bounds = _hardwareContentBounds();
    if (!bounds) {
        return false;
    }
    const target = includeDevices && bounds.hasDevices ? bounds : bounds.mcu;
    const availableWidth = Math.max(1, viewport.clientWidth - (HARDWARE_VIEW_MARGIN * 2));
    const availableHeight = Math.max(1, viewport.clientHeight - (HARDWARE_VIEW_MARGIN * 2));
    const fitScale = Math.min(availableWidth / target.width, availableHeight / target.height);
    const scale = clamp(includeDevices && bounds.hasDevices ? fitScale : Math.min(1, fitScale), HARDWARE_VIEW_MIN_SCALE, HARDWARE_VIEW_MAX_SCALE);
    const centerX = target.x + (target.width / 2);
    const centerY = target.y + (target.height / 2);
    const unclampedPanX = (viewport.clientWidth / 2) - (centerX * scale);
    const unclampedPanY = (viewport.clientHeight / 2) - (centerY * scale);
    const clampedPan = _clampHardwarePan(unclampedPanX, unclampedPanY, scale, bounds);
    hardwareState.view.scale = scale;
    hardwareState.view.panX = clampedPan.panX;
    hardwareState.view.panY = clampedPan.panY;
    hardwareState.viewportNeedsFit = false;
    hardwareState.lastViewportArchitecture = appState.architecture;
    if (HEXLOGIC_HW_DEBUG) {
        console.log("Hardware viewport fit", {
            reason,
            scale,
            panX: hardwareState.view.panX,
            panY: hardwareState.view.panY,
            mcu: bounds.mcu,
            bounds,
        });
    }
    applyHardwareBoardView();
    return true;
}

function _ensureHardwareViewport({ force = false, reason = "auto" } = {}) {
    if (appState.workspaceMode !== "hardware") {
        return;
    }
    const viewport = getHardwareViewport();
    if (!viewport || viewport.clientWidth <= 0 || viewport.clientHeight <= 0) {
        return;
    }
    const scale = Number(hardwareState.view.scale);
    const invalidView = !Number.isFinite(scale)
        || scale < HARDWARE_VIEW_MIN_SCALE
        || scale > HARDWARE_VIEW_MAX_SCALE
        || !Number.isFinite(hardwareState.view.panX)
        || !Number.isFinite(hardwareState.view.panY);
    const architectureChanged = hardwareState.lastViewportArchitecture !== appState.architecture;
    if (force || invalidView || architectureChanged || hardwareState.viewportNeedsFit) {
        fitHardwareViewport({ includeDevices: true, reason });
        return;
    }
    if (!_isMcuVisible()) {
        const bounds = _hardwareContentBounds();
        if (!bounds?.mcu) {
            return;
        }
        const scaleToUse = clamp(_hardwareViewScale(), HARDWARE_VIEW_MIN_SCALE, HARDWARE_VIEW_MAX_SCALE);
        const centerX = bounds.mcu.x + (bounds.mcu.width / 2);
        const centerY = bounds.mcu.y + (bounds.mcu.height / 2);
        const unclampedPanX = (viewport.clientWidth / 2) - (centerX * scaleToUse);
        const unclampedPanY = (viewport.clientHeight / 2) - (centerY * scaleToUse);
        const clampedPan = _clampHardwarePan(unclampedPanX, unclampedPanY, scaleToUse, bounds);
        hardwareState.view.scale = scaleToUse;
        hardwareState.view.panX = clampedPan.panX;
        hardwareState.view.panY = clampedPan.panY;
        if (window.DEBUG_MCU || HEXLOGIC_HW_DEBUG) {
            console.log("Hardware viewport pan-corrected", {
                reason,
                scale: scaleToUse,
                panX: hardwareState.view.panX,
                panY: hardwareState.view.panY,
                mcu: bounds.mcu,
            });
        }
        applyHardwareBoardView();
    }
}

function scheduleHardwareViewportEnsure(options = {}) {
    if (hardwareState.viewportFitRaf) {
        window.cancelAnimationFrame(hardwareState.viewportFitRaf);
    }
    hardwareState.viewportFitRaf = window.requestAnimationFrame(() => {
        hardwareState.viewportFitRaf = 0;
        _ensureHardwareViewport(options);
    });
}

function applyHardwareBoardView() {
    const stage = getHardwareStage();
    if (!stage) {
        return;
    }
    hardwareState.view.scale = clamp(_hardwareViewScale(), HARDWARE_VIEW_MIN_SCALE, HARDWARE_VIEW_MAX_SCALE);
    const clampedPan = _clampHardwarePan(hardwareState.view.panX, hardwareState.view.panY, hardwareState.view.scale);
    hardwareState.view.panX = clampedPan.panX;
    hardwareState.view.panY = clampedPan.panY;
    stage.style.transform = `translate3d(${hardwareState.view.panX}px, ${hardwareState.view.panY}px, 0) scale(${hardwareState.view.scale})`;
    safeSetText("vh_zoom_label", `${Math.round(hardwareState.view.scale * 100)}%`);
    _saveHardwareLayoutLocal();
    scheduleHardwareWireRender();
}

function clientToHardwarePoint(event) {
    const viewport = getHardwareViewport();
    if (!viewport) {
        return { x: 0, y: 0 };
    }
    const point = getClientPoint(event);
    const rect = viewport.getBoundingClientRect();
    return {
        x: (point.x - rect.left - hardwareState.view.panX) / hardwareState.view.scale,
        y: (point.y - rect.top - hardwareState.view.panY) / hardwareState.view.scale,
    };
}

function getBoardRect(node) {
    if (!node) {
        return { x: 0, y: 0, width: 0, height: 0 };
    }
    return {
        x: Number.parseFloat(node.style.left || "0") || 0,
        y: Number.parseFloat(node.style.top || "0") || 0,
        width: node.offsetWidth || node.getBoundingClientRect().width || 0,
        height: node.offsetHeight || node.getBoundingClientRect().height || 0,
    };
}

function _gridCellKey(col, row) {
    return `${col}:${row}`;
}

function _gridRange(x, y, width, height) {
    return {
        left: Math.floor(x / HARDWARE_GRID),
        top: Math.floor(y / HARDWARE_GRID),
        right: Math.max(0, Math.ceil((x + width) / HARDWARE_GRID) - 1),
        bottom: Math.max(0, Math.ceil((y + height) / HARDWARE_GRID) - 1),
    };
}

function rebuildHardwareOccupancy() {
    hardwareState.occupancy.clear();
    hardwareState.nodes.forEach((node, id) => {
        const rect = getBoardRect(node);
        const cells = _gridRange(rect.x, rect.y, rect.width, rect.height);
        for (let row = cells.top; row <= cells.bottom; row += 1) {
            for (let col = cells.left; col <= cells.right; col += 1) {
                hardwareState.occupancy.set(_gridCellKey(col, row), id);
            }
        }
    });
}

function _occupancyHit(deviceId, x, y, width, height) {
    const cells = _gridRange(x, y, width, height);
    for (let row = cells.top; row <= cells.bottom; row += 1) {
        for (let col = cells.left; col <= cells.right; col += 1) {
            const occupant = hardwareState.occupancy.get(_gridCellKey(col, row));
            if (occupant && occupant !== deviceId) {
                return true;
            }
        }
    }
    return false;
}

function _highlightConnector(key) {
    if (hardwareState.hoverConnectorKey === key) {
        return;
    }
    const previous = hardwareState.hoverConnectorKey;
    hardwareState.hoverConnectorKey = key;
    if (previous) {
        const [prevKind, ...prevRest] = previous.split(":");
        const prevId = prevRest.join(":");
        const prevNode = prevKind === "device"
            ? hardwareState.connectionNodes.get(prevId)
            : hardwareState.signalNodes.get(previous);
        prevNode?.classList.remove("is-hover");
    }
    if (!key) {
        return;
    }
    const [kind, ...rest] = key.split(":");
    const node = kind === "device"
        ? hardwareState.connectionNodes.get(rest.join(":"))
        : hardwareState.signalNodes.get(key);
    node?.classList.add("is-hover");
}

function _selectionRect() {
    const selection = hardwareState.selection;
    if (!selection) {
        return null;
    }
    const left = Math.min(selection.startPoint.x, selection.currentPoint.x);
    const top = Math.min(selection.startPoint.y, selection.currentPoint.y);
    const right = Math.max(selection.startPoint.x, selection.currentPoint.x);
    const bottom = Math.max(selection.startPoint.y, selection.currentPoint.y);
    return {
        x: left,
        y: top,
        width: right - left,
        height: bottom - top,
    };
}

function _renderSelectionBox() {
    const node = byId("vh-selection-box");
    const rect = _selectionRect();
    if (!node || !rect || (rect.width < 2 && rect.height < 2)) {
        node?.setAttribute("hidden", "hidden");
        return;
    }
    node.removeAttribute("hidden");
    node.style.left = `${rect.x}px`;
    node.style.top = `${rect.y}px`;
    node.style.width = `${rect.width}px`;
    node.style.height = `${rect.height}px`;
}

function _clearSelectionBox() {
    byId("vh-selection-box")?.setAttribute("hidden", "hidden");
}

function _selectedIdsInRect(rect) {
    const selected = [];
    hardwareState.nodes.forEach((node, id) => {
        if (_rectOverlap(rect, getBoardRect(node))) {
            selected.push(id);
        }
    });
    return selected;
}

function _nearestCompatibleConnector(point) {
    const drag = hardwareState.connectionDrag;
    if (!drag) {
        return null;
    }
    return findNearestConnector(point, (descriptor) => {
        if (!descriptor) {
            return false;
        }
        if (drag.sourceType === "signal") {
            return descriptor.type === "device" && ["pin", "bus8", "bus4"].includes(descriptor.kind);
        }
        return descriptor.type === "signal" && descriptor.kind === "pin";
    }, 18);
}

function findNearestConnector(point, predicate, radius = 16) {
    const stage = getHardwareStage();
    if (!stage) {
        return null;
    }
    let best = null;
    const inspectNode = (node, descriptor) => {
        if (!node || !predicate(descriptor)) {
            return;
        }
        const center = _connectorCenter(node, stage);
        const distance = Math.hypot(center.x - point.x, center.y - point.y);
        if (distance > radius) {
            return;
        }
        if (!best || distance < best.distance) {
            best = { ...descriptor, node, distance, center };
        }
    };
    hardwareState.signalNodes.forEach((node, key) => {
        inspectNode(node, { type: "signal", key, signalId: node.dataset.signalId, kind: node.dataset.signalKind });
    });
    hardwareState.connectionNodes.forEach((node, key) => {
        inspectNode(node, { type: "device", key: `device:${key}`, deviceId: node.dataset.deviceId, connectionKey: node.dataset.connectionKey, kind: node.dataset.connectionKind });
    });
    return best;
}

function _selectionNodes() {
    return Array.from(hardwareState.selectedIds)
        .map((id) => hardwareState.nodes.get(id))
        .filter(Boolean);
}

function _updateSelectionVisuals() {
    hardwareState.nodes.forEach((node, id) => {
        node.classList.toggle("is-selected", hardwareState.selectedIds.has(id));
        node.classList.toggle("is-primary", id === hardwareState.selectedDeviceId);
    });
}

function _setSelectedDevices(ids, primary = null) {
    hardwareState.selectedIds = new Set(ids.filter(Boolean));
    hardwareState.selectedDeviceId = primary || Array.from(hardwareState.selectedIds)[0] || null;
    _updateSelectionVisuals();
    if (appState.snapshot?.hardware) {
        _renderHardwareDebugPanel(appState.snapshot.hardware);
    }
}

function _toggleSelectedDevice(id) {
    const next = new Set(hardwareState.selectedIds);
    if (next.has(id)) {
        next.delete(id);
    } else {
        next.add(id);
    }
    _setSelectedDevices(Array.from(next), id);
}

function _signalState(hw, signalId) {
    return hw?.debug?.signals?.[signalId] || null;
}

async function _saveDeviceLayoutRemote(deviceId, layout) {
    try {
        await client.hardwareUpdateDevice({
            id: deviceId,
            position: { x: layout.x, y: layout.y },
            settings: {
                layout: {
                    width: layout.width,
                    height: layout.height,
                    zIndex: layout.zIndex,
                    props: layout.props || {},
                },
            },
        });
    } catch (error) {
        if (HEXLOGIC_HW_DEBUG) {
            console.warn("save layout failed", deviceId, error);
        }
    }
}

function _nextHardwareZ() {
    hardwareState.zCounter += 1;
    return hardwareState.zCounter;
}

function _setDeviceZ(deviceId, node, zIndex) {
    node.style.zIndex = String(zIndex);
    const entry = hardwareState.localLayout[deviceId] || {};
    entry.zIndex = zIndex;
    hardwareState.localLayout[deviceId] = entry;
    _saveHardwareLayoutLocal();
    scheduleHardwareWireRender();
}

function _bindDrag(node) {
    if (node.dataset.dragBound === "true") {
        return;
    }
    node.dataset.dragBound = "true";
    node.addEventListener("pointerdown", (event) => {
        const canvas = document.getElementById("hardware-canvas");
        if (!canvas || event.button !== 0) {
            return;
        }
        if (event.target.closest("select,button,input,label,.vh-port-node,.vh-signal-node")) {
            return;
        }
        const deviceId = node.dataset.deviceId;
        if (event.metaKey || event.ctrlKey) {
            _toggleSelectedDevice(deviceId);
        } else if (!hardwareState.selectedIds.has(deviceId)) {
            _setSelectedDevices([deviceId], deviceId);
        } else if (!hardwareState.selectedDeviceId) {
            hardwareState.selectedDeviceId = deviceId;
        }
        const rect = getBoardRect(node);
        const pointer = clientToHardwarePoint(event);
        const z = _nextHardwareZ();
        _setDeviceZ(deviceId, node, z);
        const selectedNodes = _selectionNodes();
        const dragNodes = selectedNodes.length && selectedNodes.includes(node) ? selectedNodes : [node];
        hardwareState.drag = {
            node,
            canvas,
            pointerId: event.pointerId,
            startOffsetX: pointer.x - rect.x,
            startOffsetY: pointer.y - rect.y,
            width: rect.width,
            height: rect.height,
            latestX: rect.x,
            latestY: rect.y,
            items: dragNodes.map((item) => {
                const itemRect = getBoardRect(item);
                return {
                    id: item.dataset.deviceId,
                    node: item,
                    offsetX: itemRect.x - rect.x,
                    offsetY: itemRect.y - rect.y,
                    width: itemRect.width,
                    height: itemRect.height,
                };
            }),
        };
        node.classList.add("is-dragging");
        if (typeof node.setPointerCapture === "function" && event.pointerId != null) {
            node.setPointerCapture(event.pointerId);
        }
    });
}

function _dragFrame() {
    hardwareState.rafId = 0;
    if (!hardwareState.drag || !hardwareState.rafPending) {
        return;
    }
    const drag = hardwareState.drag;
    const pointer = hardwareState.rafPending;
    hardwareState.rafPending = null;
    const x = pointer.x - drag.startOffsetX;
    const y = pointer.y - drag.startOffsetY;
    const clamped = _clampDeviceToCanvas(drag.canvas, x, y, drag.width, drag.height);
    drag.latestX = clamped.x;
    drag.latestY = clamped.y;
    drag.items.forEach((item) => {
        const itemPos = _clampDeviceToCanvas(drag.canvas, clamped.x + item.offsetX, clamped.y + item.offsetY, item.width, item.height);
        item.node.style.left = `${itemPos.x}px`;
        item.node.style.top = `${itemPos.y}px`;
    });
    scheduleHardwareWireRender();
}

function _bindDragGlobal() {
    if (document.body.dataset.hwDragGlobalBound === "true") {
        return;
    }
    document.body.dataset.hwDragGlobalBound = "true";
    document.addEventListener("pointermove", (event) => {
        if (!hardwareState.drag) {
            return;
        }
        hardwareState.rafPending = clientToHardwarePoint(event);
        if (!hardwareState.rafId) {
            hardwareState.rafId = window.requestAnimationFrame(_dragFrame);
        }
    });
    document.addEventListener("pointerup", async (event) => {
        const drag = hardwareState.drag;
        if (!drag) {
            return;
        }
        if (drag.pointerId != null && event.pointerId != null && drag.pointerId !== event.pointerId) {
            return;
        }
        const snapped = {
            x: _snap(drag.latestX),
            y: _snap(drag.latestY),
        };
        const clamped = _clampDeviceToCanvas(drag.canvas, snapped.x, snapped.y, drag.width, drag.height);
        const finalPos = _resolveCollision(drag.canvas, drag.node.dataset.deviceId, clamped.x, clamped.y, drag.width, drag.height);
        const remoteSaves = [];
        drag.items.forEach((item, index) => {
            const desiredX = index === 0 ? finalPos.x : _snap(finalPos.x + item.offsetX);
            const desiredY = index === 0 ? finalPos.y : _snap(finalPos.y + item.offsetY);
            const itemClamped = _clampDeviceToCanvas(drag.canvas, desiredX, desiredY, item.width, item.height);
            item.node.style.left = `${itemClamped.x}px`;
            item.node.style.top = `${itemClamped.y}px`;
            item.node.classList.remove("is-dragging");
            const entry = hardwareState.localLayout[item.id] || {};
            const layout = {
                id: item.id,
                type: item.node.dataset.deviceType || entry.type || "unknown",
                x: itemClamped.x,
                y: itemClamped.y,
                width: item.width,
                height: item.height,
                zIndex: Number(item.node.style.zIndex || entry.zIndex || 1),
                props: entry.props || {},
            };
            hardwareState.localLayout[item.id] = layout;
            remoteSaves.push(_saveDeviceLayoutRemote(item.id, layout));
        });
        _saveHardwareLayoutLocal();
        hardwareState.drag = null;
        rebuildHardwareOccupancy();
        await Promise.all(remoteSaves);
    });
}

function _findNewHardwareDeviceId(response, previousIds, kind) {
    if (response?.created_device_id) {
        return response.created_device_id;
    }
    const devices = response?.hardware?.devices || [];
    const created = devices.find((device) => !previousIds.has(device.id) && (!kind || device.type === kind));
    return created?.id || null;
}

function _hardwareDevices(snapshot = appState.snapshot) {
    return Array.isArray(snapshot?.hardware?.devices) ? snapshot.hardware.devices : [];
}

function _hasHardwareDevice(deviceId, snapshot = appState.snapshot) {
    return Boolean(deviceId) && _hardwareDevices(snapshot).some((device) => device?.id === deviceId);
}

async function _resolveHardwareDeviceId(deviceId) {
    if (_hasHardwareDevice(deviceId)) {
        return deviceId;
    }
    const refreshed = await refreshState();
    return _hasHardwareDevice(deviceId, refreshed) ? deviceId : null;
}

function _isUnknownVirtualDeviceError(error) {
    return error instanceof SimulatorApiError && /unknown virtual device/i.test(String(error.message || ""));
}

function _notifyMissingHardwareDevice(actionLabel = "Hardware update") {
    const message = `${actionLabel}: device is no longer available in the current session. State refreshed.`;
    setStatusExtra(message);
    pushToast(message, "warn", 4200);
}

async function _resolveHardwareDeviceIdOrWarn(deviceId, actionLabel = "Hardware update") {
    const resolvedId = await _resolveHardwareDeviceId(deviceId);
    if (resolvedId) {
        return resolvedId;
    }
    _notifyMissingHardwareDevice(actionLabel);
    return null;
}

async function _recoverUnknownHardwareDevice(error, actionLabel = "Hardware update") {
    if (!_isUnknownVirtualDeviceError(error)) {
        return false;
    }
    try {
        await refreshState();
    } catch (_refreshError) {
    }
    _notifyMissingHardwareDevice(actionLabel);
    return true;
}

function _defaultHardwarePlacement(canvas, deviceType) {
    const width = 180;
    const height = 120;
    const placements = appState.architecture === "arm"
        ? [
            { x: 240, y: 180 }, { x: 1580, y: 180 }, { x: 240, y: 360 }, { x: 1580, y: 360 },
            { x: 240, y: 540 }, { x: 1580, y: 540 }, { x: 240, y: 720 }, { x: 1580, y: 720 },
        ]
        : [
            { x: 220, y: 150 }, { x: 1580, y: 150 }, { x: 220, y: 320 }, { x: 1580, y: 320 },
            { x: 220, y: 490 }, { x: 1580, y: 490 }, { x: 220, y: 660 }, { x: 1580, y: 660 },
        ];
    const guess = placements[hardwareState.nodes.size % placements.length];
    return _resolveCollision(canvas, `new-${deviceType}`, guess.x, guess.y, width, height);
}

async function createHardwareDeviceAt(kind, point = null, binding = null) {
    const previousIds = new Set((appState.snapshot?.hardware?.devices || []).map((device) => device.id));
    const initial = await client.hardwareAddDevice(kind);
    renderSnapshot(initial);
    const canvas = getHardwareCanvas();
    const nextId = _findNewHardwareDeviceId(initial, previousIds, kind);
    if (!nextId || !canvas) {
        return;
    }
    const resolvedId = await _resolveHardwareDeviceIdOrWarn(nextId, "Add device");
    if (!resolvedId) {
        return;
    }
    const desired = point
        ? _resolveCollision(canvas, resolvedId, _snap(point.x), _snap(point.y), 180, 120)
        : _defaultHardwarePlacement(canvas, kind);
    const updatePayload = {
        id: resolvedId,
        position: desired,
        settings: {
            layout: {
                zIndex: _nextHardwareZ(),
            },
        },
    };
    if (binding && typeof binding === "object") {
        updatePayload.connections = binding;
    }
    try {
        const response = await client.hardwareUpdateDevice(updatePayload);
        renderSnapshot(response);
        _selectHardwareDevice(resolvedId);
    } catch (error) {
        if (!(await _recoverUnknownHardwareDevice(error, "Add device"))) {
            throw error;
        }
    }
}

function _renderHardwarePalette(hw) {
    const node = byId("vh-component-palette");
    if (!node) {
        return;
    }
    const types = hw?.device_types || hw?.deviceTypes || [];
    const resolvedTypes = types.length ? types : DEFAULT_HARDWARE_DEVICE_TYPES;
    if (!types.length) {
        node.innerHTML = `
            <div class="vh-pass-line">
                Hardware catalog missing from backend response. Showing fallback palette.
                <div class="vh-cause-line">Redeploy the Render backend to the latest commit to restore full hardware metadata.</div>
            </div>
        ` + resolvedTypes.map((type) => `
            <button type="button" class="vh-palette-item" data-device-type="${escapeHtml(type.type)}">
                <span class="vh-palette-icon">${escapeHtml(type.icon || "•")}</span>
                <span class="vh-palette-copy">
                    <strong>${escapeHtml(type.label || type.type)}</strong>
                    <span>${escapeHtml(type.type)}</span>
                </span>
            </button>
        `).join("");
        return;
    }
    node.innerHTML = resolvedTypes.map((type) => `
        <button type="button" class="vh-palette-item" data-device-type="${escapeHtml(type.type)}">
            <span class="vh-palette-icon">${escapeHtml(type.icon || "•")}</span>
            <span class="vh-palette-copy">
                <strong>${escapeHtml(type.label || type.type)}</strong>
                <span>${escapeHtml(type.type)}</span>
            </span>
        </button>
    `).join("");
}

function _signalGroups(hw) {
    const catalog = hw?.catalog || {};
    const pins = catalog.pins || [];
    const groups = new Map();
    pins.forEach((pin) => {
        const [groupName] = String(pin.id || "").split(".");
        const bucket = groups.get(groupName) || [];
        bucket.push(pin);
        groups.set(groupName, bucket);
    });
    return {
        pins: Array.from(groups.entries()),
        bus8: catalog.bus8 || [],
        bus4: catalog.bus4 || [],
    };
}

function _busIdForPin(hw, signalId, kind) {
    const catalog = hw?.catalog || {};
    const pools = kind === "bus4" ? (catalog.bus4 || []) : (catalog.bus8 || []);
    const match = pools.find((bus) => (bus.pins || []).includes(signalId));
    return match?.id || null;
}

function _isSignalConnected(hw, signalId) {
    return Boolean((hw?.wires || []).some((wire) => wire.fromPin === signalId));
}

function _signalVisualState(hw, signalId) {
    const signalState = _signalState(hw, signalId) || {};
    if (signalState.contention) {
        return "error";
    }
    if (signalState.fault || signalState.floating) {
        return "warning";
    }
    return _pinLevel(hw, signalId) ? "high" : "low";
}

function _pinLevel(hw, signalId) {
    return Number(hw?.pins?.[signalId]?.level ?? 0);
}

function _busLevel(hw, signalId) {
    const catalog = hw?.catalog || {};
    const bus = [...(catalog.bus8 || []), ...(catalog.bus4 || [])].find((item) => item.id === signalId);
    if (!bus) {
        return 0;
    }
    return (bus.pins || []).some((pin) => _pinLevel(hw, pin)) ? 1 : 0;
}

function _renderSignalNode(signalId, kind, label, hw) {
    const active = kind === "pin" ? _pinLevel(hw, signalId) : _busLevel(hw, signalId);
    const signalState = _signalState(hw, signalId) || {};
    const visualState = kind === "pin" ? _signalVisualState(hw, signalId) : (active ? "high" : "low");
    const statusClass = visualState === "error"
        ? "is-error"
        : visualState === "warning"
            ? "is-warning"
            : visualState === "high"
                ? "is-high"
                : "is-low";
    const titleParts = [label];
    if (signalState.contention) {
        titleParts.push("Bus contention");
    } else if (signalState.floating) {
        titleParts.push("Floating input");
    }
    if (signalState.fault) {
        titleParts.push(`Fault: ${signalState.fault}`);
    }
    if (_isSignalConnected(hw, signalId)) {
        titleParts.push("Connected");
    }
    return `
        <button type="button" class="vh-signal-node vh-mcu-pin ${active ? "is-active" : ""} ${statusClass} ${_isSignalConnected(hw, signalId) ? "is-connected" : ""}" data-signal-id="${escapeHtml(signalId)}" data-signal-kind="${escapeHtml(kind)}" title="${escapeHtml(titleParts.join(" • "))}">
            <span class="vh-node-dot"></span>
            <span>${escapeHtml(label)}</span>
        </button>
    `;
}

function _renderPassiveDipPin(number, label, side) {
    return `
        <div class="vh-dip-row ${side}">
            ${side === "left"
                ? `<span class="vh-dip-number">${number}</span><span class="vh-dip-label">${escapeHtml(label)}</span><span class="vh-dip-pad is-passive" aria-hidden="true"></span>`
                : `<span class="vh-dip-pad is-passive" aria-hidden="true"></span><span class="vh-dip-label">${escapeHtml(label)}</span><span class="vh-dip-number">${number}</span>`}
        </div>
    `;
}

function _renderActiveDipPin(number, signalId, side, hw) {
    const signalState = _signalState(hw, signalId) || {};
    const visualState = _signalVisualState(hw, signalId);
    const statusClass = visualState === "error"
        ? "is-error"
        : visualState === "warning"
            ? "is-warning"
            : visualState === "high"
                ? "is-high"
                : "is-low";
    const titleParts = [signalId];
    if (signalState.contention) {
        titleParts.push("Bus contention");
    } else if (signalState.floating) {
        titleParts.push("Floating input");
    }
    if (signalState.fault) {
        titleParts.push(`Fault: ${signalState.fault}`);
    }
    if (_isSignalConnected(hw, signalId)) {
        titleParts.push("Connected");
    }
    const button = `
        <button
            type="button"
            class="vh-signal-node vh-mcu-pin vh-dip-pad ${statusClass} ${_isSignalConnected(hw, signalId) ? "is-connected" : ""}"
            data-signal-id="${escapeHtml(signalId)}"
            data-signal-kind="pin"
            title="${escapeHtml(titleParts.join(" • "))}"
            aria-label="${escapeHtml(signalId)}"
        >
            <span class="vh-node-dot"></span>
        </button>
    `;
    return `
        <div class="vh-dip-row ${side}">
            ${side === "left"
                ? `<span class="vh-dip-number">${number}</span><span class="vh-dip-label">${escapeHtml(signalId)}</span>${button}`
                : `${button}<span class="vh-dip-label">${escapeHtml(signalId)}</span><span class="vh-dip-number">${number}</span>`}
        </div>
    `;
}

function _renderFallbackMcu(architecture, hw) {
    if (architecture === "arm") {
        const pins = Array.from({ length: 16 }, (_, index) => `GPIOA.${index}`);
        return `
            <div class="vh-mcu-fallback">
                <div class="vh-mcu-chip-copy">
                    <strong>ARM GPIO</strong>
                    <span>Fallback header view</span>
                </div>
                <div class="vh-mcu-fallback-grid">
                    ${pins.map((pin) => _renderSignalNode(pin, "pin", pin, hw)).join("")}
                </div>
            </div>
        `;
    }
    const ports = ["P0", "P1", "P2", "P3"];
    return `
        <div class="vh-mcu-fallback">
            <div class="vh-mcu-chip-copy">
                <strong>AT89C51</strong>
                <span>Fallback pin view</span>
            </div>
            <div class="vh-mcu-fallback-grid">
                ${ports.flatMap((port) => Array.from({ length: 8 }, (_, bit) => _renderSignalNode(`${port}.${bit}`, "pin", `${port}.${bit}`, hw))).join("")}
            </div>
        </div>
    `;
}

function _renderHardwareMcuBoard(hw) {
    const node = ensureMcuBoardNode();
    if (!node) {
        return;
    }
    const model = _currentMcuModel();
    if (!Number.isFinite(model.width) || model.width <= 0) {
        model.width = MCU_DEFAULTS[model.type]?.width || 220;
    }
    if (!Number.isFinite(model.height) || model.height <= 0) {
        model.height = MCU_DEFAULTS[model.type]?.height || 320;
    }
    node.dataset.mcuId = model.id;
    node.dataset.mcuType = model.type;
    node.dataset.locked = "true";
    node.style.left = `${model.x}px`;
    node.style.top = `${model.y}px`;
    node.style.width = `${model.width}px`;
    node.style.minHeight = `${model.height}px`;
    node.style.transform = "none";
    node.classList.toggle("is-debug", Boolean(window.DEBUG_MCU));
    if (appState.architecture === "arm") {
        const groups = _signalGroups(hw);
        node.innerHTML = `
            <div class="vh-mcu-shell is-arm">
                <div class="vh-mcu-chip">
                    <div class="vh-mcu-chip-copy">
                        <strong>ARM GPIO</strong>
                        <span>GPIOA header</span>
                    </div>
                    <div class="vh-arm-groups">
                        ${groups.pins.map(([group, pins]) => `
                            <div class="vh-arm-bank">
                                <div class="vh-signal-group-title">${escapeHtml(group)}</div>
                                <div class="vh-arm-pin-grid">
                                    ${pins.map((pin) => _renderSignalNode(pin.id, "pin", pin.label || pin.id, hw)).join("")}
                                </div>
                            </div>
                        `).join("")}
                    </div>
                </div>
            </div>
        `;
        if (!node.innerHTML.trim()) {
            node.innerHTML = _renderFallbackMcu("arm", hw);
        }
        if (window.DEBUG_MCU) {
            console.log("MCU render", { type: "arm", x: model.x, y: model.y, width: model.width, height: model.height });
        }
        window.requestAnimationFrame(() => {
            const liveNode = document.getElementById("vh-mcu-board");
            if (!liveNode) {
                console.error("MCU NOT IN DOM");
                return;
            }
            if ((liveNode.offsetWidth || 0) <= 0) {
                liveNode.style.width = `${model.width}px`;
            }
            if ((liveNode.offsetHeight || 0) <= 0) {
                liveNode.style.minHeight = `${model.height}px`;
            }
            if (window.DEBUG_MCU) {
                console.log("MCU visibility", {
                    width: liveNode.offsetWidth,
                    height: liveNode.offsetHeight,
                    left: liveNode.style.left,
                    top: liveNode.style.top,
                });
            }
        });
        return;
    }
    const leftPins = [
        ...Array.from({ length: 8 }, (_, index) => ({ number: index + 1, signal: `P1.${index}` })),
        { number: 9, label: "RST" },
        ...Array.from({ length: 8 }, (_, index) => ({ number: index + 10, signal: `P3.${index}` })),
        { number: 18, label: "XTAL2" },
        { number: 19, label: "XTAL1" },
        { number: 20, label: "GND" },
    ];
    const rightPins = [
        { number: 40, label: "VCC" },
        ...Array.from({ length: 8 }, (_, index) => ({ number: 39 - index, signal: `P0.${index}` })),
        { number: 31, label: "EA/VPP" },
        { number: 30, label: "ALE/PROG" },
        { number: 29, label: "PSEN" },
        ...Array.from({ length: 8 }, (_, index) => ({ number: 28 - index, signal: `P2.${7 - index}` })),
    ];
    node.innerHTML = `
        <div class="vh-mcu-shell is-8051">
            <div class="vh-mcu-chip vh-dip-chip">
                <div class="vh-dip-notch" aria-hidden="true"></div>
                <div class="vh-mcu-chip-copy">
                    <strong>AT89C51</strong>
                    <span>8051 MCU • 40-pin DIP</span>
                </div>
                <div class="vh-dip-sides">
                    <div class="vh-dip-side left">
                        ${leftPins.map((pin) => pin.signal ? _renderActiveDipPin(pin.number, pin.signal, "left", hw) : _renderPassiveDipPin(pin.number, pin.label, "left")).join("")}
                    </div>
                    <div class="vh-dip-body">
                        <div class="vh-mcu-chip-mark"></div>
                        <div class="vh-mcu-chip-meta">
                            <span>Port 1</span>
                            <span>Port 3</span>
                            <span>Crystal / Reset</span>
                            <span>Port 0</span>
                            <span>Port 2</span>
                        </div>
                    </div>
                    <div class="vh-dip-side right">
                        ${rightPins.map((pin) => pin.signal ? _renderActiveDipPin(pin.number, pin.signal, "right", hw) : _renderPassiveDipPin(pin.number, pin.label, "right")).join("")}
                    </div>
                </div>
            </div>
        </div>
    `;
    if (!node.innerHTML.trim()) {
        node.innerHTML = _renderFallbackMcu("8051", hw);
    }
    if (window.DEBUG_MCU) {
        console.log("MCU render", { type: "8051", x: model.x, y: model.y, width: model.width, height: model.height });
    }
    window.requestAnimationFrame(() => {
        const liveNode = document.getElementById("vh-mcu-board");
        if (!liveNode) {
            console.error("MCU NOT IN DOM");
            return;
        }
        if ((liveNode.offsetWidth || 0) <= 0) {
            liveNode.style.width = `${model.width}px`;
        }
        if ((liveNode.offsetHeight || 0) <= 0) {
            liveNode.style.minHeight = `${model.height}px`;
        }
        if (window.DEBUG_MCU) {
            console.log("MCU visibility", {
                width: liveNode.offsetWidth,
                height: liveNode.offsetHeight,
                left: liveNode.style.left,
                top: liveNode.style.top,
            });
        }
    });
}

function _componentPinDescriptors(device) {
    if (Array.isArray(device.pins) && device.pins.length) {
        return device.pins.map((pin, index) => ({
            id: pin.id || `${device.id}-pin-${index}`,
            label: pin.label || pin.id || `PIN${index}`,
            kind: pin.kind || "input",
            signal: pin.signal || null,
        }));
    }
    if (device.type === "led_array") {
        return Array.from({ length: 8 }, (_, index) => ({ id: `bit${index}`, label: `D${index}`, kind: "input", signal: null }));
    }
    if (device.type === "seven_segment") {
        return ["a", "b", "c", "d", "e", "f", "g", "dp"].map((label) => ({ id: label, label: label.toUpperCase(), kind: "input", signal: null }));
    }
    if (device.type === "stepper") {
        return ["A", "B", "C", "D"].map((label) => ({ id: label.toLowerCase(), label, kind: "input", signal: null }));
    }
    const conn = device.schema?.connections?.[0];
    return conn ? [{ id: conn.key, label: conn.label || conn.key, kind: "input", signal: null }] : [];
}

function _renderDeviceConnections(device, hw) {
    const schema = device.schema || {};
    const descriptors = _componentPinDescriptors(device);
    return (schema.connections || []).map((conn) => {
        const current = device.connections?.[conn.key] || "";
        const signalState = current ? (_signalState(hw, current) || {}) : {};
        const targetClass = signalState.contention
            ? "is-error"
            : (signalState.floating || signalState.fault ? "is-warning" : "");
        const pins = descriptors.map((pin, index) => {
            const signal = pin.signal || "";
            const nodeId = `${device.id}:${conn.key}:${pin.id || index}`;
            const state = signal ? (_signalState(hw, signal) || {}) : {};
            const statusClass = state.contention ? "is-error" : ((state.floating || state.fault) ? "is-warning" : "");
            return `
                <button type="button" class="vh-port-node vh-device-pin ${statusClass}" data-node-id="${escapeHtml(nodeId)}" data-device-id="${escapeHtml(device.id)}" data-connection-key="${escapeHtml(conn.key)}" data-connection-kind="${escapeHtml(conn.kind)}" data-pin-id="${escapeHtml(pin.id)}" data-wire-signal="${escapeHtml(signal)}" title="${escapeHtml(signal || conn.label)}">
                    <span class="vh-node-dot"></span>
                    <span>${escapeHtml(pin.label)}</span>
                </button>
            `;
        }).join("");
        return `
            <div class="vh-port-cluster">
                <div class="vh-port-cluster-head">
                    <span class="vh-port-cluster-label">${escapeHtml(conn.label)}</span>
                    <span class="vh-port-target ${targetClass}" title="${escapeHtml(current || "unbound")}">${escapeHtml(current ? `Connected to ${current}` : "unbound")}</span>
                </div>
                <div class="vh-port-pin-row">${pins}</div>
            </div>
        `;
    }).join("");
}

function _connectorKey(kind, id) {
    return `${kind}:${id}`;
}

function _connectionNodeKey(deviceId, connectionKey) {
    return `${deviceId}:${connectionKey}`;
}

function _refreshHardwareConnectorRegistry() {
    hardwareState.signalNodes.clear();
    hardwareState.connectionNodes.clear();
    document.querySelectorAll(".vh-signal-node[data-signal-id]").forEach((node) => {
        hardwareState.signalNodes.set(_connectorKey(node.dataset.signalKind, node.dataset.signalId), node);
    });
    document.querySelectorAll(".vh-port-node[data-device-id][data-connection-key]").forEach((node) => {
        const key = node.dataset.nodeId || _connectionNodeKey(node.dataset.deviceId, node.dataset.connectionKey);
        hardwareState.connectionNodes.set(key, node);
    });
}

function _connectorCenter(node, stage) {
    if (!node || !stage) {
        return { x: 0, y: 0 };
    }
    const rect = node.getBoundingClientRect();
    const stageRect = stage.getBoundingClientRect();
    return {
        x: ((rect.left + (rect.width / 2)) - stageRect.left) / hardwareState.view.scale,
        y: ((rect.top + (rect.height / 2)) - stageRect.top) / hardwareState.view.scale,
    };
}

function _wirePath(from, to, laneOffset = 0) {
    const direction = to.x >= from.x ? 1 : -1;
    const bendX = from.x + (direction * Math.max(42, Math.abs(to.x - from.x) * 0.5)) + laneOffset;
    const cornerRadius = 10;
    const firstBendX = bendX - (direction * cornerRadius);
    const secondBendY = to.y > from.y ? to.y - cornerRadius : to.y + cornerRadius;
    return [
        `M ${from.x} ${from.y}`,
        `L ${firstBendX} ${from.y}`,
        `Q ${bendX} ${from.y} ${bendX} ${from.y + (to.y > from.y ? cornerRadius : -cornerRadius)}`,
        `L ${bendX} ${secondBendY}`,
        `Q ${bendX} ${to.y} ${bendX + (direction * cornerRadius)} ${to.y}`,
        `L ${to.x} ${to.y}`,
    ].join(" ");
}

function _resolveConnectionSignals(device, conn, hw) {
    const direct = Array.isArray(device.pins) ? device.pins : [];
    const fromPins = direct
        .filter((pin) => pin.signal && conn.key === (device.schema?.connections?.[0]?.key || conn.key))
        .map((pin, index) => ({
            nodeKey: `${device.id}:${conn.key}:${pin.id || index}`,
            signal: pin.signal,
        }));
    if (fromPins.length) {
        return fromPins;
    }
    const signalId = device.connections?.[conn.key];
    if (!signalId) {
        return [];
    }
    if (conn.kind === "pin") {
        return [{ nodeKey: `${device.id}:${conn.key}:${conn.key}`, signal: signalId }];
    }
    const bus = [...(hw.catalog?.bus8 || []), ...(hw.catalog?.bus4 || [])].find((item) => item.id === signalId);
    return (bus?.pins || []).map((pin, index) => ({
        nodeKey: `${device.id}:${conn.key}:${index}`,
        signal: pin,
    }));
}

function renderHardwareWires(hw = appState.snapshot?.hardware) {
    const svg = getHardwareWireLayer();
    const stage = getHardwareStage();
    const canvas = getHardwareCanvas();
    if (!svg || !stage || !canvas || !hw) {
        return;
    }
    const width = canvas.offsetWidth || 1600;
    const height = canvas.offsetHeight || 960;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("width", String(width));
    svg.setAttribute("height", String(height));
    const liveIds = new Set();
    const ensurePath = (id, d, preview = false, signalState = null) => {
        liveIds.add(id);
        let path = hardwareState.wirePaths.get(id);
        if (!path) {
            path = document.createElementNS("http://www.w3.org/2000/svg", "path");
            path.classList.add("vh-wire-path");
            svg.appendChild(path);
            hardwareState.wirePaths.set(id, path);
        }
        path.setAttribute("d", d);
        path.classList.toggle("vh-wire-preview", preview);
        path.classList.toggle("is-high", !preview && Boolean(signalState && signalState.state === "high"));
        path.classList.toggle("is-low", !preview && Boolean(signalState && signalState.state === "low"));
        path.classList.toggle("is-connected", !preview && Boolean(signalState));
        path.classList.toggle("is-warning", !preview && Boolean(signalState && (signalState.floating || signalState.fault)));
        path.classList.toggle("is-error", !preview && Boolean(signalState && signalState.contention));
    };
    (hw.devices || []).forEach((device) => {
        (device.schema?.connections || []).forEach((conn) => {
            const wireSignals = _resolveConnectionSignals(device, conn, hw);
            wireSignals.forEach(({ nodeKey, signal }, index) => {
                const signalNode = hardwareState.signalNodes.get(_connectorKey("pin", String(signal)));
                const connectionNode = hardwareState.connectionNodes.get(nodeKey);
                if (!signalNode || !connectionNode) {
                    return;
                }
                const from = _connectorCenter(signalNode, stage);
                const to = _connectorCenter(connectionNode, stage);
                const laneOffset = ((index % 3) - 1) * 10;
                ensurePath(`wire:${nodeKey}`, _wirePath(from, to, laneOffset), false, _signalState(hw, signal));
            });
        });
    });
    if (hardwareState.connectionDrag) {
        const drag = hardwareState.connectionDrag;
        const source = drag.sourceType === "signal"
            ? hardwareState.signalNodes.get(_connectorKey(drag.kind, drag.signalId))
            : hardwareState.connectionNodes.get(drag.nodeId || _connectionNodeKey(drag.deviceId, drag.connectionKey));
        if (source) {
            const from = _connectorCenter(source, stage);
            ensurePath("__preview__", _wirePath(from, drag.currentPoint || from, 0), true);
        }
    }
    Array.from(hardwareState.wirePaths.entries()).forEach(([id, path]) => {
        if (!liveIds.has(id)) {
            path.remove();
            hardwareState.wirePaths.delete(id);
        }
    });
}

function scheduleHardwareWireRender(hw = appState.snapshot?.hardware) {
    hardwareState.pendingWireSnapshot = hw;
    if (hardwareState.wireRafId) {
        return;
    }
    hardwareState.wireRafId = window.requestAnimationFrame(() => {
        hardwareState.wireRafId = 0;
        renderHardwareWires(hardwareState.pendingWireSnapshot);
        hardwareState.pendingWireSnapshot = null;
    });
}

function _beginConnectionDrag(event, descriptor) {
    event.preventDefault();
    event.stopPropagation();
    hardwareState.connectionDrag = {
        ...descriptor,
        pointerId: event.pointerId,
        currentPoint: clientToHardwarePoint(event),
        hover: null,
    };
    _highlightConnector(null);
    scheduleHardwareWireRender();
}

async function _completeConnectionDrag(target) {
    const drag = hardwareState.connectionDrag;
    if (!drag || !target) {
        return;
    }
    let deviceId = null;
    let connectionKey = null;
    let signalId = null;
    let compatible = false;
    if (drag.sourceType === "signal" && target.dataset.deviceId) {
        compatible = target.dataset.connectionKind === "pin" || target.dataset.connectionKind === "bus8" || target.dataset.connectionKind === "bus4";
        deviceId = target.dataset.deviceId;
        connectionKey = target.dataset.connectionKey;
        signalId = target.dataset.connectionKind === "pin"
            ? drag.signalId
            : _busIdForPin(appState.snapshot?.hardware, drag.signalId, target.dataset.connectionKind);
    } else if (drag.sourceType === "device" && target.dataset.signalId) {
        compatible = target.dataset.signalKind === "pin";
        deviceId = drag.deviceId;
        connectionKey = drag.connectionKey;
        signalId = drag.kind === "pin"
            ? target.dataset.signalId
            : _busIdForPin(appState.snapshot?.hardware, target.dataset.signalId, drag.kind);
    }
    if (!compatible || !deviceId || !connectionKey || !signalId) {
        return;
    }
    const resolvedId = await _resolveHardwareDeviceIdOrWarn(deviceId, "Connect signal");
    if (!resolvedId) {
        return;
    }
    try {
        const response = await client.hardwareUpdateDevice({
            id: resolvedId,
            connections: { [connectionKey]: signalId },
        });
        renderSnapshot(response);
        _selectHardwareDevice(resolvedId);
        const deviceLabel = response.hardware?.devices?.find((item) => item.id === resolvedId)?.label || resolvedId;
        pushToast(`${signalId} → ${deviceLabel}`, "info");
    } catch (error) {
        if (!(await _recoverUnknownHardwareDevice(error, "Connect signal"))) {
            throw error;
        }
    }
}

function _deviceBodyHtml(device, st) {
    if (device.type === "led") {
        return `<div class="vh-led-dot ${st.on ? "on" : ""}" aria-hidden="true"></div>`;
    }
    if (device.type === "led_array") {
        const bits = st.bits || [];
        return `<div class="vh-led-row">${bits.map((b) => `<span class="vh-led-bit ${b ? "on" : ""}"></span>`).join("")}</div><div class="vh-meta">0x${Number(st.value ?? 0)
            .toString(16)
            .toUpperCase()}</div>`;
    }
    if (device.type === "seven_segment") {
        return `<div class="vh-seven" aria-label="7-segment">${escapeHtml(st.digit ?? "?")}</div>`;
    }
    if (device.type === "switch") {
        return `<div class="vh-switch"><label><input type="checkbox" data-switch ${st.input_level ? "checked" : ""}/> High</label></div>`;
    }
    if (device.type === "stepper") {
        const rot = Number(st.angle ?? 0);
        return `<div class="vh-motor" style="transform:rotate(${rot}deg)"></div><div class="vh-meta">Pattern ${Number(st.pattern ?? 0)}</div>`;
    }
    return `<div class="vh-meta">No renderer</div>`;
}

function _createHardwareDeviceNode(device, catalog, hw) {
    const wrap = document.createElement("div");
    wrap.className = "vh-device";
    wrap.dataset.deviceId = device.id;
    wrap.dataset.deviceType = device.type;
    const st = device.state || {};
    wrap.innerHTML = `
        <header>
            <span>${escapeHtml(device.label || device.type)}</span>
            <div class="vh-head-actions"><span class="vh-status-chip"></span><button type="button" class="vh-remove" title="Remove">×</button></div>
        </header>
        <div class="vh-port-list">${_renderDeviceConnections(device, hw)}</div>
        <div class="vh-body">${_deviceBodyHtml(device, st)}</div>
    `;
    _bindDrag(wrap);
    return wrap;
}

function _updateHardwareDeviceNode(node, device, catalog, hw) {
    const st = device.state || {};
    const headerSpan = node.querySelector("header span");
    if (headerSpan && headerSpan.textContent !== (device.label || device.type)) {
        headerSpan.textContent = device.label || device.type;
    }
    const body = node.querySelector(".vh-body");
    if (body) {
        body.innerHTML = _deviceBodyHtml(device, st);
    }
    const validationStatus = device.validation?.status || "pass";
    node.dataset.validation = validationStatus;
    node.classList.toggle("is-selected", hardwareState.selectedDeviceId === device.id);
    const chip = node.querySelector(".vh-status-chip");
    if (chip) {
        chip.textContent = validationStatus.toUpperCase();
        chip.className = `vh-status-chip is-${validationStatus}`;
    }
    const schema = device.schema || {};
    const signature = JSON.stringify({ schema: schema.connections || [], conns: device.connections || {}, pinCount: Object.keys(hw?.pins || {}).length });
    if (node.dataset.connSignature !== signature) {
        const portList = node.querySelector(".vh-port-list");
        if (portList) {
            portList.innerHTML = _renderDeviceConnections(device, hw);
        }
        node.dataset.connSignature = signature;
    }
}

function _applyDeviceLayout(canvas, node, device) {
    const id = device.id;
    const saved = hardwareState.localLayout[id] || {};
    const backendLayout = device.settings?.layout || {};
    const rawPos = device.position || {};
    const hasExplicitPos = Number.isFinite(saved.x) || Number.isFinite(rawPos.x) || Number.isFinite(backendLayout.x);
    let x = Number.isFinite(saved.x) ? saved.x : Number.isFinite(rawPos.x) ? rawPos.x : Number.isFinite(backendLayout.x) ? backendLayout.x : 16;
    let y = Number.isFinite(saved.y) ? saved.y : Number.isFinite(rawPos.y) ? rawPos.y : Number.isFinite(backendLayout.y) ? backendLayout.y : 16;
    const width = node.offsetWidth || node.getBoundingClientRect().width || 140;
    const height = node.offsetHeight || node.getBoundingClientRect().height || 100;
    if (!hasExplicitPos && x === 16 && y === 16) {
        const auto = _defaultHardwarePlacement(canvas, device.type);
        x = auto.x;
        y = auto.y;
    }
    const clamped = _clampDeviceToCanvas(canvas, x, y, width, height);
    x = clamped.x;
    y = clamped.y;
    const zIndex = Number.isFinite(saved.zIndex) ? saved.zIndex : Number(backendLayout.zIndex || 1);
    node.style.left = `${x}px`;
    node.style.top = `${y}px`;
    node.style.zIndex = String(zIndex);
    hardwareState.localLayout[id] = {
        id,
        type: device.type,
        x,
        y,
        width,
        height,
        zIndex,
        props: saved.props || {},
    };
    hardwareState.zCounter = Math.max(hardwareState.zCounter, zIndex);
    scheduleHardwareWireRender();
}

function _selectHardwareDevice(deviceId) {
    _setSelectedDevices(deviceId ? [deviceId] : [], deviceId || null);
    if (appState.snapshot?.hardware) {
        _renderHardwareDebugPanel(appState.snapshot.hardware);
    }
}

function _faultControlHtml(signal, faults) {
    if (!signal) {
        return "";
    }
    const active = faults?.[signal]?.type || "";
    const options = ["", "stuck_high", "stuck_low", "delay", "noise"].map((type) => {
        const label = type ? type.replace("_", " ") : "none";
        return `<option value="${escapeHtml(type)}"${type === active ? " selected" : ""}>${escapeHtml(label)}</option>`;
    }).join("");
    return `
        <label class="vh-fault-label">
            <span>Fault (${escapeHtml(signal)})</span>
            <select class="vh-fault-select" data-signal="${escapeHtml(signal)}">${options}</select>
        </label>
    `;
}

function _inspectorSignalsHtml(selectedDevice, hw) {
    const signalStates = hw?.debug?.signals || {};
    const faults = hw?.debug?.faults || {};
    return (selectedDevice?.schema?.connections || []).map((conn) => {
        const signal = selectedDevice.connections?.[conn.key] || "";
        if (!signal) {
            return `<div class="vh-inspector-signal"><strong>${escapeHtml(conn.label)}</strong><span>unbound</span></div>`;
        }
        const state = signalStates[signal] || {};
        const badges = [];
        if (state.contention) {
            badges.push("contention");
        } else if (state.floating) {
            badges.push("floating");
        }
        if (state.fault) {
            badges.push(`fault:${state.fault}`);
        }
        return `
            <div class="vh-inspector-signal ${state.contention ? "is-error" : (state.floating || state.fault ? "is-warning" : "")}">
                <strong>${escapeHtml(conn.label)}</strong>
                <span>${escapeHtml(signal)}</span>
                <span>${escapeHtml(`state=${state.state || (state.level ? "high" : "low")}`)}</span>
                <span>${escapeHtml(badges.join(" • ") || "ok")}</span>
                <span>${escapeHtml(state.last_read_ms != null ? `read=${Number(state.last_read_ms).toFixed(3)}ms` : "read=never")}</span>
            </div>
            ${_faultControlHtml(signal, faults)}
        `;
    }).join("");
}

function _renderLogicAnalyzer(hw) {
    const canvas = byId("vh-logic-analyzer");
    if (!canvas) {
        return;
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) {
        return;
    }
    const signals = hw?.debug?.signals || {};
    const log = hw?.debug?.signal_log || [];
    const cssWidth = Math.max(320, canvas.clientWidth || canvas.width || 420);
    const cssHeight = Math.max(150, canvas.clientHeight || canvas.height || 150);
    if (canvas.width !== cssWidth || canvas.height !== cssHeight) {
        canvas.width = cssWidth;
        canvas.height = cssHeight;
    }
    const selectedDevice = (hw?.devices || []).find((device) => device.id === hardwareState.selectedDeviceId);
    const selectedSignals = new Set();
    (selectedDevice?.schema?.connections || []).forEach((conn) => {
        const signal = selectedDevice.connections?.[conn.key];
        if (signal) {
            selectedSignals.add(signal);
        }
    });
    const signalNames = Array.from(selectedSignals);
    if (!signalNames.length) {
        const seen = new Set();
        log.slice(-32).forEach((event) => {
            if (!seen.has(event.pin)) {
                seen.add(event.pin);
                signalNames.push(event.pin);
            }
        });
    }
    const visibleSignals = signalNames.slice(0, 6);
    const width = canvas.width;
    const height = canvas.height;
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = getComputedStyle(document.body).getPropertyValue("--panel-bg").trim() || "#181818";
    ctx.fillRect(0, 0, width, height);
    if (!visibleSignals.length) {
        ctx.fillStyle = "#888888";
        ctx.font = "12px ui-monospace, SFMono-Regular, monospace";
        ctx.fillText("No signal activity yet", 14, 24);
        return;
    }
    const laneHeight = Math.max(22, Math.floor((height - 18) / visibleSignals.length));
    const allEvents = log.slice(-128);
    const latestTime = Number(allEvents[allEvents.length - 1]?.time_ms ?? 0);
    const earliestTime = Number(allEvents[0]?.time_ms ?? 0);
    const totalSpan = Math.max(1, latestTime - earliestTime || 1);
    const effectiveZoom = clamp(hardwareState.analyzer.zoom || 1, 1, 12);
    const visibleSpan = Math.max(1, totalSpan / effectiveZoom);
    const maxOffset = Math.max(0, totalSpan - visibleSpan);
    hardwareState.analyzer.offsetMs = clamp(hardwareState.analyzer.offsetMs || 0, 0, maxOffset);
    const end = latestTime - hardwareState.analyzer.offsetMs;
    const start = Math.max(0, end - visibleSpan);
    const span = Math.max(1, end - start);
    const recent = allEvents.filter((event) => Number(event.time_ms || 0) >= start && Number(event.time_ms || 0) <= end);
    ctx.font = "11px ui-monospace, SFMono-Regular, monospace";
    ctx.fillStyle = "#9aa4b2";
    ctx.fillText(`${start.toFixed(1)}ms`, 78, height - 6);
    ctx.fillText(`${end.toFixed(1)}ms`, width - 64, height - 6);
    visibleSignals.forEach((signal, index) => {
        const laneTop = 8 + (index * laneHeight);
        const highY = laneTop + 6;
        const lowY = laneTop + laneHeight - 8;
        ctx.strokeStyle = "#2a2a2a";
        ctx.beginPath();
        ctx.moveTo(78, lowY);
        ctx.lineTo(width - 8, lowY);
        ctx.stroke();
        ctx.fillStyle = "#aaaaaa";
        ctx.fillText(signal, 10, laneTop + 12);
        const signalEvents = recent.filter((event) => event.pin === signal);
        let level = Number(signals[signal]?.level ?? 0);
        if (signalEvents.length) {
            level = Number(signalEvents[0].value ?? level);
        }
        let previousX = 78;
        let previousY = level ? highY : lowY;
        ctx.strokeStyle = "#8fb3ff";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(previousX, previousY);
        signalEvents.forEach((event) => {
            const x = 78 + (((Number(event.time_ms || 0) - start) / span) * (width - 92));
            ctx.lineTo(x, previousY);
            previousY = Number(event.value) ? highY : lowY;
            ctx.lineTo(x, previousY);
            previousX = x;
        });
        ctx.lineTo(width - 8, previousY);
        ctx.stroke();
    });
    if (Number.isFinite(hardwareState.analyzer.cursorX)) {
        const cursorX = clamp(Number(hardwareState.analyzer.cursorX), 78, width - 8);
        const cursorTime = start + (((cursorX - 78) / Math.max(1, width - 86)) * span);
        ctx.strokeStyle = "rgba(143,179,255,0.55)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(cursorX, 6);
        ctx.lineTo(cursorX, height - 18);
        ctx.stroke();
        ctx.fillStyle = "#d8e2f0";
        ctx.fillText(`${cursorTime.toFixed(2)}ms`, Math.min(width - 92, cursorX + 8), 16);
    }
}

function _renderHardwareDebugPanel(hw) {
    const portNode = byId("vh-port-values");
    const compNode = byId("vh-component-states");
    const errNode = byId("vh-validation-errors");
    const signalNode = byId("vh-signal-log");
    const inspectorNode = byId("vh-inspector");
    const testNode = byId("vh-test-results");
    if (!portNode || !compNode || !errNode || !signalNode || !inspectorNode || !testNode) {
        return;
    }
    const debug = hw?.debug || {};
    const ports = debug.ports || [];
    portNode.innerHTML = ports.length
        ? ports.map((row) => `<div class="vh-debug-row"><strong>${escapeHtml(row.name)}</strong><span>${escapeHtml(row.hex || "")}</span><span>${escapeHtml(row.binary || "")}</span></div>`).join("")
        : "<div class='vh-pass-line'>No port activity yet</div>";
    const components = debug.components || [];
    compNode.innerHTML = components.length
        ? components.map((component) => {
            const state = component.state || {};
            const summary = Object.entries(state)
                .filter(([key]) => !["segments", "bits", "window"].includes(key))
                .slice(0, 5)
                .map(([key, value]) => `${key}=${typeof value === "object" ? JSON.stringify(value) : String(value)}`)
                .join(" • ");
            return `<button type="button" class="vh-debug-item ${hardwareState.selectedDeviceId === component.id ? "is-selected" : ""}" data-inspect-device="${escapeHtml(component.id)}"><strong>${escapeHtml(component.label)}</strong><span>${escapeHtml(component.type.toUpperCase())}</span><span>${escapeHtml(summary || "No state")}</span></button>`;
        }).join("")
        : "<div class='vh-pass-line'>No hardware components</div>";
    const errs = debug.issues || [];
    if (!errs.length) {
        errNode.innerHTML = `<div class="vh-pass-line">PASS: no mismatches detected</div>`;
    } else {
        errNode.innerHTML = errs
            .map((issue) => `<div class="vh-error-line"><strong>[${escapeHtml(String(issue.level || "warn").toUpperCase())}]</strong> ${escapeHtml(issue.message || "Issue")}<div class="vh-cause-line">${escapeHtml(issue.cause || "")}</div></div>`)
            .join("");
    }
    const signals = debug.signal_log || [];
    signalNode.innerHTML = signals.length
        ? signals.slice(-24).map((event) => `<div class="vh-debug-row"><strong>${escapeHtml(event.pin)}</strong><span>${escapeHtml(String(event.value))}</span><span>${escapeHtml(`${event.time_ms} ms`)}</span></div>`).join("")
        : "<div class='vh-pass-line'>No transitions yet</div>";
    const selectedId = hardwareState.selectedDeviceId || components[0]?.id || null;
    if (selectedId && !hardwareState.selectedDeviceId) {
        hardwareState.selectedDeviceId = selectedId;
    }
    const selected = components.find((component) => component.id === hardwareState.selectedDeviceId) || components[0];
    const selectedDevice = (hw?.devices || []).find((device) => device.id === selected?.id) || null;
    hardwareState.nodes.forEach((node, id) => {
        node.classList.toggle("is-selected", hardwareState.selectedIds.has(id));
        node.classList.toggle("is-primary", id === (selected?.id || null));
    });
    inspectorNode.innerHTML = selected
        ? (() => {
            const changedAt = Number(selected.metrics?.lastChangeTime ?? 0);
            const currentTime = Number(hw?.time_ms ?? 0);
            const stableFor = Math.max(0, currentTime - changedAt);
            const togglePeriodMs = selected.metrics?.togglePeriodMs;
            const humanThresholdMs = selected.metrics?.humanThresholdMs;
            const changedCycle = selected.metrics?.lastChangeCycle;
            const stableCycles = selected.metrics?.stableCycles;
            const togglePeriodCycles = selected.metrics?.togglePeriodCycles;
            return `<div class="vh-inspector-head"><strong>${escapeHtml(selected.label)}</strong><span>${escapeHtml(selected.type.toUpperCase())}</span></div>
           <div class="vh-inspector-block"><strong>Status:</strong> ${escapeHtml((selected.validation?.status || "pass").toUpperCase())}</div>
           <div class="vh-inspector-block"><strong>Metrics:</strong> transitions=${escapeHtml(String(selected.metrics?.transitionCount ?? 0))} • changed_at=${escapeHtml(changedAt.toFixed(3))}ms • stable_for=${escapeHtml(stableFor.toFixed(3))}ms • changed_cycle=${escapeHtml(changedCycle == null ? "n/a" : String(changedCycle))} • stable_cycles=${escapeHtml(stableCycles == null ? "n/a" : String(stableCycles))}</div>
           <div class="vh-inspector-block"><strong>Timing:</strong> toggle_period_ms=${escapeHtml(togglePeriodMs == null ? "n/a" : Number(togglePeriodMs).toFixed(3))} • toggle_period_cycles=${escapeHtml(togglePeriodCycles == null ? "n/a" : String(togglePeriodCycles))} • human_threshold_ms=${escapeHtml(humanThresholdMs == null ? "n/a" : Number(humanThresholdMs).toFixed(3))}</div>
           <div class="vh-inspector-block"><strong>Connections</strong>${selectedDevice ? (selectedDevice.schema?.connections || []).map((conn) => buildConnectionSelect(selectedDevice, conn, hw?.catalog || {}, hw)).join("") : "<div class='vh-meta'>No connections</div>"}</div>
           <div class="vh-inspector-block"><strong>Signals</strong>${_inspectorSignalsHtml(selectedDevice, hw) || "<div class='vh-meta'>No signal bindings</div>"}</div>
           <pre class="vh-json">${escapeHtml(JSON.stringify(selected.state || {}, null, 2))}</pre>`;
        })()
        : "<div class='vh-pass-line'>Select a component to inspect</div>";
    const lastTest = hardwareState.lastTestResult;
    testNode.innerHTML = Array.isArray(lastTest?.results) && lastTest.results.length
        ? lastTest.results
            .map((result) => `<div class="vh-test-line is-${escapeHtml(result.status || "pass")}"><strong>${escapeHtml(String(result.status || "pass").toUpperCase())}</strong><span>${escapeHtml(result.name || "Test")}</span><span>${escapeHtml(result.reason || "")}</span></div>`)
            .join("")
        : "<div class='vh-pass-line'>Run Hardware Test to execute deterministic checks</div>";
    _renderLogicAnalyzer(hw);
}

async function runHardwareValidationTests() {
    if (!appState.snapshot?.hardware) {
        pushToast("No hardware state available", "warn");
        return;
    }
    hardwareState.runningTests = true;
    try {
        const response = await client.hardwareTest();
        hardwareState.lastTestResult = response.hardware_test || null;
        renderSnapshot(response.state);
        const failures = (response.hardware_test?.results || []).filter((item) => item.status !== "pass");
        pushToast(
            failures.length ? `Hardware test FAIL (${failures.length})` : "Hardware test PASS",
            failures.length ? "error" : "info",
        );
    } catch (error) {
        handleError(error, "Hardware test failed");
    } finally {
        hardwareState.runningTests = false;
    }
}

function renderHardware(snapshot, hardwareDiff = null) {
    const s = snapshot ?? appState.snapshot;
    const el = document.getElementById("hardware-canvas");
    if (!el) {
        console.warn("renderHardware: #hardware-canvas not found");
        return;
    }
    const hw = s?.hardware;
    if (HEXLOGIC_HW_DEBUG) {
        console.log("Hardware:", hw);
    }
    if (!hw) {
        el.innerHTML = "<div class='vh-meta'>No hardware data</div>";
        _renderHardwareDebugPanel(hw);
        return;
    }
    _renderHardwarePalette(hw);
    _renderHardwareMcuBoard(hw);
    const mcuNode = ensureMcuBoardNode();
    if (!mcuNode) {
        console.error("MCU NOT IN DOM");
    } else if (!mcuNode.innerHTML.trim()) {
        console.error("MCU rendered empty, forcing fallback");
        mcuNode.innerHTML = _renderFallbackMcu(appState.architecture, hw);
    }
    const catalog = hw.catalog || {};
    const devices = hw.devices || [];
    const liveIds = new Set(devices.map((d) => d.id));
    const changedIds = new Set(hardwareDiff?.changed_ids || devices.map((d) => d.id));

    devices.forEach((device) => {
        let node = hardwareState.nodes.get(device.id);
        if (!node) {
            node = _createHardwareDeviceNode(device, catalog, hw);
            hardwareState.nodes.set(device.id, node);
            el.appendChild(node);
        }
        if (changedIds.has(device.id) || !node.dataset.connSignature) {
            _updateHardwareDeviceNode(node, device, catalog, hw);
        }
        if (node.parentElement !== el) {
            el.appendChild(node);
        }
        _applyDeviceLayout(el, node, device);
    });

    const removedIds = hardwareDiff?.removed_ids || Array.from(hardwareState.nodes.keys()).filter((id) => !liveIds.has(id));
    removedIds.forEach((id) => {
        if (!liveIds.has(id) && hardwareState.nodes.has(id)) {
            const node = hardwareState.nodes.get(id);
            node?.remove();
            hardwareState.nodes.delete(id);
            delete hardwareState.localLayout[id];
        }
    });
    hardwareState.selectedIds = new Set(Array.from(hardwareState.selectedIds).filter((id) => liveIds.has(id)));
    if (hardwareState.selectedDeviceId && !liveIds.has(hardwareState.selectedDeviceId)) {
        hardwareState.selectedDeviceId = Array.from(hardwareState.selectedIds)[0] || null;
    }
    rebuildHardwareOccupancy();
    _updateSelectionVisuals();
    _saveHardwareLayoutLocal();
    _refreshHardwareConnectorRegistry();
    scheduleHardwareWireRender(hw);
    _renderHardwareDebugPanel(hw);
}

async function refreshState() {
    const snapshot = await client.state();
    renderSnapshot(snapshot);
    return snapshot;
}

async function updateHardwareConnection(deviceId, key, value) {
    try {
        const resolvedId = await _resolveHardwareDeviceIdOrWarn(deviceId, "Update connection");
        if (!resolvedId) {
            return;
        }
        const response = await client.hardwareUpdateDevice({ id: resolvedId, connections: { [key]: value } });
        renderSnapshot(response);
    } catch (error) {
        if (!(await _recoverUnknownHardwareDevice(error, "Update connection"))) {
            handleError(error, "Hardware update failed");
        }
    }
}

async function handleHardwareFaultChange(select) {
    const signal = select.dataset.signal;
    if (!signal) {
        return;
    }
    try {
        const response = !select.value
            ? await client.hardwareClearFault(signal)
            : await client.hardwareSetFault(
                signal,
                select.value,
                select.value === "delay" ? { delay_ms: 60 } : (select.value === "noise" ? { period_ms: 40 } : {}),
            );
        renderSnapshot(response);
    } catch (error) {
        handleError(error, "Fault update failed");
    }
}

async function handleHardwareConnectionChange(select) {
    const deviceId = select.dataset.deviceId || select.closest(".vh-device")?.dataset.deviceId;
    const key = select.dataset.conn;
    if (!deviceId || !key) {
        return;
    }
    await updateHardwareConnection(deviceId, key, select.value);
}

async function handleHardwareSwitchToggle(checkbox) {
    const deviceEl = checkbox.closest(".vh-device");
    if (!deviceEl) {
        return;
    }
    try {
        const resolvedId = await _resolveHardwareDeviceIdOrWarn(deviceEl.dataset.deviceId, "Toggle switch");
        if (!resolvedId) {
            checkbox.checked = !checkbox.checked;
            return;
        }
        const response = await client.hardwareSetSwitch(resolvedId, checkbox.checked ? 1 : 0);
        renderSnapshot(response);
    } catch (error) {
        checkbox.checked = !checkbox.checked;
        if (!(await _recoverUnknownHardwareDevice(error, "Toggle switch"))) {
            handleError(error, "Hardware update failed");
        }
    }
}

function bindVirtualHardwareUi() {
    _bindDragGlobal();
    const canvas = document.getElementById("hardware-canvas");
    const viewport = getHardwareViewport();
    scheduleHardwareViewportEnsure({ force: true, reason: "mount" });
    if (!canvas) {
        console.warn("Virtual hardware: #hardware-canvas not in DOM (canvas actions skipped)");
    } else {
        canvas.addEventListener("click", async (event) => {
            const deviceCard = event.target.closest(".vh-device");
            if (deviceCard?.dataset.deviceId) {
                _selectHardwareDevice(deviceCard.dataset.deviceId);
            }
            const inspect = event.target.closest("[data-inspect-device]");
            if (inspect?.dataset.inspectDevice) {
                _selectHardwareDevice(inspect.dataset.inspectDevice);
                return;
            }
            const btn = event.target.closest(".vh-remove");
            if (!btn) {
                return;
            }
            const deviceEl = btn.closest(".vh-device");
            if (!deviceEl?.dataset.deviceId) {
                return;
            }
            event.preventDefault();
            try {
                const resolvedId = await _resolveHardwareDeviceIdOrWarn(deviceEl.dataset.deviceId, "Remove device");
                if (!resolvedId) {
                    return;
                }
                const response = await client.hardwareRemoveDevice(resolvedId);
                renderSnapshot(response);
            } catch (error) {
                if (!(await _recoverUnknownHardwareDevice(error, "Remove device"))) {
                    handleError(error, "Remove device failed");
                }
            }
        });
        canvas.addEventListener("change", (event) => {
            if (event.target.matches("input[data-switch]")) {
                handleHardwareSwitchToggle(event.target);
            }
        });
    }
    viewport?.addEventListener("wheel", (event) => {
        event.preventDefault();
        const previousScale = hardwareState.view.scale;
        const nextScale = clamp(previousScale * (event.deltaY < 0 ? 1.1 : 0.9), HARDWARE_VIEW_MIN_SCALE, HARDWARE_VIEW_MAX_SCALE);
        const rect = viewport.getBoundingClientRect();
        const point = getClientPoint(event);
        const boardX = (point.x - rect.left - hardwareState.view.panX) / previousScale;
        const boardY = (point.y - rect.top - hardwareState.view.panY) / previousScale;
        hardwareState.view.scale = nextScale;
        hardwareState.view.panX = (point.x - rect.left) - (boardX * nextScale);
        hardwareState.view.panY = (point.y - rect.top) - (boardY * nextScale);
        applyHardwareBoardView();
    }, { passive: false });
    viewport?.addEventListener("pointerdown", (event) => {
        if (event.button !== 0 || event.target.closest(".vh-device,.vh-signal-node,.vh-port-node,.vh-palette-item,select,button,input")) {
            return;
        }
        const boardPoint = clientToHardwarePoint(event);
        if (event.altKey) {
            const point = getClientPoint(event);
            hardwareState.pan = {
                pointerId: event.pointerId,
                startX: point.x,
                startY: point.y,
                panX: hardwareState.view.panX,
                panY: hardwareState.view.panY,
            };
        } else {
            hardwareState.selection = {
                pointerId: event.pointerId,
                startPoint: boardPoint,
                currentPoint: boardPoint,
                additive: event.metaKey || event.ctrlKey,
            };
            _renderSelectionBox();
        }
        viewport.setPointerCapture?.(event.pointerId);
    });
    byId("vh-debug-panel")?.addEventListener("click", (event) => {
        const inspect = event.target.closest("[data-inspect-device]");
        if (!inspect?.dataset.inspectDevice) {
            const clear = event.target.closest("[data-clear-conn]");
            if (clear?.dataset.deviceId && clear.dataset.clearConn) {
                updateHardwareConnection(clear.dataset.deviceId, clear.dataset.clearConn, "");
            }
            return;
        }
        _selectHardwareDevice(inspect.dataset.inspectDevice);
    });
    byId("vh-debug-panel")?.addEventListener("change", (event) => {
        const select = event.target.closest("select.vh-conn");
        if (select) {
            handleHardwareConnectionChange(select);
            return;
        }
        const faultSelect = event.target.closest("select.vh-fault-select");
        if (faultSelect) {
            handleHardwareFaultChange(faultSelect);
        }
    });
    byId("vh_export_btn")?.addEventListener("click", async () => {
        try {
            const payload = await client.hardwareExport();
            const blob = new Blob([JSON.stringify(payload.hardware, null, 2)], { type: "application/json" });
            const url = URL.createObjectURL(blob);
            const anchor = document.createElement("a");
            anchor.href = url;
            anchor.download = "hexalogic-hardware.json";
            anchor.click();
            URL.revokeObjectURL(url);
        } catch (error) {
            handleError(error, "Hardware export failed");
        }
    });
    byId("vh_import_btn")?.addEventListener("click", () => {
        const input = document.createElement("input");
        input.type = "file";
        input.accept = "application/json,.json";
        input.addEventListener("change", async () => {
            const file = input.files?.[0];
            if (!file) {
                return;
            }
            try {
                const text = await file.text();
                const data = JSON.parse(text);
                const hwPayload = data.hardware !== undefined ? data.hardware : data;
                const response = await client.hardwareImport(hwPayload);
                renderSnapshot(response);
            } catch (error) {
                handleError(error, "Hardware import failed");
            }
        });
        input.click();
    });
    byId("vh_test_btn")?.addEventListener("click", async () => {
        await runHardwareValidationTests();
    });
    byId("vh_zoom_in")?.addEventListener("click", () => {
        hardwareState.view.scale = clamp(hardwareState.view.scale * 1.1, HARDWARE_VIEW_MIN_SCALE, HARDWARE_VIEW_MAX_SCALE);
        applyHardwareBoardView();
    });
    byId("vh_zoom_out")?.addEventListener("click", () => {
        hardwareState.view.scale = clamp(hardwareState.view.scale * 0.9, HARDWARE_VIEW_MIN_SCALE, HARDWARE_VIEW_MAX_SCALE);
        applyHardwareBoardView();
    });
    byId("vh_zoom_reset")?.addEventListener("click", () => {
        hardwareState.viewportNeedsFit = true;
        scheduleHardwareViewportEnsure({ force: true, reason: "reset" });
    });
    viewport?.addEventListener("pointerleave", () => {
        if (!hardwareState.connectionDrag) {
            _highlightConnector(null);
        }
    });
    byId("vh-logic-analyzer")?.addEventListener("wheel", (event) => {
        event.preventDefault();
        if (event.shiftKey) {
            hardwareState.analyzer.offsetMs = Math.max(0, (hardwareState.analyzer.offsetMs || 0) + (event.deltaY * 0.15));
        } else {
            hardwareState.analyzer.zoom = clamp((hardwareState.analyzer.zoom || 1) * (event.deltaY < 0 ? 1.15 : 0.87), 1, 12);
        }
        _renderLogicAnalyzer(appState.snapshot?.hardware);
    }, { passive: false });
    byId("vh-logic-analyzer")?.addEventListener("pointermove", (event) => {
        const canvasNode = byId("vh-logic-analyzer");
        if (!canvasNode) {
            return;
        }
        const rect = canvasNode.getBoundingClientRect();
        hardwareState.analyzer.cursorX = event.clientX - rect.left;
        _renderLogicAnalyzer(appState.snapshot?.hardware);
    });
    byId("vh-logic-analyzer")?.addEventListener("pointerleave", () => {
        hardwareState.analyzer.cursorX = null;
        _renderLogicAnalyzer(appState.snapshot?.hardware);
    });
    document.addEventListener("pointerdown", (event) => {
        const signalNode = event.target.closest(".vh-signal-node");
        if (signalNode) {
            _beginConnectionDrag(event, {
                sourceType: "signal",
                signalId: signalNode.dataset.signalId,
                kind: signalNode.dataset.signalKind,
            });
            return;
        }
        const portNode = event.target.closest(".vh-port-node");
        if (portNode) {
            _beginConnectionDrag(event, {
                sourceType: "device",
                deviceId: portNode.dataset.deviceId,
                connectionKey: portNode.dataset.connectionKey,
                kind: portNode.dataset.connectionKind,
                nodeId: portNode.dataset.nodeId,
            });
            return;
        }
        const paletteItem = event.target.closest(".vh-palette-item");
        if (!paletteItem?.dataset.deviceType || event.button !== 0) {
            return;
        }
        const ghost = document.createElement("div");
        ghost.className = "vh-palette-ghost";
        ghost.textContent = paletteItem.querySelector("strong")?.textContent || paletteItem.dataset.deviceType;
        document.body.appendChild(ghost);
        hardwareState.paletteDrag = {
            pointerId: event.pointerId,
            deviceType: paletteItem.dataset.deviceType,
            ghost,
        };
        const point = getClientPoint(event);
        ghost.style.transform = `translate3d(${point.x + 14}px, ${point.y + 14}px, 0)`;
    });
    document.addEventListener("pointermove", (event) => {
        if (hardwareState.connectionDrag) {
            const point = clientToHardwarePoint(event);
            const nearest = _nearestCompatibleConnector(point);
            hardwareState.connectionDrag.hover = nearest;
            hardwareState.connectionDrag.currentPoint = nearest?.center || point;
            _highlightConnector(nearest?.key || null);
            scheduleHardwareWireRender();
        } else if (!hardwareState.selection && !hardwareState.pan && getHardwareViewport()?.contains(event.target)) {
            const nearest = findNearestConnector(clientToHardwarePoint(event), () => true, 14);
            _highlightConnector(nearest?.key || null);
        }
        if (hardwareState.pan && hardwareState.pan.pointerId === event.pointerId) {
            const point = getClientPoint(event);
            hardwareState.view.panX = hardwareState.pan.panX + (point.x - hardwareState.pan.startX);
            hardwareState.view.panY = hardwareState.pan.panY + (point.y - hardwareState.pan.startY);
            applyHardwareBoardView();
        }
        if (hardwareState.selection && hardwareState.selection.pointerId === event.pointerId) {
            hardwareState.selection.currentPoint = clientToHardwarePoint(event);
            _renderSelectionBox();
        }
        if (hardwareState.paletteDrag && hardwareState.paletteDrag.pointerId === event.pointerId) {
            const point = getClientPoint(event);
            hardwareState.paletteDrag.ghost.style.transform = `translate3d(${point.x + 14}px, ${point.y + 14}px, 0)`;
        }
    });
    document.addEventListener("pointerup", async (event) => {
        if (hardwareState.connectionDrag && hardwareState.connectionDrag.pointerId === event.pointerId) {
            try {
                await _completeConnectionDrag(hardwareState.connectionDrag.hover?.node || document.elementFromPoint(event.clientX, event.clientY)?.closest(".vh-signal-node,.vh-port-node"));
            } catch (error) {
                handleError(error, "Hardware connection failed");
            } finally {
                hardwareState.connectionDrag = null;
                _highlightConnector(null);
                scheduleHardwareWireRender();
            }
        }
        if (hardwareState.pan && hardwareState.pan.pointerId === event.pointerId) {
            hardwareState.pan = null;
            _saveHardwareLayoutLocal();
        }
        if (hardwareState.selection && hardwareState.selection.pointerId === event.pointerId) {
            const rect = _selectionRect();
            const additive = hardwareState.selection.additive;
            const isClick = !rect || (rect.width < 6 && rect.height < 6);
            if (isClick) {
                if (!additive) {
                    _setSelectedDevices([], null);
                }
            } else {
                const selectedIds = _selectedIdsInRect(rect);
                const merged = additive ? Array.from(new Set([...hardwareState.selectedIds, ...selectedIds])) : selectedIds;
                _setSelectedDevices(merged, merged[0] || null);
            }
            hardwareState.selection = null;
            _clearSelectionBox();
        }
        if (hardwareState.paletteDrag && hardwareState.paletteDrag.pointerId === event.pointerId) {
            const drag = hardwareState.paletteDrag;
            const ghost = drag.ghost;
            const viewportRect = getHardwareViewport()?.getBoundingClientRect();
            const insideViewport = viewportRect
                && event.clientX >= viewportRect.left
                && event.clientX <= viewportRect.right
                && event.clientY >= viewportRect.top
                && event.clientY <= viewportRect.bottom;
            hardwareState.paletteDrag = null;
            ghost?.remove();
            if (insideViewport) {
                try {
                    await createHardwareDeviceAt(drag.deviceType, clientToHardwarePoint(event));
                } catch (error) {
                    handleError(error, "Add device failed");
                }
            }
        }
    });
}

function getClientPoint(event) {
    if (event.touches && event.touches[0]) {
        return { x: event.touches[0].clientX, y: event.touches[0].clientY };
    }
    if (event.changedTouches && event.changedTouches[0]) {
        return { x: event.changedTouches[0].clientX, y: event.changedTouches[0].clientY };
    }
    return { x: event.clientX, y: event.clientY };
}

function parseNumeric(value) {
    const text = String(value || "").trim().toUpperCase();
    if (text.endsWith("H")) {
        return Number.parseInt(text.slice(0, -1), 16);
    }
    if (text.startsWith("0X")) {
        return Number.parseInt(text, 16);
    }
    if (text.startsWith("0B")) {
        return Number.parseInt(text.slice(2), 2);
    }
    return Number.parseInt(text, 10);
}

function setStatus(message, isError = false) {
    const node = byId("status-text");
    if (!node) {
        return;
    }
    node.textContent = message;
    node.classList.toggle("status-error", Boolean(isError));
}

function setStatusExtra(message) {
    const node = byId("status-extra");
    if (node) {
        node.textContent = message;
    }
}

function pushToast(message, level = "info", timeoutMs = 2800) {
    const host = byId("toast-stack");
    if (!host) {
        return;
    }
    const toast = document.createElement("div");
    toast.className = `toast toast-${level}`;
    toast.innerHTML = `<div class="toast-title">${escapeHtml(level.toUpperCase())}</div><div class="toast-copy">${escapeHtml(message)}</div>`;
    host.appendChild(toast);
    window.requestAnimationFrame(() => toast.classList.add("is-visible"));
    window.setTimeout(() => {
        toast.classList.remove("is-visible");
        window.setTimeout(() => toast.remove(), 220);
    }, timeoutMs);
}

function setTheme(theme) {
    appState.theme = theme === "dark" ? "dark" : "light";
    document.body.setAttribute("data-theme", appState.theme);
    localStorage.setItem("sim-theme", appState.theme);
    const toggle = byId("theme_toggle");
    if (toggle) {
        toggle.textContent = appState.theme === "dark" ? "Light Mode" : "Dark Mode";
    }
    if (appState.monaco) {
        appState.monaco.editor.setTheme(appState.theme === "dark" ? "vs-dark" : "vs");
    }
}

function mergedSnapshot(snapshot) {
    const incoming = snapshot || {};
    const merged = { ...(appState.snapshot || {}), ...incoming };
    if (!("hardware" in incoming) && appState.snapshot?.hardware) {
        merged.hardware = appState.snapshot.hardware;
    }
    return merged;
}

function setLoaderProgress(value) {
    animateLoaderProgress(byId("loader-progress"), value);
}

function clearConsole() {
    safeSetHTML("debug-console", "");
    debugConsoleState.lastMessage = null;
    debugConsoleState.lastLevel = null;
    debugConsoleState.repeatCount = 0;
    debugConsoleState.lastNode = null;
    const controller = smartScrollControllers.get("debug-console");
    if (controller) {
        controller.unseenCount = 0;
        hideNewLogsIndicator(controller);
    }
}

function isNearBottom(container) {
    if (!container) {
        return true;
    }
    return (container.scrollHeight - container.scrollTop - container.clientHeight) < SMART_SCROLL_THRESHOLD_PX;
}

function updateAutoScrollToggleLabel(controller) {
    if (!controller?.toggleButton) {
        return;
    }
    controller.toggleButton.textContent = `Auto-scroll: ${controller.manualEnabled ? "ON" : "OFF"}`;
}

function hideNewLogsIndicator(controller) {
    if (!controller?.indicatorButton) {
        return;
    }
    controller.indicatorButton.hidden = true;
}

function showNewLogsIndicator(controller) {
    if (!controller?.indicatorButton || controller.unseenCount <= 0 || controller.autoScrollEnabled) {
        hideNewLogsIndicator(controller);
        return;
    }
    controller.indicatorButton.hidden = false;
    controller.indicatorButton.textContent = `⬇ New logs (${controller.unseenCount}) - Click to jump`;
}

function updateSmartScrollFromPosition(controller) {
    if (!controller?.container) {
        return;
    }
    const atBottom = isNearBottom(controller.container);
    controller.autoScrollEnabled = controller.manualEnabled && atBottom;
    if (atBottom) {
        controller.unseenCount = 0;
        hideNewLogsIndicator(controller);
    } else {
        showNewLogsIndicator(controller);
    }
}

function requestScrollToBottom(controller) {
    if (!controller?.container) {
        return;
    }
    if (controller.scrollRafId) {
        return;
    }
    controller.scrollRafId = window.requestAnimationFrame(() => {
        controller.scrollRafId = 0;
        controller.container.scrollTop = controller.container.scrollHeight;
    });
}

function highlightNewEntry(el) {
    if (!el) {
        return;
    }
    el.classList.add("new-entry");
    window.setTimeout(() => el.classList.remove("new-entry"), NEW_ENTRY_HIGHLIGHT_MS);
}

function jumpToBottom(controller) {
    if (!controller?.container) {
        return;
    }
    controller.container.scrollTop = controller.container.scrollHeight;
    controller.manualEnabled = true;
    controller.autoScrollEnabled = true;
    controller.unseenCount = 0;
    updateAutoScrollToggleLabel(controller);
    hideNewLogsIndicator(controller);
}

function notifySmartScrollAppend(controller, newCount, newNodes = []) {
    if (!controller || newCount <= 0) {
        return;
    }
    if (controller.autoScrollEnabled) {
        requestScrollToBottom(controller);
        return;
    }
    controller.unseenCount += newCount;
    newNodes.forEach((node) => highlightNewEntry(node));
    showNewLogsIndicator(controller);
}

function ensureSmartScrollController({
    panelId,
    containerId,
    indicatorClass = "",
}) {
    const container = byId(containerId);
    const panel = document.querySelector(`.dock-panel[data-panel="${panelId}"]`);
    if (!container || !panel) {
        return null;
    }

    const existing = smartScrollControllers.get(containerId);
    if (existing && existing.container === container) {
        return existing;
    }
    if (existing) {
        existing.container?.removeEventListener("scroll", existing.onScroll);
        existing.toggleButton?.removeEventListener("click", existing.onToggleClick);
        existing.toggleButton?.removeEventListener("pointerdown", existing.stopPointerDown);
        existing.indicatorButton?.removeEventListener("click", existing.onJumpClick);
        existing.indicatorButton?.removeEventListener("pointerdown", existing.stopPointerDown);
        if (existing.scrollRafId) {
            window.cancelAnimationFrame(existing.scrollRafId);
        }
    }

    const title = panel.querySelector(".panel-title");
    if (!title) {
        return null;
    }
    title.classList.add("panel-title-row");

    let toggleButton = title.querySelector(`[data-smart-scroll-toggle="${containerId}"]`);
    if (!toggleButton) {
        toggleButton = document.createElement("button");
        toggleButton.type = "button";
        toggleButton.className = "tool-btn tiny smart-scroll-toggle";
        toggleButton.dataset.smartScrollToggle = containerId;
        title.appendChild(toggleButton);
    }

    let indicatorButton = panel.querySelector(`[data-smart-scroll-indicator="${containerId}"]`);
    if (!indicatorButton) {
        indicatorButton = document.createElement("button");
        indicatorButton.type = "button";
        indicatorButton.className = `smart-scroll-indicator ${indicatorClass}`.trim();
        indicatorButton.dataset.smartScrollIndicator = containerId;
        indicatorButton.hidden = true;
        panel.appendChild(indicatorButton);
    }

    const controller = {
        panel,
        container,
        toggleButton,
        indicatorButton,
        manualEnabled: true,
        autoScrollEnabled: true,
        unseenCount: 0,
        scrollRafId: 0,
        stopPointerDown: (event) => {
            event.stopPropagation();
        },
        onScroll: () => {
            updateSmartScrollFromPosition(controller);
        },
        onToggleClick: (event) => {
            event.preventDefault();
            event.stopPropagation();
            controller.manualEnabled = !controller.manualEnabled;
            if (controller.manualEnabled && isNearBottom(controller.container)) {
                controller.autoScrollEnabled = true;
            } else {
                controller.autoScrollEnabled = false;
            }
            updateAutoScrollToggleLabel(controller);
            if (controller.autoScrollEnabled) {
                jumpToBottom(controller);
            } else {
                showNewLogsIndicator(controller);
            }
        },
        onJumpClick: (event) => {
            event.preventDefault();
            event.stopPropagation();
            jumpToBottom(controller);
        },
    };

    toggleButton.addEventListener("pointerdown", controller.stopPointerDown);
    toggleButton.addEventListener("click", controller.onToggleClick);
    indicatorButton.addEventListener("pointerdown", controller.stopPointerDown);
    indicatorButton.addEventListener("click", controller.onJumpClick);
    container.addEventListener("scroll", controller.onScroll, { passive: true });
    updateAutoScrollToggleLabel(controller);
    updateSmartScrollFromPosition(controller);
    smartScrollControllers.set(containerId, controller);
    return controller;
}

function shouldAutoFollowScroll(node) {
    if (!node) {
        return false;
    }
    return isNearBottom(node);
}

function logConsole(message, level = "info") {
    const node = byId("debug-console");
    if (!node) {
        return;
    }
    const scrollController = ensureSmartScrollController({
        panelId: "debugger",
        containerId: "debug-console",
        indicatorClass: "for-debugger",
    });
    const follow = scrollController ? scrollController.autoScrollEnabled : shouldAutoFollowScroll(node);
    if (debugConsoleState.lastNode && debugConsoleState.lastMessage === message && debugConsoleState.lastLevel === level) {
        debugConsoleState.repeatCount += 1;
        debugConsoleState.lastNode.textContent = `${message} [x${debugConsoleState.repeatCount}]`;
        if (follow) {
            if (scrollController) {
                requestScrollToBottom(scrollController);
            } else {
                node.scrollTop = node.scrollHeight;
            }
        } else if (scrollController) {
            notifySmartScrollAppend(scrollController, 1, [debugConsoleState.lastNode]);
        }
        return;
    }
    const line = document.createElement("div");
    line.className = `debug-line ${level}`;
    line.textContent = message;
    node.appendChild(line);
    while (node.childElementCount > DEBUG_CONSOLE_MAX_LINES) {
        node.firstElementChild?.remove();
    }
    debugConsoleState.lastMessage = message;
    debugConsoleState.lastLevel = level;
    debugConsoleState.repeatCount = 1;
    debugConsoleState.lastNode = line;
    if (follow) {
        if (scrollController) {
            requestScrollToBottom(scrollController);
        } else {
            node.scrollTop = node.scrollHeight;
        }
    } else if (scrollController) {
        notifySmartScrollAppend(scrollController, 1, [line]);
    }
}

function clearError() {
    const node = byId("error-box");
    if (node) {
        node.hidden = true;
        node.textContent = "No errors.";
    }
    if (appState.monaco && appState.editor) {
        appState.monaco.editor.setModelMarkers(appState.editor.getModel(), "hexlogic", []);
    }
}

function showError(message, line = null) {
    const node = byId("error-box");
    if (!node) {
        return;
    }
    node.hidden = false;
    node.textContent = line ? `${message} (line ${line})` : message;
    if (appState.monaco && appState.editor && line) {
        appState.monaco.editor.setModelMarkers(appState.editor.getModel(), "hexlogic", [
            {
                startLineNumber: line,
                endLineNumber: line,
                startColumn: 1,
                endColumn: 160,
                message,
                severity: appState.monaco.MarkerSeverity.Error,
            },
        ]);
    }
}

async function loadMonaco() {
    if (window.monaco) {
        return window.monaco;
    }
    if (typeof window.require === "undefined") {
        console.error("Monaco loader not available");
        return null;
    }
    if (typeof window.require.config !== "function") {
        console.error("Monaco AMD loader is missing require.config");
        return null;
    }
    const vsBase = (window.HEXLOGIC_MONACO_BASE || "https://unpkg.com/monaco-editor@0.45.0/min/").replace(/\/+$/, "");
    window.MonacoEnvironment = {
        getWorkerUrl() {
            const source = `self.MonacoEnvironment={baseUrl:'${vsBase}/'};importScripts('${vsBase}/vs/base/worker/workerMain.js');`;
            return `data:text/javascript;charset=utf-8,${encodeURIComponent(source)}`;
        },
    };
    return new Promise((resolve) => {
        window.require(["vs/editor/editor.main"], () => {
            resolve(window.monaco || null);
        }, (error) => {
            console.error("Failed to load Monaco editor", error);
            resolve(null);
        });
    });
}

function createFallbackEditor() {
    const host = byId("editor-host");
    if (!host) {
        console.error("Editor host missing; fallback editor cannot be created.");
        return;
    }
    host.innerHTML = "";
    const textarea = document.createElement("textarea");
    textarea.className = "editor-fallback";
    textarea.spellcheck = false;
    textarea.value = DEFAULT_SOURCE[appState.architecture];
    host.appendChild(textarea);

    const model = {
        setValue(value) {
            textarea.value = value;
        },
        getValue() {
            return textarea.value;
        },
    };

    appState.editor = {
        getValue() {
            return textarea.value;
        },
        getModel() {
            return model;
        },
        getPosition() {
            const head = textarea.selectionStart || 0;
            const lineNumber = textarea.value.slice(0, head).split("\n").length;
            return { lineNumber };
        },
        getDomNode() {
            return host;
        },
        deltaDecorations() {
            return [];
        },
        onMouseDown() {
            return { dispose() {} };
        },
    };
    host.dataset.editorMode = "fallback";
    logConsole("Monaco unavailable. Using textarea fallback editor.", "warn");
}

function registerEditorLanguage(monaco) {
    monaco.languages.register({ id: "hexlogic-asm" });
    monaco.languages.setMonarchTokensProvider("hexlogic-asm", {
        ignoreCase: true,
        tokenizer: {
            root: [
                [/;.*$/, "comment"],
                [/^\s*[A-Za-z_][A-Za-z0-9_]*:/, "type.identifier"],
                [/\b(?:ORG|END|DB|MOV|MOVX|MOVC|PUSH|POP|SETB|CLR|CPL|INC|DEC|ADD|ADDC|SUBB|ANL|ORL|XRL|RL|RR|RLC|RRC|SWAP|MUL|DIV|DJNZ|CJNE|JZ|JNZ|JC|JNC|SJMP|AJMP|LJMP|ACALL|LCALL|RET|RETI|JB|JNB|JBC|NOP|LDR|STR|B|SUB)\b/, "keyword"],
                [/\b(?:R1[0-5]|R[0-9]|A|AB|C|DPTR|SP|ACC|PSW|B|P[0-3]|IE|IP|TCON|TMOD|TH0|TL0|TH1|TL1|SCON|SBUF|DPL|DPH|PC|LR)\b/, "variable"],
                [/#?[0-9A-F]+H\b/, "number.hex"],
                [/#?0x[0-9a-f]+\b/, "number.hex"],
                [/#?0b[01]+\b/, "number.binary"],
                [/#?-?[0-9]+\b/, "number"],
                [/\[[^\]]+\]/, "delimiter.square"],
            ],
        },
    });
}

function createEditor(monaco) {
    const host = byId("editor-host");
    if (!host) {
        throw new Error("Editor host missing; Monaco editor cannot initialize.");
    }
    registerEditorLanguage(monaco);
    appState.monaco = monaco;
    appState.editor = monaco.editor.create(host, {
        language: "hexlogic-asm",
        value: DEFAULT_SOURCE[appState.architecture],
        theme: appState.theme === "dark" ? "vs-dark" : "vs",
        automaticLayout: true,
        glyphMargin: true,
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        smoothScrolling: true,
        fontFamily: "JetBrains Mono, Fira Code, monospace",
        fontSize: 14,
        lineNumbersMinChars: 3,
        roundedSelection: false,
        tabSize: 4,
    });
    appState.editor.onMouseDown((event) => {
        if (event.target.type !== monaco.editor.MouseTargetType.GUTTER_GLYPH_MARGIN) {
            return;
        }
        toggleBreakpoint(event.target.position.lineNumber).catch((error) => handleError(error, "Breakpoint update failed"));
    });
}

function updateBreakpointDecorations() {
    if (!appState.editor || !appState.monaco) {
        return;
    }
    const decorations = Array.from(appState.breakpoints).map((line) => ({
        range: new appState.monaco.Range(line, 1, line, 1),
        options: {
            glyphMarginClassName: "hexlogic-breakpoint-glyph",
            glyphMarginHoverMessage: { value: `Breakpoint on line ${line}` },
        },
    }));
    appState.breakpointDecorations = appState.editor.deltaDecorations(appState.breakpointDecorations, decorations);
}

function updateExecutionDecorations(snapshot, { pulse = false } = {}) {
    if (!appState.editor || !appState.monaco) {
        return;
    }
    const pc = Number(snapshot?.registers?.PC ?? 0);
    const line = appState.listingByAddress.get(pc) || null;
    if (!line) {
        appState.executionDecorations = appState.editor.deltaDecorations(appState.executionDecorations, []);
        appState.activeExecutionLine = null;
        return;
    }

    appState.executionDecorations = appState.editor.deltaDecorations(appState.executionDecorations, [
        {
            range: new appState.monaco.Range(line, 1, line, 1),
            options: {
                isWholeLine: true,
                className: "hexlogic-execution-line",
                glyphMarginClassName: "hexlogic-execution-glyph",
                glyphMarginHoverMessage: { value: `Current execution line: ${line}` },
            },
        },
    ]);
    appState.activeExecutionLine = line;

    if (appState.running) {
        return;
    }
    if (typeof appState.editor.revealLineInCenterIfOutsideViewport === "function") {
        appState.editor.revealLineInCenterIfOutsideViewport(line, appState.monaco.editor.ScrollType.Immediate);
    } else if (typeof appState.editor.revealLineInCenter === "function") {
        appState.editor.revealLineInCenter(line);
    }
}

function renderBreakpointSummary() {
    const node = byId("breakpoint-list");
    if (!node) {
        return;
    }
    if (!appState.breakpoints.size) {
        node.textContent = "None";
        return;
    }
    node.textContent = Array.from(appState.breakpoints)
        .sort((left, right) => left - right)
        .map((line) => `L${line}`)
        .join(", ");
}

function lineToProgramCounters(line) {
    return appState.listingByLine.get(line) || [];
}

async function syncBreakpoints() {
    const pcs = [];
    for (const line of appState.breakpoints) {
        pcs.push(...lineToProgramCounters(line));
    }
    await client.setBreakpoints(Array.from(new Set(pcs)));
    updateBreakpointDecorations();
    renderBreakpointSummary();
}

async function toggleBreakpoint(line) {
    if (appState.breakpoints.has(line)) {
        appState.breakpoints.delete(line);
    } else {
        appState.breakpoints.add(line);
    }
    updateBreakpointDecorations();
    renderBreakpointSummary();
    if (appState.assembled) {
        await syncBreakpoints();
    }
}

function getColumns() {
    return Array.from(document.querySelectorAll(".panel-column"));
}

function getWorkspace() {
    return byId("workspace");
}

function focusPanel(panel) {
    if (!panel) {
        return;
    }
    document.querySelectorAll(".dock-panel").forEach((node) => node.classList.remove("is-focused"));
    panel.classList.add("is-focused");
    uiState.activePanelId = panel.dataset.panel || null;
}

function setPanelFlexHeight(panel, heightPx) {
    panel.style.flexBasis = `${Math.round(heightPx)}px`;
    panel.style.flexGrow = "0";
    panel.style.flexShrink = "0";
}

function clearRowResizers(column) {
    column.querySelectorAll(".row-resizer").forEach((resizer) => resizer.remove());
}

function startRowResize(event, resizer) {
    const previousPanel = resizer.previousElementSibling;
    const nextPanel = resizer.nextElementSibling;
    if (!previousPanel || !nextPanel || !previousPanel.classList.contains("dock-panel") || !nextPanel.classList.contains("dock-panel")) {
        return;
    }

    event.preventDefault();
    resizer.classList.add("is-dragging");
    document.body.style.cursor = "row-resize";
    if (event.pointerId && typeof resizer.setPointerCapture === "function") {
        resizer.setPointerCapture(event.pointerId);
    }

    const startY = getClientPoint(event).y;
    const previousHeight = previousPanel.getBoundingClientRect().height;
    const nextHeight = nextPanel.getBoundingClientRect().height;

    const onMove = rafThrottle((moveEvent) => {
        const { previous, next } = resolveVerticalSplit(
            previousHeight,
            nextHeight,
            getClientPoint(moveEvent).y - startY,
            MIN_PANEL_HEIGHT,
        );
        setPanelFlexHeight(previousPanel, previous);
        setPanelFlexHeight(nextPanel, next);
    });

    function onUp() {
        resizer.classList.remove("is-dragging");
        document.body.style.cursor = "";
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("mousemove", onMove);
        persistPanelLayout();
    }

    window.addEventListener("pointermove", onMove);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("pointerup", onUp, { once: true });
    window.addEventListener("mouseup", onUp, { once: true });
}

function buildRowResizers() {
    getColumns().forEach((column) => {
        clearRowResizers(column);
        const panels = Array.from(column.querySelectorAll(".dock-panel"));
        if (panels.length < 2) {
            return;
        }
        for (let index = 0; index < panels.length - 1; index += 1) {
            const resizer = document.createElement("div");
            resizer.className = "row-resizer";
            resizer.setAttribute("role", "separator");
            resizer.setAttribute("aria-orientation", "horizontal");
            resizer.setAttribute("title", "Resize stacked panels");
            if (window.PointerEvent) {
                resizer.addEventListener("pointerdown", (resizeEvent) => startRowResize(resizeEvent, resizer));
            } else {
                resizer.addEventListener("mousedown", (resizeEvent) => startRowResize(resizeEvent, resizer));
                resizer.addEventListener("touchstart", (resizeEvent) => startRowResize(resizeEvent, resizer), { passive: false });
            }
            panels[index].after(resizer);
        }
    });
}

function gatherPanelOrder(selector) {
    const column = document.querySelector(selector);
    if (!column) {
        return [];
    }
    return Array.from(column.querySelectorAll(".dock-panel"))
        .map((panel) => panel.dataset.panel)
        .filter(Boolean);
}

function persistPanelLayout() {
    const workspace = byId("workspace");
    if (!workspace) {
        return;
    }
    const layout = {
        columns: {
            left: gatherPanelOrder(".left-pane"),
            center: gatherPanelOrder(".center-pane"),
            right: gatherPanelOrder(".right-pane"),
        },
        widths: {
            left: workspace.style.getPropertyValue("--left-col") || "",
            right: workspace.style.getPropertyValue("--right-col") || "",
        },
        heights: {},
    };
    document.querySelectorAll(".dock-panel").forEach((panel) => {
        const id = panel.dataset.panel;
        if (!id) {
            return;
        }
        if (panel.style.flexBasis) {
            layout.heights[id] = panel.style.flexBasis;
        }
    });
    localStorage.setItem(PANEL_LAYOUT_KEY, JSON.stringify(layout));
}

function applyPanelOrder(columnName, orderList, assigned) {
    const column = document.querySelector(`.${columnName}-pane`);
    if (!column || !Array.isArray(orderList)) {
        return;
    }
    orderList.forEach((panelId) => {
        const panel = document.querySelector(`.dock-panel[data-panel="${panelId}"]`);
        if (!panel) {
            return;
        }
        column.appendChild(panel);
        assigned.add(panelId);
    });
}

function restorePanelLayout() {
    const raw = localStorage.getItem(PANEL_LAYOUT_KEY);
    if (!raw) {
        return;
    }

    let parsed;
    try {
        parsed = JSON.parse(raw);
    } catch {
        return;
    }
    if (!parsed || typeof parsed !== "object") {
        return;
    }

    const assigned = new Set();
    applyPanelOrder("left", parsed.columns?.left, assigned);
    applyPanelOrder("center", parsed.columns?.center, assigned);
    applyPanelOrder("right", parsed.columns?.right, assigned);

    const centerColumn = document.querySelector(".center-pane");
    document.querySelectorAll(".dock-panel").forEach((panel) => {
        const id = panel.dataset.panel;
        if (centerColumn && id && !assigned.has(id)) {
            centerColumn.appendChild(panel);
        }
    });

    const workspace = byId("workspace");
    if (workspace) {
        if (parsed.widths?.left) {
            workspace.style.setProperty("--left-col", parsed.widths.left);
        }
        if (parsed.widths?.right) {
            workspace.style.setProperty("--right-col", parsed.widths.right);
        }
    }

    if (parsed.heights && typeof parsed.heights === "object") {
        Object.entries(parsed.heights).forEach(([panelId, basis]) => {
            const panel = document.querySelector(`.dock-panel[data-panel="${panelId}"]`);
            if (!panel || !basis) {
                return;
            }
            panel.style.flexBasis = basis;
            panel.style.flexGrow = "0";
            panel.style.flexShrink = "0";
        });
    }
}

function setupColumnResizers() {
    const workspace = byId("workspace");
    const leftPane = workspace?.querySelector(".left-pane");
    const rightPane = workspace?.querySelector(".right-pane");
    if (!workspace || !leftPane || !rightPane) {
        return;
    }

    document.querySelectorAll(".col-resizer").forEach((resizer) => {
        const startResize = (event) => {
            event.preventDefault();
            resizer.classList.add("is-dragging");
            document.body.style.cursor = "col-resize";
            if (event.pointerId && typeof resizer.setPointerCapture === "function") {
                resizer.setPointerCapture(event.pointerId);
            }

            const startX = getClientPoint(event).x;
            const workspaceRect = workspace.getBoundingClientRect();
            const leftStart = leftPane.getBoundingClientRect().width;
            const rightStart = rightPane.getBoundingClientRect().width;
            const which = resizer.dataset.resizer;

            const onMove = rafThrottle((moveEvent) => {
                const delta = getClientPoint(moveEvent).x - startX;
                if (which === "left") {
                    workspace.style.setProperty("--left-col", `${resolveLeftColumnWidth({
                        leftStart,
                        delta,
                        workspaceWidth: workspaceRect.width,
                        rightWidth: rightPane.getBoundingClientRect().width,
                        minLeft: MIN_LEFT_WIDTH,
                        minCenter: MIN_CENTER_WIDTH,
                    })}px`);
                    return;
                }
                workspace.style.setProperty("--right-col", `${resolveRightColumnWidth({
                    rightStart,
                    delta,
                    workspaceWidth: workspaceRect.width,
                    leftWidth: leftPane.getBoundingClientRect().width,
                    minRight: MIN_RIGHT_WIDTH,
                    minCenter: MIN_CENTER_WIDTH,
                })}px`);
            });

            function onUp() {
                resizer.classList.remove("is-dragging");
                document.body.style.cursor = "";
                window.removeEventListener("pointermove", onMove);
                window.removeEventListener("mousemove", onMove);
                persistPanelLayout();
            }

            window.addEventListener("pointermove", onMove);
            window.addEventListener("mousemove", onMove);
            window.addEventListener("pointerup", onUp, { once: true });
            window.addEventListener("mouseup", onUp, { once: true });
        };

        if (window.PointerEvent) {
            resizer.addEventListener("pointerdown", startResize);
        } else {
            resizer.addEventListener("mousedown", startResize);
            resizer.addEventListener("touchstart", startResize, { passive: false });
        }
    });
}

function setupPanelInteractions() {
    document.querySelectorAll(".dock-panel").forEach((panel) => {
        if (panel.dataset.focusBound === "true") {
            return;
        }
        panel.dataset.focusBound = "true";
        panel.addEventListener("pointerdown", () => focusPanel(panel));
    });
}

function setupWorkspaceChrome() {
    const workspace = getWorkspace();
    if (!workspace || workspace.dataset.layoutReady === "true") {
        return;
    }
    workspace.dataset.layoutReady = "true";
    workspace.classList.remove("workspace-docking-ready");
    safeSetHidden("snap-preview", true);
    safeSetHidden("floating-layer", true);
    clearRowResizers(workspace);
    document.querySelectorAll(".col-resizer").forEach((resizer) => {
        resizer.setAttribute("hidden", "hidden");
        resizer.setAttribute("aria-hidden", "true");
    });
    setupPanelInteractions();
    refreshPanelRegistry();
}

function renderProjectTree(snapshot) {
    const architecture = snapshot?.architecture || appState.architecture || "8051";
    safeSetHTML("project-tree", `
        <li><span class="tree-folder">Target 1 (${architecture.toUpperCase()})</span></li>
        <li><span class="tree-file">startup.${architecture === "arm" ? "s" : "a51"}</span></li>
        <li><span class="tree-file active">main.asm</span></li>
        <li><span class="tree-file">call_stack</span></li>
        <li><span class="tree-file">trace</span></li>
    `);
}

function renderExecutionState(snapshot) {
    const registers = snapshot?.registers || {};
    safeSetHTML("exec-state-panel", `
        <div class="subpanel">
            <div class="subpanel-title">Execution State</div>
            <table class="keil-table compact">
                <tbody>
                    <tr><th>Architecture</th><td>${escapeHtml(snapshot?.architecture?.toUpperCase?.() || "UNKNOWN")}</td></tr>
                    <tr><th>Ready</th><td>${snapshot?.has_program ? "Yes" : "No"}</td></tr>
                    <tr><th>PC</th><td>${toHex(registers.PC ?? 0, snapshot?.architecture === "arm" ? 8 : 4)}</td></tr>
                    <tr><th>Cycles</th><td>${escapeHtml(String(snapshot?.cycles ?? 0))}</td></tr>
                    <tr><th>Clock</th><td>${escapeHtml(String(snapshot?.clock_hz ?? 0))}</td></tr>
                    <tr><th>Execution</th><td>${escapeHtml(String(snapshot?.execution_mode || "realtime").toUpperCase())}</td></tr>
                    <tr><th>Endian</th><td>${escapeHtml(snapshot?.architecture === "arm" ? (snapshot?.endian || "little") : "n/a")}</td></tr>
                    <tr><th>Debug</th><td>${snapshot?.debug_mode ? "On" : "Off"}</td></tr>
                    <tr><th>Last Interrupt</th><td>${escapeHtml(snapshot?.last_interrupt || "-")}</td></tr>
                </tbody>
            </table>
        </div>
    `);
}

function renderRegisters(snapshot) {
    const architecture = snapshot?.architecture || appState.architecture || "8051";
    const registerRows = Object.entries(snapshot?.registers || {})
        .map(([name, value]) => {
            const width = name === "PC" || name === "LR" || name === "SP" || name === "DPTR"
                ? (architecture === "arm" ? 8 : 4)
                : 2;
            return `<tr><th>${escapeHtml(name)}</th><td data-register="${escapeHtml(name)}">${toHex(value, width)}</td></tr>`;
        })
        .join("");
    const flagRows = Object.entries(snapshot?.flags || {})
        .map(([name, value]) => `<tr><th>${escapeHtml(name)}</th><td data-flag="${escapeHtml(name)}">${value ? "1" : "0"}</td></tr>`)
        .join("") || '<tr><td colspan="2" class="empty-output">No flags for this architecture.</td></tr>';
    const timerRows = Object.entries(snapshot?.timers || {})
        .map(([name, value]) => `<tr><th>${escapeHtml(name.toUpperCase())}</th><td>${escapeHtml(JSON.stringify(value))}</td></tr>`)
        .join("") || '<tr><td colspan="2" class="empty-output">No timer model.</td></tr>';
    const body = safeSetHTML("registers-panel-body", `
        <div class="subpanel">
            <div class="subpanel-title">Registers</div>
            <table class="keil-table compact"><tbody>${registerRows}</tbody></table>
        </div>
        <div class="subpanel">
            <div class="subpanel-title">Flags</div>
            <table class="keil-table compact"><tbody>${flagRows}</tbody></table>
        </div>
        <div class="subpanel">
            <div class="subpanel-title">Timers</div>
            <table class="keil-table compact"><tbody>${timerRows}</tbody></table>
        </div>
    `);
    domMaps.registers.clear();
    domMaps.flags.clear();
    if (!body) {
        return;
    }
    body.querySelectorAll("[data-register]").forEach((cell) => domMaps.registers.set(cell.dataset.register, cell));
    body.querySelectorAll("[data-flag]").forEach((cell) => domMaps.flags.set(cell.dataset.flag, cell));
}

function renderCallStack(snapshot) {
    const values = snapshot?.call_stack || [];
    const architecture = snapshot?.architecture || appState.architecture || "8051";
    safeSetHTML("call-stack-body", values.length
        ? `<ol class="call-stack-list">${values.map((value) => `<li>${toHex(value, architecture === "arm" ? 8 : 4)}</li>`).join("")}</ol>`
        : '<div class="empty-output">Call stack empty.</div>');
}

function buildMemoryTable(title, space, values, addressWidth) {
    const keys = Object.keys(values || {}).map((key) => Number(key)).sort((left, right) => left - right);
    if (!keys.length) {
        return `<div class="memory-window"><div class="subpanel-title">${title}</div><div class="empty-output">No data.</div></div>`;
    }
    const maxAddress = Math.max(...keys);
    const rows = [];
    for (let base = 0; base <= maxAddress; base += 16) {
        const cells = [];
        for (let offset = 0; offset < 16; offset += 1) {
            const address = base + offset;
            const value = values[address] ?? values[String(address)] ?? 0;
            cells.push(`<td data-space="${space}" data-address="${address}">${Number(value).toString(16).toUpperCase().padStart(2, "0")}</td>`);
        }
        rows.push(`<tr><th>${toHex(base, addressWidth)}</th>${cells.join("")}</tr>`);
    }
    return `
        <div class="memory-window">
            <div class="subpanel-title">${title}</div>
            <div class="memory-scroll">
                <table class="keil-table compact memory-table">
                    <thead><tr><th>Addr</th>${Array.from({ length: 16 }, (_, index) => `<th>${index.toString(16).toUpperCase()}</th>`).join("")}</tr></thead>
                    <tbody>${rows.join("")}</tbody>
                </table>
            </div>
        </div>
    `;
}

function renderMemory(snapshot) {
    const architecture = snapshot?.architecture || appState.architecture || "8051";
    const ramNode = safeSetHTML("memory-ram", buildMemoryTable(architecture === "8051" ? "Internal RAM" : "Register Shadow", "iram", snapshot?.iram || {}, 4));
    const xramNode = safeSetHTML("memory-xram", buildMemoryTable(architecture === "8051" ? "XRAM Sample" : "Data Memory", "xram", snapshot?.xram_sample || {}, 4));
    const romNode = safeSetHTML("memory-rom", buildMemoryTable("Code ROM", "code", snapshot?.rom || {}, architecture === "arm" ? 8 : 4));
    domMaps.memory.clear();
    domMaps.xram.clear();
    domMaps.code.clear();
    ramNode?.querySelectorAll("[data-space='iram'], [data-space='sfr']").forEach((cell) => {
        domMaps.memory.set(`${cell.dataset.space}:${cell.dataset.address}`, cell);
    });
    xramNode?.querySelectorAll("[data-space='xram']").forEach((cell) => {
        domMaps.xram.set(`xram:${cell.dataset.address}`, cell);
    });
    romNode?.querySelectorAll("[data-space='code']").forEach((cell) => {
        domMaps.code.set(`code:${cell.dataset.address}`, cell);
    });
}

function renderAssembler(snapshot) {
    const architecture = snapshot?.architecture || appState.architecture || "8051";
    const listing = snapshot?.program?.listing || [];
    appState.listingByLine = new Map();
    appState.listingByAddress = new Map();
    if (!listing.length) {
        safeSetHTML("assembler-panel-body", '<div class="empty-output">Assemble code to see machine code.</div>');
        updateExecutionDecorations({ registers: { PC: 0 } });
        return;
    }
    for (const row of listing) {
        const values = appState.listingByLine.get(row.line) || [];
        values.push(row.address);
        appState.listingByLine.set(row.line, values);
        appState.listingByAddress.set(row.address, row.line);
    }
    safeSetHTML("assembler-panel-body", `
        <div class="assembler-scroll">
            <table class="keil-table compact">
                <thead><tr><th>#</th><th>Line</th><th>Address</th><th>Source</th><th>Bytes</th></tr></thead>
                <tbody>
                    ${listing.map((row, index) => `
                        <tr data-asm-address="${row.address}">
                            <td>${index}</td>
                            <td>${row.line}</td>
                            <td>${toHex(row.address, architecture === "arm" ? 8 : 4)}</td>
                            <td>${escapeHtml(row.text)}</td>
                            <td>${row.bytes.map((value) => Number(value).toString(16).toUpperCase().padStart(2, "0")).join(" ")}</td>
                        </tr>
                    `).join("")}
                </tbody>
            </table>
        </div>
    `);
    updateBreakpointDecorations();
    updateExecutionDecorations(snapshot);
}

function renderTrace(snapshot) {
    appState.traceTimeline = normalizeTraceEntries(snapshot?.trace || []).slice(-TRACE_LIMIT);
    drawTraceTimeline();
}

function drawTraceTimeline() {
    const timeline = normalizeTraceEntries(appState.traceTimeline || []);
    appState.traceTimeline = timeline.slice(-TRACE_LIMIT);
    const node = byId("trace-panel-body");
    const scrollController = ensureSmartScrollController({
        panelId: "trace",
        containerId: "trace-panel-body",
    });
    const follow = scrollController ? scrollController.autoScrollEnabled : shouldAutoFollowScroll(node);
    const preservedScrollTop = node ? node.scrollTop : 0;
    if (!timeline.length) {
        safeSetHTML("trace-panel-body", '<div class="empty-output">Trace timeline is empty.</div>');
        if (scrollController) {
            scrollController.unseenCount = 0;
            hideNewLogsIndicator(scrollController);
        }
        return;
    }
    safeSetHTML("trace-panel-body", appState.traceTimeline
        .map((item) => renderTraceEntryMarkup(item))
        .join(""));
    if (!node) {
        return;
    }
    if (follow) {
        if (scrollController) {
            requestScrollToBottom(scrollController);
        } else {
            node.scrollTop = node.scrollHeight;
        }
    } else {
        node.scrollTop = Math.min(preservedScrollTop, Math.max(0, node.scrollHeight - node.clientHeight));
    }
}

function renderTraceEntryMarkup(item) {
    return `
        <div class="trace-entry">
            <div class="trace-head">
                <span>${toHex(item.pc, appState.architecture === "arm" ? 8 : 4)}</span>
                <span>${escapeHtml(item?.mnemonic ?? "UNKNOWN")}</span>
                <span>${Number(item?.cycles ?? 0)} cyc</span>
            </div>
            <div class="trace-sub">${item.text ? escapeHtml(item.text) : ""}</div>
            ${formatRegisterDiff(item.register_diff || {})}
        </div>
    `;
}

function createTraceEntryElement(item) {
    const wrapper = document.createElement("div");
    wrapper.innerHTML = renderTraceEntryMarkup(item).trim();
    return wrapper.firstElementChild;
}

function formatRegisterDiff(registerDiff) {
    const entries = Object.entries(isPlainObject(registerDiff) ? registerDiff : {});
    if (!entries.length) {
        return "";
    }
    return `<div class="trace-diff">${entries
        .slice(0, 6)
        .map(([name, change]) => {
            const before = isPlainObject(change) ? (change.before ?? 0) : 0;
            const after = isPlainObject(change) ? (change.after ?? 0) : 0;
            const width = name === "PC" || name === "LR" || name === "SP" || name === "DPTR"
                ? (appState.architecture === "arm" ? 8 : 4)
                : 2;
            return `${escapeHtml(name)}: ${toHex(before, width)} → ${toHex(after, width)}`;
        })
        .join(" | ")}</div>`;
}

function appendTraceEntries(entries) {
    const normalized = normalizeTraceEntries(entries || []);
    if (!normalized.length) {
        return;
    }
    appState.traceTimeline.push(...normalized);
    appState.traceTimeline = appState.traceTimeline.slice(-TRACE_LIMIT);
    const node = byId("trace-panel-body");
    if (!node) {
        drawTraceTimeline();
        return;
    }
    const scrollController = ensureSmartScrollController({
        panelId: "trace",
        containerId: "trace-panel-body",
    });
    const shouldScroll = scrollController ? scrollController.autoScrollEnabled : shouldAutoFollowScroll(node);
    if (node.querySelector(".empty-output")) {
        node.innerHTML = "";
    }
    const fragment = document.createDocumentFragment();
    const appendedNodes = [];
    normalized.forEach((item) => {
        const entryNode = createTraceEntryElement(item);
        if (entryNode) {
            fragment.appendChild(entryNode);
            appendedNodes.push(entryNode);
        }
    });
    node.appendChild(fragment);
    while (node.childElementCount > TRACE_LIMIT) {
        node.firstElementChild?.remove();
    }
    if (shouldScroll) {
        if (scrollController) {
            requestScrollToBottom(scrollController);
        } else {
            node.scrollTop = node.scrollHeight;
        }
    } else if (scrollController) {
        notifySmartScrollAppend(scrollController, appendedNodes.length, appendedNodes);
    }
}

function renderMetrics(snapshot, metricsPayload = null) {
    const metrics = metricsPayload?.metrics || {};
    const uiTiming = appState.uiTiming || {};
    const rows = {
        cycles: snapshot?.cycles ?? 0,
        clock_hz: snapshot?.clock_hz ?? 0,
        active_sessions: metrics.active_sessions ?? "-",
        api_requests: metrics.api_requests ?? "-",
        steps_per_second: metrics.steps_per_second ?? "-",
        estimated_session_bytes: metrics.estimated_session_bytes ?? "-",
        ui_channel: uiTiming.lastChannel || "-",
        ui_receive_to_paint_ms: uiTiming.lastReceiveToPaintMs == null ? "-" : uiTiming.lastReceiveToPaintMs.toFixed(3),
        ui_sync_render_ms: uiTiming.lastSyncRenderMs == null ? "-" : uiTiming.lastSyncRenderMs.toFixed(3),
        ui_round_trip_ms: uiTiming.lastRoundTripMs == null ? "-" : uiTiming.lastRoundTripMs.toFixed(3),
        ui_server_to_paint_ms: uiTiming.lastServerToPaintMs == null ? "-" : uiTiming.lastServerToPaintMs.toFixed(3),
        ui_last_frame_gap_ms: uiTiming.lastFrameGapMs == null ? "-" : uiTiming.lastFrameGapMs.toFixed(3),
        ui_dropped_frames: uiTiming.droppedFrames ?? 0,
        ui_max_receive_to_paint_ms: Number(uiTiming.maxReceiveToPaintMs || 0).toFixed(3),
        ui_samples: uiTiming.samples ?? 0,
    };
    safeSetHTML("metrics-panel-body", `
        <table class="keil-table compact">
            <tbody>
                ${Object.entries(rows).map(([key, value]) => `<tr><th>${escapeHtml(key)}</th><td>${escapeHtml(String(value))}</td></tr>`).join("")}
            </tbody>
        </table>
    `);
}

function _recordSignalEvents(events = []) {
    for (const event of events) {
        const name = String(event?.pin || "");
        if (!name) {
            continue;
        }
        const token = `${name}:${event?.cycle ?? event?.time_ms ?? "na"}:${event?.value ?? 0}`;
        if (hardwareState.seenSignalTokens.has(token)) {
            continue;
        }
        hardwareState.seenSignalTokens.add(token);
        hardwareState.signalTokenOrder.push(token);
        while (hardwareState.signalTokenOrder.length > 1024) {
            const oldest = hardwareState.signalTokenOrder.shift();
            if (oldest) {
                hardwareState.seenSignalTokens.delete(oldest);
            }
        }
        const history = hardwareState.signalHistory[name] || [];
        history.push(Number(event?.value || 0));
        hardwareState.signalHistory[name] = history.slice(-48);
    }
}

function _seedSignalHistoryFromHardware(snapshot) {
    const events = snapshot?.hardware?.debug?.signal_log || [];
    if (events.length) {
        _recordSignalEvents(events);
    }
}

function updateWaveform(snapshot) {
    _seedSignalHistoryFromHardware(snapshot);
    const channels = Object.entries(hardwareState.signalHistory)
        .filter(([, history]) => Array.isArray(history) && history.length && history.some((sample) => sample !== history[0]))
        .map(([name, history]) => ({ name, history }));
    safeSetText("wave-status", channels.length ? `${channels.length} active signal channel(s)` : "No waveform channels detected.");
    safeSetHidden("wave-empty", Boolean(channels.length));
    safeSetHTML("wave-list", channels
        .map((channel) => `
            <div class="wave-row">
                <div class="wave-label">${channel.name}</div>
                <svg viewBox="0 0 240 28" class="wave-canvas" aria-hidden="true">
                    <path d="${buildWavePath(channel.history)}" />
                </svg>
            </div>
        `)
        .join(""));
}

function buildWavePath(samples) {
    if (!samples.length) {
        return "";
    }
    const step = 240 / Math.max(samples.length - 1, 1);
    const high = 6;
    const low = 22;
    let path = `M 0 ${samples[0] ? high : low}`;
    for (let index = 1; index < samples.length; index += 1) {
        const x = index * step;
        const previousY = samples[index - 1] ? high : low;
        const nextY = samples[index] ? high : low;
        path += ` L ${x} ${previousY} L ${x} ${nextY}`;
    }
    return path;
}

function flashCell(node, className = "cell-updated") {
    if (!node) {
        return;
    }
    pulseClass(node, className);
}

function applyDiff(snapshot, diff, previousSnapshot = null) {
    if (!snapshot || !diff) {
        return;
    }
    for (const [name, pair] of Object.entries(diff.registers || {})) {
        const cell = domMaps.registers.get(name);
        if (!cell) {
            continue;
        }
        const width = name === "PC" || name === "LR" || name === "SP" || name === "DPTR"
            ? ((snapshot?.architecture || appState.architecture) === "arm" ? 8 : 4)
            : 2;
        cell.textContent = toHex(pair.after, width);
        flashCell(cell);
    }
    for (const [name, value] of Object.entries(snapshot.flags || {})) {
        const cell = domMaps.flags.get(name);
        if (cell) {
            const changed = previousSnapshot?.flags && previousSnapshot.flags[name] !== value;
            cell.textContent = value ? "1" : "0";
            if (changed) {
                flashCell(cell, "flag-updated");
            }
        }
    }
    [["iram", domMaps.memory], ["sfr", domMaps.memory], ["xram", domMaps.xram], ["code", domMaps.code]].forEach(([space, map]) => {
        for (const change of diff.memory?.[space] || []) {
            const [address, , value] = change;
            const cell = map.get(`${space}:${address}`);
            if (cell) {
                cell.textContent = Number(value).toString(16).toUpperCase().padStart(2, "0");
                flashCell(cell);
            }
        }
    });
    renderExecutionState(snapshot);
    renderCallStack(snapshot);
    updateWaveform(snapshot);
}

function highlightActiveAssemblerRow(snapshot) {
    document.querySelectorAll("[data-asm-address]").forEach((row) => row.classList.remove("active-asm-row"));
    const active = document.querySelector(`[data-asm-address="${snapshot?.registers?.PC ?? 0}"]`);
    if (active) {
        active.classList.add("active-asm-row");
    }
}

async function refreshMetrics(snapshot, { force = false } = {}) {
    const now = Date.now();
    if (!force && now - appState.lastMetricsFetchMs < 750) {
        return;
    }
    try {
        appState.lastMetricsFetchMs = now;
        appState.lastMetricsPayload = await client.metrics();
        renderMetrics(snapshot, appState.lastMetricsPayload);
    } catch (_error) {
        appState.lastMetricsPayload = null;
        renderMetrics(snapshot, null);
    }
}

function _beginUiTimingMeasurement(payload = null, channel = "snapshot") {
    const now = window.performance?.now?.() ?? Date.now();
    const telemetry = payload?.telemetry || {};
    const clientTiming = payload?.__clientTiming || {};
    appState.uiTiming.pending = {
        channel,
        receiveAtMs: Number(clientTiming.responseReceivedAtMs ?? now),
        renderStartAtMs: now,
        requestStartedAtMs: clientTiming.requestStartedAtMs == null ? null : Number(clientTiming.requestStartedAtMs),
        serverGeneratedAtMs: telemetry.server_generated_at_ms == null ? null : Number(telemetry.server_generated_at_ms),
    };
}

function _finishUiTimingMeasurement(snapshot = appState.snapshot) {
    const pending = appState.uiTiming.pending;
    if (!pending) {
        return;
    }
    const syncRenderMs = Math.max(0, (window.performance?.now?.() ?? Date.now()) - pending.renderStartAtMs);
    window.requestAnimationFrame((frameTs) => {
        const timing = appState.uiTiming;
        const receiveToPaintMs = Math.max(0, Number(frameTs) - pending.receiveAtMs);
        const roundTripMs = pending.requestStartedAtMs == null ? null : Math.max(0, pending.receiveAtMs - pending.requestStartedAtMs);
        const clientPaintEpochMs = (window.performance?.timeOrigin ?? Date.now()) + Number(frameTs);
        const serverToPaintMs = pending.serverGeneratedAtMs == null ? null : Math.max(0, clientPaintEpochMs - pending.serverGeneratedAtMs);
        const lastFrameTs = timing.lastFrameTs;
        const frameGapMs = lastFrameTs == null ? null : Math.max(0, Number(frameTs) - Number(lastFrameTs));
        if (frameGapMs != null && frameGapMs > (UI_FRAME_BUDGET_MS * 1.5)) {
            timing.droppedFrames += Math.max(1, Math.round(frameGapMs / UI_FRAME_BUDGET_MS) - 1);
        }
        timing.lastFrameTs = Number(frameTs);
        timing.lastReceiveToPaintMs = receiveToPaintMs;
        timing.lastSyncRenderMs = syncRenderMs;
        timing.lastRoundTripMs = roundTripMs;
        timing.lastServerToPaintMs = serverToPaintMs;
        timing.lastFrameGapMs = frameGapMs;
        timing.lastChannel = pending.channel;
        timing.lastServerGeneratedAtMs = pending.serverGeneratedAtMs;
        timing.samples = Number(timing.samples || 0) + 1;
        timing.maxReceiveToPaintMs = Math.max(Number(timing.maxReceiveToPaintMs || 0), receiveToPaintMs);
        timing.pending = null;
        renderMetrics(snapshot, appState.lastMetricsPayload);
    });
}

function renderSnapshot(snapshot) {
    _beginUiTimingMeasurement(snapshot, "snapshot");
    const previous = appState.snapshot;
    const merged = { ...(snapshot || {}) };
    if (!previous || previous.architecture !== merged.architecture || Number(merged.cycles || 0) === 0) {
        hardwareState.signalHistory = {};
        hardwareState.signalTokenOrder = [];
        hardwareState.seenSignalTokens = new Set();
    }
    appState.snapshot = merged;
    appState.architecture = merged.architecture;
    appState.endian = merged.endian || "little";
    appState.executionMode = merged.execution_mode || "realtime";
    appState.debugMode = Boolean(merged.debug_mode);
    appState.assembled = Boolean(merged.has_program);
    renderProjectTree(merged);
    renderExecutionState(merged);
    renderRegisters(merged);
    renderCallStack(merged);
    renderMemory(merged);
    renderAssembler(merged);
    safeInvoke("renderTrace", () => renderTrace(merged));
    safeInvoke("updateWaveform", () => updateWaveform(merged));
    renderBreakpointSummary();
    highlightActiveAssemblerRow(merged);
    updateExecutionDecorations(merged, { pulse: true });
    applyControls(merged);
    setStatus(merged.last_error || (merged.halted ? "Simulator halted." : "Ready."), Boolean(merged.last_error));
    setStatusExtra(`Mode: ${merged.architecture.toUpperCase()} | PC ${toHex(merged.registers?.PC ?? 0, merged.architecture === "arm" ? 8 : 4)} | Cycles ${merged.cycles ?? 0}`);
    refreshMetrics(merged, { force: true });
    safeInvoke("renderHardware", () => renderHardware(merged));
    _finishUiTimingMeasurement(merged);
}

function renderExecutionDelta(snapshot, diff, traceEntries = [], statusMessage = null, telemetryPayload = null) {
    _beginUiTimingMeasurement(telemetryPayload, "delta");
    const previousSnapshot = appState.snapshot;
    const merged = mergedSnapshot(snapshot);
    appState.snapshot = merged;
    appState.architecture = merged.architecture;
    appState.endian = merged.endian || "little";
    appState.executionMode = merged.execution_mode || "realtime";
    appState.debugMode = Boolean(merged.debug_mode);
    appState.assembled = Boolean(merged.has_program);
    applyControls(merged);
    applyDiff(merged, diff || { registers: {}, memory: {} }, previousSnapshot);
    safeInvoke("appendTraceEntries", () => appendTraceEntries(traceEntries));
    highlightActiveAssemblerRow(merged);
    updateExecutionDecorations(merged, { pulse: true });
    setStatus(statusMessage || merged.last_error || (merged.halted ? "Simulator halted." : "Ready."), Boolean(merged.last_error));
    setStatusExtra(`Mode: ${merged.architecture.toUpperCase()} | PC ${toHex(merged.registers?.PC ?? 0, merged.architecture === "arm" ? 8 : 4)} | Cycles ${merged.cycles ?? 0}`);
    refreshMetrics(merged);
    if (!(appState.running && appState.signalEventSource)) {
        safeInvoke("renderHardware", () => renderHardware(merged, diff?.hardware || null));
    }
    _finishUiTimingMeasurement(merged);
}

function _debugComponentFromDevice(device) {
    return {
        id: device.id,
        label: device.label,
        type: device.type,
        state: device.state,
        validation: device.validation,
        metrics: device.metrics,
    };
}

function _applySignalStreamPayload(payload) {
    return safeInvoke("applySignalStreamPayload", () => {
        const stream = payload?.hardware || null;
        if (!stream || !appState.snapshot?.hardware) {
            return;
        }
        _beginUiTimingMeasurement(payload, "signals");
        const merged = appState.snapshot;
        const hw = merged.hardware;
        hw.debug = hw.debug || {};
        hw.debug.signal_log = Array.isArray(hw.debug.signal_log) ? hw.debug.signal_log : [];
        hw.debug.signals = hw.debug.signals || {};
        hw.debug.components = Array.isArray(hw.debug.components) ? hw.debug.components : [];
        hw.devices = Array.isArray(hw.devices) ? hw.devices : [];
        hw.pins = hw.pins || {};
        if (payload?.state) {
            merged.cycles = payload.state.cycles ?? merged.cycles;
            merged.halted = payload.state.halted ?? merged.halted;
            merged.last_error = payload.state.last_error ?? merged.last_error;
            merged.last_interrupt = payload.state.last_interrupt ?? merged.last_interrupt;
            merged.registers = { ...(merged.registers || {}), ...(payload.state.registers || {}) };
        }
        if (stream.time_ms != null) {
            hw.time_ms = stream.time_ms;
        }
        const signalChanges = Array.isArray(stream.signal_changes) ? stream.signal_changes : [];
        if (signalChanges.length) {
            hw.debug.signal_log = hw.debug.signal_log.concat(signalChanges).slice(-64);
            _recordSignalEvents(signalChanges);
        }
        for (const [name, signal] of Object.entries(stream.signals || {})) {
            hw.debug.signals[name] = { ...(hw.debug.signals[name] || {}), ...signal };
            const pin = hw.pins[name] || { name };
            hw.pins[name] = {
                ...pin,
                name,
                level: signal.level ?? pin.level ?? 0,
                direction: signal.direction ?? pin.direction ?? "unknown",
                metadata: {
                    ...(pin.metadata || {}),
                    floating: signal.floating,
                    contention: signal.contention,
                    state: signal.state,
                    drivers: signal.drivers,
                    fault: signal.fault,
                },
            };
        }
        const deviceMap = new Map(hw.devices.map((device) => [device.id, device]));
        for (const device of Object.values(stream.devices || {})) {
            deviceMap.set(device.id, device);
        }
        for (const removedId of stream.removed_ids || []) {
            deviceMap.delete(removedId);
        }
        hw.devices = Array.from(deviceMap.values());
        const componentMap = new Map((hw.debug.components || []).map((component) => [component.id, component]));
        for (const device of Object.values(stream.devices || {})) {
            componentMap.set(device.id, _debugComponentFromDevice(device));
        }
        for (const removedId of stream.removed_ids || []) {
            componentMap.delete(removedId);
        }
        hw.debug.components = Array.from(componentMap.values());
        safeInvoke("renderHardware", () => renderHardware(merged, {
            changed_ids: Array.isArray(stream.changed_ids) ? stream.changed_ids : Object.keys(stream.devices || {}),
            removed_ids: Array.isArray(stream.removed_ids) ? stream.removed_ids : [],
            signal_changes: signalChanges,
        }));
        safeInvoke("updateWaveform", () => updateWaveform(merged));
        _finishUiTimingMeasurement(merged);
    });
}

function applyRuntimeEvent(payload) {
    if (payload?.hardware) {
        _applySignalStreamPayload(payload);
        return;
    }
    const incoming = payload?.state || null;
    if (!incoming || !appState.snapshot) {
        return;
    }
    _beginUiTimingMeasurement(payload, "runtime");
    const merged = mergedSnapshot(incoming);
    appState.snapshot = merged;
    renderExecutionState(merged);
    renderRegisters(merged);
    highlightActiveAssemblerRow(merged);
    updateExecutionDecorations(merged, { pulse: false });
    setStatusExtra(`Mode: ${merged.architecture.toUpperCase()} | PC ${toHex(merged.registers?.PC ?? 0, merged.architecture === "arm" ? 8 : 4)} | Cycles ${merged.cycles ?? 0}`);
    renderHardware(merged, payload?.diff?.hardware || null);
    _finishUiTimingMeasurement(merged);
}

function setRunVisualState(mode = "idle") {
    const buttons = ["run", "pause", "step", "step_over", "step_out", "step_back"];
    buttons.forEach((id) => byId(id)?.classList.remove("is-active"));
    if (mode !== "idle") {
        byId(mode)?.classList.add("is-active");
    }
}

function shouldContinueRun(reason) {
    return reason === "max_steps" || reason === "timeout" || reason === "cycle_cap";
}

function ensureRuntimeEventStream() {
    if (appState.signalEventSource || typeof window.EventSource === "undefined") {
        return;
    }
    const source = new window.EventSource(client.eventStreamUrl("/events/signals"));
    source.onmessage = (event) => {
        let payload;
        try {
            payload = JSON.parse(event.data);
        } catch (error) {
            console.error("Runtime event parse failed", error);
            return;
        }
        safeInvoke("applyRuntimeEvent", () => {
            Object.defineProperty(payload, "__clientTiming", {
                value: { responseReceivedAtMs: window.performance?.now?.() ?? Date.now() },
                enumerable: false,
                configurable: true,
            });
            applyRuntimeEvent(payload);
        });
    };
    source.onerror = () => {
        source.close();
        if (appState.signalEventSource === source) {
            appState.signalEventSource = null;
            window.setTimeout(() => ensureRuntimeEventStream(), 1000);
        }
    };
    appState.signalEventSource = source;
}

function applyControls(snapshot) {
    const endianNode = safeSetValue("endian_select", snapshot.endian || "little");
    safeSetValue("architecture_select", snapshot.architecture);
    safeSetValue("execution_mode_select", snapshot.execution_mode || "realtime");
    applyClockControlValue(snapshot.clock_hz || 11_059_200);
    if (endianNode) {
        endianNode.disabled = snapshot.architecture !== "arm";
    }
    safeSetChecked("debug_toggle", Boolean(snapshot.debug_mode));
    safeSetText("target-chip", `Target: ${snapshot.architecture.toUpperCase()}`);
    if (snapshot.halted) {
        setRunVisualState("idle");
    }
}

async function handleAssemble() {
    try {
        clearError();
        clearConsole();
        const response = await client.assemble(appState.editor.getValue());
        renderSnapshot(response);
        logConsole(`Assembled ${response.program?.listing?.length || 0} statements.`);
        await syncBreakpoints();
    } catch (error) {
        handleError(error, "Assembly failed");
    }
}

async function handleSingleStep(mode) {
    try {
        if (!appState.assembled) {
            await handleAssemble();
            if (!appState.assembled) {
                return;
            }
        }
        clearError();
        setRunVisualState(mode === "step" ? "step" : mode === "stepOver" ? "step_over" : "step_out");
        const response = await client[mode]();
        const pcWidth = appState.architecture === "arm" ? 8 : 4;
        if (response.trace) {
            const trace = normalizeTraceEntry(response.trace);
            renderExecutionDelta(response.state, response.diff, [trace], null, response);
            logConsole(formatTraceLogEntry(trace, pcWidth));
        } else if (response.result) {
            const steps = normalizeTraceEntries(response.result.steps || []);
            renderExecutionDelta(response.state, response.diff, steps, null, response);
            for (const step of steps) {
                logConsole(formatTraceLogEntry(step, pcWidth));
            }
        }
        window.setTimeout(() => {
            if (!appState.running) {
                setRunVisualState("idle");
            }
        }, 220);
    } catch (error) {
        handleError(error, "Execution failed");
    }
}

async function runLoop() {
    if (appState.running && !appState.paused) {
        return;
    }
    if (!appState.assembled) {
        setStatus("Assembling...", false);
        await handleAssemble();
        if (!appState.assembled) {
            appState.running = false;
            appState.paused = false;
            setRunVisualState("idle");
            setStatus("Run aborted: assembly failed.", true);
            return;
        }
    }
    appState.running = true;
    appState.paused = false;
    setRunVisualState("run");
    setStatus("Running...");
    while (appState.running && !appState.paused) {
        try {
            const speed = getRunSpeedMultiplier();
            const pcWidth = appState.architecture === "arm" ? 8 : 4;
            const response = await client.run(RUN_FRAME_MAX_STEPS, speed);
            const steps = normalizeTraceEntries(response?.result?.steps || [], { pc: response?.state?.registers?.PC ?? 0 });
            renderExecutionDelta(
                response.state,
                response.diff,
                steps,
                response.result.reason !== "max_steps" ? `Run stopped: ${response.result.reason}.` : null,
                response,
            );
            if (window.DEBUG_TIMING) {
                console.log("[DEBUG_TIMING]", {
                    architecture: appState.architecture,
                    executionMode: appState.executionMode,
                    cyclesExecuted: Number(response.metrics?.cycles_executed || 0),
                    effectiveHzExpected: Number(response.metrics?.effective_hz_expected || _effectiveExecutionHz(appState.architecture, response.state?.clock_hz, speed)),
                    cyclesPerSecActual: Number(response.metrics?.cycles_per_sec_actual || 0),
                    requestCyclesPerSec: Number(response.metrics?.request_cycles_per_sec || 0),
                    instructionsPerSec: Number(response.metrics?.instructions_per_sec || 0),
                    elapsedWallTimeSec: Number(response.metrics?.elapsed_wall_time_sec || 0),
                    computedSimulatedTimeSec: Number(response.metrics?.computed_simulated_time_sec || 0),
                    catchUpRatio: Number(response.metrics?.catch_up_ratio || 0),
                    speedMultiplier: speed,
                });
            }
            const stepCount = Number(response.result.step_count || steps.length || 0);
            const droppedSteps = Number(response.result.dropped_steps || 0);
            if (steps.length === 1) {
                const step = steps[0];
                logConsole(formatTraceLogEntry(step, pcWidth));
            } else if (stepCount > 1) {
                logConsole(formatRunSummary(stepCount, droppedSteps, steps[steps.length - 1] || null, pcWidth));
            }
            if (!shouldContinueRun(response.result.reason)) {
                appState.running = false;
                setRunVisualState("idle");
                setStatus(`Run stopped: ${response.result.reason}.`);
                break;
            }
            await nextAnimationFrame();
        } catch (error) {
            appState.running = false;
            setRunVisualState("idle");
            handleError(error, "Run failed");
            break;
        }
    }
    if (!appState.running) {
        setRunVisualState("idle");
    }
}

async function handleRunToCursor() {
    if (!appState.editor) {
        return;
    }
    const line = appState.editor.getPosition().lineNumber;
    if (!lineToProgramCounters(line).length) {
        setStatus(`No executable statement at line ${line}.`, true);
        return;
    }
    const saved = new Set(appState.breakpoints);
    appState.breakpoints.add(line);
    await syncBreakpoints();
    try {
        await runLoop();
    } finally {
        appState.breakpoints = saved;
        await syncBreakpoints();
    }
}

async function handleReset() {
    try {
        appState.running = false;
        appState.paused = false;
        setRunVisualState("idle");
        clearConsole();
        clearError();
        renderSnapshot(await client.reset());
        logConsole("Simulator reset.");
    } catch (error) {
        handleError(error, "Reset failed");
    }
}

async function handleArchitectureChange(event) {
    try {
        hardwareState.viewportNeedsFit = true;
        hardwareState.lastViewportArchitecture = null;
        const response = await client.setArchitecture(event.target.value);
        appState.breakpoints.clear();
        renderSnapshot(response);
        scheduleHardwareViewportEnsure({ force: true, reason: "architecture-change" });
        appState.editor.getModel().setValue(response.source_code || DEFAULT_SOURCE[response.architecture]);
        clearConsole();
        logConsole(`Switched to ${response.architecture.toUpperCase()}.`);
    } catch (error) {
        handleError(error, "Architecture switch failed");
    }
}

async function handleEndianChange(event) {
    try {
        const response = await client.setEndian(event.target.value);
        renderSnapshot(response);
        logConsole(`Endian switched to ${response.endian}.`);
    } catch (error) {
        handleError(error, "Endian switch failed");
    }
}

async function handleDebugToggle(event) {
    try {
        renderSnapshot(await client.setDebugMode(event.target.checked));
    } catch (error) {
        handleError(error, "Debug mode update failed");
    }
}

async function handleExecutionModeChange(event) {
    try {
        const response = await client.setExecutionMode(event.target.value);
        renderSnapshot(response);
        logConsole(`Execution mode set to ${response.execution_mode}.`);
    } catch (error) {
        handleError(error, "Execution mode update failed");
    }
}

async function handleClockPresetChange(event) {
    const value = event.target.value;
    if (value === "custom") {
        byId("clock_input")?.focus();
        return;
    }
    safeSetValue("clock_input", value);
    await handleClockApply();
}

async function handleClockApply() {
    const hz = getClockHzInput();
    if (!hz) {
        setStatus("Clock must be a positive integer in hertz.", true);
        return;
    }
    try {
        const response = await client.setClock(hz);
        renderSnapshot(response);
        logConsole(`Clock set to ${Number(response.clock_hz || hz).toLocaleString()} Hz.`);
    } catch (error) {
        handleError(error, "Clock update failed");
    }
}

async function handleMemoryEdit(raw) {
    const text = String(raw || "").trim();
    if (!text.includes("=")) {
        setStatus("Use ADDRESS=VALUE format for memory edit.", true);
        return;
    }
    const [lhs, rhs] = text.split("=", 2);
    const address = parseNumeric(lhs);
    const value = parseNumeric(rhs);
    const space = appState.architecture === "arm" ? "xram" : address >= 0x80 ? "sfr" : "iram";
    try {
        renderSnapshot(await client.writeMemory(space, address, value));
        logConsole(`Memory write ${space}[${toHex(address, 4)}] = ${toHex(value, 2)}`);
    } catch (error) {
        handleError(error, "Memory write failed");
    }
}

function updateBaseConverter() {
    const from = byId("convert_from").value;
    const to = byId("convert_to").value;
    const input = byId("convert_input").value.trim();
    const output = byId("convert_output");
    if (!input) {
        output.value = "";
        return;
    }
    const radix = from === "hex" ? 16 : from === "bin" ? 2 : 10;
    const normalized = from === "hex" ? input.replace(/^0x/i, "") : input;
    const value = Number.parseInt(normalized, radix);
    if (Number.isNaN(value)) {
        output.value = "Invalid";
        return;
    }
    output.value = to === "hex" ? value.toString(16).toUpperCase() : to === "bin" ? value.toString(2) : String(value);
}

function swapBaseConverter() {
    const from = byId("convert_from");
    const to = byId("convert_to");
    const input = byId("convert_input");
    const output = byId("convert_output");
    [from.value, to.value] = [to.value, from.value];
    input.value = output.value;
    updateBaseConverter();
}

function setWaveDrawerOpen(open) {
    const drawer = byId("wave-drawer");
    if (!drawer) {
        return;
    }
    if (open) {
        drawer.hidden = false;
        drawer.classList.add("is-open");
        return;
    }
    drawer.classList.remove("is-open");
    drawer.hidden = true;
}

function bindWaveDrawerDrag() {
    const drawer = byId("wave-drawer");
    const header = drawer?.querySelector(".wave-drawer-head");
    if (!drawer || !header || header.dataset.dragBound === "true") {
        return;
    }
    header.dataset.dragBound = "true";
    header.addEventListener("pointerdown", (event) => {
        if (event.target?.closest?.("button")) {
            return;
        }
        event.preventDefault();
        const rect = drawer.getBoundingClientRect();
        const offsetX = event.clientX - rect.left;
        const offsetY = event.clientY - rect.top;
        const onMove = (moveEvent) => {
            const maxLeft = Math.max(8, window.innerWidth - drawer.offsetWidth - 8);
            const maxTop = Math.max(8, window.innerHeight - drawer.offsetHeight - 8);
            const left = clamp(moveEvent.clientX - offsetX, 8, maxLeft);
            const top = clamp(moveEvent.clientY - offsetY, 8, maxTop);
            drawer.style.left = `${left}px`;
            drawer.style.top = `${top}px`;
            drawer.style.right = "auto";
        };
        const onUp = () => {
            window.removeEventListener("pointermove", onMove);
            window.removeEventListener("pointerup", onUp);
        };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp, { once: true });
    });
}

function downloadSnapshot() {
    if (!appState.snapshot) {
        return;
    }
    const blob = new Blob([JSON.stringify(appState.snapshot, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `hexalogic--snapshot.json`;
    link.click();
    URL.revokeObjectURL(url);
}

async function exportSessionState() {
    try {
        const response = await client.exportSession();
        const blob = new Blob([JSON.stringify(response.export, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = `hexalogic--session.json`;
        link.click();
        URL.revokeObjectURL(url);
        logConsole("Session exported.");
    } catch (error) {
        handleError(error, "Session export failed");
    }
}

async function importSessionState() {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "application/json,.json";
    input.addEventListener("change", async () => {
        const [file] = input.files || [];
        if (!file) {
            return;
        }
        try {
            const payload = JSON.parse(await file.text());
            const response = await client.importSession(payload);
            appState.breakpoints.clear();
            renderSnapshot(response);
            appState.editor.getModel().setValue(response.source_code || DEFAULT_SOURCE[response.architecture]);
            logConsole("Session imported.");
        } catch (error) {
            handleError(error, "Session import failed");
        }
    });
    input.click();
}

function handleError(error, fallback) {
    const message = error instanceof SimulatorApiError ? error.message : (error?.message || fallback);
    const line = error instanceof SimulatorApiError ? error.details?.context?.line || null : null;
    console.error(fallback, error);
    setStatus(message, true);
    showError(message, line);
    logConsole(message, "error");
    pushToast(message, "error", 3600);
}

async function handleStepBack() {
    try {
        clearError();
        setRunVisualState("step_back");
        const response = await client.stepBack();
        renderExecutionDelta(response.state, response.diff, [], response.reason === "history_empty" ? "No reverse history." : "Stepped back.", response);
        renderTrace(response.state);
        if (response.reverted) {
            const reverted = normalizeTraceEntry(response.reverted);
            logConsole(`Reversed ${formatTraceLogEntry(reverted, appState.architecture === "arm" ? 8 : 4)}`);
        }
        window.setTimeout(() => {
            if (!appState.running) {
                setRunVisualState("idle");
            }
        }, 220);
    } catch (error) {
        handleError(error, "Reverse execution failed");
    }
}

function handleKeyboardShortcuts(event) {
    if (event.defaultPrevented || !appState.editor) {
        return;
    }
    const target = event.target;
    if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target?.isContentEditable) {
        if (target !== appState.editor.getDomNode()?.querySelector("textarea")) {
            return;
        }
    }
    if (event.key === "F5") {
        event.preventDefault();
        runLoop();
    } else if (event.key === "F10" && event.shiftKey) {
        event.preventDefault();
        handleStepBack();
    } else if (event.key === "F10") {
        event.preventDefault();
        handleSingleStep("stepOver");
    } else if (event.key === "F11" && event.shiftKey) {
        event.preventDefault();
        handleSingleStep("stepOut");
    } else if (event.key === "F11") {
        event.preventDefault();
        handleSingleStep("step");
    }
}

function bindUi() {
    bindWorkspaceModeControls();
    byId("assemble").addEventListener("click", handleAssemble);
    byId("step").addEventListener("click", () => handleSingleStep("step"));
    byId("step_over").addEventListener("click", () => handleSingleStep("stepOver"));
    byId("step_out").addEventListener("click", () => handleSingleStep("stepOut"));
    byId("step_back").addEventListener("click", handleStepBack);
    byId("run").addEventListener("click", runLoop);
    byId("pause").addEventListener("click", () => {
        appState.paused = true;
        appState.running = false;
        setRunVisualState("pause");
        setStatus("Execution paused.");
        refreshState().catch(() => {});
        window.setTimeout(() => setRunVisualState("idle"), 220);
    });
    byId("stop").addEventListener("click", () => {
        appState.paused = true;
        appState.running = false;
        handleReset();
    });
    byId("run_to_cursor").addEventListener("click", handleRunToCursor);
    byId("reset").addEventListener("click", handleReset);
    byId("theme_toggle").addEventListener("click", () => setTheme(appState.theme === "dark" ? "light" : "dark"));
    byId("architecture_select").addEventListener("change", handleArchitectureChange);
    byId("endian_select").addEventListener("change", handleEndianChange);
    byId("debug_toggle").addEventListener("change", handleDebugToggle);
    byId("execution_mode_select").addEventListener("change", handleExecutionModeChange);
    byId("run_speed").addEventListener("input", updateRunSpeedLabel);
    byId("run_speed").addEventListener("change", updateRunSpeedLabel);
    byId("clock_preset").addEventListener("change", handleClockPresetChange);
    byId("clock_apply").addEventListener("click", handleClockApply);
    byId("clock_input").addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            handleClockApply();
        }
    });
    byId("clock_input").addEventListener("input", (event) => syncClockPreset(event.target.value));
    updateRunSpeedLabel();
    byId("clear_breakpoints").addEventListener("click", async () => {
        appState.breakpoints.clear();
        await syncBreakpoints();
    });
    byId("download_output").addEventListener("click", downloadSnapshot);
    byId("export_state").addEventListener("click", exportSessionState);
    byId("import_state").addEventListener("click", importSessionState);
    byId("convert_input").addEventListener("input", updateBaseConverter);
    byId("convert_from").addEventListener("change", updateBaseConverter);
    byId("convert_to").addEventListener("change", updateBaseConverter);
    byId("convert_swap").addEventListener("click", swapBaseConverter);
    byId("waveform_toggle").addEventListener("click", () => setWaveDrawerOpen(byId("wave-drawer").hidden));
    byId("wave-collapse").addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        setWaveDrawerOpen(false);
    });
    byId("help_toggle").addEventListener("click", () => byId("help-popover").toggleAttribute("hidden"));
    byId("memory_edit_input").addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            handleMemoryEdit(event.target.value);
        }
    });
    window.addEventListener("keydown", handleKeyboardShortcuts);
    bindWaveDrawerDrag();
    bindVirtualHardwareUi();
    ensureSmartScrollController({
        panelId: "debugger",
        containerId: "debug-console",
        indicatorClass: "for-debugger",
    });
    ensureSmartScrollController({
        panelId: "trace",
        containerId: "trace-panel-body",
    });
}

function auditBackendHardwareContract(snapshot) {
    const hw = snapshot?.hardware;
    const missing = [];
    if (!hw) {
        missing.push("state.hardware");
    } else {
        const types = hw.device_types || hw.deviceTypes;
        if (!Array.isArray(types) || types.length === 0) {
            missing.push("hardware.device_types");
        }
        if (!hw.catalog || typeof hw.catalog !== "object") {
            missing.push("hardware.catalog");
        }
        if (!hw.pins || typeof hw.pins !== "object") {
            missing.push("hardware.pins");
        }
    }
    if (!missing.length) {
        return;
    }
    console.warn("[HexaLogic] Backend hardware payload is incomplete:", { missing, hardware: hw });
    pushToast("Backend hardware payload is incomplete. Hardware UI may be degraded.", "warn", 6000);
    setStatusExtra(`Hardware degraded: missing ${missing.join(", ")}`);
}

async function auditHardwareApiEndpoints() {
    try {
        await client.hardwareBridge();
    } catch (error) {
        console.warn("[HexaLogic] Hardware API probe failed:", error);
        pushToast("Hardware API is unreachable (check Netlify proxy + Render deploy).", "error", 8000);
        setStatusExtra("Hardware API unreachable: redeploy backend / fix Netlify redirects");
    }
}

async function initialize() {
    setLoaderProgress(8);
    setTheme(appState.theme);
    setLoaderProgress(18);
    setupWorkspaceChrome();
    refreshPanelRegistry();
    setLoaderProgress(30);
    const monaco = await loadMonaco();
    setLoaderProgress(monaco ? 48 : 40);
    if (monaco) {
        createEditor(monaco);
    } else {
        createFallbackEditor();
        setStatusExtra("Editor: fallback textarea");
    }
    if (!appState.editor) {
        throw new Error("Editor initialization failed: editor host unavailable.");
    }
    setLoaderProgress(58);
    bindUi();
    setWorkspaceMode(appState.workspaceMode);
    setLoaderProgress(68);
    let snapshot;
    try {
        snapshot = await client.state();
    } catch (error) {
        handleError(error, "Backend API unreachable");
        const errorBox = byId("error-box");
        if (errorBox) {
            errorBox.hidden = false;
            errorBox.innerHTML = `
                <strong>Backend API unreachable.</strong>
                HexaLogic needs the Flask API to run (assemble/step/run + hardware state).
                <br><br>
                Fix checklist:
                <br>1) Ensure Render service is live and passing <code>/health</code>.
                <br>2) Ensure Netlify proxies <code>/api/v2/*</code> to Render in <code>netlify.toml</code>.
                <br>3) Reload after backend deploy completes.
            `.trim();
        }
        setStatusExtra("Backend offline: deploy Render / fix Netlify proxy");
        const loader = byId("app-loader");
        if (loader) {
            loader.remove();
        }
        return;
    }
    setLoaderProgress(82);
    renderSnapshot(snapshot);
    ensureRuntimeEventStream();
    auditBackendHardwareContract(snapshot);
    auditHardwareApiEndpoints();
    appState.editor.getModel().setValue(snapshot.source_code || DEFAULT_SOURCE[snapshot.architecture]);
    clearConsole();
    logConsole(monaco ? "Simulator ready. Monaco initialized." : "Simulator ready. Monaco failed to load; fallback editor active.");
    setLoaderProgress(100);
    window.__HEXLOGIC_APP_READY__ = true;
    const loader = byId("app-loader");
    if (loader) {
        const elapsed = Date.now() - APP_BOOT_TS;
        await sleep(Math.max(0, LOADER_MIN_MS - elapsed));
        loader.classList.add("loader-hide");
        window.setTimeout(() => loader.remove(), 320);
    }
}

function bootApplication() {
    try {
        initialize().catch((error) => {
            handleError(error, "Application initialization failed");
            const loader = byId("app-loader");
            if (loader) {
                loader.remove();
            }
        });
    } catch (error) {
        handleError(error, "Application bootstrap failed");
        const loader = byId("app-loader");
        if (loader) {
            loader.remove();
        }
    }
}

if (document.readyState === "loading") {
    window.addEventListener("DOMContentLoaded", bootApplication, { once: true });
} else {
    bootApplication();
}
