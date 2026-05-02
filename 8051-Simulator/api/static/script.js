const PANEL_LAYOUT_KEY = "hexlogic-layout-v3";
const MIN_PANEL_HEIGHT = 140;
const MIN_LEFT_WIDTH = 240;
const MIN_RIGHT_WIDTH = 300;
const MIN_CENTER_WIDTH = 420;
const LOADER_MIN_MS = 1500;
const APP_BOOT_TS = Date.now();
const API_BASE = typeof window.HEXLOGIC_API_BASE === "string" ? window.HEXLOGIC_API_BASE.replace(/\/+$/, "") : "";
const WAVE_MAX_SAMPLES = 180;
const WAVE_DRAW_WIDTH = 520;
const WAVE_DRAW_HEIGHT = 40;

function byId(id) {
    return document.getElementById(id);
}

function sleep(ms) {
    return new Promise((resolve) => {
        window.setTimeout(resolve, ms);
    });
}

function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
}

function getClientPoint(event) {
    if (event.touches && event.touches.length > 0) {
        return {
            x: event.touches[0].clientX,
            y: event.touches[0].clientY,
        };
    }
    return {
        x: event.clientX,
        y: event.clientY,
    };
}

const simState = {
    assembled: false,
    isRunning: false,
    runMode: null,
    breakpoints: new Set(),
    activeLine: null,
    nextLine: null,
    errorLine: null,
    theme: "light",
};

const uiState = {
    draggedPanel: null,
};

const waveformState = {
    pins: [],
    togglePins: new Set(),
    history: {},
    sampleCount: 0,
    isOpen: false,
    userCollapsed: false,
};

function setStatus(message, isError = false) {
    const statusEl = byId("status-text");
    if (!statusEl) {
        return;
    }

    statusEl.textContent = message;
    if (isError) {
        statusEl.classList.add("status-error");
    } else {
        statusEl.classList.remove("status-error");
    }
}

function setStatusExtra(text) {
    const statusExtra = byId("status-extra");
    if (!statusExtra) {
        return;
    }
    statusExtra.textContent = text;
}

function updateThemeToggleLabel() {
    const button = byId("theme_toggle");
    if (!button) {
        return;
    }
    button.textContent = simState.theme === "dark" ? "Light Mode" : "Dark Mode";
}

function setTheme(theme) {
    const normalized = theme === "dark" ? "dark" : "light";
    simState.theme = normalized;
    document.body.setAttribute("data-theme", normalized);
    localStorage.setItem("sim-theme", normalized);
    updateThemeToggleLabel();
}

function initializeTheme() {
    const storedTheme = localStorage.getItem("sim-theme");
    if (storedTheme === "dark" || storedTheme === "light") {
        setTheme(storedTheme);
        return;
    }
    const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    setTheme(prefersDark ? "dark" : "light");
}

function hideLoader() {
    const loader = byId("app-loader");
    const progress = byId("loader-progress");
    if (!loader) {
        return;
    }
    if (progress) {
        progress.style.width = "100%";
    }
    const elapsed = Date.now() - APP_BOOT_TS;
    const waitTime = Math.max(0, LOADER_MIN_MS - elapsed);
    window.setTimeout(() => {
        loader.classList.add("loader-hide");
        window.setTimeout(() => {
            loader.remove();
        }, 520);
    }, waitTime);
}

function logDebug(message, level = "info", clear = false) {
    const debug = byId("debug-console");
    if (!debug) {
        return;
    }

    if (clear) {
        debug.innerHTML = "";
    }

    const line = document.createElement("div");
    line.className = `debug-line ${level}`;
    line.textContent = message;
    debug.appendChild(line);
    debug.scrollTop = debug.scrollHeight;
}

function clearErrorBox() {
    const errorBox = byId("error-box");
    if (!errorBox) {
        return;
    }
    errorBox.hidden = true;
    errorBox.textContent = "No errors.";
    simState.errorLine = null;
}

function showErrorBox(message, line = null) {
    const errorBox = byId("error-box");
    if (!errorBox) {
        return;
    }

    const lineInfo = line ? ` (line ${line})` : "";
    errorBox.hidden = false;
    errorBox.textContent = `${message}${lineInfo}`;
    simState.errorLine = line;
    renderGutter();

    if (line) {
        revealLine(line);
    }
}

function extractLineNumber(message) {
    const match = String(message || "").match(/(?:Runtime line|Line)\s+(\d+)/i);
    if (!match) {
        return null;
    }
    return parseInt(match[1], 10);
}

function keepBreakpointsInRange(lineCount) {
    const normalized = new Set();
    simState.breakpoints.forEach((lineNo) => {
        if (lineNo >= 1 && lineNo <= lineCount) {
            normalized.add(lineNo);
        }
    });
    simState.breakpoints = normalized;
}

function getCodeLines() {
    return (byId("code").value || "").split("\n");
}

function updateBreakpointSummary() {
    const summaryEl = byId("breakpoint-list");
    if (!summaryEl) {
        return;
    }

    if (!simState.breakpoints.size) {
        summaryEl.textContent = "None";
        return;
    }

    const lines = Array.from(simState.breakpoints).sort((a, b) => a - b);
    summaryEl.textContent = lines.map((lineNo) => `L${lineNo}`).join(", ");
}

function createGutterLine(lineNo) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "gutter-line";
    button.dataset.line = String(lineNo);

    if (simState.breakpoints.has(lineNo)) {
        button.classList.add("has-breakpoint");
    }
    if (simState.activeLine === lineNo) {
        button.classList.add("is-active");
    }
    if (simState.nextLine === lineNo) {
        button.classList.add("is-next");
    }
    if (simState.errorLine === lineNo) {
        button.classList.add("is-error");
    }

    const dot = document.createElement("span");
    dot.className = "bp-dot";
    const label = document.createElement("span");
    label.textContent = String(lineNo).padStart(3, "0");

    button.appendChild(dot);
    button.appendChild(label);
    return button;
}

function renderGutter() {
    const gutter = byId("gutter");
    if (!gutter) {
        return;
    }

    const lines = getCodeLines();
    keepBreakpointsInRange(lines.length);

    gutter.innerHTML = "";
    for (let i = 0; i < lines.length; i += 1) {
        gutter.appendChild(createGutterLine(i + 1));
    }

    updateBreakpointSummary();
    syncGutterScroll();
}

function syncGutterScroll() {
    const codeEl = byId("code");
    const gutter = byId("gutter");
    if (!codeEl || !gutter) {
        return;
    }

    gutter.scrollTop = codeEl.scrollTop;
}

