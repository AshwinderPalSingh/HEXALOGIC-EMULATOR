export class SimulatorApiError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = "SimulatorApiError";
    this.details = details;
  }
}

const SESSION_STORAGE_KEY = "hexalogic-session-id";

export class Sim8051Client {
  constructor({ baseUrl = "/api/v2" } = {}) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.sessionId = this.#loadSessionId();
  }

  state() {
    return this.#request("GET", "/state");
  }

  metrics() {
    return this.#request("GET", "/metrics");
  }

  reset() {
    return this.#request("POST", "/reset");
  }

  assemble(code) {
    return this.#request("POST", "/assemble", { code });
  }

  step() {
    return this.#request("POST", "/step");
  }

  stepOver() {
    return this.#request("POST", "/step-over");
  }

  stepOut() {
    return this.#request("POST", "/step-out");
  }

  stepBack() {
    return this.#request("POST", "/step-back");
  }

  run(maxSteps = 1000, speedMultiplier = 1) {
    return this.#request("POST", "/run", { max_steps: maxSteps, speed_multiplier: speedMultiplier });
  }

  setArchitecture(architecture) {
    return this.#request("POST", "/architecture", { architecture });
  }

  setEndian(endian) {
    return this.#request("POST", "/endian", { endian });
  }

  setDebugMode(enabled) {
    return this.#request("POST", "/debug", { enabled });
  }

  setExecutionMode(mode) {
    return this.#request("POST", "/execution-mode", { mode });
  }

  setBreakpoints(pcs) {
    return this.#request("POST", "/breakpoints", { pcs });
  }

  setWatchpoints(watchpoints) {
    return this.#request("POST", "/watchpoints", { watchpoints });
  }

  setPin(port, bit, level) {
    return this.#request("POST", "/pins", { port, bit, level });
  }

  pushSerialRx(bytes) {
    return this.#request("POST", "/serial/rx", { bytes });
  }

  writeMemory(space, address, value) {
    return this.#request("POST", "/memory", { space, address, value });
  }

  setClock(hz) {
    return this.#request("POST", "/clock", { hz });
  }

  exportSession() {
    return this.#request("GET", "/export");
  }

  importSession(session) {
    return this.#request("POST", "/import", { session });
  }

  hardwareAddDevice(type, label) {
    return this.#request("POST", "/hardware/device", { type, label });
  }

  hardwareUpdateDevice(payload) {
    return this.#request("PATCH", "/hardware/device", payload);
  }

  hardwareRemoveDevice(id) {
    return this.#request("DELETE", "/hardware/device", { id });
  }

  hardwareSetSwitch(deviceId, level) {
    return this.#request("POST", "/hardware/switch", { device_id: deviceId, level });
  }

  hardwareSetFault(signal, type, options = {}) {
    return this.#request("POST", "/hardware/fault", { signal, type, enabled: true, ...options });
  }

  hardwareClearFault(signal) {
    return this.#request("POST", "/hardware/fault", { signal, enabled: false, type: "stuck_low" });
  }

  hardwareExport() {
    return this.#request("GET", "/hardware/export");
  }

  hardwareImport(hardware) {
    return this.#request("POST", "/hardware/import", { hardware });
  }

  hardwareBridge() {
    return this.#request("GET", "/hardware/bridge");
  }

  hardwareTest() {
    return this.#request("POST", "/hardware/test");
  }

  eventStreamUrl(path) {
    const url = new URL(`${this.baseUrl}${path}`, window.location.href);
    if (this.sessionId) {
      url.searchParams.set("session_id", String(this.sessionId));
    }
    return url.toString();
  }

  async #request(method, path, body) {
    const requestStartedAtMs = window.performance?.now?.() ?? Date.now();
    const headers = body ? { "Content-Type": "application/json" } : {};
    if (this.sessionId) {
      headers["X-Hexlogic-Session"] = String(this.sessionId);
    }
    const response = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers: Object.keys(headers).length ? headers : undefined,
      credentials: "same-origin",
      body: body ? JSON.stringify(body) : undefined,
    });
    const responseReceivedAtMs = window.performance?.now?.() ?? Date.now();

    const payload = await response.json().catch(() => ({}));
    const headerSession = response.headers.get("x-hexlogic-session");
    if (headerSession) {
      this.#rememberSessionId(headerSession);
    }
    if (payload?.session_id) {
      this.#rememberSessionId(payload.session_id);
    }
    if (payload && typeof payload === "object") {
      Object.defineProperty(payload, "__clientTiming", {
        value: {
          requestStartedAtMs,
          responseReceivedAtMs,
        },
        enumerable: false,
        configurable: true,
      });
    }
    if (!response.ok) {
      const error = payload?.error ?? {};
      throw new SimulatorApiError(error.message || response.statusText, error);
    }
    return payload;
  }

  #loadSessionId() {
    try {
      return window.localStorage?.getItem(SESSION_STORAGE_KEY) || null;
    } catch (_error) {
      return null;
    }
  }

  #rememberSessionId(sessionId) {
    this.sessionId = sessionId ? String(sessionId) : null;
    try {
      if (this.sessionId) {
        window.localStorage?.setItem(SESSION_STORAGE_KEY, this.sessionId);
      } else {
        window.localStorage?.removeItem(SESSION_STORAGE_KEY);
      }
    } catch (_error) {
    }
  }
}
