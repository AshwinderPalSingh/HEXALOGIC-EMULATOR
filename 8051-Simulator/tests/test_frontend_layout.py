from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "api" / "static" / "layout-utils.js"
DOCK_MODULE = ROOT / "api" / "static" / "dock-layout.js"
APP_MODULE = ROOT / "api" / "static" / "sim8051-app.js"
STATIC_INDEX = ROOT / "index.html"
FLASK_SHELL = ROOT / "api" / "templates" / "_app_shell.html"
NETLIFY_CONFIG = ROOT / "netlify.toml"


def _run_node_layout_probe() -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not available in this environment")
    script = f"""
import * as layout from {json.dumps(MODULE.as_uri())};
import * as dock from {json.dumps(DOCK_MODULE.as_uri())};

const vertical = layout.resolveVerticalSplit(240, 260, 80);
const left = layout.resolveLeftColumnWidth({{
  leftStart: 320,
  delta: 120,
  workspaceWidth: 1400,
  rightWidth: 520,
}});
const right = layout.resolveRightColumnWidth({{
  rightStart: 560,
  delta: -140,
  workspaceWidth: 1400,
  leftWidth: 320,
}});
const center = dock.resolveStackDropZone(
  {{ left: 100, top: 100, right: 320, bottom: 280, width: 220, height: 180 }},
  210,
  190,
);
const edge = dock.resolveStackDropZone(
  {{ left: 100, top: 100, right: 320, bottom: 280, width: 220, height: 180 }},
  108,
  190,
);
let root = dock.createSplit(\"row\", [dock.createStack([\"a\"]), dock.createStack([\"b\"])], [0.5, 0.5]);
const rightId = root.children[1].id;
root = dock.splitStackInLayout(root, rightId, \"bottom\", \"c\");
const inserted = dock.findPanelLocation(root, \"c\");
root = dock.removePanelFromLayout(root, \"c\");
const removed = dock.findPanelLocation(root, \"c\");

console.log(JSON.stringify({{
  vertical,
  left,
  right,
  center,
  edge,
  insertedType: inserted?.parent?.type || inserted?.stack?.type || null,
  removed: removed || null,
}}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout.strip())


def test_frontend_layout_resize_math_stays_within_expected_bounds():
    payload = _run_node_layout_probe()

    assert payload["vertical"] == {"previous": 320, "next": 180}
    assert payload["left"] == 440
    assert payload["right"] == 644
    assert payload["center"]["zone"] == "center"
    assert payload["edge"]["zone"] == "left"
    assert payload["insertedType"] == "split"
    assert payload["removed"] is None


def test_frontend_renderer_uses_null_safe_panel_updates():
    source = APP_MODULE.read_text()
    shell = FLASK_SHELL.read_text()

    assert "function safeSetHTML" in source
    assert "function formatTraceLogEntry" in source
    assert "function formatRunSummary" in source
    assert "function refreshPanelRegistry" in source
    assert "function _beginUiTimingMeasurement" in source
    assert "function _finishUiTimingMeasurement" in source
    assert "ui_receive_to_paint_ms" in source
    assert "ui_dropped_frames" in source

    critical_patterns = [
        'byId("exec-state-panel").innerHTML',
        'byId("registers-panel-body").innerHTML',
        'byId("memory-ram").innerHTML',
        'byId("memory-xram").innerHTML',
        'byId("memory-rom").innerHTML',
        'byId("assembler-panel-body").innerHTML',
        'byId("trace-panel-body").innerHTML',
    ]
    for pattern in critical_patterns:
        assert pattern not in source

    unsafe_trace_patterns = [
        'logConsole(`${trace.mnemonic}',
        'logConsole(`${step.mnemonic}',
        'lastStep.mnemonic',
        'reverted.mnemonic',
    ]
    for pattern in unsafe_trace_patterns:
        assert pattern not in source

    assert '"8051": ""' in source
    assert 'arm: ""' in source
    assert "formatTimerFields" in source
    assert "notifyUnrelatedHardwareIfNeeded" in source
    assert "Booting sandboxed multi-architecture debugger" not in shell
    assert "Sandboxed Multi-Architecture Debug View" not in shell
    assert "Glyph margin toggles breakpoints" not in shell
    assert "Target Explorer" not in shell


def test_static_ui_controls_are_bound_to_runtime_handlers():
    source = APP_MODULE.read_text()

    bindings = {
        "assemble": 'byId("assemble").addEventListener("click", handleAssemble)',
        "run": 'byId("run").addEventListener("click", runLoop)',
        "step": 'byId("step").addEventListener("click", () => handleSingleStep("step"))',
        "step_over": 'byId("step_over").addEventListener("click", () => handleSingleStep("stepOver"))',
        "step_out": 'byId("step_out").addEventListener("click", () => handleSingleStep("stepOut"))',
        "step_back": 'byId("step_back").addEventListener("click", handleStepBack)',
        "pause": 'byId("pause").addEventListener("click", () => {',
        "stop": 'byId("stop").addEventListener("click", () => {',
        "run_to_cursor": 'byId("run_to_cursor").addEventListener("click", handleRunToCursor)',
        "reset": 'byId("reset").addEventListener("click", handleReset)',
        "download_output": 'byId("download_output").addEventListener("click", downloadSnapshot)',
        "export_state": 'byId("export_state").addEventListener("click", exportSessionState)',
        "import_state": 'byId("import_state").addEventListener("click", importSessionState)',
        "waveform_toggle": 'byId("waveform_toggle").addEventListener("click", () => setWaveDrawerOpen(byId("wave-drawer").hidden))',
        "wave-collapse": 'byId("wave-collapse").addEventListener("click", (event) => {',
        "help_toggle": 'byId("help_toggle").addEventListener("click", () => byId("help-popover").toggleAttribute("hidden"))',
        "theme_toggle": 'byId("theme_toggle").addEventListener("click", () => setTheme(appState.theme === "dark" ? "light" : "dark"))',
        "clear_breakpoints": 'byId("clear_breakpoints").addEventListener("click", async () => {',
        "architecture_select": 'byId("architecture_select").addEventListener("change", handleArchitectureChange)',
        "endian_select": 'byId("endian_select").addEventListener("change", handleEndianChange)',
        "debug_toggle": 'byId("debug_toggle").addEventListener("change", handleDebugToggle)',
        "execution_mode_select": 'byId("execution_mode_select").addEventListener("change", handleExecutionModeChange)',
        "run_speed": 'byId("run_speed").addEventListener("input", updateRunSpeedLabel)',
        "clock_preset": 'byId("clock_preset").addEventListener("change", handleClockPresetChange)',
        "clock_apply": 'byId("clock_apply").addEventListener("click", handleClockApply)',
        "clock_input": 'byId("clock_input").addEventListener("keydown", (event) => {',
        "convert_input": 'byId("convert_input").addEventListener("input", updateBaseConverter)',
        "convert_from": 'byId("convert_from").addEventListener("change", updateBaseConverter)',
        "convert_to": 'byId("convert_to").addEventListener("change", updateBaseConverter)',
        "convert_swap": 'byId("convert_swap").addEventListener("click", swapBaseConverter)',
        "memory_edit_input": 'byId("memory_edit_input").addEventListener("keydown", (event) => {',
        "vh_export_btn": 'byId("vh_export_btn")?.addEventListener("click", async () => {',
        "vh_import_btn": 'byId("vh_import_btn")?.addEventListener("click", () => {',
        "vh_test_btn": 'byId("vh_test_btn")?.addEventListener("click", async () => {',
        "vh_zoom_in": 'byId("vh_zoom_in")?.addEventListener("click", () => {',
        "vh_zoom_out": 'byId("vh_zoom_out")?.addEventListener("click", () => {',
        "vh_zoom_reset": 'byId("vh_zoom_reset")?.addEventListener("click", () => {',
        "workspace_code": '["workspace_code", "workspace_hardware"].forEach((id) => {',
        "workspace_hardware": '["workspace_code", "workspace_hardware"].forEach((id) => {',
    }

    for control_id, binding in bindings.items():
        assert binding in source, control_id


def test_dock_layout_keeps_inactive_panels_mounted_for_stable_rendering():
    source = DOCK_MODULE.read_text()

    assert "panel.hidden = panelId !== node.active;" in source
    assert "body.appendChild(panel);" in source


def test_dock_layout_uses_transform_based_panel_motion():
    source = DOCK_MODULE.read_text()

    assert "translate3d(" in source
    assert "panel.style.transform =" in source


def test_hardware_workspace_css_forces_center_column_to_stretch():
    source = (ROOT / "api" / "static" / "styles.css").read_text()

    assert '.ide-root[data-workspace="hardware"] .center-pane {' in source
    assert "height: 100%;" in source
    assert "align-self: stretch;" in source
    assert "overflow: hidden;" in source


def test_code_workspace_constrains_debug_panels_and_console_growth():
    source = (ROOT / "api" / "static" / "styles.css").read_text()
    app_source = APP_MODULE.read_text()

    assert ".code-workspace {" in source
    assert "display: flex;" in source
    assert "flex-direction: column;" in source
    assert "flex: 1 1 auto;" in source
    assert "DEBUG_CONSOLE_MAX_LINES = 240" in app_source
    assert "debugConsoleState.lastNode.textContent = `${message} [x${debugConsoleState.repeatCount}]`;" in app_source
    assert "function shouldAutoFollowScroll" in app_source
    assert "if (appState.running) {" in app_source
    assert '.ide-root[data-workspace="code"] .center-pane .debugger-panel {' in source
    assert '.ide-root[data-workspace="code"] .center-pane .trace-panel {' in source
    assert "flex: 0 0 170px;" in source
    assert "flex: 0 0 210px;" in source


def test_static_netlify_entry_exposes_required_app_shell_ids():
    source = STATIC_INDEX.read_text()

    required_ids = {
        "ide-root",
        "target-chip",
        "workspace_code",
        "workspace_hardware",
        "architecture_select",
        "endian_select",
        "debug_toggle",
        "execution_mode_select",
        "clock_preset",
        "clock_input",
        "clock_apply",
        "step_over",
        "step_out",
        "step_back",
        "export_state",
        "import_state",
        "editor-host",
        "project-tree",
        "exec-state-panel",
        "registers-panel-body",
        "call-stack-body",
        "assembler-panel-body",
        "trace-panel-body",
        "memory-ram",
        "memory-xram",
        "memory-rom",
        "metrics-panel-body",
        "hardware-panel",
        "vh-component-palette",
        "vh-board-viewport",
        "hardware-stage",
        "vh-wire-layer",
        "hardware-canvas",
        "vh-port-values",
        "vh-component-states",
        "vh-validation-errors",
        "vh-signal-log",
        "vh-logic-analyzer",
        "vh-inspector",
        "vh-test-results",
        "toast-stack",
    }

    for element_id in required_ids:
        assert f'id="{element_id}"' in source

    assert 'window.HEXLOGIC_API_BASE = "/api/v2";' in source
    assert '/api/static/styles.css' in source
    assert 'type="module" src="/src/main.js"' in source
    assert "/api/static/sim8051-app.js" not in source
    assert "/api/static/sim8051-client.js" not in source


def test_flask_app_shell_exposes_same_runtime_controls_as_static_entry():
    source = FLASK_SHELL.read_text()

    for element_id in {
        "assemble",
        "run",
        "step",
        "step_over",
        "step_out",
        "step_back",
        "pause",
        "stop",
        "reset",
        "workspace_code",
        "workspace_hardware",
        "clock_preset",
        "clock_input",
        "clock_apply",
        "waveform_toggle",
        "wave-collapse",
        "debug-console",
        "trace-panel-body",
    }:
        assert f'id="{element_id}"' in source


def test_netlify_build_uses_vite_dist_output():
    config = NETLIFY_CONFIG.read_text()
    assert 'publish = "dist"' in config
    assert 'command = "npm run build"' in config