function lineToCharRange(lineNo, text) {
    const lines = text.split("\n");
    const safeLine = Math.max(1, Math.min(lineNo, lines.length));
    let start = 0;

    for (let i = 1; i < safeLine; i += 1) {
        start += lines[i - 1].length + 1;
    }

    const end = start + lines[safeLine - 1].length;
    return [start, end];
}

function revealLine(lineNo) {
    if (!lineNo) {
        return;
    }

    const codeEl = byId("code");
    if (!codeEl) {
        return;
    }

    const [start, end] = lineToCharRange(lineNo, codeEl.value);
    codeEl.focus();
    codeEl.setSelectionRange(start, end);

    const lineHeight = parseFloat(window.getComputedStyle(codeEl).lineHeight) || 18;
    const top = Math.max((lineNo - 2) * lineHeight, 0);
    codeEl.scrollTop = top;
    syncGutterScroll();
}

function updateRunButtons() {
    const runBtn = byId("run");
    if (runBtn) {
        runBtn.textContent = "Run";
    }
}

function setControlsState() {
    const canExecute = simState.assembled;
    const running = simState.isRunning;
    const runBtn = byId("run");
    const pauseBtn = byId("pause");
    const stopBtn = byId("stop");

    byId("assemble").disabled = running;
    byId("step").disabled = !canExecute || running;
    byId("run_to_cursor").disabled = !canExecute || running;
    byId("reset").disabled = running;
    byId("clear_breakpoints").disabled = running;

    if (runBtn) {
        runBtn.disabled = !canExecute || running;
    }
    if (pauseBtn) {
        pauseBtn.disabled = !running;
    }
    if (stopBtn) {
        stopBtn.disabled = !canExecute;
    }

    updateRunButtons();
}

function getRunDelay() {
    const speed = parseInt(byId("run_speed").value || "6", 10);
    return Math.max(15, 420 - speed * 38);
}

function updateSpeedLabel() {
    const speed = byId("run_speed").value || "6";
    byId("speed_label").textContent = `${speed}x`;
}

function parseJSONResponse(text) {
    if (!text) {
        return {};
    }

    try {
        return JSON.parse(text);
    } catch {
        return { error: text };
    }
}

function buildApiUrl(path) {
    const rawPath = String(path || "").trim();
    if (!rawPath) {
        return API_BASE || "/";
    }
    if (/^https?:\/\//i.test(rawPath)) {
        return rawPath;
    }
    const normalizedPath = rawPath.startsWith("/") ? rawPath : `/${rawPath}`;
    return `${API_BASE}${normalizedPath}`;
}

async function requestJSON(url, payload = undefined) {
    const options = { method: "POST", headers: {} };
    if (payload !== undefined) {
        options.headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(payload);
    }

    const response = await fetch(buildApiUrl(url), options);
    const text = await response.text();
    const data = parseJSONResponse(text);

    if (!response.ok) {
        const error = new Error(data.error || text || "Request failed");
        error.data = data;
        throw error;
    }

    return data;
}

function updatePanels(responseDict) {
    if (responseDict.registers_flags) {
        byId("registers-flags").innerHTML = responseDict.registers_flags;
    }
    if (responseDict.memory) {
        byId("memory-container").innerHTML = responseDict.memory;
    }
    if (responseDict.assembler) {
        byId("assembler-container").innerHTML = responseDict.assembler;
    }
}

function updateStatusFromState(state) {
    if (!state) {
        return;
    }

    const nextLineText = state.next_source_line ? ` | Next Ln ${state.next_source_line}` : "";
    setStatusExtra(`Mode: AT89C51 | IP ${state.run_index}/${state.instruction_count}${nextLineText}`);
}

function normalizeCellValue(text) {
    return String(text || "").trim().toUpperCase();
}

function parseHexNumber(value) {
    const normalized = String(value || "")
        .trim()
        .replace(/^0x/i, "")
        .replace(/h$/i, "");
    if (!/^[0-9a-fA-F]+$/.test(normalized)) {
        return null;
    }
    return Number.parseInt(normalized, 16);
}

function formatAddress(value, width = 4) {
    const hex = Number(value).toString(16).padStart(width, "0");
    return `0x${hex}`;
}

function findWatchCell(targetKey) {
    const wanted = String(targetKey || "").toLowerCase();
    if (!wanted) {
        return null;
    }
    const allCells = document.querySelectorAll("[data-watch-key]");
    for (let i = 0; i < allCells.length; i += 1) {
        const cell = allCells[i];
        const key = String(cell.dataset.watchKey || "").toLowerCase();
        if (key === wanted) {
            return cell;
        }
    }
    return null;
}

function captureWatchSnapshot() {
    const snapshot = {};

    document.querySelectorAll("[data-watch-key]").forEach((node) => {
        if (node.querySelector("input[data-flag-key]")) {
            return;
        }
        const key = node.dataset.watchKey;
        if (!key) {
            return;
        }
        snapshot[key] = normalizeCellValue(node.textContent);
    });

    document.querySelectorAll("input[data-flag-key]").forEach((input) => {
        const key = input.dataset.flagKey;
        if (!key) {
            return;
        }
        snapshot[key] = input.checked ? "1" : "0";
    });

    return snapshot;
}

function flashElement(element, className = "cell-updated") {
    if (!element) {
        return;
    }
    element.classList.remove(className);
    void element.offsetWidth;
    element.classList.add(className);
}

function highlightChangedValues(previousSnapshot) {
    if (!previousSnapshot) {
        return;
    }

    document.querySelectorAll("[data-watch-key]").forEach((node) => {
        if (node.querySelector("input[data-flag-key]")) {
            return;
        }
        const key = node.dataset.watchKey;
        if (!key || !Object.prototype.hasOwnProperty.call(previousSnapshot, key)) {
            return;
        }

        const current = normalizeCellValue(node.textContent);
        if (previousSnapshot[key] !== current) {
            flashElement(node);
        }
    });

    document.querySelectorAll("input[data-flag-key]").forEach((input) => {
        const key = input.dataset.flagKey;
        if (!key || !Object.prototype.hasOwnProperty.call(previousSnapshot, key)) {
            return;
        }

        const current = input.checked ? "1" : "0";
        if (previousSnapshot[key] !== current) {
            const cell = input.closest("td");
            flashElement(cell);
            if (cell) {
                cell.classList.add("flag-updated");
                window.setTimeout(() => {
                    cell.classList.remove("flag-updated");
                }, 820);
            }
        }
    });
}

function pulseMemoryTables() {
    document.querySelectorAll(".memory-table").forEach((table) => {
        flashElement(table, "memory-live-tick");
    });
}

function highlightMemoryPointers() {
    document.querySelectorAll(".memory-live-pointer").forEach((cell) => {
        cell.classList.remove("memory-live-pointer");
    });

    const pcCell = findWatchCell("reg:PC");
    const spCell = findWatchCell("reg:SP");
    const pcValue = pcCell ? parseHexNumber(pcCell.textContent) : null;
    const spValue = spCell ? parseHexNumber(spCell.textContent) : null;

    if (Number.isInteger(pcValue)) {
        const romCell = findWatchCell(`rom:${formatAddress(pcValue, 4)}`);
        if (romCell) {
            flashElement(romCell, "memory-live-pointer");
        }
    }

    if (Number.isInteger(spValue)) {
        const ramCell = findWatchCell(`ram:${formatAddress(spValue, 4)}`);
        if (ramCell) {
            flashElement(ramCell, "memory-live-pointer");
        }
    }
}

function runMemoryLiveEffects() {
    pulseMemoryTables();
    highlightMemoryPointers();
}

function parsePinReference(port, bit) {
    const portId = Number.parseInt(String(port || "").trim(), 10);
    const bitId = Number.parseInt(String(bit || "").trim(), 10);
    if (!Number.isInteger(portId) || !Number.isInteger(bitId)) {
        return null;
    }
    if (portId < 0 || portId > 3 || bitId < 0 || bitId > 7) {
        return null;
    }
    return `P${portId}.${bitId}`;
}

function detectWavePinsFromCode(code) {
    const source = String(code || "");
    const pins = new Set();
    const bitPattern = /\bP([0-3])\s*\.\s*([0-7])\b/gi;
    for (const match of source.matchAll(bitPattern)) {
        const pin = parsePinReference(match[1], match[2]);
        if (pin) {
            pins.add(pin);
        }
    }
    return Array.from(pins).sort((a, b) => a.localeCompare(b, "en", { numeric: true }));
}

function detectTogglePinsFromCode(code) {
    const source = String(code || "");
    const pins = new Set();
    const togglePattern = /\b(?:CPL|SETB|CLR)\s+\/?\s*P([0-3])\s*\.\s*([0-7])\b/gi;
    for (const match of source.matchAll(togglePattern)) {
        const pin = parsePinReference(match[1], match[2]);
        if (pin) {
            pins.add(pin);
        }
    }
    return pins;
}

function resetWaveformHistory() {
    waveformState.sampleCount = 0;
    waveformState.history = {};
    waveformState.pins.forEach((pin) => {
        waveformState.history[pin] = [];
    });
}

function setWaveDrawerOpen(open, options = {}) {
    const drawer = byId("wave-drawer");
    const toggle = byId("waveform_toggle");
    if (!drawer) {
        return;
    }

    const manual = Boolean(options.manual);
    waveformState.isOpen = Boolean(open);
    if (manual) {
        waveformState.userCollapsed = !waveformState.isOpen;
    } else if (waveformState.isOpen) {
        waveformState.userCollapsed = false;
    }

    drawer.hidden = false;
    drawer.classList.toggle("is-open", waveformState.isOpen);
    if (toggle) {
        toggle.classList.toggle("is-active", waveformState.isOpen);
    }
}

function readPinLevel(pin) {
    const parts = String(pin || "").split(".");
    if (parts.length !== 2) {
        return null;
    }

    const port = parts[0].toUpperCase();
    const bit = Number.parseInt(parts[1], 10);
    if (!Number.isInteger(bit) || bit < 0 || bit > 7) {
        return null;
    }

    const portCell = findWatchCell(`sfr:${port}`);
    if (!portCell) {
        return null;
    }

    const portValue = parseHexNumber(portCell.textContent);
    if (!Number.isInteger(portValue)) {
        return null;
    }
    return (portValue >> bit) & 0x01;
}

function countWaveTransitions(values) {
    let transitions = 0;
    for (let idx = 1; idx < values.length; idx += 1) {
        if (values[idx] !== values[idx - 1]) {
            transitions += 1;
        }
    }
    return transitions;
}

function buildPredictedSeries(length) {
    const count = Math.max(18, Math.min(WAVE_MAX_SAMPLES, Number.parseInt(length, 10) || 0));
    const series = [];
    for (let idx = 0; idx < count; idx += 1) {
        series.push(Math.floor(idx / 3) % 2);
    }
    return series;
}

function buildWavePath(samples) {
    if (!samples.length) {
        return "";
    }

    const highY = 8;
    const lowY = WAVE_DRAW_HEIGHT - 8;
    const stepX = samples.length > 1 ? WAVE_DRAW_WIDTH / (samples.length - 1) : WAVE_DRAW_WIDTH;

    let path = `M 0 ${samples[0] ? highY : lowY}`;
    for (let idx = 1; idx < samples.length; idx += 1) {
        const previous = samples[idx - 1];
        const current = samples[idx];
        const x = idx * stepX;
        const prevY = previous ? highY : lowY;
        const currentY = current ? highY : lowY;
        if (current !== previous) {
            path += ` L ${x.toFixed(2)} ${prevY} L ${x.toFixed(2)} ${currentY}`;
        } else {
            path += ` L ${x.toFixed(2)} ${currentY}`;
        }
    }
    return path;
}

function buildWaveRow(pin, samples, predicted) {
    const row = document.createElement("article");
    row.className = "wave-row";

    const head = document.createElement("div");
    head.className = "wave-row-head";

    const label = document.createElement("span");
    label.className = "wave-pin";
    label.textContent = pin;

    const mode = document.createElement("span");
    mode.className = `wave-chip ${predicted ? "predicted" : "live"}`;
    mode.textContent = predicted ? "EST" : "LIVE";

    const transitions = countWaveTransitions(samples);
    const meta = document.createElement("span");
    meta.className = "wave-meta";
    meta.textContent = `Transitions: ${transitions}`;

    head.appendChild(label);
    head.appendChild(mode);
    head.appendChild(meta);
    row.appendChild(head);

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "wave-svg");
    svg.setAttribute("viewBox", `0 0 ${WAVE_DRAW_WIDTH} ${WAVE_DRAW_HEIGHT}`);
    svg.setAttribute("preserveAspectRatio", "none");

    const midline = document.createElementNS("http://www.w3.org/2000/svg", "line");
    midline.setAttribute("x1", "0");
    midline.setAttribute("x2", String(WAVE_DRAW_WIDTH));
    midline.setAttribute("y1", String(WAVE_DRAW_HEIGHT / 2));
    midline.setAttribute("y2", String(WAVE_DRAW_HEIGHT / 2));
    midline.setAttribute("class", "wave-midline");
    svg.appendChild(midline);

    for (let idx = 1; idx < 8; idx += 1) {
        const grid = document.createElementNS("http://www.w3.org/2000/svg", "line");
        const x = (WAVE_DRAW_WIDTH / 8) * idx;
        grid.setAttribute("x1", x.toFixed(2));
        grid.setAttribute("x2", x.toFixed(2));
        grid.setAttribute("y1", "0");
        grid.setAttribute("y2", String(WAVE_DRAW_HEIGHT));
        grid.setAttribute("class", "wave-gridline");
        svg.appendChild(grid);
    }

    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("class", `wave-trace ${predicted ? "predicted" : "live"}`);
    path.setAttribute("d", buildWavePath(samples));
    svg.appendChild(path);
    row.appendChild(svg);

    return row;
}

function renderWavePanel() {
    const statusEl = byId("wave-status");
    const emptyEl = byId("wave-empty");
    const listEl = byId("wave-list");
    if (!statusEl || !emptyEl || !listEl) {
        return;
    }

    listEl.innerHTML = "";

    if (!waveformState.pins.length) {
        emptyEl.hidden = false;
        emptyEl.textContent = "Assemble code with pin bit operations like CPL P2.1 to view square-wave traces.";
        statusEl.textContent = "No waveform channels detected.";
        return;
    }

    emptyEl.hidden = true;
    let liveChannels = 0;

    waveformState.pins.forEach((pin) => {
        const samples = waveformState.history[pin] || [];
        const transitionCount = countWaveTransitions(samples);
        const predicted = transitionCount === 0 && waveformState.togglePins.has(pin);
        const drawSamples = predicted ? buildPredictedSeries(samples.length || 36) : samples;
        if (!predicted) {
            liveChannels += 1;
        }
        listEl.appendChild(buildWaveRow(pin, drawSamples, predicted));
    });

    if (liveChannels > 0) {
        statusEl.textContent = `Live channels: ${liveChannels} | Samples: ${waveformState.sampleCount}`;
    } else {
        statusEl.textContent = "Waiting for pin transitions. Showing estimated square wave preview.";
    }
}

function sampleWaveforms() {
    if (!waveformState.pins.length) {
        return;
    }

    waveformState.sampleCount += 1;
    waveformState.pins.forEach((pin) => {
        const samples = waveformState.history[pin] || [];
        const value = readPinLevel(pin);
        const last = samples.length ? samples[samples.length - 1] : 0;
        const nextValue = Number.isInteger(value) ? value : last;
        samples.push(nextValue ? 1 : 0);
        if (samples.length > WAVE_MAX_SAMPLES) {
            samples.shift();
        }
        waveformState.history[pin] = samples;
    });

    renderWavePanel();
}

function configureWaveformChannels(code) {
    const pins = detectWavePinsFromCode(code);
    waveformState.pins = pins;
    waveformState.togglePins = detectTogglePinsFromCode(code);
    resetWaveformHistory();
    renderWavePanel();

    if (!pins.length) {
        setWaveDrawerOpen(false);
        return;
    }

    if (waveformState.togglePins.size && !waveformState.userCollapsed) {
        setWaveDrawerOpen(true);
    }
}

function setupWavePanel() {
    const toggleBtn = byId("waveform_toggle");
    const closeBtn = byId("wave-collapse");

    if (toggleBtn) {
        toggleBtn.addEventListener("click", () => {
            setWaveDrawerOpen(!waveformState.isOpen, { manual: true });
        });
    }

    if (closeBtn) {
        closeBtn.addEventListener("click", () => {
            setWaveDrawerOpen(false, { manual: true });
        });
    }

    setWaveDrawerOpen(false);
    renderWavePanel();
}

function revealAssemblerActiveRow() {
    const activeRow = document.querySelector(".active-asm-row");
    if (!activeRow) {
        return;
    }
    activeRow.scrollIntoView({ block: "nearest" });
}

function applyServerResponse(data, options = {}) {
    const animateChanges = options.animateChanges !== false;
    const snapshot = animateChanges ? captureWatchSnapshot() : null;
    updatePanels(data);
    if (animateChanges) {
        highlightChangedValues(snapshot);
    }
    runMemoryLiveEffects();
    updateStatusFromState(data.state);
    if (data.state && Object.prototype.hasOwnProperty.call(data.state, "next_source_line")) {
        simState.nextLine = data.state.next_source_line;
    }
    revealAssemblerActiveRow();
    renderGutter();
    sampleWaveforms();
}

function updateStepVisuals(step) {
    if (!step) {
        return;
    }

    simState.activeLine = step.source_line || simState.activeLine;
    simState.nextLine = step.next_source_line || null;
    simState.errorLine = null;
    renderGutter();

    if (step.source_line) {
        revealLine(step.source_line);
    }

    if (step.source_line || step.source_command) {
        const line = step.source_line ? `L${step.source_line}` : "L?";
        const code = step.source_command || "";
        logDebug(`STEP ${line} -> ${code}`);
    }
}

function handleRequestError(error, defaultMessage) {
    const payload = error.data || {};
    const message = payload.error || error.message || defaultMessage;
    const lineNo = payload.line || extractLineNumber(message);

    setStatus(defaultMessage, true);
    showErrorBox(message, lineNo);
    logDebug(`ERROR: ${message}`, "error");
}

function getCodeValue() {
    return byId("code").value || "";
}

function getCursorLine() {
    const codeEl = byId("code");
    const cursor = codeEl.selectionStart || 0;
    const before = codeEl.value.slice(0, cursor);
    return before.split("\n").length;
}

function GetFlags() {
    const flags = {};
    document.querySelectorAll(".flag-input").forEach((element) => {
        flags[element.id] = element.checked;
    });
    return flags;
}

async function assembleProgram() {
    const code = getCodeValue();
    if (!code.trim()) {
        setStatus("Code editor is empty.", true);
        showErrorBox("Code editor is empty.");
        return;
    }

    clearErrorBox();
    setStatus("Assembling...");
    try {
        const response = await requestJSON("/assemble", {
            code,
            flags: GetFlags(),
        });

        localStorage.setItem("code", code);
        simState.assembled = Boolean(response.state && response.state.ready);
        simState.activeLine = null;
        simState.nextLine = response.state ? response.state.next_source_line || null : null;
        configureWaveformChannels(code);

        applyServerResponse(response, { animateChanges: false });
        logDebug("Assemble finished successfully.", "info", true);
        if (waveformState.pins.length) {
            logDebug(`Waveform monitor armed for: ${waveformState.pins.join(", ")}`);
        }
        setStatus("Assemble completed.");
    } catch (error) {
        simState.assembled = false;
        handleRequestError(error, "Assemble failed.");
    }

    setControlsState();
}

async function executeSingleStep() {
    if (!simState.assembled) {
        setStatus("Assemble code before stepping.", true);
        showErrorBox("Assemble code before stepping.");
        return;
    }

    clearErrorBox();
    setStatus("Stepping...");

    try {
        const response = await requestJSON("/run-once");
        applyServerResponse(response);
        updateStepVisuals(response.step);

        if (response.step && response.step.done) {
            setStatus("Program execution complete.");
            logDebug("Execution completed.");
        } else {
            setStatus(`Stepped to line ${response.step && response.step.source_line ? response.step.source_line : "?"}.`);
        }
    } catch (error) {
        handleRequestError(error, "Step failed.");
    }
}

function shouldBreakOnNext(step, runToLine = null) {
    const nextLine = step ? step.next_source_line : null;
    if (!nextLine) {
        return false;
    }

    if (runToLine && nextLine === runToLine) {
        setStatus(`Run-to-cursor reached line ${runToLine}.`);
        logDebug(`Run-to-cursor stop at L${runToLine}.`, "warn");
        return true;
    }

    if (simState.breakpoints.has(nextLine)) {
        setStatus(`Paused at breakpoint line ${nextLine}.`);
        logDebug(`Breakpoint hit at L${nextLine}.`, "warn");
        return true;
    }

    return false;
}

function pauseExecution(message = "Execution paused.", level = "warn") {
    if (!simState.isRunning) {
        setStatus("Execution is not running.");
        return;
    }
    simState.isRunning = false;
    simState.runMode = null;
    setStatus(message);
    logDebug(message, level);
    setControlsState();
}

async function stopAndRewindExecution() {
    const wasRunning = simState.isRunning;
    simState.isRunning = false;
    simState.runMode = null;
    setControlsState();

    if (!simState.assembled) {
        setStatus("Nothing to stop.");
        return;
    }

    clearErrorBox();
    setStatus("Stopping execution...");

    try {
        const currentCode = getCodeValue();
        const currentFlags = GetFlags();
        const resetResponse = await requestJSON("/reset");
        applyServerResponse(resetResponse, { animateChanges: false });

        const assembleResponse = await requestJSON("/assemble", {
            code: currentCode,
            flags: currentFlags,
        });

        simState.assembled = Boolean(assembleResponse.state && assembleResponse.state.ready);
        simState.activeLine = null;
        simState.nextLine = assembleResponse.state ? assembleResponse.state.next_source_line || null : null;
        configureWaveformChannels(currentCode);
        applyServerResponse(assembleResponse, { animateChanges: false });
        setStatus("Execution stopped.");

        if (wasRunning) {
            logDebug("Execution stopped and rewound to program start.", "warn");
        } else {
            logDebug("Program rewound to start.", "warn");
        }
    } catch (error) {
        handleRequestError(error, "Stop failed.");
    }

    setControlsState();
}

async function runLoop(options = {}) {
    const runToLine = options.runToLine || null;

    if (!simState.assembled) {
        setStatus("Assemble code before running.", true);
        showErrorBox("Assemble code before running.");
        return;
    }

    clearErrorBox();

    if (simState.isRunning) {
        setStatus("Execution already running. Use Pause or Stop.", true);
        return;
    }

    simState.isRunning = true;
    simState.runMode = runToLine ? "run_to_cursor" : "run";
    setControlsState();

    setStatus(runToLine ? `Running toward line ${runToLine}...` : "Running...");

    while (simState.isRunning) {
        try {
            const response = await requestJSON("/run-once");
            applyServerResponse(response);
            updateStepVisuals(response.step);

            if (response.step && response.step.done) {
                simState.isRunning = false;
                setStatus("Program execution complete.");
                logDebug("Execution completed.");
                break;
            }

            if (shouldBreakOnNext(response.step, runToLine)) {
                simState.isRunning = false;
                break;
            }

            await sleep(getRunDelay());
        } catch (error) {
            simState.isRunning = false;
            simState.runMode = null;
            setControlsState();
            handleRequestError(error, "Run failed.");
            return;
        }
    }

    simState.runMode = null;
    setControlsState();
}

async function resetSimulator() {
    setStatus("Resetting simulator...");

    try {
        const response = await requestJSON("/reset");
        simState.assembled = false;
        simState.isRunning = false;
        simState.runMode = null;
        simState.activeLine = null;
        simState.nextLine = null;
        simState.errorLine = null;
        simState.breakpoints.clear();
        configureWaveformChannels(getCodeValue());

        applyServerResponse(response, { animateChanges: false });
        logDebug("Simulator reset.", "info", true);
        clearErrorBox();
        setStatus("Simulator reset.");
    } catch (error) {
        handleRequestError(error, "Reset failed.");
    }

    setControlsState();
}

function ParseHex(data) {
    const value = String(data || "").trim();
    if (/^0[xX][a-fA-F0-9]+$/.test(value)) {
        return value;
    }
    if (/^[a-fA-F0-9]+[hH]$/.test(value)) {
        return `0x${value.slice(0, -1)}`;
    }
    return `0x${value}`;
}

function ProcessMemEdit(data) {
    if (data.includes(":")) {
        const splitData = data.split(":");
        const parsedData = [];
        for (let i = 0; i < splitData.length; i += 1) {
            splitData[i] = ParseHex(splitData[i]);
        }

        const start = parseInt(splitData[0], 16);
        const end = parseInt(splitData[1], 16);
        let idx = 0;

        for (let i = start; i <= end; i += 1) {
            parsedData[idx] = [
                `0x${i.toString(16)}`,
                `0x${Math.floor(Math.random() * 255).toString(16)}`,
            ];
            idx += 1;
        }
        return parsedData;
    }

    if (!data.includes("=")) {
        return false;
    }

    const assignment = data.split("=");
    for (let i = 0; i < assignment.length; i += 1) {
        assignment[i] = ParseHex(assignment[i]);
    }
    return [assignment];
}

async function applyMemoryEdit(input) {
    const parsed = ProcessMemEdit(input);
    if (!parsed) {
        setStatus("Invalid memory edit format.", true);
        showErrorBox("Invalid memory edit format.");
        return;
    }

    clearErrorBox();
    setStatus("Applying memory edit...");

    try {
        const response = await requestJSON("/memory-edit", parsed);
        applyServerResponse(response);
        setStatus("Memory updated.");
        logDebug(`Memory edit applied: ${input}`);
    } catch (error) {
        handleRequestError(error, "Memory edit failed.");
    }
}

function toggleBreakpoint(lineNo) {
    if (simState.breakpoints.has(lineNo)) {
        simState.breakpoints.delete(lineNo);
        logDebug(`Breakpoint removed at L${lineNo}.`);
    } else {
        simState.breakpoints.add(lineNo);
        logDebug(`Breakpoint set at L${lineNo}.`);
    }
    renderGutter();
}

function parseConverterInput(value, fromBase) {
    const source = String(value || "").trim();
    if (!source) {
        return null;
    }

    let normalized = source;
    if (fromBase === "hex") {
        normalized = source.replace(/^0x/i, "").replace(/h$/i, "");
        if (!/^[0-9a-fA-F]+$/.test(normalized)) {
            throw new Error("Invalid hex value");
        }
        return Number.parseInt(normalized, 16);
    }

    if (fromBase === "bin") {
        normalized = source.replace(/^0b/i, "").replace(/b$/i, "");
        if (!/^[01]+$/.test(normalized)) {
            throw new Error("Invalid binary value");
        }
        return Number.parseInt(normalized, 2);
    }

    if (!/^\d+$/.test(source)) {
        throw new Error("Invalid decimal value");
    }
    return Number.parseInt(source, 10);
}

function formatConverterOutput(value, toBase) {
    if (!Number.isFinite(value) || value < 0) {
        throw new Error("Only non-negative values are supported");
    }

    if (toBase === "hex") {
        return `0x${value.toString(16).toUpperCase()}`;
    }
    if (toBase === "bin") {
        return `0b${value.toString(2)}`;
    }
    return String(value);
}

function runConverter() {
    const fromBase = byId("convert_from").value;
    const toBase = byId("convert_to").value;
    const input = byId("convert_input").value;
    const output = byId("convert_output");

    if (!input.trim()) {
        output.value = "";
        return;
    }

    try {
        const numeric = parseConverterInput(input, fromBase);
        output.value = formatConverterOutput(numeric, toBase);
    } catch {
        output.value = "Invalid";
    }
}

function normalizeNodeText(node) {
    return String(node ? node.textContent || "" : "")
        .replace(/\s+/g, " ")
        .trim();
}

function extractCellValue(cell) {
    const checkbox = cell.querySelector('input[type="checkbox"]');
    if (checkbox) {
        return checkbox.checked;
    }
    return normalizeNodeText(cell);
}

function extractTableSnapshot(table) {
    const headers = Array.from(table.querySelectorAll("thead th")).map((cell) => normalizeNodeText(cell));
    const rows = Array.from(table.querySelectorAll("tbody tr")).map((row) =>
        Array.from(row.children).map((cell) => extractCellValue(cell)),
    );
    return { headers, rows };
}

function extractSectionsSnapshot(rootElement) {
    const sections = [];
    if (!rootElement) {
        return sections;
    }

    const sectionNodes = rootElement.querySelectorAll("section");
    sectionNodes.forEach((section) => {
        const titleNode = section.querySelector(".subpanel-title, .panel-title, .summary-title");
        const title = normalizeNodeText(titleNode) || "Section";
        const tables = Array.from(section.querySelectorAll("table")).map((table) => extractTableSnapshot(table));
        const notes = Array.from(section.querySelectorAll(".empty-output, .summary-body"))
            .map((node) => normalizeNodeText(node))
            .filter(Boolean);

        sections.push({
            title,
            tables,
            notes,
        });
    });

    return sections;
}

function getBreakpointLines() {
    return Array.from(simState.breakpoints).sort((a, b) => a - b);
}

function buildSimulationSnapshot() {
    return {
        generated_at: new Date().toISOString(),
        simulator: "HexaLogic AT89C51",
        status: {
            text: normalizeNodeText(byId("status-text")),
            extra: normalizeNodeText(byId("status-extra")),
            assembled: simState.assembled,
            is_running: simState.isRunning,
            run_mode: simState.runMode,
            active_line: simState.activeLine,
            next_line: simState.nextLine,
            error_line: simState.errorLine,
        },
        code: {
            language: "8051-asm",
            source: getCodeValue(),
        },
        breakpoints: getBreakpointLines(),
        debug_console: Array.from(document.querySelectorAll("#debug-console .debug-line")).map((line) => ({
            level: line.classList.contains("error") ? "error" : line.classList.contains("warn") ? "warn" : "info",
            text: normalizeNodeText(line),
        })),
        assembler_output: extractSectionsSnapshot(byId("assembler-container")),
        registers_and_flags: extractSectionsSnapshot(byId("registers-flags")),
        memory_views: extractSectionsSnapshot(byId("memory-container")),
        waveforms: {
            channels: waveformState.pins.map((pin) => ({
                pin,
                samples: [...(waveformState.history[pin] || [])],
                predicted_preview: waveformState.togglePins.has(pin),
            })),
            sample_count: waveformState.sampleCount,
            panel_open: waveformState.isOpen,
        },
    };
}

function downloadOutputReport() {
    const report = buildSimulationSnapshot();
    const content = JSON.stringify(report, null, 2);
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const filename = `hexalogic-output-${stamp}.json`;
    const blob = new Blob([content], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => {
        URL.revokeObjectURL(link.href);
    }, 0);
    setStatus(`Output downloaded: ${filename}`);
    logDebug(`Output report downloaded: ${filename}`);
}

function setupHelpPopover() {
    const helpButton = byId("help_toggle");
    const popover = byId("help-popover");
    if (!helpButton || !popover) {
        return;
    }

    function closePopover() {
        popover.hidden = true;
        helpButton.setAttribute("aria-expanded", "false");
    }

    helpButton.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        const isOpening = popover.hidden;
        popover.hidden = !isOpening;
        helpButton.setAttribute("aria-expanded", String(isOpening));
    });

    document.addEventListener("click", (event) => {
        if (popover.hidden) {
            return;
        }
        const target = event.target;
        if (popover.contains(target) || helpButton.contains(target)) {
            return;
        }
        closePopover();
    });

    window.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !popover.hidden) {
            closePopover();
        }
    });
}

function setupConverter() {
    const input = byId("convert_input");
    const from = byId("convert_from");
    const to = byId("convert_to");
    const swap = byId("convert_swap");

    if (!input || !from || !to || !swap) {
        return;
    }

    input.addEventListener("input", runConverter);
    from.addEventListener("change", runConverter);
    to.addEventListener("change", runConverter);
    swap.addEventListener("click", () => {
        const tmp = from.value;
        from.value = to.value;
        to.value = tmp;
        if (byId("convert_output").value) {
            input.value = byId("convert_output").value;
        }
        runConverter();
    });
}

function addDragPills() {
    document.querySelectorAll(".dock-panel .panel-title").forEach((title) => {
        if (title.querySelector(".panel-drag-pill")) {
            title.setAttribute("draggable", "true");
            return;
        }
        const pill = document.createElement("span");
        pill.className = "panel-drag-pill";
        pill.title = "Drag panel to move";
        title.appendChild(pill);
        title.setAttribute("draggable", "true");
    });
}

function getColumns() {
    return Array.from(document.querySelectorAll(".panel-column"));
}

function clearDropHints() {
    document.querySelectorAll(".dock-panel").forEach((panel) => {
        panel.classList.remove("drop-before", "drop-after");
    });
    getColumns().forEach((column) => {
        column.classList.remove("drag-over");
    });
}

function findDropTarget(column, pointerY) {
    const panels = Array.from(column.querySelectorAll(".dock-panel")).filter((panel) => panel !== uiState.draggedPanel);
    for (let i = 0; i < panels.length; i += 1) {
        const panel = panels[i];
        const rect = panel.getBoundingClientRect();
        const midpoint = rect.top + rect.height / 2;
        if (pointerY < midpoint) {
            return { panel, before: true };
        }
    }
    return { panel: panels[panels.length - 1] || null, before: false };
}

function placePanelInColumn(column, pointerY) {
    const { panel, before } = findDropTarget(column, pointerY);
    if (!panel) {
        column.appendChild(uiState.draggedPanel);
        return;
    }
    if (before) {
        column.insertBefore(uiState.draggedPanel, panel);
    } else {
        column.insertBefore(uiState.draggedPanel, panel.nextSibling);
    }
}

function setPanelFlexHeight(panel, heightPx) {
    panel.style.flexBasis = `${Math.round(heightPx)}px`;
    panel.style.flexGrow = "0";
    panel.style.flexShrink = "0";
}

function clearRowResizers(column) {
    column.querySelectorAll(".row-resizer").forEach((resizer) => {
        resizer.remove();
    });
}

function startRowResize(event, resizer) {
    const previousPanel = resizer.previousElementSibling;
    const nextPanel = resizer.nextElementSibling;
    if (!previousPanel || !nextPanel) {
        return;
    }
    if (!previousPanel.classList.contains("dock-panel") || !nextPanel.classList.contains("dock-panel")) {
        return;
    }

    event.preventDefault();
    resizer.classList.add("is-dragging");
    document.body.style.cursor = "row-resize";
    if (event.pointerId && typeof resizer.setPointerCapture === "function") {
        resizer.setPointerCapture(event.pointerId);
    }

    const startPoint = getClientPoint(event);
    const startY = startPoint.y;
    const prevStart = previousPanel.getBoundingClientRect().height;
    const nextStart = nextPanel.getBoundingClientRect().height;

    function onMove(moveEvent) {
        const movePoint = getClientPoint(moveEvent);
        const delta = movePoint.y - startY;
        let nextPrev = prevStart + delta;
        let nextNext = nextStart - delta;

        if (nextPrev < MIN_PANEL_HEIGHT) {
            nextPrev = MIN_PANEL_HEIGHT;
            nextNext = prevStart + nextStart - nextPrev;
        }
        if (nextNext < MIN_PANEL_HEIGHT) {
            nextNext = MIN_PANEL_HEIGHT;
            nextPrev = prevStart + nextStart - nextNext;
        }

        setPanelFlexHeight(previousPanel, nextPrev);
        setPanelFlexHeight(nextPanel, nextNext);
    }

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

        for (let i = 0; i < panels.length - 1; i += 1) {
            const resizer = document.createElement("div");
            resizer.className = "row-resizer";
            resizer.setAttribute("role", "separator");
            resizer.setAttribute("aria-orientation", "horizontal");
            resizer.setAttribute("title", "Resize stacked panels");
            if (window.PointerEvent) {
                resizer.addEventListener("pointerdown", (event) => {
                    startRowResize(event, resizer);
                });
            } else {
                resizer.addEventListener("mousedown", (event) => {
                    startRowResize(event, resizer);
                });
                resizer.addEventListener(
                    "touchstart",
                    (event) => {
                        startRowResize(event, resizer);
                    },
                    { passive: false },
                );
            }
            panels[i].after(resizer);
        }
    });
}

function setupPanelDocking() {
    const panels = Array.from(document.querySelectorAll(".dock-panel"));
    const columns = getColumns();

    panels.forEach((panel) => {
        const title = panel.querySelector(".panel-title");
        if (!title) {
            return;
        }
        title.addEventListener("dragstart", (event) => {
            uiState.draggedPanel = panel;
            panel.classList.add("dragging");
            if (event.dataTransfer) {
                event.dataTransfer.effectAllowed = "move";
                event.dataTransfer.setData("text/plain", panel.dataset.panel || "");
                event.dataTransfer.setDragImage(panel, 22, 14);
            }
        });

        title.addEventListener("dragend", () => {
            panel.classList.remove("dragging");
            uiState.draggedPanel = null;
            clearDropHints();
        });
    });

    columns.forEach((column) => {
        column.addEventListener("dragover", (event) => {
            if (!uiState.draggedPanel) {
                return;
            }

            event.preventDefault();
            clearDropHints();
            column.classList.add("drag-over");
            const target = findDropTarget(column, event.clientY);
            if (target.panel) {
                target.panel.classList.add(target.before ? "drop-before" : "drop-after");
            }
        });

        column.addEventListener("dragenter", (event) => {
            if (!uiState.draggedPanel) {
                return;
            }
            event.preventDefault();
            column.classList.add("drag-over");
        });

        column.addEventListener("drop", (event) => {
            if (!uiState.draggedPanel) {
                return;
            }

            event.preventDefault();
            placePanelInColumn(column, event.clientY);
            clearDropHints();
            buildRowResizers();
            persistPanelLayout();
        });

        column.addEventListener("dragleave", (event) => {
            if (!column.contains(event.relatedTarget)) {
                column.classList.remove("drag-over");
            }
        });
    });
}

function setupColumnResizers() {
    const workspace = byId("workspace");
    const leftPane = workspace ? workspace.querySelector(".left-pane") : null;
    const rightPane = workspace ? workspace.querySelector(".right-pane") : null;
    if (!workspace || !leftPane || !rightPane) {
        return;
    }

    document.querySelectorAll(".col-resizer").forEach((resizer) => {
        function startResize(event) {
            event.preventDefault();
            resizer.classList.add("is-dragging");
            document.body.style.cursor = "col-resize";
            if (event.pointerId && typeof resizer.setPointerCapture === "function") {
                resizer.setPointerCapture(event.pointerId);
            }

            const startPoint = getClientPoint(event);
            const startX = startPoint.x;
            const workspaceRect = workspace.getBoundingClientRect();
            const leftStart = leftPane.getBoundingClientRect().width;
            const rightStart = rightPane.getBoundingClientRect().width;
            const which = resizer.dataset.resizer;

            function onMove(moveEvent) {
                const movePoint = getClientPoint(moveEvent);
                const delta = movePoint.x - startX;
                if (which === "left") {
                    const maxLeft = workspaceRect.width - MIN_CENTER_WIDTH - rightPane.getBoundingClientRect().width - 16;
                    const nextLeft = clamp(leftStart + delta, MIN_LEFT_WIDTH, maxLeft);
                    workspace.style.setProperty("--left-col", `${nextLeft}px`);
                    return;
                }

                const leftCurrent = leftPane.getBoundingClientRect().width;
                const maxRight = workspaceRect.width - MIN_CENTER_WIDTH - leftCurrent - 16;
                const nextRight = clamp(rightStart - delta, MIN_RIGHT_WIDTH, maxRight);
                workspace.style.setProperty("--right-col", `${nextRight}px`);
            }

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

        if (window.PointerEvent) {
            resizer.addEventListener("pointerdown", startResize);
        } else {
            resizer.addEventListener("mousedown", startResize);
            resizer.addEventListener(
                "touchstart",
                (event) => {
                    startResize(event);
                },
                { passive: false },
            );
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

    const data = {
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
            data.heights[id] = panel.style.flexBasis;
        }
    });

    localStorage.setItem(PANEL_LAYOUT_KEY, JSON.stringify(data));
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
    applyPanelOrder("left", parsed.columns && parsed.columns.left, assigned);
    applyPanelOrder("center", parsed.columns && parsed.columns.center, assigned);
    applyPanelOrder("right", parsed.columns && parsed.columns.right, assigned);

    const centerColumn = document.querySelector(".center-pane");
    document.querySelectorAll(".dock-panel").forEach((panel) => {
        const id = panel.dataset.panel;
        if (!id || assigned.has(id) || !centerColumn) {
            return;
        }
        centerColumn.appendChild(panel);
    });

    const workspace = byId("workspace");
    if (workspace && parsed.widths) {
        if (parsed.widths.left) {
            workspace.style.setProperty("--left-col", parsed.widths.left);
        }
        if (parsed.widths.right) {
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

window.addEventListener("load", function () {
    const codeEl = byId("code");
    const savedCode = localStorage.getItem("code");

    if (savedCode) {
        codeEl.value = savedCode;
    } else {
        codeEl.value = [
            "ORG 0000H",
            "MOV A,#05H",
            "MOV R0,#03H",
            "ACALL DELAY",
            "SJMP MAIN",
            "DELAY: DJNZ R0,DELAY",
            "RET",
            "MAIN: NOP",
        ].join("\n");
    }

    initializeTheme();
    restorePanelLayout();
    addDragPills();
    setupPanelDocking();
    setupColumnResizers();
    buildRowResizers();
    setupConverter();
    setupHelpPopover();
    setupWavePanel();
    configureWaveformChannels(codeEl.value);

    updateSpeedLabel();
    renderGutter();
    updateBreakpointSummary();
    setStatus("Ready. Assemble code to start simulation.");
    setStatusExtra("Mode: AT89C51");
    logDebug("Debugger initialized.", "info", true);
    setControlsState();

    codeEl.addEventListener("input", () => {
        localStorage.setItem("code", codeEl.value);
        renderGutter();
        if (!simState.assembled) {
            configureWaveformChannels(codeEl.value);
        }
    });

    codeEl.addEventListener("scroll", syncGutterScroll);

    byId("gutter").addEventListener("click", (event) => {
        const target = event.target.closest(".gutter-line");
        if (!target) {
            return;
        }
        const lineNo = parseInt(target.dataset.line, 10);
        if (!Number.isInteger(lineNo)) {
            return;
        }
        toggleBreakpoint(lineNo);
    });

    byId("assemble").addEventListener("click", async () => {
        await assembleProgram();
    });

    byId("step").addEventListener("click", async () => {
        await executeSingleStep();
    });

    byId("run").addEventListener("click", async () => {
        await runLoop();
    });

    byId("pause").addEventListener("click", () => {
        pauseExecution();
    });

    byId("stop").addEventListener("click", async () => {
        await stopAndRewindExecution();
    });

    byId("run_to_cursor").addEventListener("click", async () => {
        const lineNo = getCursorLine();
        await runLoop({ runToLine: lineNo });
    });

    byId("reset").addEventListener("click", async () => {
        await resetSimulator();
    });

    byId("clear_breakpoints").addEventListener("click", () => {
        simState.breakpoints.clear();
        renderGutter();
        logDebug("All breakpoints cleared.", "warn");
    });

    byId("run_speed").addEventListener("input", () => {
        updateSpeedLabel();
    });

    byId("download_output").addEventListener("click", () => {
        downloadOutputReport();
    });

    byId("theme_toggle").addEventListener("click", () => {
        const nextTheme = simState.theme === "dark" ? "light" : "dark";
        setTheme(nextTheme);
        logDebug(`Theme switched to ${nextTheme}.`, "info");
    });

    window.addEventListener("keydown", async (event) => {
        const targetTag = event.target ? event.target.tagName : "";
        const isTypingContext = targetTag === "INPUT" || targetTag === "TEXTAREA";

        if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
            event.preventDefault();
            await assembleProgram();
            return;
        }

        if (event.key === "F10") {
            event.preventDefault();
            await executeSingleStep();
            return;
        }

        if (event.shiftKey && event.key === "F5") {
            event.preventDefault();
            await stopAndRewindExecution();
            return;
        }

        if (event.key === "F5") {
            event.preventDefault();
            await runLoop();
            return;
        }

        if (event.key === "F6") {
            event.preventDefault();
            pauseExecution();
            return;
        }

        if (event.key === "F9" && !isTypingContext) {
            event.preventDefault();
            toggleBreakpoint(getCursorLine());
            return;
        }

        if (event.key === "Escape" && simState.isRunning) {
            event.preventDefault();
            pauseExecution("Execution paused by user.");
        }
    });

    byId("memory_edit_input").addEventListener("keyup", async (event) => {
        if (event.key !== "Enter") {
            return;
        }

        event.preventDefault();
        await applyMemoryEdit(event.target.value);
    });

    hideLoader();
});
