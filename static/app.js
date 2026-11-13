const AUTO_REFRESH_INTERVAL_MS = 10000;
const WS_RECONNECT_DELAY_MS = 3000;
const SESSION_TOKEN_STORAGE_KEY = "systemalerts_session_token";
const VALID_RAID_STATES = new Set([
  "clean",
  "active",
  "degraded",
  "recovering",
  "resyncing",
  "failed",
  "error",
  "unknown",
  "none",
]);

let refreshTimerId = null;
let wsConnection = null;
let wsReconnectTimerId = null;
let sseSource = null;
let sseConnected = false;
let sseRetryCount = 0;
let sseReconnectTimerId = null;
const SSE_ADAPTIVE_POLL_INTERVAL_MS = 60000;
let localTickerTimerId = null;
let currentBootTime = null;
let currentWeeklyDueAt = null;
let isFetching = false;
let isAuthenticated = false;
let currentGoogleClientId = "";
let currentViewTab = "overview";
let currentDockerSubtab = "stacks";
let dockerStatusCache = null;
let activeLogContainerId = null;
let activeLogContainerName = "";
let logsTimerId = null;
let activeTerminalContainerId = null;
let activeTerminalContainerName = "";
let terminalWs = null;
let terminalUtf8Decoder = new TextDecoder("utf-8", { stream: true });
let terminalLines = [[]];
let terminalCursorRow = 0;
let terminalCursorCol = 0;
let terminalCursorVisible = true;
let terminalSavedLines = null;
let terminalSavedCursorRow = 0;
let terminalSavedCursorCol = 0;
let terminalCurrentStyle = {
  fg: null,
  bg: null,
  bold: false,
  dim: false,
  italic: false,
  underline: false,
  inverse: false,
};
let terminalParserState = "NORMAL";
let terminalCsiParams = "";
let terminalOscParams = "";
let terminalPendingBuffer = "";
let terminalRenderScheduled = false;
let cachedDockerContainers = [];
let cachedDockerStacks = [];
let cachedDockerVolumes = [];
let cachedDockerUpdates = null;
let cachedAppTemplates = [];
let currentAppCatalogCategory = "All";
let activeStackEditorTab = "compose";
let cachedStorageDisks = null;

const MEMORY_HISTORY_LIMIT = 30;
const memoryHistoryBuffer = [];

const CPU_CORE_HISTORY_LIMIT = 60;
const cpuCoresHistoryBuffers = [];
let activeHoveredCoreIndex = null;
const CPU_CORE_COLORS = [
  "#00f0ff",
  "#10b981",
  "#f59e0b",
  "#a855f7",
  "#ec4899",
  "#38bdf8",
  "#84cc16",
  "#f97316",
  "#eab308",
  "#06b6d4",
  "#6366f1",
  "#14b8a6",
  "#d946ef",
  "#e11d48",
  "#22c55e",
  "#3b82f6",
  "#8b5cf6",
  "#f43f5e",
  "#0ea5e9",
  "#fbbf24",
];

/**
 * 60 FPS Decoupled Live Telemetry State & Oscilloscope Engine
 */
const liveAnimState = {
  // Target values updated by SSE/WS/polling packets
  targetCpu: 0,
  targetRam: 0,
  targetDisk: 0,
  targetCores: [],
  targetNetIn: 0,
  targetNetOut: 0,

  // Fluid 60 FPS interpolated values (speedometer glide)
  currentCpu: 0,
  currentRam: 0,
  currentDisk: 0,
  currentCores: [],
  currentNetIn: 0,
  currentNetOut: 0,

  // Last rendered values for change threshold (> 0.05%)
  renderedCpu: -1,
  renderedRam: -1,
  renderedDisk: -1,
  renderedCores: [],

  // Timing & Continuous Oscilloscope Scroll
  lastFrameTime: typeof performance !== "undefined" && typeof performance.now === "function" ? performance.now() : Date.now(),
  lastSampleTime: 0,
  estimatedSampleInterval: 250,
  rafId: null,
  isRunning: false,
  initialized: false,
};

/**
 * LERP interpolation helper for smooth speedometer & meter gliding
 */
function lerpValue(current, target, factor) {
  const diff = target - current;
  if (Math.abs(diff) < 0.005) {
    return target;
  }
  return current + diff * factor;
}

/**
 * Update continuous system uptime and countdown indicators smoothly
 */
function updateContinuousUptimeAndClocks(nowMs) {
  if (currentBootTime) {
    const uptimeValEl = document.getElementById("uptime-value");
    if (uptimeValEl) {
      const nowSeconds = Date.now() / 1000;
      const elapsed = Math.max(0, nowSeconds - Number(currentBootTime));
      const days = Math.floor(elapsed / 86400);
      const hours = Math.floor((elapsed % 86400) / 3600);
      const minutes = Math.floor((elapsed % 3600) / 60);
      const seconds = Math.floor(elapsed % 60);

      let formatted = "";
      if (days > 0) {
        formatted = `${days}d ${hours}h ${minutes}m ${seconds}s`;
      } else if (hours > 0) {
        formatted = `${hours}h ${minutes}m ${seconds}s`;
      } else if (minutes > 0) {
        formatted = `${minutes}m ${seconds}s`;
      } else {
        formatted = `${seconds}s`;
      }

      if (uptimeValEl.textContent !== formatted) {
        uptimeValEl.textContent = formatted;
      }
    }
  }

  if (currentWeeklyDueAt !== null && currentWeeklyDueAt !== undefined) {
    const countdownEl = document.getElementById("weekly-countdown");
    if (countdownEl) {
      const nowSec = Date.now() / 1000;
      const remaining = Math.floor(currentWeeklyDueAt - nowSec);
      if (!Number.isFinite(currentWeeklyDueAt) || remaining <= 0) {
        if (countdownEl.textContent !== "Due on the next clean/active RAID check") {
          countdownEl.textContent = "Due on the next clean/active RAID check";
        }
      } else {
        const days = Math.floor(remaining / 86400);
        const hours = Math.floor((remaining % 86400) / 3600);
        const minutes = Math.floor((remaining % 3600) / 60);
        const seconds = Math.floor(remaining % 60);
        const formatted = `${days}d ${hours}h ${minutes}m ${seconds}s remaining`;
        if (countdownEl.textContent !== formatted) {
          countdownEl.textContent = formatted;
        }
      }
    }
  }
}

/**
 * 60 FPS Decoupled Live Telemetry Render Tick
 */
function liveRenderTick(frameTime) {
  if (!liveAnimState.isRunning) return;

  const now = typeof performance !== "undefined" && typeof performance.now === "function" ? performance.now() : Date.now();
  const factor = 0.18; // smooth responsive fluid glide

  // 1. Fluid CPU Speedometer Glide
  liveAnimState.currentCpu = lerpValue(liveAnimState.currentCpu, liveAnimState.targetCpu, factor);
  if (Math.abs(liveAnimState.currentCpu - liveAnimState.renderedCpu) > 0.05) {
    const cpuValEl = document.getElementById("cpu-percent-value");
    if (cpuValEl) setTextIfChanged(cpuValEl, `${liveAnimState.currentCpu.toFixed(1)}%`);
    liveAnimState.renderedCpu = liveAnimState.currentCpu;
  }

  // 2. Fluid RAM Meter Glide
  liveAnimState.currentRam = lerpValue(liveAnimState.currentRam, liveAnimState.targetRam, factor);
  if (Math.abs(liveAnimState.currentRam - liveAnimState.renderedRam) > 0.05) {
    const memValEl = document.getElementById("memory-percent-value");
    if (memValEl) setTextIfChanged(memValEl, `${liveAnimState.currentRam.toFixed(1)}%`);
    liveAnimState.renderedRam = liveAnimState.currentRam;
  }

  // 3. Fluid CPU Per-Core Glide
  if (Array.isArray(liveAnimState.targetCores) && liveAnimState.targetCores.length > 0) {
    while (liveAnimState.currentCores.length < liveAnimState.targetCores.length) {
      liveAnimState.currentCores.push(liveAnimState.targetCores[liveAnimState.currentCores.length]);
    }
    while (liveAnimState.renderedCores.length < liveAnimState.targetCores.length) {
      liveAnimState.renderedCores.push(-1);
    }

    const coresGrid = document.getElementById("cpu-cores-grid");
    const items = coresGrid && coresGrid.querySelectorAll ? coresGrid.querySelectorAll(".cpu-core-item") : null;

    for (let idx = 0; idx < liveAnimState.targetCores.length; idx++) {
      liveAnimState.currentCores[idx] = lerpValue(
        liveAnimState.currentCores[idx] || 0,
        liveAnimState.targetCores[idx],
        factor
      );

      if (Math.abs(liveAnimState.currentCores[idx] - (liveAnimState.renderedCores[idx] || 0)) > 0.05) {
        if (items && items[idx]) {
          const item = items[idx];
          const pctEl = item.querySelector ? item.querySelector(".cpu-core-percent") : null;
          if (pctEl) setTextIfChanged(pctEl, `${liveAnimState.currentCores[idx].toFixed(1)}%`);
        }
        liveAnimState.renderedCores[idx] = liveAnimState.currentCores[idx];
      }
    }
  }

  // 4. Continuous Rolling Multi-Core Oscilloscope Waveform
  const unifiedCanvas =
    document.getElementById("cpu-cores-unified-canvas") ||
    (document.querySelector && document.querySelector(".cpu-core-canvas"));

  if (unifiedCanvas && typeof drawCpuCoreGraph === "function") {
    const isVisible = unifiedCanvas.offsetParent !== null || unifiedCanvas.clientWidth > 0;
    if (isVisible && cpuCoresHistoryBuffers.length > 0) {
      drawCpuCoreGraph(
        unifiedCanvas,
        cpuCoresHistoryBuffers,
        liveAnimState.currentCores,
        typeof activeHoveredCoreIndex !== "undefined" ? activeHoveredCoreIndex : null,
        true
      );
    }
  }

  // 5. Continuous Hardware Clock & Uptime
  updateContinuousUptimeAndClocks(now);
}

let isSchedulingLiveFrame = false;

/**
 * Start the 60 FPS Decoupled Live Telemetry Render Loop
 */
function startLiveRenderLoop() {
  if (liveAnimState.isRunning) return;
  liveAnimState.isRunning = true;
  liveAnimState.lastFrameTime = typeof performance !== "undefined" && typeof performance.now === "function"
    ? performance.now()
    : Date.now();

  function scheduleNextLiveFrame() {
    if (!liveAnimState.isRunning || isSchedulingLiveFrame) return;
    isSchedulingLiveFrame = true;
    try {
      const raf = typeof window !== "undefined" && typeof window.requestAnimationFrame === "function"
        ? window.requestAnimationFrame
        : (typeof requestAnimationFrame === "function" ? requestAnimationFrame : null);

      if (raf) {
        liveAnimState.rafId = raf((frameTime) => {
          liveRenderTick(frameTime);
          if (liveAnimState.isRunning && !isSchedulingLiveFrame) {
            scheduleNextLiveFrame();
          }
        });
      }
    } finally {
      isSchedulingLiveFrame = false;
    }
  }

  scheduleNextLiveFrame();
}

/**
 * Stop the 60 FPS Decoupled Live Telemetry Render Loop
 */
function stopLiveRenderLoop() {
  liveAnimState.isRunning = false;
  if (liveAnimState.rafId) {
    const caf = typeof window !== "undefined" && typeof window.cancelAnimationFrame === "function"
      ? window.cancelAnimationFrame
      : (typeof cancelAnimationFrame === "function" ? cancelAnimationFrame : null);
    if (caf) caf(liveAnimState.rafId);
    liveAnimState.rafId = null;
  }
}

let systemUpdatePollTimerId = null;
let systemUpdateNoticeDismissed = false;
let lastKnownUpdateSignature = null;
const SYSTEM_UPDATE_POLL_INTERVAL_MS = 5 * 60 * 1000;

let isFetchingDockerStatus = false;
let isFetchingDockerStacks = false;
let isFetchingDockerContainers = false;
let isFetchingDockerImages = false;
let isFetchingDockerVolumes = false;
let isFetchingDockerNetworks = false;
let isFetchingDockerUpdates = false;
let isFetchingContainerLogs = false;
let isFetchingUpdateSchedule = false;

let lastDockerSubtabFetchTime = 0;
let dockerSubtabThrottleTimer = null;
const DOCKER_SUBTAB_THROTTLE_MS = 800;

let lastContainersRenderKey = "";
let lastStacksRenderKey = "";
let lastDockerUpdatesRenderKey = "";
let lastSystemNetSample = null;

/**
 * Global Reactive Live Store with Pub/Sub (VigilLiveStore)
 */
const VigilLiveStore = (function () {
  const state = {
    telemetry: null,
    system: null,
    docker: null,
    raid: null,
    notifications: null,
    probes: null,
    connection: "disconnected",
  };
  const listeners = new Map(); // slice -> Set<Function>

  function subscribe(slice, callback) {
    if (typeof callback !== "function") return () => {};
    if (!listeners.has(slice)) {
      listeners.set(slice, new Set());
    }
    listeners.get(slice).add(callback);
    // Immediately replay current state for this slice if present
    const currentVal = slice === "*" ? state : state[slice];
    if (currentVal !== null && currentVal !== undefined) {
      try {
        callback(currentVal);
      } catch (err) {
        console.error(`Error in VigilLiveStore immediate subscriber for '${slice}':`, err);
      }
    }
    return () => {
      const set = listeners.get(slice);
      if (set) set.delete(callback);
    };
  }

  function getState(slice) {
    return slice ? state[slice] : state;
  }

  function dispatch(slice, data) {
    if (!slice) return;
    state[slice] = data;
    const directListeners = listeners.get(slice);
    if (directListeners) {
      directListeners.forEach((cb) => {
        try {
          cb(data);
        } catch (err) {
          console.error(`Error in VigilLiveStore listener for '${slice}':`, err);
        }
      });
    }
    const wildcardListeners = listeners.get("*");
    if (wildcardListeners) {
      wildcardListeners.forEach((cb) => {
        try {
          cb(slice, data);
        } catch (err) {
          console.error("Error in VigilLiveStore wildcard listener:", err);
        }
      });
    }
  }

  return { subscribe, getState, dispatch };
})();

if (typeof window !== "undefined") {
  window.VigilLiveStore = VigilLiveStore;
}
if (typeof globalThis !== "undefined") {
  globalThis.VigilLiveStore = VigilLiveStore;
}

/**
 * Retrieve stored session token if present.
 */
function getStoredToken() {
  try {
    return localStorage.getItem(SESSION_TOKEN_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

/**
 * Save or clear session token in localStorage.
 */
function setStoredToken(token) {
  try {
    if (token) {
      localStorage.setItem(SESSION_TOKEN_STORAGE_KEY, token);
    } else {
      localStorage.removeItem(SESSION_TOKEN_STORAGE_KEY);
    }
  } catch {
    // Ignore storage errors in restricted browsing modes
  }
}

/**
 * Wrapper around fetch() that attaches session credentials and handles 401 responses.
 */
async function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }
  const token = getStoredToken();
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const fetchOptions = {
    ...options,
    headers,
    credentials: "same-origin",
  };

  const response = await fetch(url, fetchOptions);
  if (response.status === 401 && !url.startsWith("/api/auth/")) {
    await handleUnauthorized();
  }
  return response;
}

/**
 * Format raw byte count into human-readable GB/TB string.
 * Supports numbers or pre-formatted strings gracefully.
 */
function formatBytes(bytes) {
  if (typeof bytes === "string" && isNaN(Number(bytes)) && bytes.trim().length > 0) {
    return bytes.trim();
  }
  const num = Number(bytes);
  if (!Number.isFinite(num) || num < 0) {
    return "--";
  }
  if (num === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.min(
    Math.floor(Math.log(num) / Math.log(1024)),
    units.length - 1
  );
  const value = num / Math.pow(1024, i);
  return `${value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2)} ${units[i]}`;
}

/**
 * Generate ASCII progress bar e.g. [======>     ] for RAID rebuild/sync.
 */
function generateAsciiProgressBar(percent, innerLength = 12) {
  const p = Math.max(0, Math.min(100, Number(percent) || 0));
  if (p >= 100) {
    return `[${"=".repeat(innerLength)}]`;
  }
  const equalsCount = Math.min(
    innerLength - 1,
    Math.max(0, Math.round((p / 100) * (innerLength - 1)))
  );
  const equals = "=".repeat(equalsCount);
  const spaces = " ".repeat(Math.max(0, innerLength - 1 - equalsCount));
  return `[${equals}>${spaces}]`;
}

/**
 * Extract active RAID sync/rebuild info from sysfs attributes or status.
 */
function getRaidSyncInfo(raid) {
  if (!raid || typeof raid !== "object") return null;

  const rawAction = String(raid.sync_action || "").trim().toLowerCase();
  const statusStr = String(raid.status || "").trim().toLowerCase();
  const checkStatus = String(raid.check_status || "").trim().toLowerCase();

  const isSyncing =
    ["resync", "recover", "recovering", "check", "checking", "repair", "syncing"].includes(rawAction) ||
    ["recovering", "resyncing"].includes(statusStr) ||
    checkStatus.includes("sync") ||
    checkStatus.includes("recover") ||
    checkStatus.includes("check") ||
    (checkStatus.includes("/") && checkStatus !== "N/A");

  let percent = null;
  if (raid.sync_percent !== undefined && raid.sync_percent !== null && raid.sync_percent !== "") {
    const p = parseFloat(String(raid.sync_percent).replace("%", ""));
    if (Number.isFinite(p)) percent = Math.max(0, Math.min(100, p));
  } else if (raid.sync_completed !== undefined && raid.sync_completed !== null) {
    if (typeof raid.sync_completed === "string" && raid.sync_completed.includes("/")) {
      const parts = raid.sync_completed.split("/").map((s) => parseFloat(s.trim()));
      if (parts.length === 2 && parts[1] > 0) {
        percent = Math.max(0, Math.min(100, (parts[0] / parts[1]) * 100));
      }
    } else if (Number.isFinite(Number(raid.sync_completed))) {
      percent = parseFloat(raid.sync_completed);
    }
  }

  if (percent === null) {
    const combined = `${checkStatus} ${statusStr} ${String(raid.full_state || "")}`;
    const match = combined.match(/(\d+(?:\.\d+)?)%/);
    if (match) {
      percent = parseFloat(match[1]);
    }
  }

  if (percent === null && checkStatus.includes("/")) {
    const parts = checkStatus.split("/").map((s) => parseFloat(s.trim()));
    if (parts.length === 2 && parts[1] > 0 && Number.isFinite(parts[0])) {
      percent = Math.max(0, Math.min(100, (parts[0] / parts[1]) * 100));
    }
  }

  if (!isSyncing && (percent === null || percent <= 0)) {
    return null;
  }

  let actionName = "Resyncing";
  if (rawAction.includes("recover") || statusStr.includes("recover") || checkStatus.includes("recover")) {
    actionName = "Recovering";
  } else if (rawAction.includes("check") || statusStr.includes("check") || checkStatus.includes("check")) {
    actionName = "Checking";
  } else if (rawAction.includes("repair") || checkStatus.includes("repair")) {
    actionName = "Repairing";
  } else if (rawAction.includes("sync") || statusStr.includes("sync") || checkStatus.includes("sync")) {
    actionName = "Resyncing";
  }

  return {
    action: actionName,
    percent: percent !== null ? percent : 0,
    speed: raid.sync_speed || null,
    eta: raid.sync_eta || null,
  };
}

/**
 * Format uptime duration from boot_time (unix seconds) to "Xd Xh Xm Xs" with continuous second precision.
 */
function formatUptimeFromBoot(bootTimeSeconds, includeSeconds = true) {
  const boot = Number(bootTimeSeconds);
  if (!Number.isFinite(boot) || boot <= 0) {
    return "--";
  }
  const nowSeconds = Date.now() / 1000;
  const elapsed = Math.max(0, nowSeconds - boot);
  const days = Math.floor(elapsed / 86400);
  const hours = Math.floor((elapsed % 86400) / 3600);
  const minutes = Math.floor((elapsed % 3600) / 60);
  const seconds = Math.floor(elapsed % 60);

  if (includeSeconds) {
    if (days > 0) {
      return `${days}d ${hours}h ${minutes}m ${seconds}s`;
    }
    if (hours > 0) {
      return `${hours}h ${minutes}m ${seconds}s`;
    }
    if (minutes > 0) {
      return `${minutes}m ${seconds}s`;
    }
    return `${seconds}s`;
  }

  if (days > 0) {
    return `${days}d ${hours}h ${minutes}m`;
  }
  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  return `${minutes}m`;
}

const TIMEZONE_STORAGE_KEY = "systemalerts_user_timezone";

function getStoredTimezone() {
  try {
    return localStorage.getItem(TIMEZONE_STORAGE_KEY) || "auto";
  } catch {
    return "auto";
  }
}

function setStoredTimezone(tz) {
  try {
    if (tz) {
      localStorage.setItem(TIMEZONE_STORAGE_KEY, tz);
    } else {
      localStorage.removeItem(TIMEZONE_STORAGE_KEY);
    }
  } catch {}
}

function getEffectiveTimezone() {
  const stored = getStoredTimezone();
  if (!stored || stored === "auto") {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    } catch {
      return "UTC";
    }
  }
  return stored;
}

/**
 * Centralized date/time formatter that formats timestamps across the dashboard
 * according to the configured timezone.
 */
function formatDateTime(val, options = {}) {
  if (val === null || val === undefined || val === "" || val === 0) {
    return "--";
  }
  let date = null;
  if (val instanceof Date) {
    date = val;
  } else if (typeof val === "number" || (!isNaN(val) && !String(val).includes("-") && !String(val).includes("T") && !String(val).includes(":"))) {
    const num = Number(val);
    if (!Number.isFinite(num) || num <= 0) return "--";
    date = new Date(num > 1e11 ? num : num * 1000);
  } else if (typeof val === "string") {
    const parsed = Date.parse(val);
    if (!isNaN(parsed)) {
      date = new Date(parsed);
    } else {
      return val;
    }
  }

  if (!date || isNaN(date.getTime())) {
    return String(val || "--");
  }

  const tz = getEffectiveTimezone();
  try {
    const defaultOptions = {
      timeZone: tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    };
    return new Intl.DateTimeFormat(undefined, { ...defaultOptions, ...options }).format(date);
  } catch {
    return date.toLocaleString();
  }
}

function formatTimeOnly(val) {
  return formatDateTime(val, {
    year: undefined,
    month: undefined,
    day: undefined,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

let lastHeaderUpdatedTimestamp = 0;

/**
 * Throttle header last-updated timestamp updates to at least 5s intervals during live streams,
 * while allowing forced instant updates on full snapshots and manual refreshes.
 */
function updateHeaderLastUpdated(forced = false) {
  const now = Date.now();
  if (forced || now - lastHeaderUpdatedTimestamp >= 5000) {
    const lastUpdatedEl = document.getElementById("last-updated");
    if (lastUpdatedEl) {
      const timeStr = typeof formatTimeOnly === "function" ? formatTimeOnly(new Date()) : new Date().toLocaleTimeString();
      setTextIfChanged(lastUpdatedEl, `Updated ${timeStr}`);
    }
    lastHeaderUpdatedTimestamp = now;
  }
}

/**
 * Format unix timestamp seconds into local date-time string.
 */
function formatTimestampLocal(unixSeconds) {
  return formatDateTime(unixSeconds);
}

/**
 * Format timestamp into human-readable relative time (e.g. "just now", "5 mins ago", "2 hrs ago", "yesterday").
 */
function formatRelativeTime(val) {
  if (!val) return "--";
  let date = null;
  if (val instanceof Date) {
    date = val;
  } else if (typeof val === "number") {
    date = new Date(val > 1e11 ? val : val * 1000);
  } else if (typeof val === "string") {
    const parsed = Date.parse(val);
    if (!isNaN(parsed)) {
      date = new Date(parsed);
    }
  }

  if (!date || isNaN(date.getTime())) {
    return String(val);
  }

  const diffSec = Math.floor((Date.now() - date.getTime()) / 1000);
  if (diffSec < 0) return "just now";
  if (diffSec < 45) return "just now";
  if (diffSec < 90) return "1 min ago";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} mins ago`;
  if (diffSec < 7200) return "1 hr ago";
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} hrs ago`;
  if (diffSec < 172800) return "yesterday";
  const days = Math.floor(diffSec / 86400);
  if (days < 30) return `${days} days ago`;
  return formatDateTime(date, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/**
 * Normalize RAID status string to one of the supported states.
 */
function normalizeRaidState(rawState) {
  const cleaned = String(rawState || "unknown").trim().toLowerCase();
  return VALID_RAID_STATES.has(cleaned) ? cleaned : "unknown";
}

/**
 * Fast, safe DOM reconciliation helpers to prevent visual flicker and jumping elements.
 */
function setTextIfChanged(el, text) {
  if (el) {
    const str = text === null || text === undefined ? "" : String(text);
    if (el.textContent !== str) {
      el.textContent = str;
    }
  }
}

function setWidthIfChanged(el, widthPercent) {
  if (el) {
    const widthStr = `${widthPercent}%`;
    if (el.style.width !== widthStr) {
      el.style.width = widthStr;
    }
  }
}

function setBadgeIfChanged(badgeEl, textEl, stateClass, text) {
  if (!badgeEl) return;
  if (stateClass && badgeEl.className !== stateClass) {
    badgeEl.className = stateClass;
  }
  const target = textEl || badgeEl;
  setTextIfChanged(target, text);
}

/**
 * Apply status badge classes and text for a given RAID state.
 */
function applyStatusBadge(badgeEl, textEl, rawState) {
  if (!badgeEl) return;
  const state = normalizeRaidState(rawState);
  const targetClass = `status-badge status-${state}`;
  if (badgeEl.className !== targetClass) {
    badgeEl.className = targetClass;
  }
  if (badgeEl.getAttribute("data-state") !== state) {
    badgeEl.setAttribute("data-state", state);
  }

  const label = state === "none" ? "NO RAID" : state.toUpperCase();
  const targetEl = textEl || badgeEl;
  setTextIfChanged(targetEl, label);
}

/**
 * Update a progress bar width and threshold color class.
 */
function updateProgressBar(barEl, percent) {
  if (!barEl) return;
  const clamped = Math.max(0, Math.min(100, Number(percent) || 0));
  setWidthIfChanged(barEl, clamped.toFixed(1));
  const isDanger = clamped >= 90;
  const isWarn = !isDanger && clamped >= 75;
  if (barEl.classList.contains("danger") !== isDanger) {
    barEl.classList.toggle("danger", isDanger);
  }
  if (barEl.classList.contains("warn") !== isWarn) {
    barEl.classList.toggle("warn", isWarn);
  }
}

/**
 * Render rolling historical SVG trend line & area chart for memory usage.
 * Debounced using requestAnimationFrame to prevent layout reflows on high-frequency streaming.
 */
let memoryChartRafPending = false;
let latestMemoryHistorySnapshot = null;

function _renderMemoryHistoryChartInternal(history) {
  const chartEl = document.getElementById("memory-history-chart");
  if (!chartEl) return;
  if (!Array.isArray(history) || history.length === 0) return;

  const width = 240;
  const height = 44;
  const padTop = 3;
  const padBottom = 3;
  const usableHeight = height - padTop - padBottom;
  const n = history.length;

  const points = history.map((val, idx) => {
    const x = n > 1 ? (idx / (n - 1)) * width : width;
    const clamped = Math.max(0, Math.min(100, Number(val) || 0));
    const y = height - padBottom - (clamped / 100) * usableHeight;
    return { x: Number(x.toFixed(1)), y: Number(y.toFixed(1)) };
  });

  const pathLine = points.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x} ${p.y}`).join(" ");
  const firstP = points[0];
  const lastP = points[points.length - 1];
  const pathArea = `${pathLine} L ${lastP.x} ${height} L ${firstP.x} ${height} Z`;

  const areaEl = document.getElementById("memory-chart-area");
  const lineEl = document.getElementById("memory-chart-line");
  if (areaEl && areaEl.getAttribute("d") !== pathArea) {
    areaEl.setAttribute("d", pathArea);
  }
  if (lineEl && lineEl.getAttribute("d") !== pathLine) {
    lineEl.setAttribute("d", pathLine);
  }
}

function renderMemoryHistoryChart(history, direct = false) {
  if (!Array.isArray(history) || history.length === 0) return;
  latestMemoryHistorySnapshot = history;

  if (direct || typeof window === "undefined" || typeof window.requestAnimationFrame !== "function") {
    _renderMemoryHistoryChartInternal(history);
    return;
  }

  if (memoryChartRafPending) return;
  memoryChartRafPending = true;

  window.requestAnimationFrame(() => {
    memoryChartRafPending = false;
    if (latestMemoryHistorySnapshot) {
      _renderMemoryHistoryChartInternal(latestMemoryHistorySnapshot);
    }
  });
}

/**
 * Render Windows Task Manager style rolling time-series graph on the unified CPU cores canvas.
 * Handles window.devicePixelRatio for crisp HiDPI / Retina rendering.
 * Renders multiple core series simultaneously with vibrant colors and interactive hover focus.
 */
function drawCpuCoreGraph(canvas, buffers, coresData, activeHoverIndex = null, direct = false) {
  if (!canvas || typeof canvas.getContext !== "function") return;

  const render = () => {
    const dpr = (typeof window !== "undefined" && window.devicePixelRatio) || 1;
    let w = canvas._cachedWidth;
    let h = canvas._cachedHeight;
    if (!w || !h) {
      const rect = typeof canvas.getBoundingClientRect === "function" ? canvas.getBoundingClientRect() : null;
      w = (rect && Math.floor(rect.width)) || canvas.clientWidth || 600;
      h = (rect && Math.floor(rect.height)) || canvas.clientHeight || 150;
      canvas._cachedWidth = w;
      canvas._cachedHeight = h;
    }

    const targetW = Math.round(w * dpr);
    const targetH = Math.round(h * dpr);
    if (canvas.width !== targetW || canvas.height !== targetH) {
      canvas.width = targetW;
      canvas.height = targetH;
    }

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    ctx.save();
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    // 1. Task-manager style dark background
    ctx.fillStyle = "rgba(4, 9, 20, 0.8)";
    ctx.fillRect(0, 0, w, h);

    // 2. Padding and usable plot geometry
    const padLeft = 34;
    const padRight = 12;
    const padTop = 14;
    const padBottom = 18;
    const plotW = Math.max(10, w - padLeft - padRight);
    const plotH = Math.max(10, h - padTop - padBottom);

    // 3. Subtle horizontal grid lines & percentage labels (100%, 75%, 50%, 25%, 0%)
    const gridPcts = [100, 75, 50, 25, 0];
    ctx.lineWidth = 1;
    gridPcts.forEach((pct) => {
      const y = Math.round(padTop + plotH * (1 - pct / 100)) + 0.5;
      ctx.strokeStyle = pct === 0 ? "rgba(148, 163, 184, 0.35)" : "rgba(71, 85, 105, 0.24)";
      ctx.beginPath();
      ctx.moveTo(padLeft, y);
      ctx.lineTo(w - padRight, y);
      ctx.stroke();

      ctx.fillStyle = "rgba(148, 163, 184, 0.72)";
      ctx.font = "9px 'JetBrains Mono', ui-monospace, SFMono-Regular, monospace";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(`${pct}%`, padLeft - 5, y);
    });

    // Vertical time ticks
    const numVDivs = 6;
    ctx.strokeStyle = "rgba(71, 85, 105, 0.16)";
    ctx.beginPath();
    for (let d = 1; d < numVDivs; d++) {
      const x = Math.round(padLeft + (plotW / numVDivs) * d) + 0.5;
      ctx.moveTo(x, padTop);
      ctx.lineTo(x, padTop + plotH);
    }
    ctx.stroke();

    // Handle normalization of buffers (support single buffer or array of buffers)
    if (!buffers || buffers.length === 0) {
      ctx.restore();
      return;
    }
    const isSingleBuffer = Array.isArray(buffers) && typeof buffers[0] === "number";
    const allBuffers = isSingleBuffer ? [buffers] : buffers;

    const maxSamples = CPU_CORE_HISTORY_LIMIT;
    const stepX = plotW / Math.max(1, maxSamples - 1);

    // Prepare points for each core using smooth interpolation without jerky scrollOffset reset
    const coreSeries = [];
    for (let idx = 0; idx < allBuffers.length; idx++) {
      const buf = allBuffers[idx];
      if (!buf || buf.length === 0) continue;
      const n = buf.length;
      const pts = [];
      for (let i = 0; i < n; i++) {
        const sample = Math.max(0, Math.min(100, Number(buf[i]) || 0));
        const x = (w - padRight) - (n - 1 - i) * stepX;
        const y = padTop + plotH * (1 - sample / 100);
        pts.push({ x, y });
      }

      // Smooth trailing live point gliding cleanly to current interpolated telemetry
      const liveVal = (typeof liveAnimState !== "undefined" && liveAnimState.currentCores[idx] !== undefined)
        ? Number(liveAnimState.currentCores[idx])
        : ((Array.isArray(coresData) && coresData[idx] !== undefined)
          ? Number(coresData[idx])
          : (buf.length > 0 ? buf[buf.length - 1] : 0));
      const clampedLive = Math.max(0, Math.min(100, liveVal));
      const liveY = padTop + plotH * (1 - clampedLive / 100);

      if (pts.length > 0) {
        pts[pts.length - 1].y = liveY;
      } else {
        pts.push({ x: w - padRight, y: liveY });
      }

      if (pts.length === 1) {
        pts.unshift({ x: padLeft, y: pts[0].y });
      } else if (pts[0].x > padLeft) {
        pts.unshift({ x: padLeft, y: pts[0].y });
      }

      const color = CPU_CORE_COLORS[idx % CPU_CORE_COLORS.length];
      coreSeries.push({ idx, color, pts, livePt: { x: w - padRight, y: liveY } });
    }

    const hasHover =
      activeHoverIndex !== null &&
      activeHoverIndex !== undefined &&
      activeHoverIndex >= 0;

    // Helper: draw smooth cubic interpolation through series points
    const drawSmoothPath = (pts, startWithMove = true) => {
      if (!pts || pts.length === 0) return;
      if (startWithMove) {
        ctx.moveTo(pts[0].x, pts[0].y);
      } else {
        ctx.lineTo(pts[0].x, pts[0].y);
      }
      if (pts.length === 1) return;
      if (pts.length === 2 || typeof ctx.bezierCurveTo !== "function") {
        for (let i = 1; i < pts.length; i++) {
          ctx.lineTo(pts[i].x, pts[i].y);
        }
        return;
      }
      for (let i = 0; i < pts.length - 1; i++) {
        const p0 = pts[Math.max(0, i - 1)];
        const p1 = pts[i];
        const p2 = pts[i + 1];
        const p3 = pts[Math.min(pts.length - 1, i + 2)];

        const cp1x = p1.x + (p2.x - p0.x) / 6;
        const cp1y = p1.y + (p2.y - p0.y) / 6;
        const cp2x = p2.x - (p3.x - p1.x) / 6;
        const cp2y = p2.y - (p3.y - p1.y) / 6;

        ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, p2.x, p2.y);
      }
    };

    // Render function for a core series
    const renderSeriesLine = (series, isEmphasized, isDimmed) => {
      const { color, pts } = series;
      if (!pts || pts.length === 0) return;

      if (isEmphasized) {
        // Glow and thicker line
        ctx.save();
        ctx.strokeStyle = color;
        ctx.lineWidth = 2.4;
        ctx.shadowColor = color;
        ctx.shadowBlur = 8;
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.globalAlpha = 1.0;
        ctx.beginPath();
        drawSmoothPath(pts, true);
        ctx.stroke();
        ctx.restore();

        // Subtle gradient underlay for emphasized line
        ctx.save();
        const areaGrad = ctx.createLinearGradient(0, padTop, 0, padTop + plotH);
        areaGrad.addColorStop(0, `${color}33`);
        areaGrad.addColorStop(1, `${color}00`);
        ctx.beginPath();
        ctx.moveTo(pts[0].x, padTop + plotH);
        drawSmoothPath(pts, false);
        ctx.lineTo(pts[pts.length - 1].x, padTop + plotH);
        ctx.closePath();
        ctx.fillStyle = areaGrad;
        ctx.fill();
        ctx.restore();

        // Rightmost current utilization pulse dot
        const last = series.livePt || pts[pts.length - 1];
        ctx.save();
        ctx.fillStyle = color;
        ctx.shadowColor = color;
        ctx.shadowBlur = 8;
        ctx.beginPath();
        ctx.arc(last.x, last.y, 3.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      } else if (isDimmed) {
        // Dimmed line
        ctx.save();
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.0;
        ctx.globalAlpha = 0.2;
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.beginPath();
        drawSmoothPath(pts, true);
        ctx.stroke();

        const last = series.livePt || pts[pts.length - 1];
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(last.x, last.y, 1.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      } else {
        // Subtle area fill under standard series line
        ctx.save();
        const stdGrad = ctx.createLinearGradient(0, padTop, 0, padTop + plotH);
        stdGrad.addColorStop(0, `${color}18`);
        stdGrad.addColorStop(1, `${color}04`);
        ctx.beginPath();
        ctx.moveTo(pts[0].x, padTop + plotH);
        drawSmoothPath(pts, false);
        ctx.lineTo(pts[pts.length - 1].x, padTop + plotH);
        ctx.closePath();
        ctx.fillStyle = stdGrad;
        ctx.fill();
        ctx.restore();

        // Clean standard line
        ctx.save();
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.8;
        ctx.shadowColor = color;
        ctx.shadowBlur = 3;
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.globalAlpha = 0.92;
        ctx.beginPath();
        drawSmoothPath(pts, true);
        ctx.stroke();
        ctx.restore();

        // Rightmost edge dot
        const last = series.livePt || pts[pts.length - 1];
        ctx.save();
        ctx.fillStyle = color;
        ctx.shadowColor = color;
        ctx.shadowBlur = 4;
        ctx.beginPath();
        ctx.arc(last.x, last.y, 2.2, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      }
    };

    // Clip lines strictly to the plot area so scrolling wave leaves the left cleanly
    const canClip = typeof ctx.rect === "function" && typeof ctx.clip === "function";
    if (canClip) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(padLeft, padTop - 2, plotW, plotH + 4);
      ctx.clip();
    }

    if (hasHover) {
      // Draw non-hovered (dimmed) lines first
      coreSeries
        .filter((s) => s.idx !== activeHoverIndex)
        .forEach((s) => renderSeriesLine(s, false, true));

      // Draw active hovered (emphasized) line on top
      const hoveredSeries = coreSeries.find((s) => s.idx === activeHoverIndex);
      if (hoveredSeries) {
        renderSeriesLine(hoveredSeries, true, false);
      }
    } else {
      // Draw all lines cleanly
      coreSeries.forEach((s) => renderSeriesLine(s, false, false));
    }

    if (canClip) {
      ctx.restore(); // unclip
    }
    ctx.restore(); // outer restore
  };

  if (direct || (typeof liveAnimState !== "undefined" && liveAnimState.isRunning)) {
    render();
  } else if (typeof window !== "undefined" && typeof window.requestAnimationFrame === "function") {
    if (canvas._cpuRafId) {
      window.cancelAnimationFrame(canvas._cpuRafId);
    }
    canvas._cpuRafId = window.requestAnimationFrame(() => {
      canvas._cpuRafId = null;
      render();
    });
  } else {
    render();
  }
}

let cpuCanvasResizeDebounceTimer = null;
if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
  window.addEventListener("resize", () => {
    if (cpuCanvasResizeDebounceTimer) clearTimeout(cpuCanvasResizeDebounceTimer);
    cpuCanvasResizeDebounceTimer = setTimeout(() => {
      const unifiedCanvas =
        document.getElementById("cpu-cores-unified-canvas") ||
        (document.querySelector && document.querySelector(".cpu-core-canvas"));
      if (unifiedCanvas) {
        unifiedCanvas._cachedWidth = null;
        unifiedCanvas._cachedHeight = null;
        if (typeof cpuCoresHistoryBuffers !== "undefined" && cpuCoresHistoryBuffers.length > 0) {
          const currentValues = cpuCoresHistoryBuffers.map((b) => (b.length > 0 ? b[b.length - 1] : 0));
          drawCpuCoreGraph(unifiedCanvas, cpuCoresHistoryBuffers, currentValues, typeof activeHoveredCoreIndex !== "undefined" ? activeHoveredCoreIndex : null);
        }
      }
    }, 150);
  });
}

/**
 * Render Storage Volumes & Filesystems table on #view-raid.
 * Populates from system.disks or falls back to /api/system/storage.
 */
async function renderStorageMounts(disks) {
  const tbody = document.getElementById("storage-mounts-tbody");
  if (!tbody) return;

  if (Array.isArray(disks) && disks.length > 0) {
    cachedStorageDisks = disks;
  } else if (!cachedStorageDisks) {
    try {
      const resp = await apiFetch("/api/system/storage");
      if (resp.ok) {
        const data = await resp.json();
        if (data && Array.isArray(data.disks) && data.disks.length > 0) {
          cachedStorageDisks = data.disks;
          disks = data.disks;
        }
      }
    } catch (err) {
      console.debug("Failed to fetch storage mounts fallback:", err);
    }
  } else {
    disks = cachedStorageDisks;
  }

  const countBadge = document.getElementById("storage-mounts-count-badge");
  if (!disks || !Array.isArray(disks) || disks.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No storage volumes detected.</td></tr>`;
    if (countBadge) countBadge.textContent = "0 volumes";
    return;
  }

  if (countBadge) {
    countBadge.textContent = `${disks.length} ${disks.length === 1 ? "volume" : "volumes"}`;
  }

  tbody.innerHTML = disks
    .map((d) => {
      const percent = Math.max(0, Math.min(100, Number(d.percent) || 0));
      let barClass = "progress-fill";
      if (percent >= 90) barClass += " danger";
      else if (percent >= 75) barClass += " warn";

      const usedFormatted = formatBytes(d.used);
      const totalFormatted = formatBytes(d.total);
      const freeFormatted = formatBytes(d.free);

      return `
      <tr>
        <td><strong class="mono-text">${escapeHtml(d.device || "-")}</strong></td>
        <td><span class="mono-text mount-point">${escapeHtml(d.mountpoint || "-")}</span></td>
        <td><span class="kind-badge kind-network">${escapeHtml(d.fstype || "-")}</span></td>
        <td>
          <div class="storage-usage-cell">
            <div class="progress-track">
              <div class="${barClass}" style="width: ${percent.toFixed(1)}%"></div>
            </div>
            <span class="mono-text storage-usage-val">${percent.toFixed(1)}%</span>
          </div>
        </td>
        <td class="mono-text storage-capacity-cell">${usedFormatted} / ${totalFormatted} <span class="text-muted">(${freeFormatted} free)</span></td>
        <td><span class="mono-text opts-text" title="${escapeHtml(d.opts || "")}">${escapeHtml(d.opts || "-")}</span></td>
      </tr>
    `;
    })
    .join("");
}

/**
 * Render System Metrics section from /api/status system payload.
 */
function renderSystemMetrics(system) {
  if (!system) return;

  // CPU
  const cpuPercent = Number(system.cpu_percent) || 0;
  liveAnimState.targetCpu = cpuPercent;
  if (!liveAnimState.initialized) {
    liveAnimState.currentCpu = cpuPercent;
    liveAnimState.renderedCpu = cpuPercent;
  }
  const cpuValEl = document.getElementById("cpu-percent-value");
  const cpuBarEl = document.getElementById("cpu-progress-bar");
  if (!liveAnimState.isRunning) {
    if (cpuValEl) setTextIfChanged(cpuValEl, `${cpuPercent.toFixed(1)}%`);
    updateProgressBar(cpuBarEl, cpuPercent);
  }

  // CPU Cores Badge and Load Average
  const cpuCoresBadge = document.getElementById("cpu-cores-badge");
  if (cpuCoresBadge) {
    const logicalCores =
      system.cpu_count_logical ||
      (Array.isArray(system.cpu_cores) ? system.cpu_cores.length : 1);
    setTextIfChanged(cpuCoresBadge, `${logicalCores} ${logicalCores === 1 ? "Core" : "Cores"}`);
  }

  // CPU Package Temperature Badge
  if (system.thermal || system.thermals) {
    updateCpuTempBadge(system.thermal || system.thermals);
  } else {
    fetchHardwareThermals();
  }

  const cpuLoadEl = document.getElementById("cpu-load-detail");
  if (cpuLoadEl) {
    if (Array.isArray(system.load_avg) && system.load_avg.length > 0) {
      setTextIfChanged(cpuLoadEl, `Load avg: ${system.load_avg.map((v) => Number(v).toFixed(2)).join(", ")}`);
    } else {
      setTextIfChanged(cpuLoadEl, "Load avg: --");
    }
  }

  // CPU per-core drilldown (Unified Multi-Line Chart + Interactive Core Chips)
  const coresGrid = document.getElementById("cpu-cores-grid");
  const unifiedCanvas =
    document.getElementById("cpu-cores-unified-canvas") ||
    document.querySelector(".cpu-core-canvas");
  if (coresGrid) {
    const cores =
      Array.isArray(system.cpu_cores) && system.cpu_cores.length > 0
        ? system.cpu_cores
        : [cpuPercent];

    liveAnimState.targetCores = cores.map((c) => Math.max(0, Math.min(100, Number(c) || 0)));
    if (!liveAnimState.initialized || liveAnimState.currentCores.length !== cores.length) {
      liveAnimState.currentCores = [...liveAnimState.targetCores];
      liveAnimState.renderedCores = [...liveAnimState.targetCores];
    }

    const nowTs = typeof performance !== "undefined" && typeof performance.now === "function" ? performance.now() : Date.now();
    if (liveAnimState.lastSampleTime > 0) {
      const dt = nowTs - liveAnimState.lastSampleTime;
      if (dt > 30 && dt < 5000) {
        liveAnimState.estimatedSampleInterval = liveAnimState.estimatedSampleInterval * 0.7 + dt * 0.3;
      }
    }
    liveAnimState.lastSampleTime = nowTs;

    while (cpuCoresHistoryBuffers.length < cores.length) {
      cpuCoresHistoryBuffers.push([]);
    }
    if (cpuCoresHistoryBuffers.length > cores.length) {
      cpuCoresHistoryBuffers.length = cores.length;
    }

    let items = coresGrid.querySelectorAll(".cpu-core-item");
    if (items.length !== cores.length) {
      coresGrid.innerHTML = cores
        .map((_, idx) => {
          const color = CPU_CORE_COLORS[idx % CPU_CORE_COLORS.length];
          return `
        <div class="cpu-core-item" data-core-idx="${idx}" title="Core ${idx}">
          <div class="cpu-core-header">
            <div class="cpu-core-legend-info">
              <span class="cpu-core-color-dot" style="background-color: ${color};"></span>
              <span class="cpu-core-label">Core ${idx}</span>
            </div>
            <span class="cpu-core-percent mono-text">0.0%</span>
          </div>
          <div class="progress-track cpu-core-track">
            <div class="progress-fill" style="width: 0%; background-color: ${color};"></div>
          </div>
        </div>
      `;
        })
        .join("");
      items = coresGrid.querySelectorAll(".cpu-core-item");

      items.forEach((item) => {
        const idx = parseInt(item.getAttribute("data-core-idx"), 10);
        item.addEventListener("mouseenter", () => {
          activeHoveredCoreIndex = idx;
          const allChips = coresGrid.querySelectorAll(".cpu-core-item");
          allChips.forEach((chip, cIdx) => {
            chip.classList.toggle("is-hovered", cIdx === idx);
            chip.classList.toggle("is-dimmed", cIdx !== idx);
          });
          const currentValues = cpuCoresHistoryBuffers.map((b) => (b.length > 0 ? b[b.length - 1] : 0));
          if (unifiedCanvas) {
            drawCpuCoreGraph(unifiedCanvas, cpuCoresHistoryBuffers, currentValues, activeHoveredCoreIndex);
          }
        });
        item.addEventListener("mouseleave", () => {
          activeHoveredCoreIndex = null;
          const allChips = coresGrid.querySelectorAll(".cpu-core-item");
          allChips.forEach((chip) => {
            chip.classList.remove("is-hovered", "is-dimmed");
          });
          const currentValues = cpuCoresHistoryBuffers.map((b) => (b.length > 0 ? b[b.length - 1] : 0));
          if (unifiedCanvas) {
            drawCpuCoreGraph(unifiedCanvas, cpuCoresHistoryBuffers, currentValues, null);
          }
        });
      });
    }

    const currentCoreValues = [];
    cores.forEach((coreVal, idx) => {
      const cPercent = Math.max(0, Math.min(100, Number(coreVal) || 0));
      currentCoreValues.push(cPercent);
      const buf = cpuCoresHistoryBuffers[idx];
      buf.push(cPercent);
      if (buf.length > CPU_CORE_HISTORY_LIMIT) {
        buf.shift();
      }

      if (!liveAnimState.isRunning) {
        const item = items[idx];
        if (!item) return;

        const labelEl = item.querySelector(".cpu-core-label");
        if (labelEl) setTextIfChanged(labelEl, `Core ${idx}`);

        const percentEl = item.querySelector(".cpu-core-percent");
        if (percentEl) setTextIfChanged(percentEl, `${cPercent.toFixed(1)}%`);

        const barEl = item.querySelector(".cpu-core-track .progress-fill");
        if (barEl) {
          const color = CPU_CORE_COLORS[idx % CPU_CORE_COLORS.length];
          setWidthIfChanged(barEl, cPercent.toFixed(1));
          if (barEl.style.backgroundColor !== color) {
            barEl.style.backgroundColor = color;
          }
        }
      }
    });

    if (unifiedCanvas && !liveAnimState.isRunning) {
      drawCpuCoreGraph(unifiedCanvas, cpuCoresHistoryBuffers, currentCoreValues, activeHoveredCoreIndex);
    }
  }

  // Memory
  const mem = system.memory || {};
  const memPercent = Number(mem.percent) || 0;
  liveAnimState.targetRam = memPercent;
  if (!liveAnimState.initialized) {
    liveAnimState.currentRam = memPercent;
    liveAnimState.renderedRam = memPercent;
  }
  const memValEl = document.getElementById("memory-percent-value");
  const memBarEl = document.getElementById("memory-progress-bar");
  const memDetailEl = document.getElementById("memory-detail");
  if (!liveAnimState.isRunning) {
    if (memValEl) setTextIfChanged(memValEl, `${memPercent.toFixed(1)}%`);
    updateProgressBar(memBarEl, memPercent);
  }
  if (memDetailEl) {
    setTextIfChanged(memDetailEl, `${formatBytes(mem.used)} / ${formatBytes(mem.total)} used (${formatBytes(mem.available)} avail)`);
  }

  // Memory breakdown chips
  const memTotalBadge = document.getElementById("memory-total-badge");
  if (memTotalBadge && mem.total) {
    setTextIfChanged(memTotalBadge, `${formatBytes(mem.total)} Total`);
  }
  const chipUsed = document.getElementById("memory-chip-used");
  const chipAvailable = document.getElementById("memory-chip-available");
  const chipCached = document.getElementById("memory-chip-cached");
  const chipFree = document.getElementById("memory-chip-free");
  if (chipUsed) setTextIfChanged(chipUsed, `In use: ${formatBytes(mem.used)}`);
  if (chipAvailable) setTextIfChanged(chipAvailable, `Available: ${formatBytes(mem.available || 0)}`);
  if (chipCached) setTextIfChanged(chipCached, `Cached: ${formatBytes(mem.cached || 0)}`);
  if (chipFree) setTextIfChanged(chipFree, `Free: ${formatBytes(mem.free || 0)}`);

  // Memory rolling historical trend chart
  memoryHistoryBuffer.push(memPercent);
  if (memoryHistoryBuffer.length > MEMORY_HISTORY_LIMIT) {
    memoryHistoryBuffer.shift();
  }
  renderMemoryHistoryChart(memoryHistoryBuffer);

  // Disk / Direct Storage (POSIX statvfs / psutil)
  const disk = system.disk || system.storage || {};
  const diskPercent = Number(disk.percent ?? disk.percentage) || 0;
  liveAnimState.targetDisk = diskPercent;
  if (!liveAnimState.initialized) {
    liveAnimState.currentDisk = diskPercent;
    liveAnimState.renderedDisk = diskPercent;
    liveAnimState.initialized = true;
  }
  const diskValEl = document.getElementById("disk-percent-value");
  const diskBarEl = document.getElementById("disk-progress-bar");
  const diskDetailEl = document.getElementById("disk-detail");
  if (!liveAnimState.isRunning) {
    if (diskValEl) setTextIfChanged(diskValEl, `${diskPercent.toFixed(1)}%`);
    updateProgressBar(diskBarEl, diskPercent);
  }
  if (diskDetailEl) {
    const usedFormatted = formatBytes(disk.used ?? disk.used_bytes);
    const totalFormatted = formatBytes(disk.total ?? disk.total_bytes);
    const freeFormatted = formatBytes(disk.free ?? disk.free_bytes ?? disk.available);
    const mountLabel = disk.mount || disk.mount_path || disk.path;
    const prefix = mountLabel ? `[${mountLabel}] ` : "";
    setTextIfChanged(diskDetailEl, `${prefix}${usedFormatted} / ${totalFormatted} (${freeFormatted} free)`);
  }

  // Uptime & Host Platform Telemetry
  if (system.boot_time) {
    currentBootTime = system.boot_time;
  }
  const uptimeValEl = document.getElementById("uptime-value");
  const bootDetailEl = document.getElementById("boot-time-detail");
  if (uptimeValEl) {
    setTextIfChanged(uptimeValEl, formatUptimeFromBoot(system.boot_time));
  }
  if (bootDetailEl) {
    setTextIfChanged(bootDetailEl, `Booted: ${formatTimestampLocal(system.boot_time)}`);
  }
  const uptimeLoadBadge = document.getElementById("uptime-load-badge");
  if (uptimeLoadBadge) {
    if (Array.isArray(system.load_avg) && system.load_avg.length > 0) {
      setTextIfChanged(uptimeLoadBadge, `Load: ${system.load_avg.map((v) => Number(v).toFixed(2)).join(", ")}`);
      if (uptimeLoadBadge.style.display !== "") uptimeLoadBadge.style.display = "";
    } else {
      setTextIfChanged(uptimeLoadBadge, "Load: --");
    }
  }

  const hostOsEl = document.getElementById("host-os-detail");
  if (hostOsEl) {
    setTextIfChanged(hostOsEl, system.os_distro || system.platform || "Linux");
  }
  const hostKernelEl = document.getElementById("host-kernel-detail");
  if (hostKernelEl) {
    setTextIfChanged(hostKernelEl, system.kernel || "--");
  }

  // Network Telemetry
  const net = system.network || {};
  const ifaces = Array.isArray(net.interfaces) ? net.interfaces : [];
  const primaryIface =
    ifaces.find((i) => i.is_physical && i.is_up && i.ip) ||
    ifaces.find((i) => !i.is_virtual && i.is_up && i.ip) ||
    ifaces.find((i) => i.is_physical && i.is_up) ||
    ifaces.find((i) => !i.is_virtual && i.is_up) ||
    ifaces.find((i) => i.is_up && i.ip) ||
    ifaces[0] ||
    {};

  const ifaceNameEl = document.getElementById("host-network-interface");
  if (ifaceNameEl) {
    const rawName = primaryIface.interface || (ifaces.length > 0 ? ifaces[0].interface : "--");
    const typeLabel = primaryIface.adapter_type && primaryIface.adapter_type !== "virtual" && primaryIface.adapter_type !== "docker"
      ? ` (${primaryIface.adapter_type})`
      : "";
    setTextIfChanged(ifaceNameEl, `${rawName}${typeLabel}`);
  }

  const ifaceIpEl = document.getElementById("host-network-ip");
  if (ifaceIpEl) {
    setTextIfChanged(ifaceIpEl, primaryIface.ip || (primaryIface.interface ? "No IPv4" : "--"));
  }

  const rxBytes = Number(net.total_bytes_recv ?? primaryIface.bytes_recv) || 0;
  const txBytes = Number(net.total_bytes_sent ?? primaryIface.bytes_sent) || 0;
  const nowTs = Number(system.timestamp) || Date.now() / 1000;
  let rx_rate = Number(net.rx_rate ?? net.rx_rate_bps) || 0;
  let tx_rate = Number(net.tx_rate ?? net.tx_rate_bps) || 0;
  if (lastSystemNetSample && nowTs > lastSystemNetSample.timestamp) {
    const dt = nowTs - lastSystemNetSample.timestamp;
    if (dt > 0.05) {
      if (!rx_rate && rxBytes >= lastSystemNetSample.rxBytes) {
        rx_rate = (rxBytes - lastSystemNetSample.rxBytes) / dt;
      }
      if (!tx_rate && txBytes >= lastSystemNetSample.txBytes) {
        tx_rate = (txBytes - lastSystemNetSample.txBytes) / dt;
      }
    }
  }
  lastSystemNetSample = { timestamp: nowTs, rxBytes, txBytes };

  const netRxEl = document.getElementById("host-network-rx");
  if (netRxEl) {
    setTextIfChanged(
      netRxEl,
      rx_rate > 0 ? `${formatBytes(rxBytes)} (${formatBytes(rx_rate)}/s)` : formatBytes(rxBytes)
    );
  }

  const netTxEl = document.getElementById("host-network-tx");
  if (netTxEl) {
    setTextIfChanged(
      netTxEl,
      tx_rate > 0 ? `${formatBytes(txBytes)} (${formatBytes(tx_rate)}/s)` : formatBytes(txBytes)
    );
  }

  const netStatusEl = document.getElementById("host-network-status");
  if (netStatusEl) {
    const isOnline = ifaces.some((i) => i.is_up) || (Number(net.total_bytes_recv) > 0);
    setBadgeIfChanged(netStatusEl, null, `status-badge ${isOnline ? "status-ok" : "status-failed"}`, isOnline ? "CONNECTED" : "DISCONNECTED");
  }

  // Mounted Storage Volumes Mini-List on Overview Card 3 (if present and visible)
  const mountsListEl = document.getElementById("overview-mounts-list");
  if (mountsListEl && !mountsListEl.classList.contains("hidden")) {
    const disks = Array.isArray(system.disks) ? system.disks : (Array.isArray(system.filesystems) ? system.filesystems : []);
    if (disks.length > 0) {
      mountsListEl.innerHTML = disks.map((d) => {
        const pct = Math.max(0, Math.min(100, Number(d.percent) || 0));
        let barClass = "progress-fill";
        if (pct >= 90) barClass += " danger";
        else if (pct >= 75) barClass += " warn";
        return `
          <div class="overview-mount-item">
            <div class="mount-item-row">
              <span class="mount-point-name mono-text">${escapeHtml(d.mountpoint || "/")}</span>
              <span class="mount-fs-type">${escapeHtml(d.fstype || "ext4")}</span>
              <span class="mount-usage-subtext mono-text">${pct.toFixed(1)}% (${formatBytes(d.used)} / ${formatBytes(d.total)})</span>
            </div>
            <div class="progress-track" style="height: 4px;">
              <div class="${barClass}" style="width: ${pct.toFixed(1)}%"></div>
            </div>
          </div>
        `;
      }).join("");
    } else {
      mountsListEl.innerHTML = `<div class="empty-state-mini">No additional mounted volumes.</div>`;
    }
  }

  // Storage Volumes breakdown table on #view-raid
  if (Array.isArray(system.disks) && system.disks.length > 0) {
    cachedStorageDisks = system.disks;
    renderStorageMounts(system.disks);
  }

  // Services Ribbon Summary
  if (system.services_summary) {
    renderServicesSummary(system.services_summary);
  }

  // Packages & Reboot Summary
  if (system.reboot_status) {
    renderPackagesOverviewRibbon(system.reboot_status, cachedPackageUpdates);
  }
}

// ==========================================================================
// System Telemetry Time-Machine (1h / 6h / 24h / 7d)
// ==========================================================================

let currentTelemetryRange = "live"; // "live" | "1h" | "6h" | "24h" | "7d"
let cachedTelemetryHistory = null;
let isFetchingTelemetryHistory = false;
let activeTelemetryHoverIndex = null;

function formatNetworkRate(bps) {
  const num = Number(bps);
  if (!Number.isFinite(num) || num < 0) return "--";
  if (num === 0) return "0 bps";
  if (num >= 1e9) return `${(num / 1e9).toFixed(1)} Gbps`;
  if (num >= 1e6) return `${(num / 1e6).toFixed(1)} Mbps`;
  if (num >= 1e3) return `${(num / 1e3).toFixed(1)} Kbps`;
  return `${Math.round(num)} bps`;
}

function getRangeDescription(range) {
  switch (range) {
    case "1h":
      return "Historical Telemetry (Last 1 Hour)";
    case "6h":
      return "Historical Telemetry (Last 6 Hours)";
    case "24h":
      return "Historical Telemetry (Last 24 Hours)";
    case "7d":
      return "Historical Telemetry (Last 7 Days)";
    default:
      return "Historical Telemetry";
  }
}

/**
 * Switch telemetry display between Live mode and Historical ranges (1h, 6h, 24h, 7d).
 */
async function setTelemetryRange(range) {
  const validRanges = ["live", "1h", "6h", "24h", "7d"];
  const targetRange = validRanges.includes(range) ? range : "live";
  currentTelemetryRange = targetRange;

  const buttons = {
    live: document.getElementById("btn-telemetry-range-live"),
    "1h": document.getElementById("btn-telemetry-range-1h"),
    "6h": document.getElementById("btn-telemetry-range-6h"),
    "24h": document.getElementById("btn-telemetry-range-24h"),
    "7d": document.getElementById("btn-telemetry-range-7d"),
  };

  Object.entries(buttons).forEach(([key, btn]) => {
    if (!btn) return;
    const isActive = key === targetRange;
    btn.classList.toggle("active", isActive);
    btn.setAttribute("aria-pressed", isActive ? "true" : "false");
  });

  const badgeEl = document.getElementById("telemetry-timemachine-badge");
  const summaryEl = document.getElementById("telemetry-history-summary");
  const chartContainerEl = document.getElementById("telemetry-historical-chart-container");

  if (targetRange === "live") {
    if (badgeEl) {
      badgeEl.textContent = "LIVE STREAM";
      badgeEl.classList.remove("mode-historical");
    }
    summaryEl?.classList.add("hidden");
    chartContainerEl?.classList.add("hidden");
    activeTelemetryHoverIndex = null;
    return;
  }

  // Historical Range Selected (1h, 6h, 24h, 7d)
  if (badgeEl) {
    badgeEl.textContent = `HISTORICAL (${targetRange.toUpperCase()})`;
    badgeEl.classList.add("mode-historical");
  }
  summaryEl?.classList.remove("hidden");
  chartContainerEl?.classList.remove("hidden");

  await fetchTelemetryHistory(targetRange);
}

/**
 * Fetch historical telemetry from backend API /api/telemetry/history and update UI.
 */
async function fetchTelemetryHistory(range) {
  if (isFetchingTelemetryHistory) return;
  isFetchingTelemetryHistory = true;

  const hintEl = document.getElementById("telemetry-chart-status-hint");
  if (hintEl) hintEl.textContent = `Loading ${range} telemetry history...`;

  const titleEl = document.getElementById("telemetry-history-title");
  if (titleEl) {
    titleEl.textContent = getRangeDescription(range);
  }

  try {
    const resp = await apiFetch("/api/telemetry/history?range=" + encodeURIComponent(range) + "&points=80");
    if (!resp.ok) {
      throw new Error(`GET /api/telemetry/history failed with status ${resp.status}`);
    }
    const data = await resp.json();
    cachedTelemetryHistory = data;
    renderTelemetryHistorySummary(data);
    renderTelemetryHistoricalChart(data);
    if (hintEl) hintEl.textContent = "Hover or drag across chart to inspect point details";
  } catch (err) {
    console.error("Failed to fetch telemetry history:", err);
    if (hintEl) hintEl.textContent = "Failed to load telemetry history.";
    showToast({
      title: "Telemetry History Error",
      message: `Failed to load ${range} historical telemetry: ${err instanceof Error ? err.message : String(err)}`,
      type: "danger",
    });
  } finally {
    isFetchingTelemetryHistory = false;
  }
}

/**
 * Populate historical summary badges (CPU, Memory, Network, Temp).
 */
function renderTelemetryHistorySummary(data) {
  if (!data) return;

  const samplesBadge = document.getElementById("telemetry-history-samples-count");
  if (samplesBadge) {
    const total = data.total_samples ?? (Array.isArray(data.points) ? data.points.length : 0);
    samplesBadge.textContent = `${total} ${total === 1 ? "sample" : "samples"}`;
  }

  const summary = data.summary || {};

  // 1. CPU Load
  const cpuEl = document.getElementById("telemetry-stat-cpu");
  if (cpuEl) {
    const s = summary.cpu;
    if (s && s.avg !== null && s.avg !== undefined) {
      cpuEl.textContent = `Avg: ${Number(s.avg).toFixed(1)}% (Min: ${Number(s.min).toFixed(1)}% / Max: ${Number(s.max).toFixed(1)}%)`;
    } else {
      cpuEl.textContent = "Avg: --% (Min: --% / Max: --%)";
    }
  }

  // 2. Memory
  const memEl = document.getElementById("telemetry-stat-mem");
  if (memEl) {
    const s = summary.memory;
    if (s && s.avg !== null && s.avg !== undefined) {
      memEl.textContent = `Avg: ${Number(s.avg).toFixed(1)}% (Min: ${Number(s.min).toFixed(1)}% / Max: ${Number(s.max).toFixed(1)}%)`;
    } else {
      memEl.textContent = "Avg: --% (Min: --% / Max: --%)";
    }
  }

  // 3. Network Throughput
  const netEl = document.getElementById("telemetry-stat-net");
  if (netEl) {
    const rx = summary.net_rx_rate_bps || summary.net_rx;
    const tx = summary.net_tx_rate_bps || summary.net_tx;
    if (rx || tx) {
      const rxAvg = rx?.avg ?? 0;
      const rxMax = rx?.max ?? 0;
      const txAvg = tx?.avg ?? 0;
      const txMax = tx?.max ?? 0;
      netEl.textContent = `RX: ${formatNetworkRate(rxAvg)} (Max: ${formatNetworkRate(rxMax)}) / TX: ${formatNetworkRate(txAvg)} (Max: ${formatNetworkRate(txMax)})`;
    } else {
      netEl.textContent = "RX: -- / TX: --";
    }
  }

  // 4. CPU Temperature
  const tempEl = document.getElementById("telemetry-stat-temp");
  if (tempEl) {
    const s = summary.cpu_temp;
    if (s && s.avg !== null && s.avg !== undefined) {
      tempEl.textContent = `Avg: ${Number(s.avg).toFixed(1)}°C (Min: ${Number(s.min).toFixed(1)}°C / Max: ${Number(s.max).toFixed(1)}°C)`;
    } else {
      tempEl.textContent = "Avg: --°C (Min: --°C / Max: --°C)";
    }
  }
}

/**
 * Render multi-line time-series graph on #telemetry-historical-canvas.
 */
function renderTelemetryHistoricalChart(data) {
  const canvas = document.getElementById("telemetry-historical-canvas");
  const emptyEl = document.getElementById("telemetry-history-empty");
  if (!canvas) return;

  const points = Array.isArray(data?.points) ? data.points : [];
  if (points.length === 0) {
    emptyEl?.classList.remove("hidden");
    const ctx = canvas.getContext("2d");
    if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
    return;
  }

  emptyEl?.classList.add("hidden");

  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = Math.floor(rect.width) || canvas.clientWidth || 900;
  const h = Math.floor(rect.height) || canvas.clientHeight || 260;

  const targetW = Math.round(w * dpr);
  const targetH = Math.round(h * dpr);
  if (canvas.width !== targetW || canvas.height !== targetH) {
    canvas.width = targetW;
    canvas.height = targetH;
  }

  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  ctx.save();
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  // 1. Dark cockpit background
  ctx.fillStyle = "rgba(4, 9, 20, 0.85)";
  ctx.fillRect(0, 0, w, h);

  // 2. Plot margins
  const padLeft = 44;
  const padRight = 56;
  const padTop = 18;
  const padBottom = 28;
  const plotW = Math.max(10, w - padLeft - padRight);
  const plotH = Math.max(10, h - padTop - padBottom);

  // 3. Gridlines and Left Y-Axis (0 - 100%)
  const yTicks = [100, 75, 50, 25, 0];
  ctx.lineWidth = 1;
  yTicks.forEach((pct) => {
    const y = Math.round(padTop + plotH * (1 - pct / 100)) + 0.5;
    ctx.strokeStyle = pct === 0 ? "rgba(148, 163, 184, 0.35)" : "rgba(71, 85, 105, 0.22)";
    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(w - padRight, y);
    ctx.stroke();

    ctx.fillStyle = "rgba(148, 163, 184, 0.72)";
    ctx.font = "9px 'JetBrains Mono', ui-monospace, SFMono-Regular, monospace";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillText(`${pct}%`, padLeft - 5, y);
  });

  // Secondary scale for Net Throughput (Right Y-Axis)
  let maxNetBps = 1000;
  points.forEach((p) => {
    if (p.net_rx_rate_bps && p.net_rx_rate_bps > maxNetBps) maxNetBps = p.net_rx_rate_bps;
    if (p.net_tx_rate_bps && p.net_tx_rate_bps > maxNetBps) maxNetBps = p.net_tx_rate_bps;
  });
  maxNetBps = Math.ceil(maxNetBps * 1.15);

  const rightYTop = Math.round(padTop) + 0.5;
  const rightYMid = Math.round(padTop + plotH * 0.5) + 0.5;
  ctx.fillStyle = "rgba(16, 185, 129, 0.75)";
  ctx.font = "9px 'JetBrains Mono', ui-monospace, SFMono-Regular, monospace";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(formatNetworkRate(maxNetBps), w - padRight + 6, rightYTop);
  ctx.fillText(formatNetworkRate(maxNetBps / 2), w - padRight + 6, rightYMid);

  // 4. X-Axis Time Span and Vertical Ticks
  const startTime = data.start_time || points[0].timestamp;
  const endTime = data.end_time || points[points.length - 1].timestamp;
  const timeSpan = Math.max(1, endTime - startTime);

  const numVTicks = Math.min(6, Math.max(3, Math.floor(plotW / 140)));
  ctx.strokeStyle = "rgba(71, 85, 105, 0.16)";
  ctx.fillStyle = "rgba(148, 163, 184, 0.65)";
  ctx.font = "9px 'JetBrains Mono', ui-monospace, SFMono-Regular, monospace";
  ctx.textAlign = "center";
  ctx.textBaseline = "top";

  for (let i = 0; i <= numVTicks; i++) {
    const fraction = i / numVTicks;
    const x = Math.round(padLeft + plotW * fraction) + 0.5;
    ctx.beginPath();
    ctx.moveTo(x, padTop);
    ctx.lineTo(x, padTop + plotH);
    ctx.stroke();

    const tVal = startTime + fraction * timeSpan;
    const timeLabel = formatDateTime(tVal, {
      month: data.range === "7d" ? "numeric" : undefined,
      day: data.range === "7d" ? "numeric" : undefined,
      hour: "2-digit",
      minute: "2-digit",
      second: undefined,
    });
    ctx.fillText(timeLabel, x, padTop + plotH + 6);
  }

  // 5. Compute coordinates for each series
  const seriesCoords = points.map((p) => {
    const t = p.timestamp || startTime;
    const x = padLeft + ((t - startTime) / timeSpan) * plotW;
    p._x = x;

    const cpuP = Math.max(0, Math.min(100, Number(p.cpu_percent) || 0));
    const memP = Math.max(0, Math.min(100, Number(p.memory_percent) || 0));
    const rxP = Math.max(0, Math.min(maxNetBps, Number(p.net_rx_rate_bps) || 0));
    const txP = Math.max(0, Math.min(maxNetBps, Number(p.net_tx_rate_bps) || 0));

    const yCpu = padTop + plotH * (1 - cpuP / 100);
    const yMem = padTop + plotH * (1 - memP / 100);
    const yRx = padTop + plotH * (1 - rxP / maxNetBps);
    const yTx = padTop + plotH * (1 - txP / maxNetBps);

    return { x, yCpu, yMem, yRx, yTx, p };
  });

  // Helper to draw a line
  const drawLine = (prop, color, width, isDashed = false) => {
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    if (isDashed) {
      ctx.setLineDash([4, 3]);
    }
    ctx.beginPath();
    seriesCoords.forEach((pt, idx) => {
      if (idx === 0) ctx.moveTo(pt.x, pt[prop]);
      else ctx.lineTo(pt.x, pt[prop]);
    });
    ctx.stroke();
    ctx.restore();
  };

  // Subtle Area Gradient Under CPU Line
  if (seriesCoords.length > 0) {
    ctx.save();
    const areaGrad = ctx.createLinearGradient(0, padTop, 0, padTop + plotH);
    areaGrad.addColorStop(0, "rgba(0, 240, 255, 0.14)");
    areaGrad.addColorStop(1, "rgba(0, 240, 255, 0.0)");
    ctx.beginPath();
    ctx.moveTo(seriesCoords[0].x, padTop + plotH);
    seriesCoords.forEach((pt) => ctx.lineTo(pt.x, pt.yCpu));
    ctx.lineTo(seriesCoords[seriesCoords.length - 1].x, padTop + plotH);
    ctx.closePath();
    ctx.fillStyle = areaGrad;
    ctx.fill();
    ctx.restore();
  }

  // Draw Series in Order (Net RX, Net TX, Memory, CPU)
  drawLine("yRx", "#10b981", 1.6, true);
  drawLine("yTx", "#f59e0b", 1.6, true);
  drawLine("yMem", "#c084fc", 2.0, false);
  drawLine("yCpu", "#00f0ff", 2.2, false);

  // 6. Interactive Hover Crosshair and Tooltip
  if (
    activeTelemetryHoverIndex !== null &&
    activeTelemetryHoverIndex >= 0 &&
    activeTelemetryHoverIndex < seriesCoords.length
  ) {
    const hoverItem = seriesCoords[activeTelemetryHoverIndex];
    const hx = hoverItem.x;

    // Vertical crosshair
    ctx.save();
    ctx.strokeStyle = "rgba(255, 255, 255, 0.4)";
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 2]);
    ctx.beginPath();
    ctx.moveTo(hx, padTop);
    ctx.lineTo(hx, padTop + plotH);
    ctx.stroke();
    ctx.restore();

    // Data point circles
    const drawDot = (y, color) => {
      ctx.save();
      ctx.fillStyle = color;
      ctx.strokeStyle = "#0b0f19";
      ctx.lineWidth = 2;
      ctx.shadowColor = color;
      ctx.shadowBlur = 6;
      ctx.beginPath();
      ctx.arc(hx, y, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.restore();
    };

    drawDot(hoverItem.yRx, "#10b981");
    drawDot(hoverItem.yTx, "#f59e0b");
    drawDot(hoverItem.yMem, "#c084fc");
    drawDot(hoverItem.yCpu, "#00f0ff");
  }

  ctx.restore();
}

/**
 * Position and format hover tooltip over #telemetry-historical-canvas.
 */
function positionTelemetryTooltip(canvas, mouseX, mouseY, point) {
  const tooltip = document.getElementById("telemetry-historical-tooltip");
  if (!tooltip || !point) return;

  const isoTime = point.iso_time || point.timestamp;
  const timeFormatted = formatDateTime(isoTime);

  const cpu = point.cpu_percent !== null && point.cpu_percent !== undefined ? `${Number(point.cpu_percent).toFixed(1)}%` : "--";
  const mem = point.memory_percent !== null && point.memory_percent !== undefined ? `${Number(point.memory_percent).toFixed(1)}%` : "--";
  const rx = formatNetworkRate(point.net_rx_rate_bps);
  const tx = formatNetworkRate(point.net_tx_rate_bps);
  const tempStr = point.cpu_temp !== null && point.cpu_temp !== undefined ? `${Number(point.cpu_temp).toFixed(1)}°C` : null;

  tooltip.innerHTML = `
    <div class="tt-header">${escapeHtml(timeFormatted)}</div>
    <div class="tt-row">
      <div class="tt-label-group">
        <span class="tt-dot" style="background-color: #00f0ff;"></span>
        <span class="tt-label">CPU:</span>
      </div>
      <span class="tt-val">${cpu}</span>
    </div>
    <div class="tt-row">
      <div class="tt-label-group">
        <span class="tt-dot" style="background-color: #c084fc;"></span>
        <span class="tt-label">Memory:</span>
      </div>
      <span class="tt-val">${mem}</span>
    </div>
    <div class="tt-row">
      <div class="tt-label-group">
        <span class="tt-dot" style="background-color: #10b981;"></span>
        <span class="tt-label">Net Rx:</span>
      </div>
      <span class="tt-val">${rx}</span>
    </div>
    <div class="tt-row">
      <div class="tt-label-group">
        <span class="tt-dot" style="background-color: #f59e0b;"></span>
        <span class="tt-label">Net Tx:</span>
      </div>
      <span class="tt-val">${tx}</span>
    </div>
    ${tempStr ? `
    <div class="tt-row">
      <div class="tt-label-group">
        <span class="tt-dot" style="background-color: #f43f5e;"></span>
        <span class="tt-label">CPU Temp:</span>
      </div>
      <span class="tt-val">${tempStr}</span>
    </div>
    ` : ""}
  `;

  tooltip.classList.remove("hidden");

  // Constrain tooltip coordinates within canvas wrapper
  const wrapper = canvas.parentElement;
  const wrapW = wrapper?.clientWidth || 900;
  const wrapH = wrapper?.clientHeight || 260;

  const clampedX = Math.max(90, Math.min(wrapW - 90, mouseX));
  const clampedY = Math.max(10, Math.min(wrapH - 10, mouseY));

  tooltip.style.left = `${clampedX}px`;
  tooltip.style.top = `${clampedY}px`;
}

/**
 * Initialize event listeners for System Telemetry Time-Machine.
 */
function initTelemetryTimeMachine() {
  const rangeButtons = [
    { id: "btn-telemetry-range-live", range: "live" },
    { id: "btn-telemetry-range-1h", range: "1h" },
    { id: "btn-telemetry-range-6h", range: "6h" },
    { id: "btn-telemetry-range-24h", range: "24h" },
    { id: "btn-telemetry-range-7d", range: "7d" },
  ];

  rangeButtons.forEach(({ id, range }) => {
    const btn = document.getElementById(id);
    btn?.addEventListener("click", () => setTelemetryRange(range));
  });

  const canvas = document.getElementById("telemetry-historical-canvas");
  if (canvas) {
    const handleMove = (clientX, clientY) => {
      if (!cachedTelemetryHistory || !Array.isArray(cachedTelemetryHistory.points) || cachedTelemetryHistory.points.length === 0) {
        return;
      }
      const rect = canvas.getBoundingClientRect();
      const mouseX = clientX - rect.left;
      const mouseY = clientY - rect.top;

      let nearestIdx = 0;
      let minDist = Infinity;
      const points = cachedTelemetryHistory.points;
      for (let i = 0; i < points.length; i++) {
        const pt = points[i];
        if (pt._x !== undefined) {
          const dist = Math.abs(mouseX - pt._x);
          if (dist < minDist) {
            minDist = dist;
            nearestIdx = i;
          }
        }
      }

      activeTelemetryHoverIndex = nearestIdx;
      renderTelemetryHistoricalChart(cachedTelemetryHistory);
      positionTelemetryTooltip(canvas, mouseX, mouseY, points[nearestIdx]);
    };

    const handleLeave = () => {
      activeTelemetryHoverIndex = null;
      const tooltip = document.getElementById("telemetry-historical-tooltip");
      tooltip?.classList.add("hidden");
      if (cachedTelemetryHistory) {
        renderTelemetryHistoricalChart(cachedTelemetryHistory);
      }
    };

    canvas.addEventListener("mousemove", (e) => handleMove(e.clientX, e.clientY));
    canvas.addEventListener("mouseleave", handleLeave);

    canvas.addEventListener("touchstart", (e) => {
      if (e.touches && e.touches.length > 0) {
        handleMove(e.touches[0].clientX, e.touches[0].clientY);
      }
    }, { passive: true });

    canvas.addEventListener("touchmove", (e) => {
      if (e.touches && e.touches.length > 0) {
        handleMove(e.touches[0].clientX, e.touches[0].clientY);
      }
    }, { passive: true });

    canvas.addEventListener("touchend", handleLeave, { passive: true });
    canvas.addEventListener("touchcancel", handleLeave, { passive: true });
  }

  // Redraw chart on window resize if in historical mode
  window.addEventListener("resize", () => {
    if (currentTelemetryRange !== "live" && cachedTelemetryHistory) {
      renderTelemetryHistoricalChart(cachedTelemetryHistory);
    }
  });
}

// ==========================================================================
// Hardware Health Cockpit: S.M.A.R.T. Disks & Thermal Telemetry
// ==========================================================================

let smartDisksCache = {};
let cachedRaidData = null;

/**
 * Fetch physical disk SMART metrics and cache them.
 */
async function fetchSmartDisks() {
  try {
    const disks = await apiFetch("/api/hardware/disks");
    if (Array.isArray(disks)) {
      smartDisksCache = {};
      disks.forEach((d) => {
        if (d && d.device) {
          const raw = d.device;
          const clean = raw.replace("/dev/", "");
          smartDisksCache[raw] = d;
          smartDisksCache[clean] = d;
        }
      });
      if (cachedRaidData) {
        renderRaidDetails(cachedRaidData);
      }
      return disks;
    }
  } catch (err) {
    console.debug("Failed to fetch SMART disk metrics:", err);
  }
  return [];
}

/**
 * Fetch hardware thermal sensors telemetry and update overview CPU temperature badge.
 */
async function fetchHardwareThermals() {
  try {
    const data = await apiFetch("/api/hardware/thermals");
    updateCpuTempBadge(data);
    return data;
  } catch (err) {
    console.debug("Failed to fetch hardware thermals:", err);
  }
  return null;
}

/**
 * Update the CPU temperature badge with appropriate color classes based on temperature thresholds.
 */
function updateCpuTempBadge(thermalData) {
  const badge = document.getElementById("cpu-temp-badge");
  if (!badge) return;

  let tempVal = null;
  if (thermalData) {
    if (thermalData.cpu_package_c !== null && thermalData.cpu_package_c !== undefined) {
      tempVal = thermalData.cpu_package_c;
    } else if (Array.isArray(thermalData.cores) && thermalData.cores.length > 0 && thermalData.cores[0]?.temp_c !== undefined) {
      tempVal = thermalData.cores[0].temp_c;
    } else if (Array.isArray(thermalData.sensors) && thermalData.sensors.length > 0 && thermalData.sensors[0]?.temp_c !== undefined) {
      tempVal = thermalData.sensors[0].temp_c;
    }
  }

  if (tempVal !== null && tempVal !== undefined) {
    const num = typeof tempVal === "number" ? tempVal : parseFloat(tempVal);
    if (!isNaN(num)) {
      badge.textContent = `${num.toFixed(1)}°C`;
      badge.classList.remove("temp-ok", "temp-warn", "temp-crit");
      if (num > 60) {
        badge.classList.add("temp-crit");
      } else if (num > 50) {
        badge.classList.add("temp-warn");
      } else {
        badge.classList.add("temp-ok");
      }
      return;
    }
  }
  badge.textContent = "--°C";
  badge.classList.remove("temp-ok", "temp-warn", "temp-crit");
}

/**
 * Open S.M.A.R.T. details modal for a specific disk device.
 */
async function openSmartModal(diskName) {
  const modal = document.getElementById("smart-modal");
  if (!modal) return;

  const cleanName = (diskName || "").replace("/dev/", "").trim();
  const titleEl = document.getElementById("smart-modal-title");
  if (titleEl) {
    titleEl.textContent = cleanName ? `S.M.A.R.T. Diagnostics - /dev/${cleanName}` : "S.M.A.R.T. Diagnostics";
  }

  const statusBadge = document.getElementById("smart-modal-status");
  const typeBadge = document.getElementById("smart-modal-type");
  const modelEl = document.getElementById("smart-modal-model");
  const serialEl = document.getElementById("smart-modal-serial");
  const firmwareEl = document.getElementById("smart-modal-firmware");
  const tempEl = document.getElementById("smart-modal-temp");
  const hoursEl = document.getElementById("smart-modal-hours");
  const reallocatedEl = document.getElementById("smart-modal-reallocated");
  const wearEl = document.getElementById("smart-modal-wear");
  const errorsEl = document.getElementById("smart-modal-errors");
  const attrTbody = document.getElementById("smart-modal-attributes-tbody");

  if (statusBadge) {
    statusBadge.className = "smart-badge smart-unknown";
    statusBadge.textContent = "LOADING...";
  }
  if (typeBadge) typeBadge.textContent = "--";
  if (modelEl) modelEl.textContent = "Loading...";
  if (serialEl) serialEl.textContent = "--";
  if (firmwareEl) firmwareEl.textContent = "--";
  if (tempEl) tempEl.textContent = "--°C";
  if (hoursEl) hoursEl.textContent = "--";
  if (reallocatedEl) reallocatedEl.textContent = "--";
  if (wearEl) wearEl.textContent = "--";
  if (errorsEl) errorsEl.textContent = "--";
  if (attrTbody) {
    attrTbody.innerHTML = `<tr><td colspan="6" class="empty-state">Loading attribute telemetry...</td></tr>`;
  }

  modal.classList.remove("hidden");

  try {
    const disk = await apiFetch("/api/hardware/disks/" + encodeURIComponent(cleanName));
    if (!disk) {
      if (modelEl) modelEl.textContent = "No data returned";
      return;
    }

    if (statusBadge) {
      const isPassed = disk.passed === true || disk.status === "passed";
      const isFailed = disk.passed === false || disk.status === "failed";
      if (isPassed) {
        statusBadge.className = "smart-badge smart-passed";
        statusBadge.textContent = "PASSED";
      } else if (isFailed) {
        statusBadge.className = "smart-badge smart-failed";
        statusBadge.textContent = "FAILED";
      } else {
        statusBadge.className = "smart-badge smart-unknown";
        statusBadge.textContent = (disk.status || "UNKNOWN").toUpperCase();
      }
    }

    if (typeBadge) {
      typeBadge.textContent = (disk.device_type || "DISK").toUpperCase();
    }

    if (modelEl) modelEl.textContent = disk.model || "Unknown";
    if (serialEl) serialEl.textContent = disk.serial_number || "Unknown";
    if (firmwareEl) firmwareEl.textContent = disk.firmware_version || "Unknown";

    if (tempEl) {
      if (disk.temperature_c !== null && disk.temperature_c !== undefined) {
        tempEl.textContent = `${disk.temperature_c}°C`;
      } else {
        tempEl.textContent = "--°C";
      }
    }

    if (hoursEl) {
      hoursEl.textContent =
        disk.power_on_hours !== null && disk.power_on_hours !== undefined
          ? `${Number(disk.power_on_hours).toLocaleString()} hrs`
          : "--";
    }

    if (reallocatedEl) {
      reallocatedEl.textContent =
        disk.reallocated_sectors !== null && disk.reallocated_sectors !== undefined
          ? String(disk.reallocated_sectors)
          : "--";
    }

    if (wearEl) {
      wearEl.textContent =
        disk.wear_level_percent !== null && disk.wear_level_percent !== undefined
          ? `${disk.wear_level_percent}%`
          : "--";
    }

    if (errorsEl) {
      errorsEl.textContent =
        disk.media_errors !== null && disk.media_errors !== undefined
          ? String(disk.media_errors)
          : "--";
    }

    if (attrTbody) {
      attrTbody.innerHTML = "";
      const raw = disk.raw_json || {};
      const ataTable =
        raw.ata_smart_attributes && Array.isArray(raw.ata_smart_attributes.table)
          ? raw.ata_smart_attributes.table
          : null;
      const nvmeLog =
        raw.nvme_smart_health_information_log && typeof raw.nvme_smart_health_information_log === "object"
          ? raw.nvme_smart_health_information_log
          : null;

      if (ataTable && ataTable.length > 0) {
        ataTable.forEach((attr) => {
          const tr = document.createElement("tr");

          const tdId = document.createElement("td");
          tdId.className = "mono-text";
          tdId.textContent = attr.id !== undefined ? String(attr.id) : "--";

          const tdName = document.createElement("td");
          tdName.textContent = attr.name || "--";

          const tdVal = document.createElement("td");
          tdVal.className = "mono-text";
          tdVal.textContent = attr.value !== undefined ? String(attr.value) : "--";

          const tdWorst = document.createElement("td");
          tdWorst.className = "mono-text";
          tdWorst.textContent = attr.worst !== undefined ? String(attr.worst) : "--";

          const tdThresh = document.createElement("td");
          tdThresh.className = "mono-text";
          tdThresh.textContent = attr.thresh !== undefined ? String(attr.thresh) : "--";

          const tdRaw = document.createElement("td");
          tdRaw.className = "mono-text";
          let rawStr = "--";
          if (attr.raw && attr.raw.string !== undefined) {
            rawStr = String(attr.raw.string);
          } else if (attr.raw && attr.raw.value !== undefined) {
            rawStr = String(attr.raw.value);
          }
          tdRaw.textContent = rawStr;

          tr.appendChild(tdId);
          tr.appendChild(tdName);
          tr.appendChild(tdVal);
          tr.appendChild(tdWorst);
          tr.appendChild(tdThresh);
          tr.appendChild(tdRaw);
          attrTbody.appendChild(tr);
        });
      } else if (nvmeLog && Object.keys(nvmeLog).length > 0) {
        Object.entries(nvmeLog).forEach(([k, v]) => {
          const tr = document.createElement("tr");

          const tdId = document.createElement("td");
          tdId.className = "mono-text";
          tdId.textContent = "--";

          const tdName = document.createElement("td");
          tdName.textContent = k.replace(/_/g, " ");

          const tdVal = document.createElement("td");
          tdVal.className = "mono-text";
          tdVal.textContent = typeof v === "object" ? JSON.stringify(v) : String(v);

          const tdWorst = document.createElement("td");
          tdWorst.className = "mono-text";
          tdWorst.textContent = "--";

          const tdThresh = document.createElement("td");
          tdThresh.className = "mono-text";
          tdThresh.textContent = "--";

          const tdRaw = document.createElement("td");
          tdRaw.className = "mono-text";
          tdRaw.textContent = typeof v === "object" ? JSON.stringify(v) : String(v);

          tr.appendChild(tdId);
          tr.appendChild(tdName);
          tr.appendChild(tdVal);
          tr.appendChild(tdWorst);
          tr.appendChild(tdThresh);
          tr.appendChild(tdRaw);
          attrTbody.appendChild(tr);
        });
      } else {
        attrTbody.innerHTML = `<tr><td colspan="6" class="empty-state">No raw S.M.A.R.T. telemetry attributes available for this disk</td></tr>`;
      }
    }
  } catch (err) {
    console.error("Failed to fetch SMART disk detail:", err);
    if (modelEl) modelEl.textContent = "Error loading telemetry";
    if (statusBadge) {
      statusBadge.className = "smart-badge smart-failed";
      statusBadge.textContent = "ERROR";
    }
    if (attrTbody) {
      attrTbody.innerHTML = `<tr><td colspan="6" class="empty-state">Failed to load S.M.A.R.T. details: ${escapeHtml(err.message || String(err))}</td></tr>`;
    }
  }
}

/**
 * Close S.M.A.R.T. details modal.
 */
function closeSmartModal() {
  const modal = document.getElementById("smart-modal");
  if (modal) {
    modal.classList.add("hidden");
  }
}

/**
 * Render RAID Array Details card and Active Disks table.
 */
function renderRaidDetails(raid) {
  if (!raid) return;
  cachedRaidData = raid;

  const status = raid.status || "unknown";
  const isNoRaid = normalizeRaidState(status) === "none";

  // Header & Card badges
  const raidStatusBadge = document.getElementById("raid-status-badge");
  if (raidStatusBadge) {
    applyStatusBadge(
      raidStatusBadge,
      document.getElementById("raid-status-text"),
      status
    );
  }
  applyStatusBadge(document.getElementById("raid-card-badge"), null, status);

  // Live Sysfs RAID Rebuild & Sync Progress Bar
  const syncInfo = getRaidSyncInfo(raid);
  const syncContainer = document.getElementById("raid-sync-container");
  const syncLabel = document.getElementById("raid-sync-label");
  const syncFill = document.getElementById("raid-sync-progress-fill");
  const syncEta = document.getElementById("raid-sync-eta");

  if (syncContainer) {
    if (syncInfo && !isNoRaid) {
      syncContainer.classList.remove("hidden");
      const asciiBar = generateAsciiProgressBar(syncInfo.percent, 12);
      if (syncLabel) {
        setTextIfChanged(syncLabel, `${syncInfo.action}: ${syncInfo.percent.toFixed(1)}% ${asciiBar}`);
      }
      if (syncFill) {
        setWidthIfChanged(syncFill, syncInfo.percent.toFixed(1));
      }
      if (syncEta) {
        let etaText = "";
        if (syncInfo.speed) etaText += `${syncInfo.speed} `;
        if (syncInfo.eta) etaText += `(ETA: ${syncInfo.eta})`;
        setTextIfChanged(syncEta, etaText.trim());
      }
    } else {
      syncContainer.classList.add("hidden");
    }
  }

  // Device count boxes
  const activeCount = String(raid.active_devices ?? "0");
  const workingCount = String(raid.working_devices ?? "0");
  const failedCount = String(raid.failed_devices ?? "0");
  const spareCount = String(raid.spare_devices ?? "0");

  const activeEl = document.getElementById("raid-active-devices");
  const workingEl = document.getElementById("raid-working-devices");
  const failedEl = document.getElementById("raid-failed-devices");
  const spareEl = document.getElementById("raid-spare-devices");

  if (activeEl) setTextIfChanged(activeEl, activeCount);
  if (workingEl) setTextIfChanged(workingEl, workingCount);
  if (failedEl) {
    setTextIfChanged(failedEl, failedCount);
    failedEl.classList.toggle("count-danger", Number(failedCount) > 0);
  }
  if (spareEl) setTextIfChanged(spareEl, spareCount);

  // Details definition list
  const setField = (id, value) => {
    const el = document.getElementById(id);
    if (el) setTextIfChanged(el, value || "--");
  };

  if (isNoRaid) {
    setField("raid-device", "No RAID array detected");
    setField("raid-level", "--");
    setField("raid-array-size", "--");
    setField("raid-devices-summary", "--");
    setField("raid-storage-info", "--");
    setField("raid-check-status", "--");
    setField("raid-full-state", "No RAID array detected");
    setField("raid-persistence", "--");
    setField("raid-uuid", "--");
    setField("raid-layout-chunk", "--");
    setField("raid-consistency-policy", "--");
    setField("raid-consistency-events", "--");
  } else {
    setField("raid-device", raid.raid_device);
    setField("raid-level", (raid.raid_level || "unknown").toUpperCase());
    setField("raid-array-size", raid.array_size);
    setField(
      "raid-devices-summary",
      `${activeCount} active, ${workingCount} working, ${failedCount} failed, ${spareCount} spare`
    );
    setField("raid-storage-info", raid.storage_info);
    setField("raid-check-status", raid.check_status);
    setField("raid-full-state", raid.full_state || status);
    setField("raid-persistence", raid.persistence);

    // UUID
    setField(
      "raid-uuid",
      raid.uuid && raid.uuid !== "unknown" ? raid.uuid : "N/A"
    );

    // Layout / Chunk
    const levelStr = String(raid.raid_level || "")
      .trim()
      .toLowerCase()
      .replace(/[\s-_]/g, "");
    let layoutChunkVal = "N/A";
    if (levelStr === "raid1" || levelStr === "1") {
      layoutChunkVal = "N/A (Mirrored)";
    } else if (
      raid.layout &&
      raid.layout !== "unknown" &&
      raid.chunk_size &&
      raid.chunk_size !== "unknown"
    ) {
      layoutChunkVal = `${raid.layout} / ${raid.chunk_size}`;
    }
    setField("raid-layout-chunk", layoutChunkVal);

    // Consistency Policy
    const consistencyPolicyVal =
      raid.consistency_policy && raid.consistency_policy !== "unknown"
        ? raid.consistency_policy
        : "N/A";
    setField("raid-consistency-policy", consistencyPolicyVal);
    setField("raid-consistency-events", consistencyPolicyVal);
  }

  // Active Disks table
  const devices = Array.isArray(raid.devices) ? raid.devices : [];
  const disksBadge = document.getElementById("disks-count-badge");
  if (disksBadge) {
    setTextIfChanged(disksBadge, `${devices.length} ${devices.length === 1 ? "disk" : "disks"}`);
  }

  const tbody = document.getElementById("active-disks-tbody");
  if (!tbody) return;

  if (isNoRaid || devices.length === 0) {
    const emptyText = isNoRaid
      ? "No RAID array configured or detected on this host"
      : "No active member disks detected";
    let emptyRow = tbody.querySelector ? tbody.querySelector("tr.empty-row, tr > td.empty-state") : null;
    if (emptyRow) {
      const td = emptyRow.tagName === "TD" ? emptyRow : (emptyRow.querySelector("td") || emptyRow.children[0]);
      if (td) setTextIfChanged(td, emptyText);
    } else {
      tbody.innerHTML = "";
      const tr = document.createElement("tr");
      tr.className = "empty-row";
      const td = document.createElement("td");
      td.colSpan = 6;
      td.className = "empty-state";
      td.textContent = emptyText;
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    return;
  }

  // Remove empty state row if present
  const emptyCell = tbody.querySelector ? tbody.querySelector("td.empty-state") : null;
  if (emptyCell && emptyCell.parentElement && emptyCell.parentElement.parentElement === tbody) {
    tbody.removeChild(emptyCell.parentElement);
  }

  // Map existing device rows by data-device
  const existingRows = new Map();
  const childRows = Array.from(tbody.children || []);
  childRows.forEach((row) => {
    const devAttr = row.getAttribute ? row.getAttribute("data-device") : null;
    if (devAttr) {
      existingRows.set(devAttr, row);
    }
  });

  const activeDeviceKeys = new Set();

  devices.forEach((item) => {
    const rawDev = item.device || "unknown";
    const cleanDev = rawDev.replace("/dev/", "");
    activeDeviceKeys.add(cleanDev);

    const smartData =
      smartDisksCache[cleanDev] ||
      smartDisksCache[rawDev] ||
      smartDisksCache[`/dev/${cleanDev}`] ||
      {};

    const modelName = item.model || smartData.model || "Unknown";

    const stateStr = String(item.state || "unknown").toLowerCase();
    const isHealthy =
      stateStr.includes("active") ||
      stateStr.includes("sync") ||
      stateStr.includes("clean");
    const isFaulty =
      stateStr.includes("faulty") ||
      stateStr.includes("failed") ||
      stateStr.includes("removed");
    const stateBadgeClass = `status-badge ${
      isFaulty
        ? "status-failed"
        : isHealthy
          ? "status-clean"
          : "status-degraded"
    }`;
    const stateText = item.state || "unknown";

    const isPassed =
      item.passed === true ||
      smartData.passed === true ||
      (item.smart_status && item.smart_status.toUpperCase() === "PASSED");
    const isFailed =
      item.passed === false ||
      smartData.passed === false ||
      (item.smart_status && item.smart_status.toUpperCase() === "FAILED");

    let smartClass = "smart-unknown";
    let smartText = "UNKNOWN";
    if (isPassed) {
      smartClass = "smart-passed";
      smartText = "PASSED";
    } else if (isFailed) {
      smartClass = "smart-failed";
      smartText = "FAILED";
    } else if (item.smart_status) {
      smartText = item.smart_status.toUpperCase();
    }
    const fullSmartClass = `smart-badge ${smartClass}`;
    const smartTitle = `View S.M.A.R.T. diagnostics for ${cleanDev}`;

    const tempVal =
      item.temperature_c !== undefined && item.temperature_c !== null
        ? item.temperature_c
        : smartData.temperature_c !== undefined &&
            smartData.temperature_c !== null
          ? smartData.temperature_c
          : null;

    const slotRoleVal =
      item.slot !== undefined && item.slot !== null && item.slot !== ""
        ? item.slot === "-"
          ? "Spare"
          : `Slot ${item.slot}`
        : item.role ||
          (item.state && item.state.includes("spare")
            ? "Spare"
            : item.state
              ? item.state.split(" ")[0]
              : "--") ||
          "--";

    let tr = existingRows.get(cleanDev);
    if (tr) {
      // Reconcile existing row in-place
      const cells = tr.children;
      if (cells && cells.length >= 6) {
        setTextIfChanged(cells[0], rawDev);
        setTextIfChanged(cells[1], modelName);

        const stBadge = cells[2].querySelector ? cells[2].querySelector(".status-badge") : cells[2].children[0];
        setBadgeIfChanged(stBadge, null, stateBadgeClass, stateText);

        const smBtn = cells[3].querySelector ? cells[3].querySelector("button.smart-badge") : cells[3].children[0];
        if (smBtn) {
          if (smBtn.className !== fullSmartClass) smBtn.className = fullSmartClass;
          if (smBtn.getAttribute("data-disk") !== cleanDev) smBtn.setAttribute("data-disk", cleanDev);
          if (smBtn.title !== smartTitle) smBtn.title = smartTitle;
          setTextIfChanged(smBtn, smartText);
        }

        if (tempVal !== null && tempVal !== undefined) {
          const tempNum = Number(tempVal);
          const tempClass = tempNum > 60 ? "temp-badge temp-crit" : tempNum > 50 ? "temp-badge temp-warn" : "temp-badge temp-ok";
          let tBadge = cells[4].querySelector ? cells[4].querySelector(".temp-badge") : null;
          if (!tBadge) {
            cells[4].innerHTML = "";
            tBadge = document.createElement("span");
            cells[4].appendChild(tBadge);
          }
          setBadgeIfChanged(tBadge, null, tempClass, `${tempNum}°C`);
        } else {
          let mutedSpan = cells[4].querySelector ? cells[4].querySelector(".text-muted") : null;
          if (!mutedSpan) {
            cells[4].innerHTML = "";
            mutedSpan = document.createElement("span");
            mutedSpan.className = "text-muted";
            cells[4].appendChild(mutedSpan);
          }
          setTextIfChanged(mutedSpan, "--");
        }

        setTextIfChanged(cells[5], slotRoleVal);
      }
    } else {
      // Create new keyed row
      tr = document.createElement("tr");
      tr.setAttribute("data-device", cleanDev);

      const tdDev = document.createElement("td");
      tdDev.className = "mono-text";
      tdDev.textContent = rawDev;

      const tdModel = document.createElement("td");
      tdModel.textContent = modelName;

      const tdState = document.createElement("td");
      const stateBadge = document.createElement("span");
      stateBadge.className = stateBadgeClass;
      stateBadge.textContent = stateText;
      tdState.appendChild(stateBadge);

      const tdSmart = document.createElement("td");
      const smartBtn = document.createElement("button");
      smartBtn.type = "button";
      smartBtn.className = fullSmartClass;
      smartBtn.setAttribute("data-disk", cleanDev);
      smartBtn.title = smartTitle;
      smartBtn.textContent = smartText;
      tdSmart.appendChild(smartBtn);

      const tdTemp = document.createElement("td");
      if (tempVal !== null && tempVal !== undefined) {
        const tempBadge = document.createElement("span");
        const tempNum = Number(tempVal);
        const tempClass =
          tempNum > 60 ? "temp-crit" : tempNum > 50 ? "temp-warn" : "temp-ok";
        tempBadge.className = `temp-badge ${tempClass}`;
        tempBadge.textContent = `${tempNum}°C`;
        tdTemp.appendChild(tempBadge);
      } else {
        const noTemp = document.createElement("span");
        noTemp.className = "text-muted";
        noTemp.textContent = "--";
        tdTemp.appendChild(noTemp);
      }

      const tdSlot = document.createElement("td");
      tdSlot.textContent = slotRoleVal;

      tr.appendChild(tdDev);
      tr.appendChild(tdModel);
      tr.appendChild(tdState);
      tr.appendChild(tdSmart);
      tr.appendChild(tdTemp);
      tr.appendChild(tdSlot);
      tbody.appendChild(tr);
    }
  });

  // Remove detached device rows
  existingRows.forEach((row, devKey) => {
    if (!activeDeviceKeys.has(devKey) && row.parentElement === tbody) {
      tbody.removeChild(row);
    }
  });

  // Overview summary card
  const overviewRaidBadge = document.getElementById("overview-raid-status-badge");
  if (overviewRaidBadge) {
    applyStatusBadge(overviewRaidBadge, null, status);
  }
  const overviewRaidSummary = document.getElementById("overview-raid-summary");
  if (overviewRaidSummary) {
    if (isNoRaid) {
      setTextIfChanged(overviewRaidSummary, "No RAID array configured or detected");
    } else if (syncInfo && ["resyncing", "recovering"].includes(status)) {
      const level = (raid.raid_level || "RAID").toUpperCase();
      const speedText = syncInfo.speed ? ` • ${syncInfo.speed}` : "";
      const etaText = syncInfo.eta ? ` (ETA: ${syncInfo.eta})` : "";
      setTextIfChanged(overviewRaidSummary, `${level} • ${syncInfo.action} ${syncInfo.percent.toFixed(1)}%${speedText}${etaText}`);
    } else {
      const level = (raid.raid_level || "RAID").toUpperCase();
      const size = raid.array_size || "--";
      const act = raid.active_devices ?? 0;
      const tot = raid.working_devices ?? act;
      setTextIfChanged(overviewRaidSummary, `${level} • ${size} • ${act}/${tot} disks active`);
    }
  }
}

/**
 * Render Weekly Notification State card.
 */
function renderWeeklyNotificationState(notificationState) {
  const badgeEl = document.getElementById("weekly-state-badge");
  const sentAtEl = document.getElementById("weekly-sent-at");
  const dueAtEl = document.getElementById("weekly-due-at");
  const lastStatusEl = document.getElementById("weekly-last-status");
  const countdownEl = document.getElementById("weekly-countdown");

  if (!notificationState) {
    currentWeeklyDueAt = null;
    applyStatusBadge(badgeEl, null, "unknown");
    if (badgeEl) badgeEl.textContent = "NOT RECORDED";
    if (sentAtEl) sentAtEl.textContent = "No weekly summary sent yet";
    if (dueAtEl) dueAtEl.textContent = "Due on next clean/active check";
    if (lastStatusEl) lastStatusEl.textContent = "--";
    if (countdownEl) countdownEl.textContent = "Due immediately on next check";
    return;
  }

  currentWeeklyDueAt =
    notificationState.due_at !== undefined && notificationState.due_at !== null
      ? Number(notificationState.due_at)
      : null;

  const stateStatus = notificationState.status || "unknown";
  applyStatusBadge(badgeEl, null, stateStatus);

  if (sentAtEl) {
    sentAtEl.textContent =
      notificationState.sent_at_formatted ||
      formatTimestampLocal(notificationState.sent_at);
  }
  if (dueAtEl) {
    dueAtEl.textContent =
      notificationState.due_at_formatted ||
      formatTimestampLocal(notificationState.due_at);
  }
  if (lastStatusEl) {
    lastStatusEl.textContent = stateStatus.toUpperCase();
  }

  if (countdownEl) {
    const dueAtSec = Number(notificationState.due_at);
    const nowSec = Date.now() / 1000;
    const remaining = Math.floor(dueAtSec - nowSec);
    if (!Number.isFinite(dueAtSec) || remaining <= 0) {
      countdownEl.textContent = "Due on the next clean/active RAID check";
    } else {
      const days = Math.floor(remaining / 86400);
      const hours = Math.floor((remaining % 86400) / 3600);
      const minutes = Math.floor((remaining % 3600) / 60);
      countdownEl.textContent = `${days}d ${hours}h ${minutes}m remaining`;
    }
  }
}

let cachedNotificationHistorySig = null;

/**
 * Render Notification History table from /api/history?limit=20 payload.
 */
function renderNotificationHistory(historyPayload) {
  const records = Array.isArray(historyPayload)
    ? historyPayload
    : Array.isArray(historyPayload?.history)
      ? historyPayload.history
      : [];

  const countBadge = document.getElementById("history-count-badge");
  if (countBadge) {
    setTextIfChanged(
      countBadge,
      `${records.length} ${records.length === 1 ? "record" : "records"}`
    );
  }

  renderDrawerNotificationList(records);

  const tbody = document.getElementById("notification-history-tbody");
  if (!tbody) return;

  const sig = records.length === 0
    ? "__EMPTY__"
    : `${records.length}::` + records.map((r) => `${r.id ?? ""}:${r.sent_at ?? ""}:${r.outcome ?? ""}:${r.status ?? ""}:${r.trigger ?? ""}:${r.channel ?? ""}`).join("|");

  if (cachedNotificationHistorySig === sig && tbody.children && tbody.children.length > 0) {
    return;
  }
  cachedNotificationHistorySig = sig;

  tbody.innerHTML = "";

  if (records.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.className = "empty-state";
    td.textContent = "No notifications recorded yet";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  records.forEach((entry) => {
    const tr = document.createElement("tr");

    // 1. Timestamp (tdTime)
    const tdTime = document.createElement("td");
    tdTime.className = "mono-text";
    tdTime.textContent =
      entry.sent_at_formatted || formatTimestampLocal(entry.sent_at);

    // 2. Trigger / Source (tdTrigger)
    const tdTrigger = document.createElement("td");
    const triggerBadge = document.createElement("span");
    const triggerStr = String(entry.trigger || "");
    const statusStr = String(entry.status || "").trim();
    const statusLower = statusStr.toLowerCase();

    if (entry.trigger?.includes("cron:") || entry.status === "scheduled_report") {
      triggerBadge.className = "trigger-badge trigger-cron";
      triggerBadge.setAttribute("role", "button");
      triggerBadge.setAttribute("tabindex", "0");
      triggerBadge.setAttribute(
        "title",
        "Managed in Backups & Cron Scheduler (click to view)"
      );
      triggerBadge.textContent = "CRON: NOTIFICATIONS.SUMMARY";
      triggerBadge.addEventListener("click", () => switchViewTab("backups"));
      triggerBadge.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          switchViewTab("backups");
        }
      });
    } else if (
      entry.trigger?.startsWith("threshold:") ||
      entry.status?.startsWith("alert_")
    ) {
      const metric =
        entry.trigger?.replace("threshold:", "").toUpperCase() ||
        entry.status?.replace("alert_", "").toUpperCase() ||
        "METRIC";
      triggerBadge.className = "trigger-badge trigger-threshold";
      triggerBadge.textContent = `THRESHOLD: ${metric}`;
    } else if (
      entry.trigger?.startsWith("sentinel") ||
      entry.status?.startsWith("sentinel_")
    ) {
      triggerBadge.className = "trigger-badge trigger-sentinel";
      triggerBadge.textContent = "SENTINEL PROBE";
    } else if (entry.trigger === "manual_test" || entry.status === "test") {
      triggerBadge.className = "trigger-badge trigger-test";
      triggerBadge.textContent = "MANUAL TEST";
    } else if (
      entry.trigger === "raid_monitor" ||
      VALID_RAID_STATES.has(entry.status?.toLowerCase())
    ) {
      triggerBadge.className = "trigger-badge trigger-raid";
      triggerBadge.textContent = "RAID MONITOR";
    } else {
      triggerBadge.className = "trigger-badge trigger-neutral";
      triggerBadge.textContent = entry.trigger || entry.status || "SYSTEM";
    }
    tdTrigger.appendChild(triggerBadge);

    // 3. Channel (tdChannel)
    const tdChannel = document.createElement("td");
    const channelBadge = document.createElement("span");
    const channelName = (entry.channel || "all").toLowerCase();
    channelBadge.className = `channel-badge channel-${channelName}`;
    channelBadge.textContent = (entry.channel || "all").toUpperCase();
    tdChannel.appendChild(channelBadge);

    // 4. Subject / Summary (tdSummary)
    const tdSummary = document.createElement("td");
    const titleDiv = document.createElement("div");
    titleDiv.className = "delivery-title";
    titleDiv.textContent = entry.title || entry.kind || "Alert Dispatch";

    const detailsDiv = document.createElement("div");
    detailsDiv.className = "delivery-details";
    detailsDiv.textContent = entry.details || "";

    tdSummary.appendChild(titleDiv);
    tdSummary.appendChild(detailsDiv);

    // 5. Delivery Outcome (tdOutcome)
    const tdOutcome = document.createElement("td");
    const outcomeBadge = document.createElement("span");
    if (entry.outcome === "delivered") {
      outcomeBadge.className = "status-badge status-ok";
      outcomeBadge.textContent = "✓ Delivered";
    } else if (entry.outcome === "failed") {
      outcomeBadge.className = "status-badge status-danger";
      outcomeBadge.textContent = "✗ Failed";
    } else {
      outcomeBadge.className = "status-badge status-info";
      outcomeBadge.textContent = entry.outcome || "DISPATCHED";
    }
    tdOutcome.appendChild(outcomeBadge);

    tr.appendChild(tdTime);
    tr.appendChild(tdTrigger);
    tr.appendChild(tdChannel);
    tr.appendChild(tdSummary);
    tr.appendChild(tdOutcome);
    tbody.appendChild(tr);
  });
}

let currentDrawerFilter = "all";
let cachedDrawerRecords = [];
let dismissedNotificationIds = new Set();
try {
  const savedDismissed = localStorage.getItem("vigil_dismissed_notifications");
  if (savedDismissed) {
    dismissedNotificationIds = new Set(JSON.parse(savedDismissed));
  }
} catch (e) {}

function saveDismissedNotifications() {
  try {
    localStorage.setItem(
      "vigil_dismissed_notifications",
      JSON.stringify(Array.from(dismissedNotificationIds))
    );
  } catch (e) {}
}

/**
 * Render recent dispatches stream in the slide-over notification drawer.
 */
function renderDrawerNotificationList(records) {
  if (Array.isArray(records)) {
    cachedDrawerRecords = records;
  }
  const listEl = document.getElementById("drawer-notification-list");
  if (!listEl) return;

  const allRecords = cachedDrawerRecords || [];
  const activeRecords = allRecords.filter((entry) => {
    const id = String(entry.id ?? entry.sent_at ?? entry.timestamp ?? "");
    return id ? !dismissedNotificationIds.has(id) : true;
  });

  const alertsCount = activeRecords.filter((entry) => {
    const st = String(entry.status || "").toLowerCase();
    const kd = String(entry.kind || "").toLowerCase();
    return (
      st === "degraded" ||
      st === "critical" ||
      st === "failed" ||
      st === "warning" ||
      kd.includes("alert")
    );
  }).length;

  const appCount = activeRecords.filter((entry) => {
    const isWeekly = Boolean(entry.weekly) || entry.kind === "weekly summary";
    const isTest = entry.kind === "test notification" || entry.status === "test";
    return isWeekly || isTest;
  }).length;

  const badgeAll = document.getElementById("drawer-badge-all");
  const badgeAlerts = document.getElementById("drawer-badge-alerts");
  const badgeApp = document.getElementById("drawer-badge-app");
  const notifBadge = document.getElementById("notif-drawer-badge");

  if (badgeAll) badgeAll.textContent = String(activeRecords.length);
  if (badgeAlerts) badgeAlerts.textContent = String(alertsCount);
  if (badgeApp) badgeApp.textContent = String(appCount);

  if (notifBadge) {
    if (alertsCount > 0) {
      notifBadge.textContent = String(alertsCount);
      notifBadge.classList.remove("hidden");
      notifBadge.classList.add("has-alerts");
    } else {
      notifBadge.textContent = "0";
      notifBadge.classList.add("hidden");
      notifBadge.classList.remove("has-alerts");
    }
  }

  let displayedRecords = activeRecords;
  if (currentDrawerFilter === "alerts") {
    displayedRecords = activeRecords.filter((entry) => {
      const st = String(entry.status || "").toLowerCase();
      const kd = String(entry.kind || "").toLowerCase();
      return (
        st === "degraded" ||
        st === "critical" ||
        st === "failed" ||
        st === "warning" ||
        kd.includes("alert")
      );
    });
  } else if (currentDrawerFilter === "application") {
    displayedRecords = activeRecords.filter((entry) => {
      const isWeekly = Boolean(entry.weekly) || entry.kind === "weekly summary";
      const isTest = entry.kind === "test notification" || entry.status === "test";
      return isWeekly || isTest;
    });
  }

  if (displayedRecords.length === 0) {
    listEl.innerHTML = `
      <div class="drawer-empty-state">
        <svg viewBox="0 0 24 24" width="32" height="32" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <polyline points="20 6 9 17 4 12"></polyline>
        </svg>
        <p>All caught up! No active notifications.</p>
      </div>
    `;
    return;
  }

  listEl.innerHTML = displayedRecords
    .map((entry) => {
      const entryId = String(entry.id ?? entry.sent_at ?? entry.timestamp ?? "");
      const timeStr = entry.sent_at_formatted || formatTimestampLocal(entry.sent_at);
      const isWeekly = Boolean(entry.weekly) || entry.kind === "weekly summary";
      const isTest = entry.kind === "test notification" || entry.status === "test";
      const isDegraded =
        entry.status === "degraded" ||
        entry.status === "critical" ||
        entry.status === "failed";

      let kindClass = "kind-alert";
      let kindText = entry.kind || "Alert";
      if (isTest) {
        kindClass = "kind-test";
        kindText = "Test Dispatch";
      } else if (isWeekly) {
        kindClass = "kind-weekly";
        kindText = "Weekly Summary";
      }

      let statusBadgeClass = "status-badge";
      if (isDegraded) statusBadgeClass += " status-failed";
      else if (isTest) statusBadgeClass += " status-test";
      else statusBadgeClass += " status-ok";

      const statusText = (entry.status || "OK").toUpperCase();
      const details =
        entry.message ||
        entry.details ||
        entry.raid_status ||
        (isWeekly
          ? "Scheduled system health summary report"
          : isTest
          ? "Manual channel delivery test"
          : "System telemetry status check");

      return `
      <div class="drawer-item ${isDegraded ? "drawer-item-alert" : ""}">
        <div class="drawer-item-top">
          <div class="drawer-item-badges">
            <span class="kind-badge ${kindClass}">${escapeHtml(kindText)}</span>
            <span class="${statusBadgeClass}">${escapeHtml(statusText)}</span>
          </div>
          <div style="display:flex;align-items:center;gap:0.4rem;">
            <time class="drawer-item-time mono-text">${escapeHtml(timeStr)}</time>
            ${
              entryId
                ? `<button class="btn-icon-subtle btn-dismiss-item" data-id="${escapeHtml(
                    entryId
                  )}" title="Dismiss notification" aria-label="Dismiss notification" style="padding:2px 4px;font-size:1rem;line-height:1;opacity:0.6;">&times;</button>`
                : ""
            }
          </div>
        </div>
        <div class="drawer-item-message">${escapeHtml(details)}</div>
      </div>
    `;
    })
    .join("");

  listEl.querySelectorAll(".btn-dismiss-item").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const id = btn.getAttribute("data-id");
      if (id) {
        dismissedNotificationIds.add(id);
        saveDismissedNotifications();
        renderDrawerNotificationList();
      }
    });
  });
}

/**
 * Slide-over notification drawer controllers.
 */
function openNotificationDrawer() {
  const drawer = document.getElementById("notification-drawer");
  const backdrop = document.getElementById("notification-drawer-backdrop");
  if (!drawer) return;

  drawer.classList.remove("hidden");
  void drawer.offsetWidth;
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");

  if (backdrop) {
    backdrop.classList.remove("hidden");
    void backdrop.offsetWidth;
    backdrop.classList.add("open");
  }

  fetchNotificationHistoryDrawer();
}

function closeNotificationDrawer() {
  const drawer = document.getElementById("notification-drawer");
  const backdrop = document.getElementById("notification-drawer-backdrop");
  if (!drawer) return;

  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");

  if (backdrop) {
    backdrop.classList.remove("open");
  }

  setTimeout(() => {
    if (!drawer.classList.contains("open")) {
      drawer.classList.add("hidden");
    }
    if (backdrop && !backdrop.classList.contains("open")) {
      backdrop.classList.add("hidden");
    }
  }, 280);
}

async function fetchNotificationHistoryDrawer() {
  const refreshBtn = document.getElementById("btn-drawer-refresh-history");
  if (refreshBtn) refreshBtn.classList.add("loading");
  try {
    const resp = await apiFetch("/api/history?limit=20");
    if (resp.ok) {
      const data = await resp.json().catch(() => ({}));
      renderNotificationHistory(data);
      setModuleLastUpdated("notifications");
    }
  } catch (err) {
    console.debug("Failed to refresh drawer history:", err);
  } finally {
    if (refreshBtn) refreshBtn.classList.remove("loading");
  }
}

/**
 * Show or hide the error banner.
 */
function setErrorState(message) {
  const banner = document.getElementById("error-banner");
  const msgEl = document.getElementById("error-message");
  if (!banner) return;

  if (message) {
    if (msgEl) msgEl.textContent = message;
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
}

/**
 * Update the header version badge.
 */
function updateVersionBadge(versionInfo) {
  if (!versionInfo) return;
  const badge = document.getElementById("version-badge");
  if (!badge) return;

  const ver =
    versionInfo.current_version ||
    versionInfo.version ||
    badge.textContent ||
    "v0.1.0";
  const formattedVer = String(ver).startsWith("v") ? ver : `v${ver}`;
  const commit = versionInfo.current_commit || versionInfo.commit || "";
  badge.textContent = commit ? `${formattedVer} (${commit})` : formattedVer;
}

/**
 * Fetch both /api/status and /api/history?limit=20 and update the UI.
 */
async function refreshDashboard({ manual = false } = {}) {
  if (isFetching || !isAuthenticated) return;
  const isLive = sseConnected || (typeof VigilLiveStore !== "undefined" && VigilLiveStore.getState("connection") === "live");
  if (!manual && isLive) {
    return;
  }
  isFetching = true;

  const refreshBtn = document.getElementById("refresh-btn");
  if (manual && refreshBtn) refreshBtn.classList.add("loading");

  try {
    const [statusResp, historyResp] = await Promise.all([
      apiFetch("/api/status"),
      apiFetch("/api/history?limit=20"),
    ]);

    if (statusResp.status === 401 || historyResp.status === 401) {
      return;
    }

    if (!statusResp.ok) {
      throw new Error(`GET /api/status failed with HTTP ${statusResp.status}`);
    }
    if (!historyResp.ok) {
      throw new Error(`GET /api/history failed with HTTP ${historyResp.status}`);
    }

    const statusData = await statusResp.json();
    const historyData = await historyResp.json();

    renderRaidDetails(statusData.raid || statusData);
    renderSystemMetrics(statusData.system || statusData);
    renderWeeklyNotificationState(statusData.notification_state ?? null);
    renderNotificationHistory(historyData);

    setModuleLastUpdated("system");
    setModuleLastUpdated("raid");
    setModuleLastUpdated("notifications");

    if (statusData.raid) {
      const raidStatus = String(statusData.raid.status || "").toLowerCase();
      updateRaidAdaptiveBadge(
        ["recovering", "resyncing", "degraded"].includes(raidStatus)
      );
    }
    if (statusData.refresh_intervals) {
      syncIntervalSelects(statusData.refresh_intervals);
    }

    if (statusData.version || statusData.version_info) {
      updateVersionBadge(statusData.version_info || statusData.version);
    }

    setErrorState(null);
    updateHeaderLastUpdated(true);
  } catch (err) {
    console.error("Dashboard refresh error:", err);
    setErrorState(
      `Failed to refresh dashboard data: ${err instanceof Error ? err.message : String(err)}`
    );
  } finally {
    isFetching = false;
    if (manual && refreshBtn) refreshBtn.classList.remove("loading");
  }
}

/* =========================================================================
 * 1. Authentication Gate (#auth-overlay)
 * ========================================================================= */

function showAuthError(message) {
  const errEl = document.getElementById("auth-error");
  if (!errEl) return;
  if (message) {
    errEl.textContent = message;
    errEl.classList.remove("hidden");
  } else {
    errEl.textContent = "";
    errEl.classList.add("hidden");
  }
}

function stopPolling() {
  if (refreshTimerId) {
    window.clearInterval(refreshTimerId);
    refreshTimerId = null;
  }
}

function startPolling({ immediate = true } = {}) {
  stopPolling();
  const isLive = sseConnected || (typeof VigilLiveStore !== "undefined" && VigilLiveStore.getState("connection") === "live");
  if (isLive) {
    return;
  }
  if (immediate) {
    refreshDashboard();
  }
  refreshTimerId = window.setInterval(refreshDashboard, AUTO_REFRESH_INTERVAL_MS);
}

function pollSystemStatus() {
  return refreshDashboard();
}

function pollDockerUpdates() {
  if (typeof fetchDockerStatus === "function") {
    fetchDockerStatus();
  }
  if (typeof fetchDockerUpdates === "function") {
    fetchDockerUpdates({ background: true });
  }
}

function adjustAdaptivePolling(isSSEActive) {
  if (isSSEActive) {
    stopPolling();
  } else {
    stopPolling();
    refreshTimerId = window.setInterval(refreshDashboard, AUTO_REFRESH_INTERVAL_MS);
  }
}

/**
 * Update #auto-refresh-indicator and #live-stream-indicator to reflect WebSocket / stream state.
 */
function updateConnectionIndicator(state) {
  const indicator = document.getElementById("auto-refresh-indicator");
  const streamIndicator = document.getElementById("live-stream-indicator");
  const streamText = document.getElementById("live-stream-text");

  if (indicator) {
    indicator.classList.remove("ws-live", "ws-reconnecting");
    if (state === "live") {
      indicator.textContent = "LIVE (WebSocket)";
      indicator.classList.add("ws-live");
    } else if (state === "reconnecting") {
      indicator.textContent = "Reconnecting...";
      indicator.classList.add("ws-reconnecting");
    } else {
      indicator.textContent = "Auto-refresh: 10s";
    }
  }

  if (streamIndicator) {
    streamIndicator.classList.remove("stream-active", "stream-reconnecting", "stream-paused");
    if (state === "live") {
      streamIndicator.classList.add("stream-active");
      if (streamText) streamText.textContent = "Live Stream";
      streamIndicator.setAttribute("title", "Direct system event streaming active (POSIX statvfs, sysfs RAID, Docker socket)");
    } else if (state === "reconnecting") {
      streamIndicator.classList.add("stream-reconnecting");
      if (streamText) streamText.textContent = "Reconnecting (Polling active)";
      streamIndicator.setAttribute("title", "Reconnecting to direct system event stream... (standard interval polling active)");
    } else {
      streamIndicator.classList.add("stream-paused");
      if (streamText) streamText.textContent = "Stream: Polling";
      streamIndicator.setAttribute("title", "HTTP fallback polling mode");
    }
  }
}

/**
 * Cleanly close the active WebSocket connection and cancel pending reconnect timers.
 */
function closeWebSocket() {
  if (wsReconnectTimerId) {
    window.clearTimeout(wsReconnectTimerId);
    wsReconnectTimerId = null;
  }
  if (wsConnection) {
    wsConnection.onopen = null;
    wsConnection.onmessage = null;
    wsConnection.onerror = null;
    wsConnection.onclose = null;
    try {
      wsConnection.close();
    } catch {
      // Ignore close errors
    }
    wsConnection = null;
  }
}

/**
 * Cleanly close active Server-Sent Events (SSE) EventSource connection.
 */
function closeSSEStream() {
  if (sseReconnectTimerId) {
    window.clearTimeout(sseReconnectTimerId);
    sseReconnectTimerId = null;
  }
  if (sseSource) {
    sseSource.onopen = null;
    sseSource.onerror = null;
    try {
      sseSource.close();
    } catch {
      // Ignore close errors
    }
    sseSource = null;
  }
  sseConnected = false;
  if (typeof VigilLiveStore !== "undefined") {
    VigilLiveStore.dispatch("connection", "disconnected");
  }
}

const closeSSEConnection = closeSSEStream;

/**
 * In-place patch target container row in #docker-containers-tbody.
 */
function patchDockerContainerRowInPlace(containerId, containerName, action) {
  const tbody = document.getElementById("docker-containers-tbody");
  if (tbody) {
    const rows = tbody.querySelectorAll ? tbody.querySelectorAll("tr") : [];
    let targetRow = null;
    const cidShort = containerId ? containerId.slice(0, 12) : "";
    for (const row of rows) {
      const rowText = row.textContent || "";
      if (
        (containerId && (rowText.includes(containerId) || (cidShort && rowText.includes(cidShort)))) ||
        (containerName && rowText.includes(containerName)) ||
        (containerId && row.querySelector && row.querySelector(`[data-cid="${containerId}"]`))
      ) {
        targetRow = row;
        break;
      }
    }

    if (targetRow) {
      targetRow.classList.remove("row-highlight");
      void targetRow.offsetWidth;
      targetRow.classList.add("row-highlight");
      window.setTimeout(() => {
        targetRow.classList.remove("row-highlight");
      }, 2000);

      const badge = targetRow.querySelector ? targetRow.querySelector(".status-badge") : null;
      if (badge) {
        badge.classList.remove(
          "status-clean",
          "status-failed",
          "status-degraded",
          "status-unhealthy",
          "status-none",
          "status-recovering"
        );
        let statusClass = "status-clean";
        const actLower = (action || "").toLowerCase();
        if (["die", "kill", "stop", "exited", "dead", "failed"].includes(actLower)) {
          statusClass = "status-failed";
        } else if (["pause", "paused", "restarting"].includes(actLower)) {
          statusClass = "status-degraded";
        } else if (actLower.includes("unhealthy")) {
          statusClass = "status-unhealthy";
        }
        badge.classList.add(statusClass);
        badge.innerHTML = `<span class="status-dot"></span> ${escapeHtml(action || "updated")}`;
      }
    }
  }

  // Update cachedDockerContainers state
  if (Array.isArray(cachedDockerContainers)) {
    const cidShort = containerId ? containerId.slice(0, 12) : "";
    const item = cachedDockerContainers.find(
      (c) =>
        (containerId && (c.id === containerId || (cidShort && c.id?.startsWith(cidShort)))) ||
        (containerName && c.name === containerName)
    );
    if (item) {
      item.state = action;
      item.status = action;
    }
  }
}

/**
 * Dynamically prepend a new notification record to #notification-history-tbody.
 */
function prependNotificationRow(entry) {
  if (!entry || typeof entry !== "object") return;
  const tbody = document.getElementById("notification-history-tbody");
  if (!tbody) return;

  // Clear empty state if present
  const emptyRow = tbody.querySelector ? tbody.querySelector(".empty-state") : null;
  if (emptyRow) {
    const parentRow = emptyRow.closest ? emptyRow.closest("tr") : emptyRow.parentElement;
    if (parentRow) {
      if (typeof parentRow.remove === "function") {
        parentRow.remove();
      } else if (parentRow.parentElement && typeof parentRow.parentElement.removeChild === "function") {
        parentRow.parentElement.removeChild(parentRow);
      }
    }
  }

  const tr = document.createElement("tr");
  tr.className = "row-highlight";
  window.setTimeout(() => tr.classList.remove("row-highlight"), 2500);

  // 1. Timestamp
  const tdTime = document.createElement("td");
  tdTime.className = "mono-text";
  tdTime.textContent = entry.sent_at_formatted || (typeof formatTimestampLocal === "function" ? formatTimestampLocal(entry.sent_at || entry.timestamp || Date.now() / 1000) : new Date().toLocaleTimeString());

  // 2. Trigger
  const tdTrigger = document.createElement("td");
  const triggerBadge = document.createElement("span");
  const triggerStr = String(entry.trigger || entry.status || "ALERT");
  triggerBadge.className = "trigger-badge";
  triggerBadge.textContent = triggerStr.toUpperCase();
  tdTrigger.appendChild(triggerBadge);

  // 3. Channel
  const tdChannel = document.createElement("td");
  tdChannel.textContent = entry.channel || "Discord";

  // 4. Subject / Summary
  const tdSubject = document.createElement("td");
  tdSubject.textContent = entry.title || entry.subject || entry.details || "-";

  // 5. Outcome
  const tdOutcome = document.createElement("td");
  const isSuccess = entry.success !== false;
  tdOutcome.innerHTML = `<span class="status-badge ${isSuccess ? "status-clean" : "status-failed"}"><span class="status-dot"></span> ${isSuccess ? "Delivered" : "Failed"}</span>`;

  tr.appendChild(tdTime);
  tr.appendChild(tdTrigger);
  tr.appendChild(tdChannel);
  tr.appendChild(tdSubject);
  tr.appendChild(tdOutcome);

  if (tbody.firstChild) {
    tbody.insertBefore(tr, tbody.firstChild);
  } else {
    tbody.appendChild(tr);
  }

  const countBadge = document.getElementById("history-count-badge");
  if (countBadge) {
    const curCount = tbody.querySelectorAll ? tbody.querySelectorAll("tr").length : 1;
    countBadge.textContent = `${curCount} ${curCount === 1 ? "record" : "records"}`;
  }
}

/**
 * Handle fine-grained SSE telemetry data.
 */
function handleSSETelemetry(data) {
  if (!data || typeof data !== "object") return;

  // 1. CPU Telemetry
  const cpuPercent = Number(
    data.cpu?.percent ?? data.cpu_percent ?? (typeof data.cpu === "number" ? data.cpu : NaN)
  );
  if (!Number.isNaN(cpuPercent)) {
    liveAnimState.targetCpu = cpuPercent;
    if (!liveAnimState.initialized) {
      liveAnimState.currentCpu = cpuPercent;
      liveAnimState.renderedCpu = cpuPercent;
    }
    const cpuBarEl = document.getElementById("cpu-progress-bar");
    if (cpuBarEl && typeof updateProgressBar === "function") {
      updateProgressBar(cpuBarEl, cpuPercent);
    }
    if (!liveAnimState.isRunning) {
      const cpuValEl = document.getElementById("cpu-percent-value");
      if (cpuValEl) setTextIfChanged(cpuValEl, `${cpuPercent.toFixed(1)}%`);
    }
  }

  // CPU Cores Badge and grid
  const cores = data.cpu?.cores || data.cpu_cores;
  if (Array.isArray(cores) && cores.length > 0) {
    const cpuCoresBadge = document.getElementById("cpu-cores-badge");
    if (cpuCoresBadge) {
      setTextIfChanged(cpuCoresBadge, `${cores.length} ${cores.length === 1 ? "Core" : "Cores"}`);
    }

    while (cpuCoresHistoryBuffers.length < cores.length) {
      cpuCoresHistoryBuffers.push([]);
    }
    if (cpuCoresHistoryBuffers.length > cores.length) {
      cpuCoresHistoryBuffers.length = cores.length;
    }

    liveAnimState.targetCores = cores.map((c) => Math.max(0, Math.min(100, Number(c) || 0)));
    if (!liveAnimState.initialized || liveAnimState.currentCores.length !== cores.length) {
      liveAnimState.currentCores = [...liveAnimState.targetCores];
      liveAnimState.renderedCores = [...liveAnimState.targetCores];
    }

    const nowTs = typeof performance !== "undefined" && typeof performance.now === "function" ? performance.now() : Date.now();
    if (liveAnimState.lastSampleTime > 0) {
      const dt = nowTs - liveAnimState.lastSampleTime;
      if (dt > 30 && dt < 5000) {
        liveAnimState.estimatedSampleInterval = liveAnimState.estimatedSampleInterval * 0.7 + dt * 0.3;
      }
    }
    liveAnimState.lastSampleTime = nowTs;

    const coresGrid = document.getElementById("cpu-cores-grid");
    const currentCoreValues = [];
    if (coresGrid) {
      let items = coresGrid.querySelectorAll ? coresGrid.querySelectorAll(".cpu-core-item") : [];
      if (items.length !== cores.length) {
        coresGrid.innerHTML = cores
          .map((_, idx) => {
            const color = CPU_CORE_COLORS[idx % CPU_CORE_COLORS.length];
            return `
          <div class="cpu-core-item" data-core-idx="${idx}" title="Core ${idx}">
            <div class="cpu-core-header">
              <div class="cpu-core-legend-info">
                <span class="cpu-core-color-dot" style="background-color: ${color};"></span>
                <span class="cpu-core-label">Core ${idx}</span>
              </div>
              <span class="cpu-core-percent mono-text">0.0%</span>
            </div>
            <div class="progress-track cpu-core-track">
              <div class="progress-fill" style="width: 0%; background-color: ${color};"></div>
            </div>
          </div>
        `;
          })
          .join("");
        items = coresGrid.querySelectorAll ? coresGrid.querySelectorAll(".cpu-core-item") : [];

        items.forEach((item) => {
          const idx = parseInt(item.getAttribute("data-core-idx"), 10);
          item.addEventListener("mouseenter", () => {
            activeHoveredCoreIndex = idx;
            const allChips = coresGrid.querySelectorAll ? coresGrid.querySelectorAll(".cpu-core-item") : [];
            allChips.forEach((chip, cIdx) => {
              chip.classList.toggle("is-hovered", cIdx === idx);
              chip.classList.toggle("is-dimmed", cIdx !== idx);
            });
            const currentValues = cpuCoresHistoryBuffers.map((b) => (b.length > 0 ? b[b.length - 1] : 0));
            const unifiedCanvas =
              document.getElementById("cpu-cores-unified-canvas") ||
              document.querySelector(".cpu-core-canvas");
            if (unifiedCanvas && typeof drawCpuCoreGraph === "function") {
              drawCpuCoreGraph(unifiedCanvas, cpuCoresHistoryBuffers, currentValues, activeHoveredCoreIndex);
            }
          });
          item.addEventListener("mouseleave", () => {
            activeHoveredCoreIndex = null;
            const allChips = coresGrid.querySelectorAll ? coresGrid.querySelectorAll(".cpu-core-item") : [];
            allChips.forEach((chip) => {
              chip.classList.remove("is-hovered", "is-dimmed");
            });
            const currentValues = cpuCoresHistoryBuffers.map((b) => (b.length > 0 ? b[b.length - 1] : 0));
            const unifiedCanvas =
              document.getElementById("cpu-cores-unified-canvas") ||
              document.querySelector(".cpu-core-canvas");
            if (unifiedCanvas && typeof drawCpuCoreGraph === "function") {
              drawCpuCoreGraph(unifiedCanvas, cpuCoresHistoryBuffers, currentValues, null);
            }
          });
        });
      }

      cores.forEach((coreVal, idx) => {
        const cPercent = Math.max(0, Math.min(100, Number(coreVal) || 0));
        currentCoreValues.push(cPercent);
        if (!cpuCoresHistoryBuffers[idx]) {
          cpuCoresHistoryBuffers[idx] = [];
        }
        const buf = cpuCoresHistoryBuffers[idx];
        buf.push(cPercent);
        if (buf.length > CPU_CORE_HISTORY_LIMIT) {
          buf.shift();
        }

        const item = items[idx];
        if (item) {
          const fillEl = item.querySelector ? item.querySelector(".cpu-core-track .progress-fill") : null;
          if (fillEl) {
            const color = CPU_CORE_COLORS[idx % CPU_CORE_COLORS.length];
            setWidthIfChanged(fillEl, cPercent.toFixed(1));
            if (fillEl.style && fillEl.style.backgroundColor !== color) {
              fillEl.style.backgroundColor = color;
            }
          }
          if (!liveAnimState.isRunning) {
            const pctEl = item.querySelector ? item.querySelector(".cpu-core-percent") : null;
            if (pctEl) setTextIfChanged(pctEl, `${cPercent.toFixed(1)}%`);
          }
        }
      });
    } else {
      cores.forEach((coreVal, idx) => {
        const cPercent = Math.max(0, Math.min(100, Number(coreVal) || 0));
        currentCoreValues.push(cPercent);
        if (!cpuCoresHistoryBuffers[idx]) {
          cpuCoresHistoryBuffers[idx] = [];
        }
        const buf = cpuCoresHistoryBuffers[idx];
        buf.push(cPercent);
        if (buf.length > CPU_CORE_HISTORY_LIMIT) {
          buf.shift();
        }
      });
    }

    const unifiedCanvas =
      document.getElementById("cpu-cores-unified-canvas") ||
      document.querySelector(".cpu-core-canvas");
    if (!liveAnimState.isRunning && unifiedCanvas && typeof unifiedCanvas.getContext === "function" && typeof drawCpuCoreGraph === "function") {
      const rAF = typeof window !== "undefined" && window.requestAnimationFrame
        ? window.requestAnimationFrame
        : typeof requestAnimationFrame === "function"
          ? requestAnimationFrame
          : (cb) => cb();
      rAF(() => {
        drawCpuCoreGraph(
          unifiedCanvas,
          cpuCoresHistoryBuffers,
          currentCoreValues,
          typeof activeHoveredCoreIndex !== "undefined" ? activeHoveredCoreIndex : null
        );
      });
    }
  }

  // CPU Load Average
  const loadAvg = data.load_avg || data.cpu?.load_avg;
  if (Array.isArray(loadAvg) && loadAvg.length > 0) {
    const cpuLoadEl = document.getElementById("cpu-load-detail");
    if (cpuLoadEl) {
      setTextIfChanged(cpuLoadEl, `Load avg: ${loadAvg.map((v) => Number(v).toFixed(2)).join(", ")}`);
    }
  }

  // Thermal / Temperature
  const thermalVal = data.thermal || data.thermals || data.cpu?.temp || data.temp;
  if (thermalVal !== undefined && thermalVal !== null) {
    if (typeof thermalVal === "number" || typeof thermalVal === "string") {
      const badge = document.getElementById("cpu-temp-badge");
      if (badge) {
        const num = parseFloat(thermalVal);
        if (!isNaN(num)) {
          setTextIfChanged(badge, `${num.toFixed(0)}°C`);
          const isCrit = num >= 80;
          const isWarn = !isCrit && num >= 60;
          const targetClass = `status-badge ${isCrit ? "temp-badge temp-crit temp-hot" : isWarn ? "temp-badge temp-warn temp-warm" : "temp-badge temp-ok temp-cool"}`;
          if (badge.className !== targetClass) {
            badge.className = targetClass;
          }
        }
      }
    } else if (typeof updateCpuTempBadge === "function") {
      updateCpuTempBadge(thermalVal);
    }
  }

  // 2. Memory Telemetry
  const mem = data.memory || {};
  const memPercent = Number(
    mem.percent ?? data.mem_percent ?? data.memory_percent ?? (typeof data.memory === "number" ? data.memory : NaN)
  );
  if (!Number.isNaN(memPercent)) {
    liveAnimState.targetRam = memPercent;
    if (!liveAnimState.initialized) {
      liveAnimState.currentRam = memPercent;
      liveAnimState.renderedRam = memPercent;
    }
    const memBarEl = document.getElementById("memory-progress-bar");
    if (memBarEl && typeof updateProgressBar === "function") {
      updateProgressBar(memBarEl, memPercent);
    }
    if (!liveAnimState.isRunning) {
      const memValEl = document.getElementById("memory-percent-value");
      if (memValEl) setTextIfChanged(memValEl, `${memPercent.toFixed(1)}%`);
    }

    // Memory rolling historical trend chart
    memoryHistoryBuffer.push(memPercent);
    if (memoryHistoryBuffer.length > MEMORY_HISTORY_LIMIT) {
      memoryHistoryBuffer.shift();
    }
    if (typeof renderMemoryHistoryChart === "function") {
      renderMemoryHistoryChart(memoryHistoryBuffer);
    }
  }
  if (mem.used !== undefined || mem.used_gb !== undefined || mem.total !== undefined || mem.total_gb !== undefined) {
    const memDetailEl = document.getElementById("memory-detail");
    if (memDetailEl) {
      const usedStr = mem.used !== undefined ? formatBytes(mem.used) : (mem.used_gb !== undefined ? `${Number(mem.used_gb).toFixed(1)} GB` : "--");
      const totalStr = mem.total !== undefined ? formatBytes(mem.total) : (mem.total_gb !== undefined ? `${Number(mem.total_gb).toFixed(1)} GB` : "--");
      const availStr = mem.available !== undefined ? formatBytes(mem.available) : "";
      setTextIfChanged(
        memDetailEl,
        availStr
          ? `${usedStr} / ${totalStr} used (${availStr} avail)`
          : `${usedStr} / ${totalStr} used`
      );
    }
  }
  if (mem.total !== undefined || mem.total_gb !== undefined) {
    const memTotalBadge = document.getElementById("memory-total-badge");
    if (memTotalBadge) {
      setTextIfChanged(memTotalBadge, mem.total !== undefined ? `${formatBytes(mem.total)} Total` : `${Number(mem.total_gb).toFixed(1)} GB Total`);
    }
  }
  const chipUsed = document.getElementById("memory-chip-used");
  if (chipUsed && (mem.used !== undefined || mem.used_gb !== undefined)) {
    setTextIfChanged(chipUsed, `In use: ${mem.used !== undefined ? formatBytes(mem.used) : `${Number(mem.used_gb).toFixed(1)} GB`}`);
  }
  const chipAvail = document.getElementById("memory-chip-available");
  if (chipAvail && mem.available !== undefined) {
    setTextIfChanged(chipAvail, `Available: ${formatBytes(mem.available)}`);
  }
  const chipCached = document.getElementById("memory-chip-cached");
  if (chipCached && mem.cached !== undefined) {
    setTextIfChanged(chipCached, `Cached: ${formatBytes(mem.cached)}`);
  }
  const chipFree = document.getElementById("memory-chip-free");
  if (chipFree && mem.free !== undefined) {
    setTextIfChanged(chipFree, `Free: ${formatBytes(mem.free)}`);
  }

  // 3. Disk Telemetry
  const disk = data.disk || data.storage || {};
  const diskPercent = Number(
    disk.percent ?? disk.percentage ?? data.disk_percent ?? (typeof data.disk === "number" ? data.disk : NaN)
  );
  if (!Number.isNaN(diskPercent)) {
    liveAnimState.targetDisk = diskPercent;
    if (!liveAnimState.initialized) {
      liveAnimState.currentDisk = diskPercent;
      liveAnimState.renderedDisk = diskPercent;
    }
    const diskBarEl = document.getElementById("disk-progress-bar");
    if (diskBarEl && typeof updateProgressBar === "function") {
      updateProgressBar(diskBarEl, diskPercent);
    }
    if (!liveAnimState.isRunning) {
      const diskValEl = document.getElementById("disk-percent-value");
      if (diskValEl) setTextIfChanged(diskValEl, `${diskPercent.toFixed(1)}%`);
    }
  }
  if (disk.used !== undefined || disk.used_gb !== undefined || disk.total !== undefined || disk.total_gb !== undefined) {
    const diskDetailEl = document.getElementById("disk-detail");
    if (diskDetailEl) {
      const usedStr = disk.used !== undefined ? formatBytes(disk.used) : (disk.used_gb !== undefined ? `${Number(disk.used_gb).toFixed(1)} GB` : "--");
      const totalStr = disk.total !== undefined ? formatBytes(disk.total) : (disk.total_gb !== undefined ? `${Number(disk.total_gb).toFixed(1)} GB` : "--");
      const freeStr = disk.free !== undefined ? formatBytes(disk.free) : (disk.free_gb !== undefined ? `${Number(disk.free_gb).toFixed(1)} GB free` : "");
      setTextIfChanged(
        diskDetailEl,
        freeStr
          ? `${usedStr} / ${totalStr} (${freeStr} free)`
          : `${usedStr} / ${totalStr}`
      );
    }
  }

  // 4. Update Header Timestamp (throttled to at least 5 seconds during telemetry stream)
  updateHeaderLastUpdated(false);
}

if (typeof VigilLiveStore !== "undefined") {
  VigilLiveStore.subscribe("telemetry", (data) => {
    handleSSETelemetry(data);
  });
}

/**
 * Handle SSE full snapshot event.
 */
function handleSSESnapshot(data) {
  if (!data || typeof data !== "object") return;
  if (data.system) {
    renderSystemMetrics(data.system);
    setModuleLastUpdated("system");
  }
  if (data.raid) {
    renderRaidDetails(data.raid);
    setModuleLastUpdated("raid");
    const isAdaptive =
      Boolean(data.adaptive_fast) ||
      ["recovering", "resyncing", "degraded"].includes(
        String(data.raid.status || "").toLowerCase()
      );
    updateRaidAdaptiveBadge(isAdaptive);
  }
  if (data.docker) {
    renderDockerStatus(data.docker);
    setModuleLastUpdated("docker");
    if (Array.isArray(data.docker.containers || data.containers)) {
      cachedDockerContainers = data.docker.containers || data.containers;
      renderDockerContainers(null, cachedDockerContainers);
    }
    if (Array.isArray(data.docker.stacks || data.stacks)) {
      cachedDockerStacks = data.docker.stacks || data.stacks;
      renderDockerStacks(cachedDockerStacks);
    }
  }
  if (data.notifications || data.history) {
    renderNotificationHistory(data.notifications || data.history);
    setModuleLastUpdated("notifications");
  }
  if (data.notification_state) {
    renderWeeklyNotificationState(data.notification_state);
  }
  if (data.refresh_intervals) {
    syncIntervalSelects(data.refresh_intervals);
  }
  if (data.version || data.version_info) {
    updateVersionBadge(data.version_info || data.version);
  }
  updateHeaderLastUpdated(true);
}

/**
 * Handle SSE docker lifecycle event.
 */
function handleSSEDockerEvent(data) {
  if (!data || typeof data !== "object") return;
  const evt = data.event || data;
  const actionRaw = String(evt.action || evt.Action || evt.status || data.action || data.status || "").trim();
  const action = actionRaw.toLowerCase();
  const actor = evt.Actor || evt.actor || data.actor || {};
  const attrs = actor.Attributes || actor.attributes || {};
  const containerName =
    attrs.name ||
    evt.container_name ||
    evt.name ||
    data.container_name ||
    data.name ||
    "Container";
  const containerId =
    actor.ID ||
    evt.container_id ||
    evt.id ||
    data.container_id ||
    data.id ||
    "";

  patchDockerContainerRowInPlace(containerId, containerName, action);

  if (["die", "kill", "stop", "oom"].includes(action)) {
    if (typeof showToast === "function") {
      showToast({
        title: "Container Stopped",
        message: `${containerName} (${actionRaw || action})`,
        type: "danger",
      });
    }
  } else if (action === "start") {
    if (typeof showToast === "function") {
      showToast({
        title: "Container Started",
        message: `${containerName} is running`,
        type: "success",
      });
    }
  }

  if (data.docker) {
    renderDockerStatus(data.docker);
    setModuleLastUpdated("docker");
  }
}

/**
 * Handle SSE module update event.
 */
function handleSSEModuleUpdate(data) {
  if (!data || typeof data !== "object") return;
  const mod = data.module;
  if (mod === "raid" || data.raid) {
    if (data.raid) renderRaidDetails(data.raid);
    setModuleLastUpdated("raid");
    const isAdaptive =
      Boolean(data.adaptive_fast) ||
      (data.raid &&
        ["recovering", "resyncing", "degraded"].includes(
          String(data.raid.status || "").toLowerCase()
        ));
    updateRaidAdaptiveBadge(isAdaptive);
  }
  if (mod === "system" || data.system) {
    if (data.system) renderSystemMetrics(data.system);
    setModuleLastUpdated("system");
  }
  if (mod === "docker" || mod === "containers" || data.docker) {
    if (data.docker) renderDockerStatus(data.docker);
    setModuleLastUpdated("docker");
  }
  if (mod === "notifications" || data.history || data.notification_state) {
    if (data.history) renderNotificationHistory(data.history);
    if (data.notification_state) renderWeeklyNotificationState(data.notification_state);
    setModuleLastUpdated("notifications");
  }
}

/**
 * Handle SSE notification dispatch event.
 */
function handleSSENotification(data) {
  if (!data || typeof data !== "object") return;
  prependNotificationRow(data);

  const drawerBadge = document.getElementById("notif-drawer-badge");
  if (drawerBadge) {
    const count = (parseInt(drawerBadge.textContent, 10) || 0) + 1;
    drawerBadge.textContent = String(count);
    drawerBadge.classList.remove("hidden");
  }
  setModuleLastUpdated("notifications");
}

/**
 * Initialize client-side Server-Sent Events (SSE) connection to /api/events.
 * Provides fine-grained DOM patching, telemetry streaming, and adaptive polling backoff.
 */
function initSSEStream() {
  if (typeof window === "undefined" || typeof window.EventSource !== "function") {
    console.info("EventSource is not supported in this environment; falling back to interval polling.");
    return;
  }
  if (!isAuthenticated) {
    return;
  }

  closeSSEStream();

  const authToken = getStoredToken();
  const query = authToken ? `?token=${encodeURIComponent(authToken)}` : "";
  const sseUrl = `/api/events${query}`;

  try {
    sseSource = new window.EventSource(sseUrl);
  } catch (err) {
    console.warn("Failed to instantiate EventSource:", err);
    sseConnected = false;
    updateConnectionIndicator("reconnecting");
    adjustAdaptivePolling(false);
    return;
  }

  sseSource.onopen = () => {
    sseConnected = true;
    sseRetryCount = 0;
    if (typeof VigilLiveStore !== "undefined") {
      VigilLiveStore.dispatch("connection", "live");
    }
    updateConnectionIndicator("live");
    adjustAdaptivePolling(true);
  };

  sseSource.onerror = () => {
    sseConnected = false;
    sseRetryCount += 1;
    if (typeof VigilLiveStore !== "undefined") {
      VigilLiveStore.dispatch("connection", "reconnecting");
    }
    updateConnectionIndicator("reconnecting");
    adjustAdaptivePolling(false);
  };

  sseSource.addEventListener("snapshot", (e) => {
    try {
      const data = typeof e.data === "string" ? JSON.parse(e.data) : e.data;
      if (!data || typeof data !== "object") return;
      if (typeof VigilLiveStore !== "undefined") {
        VigilLiveStore.dispatch("snapshot", data);
        if (data.system) VigilLiveStore.dispatch("system", data.system);
        if (data.docker) VigilLiveStore.dispatch("docker", data.docker);
        if (data.raid) VigilLiveStore.dispatch("raid", data.raid);
        if (data.notifications || data.history) VigilLiveStore.dispatch("notifications", data.notifications || data.history);
        if (data.telemetry) VigilLiveStore.dispatch("telemetry", data.telemetry);
      }
      handleSSESnapshot(data);
    } catch (err) {
      console.warn("Failed to parse SSE snapshot payload:", err);
    }
  });

  sseSource.addEventListener("telemetry", (e) => {
    try {
      const data = typeof e.data === "string" ? JSON.parse(e.data) : e.data;
      if (!data || typeof data !== "object") return;
      if (typeof VigilLiveStore !== "undefined") {
        VigilLiveStore.dispatch("telemetry", data);
      } else {
        handleSSETelemetry(data);
      }
    } catch (err) {
      console.warn("Failed to parse SSE telemetry payload:", err);
    }
  });

  sseSource.addEventListener("docker_event", (e) => {
    try {
      const data = typeof e.data === "string" ? JSON.parse(e.data) : e.data;
      if (!data || typeof data !== "object") return;
      if (typeof VigilLiveStore !== "undefined") {
        VigilLiveStore.dispatch("docker", data.docker || data);
      }
      handleSSEDockerEvent(data);
    } catch (err) {
      console.warn("Failed to parse SSE docker_event payload:", err);
    }
  });

  sseSource.addEventListener("module_update", (e) => {
    try {
      const data = typeof e.data === "string" ? JSON.parse(e.data) : e.data;
      if (!data || typeof data !== "object") return;
      if (typeof VigilLiveStore !== "undefined") {
        if (data.module) VigilLiveStore.dispatch(data.module, data);
        if (data.module === "raid" || data.raid) VigilLiveStore.dispatch("raid", data.raid || data);
        if (data.module === "system" || data.system) VigilLiveStore.dispatch("system", data.system || data);
        if (data.module === "docker" || data.module === "containers" || data.docker) VigilLiveStore.dispatch("docker", data.docker || data);
        if (data.module === "notifications" || data.history) VigilLiveStore.dispatch("notifications", data.history || data);
      }
      handleSSEModuleUpdate(data);
    } catch (err) {
      console.warn("Failed to parse SSE module_update payload:", err);
    }
  });

  sseSource.addEventListener("notification", (e) => {
    try {
      const data = typeof e.data === "string" ? JSON.parse(e.data) : e.data;
      if (!data || typeof data !== "object") return;
      if (typeof VigilLiveStore !== "undefined") {
        VigilLiveStore.dispatch("notifications", data);
      }
      handleSSENotification(data);
    } catch (err) {
      console.warn("Failed to parse SSE notification payload:", err);
    }
  });

  sseSource.addEventListener("ping", () => {
    const lastUpdatedEl = document.getElementById("last-updated");
    if (lastUpdatedEl && (!lastUpdatedEl.textContent || lastUpdatedEl.textContent === "Never updated")) {
      updateHeaderLastUpdated(true);
    }
  });
}

const initSSEConnection = initSSEStream;

/**
 * Set the last-updated label for a specific module and stop button loading state.
 */
function setModuleLastUpdated(moduleName) {
  const el = document.getElementById(`${moduleName}-last-updated`);
  if (el) {
    setTextIfChanged(el, `Updated ${formatTimeOnly(new Date())}`);
  }
  const btn = document.getElementById(`refresh-${moduleName}-btn`);
  if (btn && btn.classList.contains("loading")) {
    btn.classList.remove("loading");
  }
}

/**
 * Toggle the RAID rebuild boost indicator badge.
 */
function updateRaidAdaptiveBadge(isAdaptive) {
  const badge = document.getElementById("raid-adaptive-badge");
  if (badge) {
    badge.classList.toggle("hidden", !isAdaptive);
  }
}

/**
 * Sync the per-module interval <select> dropdowns from server settings.
 */
function syncIntervalSelects(intervals) {
  if (!intervals || typeof intervals !== "object") return;
  const sysEl = document.getElementById("system-interval-select");
  const raidEl = document.getElementById("raid-interval-select");
  const notifEl = document.getElementById("notifications-interval-select");
  const dockerEl = document.getElementById("docker-interval-select");

  if (sysEl && intervals.system !== undefined && intervals.system !== null) {
    sysEl.value = String(intervals.system);
  }
  if (raidEl && intervals.raid !== undefined && intervals.raid !== null) {
    raidEl.value = String(intervals.raid);
  }
  if (
    notifEl &&
    intervals.notifications !== undefined &&
    intervals.notifications !== null
  ) {
    notifEl.value = String(intervals.notifications);
  }
  if (
    dockerEl &&
    intervals.docker !== undefined &&
    intervals.docker !== null
  ) {
    dockerEl.value = String(intervals.docker);
  }
}

/**
 * Send updated refresh intervals over WebSocket or fall back to /api/settings.
 */
async function handleIntervalChange() {
  const sysVal = Number(
    document.getElementById("system-interval-select")?.value ?? 1
  );
  const raidVal = Number(
    document.getElementById("raid-interval-select")?.value ?? 15
  );
  const notifVal = Number(
    document.getElementById("notifications-interval-select")?.value ?? 60
  );
  const dockerVal = Number(
    document.getElementById("docker-interval-select")?.value ?? 5
  );

  const intervals = {
    system: sysVal,
    raid: raidVal,
    notifications: notifVal,
    docker: dockerVal,
  };

  if (
    wsConnection &&
    typeof window.WebSocket === "function" &&
    wsConnection.readyState === window.WebSocket.OPEN
  ) {
    try {
      wsConnection.send(
        JSON.stringify({ action: "set_intervals", intervals })
      );
      return;
    } catch (err) {
      console.error("Failed to send set_intervals over WebSocket:", err);
    }
  }

  // Fall back to POST /api/settings if WebSocket is closed
  try {
    await apiFetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        refresh_system_sec: sysVal,
        refresh_raid_sec: raidVal,
        refresh_notifications_sec: notifVal,
        refresh_docker_sec: dockerVal,
      }),
    });
  } catch (err) {
    console.error("Failed to save intervals via /api/settings:", err);
  }
}

/**
 * Request an instant refresh for a specific module over WebSocket or HTTP.
 */
function requestModuleRefresh(moduleName) {
  const btn = document.getElementById(`refresh-${moduleName}-btn`);
  if (btn) btn.classList.add("loading");

  if (
    wsConnection &&
    typeof window.WebSocket === "function" &&
    wsConnection.readyState === window.WebSocket.OPEN
  ) {
    try {
      wsConnection.send(
        JSON.stringify({ action: "refresh_module", module: moduleName })
      );
      if (moduleName === "docker" && currentViewTab === "containers") {
        refreshDockerSubtab(currentDockerSubtab);
      }
      return;
    } catch {
      // Fall back below
    }
  }

  if (moduleName === "docker") {
    fetchDockerStatus();
    if (currentViewTab === "containers") {
      refreshDockerSubtab(currentDockerSubtab);
    }
  } else {
    refreshDashboard();
  }
}

/**
 * Request instant Docker module refresh over WebSocket or HTTP.
 */
function requestDockerRefresh() {
  requestModuleRefresh("docker");
}

/**
 * 1-second UI ticker to keep uptime and weekly countdown ticking smoothly.
 */
function updateLiveTickers() {
  if (currentBootTime) {
    const uptimeValEl = document.getElementById("uptime-value");
    if (uptimeValEl) {
      uptimeValEl.textContent = formatUptimeFromBoot(currentBootTime);
    }
  }
  if (currentWeeklyDueAt !== null && currentWeeklyDueAt !== undefined) {
    const countdownEl = document.getElementById("weekly-countdown");
    if (countdownEl) {
      const nowSec = Date.now() / 1000;
      const remaining = Math.floor(currentWeeklyDueAt - nowSec);
      if (!Number.isFinite(currentWeeklyDueAt) || remaining <= 0) {
        countdownEl.textContent = "Due on the next clean/active RAID check";
      } else {
        const days = Math.floor(remaining / 86400);
        const hours = Math.floor((remaining % 86400) / 3600);
        const minutes = Math.floor((remaining % 3600) / 60);
        countdownEl.textContent = `${days}d ${hours}h ${minutes}m remaining`;
      }
    }
  }
}

/**
 * Show a subtle, non-intrusive toast notification.
 */
function showToast(options, legacyType = "info") {
  const container = document.getElementById("toast-container");
  if (!container) return;

  let title = "Notification";
  let message = "";
  let type = "info";
  let duration = 4500;

  if (typeof options === "string") {
    message = options;
    type = legacyType === "error" ? "danger" : legacyType;
    title = type === "danger" ? "Error" : (type === "success" ? "Success" : "Notice");
  } else if (options && typeof options === "object") {
    title = options.title || "Notification";
    message = options.message || "";
    type = options.type || "info";
    duration = options.duration || 4500;
  }

  while (container.children.length >= 4) {
    const oldest = container.firstChild;
    if (oldest) oldest.remove();
  }

  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.setAttribute("role", "alert");

  let iconSvg = "";
  if (type === "success") {
    iconSvg = `<svg viewBox="0 0 24 24" class="toast-icon" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>`;
  } else if (type === "danger") {
    iconSvg = `<svg viewBox="0 0 24 24" class="toast-icon" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>`;
  } else if (type === "warning") {
    iconSvg = `<svg viewBox="0 0 24 24" class="toast-icon" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>`;
  } else {
    iconSvg = `<svg viewBox="0 0 24 24" class="toast-icon" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>`;
  }

  const safeTitle = escapeHtml(title || "Docker Event");
  const safeMessage = escapeHtml(message || "");

  toast.innerHTML = `
    ${iconSvg}
    <div class="toast-content">
      <div class="toast-title">${safeTitle}</div>
      <div class="toast-message">${safeMessage}</div>
    </div>
    <button type="button" class="toast-close" aria-label="Dismiss notification">&times;</button>
  `;

  const closeBtn = toast.querySelector(".toast-close");
  const dismiss = () => {
    toast.classList.remove("toast-visible");
    toast.classList.add("toast-hiding");
    window.setTimeout(() => {
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }, 260);
  };

  if (closeBtn) closeBtn.addEventListener("click", dismiss);

  container.appendChild(toast);

  requestAnimationFrame(() => {
    toast.classList.add("toast-visible");
  });

  if (duration > 0) {
    window.setTimeout(dismiss, duration);
  }
}

/**
 * Trigger visual pulse on Docker container table and highlight specific container row.
 */
function pulseContainerTable(targetId) {
  const table = document.getElementById("docker-containers-table");
  if (table) {
    table.classList.remove("table-pulse");
    void table.offsetWidth; // force reflow
    table.classList.add("table-pulse");
    window.setTimeout(() => {
      table.classList.remove("table-pulse");
    }, 1000);
  }

  if (targetId) {
    const tbody = document.getElementById("docker-containers-tbody");
    if (tbody) {
      const rows = tbody.querySelectorAll("tr");
      rows.forEach((row) => {
        if (row.textContent.includes(targetId)) {
          row.classList.remove("row-highlight");
          void row.offsetWidth;
          row.classList.add("row-highlight");
          window.setTimeout(() => {
            row.classList.remove("row-highlight");
          }, 2000);
        }
      });
    }
  }
}

/**
 * Handle incoming real-time Docker events stream.
 */
function handleDockerEvent(msg) {
  if (!msg || typeof msg !== "object") return;

  const event = msg.event || msg;
  const actionRaw = String(
    event.action || event.Action || event.status || msg.action || msg.status || ""
  ).trim();
  const action = actionRaw.toLowerCase();

  const actor = event.Actor || event.actor || msg.actor || {};
  const attrs = actor.Attributes || actor.attributes || {};
  const containerName =
    attrs.name ||
    event.container_name ||
    event.container ||
    event.name ||
    msg.container_name ||
    msg.name ||
    "Container";
  const containerId =
    actor.ID ||
    event.container_id ||
    event.id ||
    msg.container_id ||
    msg.id ||
    "";

  let toastType = "info";
  let title = `Container Event`;
  let message = `${containerName}: ${actionRaw || "state updated"}`;

  if (action === "die" || action === "kill" || action === "stop") {
    toastType = "danger";
    title = `Container Stopped`;
    message = `${containerName} (${actionRaw})`;
  } else if (action === "start") {
    toastType = "success";
    title = `Container Started`;
    message = `${containerName} is now running`;
  } else if (action === "restart") {
    toastType = "warning";
    title = `Container Restarted`;
    message = `${containerName} was restarted`;
  } else if (action.includes("unhealthy")) {
    toastType = "danger";
    title = `Container Unhealthy`;
    message = `${containerName} became unhealthy!`;
  } else if (action.includes("healthy")) {
    toastType = "success";
    title = `Container Healthy`;
    message = `${containerName} reported healthy`;
  } else if (action === "destroy" || action === "delete") {
    toastType = "warning";
    title = `Container Removed`;
    message = `${containerName} was deleted`;
  } else if (action === "create") {
    toastType = "info";
    title = `Container Created`;
    message = `${containerName} was created`;
  }

  showToast({ title, message, type: toastType });

  pulseContainerTable(containerId);

  // Invalidate containers render cache & refresh containers table in real-time
  lastContainersRenderKey = null;
  if (typeof fetchDockerContainers === "function") {
    fetchDockerContainers(containerId);
  }
  if (typeof fetchDockerStatus === "function") {
    fetchDockerStatus();
  }
}

/**
 * Send active view & subtab subscription to backend WebSocket multiplexer.
 */
function sendWsSubscription(view = currentViewTab, subtab = currentDockerSubtab) {
  if (
    wsConnection &&
    typeof window.WebSocket === "function" &&
    wsConnection.readyState === window.WebSocket.OPEN
  ) {
    try {
      wsConnection.send(
        JSON.stringify({
          action: "subscribe",
          view: view || "overview",
          subtab: subtab || "stacks",
        })
      );
    } catch (err) {
      console.debug("sendWsSubscription error:", err);
    }
  }
}

/**
 * Subscribe or unsubscribe live container log streaming over WebSocket.
 */
function sendWsLogSubscription(containerId) {
  if (
    wsConnection &&
    typeof window.WebSocket === "function" &&
    wsConnection.readyState === window.WebSocket.OPEN
  ) {
    try {
      wsConnection.send(
        JSON.stringify({
          action: "subscribe_logs",
          container_id: containerId || null,
        })
      );
    } catch (err) {
      console.debug("sendWsLogSubscription error:", err);
    }
  }
}

/**
 * Process incoming JSON messages from /api/ws ('snapshot', 'module_update', or 'container_logs').
 */
function handleWebSocketMessage(rawData) {
  let msg;
  try {
    msg = typeof rawData === "string" ? JSON.parse(rawData) : rawData;
  } catch (err) {
    console.error("Invalid WebSocket JSON payload:", err);
    return;
  }
  if (!msg || typeof msg !== "object") return;

  const refreshBtn = document.getElementById("refresh-btn");
  if (refreshBtn) refreshBtn.classList.remove("loading");

  if (msg.type === "container_logs") {
    if (activeLogContainerId && msg.container_id === activeLogContainerId) {
      const viewer = document.getElementById("container-logs-viewer");
      if (viewer) {
        const isScrolledToBottom =
          viewer.scrollHeight - viewer.clientHeight <= viewer.scrollTop + 50;
        viewer.textContent = msg.logs || "No logs available.";
        if (isScrolledToBottom) {
          viewer.scrollTop = viewer.scrollHeight;
        }
      }
    }
    return;
  }

  if (msg.type === "intervals_updated" || msg.refresh_intervals) {
    syncIntervalSelects(msg.refresh_intervals);
  }

  // Real-time Docker Events Toast & Live Table Update
  if (msg.type === "docker_event" || msg.docker_event) {
    handleDockerEvent(msg.docker_event || msg);
    return;
  }
  if (
    (msg.module === "docker" || msg.module === "containers") &&
    (msg.action || msg.event || msg.type === "event")
  ) {
    handleDockerEvent(msg);
    return;
  }

  if (
    msg.type === "telemetry" ||
    msg.type === "telemetry_sample" ||
    msg.telemetry ||
    msg.sample
  ) {
    const telem = msg.telemetry || msg.sample || msg;
    if (typeof VigilLiveStore !== "undefined") {
      VigilLiveStore.dispatch("telemetry", telem);
    } else {
      handleSSETelemetry(telem);
    }
    return;
  }

  if (msg.type === "module_update") {
    if (typeof VigilLiveStore !== "undefined" && msg.module) {
      VigilLiveStore.dispatch(msg.module, msg);
    }
    if (msg.system || msg.module === "system") {
      if (typeof VigilLiveStore !== "undefined") VigilLiveStore.dispatch("system", msg.system || msg);
      renderSystemMetrics(msg.system);
      setModuleLastUpdated("system");
    }
    if (msg.raid || msg.module === "raid") {
      if (typeof VigilLiveStore !== "undefined") VigilLiveStore.dispatch("raid", msg.raid || msg);
      renderRaidDetails(msg.raid);
      setModuleLastUpdated("raid");
      const isAdaptive =
        Boolean(msg.adaptive_fast) ||
        (msg.raid &&
          ["recovering", "resyncing", "degraded"].includes(
            String(msg.raid.status || "").toLowerCase()
          ));
      updateRaidAdaptiveBadge(isAdaptive);
    }
    if (
      "notification_state" in msg ||
      msg.history ||
      msg.module === "notifications"
    ) {
      if (typeof VigilLiveStore !== "undefined") VigilLiveStore.dispatch("notifications", msg.history || msg);
      if ("notification_state" in msg) {
        renderWeeklyNotificationState(msg.notification_state ?? null);
      }
      if (msg.history) {
        renderNotificationHistory(msg.history);
      }
      setModuleLastUpdated("notifications");
    }
    if (msg.version || msg.version_info) {
      updateVersionBadge(msg.version_info || msg.version);
    }
    if (msg.docker || msg.module === "docker" || msg.module === "containers") {
      if (typeof VigilLiveStore !== "undefined") VigilLiveStore.dispatch("docker", msg.docker || msg);
      if (msg.event || msg.action) {
        handleDockerEvent(msg);
      }
      renderDockerStatus(msg.docker || msg);
      setModuleLastUpdated("docker");
      if (Array.isArray(msg.containers)) {
        cachedDockerContainers = msg.containers;
        renderDockerContainers(null, msg.containers);
      }
      if (Array.isArray(msg.stacks)) {
        cachedDockerStacks = msg.stacks;
        renderDockerStacks(msg.stacks);
      }
      if (
        currentViewTab === "containers" &&
        !Array.isArray(msg.containers) &&
        !Array.isArray(msg.stacks)
      ) {
        throttledRefreshDockerSubtab(currentDockerSubtab);
      }
    }
    if (msg.services || msg.module === "services") {
      const svcPayload = Array.isArray(msg.services) ? msg : (msg.services || msg);
      cachedServicesData = svcPayload;
      renderSystemServices(msg.services || msg);
    }
    if (msg.processes || msg.module === "processes") {
      const procPayload = Array.isArray(msg.processes) ? msg : (msg.processes || msg);
      cachedProcessesData = procPayload;
      renderProcesses(msg.processes || msg);
    }
    if (msg.users || msg.module === "users") {
      const usersPayload = Array.isArray(msg.users) ? msg : (msg.users || msg);
      cachedUsersData = usersPayload;
      renderSystemUsers(usersPayload);
      if (msg.groups) {
        const groupsPayload = Array.isArray(msg.groups)
          ? { groups: msg.groups, summary: msg.groups_summary || msg.summary }
          : msg.groups;
        cachedGroupsData = groupsPayload;
        renderSystemGroups(groupsPayload);
      }
    }
    if (msg.module === "audit" && msg.logs) {
      renderAuditLogs(msg);
    }
    if (msg.security || msg.module === "security") {
      const sec = msg.security || msg;
      const portsPayload = Array.isArray(sec.ports)
        ? { ports: sec.ports, summary: sec.summary }
        : (sec.ports || sec);
      const fwPayload = sec.firewall || sec;
      cachedSecurityPortsData = portsPayload;
      cachedSecurityFirewallData = fwPayload;
      renderNetworkSecurity(portsPayload, fwPayload);
      if (sec.ssh || sec.ssh_security) {
        cachedSecuritySshData = sec.ssh || sec.ssh_security;
        renderSshSecurity(sec.ssh || sec.ssh_security);
      }
    }
    if (msg.backups || msg.cron_jobs || msg.module === "backups") {
      if (msg.backups) {
        cachedBackupsData = msg.backups;
        renderBackups(msg.backups);
      }
      if (msg.cron_jobs || msg.cron_runs) {
        const cronPayload = Array.isArray(msg.cron_jobs)
          ? {
              jobs: msg.cron_jobs,
              recent_runs: msg.cron_runs || cachedCronData?.recent_runs || [],
              supported_task_types: cachedCronData?.supported_task_types || [],
            }
          : msg.cron_jobs && typeof msg.cron_jobs === "object"
            ? {
                ...msg.cron_jobs,
                recent_runs: msg.cron_runs || msg.cron_jobs.recent_runs || [],
              }
            : {
                ...(cachedCronData || {}),
                recent_runs: msg.cron_runs || [],
              };
        cachedCronData = cronPayload;
        renderCronJobs(cronPayload);
      }
      if (typeof updateBackupsNavBadge === "function") {
        updateBackupsNavBadge();
      }
    }
    if (msg.probes || msg.module === "probes" || msg.type === "probe_update") {
      if (msg.probes) {
        const probesPayload = Array.isArray(msg.probes)
          ? { probes: msg.probes }
          : msg.probes;
        cachedProbesData = probesPayload;
        renderProbes(probesPayload);
      } else if (typeof fetchProbes === "function") {
        fetchProbes();
      }
    }
  } else {
    // 'snapshot' or full payload
    if (typeof VigilLiveStore !== "undefined") {
      VigilLiveStore.dispatch("snapshot", msg);
      if (msg.system) VigilLiveStore.dispatch("system", msg.system);
      if (msg.raid) VigilLiveStore.dispatch("raid", msg.raid);
      if (msg.docker) VigilLiveStore.dispatch("docker", msg.docker);
      if (msg.notifications || msg.history) VigilLiveStore.dispatch("notifications", msg.notifications || msg.history);
      if (msg.telemetry) VigilLiveStore.dispatch("telemetry", msg.telemetry);
    }
    if (msg.refresh_intervals) {
      syncIntervalSelects(msg.refresh_intervals);
    }
    if (msg.raid) {
      renderRaidDetails(msg.raid);
      setModuleLastUpdated("raid");
      const isAdaptive =
        Boolean(msg.adaptive_fast) ||
        ["recovering", "resyncing", "degraded"].includes(
          String(msg.raid.status || "").toLowerCase()
        );
      updateRaidAdaptiveBadge(isAdaptive);
    }
    if (msg.system) {
      renderSystemMetrics(msg.system);
      setModuleLastUpdated("system");
    }
    if ("notification_state" in msg) {
      renderWeeklyNotificationState(msg.notification_state ?? null);
      setModuleLastUpdated("notifications");
    }
    if (msg.history) {
      renderNotificationHistory(msg.history);
      setModuleLastUpdated("notifications");
    }
    if (msg.version || msg.version_info) {
      updateVersionBadge(msg.version_info || msg.version);
    }
    if (msg.docker || msg.module === "docker" || msg.module === "containers") {
      renderDockerStatus(msg.docker || msg);
      setModuleLastUpdated("docker");
      if (Array.isArray(msg.containers)) {
        cachedDockerContainers = msg.containers;
        renderDockerContainers(null, msg.containers);
      }
      if (Array.isArray(msg.stacks)) {
        cachedDockerStacks = msg.stacks;
        renderDockerStacks(msg.stacks);
      }
      if (
        currentViewTab === "containers" &&
        !Array.isArray(msg.containers) &&
        !Array.isArray(msg.stacks)
      ) {
        throttledRefreshDockerSubtab(currentDockerSubtab);
      }
    }
    if (msg.services || msg.module === "services") {
      renderSystemServices(msg.services || msg);
    }
    if (msg.processes || msg.module === "processes") {
      renderProcesses(msg.processes || msg);
    }
    if (msg.users || msg.module === "users") {
      const usersPayload = Array.isArray(msg.users) ? msg : (msg.users || msg);
      renderSystemUsers(usersPayload);
      if (msg.groups) {
        renderSystemGroups(
          Array.isArray(msg.groups)
            ? { groups: msg.groups, summary: msg.groups_summary || msg.summary }
            : msg.groups
        );
      }
    }
    if (msg.module === "audit" && msg.logs) {
      renderAuditLogs(msg);
    }
    if (msg.security || msg.module === "security") {
      const sec = msg.security || msg;
      const portsPayload = Array.isArray(sec.ports)
        ? { ports: sec.ports, summary: sec.summary }
        : (sec.ports || sec);
      renderNetworkSecurity(portsPayload, sec.firewall || sec);
      if (sec.ssh || sec.ssh_security) {
        renderSshSecurity(sec.ssh || sec.ssh_security);
      }
    }
    if (msg.backups || msg.cron_jobs || msg.module === "backups") {
      if (msg.backups) {
        renderBackups(msg.backups);
      }
      if (msg.cron_jobs || msg.cron_runs) {
        const cronPayload = Array.isArray(msg.cron_jobs)
          ? {
              jobs: msg.cron_jobs,
              recent_runs: msg.cron_runs || cachedCronData?.recent_runs || [],
              supported_task_types: cachedCronData?.supported_task_types || [],
            }
          : msg.cron_jobs && typeof msg.cron_jobs === "object"
            ? {
                ...msg.cron_jobs,
                recent_runs: msg.cron_runs || msg.cron_jobs.recent_runs || [],
              }
            : {
                ...(cachedCronData || {}),
                recent_runs: msg.cron_runs || [],
              };
        renderCronJobs(cronPayload);
      }
    }
    if (msg.probes || msg.module === "probes" || msg.type === "probe_update") {
      if (msg.probes) {
        renderProbes(
          Array.isArray(msg.probes) ? { probes: msg.probes } : msg.probes
        );
      } else if (typeof fetchProbes === "function") {
        fetchProbes();
      }
    }
  }

  setErrorState(null);
  updateHeaderLastUpdated(true);
}

/**
 * Connect to ws:// (or wss://) + window.location.host + '/api/ws' for real-time updates.
 */
function connectWebSocket() {
  if (!isAuthenticated || typeof window.WebSocket !== "function") return;

  closeWebSocket();

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const authToken = getStoredToken();
  const query = authToken ? `?token=${encodeURIComponent(authToken)}` : "";
  const wsUrl = `${protocol}//${window.location.host}/api/ws${query}`;

  try {
    wsConnection = new window.WebSocket(wsUrl);
  } catch (err) {
    console.error("WebSocket creation error:", err);
    updateConnectionIndicator("reconnecting");
    startPolling({ immediate: false });
    wsReconnectTimerId = window.setTimeout(
      connectWebSocket,
      WS_RECONNECT_DELAY_MS
    );
    return;
  }

  wsConnection.onopen = () => {
    updateConnectionIndicator("live");
    if (typeof VigilLiveStore !== "undefined") {
      VigilLiveStore.dispatch("connection", "live");
    }
    // Pause the 10s HTTP polling timer while WebSocket is open
    stopPolling();
    sendWsSubscription();
    if (activeLogContainerId) {
      sendWsLogSubscription(activeLogContainerId);
      if (logsTimerId) {
        clearInterval(logsTimerId);
        logsTimerId = null;
      }
    }
  };

  wsConnection.onmessage = (event) => {
    handleWebSocketMessage(event.data);
  };

  wsConnection.onerror = () => {
    // Let onclose handle reconnection and fallback polling
  };

  wsConnection.onclose = (event) => {
    wsConnection = null;
    if (typeof VigilLiveStore !== "undefined") {
      VigilLiveStore.dispatch("connection", "reconnecting");
    }
    if (event && (event.code === 4401 || event.code === 1008)) {
      handleUnauthorized();
      return;
    }
    if (!isAuthenticated) {
      return;
    }
    updateConnectionIndicator("reconnecting");
    startPolling({ immediate: false });
    wsReconnectTimerId = window.setTimeout(
      connectWebSocket,
      WS_RECONNECT_DELAY_MS
    );
  };
}

/**
 * Trigger an immediate refresh over WebSocket when connected, or fall back to HTTP.
 */
function handleManualRefresh() {
  if (
    wsConnection &&
    typeof window.WebSocket === "function" &&
    wsConnection.readyState === window.WebSocket.OPEN
  ) {
    const refreshBtn = document.getElementById("refresh-btn");
    if (refreshBtn) refreshBtn.classList.add("loading");
    try {
      wsConnection.send(JSON.stringify({ action: "refresh" }));
      return;
    } catch {
      // Fall back to HTTP refresh below if send fails
    }
  }
  refreshDashboard({ manual: true });
  if (currentViewTab === "containers") {
    fetchDockerStatus();
    refreshDockerSubtab(currentDockerSubtab);
  }
}

/**
 * Initialize Google Identity Services button when google_client_id is configured.
 */
function setupGoogleSignIn(clientId) {
  const section = document.getElementById("google-auth-section");
  const btnContainer = document.getElementById("google-signin-btn");
  if (!section || !btnContainer) return;

  if (!clientId) {
    section.classList.add("hidden");
    return;
  }

  section.classList.remove("hidden");
  currentGoogleClientId = clientId;

  const renderGsi = () => {
    if (!window.google || !window.google.accounts || !window.google.accounts.id) {
      return false;
    }
    btnContainer.innerHTML = "";
    window.google.accounts.id.initialize({
      client_id: clientId,
      callback: handleGoogleCredentialResponse,
    });
    window.google.accounts.id.renderButton(btnContainer, {
      theme: "filled_black",
      size: "large",
      width: 340,
      text: "signin_with",
    });
    return true;
  };

  if (!renderGsi()) {
    let attempts = 0;
    const waitTimer = window.setInterval(() => {
      attempts += 1;
      if (renderGsi() || attempts > 25) {
        window.clearInterval(waitTimer);
      }
    }, 200);
  }
}

async function handleGoogleCredentialResponse(response) {
  if (!response || !response.credential) return;
  showAuthError(null);
  try {
    const resp = await apiFetch("/api/auth/google", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ credential: response.credential }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || "Google authentication failed");
    }
    if (data.token) {
      setStoredToken(data.token);
    }
    onAuthenticatedSuccess(data.username || "google-user");
  } catch (err) {
    showAuthError(err instanceof Error ? err.message : String(err));
  }
}

function openAuthOverlay({ setupRequired = false, googleClientId = "" } = {}) {
  isAuthenticated = false;
  closeWebSocket();
  closeSSEStream();
  stopPolling();
  stopSystemUpdatePolling();

  const viewLogin = document.getElementById("view-login");
  const appShell = document.getElementById("app-shell");
  const overlay = document.getElementById("auth-overlay");
  const titleEl = document.getElementById("auth-modal-title");
  const subtitleEl = document.getElementById("auth-modal-subtitle");
  const setupForm = document.getElementById("auth-setup-form");
  const loginForm = document.getElementById("auth-login-form");
  const userBox = document.getElementById("user-session-box");

  if (appShell) appShell.classList.add("hidden");
  if (viewLogin) viewLogin.classList.remove("hidden");
  if (overlay) overlay.classList.remove("hidden");
  if (userBox) userBox.classList.add("hidden");
  showAuthError(null);

  if (setupRequired) {
    if (titleEl) titleEl.textContent = "Initial Admin Setup";
    if (subtitleEl) {
      subtitleEl.textContent =
        "Create the primary administrator account to secure your dashboard";
    }
    if (setupForm) setupForm.classList.remove("hidden");
    if (loginForm) loginForm.classList.add("hidden");
  } else {
    if (titleEl) titleEl.textContent = "Sign In";
    if (subtitleEl) {
      subtitleEl.textContent =
        "Authenticate to access SystemAlerts Server Manager";
    }
    if (setupForm) setupForm.classList.add("hidden");
    if (loginForm) loginForm.classList.remove("hidden");
    setupGoogleSignIn(googleClientId);
  }
}

function onAuthenticatedSuccess(username) {
  isAuthenticated = true;
  const viewLogin = document.getElementById("view-login");
  const appShell = document.getElementById("app-shell");
  const overlay = document.getElementById("auth-overlay");
  const userBox = document.getElementById("user-session-box");
  const usernameEl = document.getElementById("auth-username");

  if (viewLogin) viewLogin.classList.add("hidden");
  if (overlay) overlay.classList.add("hidden");
  if (appShell) appShell.classList.remove("hidden");
  if (usernameEl) usernameEl.textContent = username || "admin";
  if (userBox) userBox.classList.remove("hidden");

  startPolling();
  connectWebSocket();
  initSSEStream();
  fetchDiscordSettings();
  fetchNotificationCenterConfig();
  checkForUpdates({ silent: true });
  fetchDockerUpdatesConfig();
  // Preload container updates, package updates, and installed packages in background
  fetchDockerUpdates({ background: true });
  fetchPackageUpdates();
  fetchInstalledPackages("", 1);
  fetchSystemUsers();
  fetchSystemGroups();
  fetchProbes();
  fetchSmartDisks();
  fetchHardwareThermals();
  fetchNetworkSecurity();
  refreshBackupsAndCron();
  updateBackupsNavBadge();
  if (currentViewTab === "backups") {
    refreshBackupsAndCron();
  }
  startSystemUpdatePolling();
}

async function handleUnauthorized() {
  setStoredToken("");
  await checkAuthStatus();
}

/**
 * Check GET /api/auth/status on startup and whenever 401 occurs.
 */
async function checkAuthStatus() {
  try {
    const resp = await apiFetch("/api/auth/status");
    if (!resp.ok) {
      throw new Error(`Auth check failed with HTTP ${resp.status}`);
    }
    const data = await resp.json();

    if (data.setup_required) {
      openAuthOverlay({
        setupRequired: true,
        googleClientId: data.google_client_id || "",
      });
      return;
    }

    if (!data.authenticated) {
      openAuthOverlay({
        setupRequired: false,
        googleClientId: data.google_client_id || "",
      });
      return;
    }

    onAuthenticatedSuccess(data.username || "admin");
  } catch (err) {
    console.error("Failed to check auth status:", err);
    openAuthOverlay({ setupRequired: false });
    showAuthError("Unable to verify authentication status with server.");
  }
}

async function handleSetupSubmit(event) {
  event.preventDefault();
  showAuthError(null);

  const username = document.getElementById("setup-username")?.value.trim() || "";
  const password = document.getElementById("setup-password")?.value || "";
  const email = document.getElementById("setup-email")?.value.trim() || "";

  if (!username || !password) {
    showAuthError("Username and password are required.");
    return;
  }

  try {
    const resp = await apiFetch("/api/auth/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, email }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || "Initial admin setup failed");
    }
    if (data.token) {
      setStoredToken(data.token);
    }
    onAuthenticatedSuccess(data.username || username);
  } catch (err) {
    showAuthError(err instanceof Error ? err.message : String(err));
  }
}

async function handleLoginSubmit(event) {
  event.preventDefault();
  showAuthError(null);

  const username = document.getElementById("login-username")?.value.trim() || "";
  const password = document.getElementById("login-password")?.value || "";

  if (!username || !password) {
    showAuthError("Please enter both username and password.");
    return;
  }

  try {
    const resp = await apiFetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || "Invalid username or password");
    }
    if (data.token) {
      setStoredToken(data.token);
    }
    onAuthenticatedSuccess(data.username || username);
  } catch (err) {
    showAuthError(err instanceof Error ? err.message : String(err));
  }
}

async function handleLogout() {
  closeWebSocket();
  closeSSEStream();
  try {
    await apiFetch("/api/auth/logout", { method: "POST" });
  } catch {
    // Ignore network errors during logout
  }
  setStoredToken("");
  await checkAuthStatus();
}

/* =========================================================================
 * 2. Release-Tag Updater & Modal (#nav-update-notice-btn, #update-modal)
 * ========================================================================= */

let latestUpdateInfo = null;
let isViewingFullChangelog = false;

/**
 * Format inline markdown: bold (**text**) and code (`code`).
 */
function formatInlineMarkdown(text) {
  let escaped = escapeHtml(text);
  escaped = escaped.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  escaped = escaped.replace(/`([^`]+)`/g, "<code>$1</code>");
  return escaped;
}

/**
 * Lightweight markdown parser that renders #, ##, ### headings,
 * bullet lists (- or *), bold text, and inline code into clean semantic HTML.
 */
function parseMarkdown(mdText) {
  if (!mdText || typeof mdText !== "string") {
    return "<p>No release notes available.</p>";
  }

  const lines = mdText.split(/\r?\n/);
  const htmlParts = [];
  let inList = false;

  for (let i = 0; i < lines.length; i++) {
    const rawLine = lines[i];
    const trimmed = rawLine.trim();

    if (!trimmed) {
      if (inList) {
        htmlParts.push("</ul>");
        inList = false;
      }
      continue;
    }

    // List item: - or * followed by whitespace
    if (/^[-*]\s+/.test(trimmed)) {
      if (!inList) {
        htmlParts.push("<ul>");
        inList = true;
      }
      const itemContent = trimmed.replace(/^[-*]\s+/, "");
      htmlParts.push(`<li>${formatInlineMarkdown(itemContent)}</li>`);
      continue;
    }

    if (inList) {
      htmlParts.push("</ul>");
      inList = false;
    }

    if (trimmed.startsWith("### ")) {
      htmlParts.push(`<h3>${formatInlineMarkdown(trimmed.slice(4))}</h3>`);
    } else if (trimmed.startsWith("## ")) {
      htmlParts.push(`<h2>${formatInlineMarkdown(trimmed.slice(3))}</h2>`);
    } else if (trimmed.startsWith("# ")) {
      htmlParts.push(`<h1>${formatInlineMarkdown(trimmed.slice(2))}</h1>`);
    } else {
      htmlParts.push(`<p>${formatInlineMarkdown(trimmed)}</p>`);
    }
  }

  if (inList) {
    htmlParts.push("</ul>");
  }

  return htmlParts.join("\n");
}

/**
 * Update the header notice button and populate update modal content.
 */
function renderUpdateNotice(data) {
  latestUpdateInfo = data || null;

  const noticeBtn = document.getElementById("nav-update-notice-btn");
  const noticeLabel = document.getElementById("nav-update-version-label");

  const banner = document.getElementById("update-banner");
  const latestVerEl = document.getElementById("update-latest-version");
  const changelogEl = document.getElementById("update-changelog");

  if (data && data.update_available) {
    const rawTarget = String(data.latest_version || "");
    const formattedTarget = rawTarget.startsWith("v")
      ? rawTarget
      : `v${rawTarget || "--"}`;

    if (noticeLabel) {
      noticeLabel.textContent = `${formattedTarget} available`;
    }
    if (noticeBtn) {
      noticeBtn.classList.remove("hidden");
    }

    if (latestVerEl) {
      latestVerEl.textContent = data.latest_version || "latest";
    }
    if (changelogEl) {
      changelogEl.textContent =
        data.changelog || "New tagged release available on origin.";
    }
    if (banner) {
      banner.classList.add("hidden");
    }

    populateUpdateModal(data);
  } else {
    if (noticeBtn) {
      noticeBtn.classList.add("hidden");
    }
    if (banner) {
      banner.classList.add("hidden");
    }
  }
}

/**
 * Backward-compatible alias for renderUpdateBanner.
 */
function renderUpdateBanner(data) {
  renderUpdateNotice(data);
}

/**
 * Populate values inside #update-modal.
 */
function populateUpdateModal(data) {
  if (!data) return;

  const currentVerEl = document.getElementById("modal-current-version");
  const latestVerEl = document.getElementById("modal-latest-version");
  const notesEl = document.getElementById("modal-release-notes");

  if (currentVerEl) {
    const rawCur = String(data.current_version || "");
    currentVerEl.textContent = rawCur.startsWith("v")
      ? rawCur
      : `v${rawCur || "--"}`;
  }

  if (latestVerEl) {
    const rawTarget = String(data.latest_version || "");
    latestVerEl.textContent = rawTarget.startsWith("v")
      ? rawTarget
      : `v${rawTarget || "--"}`;
  }

  if (notesEl && !isViewingFullChangelog) {
    const rawNotes =
      data.release_notes ||
      data.changelog ||
      "New release available on origin.";
    notesEl.innerHTML = parseMarkdown(rawNotes);
  }
}

function openUpdateModal() {
  const modal = document.getElementById("update-modal");
  if (!modal) return;

  isViewingFullChangelog = false;
  const toggleBtnText = document.getElementById("modal-toggle-changelog-text");
  if (toggleBtnText) toggleBtnText.textContent = "View Full Changelog";
  const notesHeading = document.getElementById("modal-notes-heading");
  if (notesHeading) notesHeading.textContent = "Release Notes";

  clearModalUpdateStatus();

  if (latestUpdateInfo) {
    populateUpdateModal(latestUpdateInfo);
  }

  modal.classList.remove("hidden");
}

function closeUpdateModal() {
  const modal = document.getElementById("update-modal");
  if (modal) {
    modal.classList.add("hidden");
  }
}

function showModalUpdateStatus(message, type = "info") {
  const statusEl = document.getElementById("modal-update-status");
  if (!statusEl) return;
  statusEl.textContent = message;
  statusEl.className = `modal-update-status status-${type}`;
  statusEl.classList.remove("hidden");
}

function clearModalUpdateStatus() {
  const statusEl = document.getElementById("modal-update-status");
  if (!statusEl) return;
  statusEl.textContent = "";
  statusEl.className = "modal-update-status hidden";
}

async function toggleFullChangelog() {
  const notesEl = document.getElementById("modal-release-notes");
  const toggleBtnText = document.getElementById("modal-toggle-changelog-text");
  const notesHeading = document.getElementById("modal-notes-heading");

  if (!isViewingFullChangelog) {
    if (notesEl) {
      notesEl.innerHTML = "<p>Loading full changelog...</p>";
    }
    try {
      const resp = await apiFetch("/api/changelog");
      const data = await resp.json().catch(() => ({}));
      const fullText =
        data.changelog || "# Changelog\nNo full changelog available.";
      if (notesEl) {
        notesEl.innerHTML = parseMarkdown(fullText);
      }
      if (toggleBtnText) toggleBtnText.textContent = "View Release Notes";
      if (notesHeading) notesHeading.textContent = "Full Changelog";
      isViewingFullChangelog = true;
    } catch (err) {
      if (notesEl) {
        notesEl.innerHTML = `<p>Failed to load full changelog: ${escapeHtml(err.message)}</p>`;
      }
    }
  } else {
    if (notesEl && latestUpdateInfo) {
      const rawNotes =
        latestUpdateInfo.release_notes ||
        latestUpdateInfo.changelog ||
        "New release available on origin.";
      notesEl.innerHTML = parseMarkdown(rawNotes);
    }
    if (toggleBtnText) toggleBtnText.textContent = "View Full Changelog";
    if (notesHeading) notesHeading.textContent = "Release Notes";
    isViewingFullChangelog = false;
  }
}

async function checkForUpdates({ silent = false } = {}) {
  const checkBtn = document.getElementById("check-update-btn");
  if (checkBtn && !silent) {
    checkBtn.disabled = true;
    checkBtn.textContent = "Checking...";
  }

  try {
    const resp = await apiFetch("/api/update/check");
    if (resp.status === 401) return;
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || `Update check failed (${resp.status})`);
    }

    updateVersionBadge(data);
    renderUpdateNotice(data);

    if (data.update_available) {
      if (!silent) {
        openUpdateModal();
      }
    } else {
      if (checkBtn && !silent) {
        checkBtn.textContent = "Up to date";
        window.setTimeout(() => {
          if (checkBtn) checkBtn.textContent = "Check Updates";
        }, 2500);
      }
    }
  } catch (err) {
    console.error("Update check error:", err);
    if (!silent) {
      setErrorState(
        `Update check failed: ${err instanceof Error ? err.message : String(err)}`
      );
    }
  } finally {
    if (checkBtn && !silent) {
      checkBtn.disabled = false;
      if (checkBtn.textContent === "Checking...") {
        checkBtn.textContent = "Check Updates";
      }
    }
  }
}

async function applyReleaseUpdate() {
  const modalApplyBtn = document.getElementById("modal-apply-update-btn");
  const modalApplyLabel = document.getElementById("modal-apply-btn-label");
  const modalDismissBtn = document.getElementById("modal-dismiss-btn");
  const bannerApplyBtn = document.getElementById("apply-update-btn");

  if (modalApplyBtn) modalApplyBtn.disabled = true;
  if (modalApplyLabel) modalApplyLabel.textContent = "Updating...";
  if (modalDismissBtn) modalDismissBtn.disabled = true;
  if (bannerApplyBtn) {
    bannerApplyBtn.disabled = true;
    bannerApplyBtn.textContent = "Updating & Restarting Services...";
  }

  showModalUpdateStatus(
    "Applying update and restarting services. Please stand by...",
    "info"
  );

  try {
    const resp = await apiFetch("/api/update/apply", { method: "POST" });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || `Update failed (HTTP ${resp.status})`);
    }

    showModalUpdateStatus(
      "Update successfully applied. Restarting services and reconnecting...",
      "success"
    );

    closeWebSocket();
    closeSSEStream();
    stopPolling();
    pollUntilServerRestarts();
  } catch (err) {
    console.error("Failed to apply update:", err);
    const msg = `Failed to apply update: ${err instanceof Error ? err.message : String(err)}`;
    showModalUpdateStatus(msg, "error");
    setErrorState(msg);

    if (modalApplyBtn) modalApplyBtn.disabled = false;
    if (modalApplyLabel) modalApplyLabel.textContent = "Update & Restart Services";
    if (modalDismissBtn) modalDismissBtn.disabled = false;
    if (bannerApplyBtn) {
      bannerApplyBtn.disabled = false;
      bannerApplyBtn.textContent = "Update & Restart Services";
    }
  }
}

function pollUntilServerRestarts() {
  let attempts = 0;
  const maxAttempts = 40;
  const pollTimer = window.setInterval(async () => {
    attempts += 1;
    try {
      const resp = await fetch("/api/auth/status", {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (resp.ok && attempts >= 2) {
        window.clearInterval(pollTimer);
        window.location.reload();
      }
    } catch {
      // Server is restarting; keep polling
    }

    if (attempts >= maxAttempts) {
      window.clearInterval(pollTimer);
      window.location.reload();
    }
  }, 2000);
}

/* =========================================================================
 * 3. SQLite Settings Modal (#settings-modal)
 * ========================================================================= */

function showSettingsFeedback(message, isSuccess = false) {
  const el = document.getElementById("settings-feedback");
  if (!el) return;
  if (!message) {
    el.textContent = "";
    el.classList.add("hidden");
    el.classList.remove("success");
    return;
  }
  el.textContent = message;
  el.classList.toggle("success", isSuccess);
  el.classList.remove("hidden");
}

let hasRunDiagnostics = false;
let isRunningDiagnostics = false;
let currentSettingsSubtab = "general";

function switchSettingsSubtab(subtabName) {
  currentSettingsSubtab = subtabName;

  const btnGeneral = document.getElementById("subtab-settings-general");
  const btnDiagnostics = document.getElementById("subtab-settings");
  const btnDiagnosticsAlias = document.getElementById("subtab-settings-diagnostics");
  const panelGeneral = document.getElementById("subview-settings-general");
  const panelDiagnostics = document.getElementById("subview-settings");

  if (subtabName === "diagnostics" || subtabName === "settings") {
    btnGeneral?.classList.remove("active");
    btnGeneral?.setAttribute("aria-selected", "false");
    btnDiagnostics?.classList.add("active");
    btnDiagnostics?.setAttribute("aria-selected", "true");
    btnDiagnosticsAlias?.classList.add("active");
    btnDiagnosticsAlias?.setAttribute("aria-selected", "true");

    panelGeneral?.classList.add("hidden");
    panelDiagnostics?.classList.remove("hidden");

    if (!hasRunDiagnostics) {
      runSystemDiagnostics();
    }
  } else {
    btnGeneral?.classList.add("active");
    btnGeneral?.setAttribute("aria-selected", "true");
    btnDiagnostics?.classList.remove("active");
    btnDiagnostics?.setAttribute("aria-selected", "false");
    btnDiagnosticsAlias?.classList.remove("active");
    btnDiagnosticsAlias?.setAttribute("aria-selected", "false");

    panelGeneral?.classList.remove("hidden");
    panelDiagnostics?.classList.add("hidden");
  }
}

async function runSystemDiagnostics() {
  if (isRunningDiagnostics) return;
  isRunningDiagnostics = true;

  const btn = document.getElementById("btn-run-diagnostics");
  const overallBadge = document.getElementById("diagnostics-overall-badge");
  const summaryText = document.getElementById("diagnostics-summary-text");
  const timestampEl = document.getElementById("diagnostics-timestamp");

  const originalBtnHtml = btn ? btn.innerHTML : '<span class="btn-icon">⚡</span> Run Diagnostics';
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Running Diagnostics...";
  }

  try {
    const resp = await apiFetch("/api/system/diagnostics");
    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}));
      throw new Error(errData.detail || `Diagnostic self-test failed (${resp.status})`);
    }

    const data = await resp.json();
    hasRunDiagnostics = true;

    // Update overall badge
    if (overallBadge) {
      overallBadge.className = "status-badge";
      if (data.status === "healthy") {
        overallBadge.classList.add("status-clean");
        overallBadge.textContent = "HEALTHY";
      } else {
        overallBadge.classList.add("status-failed");
        overallBadge.textContent = "DEGRADED";
      }
    }

    // Update summary text
    if (summaryText) {
      summaryText.textContent = `${data.checks_passed} of ${data.checks_total} checks passed in ${data.duration_ms} ms`;
    }

    // Update timestamp
    if (timestampEl) {
      timestampEl.textContent = `Tested ${new Date(data.timestamp || Date.now()).toLocaleTimeString()}`;
    }

    // Subsystem checks mapping
    const subsystemKeys = [
      { key: "database", domId: "database" },
      { key: "notification_hub", domId: "notification-hub" },
      { key: "docker_and_templates", domId: "docker-templates" },
      { key: "telemetry_engine", domId: "telemetry-engine" },
      { key: "vigil_scheduler", domId: "vigil-scheduler" },
      { key: "api_security", domId: "api-security" },
    ];

    if (data.checks && typeof data.checks === "object") {
      subsystemKeys.forEach(({ key, domId }) => {
        const check = data.checks[key];
        const badgeEl =
          document.getElementById(`diag-badge-${domId}`) ||
          document.getElementById(`diag-badge-${key}`);
        const msgEl =
          document.getElementById(`diag-msg-${domId}`) ||
          document.getElementById(`diag-msg-${key}`);

        if (check) {
          const isOk = check.status === "ok";
          if (badgeEl) {
            badgeEl.className = `status-badge ${isOk ? "status-clean" : "status-failed"}`;
            badgeEl.textContent = isOk ? "OK" : "FAILED";
          }
          if (msgEl) {
            msgEl.textContent = check.message || (isOk ? "Subsystem verification passed" : "Verification check failed");
          }
        }
      });
    }

    return data;
  } catch (err) {
    if (overallBadge) {
      overallBadge.className = "status-badge status-failed";
      overallBadge.textContent = "ERROR";
    }
    if (summaryText) {
      summaryText.textContent = `Self-test failed: ${err.message || String(err)}`;
    }
    if (timestampEl) {
      timestampEl.textContent = `Tested ${new Date().toLocaleTimeString()}`;
    }
    if (typeof showToast === "function") {
      showToast(`Diagnostic check failed: ${err.message || String(err)}`, "error");
    }
  } finally {
    isRunningDiagnostics = false;
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = originalBtnHtml;
    }
  }
}

if (typeof window !== "undefined") {
  window.runSystemDiagnostics = runSystemDiagnostics;
  window.switchSettingsSubtab = switchSettingsSubtab;
}

async function openSettingsModal(defaultSubtab) {
  const modal = document.getElementById("settings-modal");
  if (!modal) return;
  showSettingsFeedback(null);
  modal.classList.remove("hidden");

  if (defaultSubtab) {
    switchSettingsSubtab(defaultSubtab);
  } else if (currentSettingsSubtab === "diagnostics" && !hasRunDiagnostics) {
    runSystemDiagnostics();
  }

  try {
    const resp = await apiFetch("/api/settings");
    if (resp.status === 401) {
      modal.classList.add("hidden");
      return;
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || `Failed to load settings (${resp.status})`);
    }

    populateSettingsForm(data);
  } catch (err) {
    showSettingsFeedback(
      err instanceof Error ? err.message : String(err),
      false
    );
  }
}

function populateSettingsForm(data) {
  const raidInput =
    document.getElementById("settings-raid-device") ||
    document.getElementById("setting-raid-device");
  const raidDatalist = document.getElementById("raid-devices-list");
  const raidHint = document.getElementById("raid-devices-hint");
  const mountInput = document.getElementById("setting-mount-path");
  const googleClientInput = document.getElementById("setting-google-client-id");
  const allowedEmailsInput = document.getElementById(
    "setting-allowed-google-emails"
  );

  if (raidInput) raidInput.value = data.raid_device || "auto";

  const availableDevices = Array.isArray(data.available_raid_devices)
    ? data.available_raid_devices
    : [];

  if (raidDatalist) {
    raidDatalist.innerHTML = "";
    const options = ["auto", ...availableDevices];
    const seen = new Set();
    options.forEach((optValue) => {
      if (!optValue || seen.has(optValue)) return;
      seen.add(optValue);
      const optionEl = document.createElement("option");
      optionEl.value = optValue;
      optionEl.textContent =
        optValue === "auto"
          ? "auto (Auto-detect active md array)"
          : optValue;
      raidDatalist.appendChild(optionEl);
    });
  }

  if (raidHint) {
    if (availableDevices.length === 0 && !data.resolved_raid_device) {
      raidHint.textContent = "Detected on host: No RAID arrays detected";
    } else {
      const detectedText =
        availableDevices.length > 0
          ? availableDevices.join(", ")
          : "none detected";
      const resolvedDevice =
        data.resolved_raid_device || data.raid_device || "none";
      raidHint.textContent = `Detected arrays: ${detectedText} | Resolved: ${resolvedDevice}`;
    }
  }

  if (mountInput) mountInput.value = data.mount_path || "/mnt/media/";
  if (googleClientInput) googleClientInput.value = data.google_client_id || "";
  if (allowedEmailsInput) {
    allowedEmailsInput.value = data.allowed_google_emails || "";
  }

  if (data.refresh_intervals) {
    syncIntervalSelects(data.refresh_intervals);
  }

  const tzSelect = document.getElementById("setting-timezone");
  const tzHint = document.getElementById("timezone-hint");
  if (tzSelect) {
    tzSelect.value = getStoredTimezone();
  }
  if (tzHint) {
    tzHint.textContent = `Current resolved timezone: ${getEffectiveTimezone()}`;
  }

  populateDiscordForm(data);
}

const populateSettingsModal = populateSettingsForm;

function closeSettingsModal() {
  const modal = document.getElementById("settings-modal");
  if (modal) modal.classList.add("hidden");
}

async function handleSettingsSubmit(event) {
  event.preventDefault();
  showSettingsFeedback(null);

  const raidInputEl =
    document.getElementById("settings-raid-device") ||
    document.getElementById("setting-raid-device");
  const payload = {
    raid_device: raidInputEl?.value.trim() || "auto",
    mount_path:
      document.getElementById("setting-mount-path")?.value.trim() ||
      "/mnt/media/",
    google_client_id:
      document.getElementById("setting-google-client-id")?.value.trim() || "",
    allowed_google_emails:
      document.getElementById("setting-allowed-google-emails")?.value.trim() ||
      "",
  };

  const saveBtn = document.getElementById("save-settings-btn");
  if (saveBtn) saveBtn.disabled = true;

  try {
    const resp = await apiFetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || `Failed to save settings (${resp.status})`);
    }

    const tzSelect = document.getElementById("setting-timezone");
    if (tzSelect) {
      setStoredTimezone(tzSelect.value);
      const tzHint = document.getElementById("timezone-hint");
      if (tzHint) {
        tzHint.textContent = `Current resolved timezone: ${getEffectiveTimezone()}`;
      }
    }

    populateSettingsForm(data);
    showSettingsFeedback("Settings saved to SQLite.", true);
    handleManualRefresh();
  } catch (err) {
    showSettingsFeedback(
      err instanceof Error ? err.message : String(err),
      false
    );
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

/**
 * Cache for app settings (Discord status, channel, tokens)
 */
let cachedDiscordSettings = null;

/**
 * Fetch settings from SQLite API to update Discord controls card.
 */
let cachedNotificationCenterConfig = null;

let notifFeedbackTimerId = null;

/**
 * Show feedback message in Notification Center (renders strictly to a single banner).
 */
function showNotificationFeedback(message, isSuccess = true) {
  const notifFeedback = document.getElementById("notif-feedback");
  const discordFeedback = document.getElementById("discord-feedback");
  const primary = notifFeedback || discordFeedback;
  if (!primary) return;

  if (notifFeedbackTimerId) {
    window.clearTimeout(notifFeedbackTimerId);
    notifFeedbackTimerId = null;
  }

  // Ensure secondary legacy element is ALWAYS cleared and hidden
  if (discordFeedback && discordFeedback !== primary) {
    discordFeedback.textContent = "";
    discordFeedback.className = "hidden";
    discordFeedback.style.display = "none";
  }

  if (!message) {
    primary.textContent = "";
    primary.classList.add("hidden");
    primary.classList.remove("feedback-success", "feedback-error");
    return;
  }

  primary.textContent = message;
  primary.classList.remove("hidden", "feedback-success", "feedback-error");
  primary.classList.add(isSuccess ? "feedback-success" : "feedback-error");

  if (isSuccess) {
    notifFeedbackTimerId = window.setTimeout(() => {
      primary.textContent = "";
      primary.classList.add("hidden");
      primary.classList.remove("feedback-success", "feedback-error");
    }, 5000);
  }
}

const showDiscordFeedback = showNotificationFeedback;
const showTestNotificationFeedback = showNotificationFeedback;

/**
 * Update next run info text based on schedule form controls.
 */
function updateNextRunDisplay(schedule = null) {
  const nextRunEl = document.getElementById("notif-schedule-next-run");
  if (!nextRunEl) return;

  const scheduleToggle = document.getElementById("notif-schedule-enable");
  const enabled = scheduleToggle ? scheduleToggle.checked : true;
  if (!enabled) {
    nextRunEl.textContent = "Scheduled reports disabled";
    return;
  }

  const freqEl = document.getElementById("notif-schedule-freq");
  const freq = freqEl ? freqEl.value : (schedule?.frequency || "daily");
  const dayEl = document.getElementById("notif-schedule-day");
  const day = dayEl ? dayEl.value : (schedule?.day || "monday");
  const timeEl = document.getElementById("notif-schedule-time");
  const time = timeEl ? timeEl.value : (schedule?.time || "09:00");

  let desc = `Daily at ${time}`;
  if (freq === "weekly") {
    const capitalizedDay = day ? (day.charAt(0).toUpperCase() + day.slice(1)) : "Monday";
    desc = `Every ${capitalizedDay} at ${time}`;
  } else if (freq === "hourly") {
    desc = `Every hour at :00`;
  } else if (freq === "6h") {
    desc = `Every 6 hours`;
  } else if (freq === "12h") {
    desc = `Every 12 hours`;
  }

  nextRunEl.textContent = `Next report scheduled: ${desc}`;
}

/**
 * Updates channel status pill badge showing ACTIVE, DISABLED, or NOT CONFIGURED.
 */
function updateChannelStatusBadge(badgeId, isConfigured, isEnabled) {
  const badge = document.getElementById(badgeId);
  if (!badge) return;
  badge.classList.remove(
    "status-clean",
    "status-active",
    "status-enabled",
    "status-degraded",
    "status-disabled",
    "status-not-configured",
    "status-unknown"
  );
  if (!isConfigured) {
    badge.textContent = "NOT CONFIGURED";
    badge.classList.add("status-degraded", "status-not-configured");
    badge.setAttribute("data-state", "not-configured");
  } else if (isEnabled) {
    badge.textContent = "ACTIVE";
    badge.classList.add("status-clean", "status-active", "status-enabled");
    badge.setAttribute("data-state", "active");
  } else {
    badge.textContent = "DISABLED";
    badge.classList.add("status-disabled");
    badge.setAttribute("data-state", "disabled");
  }
}

/**
 * Helper to extract payload for all 6 delivery channels (Discord, Telegram, Webhook, Pushover, Ntfy, Gotify)
 * including credentials and per-channel routing rules.
 */
function getDeliveryChannelsPayload() {
  const discordToggle = document.getElementById("notif-channel-discord-enable");
  const discordToken = document.getElementById("notif-discord-token-input");
  const discordChannel = document.getElementById("notif-discord-channel-input");

  const discordPayload = {
    enabled: Boolean(discordToggle ? discordToggle.checked : false),
    channel_id: discordChannel ? discordChannel.value.trim() : "",
    routing: {
      raid_alerts: Boolean(document.getElementById("notif-discord-route-raid-alerts")?.checked ?? true),
      raid_weekly: Boolean(document.getElementById("notif-discord-route-raid-weekly")?.checked ?? true),
      cron_failures: Boolean(document.getElementById("notif-discord-route-cron-failures")?.checked ?? true),
      cron_successes: Boolean(document.getElementById("notif-discord-route-cron-successes")?.checked ?? false),
    },
  };
  if (discordToken && discordToken.value.trim().length > 0) {
    discordPayload.token = discordToken.value.trim();
    discordPayload.bot_token = discordToken.value.trim();
  }

  const telegramToggle = document.getElementById("notif-channel-telegram-enable");
  const telegramToken = document.getElementById("notif-telegram-token-input");
  const telegramChatId = document.getElementById("notif-telegram-chat-id-input");

  const telegramPayload = {
    enabled: Boolean(telegramToggle ? telegramToggle.checked : false),
    chat_id: telegramChatId ? telegramChatId.value.trim() : "",
    routing: {
      raid_alerts: Boolean(document.getElementById("notif-telegram-route-raid-alerts")?.checked ?? true),
      raid_weekly: Boolean(document.getElementById("notif-telegram-route-raid-weekly")?.checked ?? true),
      cron_failures: Boolean(document.getElementById("notif-telegram-route-cron-failures")?.checked ?? true),
      cron_successes: Boolean(document.getElementById("notif-telegram-route-cron-successes")?.checked ?? false),
    },
  };
  if (telegramToken && telegramToken.value.trim().length > 0) {
    telegramPayload.token = telegramToken.value.trim();
    telegramPayload.bot_token = telegramToken.value.trim();
  }

  const webhookToggle = document.getElementById("notif-channel-webhook-enable");
  const webhookUrl = document.getElementById("notif-webhook-url-input");

  const webhookPayload = {
    enabled: Boolean(webhookToggle ? webhookToggle.checked : false),
    url: webhookUrl ? webhookUrl.value.trim() : "",
    routing: {
      raid_alerts: Boolean(document.getElementById("notif-webhook-route-raid-alerts")?.checked ?? true),
      raid_weekly: Boolean(document.getElementById("notif-webhook-route-raid-weekly")?.checked ?? true),
      cron_failures: Boolean(document.getElementById("notif-webhook-route-cron-failures")?.checked ?? true),
      cron_successes: Boolean(document.getElementById("notif-webhook-route-cron-successes")?.checked ?? false),
    },
  };

  const pushoverToggle = document.getElementById("notif-channel-pushover-enable");
  const pushoverUserKey = document.getElementById("notif-pushover-user-key-input");
  const pushoverToken = document.getElementById("notif-pushover-token-input");
  const pushoverPriority = document.getElementById("notif-pushover-priority-select");

  const pushoverPayload = {
    enabled: Boolean(pushoverToggle ? pushoverToggle.checked : false),
    priority: Number(pushoverPriority?.value ?? 0),
    routing: {
      raid_alerts: Boolean(document.getElementById("notif-pushover-route-raid-alerts")?.checked ?? true),
      raid_weekly: Boolean(document.getElementById("notif-pushover-route-raid-weekly")?.checked ?? true),
      cron_failures: Boolean(document.getElementById("notif-pushover-route-cron-failures")?.checked ?? true),
      cron_successes: Boolean(document.getElementById("notif-pushover-route-cron-successes")?.checked ?? false),
    },
  };
  if (pushoverUserKey && pushoverUserKey.value.trim().length > 0) {
    pushoverPayload.user_key = pushoverUserKey.value.trim();
  }
  if (pushoverToken && pushoverToken.value.trim().length > 0) {
    pushoverPayload.api_token = pushoverToken.value.trim();
  }

  const ntfyToggle = document.getElementById("notif-channel-ntfy-enable");
  const ntfyUrl = document.getElementById("notif-ntfy-url-input");
  const ntfyTopic = document.getElementById("notif-ntfy-topic-input");
  const ntfyToken = document.getElementById("notif-ntfy-token-input");
  const ntfyPriority = document.getElementById("notif-ntfy-priority-select");

  const ntfyPayload = {
    enabled: Boolean(ntfyToggle ? ntfyToggle.checked : false),
    server_url: (ntfyUrl?.value || "").trim() || "https://ntfy.sh",
    topic: (ntfyTopic?.value || "").trim(),
    priority: Number(ntfyPriority?.value ?? 3),
    routing: {
      raid_alerts: Boolean(document.getElementById("notif-ntfy-route-raid-alerts")?.checked ?? true),
      raid_weekly: Boolean(document.getElementById("notif-ntfy-route-raid-weekly")?.checked ?? true),
      cron_failures: Boolean(document.getElementById("notif-ntfy-route-cron-failures")?.checked ?? true),
      cron_successes: Boolean(document.getElementById("notif-ntfy-route-cron-successes")?.checked ?? false),
    },
  };
  if (ntfyToken && ntfyToken.value.trim().length > 0) {
    ntfyPayload.access_token = ntfyToken.value.trim();
  }

  const gotifyToggle = document.getElementById("notif-channel-gotify-enable");
  const gotifyUrl = document.getElementById("notif-gotify-url-input");
  const gotifyToken = document.getElementById("notif-gotify-token-input");
  const gotifyPriority = document.getElementById("notif-gotify-priority-select");

  const gotifyPayload = {
    enabled: Boolean(gotifyToggle ? gotifyToggle.checked : false),
    server_url: (gotifyUrl?.value || "").trim(),
    priority: Number(gotifyPriority?.value ?? 5),
    routing: {
      raid_alerts: Boolean(document.getElementById("notif-gotify-route-raid-alerts")?.checked ?? true),
      raid_weekly: Boolean(document.getElementById("notif-gotify-route-raid-weekly")?.checked ?? true),
      cron_failures: Boolean(document.getElementById("notif-gotify-route-cron-failures")?.checked ?? true),
      cron_successes: Boolean(document.getElementById("notif-gotify-route-cron-successes")?.checked ?? false),
    },
  };
  if (gotifyToken && gotifyToken.value.trim().length > 0) {
    gotifyPayload.app_token = gotifyToken.value.trim();
  }

  return {
    discord: discordPayload,
    telegram: telegramPayload,
    webhook: webhookPayload,
    pushover: pushoverPayload,
    ntfy: ntfyPayload,
    gotify: gotifyPayload,
  };
}

/**
 * Populate all Notification Center form controls and status pills from GET /api/notifications/center payload.
 */
function renderNotificationCenterConfig(config) {
  if (!config || typeof config !== "object") return;
  cachedNotificationCenterConfig = config;

  const channels = config.channels || {};
  const discordCh = channels.discord || {};
  const telegramCh = channels.telegram || {};
  const webhookCh = channels.webhook || {};
  const pushoverCh = channels.pushover || {};
  const ntfyCh = channels.ntfy || {};
  const gotifyCh = channels.gotify || {};
  const schedule = config.schedule || {};
  const topics = config.topics || {};
  const thresholds = config.thresholds || {};

  // 1. Discord Channel
  const notifDiscordToggle = document.getElementById("notif-channel-discord-enable");
  const discordLegacyToggle = document.getElementById("discord-enabled-toggle");
  const discordEnabled = Boolean(
    discordCh.enabled !== undefined
      ? discordCh.enabled
      : (config.discord_enabled !== undefined ? config.discord_enabled : false)
  );
  if (notifDiscordToggle) notifDiscordToggle.checked = discordEnabled;
  if (discordLegacyToggle) discordLegacyToggle.checked = discordEnabled;

  const notifDiscordToken = document.getElementById("notif-discord-token-input");
  const discordLegacyToken = document.getElementById("discord-token-input");
  const discordTokenBadge = document.getElementById("discord-token-status");
  const isTokenConfigured = Boolean(
    discordCh.token_configured !== undefined
      ? discordCh.token_configured
      : (discordCh.bot_token_configured ?? config.discord_token_configured)
  );
  const tokenMasked = discordCh.token_masked || config.discord_token_masked || "";

  if (notifDiscordToken) {
    notifDiscordToken.value = "";
    notifDiscordToken.placeholder = isTokenConfigured
      ? `Configured (${tokenMasked || "••••••••"}) — type to replace`
      : "Leave blank to keep existing token";
  }
  if (discordLegacyToken) {
    discordLegacyToken.value = "";
    discordLegacyToken.placeholder = notifDiscordToken ? notifDiscordToken.placeholder : "";
  }
  if (discordTokenBadge) {
    if (isTokenConfigured) {
      discordTokenBadge.textContent = tokenMasked ? `Configured (${tokenMasked})` : "Configured";
      discordTokenBadge.classList.add("configured");
    } else {
      discordTokenBadge.textContent = "Not configured";
      discordTokenBadge.classList.remove("configured");
    }
  }

  const notifDiscordChannel = document.getElementById("notif-discord-channel-input");
  const discordLegacyChannel = document.getElementById("discord-channel-input");
  const channelId = discordCh.channel_id || config.channel_id || "";
  if (notifDiscordChannel) notifDiscordChannel.value = channelId;
  if (discordLegacyChannel) discordLegacyChannel.value = channelId;

  // Discord Routing Checkboxes
  const discordRouting = discordCh.routing || {};
  const notifDiscordRouteAlerts = document.getElementById("notif-discord-route-raid-alerts");
  if (notifDiscordRouteAlerts) notifDiscordRouteAlerts.checked = Boolean(discordRouting.raid_alerts ?? true);
  const notifDiscordRouteWeekly = document.getElementById("notif-discord-route-raid-weekly");
  if (notifDiscordRouteWeekly) notifDiscordRouteWeekly.checked = Boolean(discordRouting.raid_weekly ?? true);
  const notifDiscordRouteCronFailures = document.getElementById("notif-discord-route-cron-failures");
  if (notifDiscordRouteCronFailures) notifDiscordRouteCronFailures.checked = Boolean(discordRouting.cron_failures ?? true);
  const notifDiscordRouteCronSuccesses = document.getElementById("notif-discord-route-cron-successes");
  if (notifDiscordRouteCronSuccesses) notifDiscordRouteCronSuccesses.checked = Boolean(discordRouting.cron_successes ?? false);

  const isDiscordConfigured = Boolean((isTokenConfigured || discordCh.bot_token) && channelId);
  updateChannelStatusBadge("discord-status-badge", isDiscordConfigured, discordEnabled);

  const overviewDiscordBadge = document.getElementById("overview-discord-badge");
  if (overviewDiscordBadge) {
    const targetClass = `status-badge ${discordEnabled ? "status-clean" : "status-degraded"}`;
    const targetText = discordEnabled ? "ACTIVE" : "DISABLED";
    setBadgeIfChanged(overviewDiscordBadge, null, targetClass, targetText);
  }
  const overviewDiscordSummary = document.getElementById("overview-discord-summary");
  if (overviewDiscordSummary) {
    const targetText = discordEnabled
      ? `Active Discord alerts channel: ${channelId || "configured"}`
      : "Discord notifications are currently disabled";
    setTextIfChanged(overviewDiscordSummary, targetText);
  }

  // 2. Telegram Channel
  const notifTelegramToggle = document.getElementById("notif-channel-telegram-enable");
  if (notifTelegramToggle) notifTelegramToggle.checked = Boolean(telegramCh.enabled);

  const notifTelegramToken = document.getElementById("notif-telegram-token-input");
  const notifTelegramBadge = document.getElementById("notif-telegram-token-badge");
  const isTgConfigured = Boolean(telegramCh.token_configured || telegramCh.bot_token_configured);
  if (notifTelegramToken) {
    notifTelegramToken.value = "";
    notifTelegramToken.placeholder = isTgConfigured
      ? `Configured (${telegramCh.token_masked || "••••••••"}) — type to replace`
      : "e.g. 123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11";
  }
  if (notifTelegramBadge) {
    if (isTgConfigured) {
      notifTelegramBadge.textContent = telegramCh.token_masked
        ? `Configured (${telegramCh.token_masked})`
        : "Configured";
      notifTelegramBadge.classList.add("configured");
    } else {
      notifTelegramBadge.textContent = "Not configured";
      notifTelegramBadge.classList.remove("configured");
    }
  }

  const notifTelegramChatId = document.getElementById("notif-telegram-chat-id-input");
  if (notifTelegramChatId) notifTelegramChatId.value = telegramCh.chat_id || "";

  // Telegram Routing Checkboxes
  const telegramRouting = telegramCh.routing || {};
  const notifTgRouteAlerts = document.getElementById("notif-telegram-route-raid-alerts");
  if (notifTgRouteAlerts) notifTgRouteAlerts.checked = Boolean(telegramRouting.raid_alerts ?? true);
  const notifTgRouteWeekly = document.getElementById("notif-telegram-route-raid-weekly");
  if (notifTgRouteWeekly) notifTgRouteWeekly.checked = Boolean(telegramRouting.raid_weekly ?? true);
  const notifTgRouteCronFailures = document.getElementById("notif-telegram-route-cron-failures");
  if (notifTgRouteCronFailures) notifTgRouteCronFailures.checked = Boolean(telegramRouting.cron_failures ?? true);
  const notifTgRouteCronSuccesses = document.getElementById("notif-telegram-route-cron-successes");
  if (notifTgRouteCronSuccesses) notifTgRouteCronSuccesses.checked = Boolean(telegramRouting.cron_successes ?? false);

  const isTgFullyConfigured = Boolean((isTgConfigured || telegramCh.bot_token) && telegramCh.chat_id);
  updateChannelStatusBadge("telegram-status-badge", isTgFullyConfigured, Boolean(telegramCh.enabled));

  // 3. Generic Webhook Channel
  const notifWebhookToggle = document.getElementById("notif-channel-webhook-enable");
  if (notifWebhookToggle) notifWebhookToggle.checked = Boolean(webhookCh.enabled);

  const notifWebhookUrl = document.getElementById("notif-webhook-url-input");
  if (notifWebhookUrl) notifWebhookUrl.value = webhookCh.url || "";

  // Webhook Routing Checkboxes
  const webhookRouting = webhookCh.routing || {};
  const notifWhRouteAlerts = document.getElementById("notif-webhook-route-raid-alerts");
  if (notifWhRouteAlerts) notifWhRouteAlerts.checked = Boolean(webhookRouting.raid_alerts ?? true);
  const notifWhRouteWeekly = document.getElementById("notif-webhook-route-raid-weekly");
  if (notifWhRouteWeekly) notifWhRouteWeekly.checked = Boolean(webhookRouting.raid_weekly ?? true);
  const notifWhRouteCronFailures = document.getElementById("notif-webhook-route-cron-failures");
  if (notifWhRouteCronFailures) notifWhRouteCronFailures.checked = Boolean(webhookRouting.cron_failures ?? true);
  const notifWhRouteCronSuccesses = document.getElementById("notif-webhook-route-cron-successes");
  if (notifWhRouteCronSuccesses) notifWhRouteCronSuccesses.checked = Boolean(webhookRouting.cron_successes ?? false);

  const isWebhookConfigured = Boolean(webhookCh.url && webhookCh.url.trim().length > 0);
  updateChannelStatusBadge("webhook-status-badge", isWebhookConfigured, Boolean(webhookCh.enabled));

  // 4. Pushover Channel
  const notifPushoverToggle = document.getElementById("notif-channel-pushover-enable");
  if (notifPushoverToggle) notifPushoverToggle.checked = Boolean(pushoverCh.enabled);

  const notifPushoverUserKey = document.getElementById("notif-pushover-user-key-input");
  const notifPushoverUserKeyBadge = document.getElementById("notif-pushover-user-key-badge");
  const isPushoverUserConfigured = Boolean(pushoverCh.user_key_configured);
  if (notifPushoverUserKey) {
    notifPushoverUserKey.value = "";
    notifPushoverUserKey.placeholder = isPushoverUserConfigured
      ? `Configured (${pushoverCh.user_key_masked || "••••••••"}) — type to replace`
      : "e.g. uQiRzpo4DXghDmr9QzzfQu27cmVRsG";
  }
  if (notifPushoverUserKeyBadge) {
    if (isPushoverUserConfigured) {
      notifPushoverUserKeyBadge.textContent = pushoverCh.user_key_masked
        ? `Configured (${pushoverCh.user_key_masked})`
        : "Configured";
      notifPushoverUserKeyBadge.classList.add("configured");
    } else {
      notifPushoverUserKeyBadge.textContent = "Not configured";
      notifPushoverUserKeyBadge.classList.remove("configured");
    }
  }

  const notifPushoverToken = document.getElementById("notif-pushover-token-input");
  const notifPushoverTokenBadge = document.getElementById("notif-pushover-token-badge");
  const isPushoverTokenConfigured = Boolean(pushoverCh.api_token_configured || pushoverCh.token_configured);
  if (notifPushoverToken) {
    notifPushoverToken.value = "";
    notifPushoverToken.placeholder = isPushoverTokenConfigured
      ? `Configured (${pushoverCh.token_masked || "••••••••"}) — type to replace`
      : "e.g. azGDORePK8gMaC0QOYAMyEEuzJnyUi";
  }
  if (notifPushoverTokenBadge) {
    if (isPushoverTokenConfigured) {
      notifPushoverTokenBadge.textContent = pushoverCh.token_masked
        ? `Configured (${pushoverCh.token_masked})`
        : "Configured";
      notifPushoverTokenBadge.classList.add("configured");
    } else {
      notifPushoverTokenBadge.textContent = "Not configured";
      notifPushoverTokenBadge.classList.remove("configured");
    }
  }

  const notifPushoverPriority = document.getElementById("notif-pushover-priority-select");
  if (notifPushoverPriority && pushoverCh.priority !== undefined) {
    notifPushoverPriority.value = String(pushoverCh.priority);
  }

  // Pushover Routing Checkboxes
  const pushoverRouting = pushoverCh.routing || {};
  const notifPushRouteAlerts = document.getElementById("notif-pushover-route-raid-alerts");
  if (notifPushRouteAlerts) notifPushRouteAlerts.checked = Boolean(pushoverRouting.raid_alerts ?? true);
  const notifPushRouteWeekly = document.getElementById("notif-pushover-route-raid-weekly");
  if (notifPushRouteWeekly) notifPushRouteWeekly.checked = Boolean(pushoverRouting.raid_weekly ?? true);
  const notifPushRouteCronFailures = document.getElementById("notif-pushover-route-cron-failures");
  if (notifPushRouteCronFailures) notifPushRouteCronFailures.checked = Boolean(pushoverRouting.cron_failures ?? true);
  const notifPushRouteCronSuccesses = document.getElementById("notif-pushover-route-cron-successes");
  if (notifPushRouteCronSuccesses) notifPushRouteCronSuccesses.checked = Boolean(pushoverRouting.cron_successes ?? false);

  const isPushoverConfigured = Boolean(isPushoverUserConfigured && isPushoverTokenConfigured);
  updateChannelStatusBadge("pushover-status-badge", isPushoverConfigured, Boolean(pushoverCh.enabled));

  // 5. Ntfy Channel
  const notifNtfyToggle = document.getElementById("notif-channel-ntfy-enable");
  if (notifNtfyToggle) notifNtfyToggle.checked = Boolean(ntfyCh.enabled);

  const notifNtfyUrl = document.getElementById("notif-ntfy-url-input");
  if (notifNtfyUrl) notifNtfyUrl.value = ntfyCh.server_url || ntfyCh.url || "https://ntfy.sh";

  const notifNtfyTopic = document.getElementById("notif-ntfy-topic-input");
  if (notifNtfyTopic) notifNtfyTopic.value = ntfyCh.topic || "";

  const notifNtfyToken = document.getElementById("notif-ntfy-token-input");
  const notifNtfyTokenBadge = document.getElementById("notif-ntfy-token-badge");
  const isNtfyTokenConfigured = Boolean(ntfyCh.access_token_configured || ntfyCh.token_configured);
  if (notifNtfyToken) {
    notifNtfyToken.value = "";
    notifNtfyToken.placeholder = isNtfyTokenConfigured
      ? `Configured (${ntfyCh.token_masked || "••••••••"}) — type to replace`
      : "e.g. tk_your_ntfy_token (optional)";
  }
  if (notifNtfyTokenBadge) {
    if (isNtfyTokenConfigured) {
      notifNtfyTokenBadge.textContent = ntfyCh.token_masked
        ? `Configured (${ntfyCh.token_masked})`
        : "Configured";
      notifNtfyTokenBadge.classList.add("configured");
    } else {
      notifNtfyTokenBadge.textContent = "Not configured";
      notifNtfyTokenBadge.classList.remove("configured");
    }
  }

  const notifNtfyPriority = document.getElementById("notif-ntfy-priority-select");
  if (notifNtfyPriority && ntfyCh.priority !== undefined) {
    notifNtfyPriority.value = String(ntfyCh.priority);
  }

  // Ntfy Routing Checkboxes
  const ntfyRouting = ntfyCh.routing || {};
  const notifNtfyRouteAlerts = document.getElementById("notif-ntfy-route-raid-alerts");
  if (notifNtfyRouteAlerts) notifNtfyRouteAlerts.checked = Boolean(ntfyRouting.raid_alerts ?? true);
  const notifNtfyRouteWeekly = document.getElementById("notif-ntfy-route-raid-weekly");
  if (notifNtfyRouteWeekly) notifNtfyRouteWeekly.checked = Boolean(ntfyRouting.raid_weekly ?? true);
  const notifNtfyRouteCronFailures = document.getElementById("notif-ntfy-route-cron-failures");
  if (notifNtfyRouteCronFailures) notifNtfyRouteCronFailures.checked = Boolean(ntfyRouting.cron_failures ?? true);
  const notifNtfyRouteCronSuccesses = document.getElementById("notif-ntfy-route-cron-successes");
  if (notifNtfyRouteCronSuccesses) notifNtfyRouteCronSuccesses.checked = Boolean(ntfyRouting.cron_successes ?? false);

  const isNtfyConfigured = Boolean(ntfyCh.topic && ntfyCh.topic.trim().length > 0);
  updateChannelStatusBadge("ntfy-status-badge", isNtfyConfigured, Boolean(ntfyCh.enabled));

  // 6. Gotify Channel
  const notifGotifyToggle = document.getElementById("notif-channel-gotify-enable");
  if (notifGotifyToggle) notifGotifyToggle.checked = Boolean(gotifyCh.enabled);

  const notifGotifyUrl = document.getElementById("notif-gotify-url-input");
  if (notifGotifyUrl) notifGotifyUrl.value = gotifyCh.server_url || gotifyCh.url || "";

  const notifGotifyToken = document.getElementById("notif-gotify-token-input");
  const notifGotifyTokenBadge = document.getElementById("notif-gotify-token-badge");
  const isGotifyTokenConfigured = Boolean(gotifyCh.app_token_configured || gotifyCh.token_configured);
  if (notifGotifyToken) {
    notifGotifyToken.value = "";
    notifGotifyToken.placeholder = isGotifyTokenConfigured
      ? `Configured (${gotifyCh.token_masked || "••••••••"}) — type to replace`
      : "e.g. A1B2C3D4E5";
  }
  if (notifGotifyTokenBadge) {
    if (isGotifyTokenConfigured) {
      notifGotifyTokenBadge.textContent = gotifyCh.token_masked
        ? `Configured (${gotifyCh.token_masked})`
        : "Configured";
      notifGotifyTokenBadge.classList.add("configured");
    } else {
      notifGotifyTokenBadge.textContent = "Not configured";
      notifGotifyTokenBadge.classList.remove("configured");
    }
  }

  const notifGotifyPriority = document.getElementById("notif-gotify-priority-select");
  if (notifGotifyPriority && gotifyCh.priority !== undefined) {
    notifGotifyPriority.value = String(gotifyCh.priority);
  }

  // Gotify Routing Checkboxes
  const gotifyRouting = gotifyCh.routing || {};
  const notifGotifyRouteAlerts = document.getElementById("notif-gotify-route-raid-alerts");
  if (notifGotifyRouteAlerts) notifGotifyRouteAlerts.checked = Boolean(gotifyRouting.raid_alerts ?? true);
  const notifGotifyRouteWeekly = document.getElementById("notif-gotify-route-raid-weekly");
  if (notifGotifyRouteWeekly) notifGotifyRouteWeekly.checked = Boolean(gotifyRouting.raid_weekly ?? true);
  const notifGotifyRouteCronFailures = document.getElementById("notif-gotify-route-cron-failures");
  if (notifGotifyRouteCronFailures) notifGotifyRouteCronFailures.checked = Boolean(gotifyRouting.cron_failures ?? true);
  const notifGotifyRouteCronSuccesses = document.getElementById("notif-gotify-route-cron-successes");
  if (notifGotifyRouteCronSuccesses) notifGotifyRouteCronSuccesses.checked = Boolean(gotifyRouting.cron_successes ?? false);

  const isGotifyConfigured = Boolean(gotifyCh.server_url && (isGotifyTokenConfigured || gotifyCh.app_token));
  updateChannelStatusBadge("gotify-status-badge", isGotifyConfigured, Boolean(gotifyCh.enabled));

  // 7. Automated Report Schedule
  const scheduleToggle = document.getElementById("notif-schedule-enable");
  if (scheduleToggle) scheduleToggle.checked = Boolean(schedule.enabled ?? true);

  const scheduleFreq = document.getElementById("notif-schedule-freq");
  if (scheduleFreq && schedule.frequency) scheduleFreq.value = schedule.frequency;

  const scheduleDay = document.getElementById("notif-schedule-day");
  if (scheduleDay && schedule.day) scheduleDay.value = schedule.day;

  const scheduleDayGroup = document.getElementById("notif-schedule-day-group");
  if (scheduleDayGroup) {
    scheduleDayGroup.classList.toggle("hidden", (scheduleFreq?.value || "daily") !== "weekly");
  }

  const scheduleTime = document.getElementById("notif-schedule-time");
  if (scheduleTime && schedule.time) scheduleTime.value = schedule.time;

  updateNextRunDisplay(schedule);

  // 8. Topics
  const topicSys = document.getElementById("notif-topic-system");
  if (topicSys) topicSys.checked = Boolean(topics.system ?? true);

  const topicDisks = document.getElementById("notif-topic-disks");
  if (topicDisks) topicDisks.checked = Boolean(topics.disks ?? true);

  const topicRaid = document.getElementById("notif-topic-raid");
  if (topicRaid) topicRaid.checked = Boolean(topics.raid ?? true);

  // 9. Thresholds
  const ramTh = thresholds.ram || {};
  const notifRamEnable = document.getElementById("notif-threshold-ram-enable");
  const notifRamVal = document.getElementById("notif-threshold-ram-value");
  const notifRamSlider = document.getElementById("notif-threshold-ram-slider");
  if (notifRamEnable) notifRamEnable.checked = Boolean(ramTh.enabled ?? true);
  const ramVal = Number(ramTh.value ?? 90);
  if (notifRamVal) notifRamVal.value = ramVal;
  if (notifRamSlider) notifRamSlider.value = ramVal;

  const cpuTh = thresholds.cpu || {};
  const notifCpuEnable = document.getElementById("notif-threshold-cpu-enable");
  const notifCpuVal = document.getElementById("notif-threshold-cpu-value");
  const notifCpuSlider = document.getElementById("notif-threshold-cpu-slider");
  if (notifCpuEnable) notifCpuEnable.checked = Boolean(cpuTh.enabled ?? true);
  const cpuVal = Number(cpuTh.value ?? 90);
  if (notifCpuVal) notifCpuVal.value = cpuVal;
  if (notifCpuSlider) notifCpuSlider.value = cpuVal;

  const diskTh = thresholds.disk || {};
  const notifDiskEnable = document.getElementById("notif-threshold-disk-enable");
  const notifDiskVal = document.getElementById("notif-threshold-disk-value");
  const notifDiskSlider = document.getElementById("notif-threshold-disk-slider");
  if (notifDiskEnable) notifDiskEnable.checked = Boolean(diskTh.enabled ?? true);
  const diskVal = Number(diskTh.value ?? 85);
  if (notifDiskVal) notifDiskVal.value = diskVal;
  if (notifDiskSlider) notifDiskSlider.value = diskVal;

  const raidTh = thresholds.raid || {};
  const notifRaidEnable = document.getElementById("notif-threshold-raid-enable");
  if (notifRaidEnable) notifRaidEnable.checked = Boolean(raidTh.enabled ?? true);

  const ribbonRam = document.getElementById("ribbon-thresh-ram-val");
  if (ribbonRam) ribbonRam.textContent = `${ramVal}%`;
  const ribbonCpu = document.getElementById("ribbon-thresh-cpu-val");
  if (ribbonCpu) ribbonCpu.textContent = `${cpuVal}%`;
  const ribbonDisk = document.getElementById("ribbon-thresh-disk-val");
  if (ribbonDisk) ribbonDisk.textContent = `${diskVal}%`;
}

const populateNotificationCenter = renderNotificationCenterConfig;
if (typeof window !== "undefined") {
  window.renderNotificationCenterConfig = renderNotificationCenterConfig;
  window.populateNotificationCenter = populateNotificationCenter;
}

/* =========================================================================
 * Notification Center Modal Controllers
 * ========================================================================= */

function openNotifChannelsModal() {
  const modal = document.getElementById("modal-notif-channels");
  if (modal) modal.classList.remove("hidden");
}

function closeNotifChannelsModal() {
  const modal = document.getElementById("modal-notif-channels");
  if (modal) modal.classList.add("hidden");
}

function openNotifScheduleModal() {
  const modal = document.getElementById("modal-notif-schedule");
  if (modal) modal.classList.remove("hidden");
}

function closeNotifScheduleModal() {
  const modal = document.getElementById("modal-notif-schedule");
  if (modal) modal.classList.add("hidden");
}

function openNotifThresholdsModal() {
  const modal = document.getElementById("modal-notif-thresholds");
  if (modal) modal.classList.remove("hidden");
}

function closeNotifThresholdsModal() {
  const modal = document.getElementById("modal-notif-thresholds");
  if (modal) modal.classList.add("hidden");
}

/**
 * Legacy populator kept for backwards compatibility with tests.
 */
function populateDiscordForm(settings) {
  if (!settings) return;
  cachedDiscordSettings = settings;
  populateNotificationCenter(settings);
}

const renderDiscordStatusCard = populateDiscordForm;

/**
 * Fetch full Notification Center configuration from GET /api/notifications/center.
 */
async function fetchNotificationCenterConfig() {
  if (!isAuthenticated) return;
  try {
    const resp = await apiFetch("/api/notifications/center");
    if (resp.ok) {
      const data = await resp.json().catch(() => ({}));
      populateNotificationCenter(data);
      return;
    }
  } catch (err) {
    console.debug("GET /api/notifications/center error, checking fallback:", err);
  }

  // Fallback to /api/settings if needed
  try {
    const respSettings = await apiFetch("/api/settings");
    if (respSettings.ok) {
      const settingsData = await respSettings.json().catch(() => ({}));
      populateDiscordForm(settingsData);
    }
  } catch (err) {
    console.warn("Fallback fetch settings error:", err);
  }
}

async function fetchDiscordSettings() {
  await fetchNotificationCenterConfig();
}

/**
 * Collect form controls and save to POST /api/notifications/center.
 */
async function saveNotificationCenterConfig() {
  const saveBtn = document.getElementById("save-notifications-btn");
  if (saveBtn) saveBtn.disabled = true;
  showNotificationFeedback(null);

  const scheduleToggle = document.getElementById("notif-schedule-enable");
  const scheduleFreq = document.getElementById("notif-schedule-freq");
  const scheduleDay = document.getElementById("notif-schedule-day");
  const scheduleTime = document.getElementById("notif-schedule-time");

  const topicSys = document.getElementById("notif-topic-system");
  const topicDisks = document.getElementById("notif-topic-disks");
  const topicRaid = document.getElementById("notif-topic-raid");

  const ramToggle = document.getElementById("notif-threshold-ram-enable");
  const ramVal = document.getElementById("notif-threshold-ram-value");
  const cpuToggle = document.getElementById("notif-threshold-cpu-enable");
  const cpuVal = document.getElementById("notif-threshold-cpu-value");
  const diskToggle = document.getElementById("notif-threshold-disk-enable");
  const diskVal = document.getElementById("notif-threshold-disk-value");
  const raidToggle = document.getElementById("notif-threshold-raid-enable");

  const payload = {
    channels: getDeliveryChannelsPayload(),
    schedule: {
      enabled: Boolean(scheduleToggle ? scheduleToggle.checked : false),
      frequency: scheduleFreq ? scheduleFreq.value : "daily",
      day: scheduleDay ? scheduleDay.value : "monday",
      time: scheduleTime ? scheduleTime.value : "09:00",
    },
    topics: {
      system: Boolean(topicSys ? topicSys.checked : false),
      disks: Boolean(topicDisks ? topicDisks.checked : false),
      raid: Boolean(topicRaid ? topicRaid.checked : false),
    },
    thresholds: {
      ram: {
        enabled: Boolean(ramToggle ? ramToggle.checked : false),
        value: Number(ramVal ? ramVal.value : 90) || 90,
      },
      cpu: {
        enabled: Boolean(cpuToggle ? cpuToggle.checked : false),
        value: Number(cpuVal ? cpuVal.value : 90) || 90,
      },
      disk: {
        enabled: Boolean(diskToggle ? diskToggle.checked : false),
        value: Number(diskVal ? diskVal.value : 85) || 85,
      },
      raid: {
        enabled: Boolean(raidToggle ? raidToggle.checked : false),
      },
    },
  };

  try {
    const resp = await apiFetch("/api/notifications/center", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json().catch(() => ({}));

    if (resp.status === 401) {
      showNotificationFeedback("Authentication required. Please sign in again.", false);
      return;
    }

    if (!resp.ok) {
      throw new Error(
        data.detail || `Failed to save Notification Center settings (${resp.status})`
      );
    }

    renderNotificationCenterConfig(data);
    showNotificationFeedback("Notification Center configuration saved successfully.", true);
    closeNotifScheduleModal();
    closeNotifThresholdsModal();
  } catch (err) {
    showNotificationFeedback(
      err instanceof Error ? err.message : String(err),
      false
    );
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

/**
 * Save Delivery Channels configuration (Discord, Telegram, Webhook, Pushover, Ntfy, Gotify)
 * from Delivery Channels modal card. Retains #save-discord-btn for contract compatibility.
 */
async function handleSaveDiscordSettings() {
  const saveBtn = document.getElementById("save-discord-btn");
  if (saveBtn) saveBtn.disabled = true;
  showNotificationFeedback(null);

  const payload = {
    channels: getDeliveryChannelsPayload(),
  };

  try {
    const resp = await apiFetch("/api/notifications/center", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json().catch(() => ({}));

    if (resp.status === 401) {
      showNotificationFeedback("Authentication required. Please sign in again.", false);
      return;
    }

    if (!resp.ok) {
      throw new Error(data.detail || `Failed to save delivery channels (${resp.status})`);
    }

    renderNotificationCenterConfig(data);
    showNotificationFeedback("Delivery channels saved successfully.", true);
    closeNotifChannelsModal();
  } catch (err) {
    showNotificationFeedback(
      err instanceof Error ? err.message : String(err),
      false
    );
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

/**
 * Send test notification to target channel ("all", "discord", "telegram", "webhook", "pushover", "ntfy", "gotify").
 */
async function handleSendTestNotification(channel) {
  const btn = document.getElementById("send-test-notification-btn");
  if (!btn || btn.disabled) return;

  const originalHtml = btn.innerHTML;
  btn.disabled = true;
  btn.classList.add("loading");
  btn.innerHTML = `
    <svg class="refresh-icon" viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <polyline points="23 4 23 10 17 10"></polyline>
      <polyline points="1 20 1 14 7 14"></polyline>
      <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path>
    </svg>
    <span>Sending test notification...</span>
  `;

  showNotificationFeedback(null);

  const testSelect = document.getElementById("notif-test-channel-select");
  const selectedChannel = (typeof channel === "string" && channel) ? channel : (testSelect ? testSelect.value : "all");

  try {
    const resp = await apiFetch("/api/notifications/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel: selectedChannel }),
    });
    const data = await resp.json().catch(() => ({}));

    if (resp.status === 401) {
      showNotificationFeedback("Authentication required. Please sign in again.", false);
      return;
    }

    if (!resp.ok) {
      const errMsg =
        data.detail || data.message || `Server returned error (${resp.status})`;
      showNotificationFeedback(errMsg, false);
      return;
    }

    if (data.success) {
      showNotificationFeedback(
        data.message || `Test notification sent successfully to ${selectedChannel}!`,
        true
      );
    } else {
      showNotificationFeedback(
        data.message || `Failed to send test notification to ${selectedChannel}.`,
        false
      );
    }

    // Automatically refresh notification history so the new test log appears immediately
    await refreshNotificationHistory();
  } catch (err) {
    showNotificationFeedback(
      `Error sending test notification: ${err instanceof Error ? err.message : String(err)}`,
      false
    );
  } finally {
    btn.disabled = false;
    btn.classList.remove("loading");
    btn.innerHTML = originalHtml;
  }
}

const sendTestNotification = handleSendTestNotification;
if (typeof window !== "undefined") {
  window.sendTestNotification = sendTestNotification;
  window.handleSaveDiscordSettings = handleSaveDiscordSettings;
  window.saveNotificationCenterConfig = saveNotificationCenterConfig;
}

/**
 * Refresh notification history table via WebSocket or HTTP fallback.
 */
async function refreshNotificationHistory() {
  if (wsConnection && wsConnection.readyState === WebSocket.OPEN) {
    try {
      wsConnection.send(
        JSON.stringify({ action: "refresh_module", module: "notifications" })
      );
    } catch (_) {}
  }
  try {
    const resp = await apiFetch("/api/history?limit=20");
    if (resp.ok) {
      const historyData = await resp.json().catch(() => ({}));
      renderNotificationHistory(historyData);
      setModuleLastUpdated("notifications");
    }
  } catch (err) {
    console.warn("Failed to fetch updated notification history:", err);
  }
}

/**
 * Escape HTML to prevent XSS when rendering user or container data.
 */
function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

/**
 * Switch view panel between 'overview', 'raid', 'notifications', 'containers', 'services', 'processes', 'users', and 'audit'.
 */
function switchViewTab(tabName, updateHash = true) {
  const normTab = (tabName || "").toLowerCase();
  const isNotifications = normTab === "notifications";
  const isContainers = normTab === "containers";
  const isRaid = normTab === "raid" || normTab === "storage" || normTab === "disks";
  const isServices = normTab === "services";
  const isProcesses = normTab === "processes";
  const isUsers = normTab === "users";
  const isAudit = normTab === "audit";
  const isPackages = normTab === "packages" || normTab === "updates" || normTab === "dpkg" || normTab === "apt";
  const isProbes = normTab === "probes" || normTab === "sentinel" || normTab === "uptime";
  const isSecurity = normTab === "security" || normTab === "network" || normTab === "firewall";
  const isBackups = normTab === "backups" || normTab === "cron" || normTab === "scheduler";
  currentViewTab = isRaid
    ? "raid"
    : isSecurity
      ? "security"
      : isBackups
        ? "backups"
        : normTab || "overview";

  const overviewTab = document.getElementById("nav-tab-overview");
  const raidTab = document.getElementById("nav-tab-raid");
  const notificationsTab = document.getElementById("nav-tab-notifications");
  const containersTab = document.getElementById("nav-tab-containers");
  const probesTab = document.getElementById("nav-tab-probes");
  const servicesTab = document.getElementById("nav-tab-services");
  const securityTab = document.getElementById("nav-tab-security");
  const processesTab = document.getElementById("nav-tab-processes");
  const usersTab = document.getElementById("nav-tab-users");
  const auditTab = document.getElementById("nav-tab-audit");
  const packagesTab = document.getElementById("nav-tab-packages");
  const backupsTab = document.getElementById("nav-tab-backups");

  const overviewView = document.getElementById("view-overview");
  const raidView = document.getElementById("view-raid");
  const notificationsView = document.getElementById("view-notifications");
  const containersView = document.getElementById("view-containers");
  const probesView = document.getElementById("view-probes");
  const servicesView = document.getElementById("view-services");
  const securityView = document.getElementById("view-security");
  const processesView = document.getElementById("view-processes");
  const usersView = document.getElementById("view-users");
  const auditView = document.getElementById("view-audit");
  const packagesView = document.getElementById("view-packages");
  const backupsView = document.getElementById("view-backups");

  const pageTitle = document.getElementById("page-title");

  overviewTab?.classList.remove("active");
  overviewTab?.setAttribute("aria-selected", "false");
  raidTab?.classList.remove("active");
  raidTab?.setAttribute("aria-selected", "false");
  notificationsTab?.classList.remove("active");
  notificationsTab?.setAttribute("aria-selected", "false");
  containersTab?.classList.remove("active");
  containersTab?.setAttribute("aria-selected", "false");
  probesTab?.classList.remove("active");
  probesTab?.setAttribute("aria-selected", "false");
  servicesTab?.classList.remove("active");
  servicesTab?.setAttribute("aria-selected", "false");
  securityTab?.classList.remove("active");
  securityTab?.setAttribute("aria-selected", "false");
  processesTab?.classList.remove("active");
  processesTab?.setAttribute("aria-selected", "false");
  usersTab?.classList.remove("active");
  usersTab?.setAttribute("aria-selected", "false");
  auditTab?.classList.remove("active");
  auditTab?.setAttribute("aria-selected", "false");
  packagesTab?.classList.remove("active");
  packagesTab?.setAttribute("aria-selected", "false");
  backupsTab?.classList.remove("active");
  backupsTab?.setAttribute("aria-selected", "false");

  overviewView?.classList.add("hidden");
  raidView?.classList.add("hidden");
  notificationsView?.classList.add("hidden");
  containersView?.classList.add("hidden");
  probesView?.classList.add("hidden");
  servicesView?.classList.add("hidden");
  securityView?.classList.add("hidden");
  processesView?.classList.add("hidden");
  usersView?.classList.add("hidden");
  auditView?.classList.add("hidden");
  packagesView?.classList.add("hidden");
  backupsView?.classList.add("hidden");

  // Automatically close mobile sidebar if opened
  document.getElementById("app-sidebar")?.classList.remove("open");
  document.getElementById("app-sidebar")?.classList.remove("mobile-open");
  document.getElementById("sidebar-backdrop")?.classList.remove("active");
  document.getElementById("mobile-nav-backdrop")?.classList.remove("active");

  if (isContainers) {
    containersTab?.classList.add("active");
    containersTab?.setAttribute("aria-selected", "true");
    containersView?.classList.remove("hidden");
    if (pageTitle) pageTitle.textContent = "Container Management";

    if (updateHash && !window.location.hash.startsWith("#containers")) {
      history.replaceState(null, "", `#containers/${currentDockerSubtab}`);
    }

    const cachedDocker = typeof VigilLiveStore !== "undefined" ? VigilLiveStore.getState("docker") : null;
    if (cachedDocker) {
      renderDockerStatus(cachedDocker);
      if (Array.isArray(cachedDocker.containers || cachedDocker)) {
        renderDockerContainers(null, cachedDocker.containers || cachedDocker);
      }
      if (Array.isArray(cachedDocker.stacks)) {
        renderDockerStacks(cachedDocker.stacks);
      }
    }

    if (isAuthenticated) {
      fetchDockerStatus();
      refreshDockerSubtab(currentDockerSubtab);
      if (!cachedDockerUpdates || cachedDockerUpdates.length === 0) {
        fetchDockerUpdates({ background: true });
      }
    }
  } else if (isRaid) {
    raidTab?.classList.add("active");
    raidTab?.setAttribute("aria-selected", "true");
    raidView?.classList.remove("hidden");
    if (pageTitle) pageTitle.textContent = "Storage";

    if (updateHash && window.location.hash !== "#storage" && window.location.hash !== "#raid" && window.location.hash !== "#disks") {
      history.replaceState(null, "", "#storage");
    }

    const cachedRaid = typeof VigilLiveStore !== "undefined" ? VigilLiveStore.getState("raid") : null;
    if (cachedRaid) {
      renderRaidDetails(cachedRaid);
    }

    renderStorageMounts(cachedStorageDisks);
    if (isAuthenticated) {
      fetchSmartDisks();
    }
  } else if (isNotifications) {
    notificationsTab?.classList.add("active");
    notificationsTab?.setAttribute("aria-selected", "true");
    notificationsView?.classList.remove("hidden");
    if (pageTitle) pageTitle.textContent = "Notifications";

    if (updateHash && window.location.hash !== "#notifications") {
      history.replaceState(null, "", "#notifications");
    }

    if (isAuthenticated) {
      fetchNotificationCenterConfig();
    }
  } else if (isServices) {
    servicesTab?.classList.add("active");
    servicesTab?.setAttribute("aria-selected", "true");
    servicesView?.classList.remove("hidden");
    if (pageTitle) pageTitle.textContent = "System Services";

    if (updateHash && window.location.hash !== "#services") {
      history.replaceState(null, "", "#services");
    }

    if (isAuthenticated) {
      fetchSystemServices();
    }
  } else if (isProcesses) {
    processesTab?.classList.add("active");
    processesTab?.setAttribute("aria-selected", "true");
    processesView?.classList.remove("hidden");
    if (pageTitle) pageTitle.textContent = "Process Explorer";

    if (updateHash && window.location.hash !== "#processes") {
      history.replaceState(null, "", "#processes");
    }

    if (isAuthenticated) {
      fetchProcesses();
    }
  } else if (isUsers) {
    usersTab?.classList.add("active");
    usersTab?.setAttribute("aria-selected", "true");
    usersView?.classList.remove("hidden");
    if (pageTitle) pageTitle.textContent = "Users & Groups";

    if (updateHash && !window.location.hash.startsWith("#users")) {
      history.replaceState(null, "", "#users");
    }

    if (isAuthenticated) {
      switchUsersSubtab(currentUsersSubtab);
      fetchSystemUsers();
      fetchSystemGroups();
    }
  } else if (isAudit) {
    auditTab?.classList.add("active");
    auditTab?.setAttribute("aria-selected", "true");
    auditView?.classList.remove("hidden");
    if (pageTitle) pageTitle.textContent = "Security & Audit Logs";

    if (updateHash && window.location.hash !== "#audit") {
      history.replaceState(null, "", "#audit");
    }

    if (isAuthenticated) {
      fetchAuditLogs();
    }
  } else if (isPackages) {
    packagesTab?.classList.add("active");
    packagesTab?.setAttribute("aria-selected", "true");
    packagesView?.classList.remove("hidden");
    if (pageTitle) pageTitle.textContent = "Packages & System Updates";

    if (updateHash && !window.location.hash.startsWith("#packages") && !window.location.hash.startsWith("#updates")) {
      history.replaceState(null, "", "#packages");
    }

    if (isAuthenticated) {
      refreshPackagesView();
    }
  } else if (isProbes) {
    probesTab?.classList.add("active");
    probesTab?.setAttribute("aria-selected", "true");
    probesView?.classList.remove("hidden");
    if (pageTitle) pageTitle.textContent = "Uptime & Probes";

    if (updateHash && !window.location.hash.startsWith("#probes") && !window.location.hash.startsWith("#sentinel")) {
      history.replaceState(null, "", "#probes");
    }

    if (isAuthenticated) {
      fetchProbes();
    }
  } else if (isSecurity) {
    securityTab?.classList.add("active");
    securityTab?.setAttribute("aria-selected", "true");
    securityView?.classList.remove("hidden");
    if (pageTitle) pageTitle.textContent = "Network & Security";

    if (
      updateHash &&
      !window.location.hash.startsWith("#security") &&
      !window.location.hash.startsWith("#/security") &&
      !window.location.hash.startsWith("#network") &&
      !window.location.hash.startsWith("#/network") &&
      !window.location.hash.startsWith("#firewall") &&
      !window.location.hash.startsWith("#/firewall")
    ) {
      history.replaceState(null, "", "#/security");
    }

    if (isAuthenticated) {
      fetchNetworkSecurity();
    }
  } else if (isBackups) {
    backupsTab?.classList.add("active");
    backupsTab?.setAttribute("aria-selected", "true");
    backupsView?.classList.remove("hidden");
    if (pageTitle) pageTitle.textContent = "Backups & Cron Scheduler";

    if (
      updateHash &&
      !window.location.hash.startsWith("#backups") &&
      !window.location.hash.startsWith("#/backups") &&
      !window.location.hash.startsWith("#cron") &&
      !window.location.hash.startsWith("#/cron")
    ) {
      history.replaceState(null, "", "#backups");
    }

    if (isAuthenticated) {
      refreshBackupsAndCron();
    }
  } else {
    overviewTab?.classList.add("active");
    overviewTab?.setAttribute("aria-selected", "true");
    overviewView?.classList.remove("hidden");
    if (pageTitle) pageTitle.textContent = "System Overview";

    if (updateHash && window.location.hash !== "" && window.location.hash !== "#overview") {
      history.replaceState(null, "", "#overview");
    }

    const cachedSys = typeof VigilLiveStore !== "undefined" ? VigilLiveStore.getState("system") : null;
    if (cachedSys) {
      renderSystemMetrics(cachedSys);
    }
    const cachedRaid = typeof VigilLiveStore !== "undefined" ? VigilLiveStore.getState("raid") : null;
    if (cachedRaid) {
      renderRaidDetails(cachedRaid);
    }
  }

  sendWsSubscription(currentViewTab, currentDockerSubtab);
}

/**
 * Synchronize view tab state from location.hash or location.pathname.
 */
function syncTabFromHash() {
  const hash = (window.location.hash || "").replace(/^#\/?/, "").toLowerCase().trim();
  const path = (window.location.pathname || "").toLowerCase().replace(/^\/+|\/+$/g, "").trim();
  const route = hash || path;

  if (route.startsWith("containers")) {
    const parts = route.split("/");
    const subtab = parts[1] || currentDockerSubtab || "stacks";
    switchViewTab("containers", false);
    switchDockerSubtab(subtab, false);
  } else if (route === "raid" || route === "storage" || route === "disks") {
    switchViewTab("raid", false);
  } else if (route === "services" || route === "systemctl") {
    switchViewTab("services", false);
  } else if (route.startsWith("security") || route.startsWith("network") || route.startsWith("firewall")) {
    switchViewTab("security", false);
  } else if (route.startsWith("backups") || route.startsWith("cron") || route.startsWith("scheduler")) {
    switchViewTab("backups", false);
  } else if (route === "processes" || route === "proc" || route === "tasks") {
    switchViewTab("processes", false);
  } else if (route === "probes" || route === "sentinel" || route === "uptime") {
    switchViewTab("probes", false);
  } else if (route.startsWith("users") || route.startsWith("groups")) {
    switchViewTab("users", false);
    if (route.includes("groups")) {
      switchUsersSubtab("groups");
    } else {
      switchUsersSubtab("users");
    }
  } else if (route === "audit" || route === "audit-log") {
    switchViewTab("audit", false);
  } else if (route.startsWith("packages") || route.startsWith("updates") || route === "dpkg" || route === "apt") {
    switchViewTab("packages", false);
  } else if (route === "logs" || route === "vigil-logs" || route === "system-logs") {
    openVigilLogsModal();
  } else if (route === "notifications" || route === "notifications-logs") {
    switchViewTab("notifications", false);
  } else {
    switchViewTab("overview", false);
  }
}

/* =========================================================================
 * Services Summary Ribbon Helper
 * ========================================================================= */
function renderServicesSummary(summary) {
  if (!summary) return;
  const running = Number(summary.running) || 0;
  const failed = Number(summary.failed) || 0;
  const badge = document.getElementById("overview-services-badge");
  const summaryEl = document.getElementById("overview-services-summary");
  if (badge) {
    if (failed > 0) {
      setBadgeIfChanged(badge, null, "status-badge status-danger", `${failed} FAILED`);
    } else if (running > 0) {
      setBadgeIfChanged(badge, null, "status-badge status-ok", "ACTIVE");
    } else {
      setBadgeIfChanged(badge, null, "status-badge status-ok", "OK");
    }
  }
  if (summaryEl) {
    setTextIfChanged(summaryEl, `${running} Running, ${failed} Failed`);
  }
}

/* =========================================================================
 * Systemctl Services Monitoring View (#view-services)
 * ========================================================================= */
let cachedServicesData = null;
let servicesFilterState = "all";
let servicesSearchTerm = "";
let currentViewingService = null;

async function fetchSystemServices() {
  const refreshBtn = document.getElementById("refresh-services-btn");
  if (refreshBtn) refreshBtn.classList.add("loading");
  try {
    const resp = await apiFetch("/api/services");
    if (!resp.ok) {
      if (resp.status === 404) {
        throw new Error(
          "Endpoint not found (HTTP 404). If you just updated Vigil, please restart the Vigil service ('sudo systemctl restart vigil' or 'vigil restart') to load backend endpoints."
        );
      }
      throw new Error(`Failed to fetch services (HTTP ${resp.status})`);
    }
    const data = await resp.json();
    cachedServicesData = data;
    renderSystemServices(data);
  } catch (err) {
    console.error("fetchSystemServices error:", err);
    showToast({
      title: "Services Error",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  } finally {
    if (refreshBtn) refreshBtn.classList.remove("loading");
  }
}

function renderSystemServices(data) {
  if (!data) return;

  const lastUpdatedEl = document.getElementById("services-last-updated");
  if (lastUpdatedEl) {
    lastUpdatedEl.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  }

  const summary = data.summary || {};
  const totalCount = Number(summary.total) || (Array.isArray(data.services) ? data.services.length : 0);
  const runningCount = Number(summary.running) || 0;
  const failedCount = Number(summary.failed) || 0;
  const inactiveCount = Number(summary.inactive) || 0;

  const totalEl = document.getElementById("services-total-count");
  if (totalEl) totalEl.textContent = String(totalCount);

  const runningEl = document.getElementById("services-running-count");
  if (runningEl) runningEl.textContent = String(runningCount);

  const failedEl = document.getElementById("services-failed-count");
  if (failedEl) failedEl.textContent = String(failedCount);

  const failedBadge = document.getElementById("services-failed-badge");
  if (failedBadge) {
    if (failedCount > 0) {
      failedBadge.className = "status-badge status-danger";
    } else {
      failedBadge.className = "status-badge status-ok";
    }
  }

  const inactiveEl = document.getElementById("services-inactive-count");
  if (inactiveEl) inactiveEl.textContent = String(inactiveCount);

  renderServicesSummary(summary);

  const navBadge = document.getElementById("nav-tab-services-badge");
  if (navBadge) {
    if (failedCount > 0) {
      navBadge.textContent = `${failedCount}`;
      navBadge.className = "nav-tab-badge danger";
      navBadge.classList.remove("hidden");
    } else {
      navBadge.classList.add("hidden");
    }
  }

  // Filter & Render Table
  const tbody = document.getElementById("services-table-body");
  if (!tbody) return;

  const services = Array.isArray(data.services) ? data.services : [];
  const term = (servicesSearchTerm || "").toLowerCase().trim();
  const stateFilter = (servicesFilterState || "all").toLowerCase();

  const filtered = services.filter((s) => {
    if (stateFilter === "running") {
      if (s.active_state !== "active" || s.sub_state !== "running") return false;
    } else if (stateFilter === "failed") {
      if (s.active_state !== "failed") return false;
    } else if (stateFilter === "inactive") {
      if (!["inactive", "dead"].includes(s.active_state)) return false;
    }

    if (term) {
      const matchUnit = (s.unit || "").toLowerCase().includes(term);
      const matchName = (s.name || "").toLowerCase().includes(term);
      const matchDesc = (s.description || "").toLowerCase().includes(term);
      if (!matchUnit && !matchName && !matchDesc) return false;
    }
    return true;
  });

  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty-state">No matching services found.</td></tr>`;
    return;
  }

  tbody.innerHTML = filtered.map((s) => {
    const isFailed = s.active_state === "failed";
    const isActive = s.active_state === "active";
    let stateBadgeClass = "status-badge status-muted";
    if (isFailed) stateBadgeClass = "status-badge status-danger";
    else if (isActive) stateBadgeClass = "status-badge status-ok";

    const isRunning = s.sub_state === "running";
    const safeUnit = escapeHtml(s.unit || "");

    return `
      <tr>
        <td class="mono-text font-bold">${safeUnit}</td>
        <td><span class="${stateBadgeClass}">${escapeHtml(s.active_state || "unknown")}</span></td>
        <td><span class="kind-badge">${escapeHtml(s.sub_state || "-")}</span></td>
        <td class="text-secondary" style="max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(s.description || "-")}</td>
        <td>
          <div class="table-actions-cell">
            <button class="btn-action-sm btn-service-restart" data-service="${safeUnit}" title="Restart Service">Restart</button>
            ${isRunning
              ? `<button class="btn-action-sm btn-action-danger btn-service-stop" data-service="${safeUnit}" title="Stop Service">Stop</button>`
              : `<button class="btn-action-sm btn-action-success btn-service-start" data-service="${safeUnit}" title="Start Service">Start</button>`
            }
            <button class="btn-action-sm btn-service-logs" data-service="${safeUnit}" title="Inspect Journal Logs">Logs</button>
          </div>
        </td>
      </tr>
    `;
  }).join("");

  tbody.querySelectorAll(".btn-service-restart").forEach((btn) => {
    btn.addEventListener("click", () => handleServiceAction(btn.getAttribute("data-service"), "restart"));
  });
  tbody.querySelectorAll(".btn-service-stop").forEach((btn) => {
    btn.addEventListener("click", () => handleServiceAction(btn.getAttribute("data-service"), "stop"));
  });
  tbody.querySelectorAll(".btn-service-start").forEach((btn) => {
    btn.addEventListener("click", () => handleServiceAction(btn.getAttribute("data-service"), "start"));
  });
  tbody.querySelectorAll(".btn-service-logs").forEach((btn) => {
    btn.addEventListener("click", () => openServiceLogsModal(btn.getAttribute("data-service")));
  });
}

async function handleServiceAction(serviceName, action) {
  if (!serviceName || !action) return;
  try {
    const resp = await apiFetch("/api/services/" + encodeURIComponent(serviceName) + "/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    const result = await resp.json();
    if (!resp.ok) {
      throw new Error(result.detail || `Action failed with HTTP ${resp.status}`);
    }
    showToast({
      title: "Service Action Executed",
      message: `${serviceName}: ${action} completed successfully`,
      type: "success",
    });
    fetchSystemServices();
  } catch (err) {
    showToast({
      title: "Action Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  }
}

async function openVigilLogsModal() {
  await openServiceLogsModal("vigil.service");
}

async function openServiceLogsModal(serviceName) {
  currentViewingService = serviceName;
  const modal = document.getElementById("service-logs-modal");
  const title = document.getElementById("service-logs-title");
  const content = document.getElementById("service-logs-content");
  const feedback = document.getElementById("service-logs-feedback");

  const isVigil = serviceName === "vigil.service" || serviceName === "vigil";
  if (title) title.textContent = isVigil ? "Vigil System & Service Logs" : `Journal Logs: ${serviceName}`;
  if (content) content.textContent = "Loading journald execution logs...";
  if (feedback) feedback.classList.add("hidden");
  if (modal) modal.classList.remove("hidden");

  await fetchServiceLogs(serviceName);
}

function closeServiceLogsModal() {
  const modal = document.getElementById("service-logs-modal");
  if (modal) modal.classList.add("hidden");
  currentViewingService = null;
}

async function fetchServiceLogs(serviceName) {
  if (!serviceName) return;
  const content = document.getElementById("service-logs-content");
  const feedback = document.getElementById("service-logs-feedback");
  const linesSelect = document.getElementById("service-logs-lines-select");
  const linesVal = linesSelect ? linesSelect.value : "100";

  try {
    const isVigil = serviceName === "vigil.service" || serviceName === "vigil";
    const endpoint = isVigil
      ? `/api/vigil/logs?lines=${encodeURIComponent(linesVal)}`
      : `/api/services/${encodeURIComponent(serviceName)}/logs?lines=${encodeURIComponent(linesVal)}`;

    const resp = await apiFetch(endpoint);
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.detail || `HTTP ${resp.status}`);
    }
    const lines = Array.isArray(data.logs) ? data.logs : [];
    if (content) {
      content.textContent = lines.length > 0 ? lines.join("\n") : "No recent journal entries recorded for this service unit.";
      content.scrollTop = content.scrollHeight;
    }
  } catch (err) {
    if (feedback) {
      feedback.textContent = `Error reading journal: ${err instanceof Error ? err.message : String(err)}`;
      feedback.classList.remove("hidden");
    }
  }
}

/* =========================================================================
 * Process Explorer View (#view-processes)
 * ========================================================================= */
let cachedProcessesData = null;
let processesSort = "cpu";
let processesLimit = 50;
let processesSearchTerm = "";
let processToKill = null;

async function fetchProcesses() {
  const refreshBtn = document.getElementById("refresh-processes-btn");
  if (refreshBtn) refreshBtn.classList.add("loading");
  try {
    const query = "?sort=" + encodeURIComponent(processesSort) + "&limit=" + encodeURIComponent(processesLimit) + (processesSearchTerm ? "&q=" + encodeURIComponent(processesSearchTerm) : "");
    const resp = await apiFetch("/api/processes" + query);
    if (!resp.ok) {
      if (resp.status === 404) {
        throw new Error(
          "Endpoint not found (HTTP 404). If you just updated Vigil, please restart the Vigil service ('sudo systemctl restart vigil' or 'vigil restart') to load backend endpoints."
        );
      }
      throw new Error(`Failed to fetch processes (HTTP ${resp.status})`);
    }
    const data = await resp.json();
    cachedProcessesData = data;
    renderProcesses(data);
  } catch (err) {
    console.error("fetchProcesses error:", err);
    showToast({
      title: "Process Explorer Error",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  } finally {
    if (refreshBtn) refreshBtn.classList.remove("loading");
  }
}

function renderProcesses(data) {
  if (!data) return;

  const lastUpdatedEl = document.getElementById("processes-last-updated");
  if (lastUpdatedEl) {
    lastUpdatedEl.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  }

  const summary = data.summary || {};
  const totalProcesses = Number(summary.total_processes) || 0;
  const runningCount = Number(summary.running_count) || 0;
  const sleepingCount = Number(summary.sleeping_count) || 0;

  const totalEl = document.getElementById("proc-total-count");
  if (totalEl) totalEl.textContent = String(totalProcesses);

  const runningEl = document.getElementById("proc-running-count");
  if (runningEl) runningEl.textContent = String(runningCount);

  const sleepingEl = document.getElementById("proc-sleeping-count");
  if (sleepingEl) sleepingEl.textContent = String(sleepingCount);

  const topCpuEl = document.getElementById("proc-top-cpu");
  if (topCpuEl) topCpuEl.textContent = summary.top_cpu_process || "--";

  const topMemEl = document.getElementById("proc-top-memory");
  if (topMemEl) topMemEl.textContent = summary.top_memory_process || "--";

  const tbody = document.getElementById("processes-table-body");
  if (!tbody) return;

  const procs = Array.isArray(data.processes) ? data.processes : [];
  if (procs.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-state">No matching processes found.</td></tr>`;
    return;
  }

  tbody.innerHTML = procs.map((p) => {
    const cpuPct = Math.max(0, Math.min(100, Number(p.cpu_percent) || 0));
    const memPct = Math.max(0, Math.min(100, Number(p.memory_percent) || 0));

    let cpuBarClass = "proc-mini-bar-fill";
    if (cpuPct >= 75) cpuBarClass += " danger";
    else if (cpuPct >= 40) cpuBarClass += " warn";

    let memBarClass = "proc-mini-bar-fill";
    if (memPct >= 75) memBarClass += " danger";
    else if (memPct >= 40) memBarClass += " warn";

    const isRunning = (p.status || "").toLowerCase() === "running";
    const statusBadgeClass = isRunning ? "status-badge status-ok" : "status-badge status-muted";

    return `
      <tr>
        <td class="mono-text font-bold">${p.pid}</td>
        <td class="font-bold">${escapeHtml(p.name || "unknown")}</td>
        <td class="text-secondary mono-text">${escapeHtml(p.username || "-")}</td>
        <td class="proc-metric-cell">
          <div class="proc-metric-row">
            <span class="mono-text">${cpuPct.toFixed(1)}%</span>
          </div>
          <div class="proc-mini-bar">
            <div class="${cpuBarClass}" style="width: ${cpuPct.toFixed(1)}%"></div>
          </div>
        </td>
        <td class="proc-metric-cell">
          <div class="proc-metric-row">
            <span class="mono-text">${memPct.toFixed(1)}%</span>
          </div>
          <div class="proc-mini-bar">
            <div class="${memBarClass}" style="width: ${memPct.toFixed(1)}%"></div>
          </div>
        </td>
        <td><span class="${statusBadgeClass}">${escapeHtml(p.status || "-")}</span></td>
        <td class="mono-text">${p.num_threads || 1}</td>
        <td>
          <div class="table-actions-cell">
            <button class="btn-action-sm btn-action-danger btn-kill-proc" data-pid="${p.pid}" data-name="${escapeHtml(p.name || "")}" title="Terminate Process">Kill</button>
          </div>
        </td>
      </tr>
    `;
  }).join("");

  tbody.querySelectorAll(".btn-kill-proc").forEach((btn) => {
    btn.addEventListener("click", () => {
      const pid = parseInt(btn.getAttribute("data-pid") || "0", 10);
      const name = btn.getAttribute("data-name") || "";
      openProcessKillModal({ pid, name });
    });
  });
}

function openProcessKillModal(proc) {
  processToKill = proc;
  const modal = document.getElementById("process-kill-modal");
  const pidEl = document.getElementById("kill-process-pid");
  const nameEl = document.getElementById("kill-process-name");
  const feedback = document.getElementById("kill-process-feedback");
  const sigSelect = document.getElementById("kill-signal-select");

  if (pidEl) pidEl.textContent = String(proc.pid);
  if (nameEl) nameEl.textContent = proc.name || "--";
  if (sigSelect) sigSelect.value = "SIGTERM";
  if (feedback) feedback.classList.add("hidden");
  if (modal) modal.classList.remove("hidden");
}

function closeProcessKillModal() {
  const modal = document.getElementById("process-kill-modal");
  if (modal) modal.classList.add("hidden");
  processToKill = null;
}

async function handleKillProcess() {
  if (!processToKill) return;
  const sigSelect = document.getElementById("kill-signal-select");
  const signal = sigSelect ? sigSelect.value : "SIGTERM";
  const feedback = document.getElementById("kill-process-feedback");

  try {
    const resp = await apiFetch("/api/processes/" + processToKill.pid + "/kill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ signal }),
    });
    const result = await resp.json();
    if (!resp.ok) {
      throw new Error(result.detail || `HTTP ${resp.status}`);
    }
    showToast({
      title: "Process Terminated",
      message: `PID ${processToKill.pid} (${processToKill.name}) sent ${signal}`,
      type: "warning",
    });
    closeProcessKillModal();
    fetchProcesses();
  } catch (err) {
    if (feedback) {
      feedback.textContent = err instanceof Error ? err.message : String(err);
      feedback.classList.remove("hidden");
    }
  }
}

/* =========================================================================
 * Linux Users & Groups View (#view-users)
 * ========================================================================= */
let currentUsersSubtab = "users";
let cachedUsersData = null;
let cachedGroupsData = null;
let usersRegularOnly = true;
let usersSearchTerm = "";
let groupsSearchTerm = "";
let editingUser = null;
let managingGroup = null;

function switchUsersSubtab(subtab) {
  currentUsersSubtab = subtab;
  const isUsers = subtab === "users";
  const usersBtn = document.getElementById("subtab-users-btn");
  const groupsBtn = document.getElementById("subtab-groups-btn");
  const usersPanel = document.getElementById("users-subtab-panel");
  const groupsPanel = document.getElementById("groups-subtab-panel");

  if (isUsers) {
    usersBtn?.classList.add("active");
    usersBtn?.setAttribute("aria-selected", "true");
    groupsBtn?.classList.remove("active");
    groupsBtn?.setAttribute("aria-selected", "false");
    usersPanel?.classList.remove("hidden");
    groupsPanel?.classList.add("hidden");
    fetchSystemUsers();
  } else {
    groupsBtn?.classList.add("active");
    groupsBtn?.setAttribute("aria-selected", "true");
    usersBtn?.classList.remove("active");
    usersBtn?.setAttribute("aria-selected", "false");
    groupsPanel?.classList.remove("hidden");
    usersPanel?.classList.add("hidden");
    fetchSystemGroups();
  }
}

async function fetchSystemUsers() {
  const refreshBtn = document.getElementById("refresh-users-btn");
  if (refreshBtn) refreshBtn.classList.add("loading");
  try {
    const resp = await apiFetch("/api/system/users");
    if (!resp.ok) {
      if (resp.status === 404) {
        throw new Error(
          "Endpoint not found (HTTP 404). If you just updated Vigil, please restart the Vigil service ('sudo systemctl restart vigil' or 'vigil restart') to load backend endpoints."
        );
      }
      throw new Error(`Failed to fetch users (HTTP ${resp.status})`);
    }
    const data = await resp.json();
    cachedUsersData = data;
    renderSystemUsers(data);
  } catch (err) {
    console.error("fetchSystemUsers error:", err);
    showToast({
      title: "Users Error",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  } finally {
    if (refreshBtn) refreshBtn.classList.remove("loading");
  }
}

function renderSystemUsers(data) {
  if (!data) return;

  const lastUpdatedEl = document.getElementById("users-last-updated");
  if (lastUpdatedEl) {
    lastUpdatedEl.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  }

  const summary = data.summary || {};
  const totalCount = Number(summary.total_users) || 0;
  const regularCount = Number(summary.regular_users_count) || 0;
  const systemCount = Number(summary.system_users_count) || 0;

  const totalEl = document.getElementById("users-total-count");
  if (totalEl) totalEl.textContent = String(totalCount);

  const regEl = document.getElementById("users-regular-count");
  if (regEl) regEl.textContent = String(regularCount);

  const sysEl = document.getElementById("users-system-count");
  if (sysEl) sysEl.textContent = String(systemCount);

  const badgeEl = document.getElementById("subtab-users-badge");
  if (badgeEl) badgeEl.textContent = String(regularCount);

  const tbody = document.getElementById("users-table-body");
  if (!tbody) return;

  const users = Array.isArray(data.users) ? data.users : [];
  const term = (usersSearchTerm || "").toLowerCase().trim();

  const filtered = users.filter((u) => {
    if (usersRegularOnly && u.is_system) return false;
    if (term) {
      const matchUser = (u.username || "").toLowerCase().includes(term);
      const matchName = (u.name || "").toLowerCase().includes(term);
      const matchShell = (u.shell || "").toLowerCase().includes(term);
      const matchHome = (u.home || "").toLowerCase().includes(term);
      if (!matchUser && !matchName && !matchShell && !matchHome) return false;
    }
    return true;
  });

  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-state">No matching users found.</td></tr>`;
    return;
  }

  tbody.innerHTML = filtered.map((u) => {
    const isSys = Boolean(u.is_system);
    const typeBadge = isSys
      ? `<span class="kind-badge">System</span>`
      : `<span class="status-badge status-ok">Human</span>`;

    const groups = Array.isArray(u.groups) ? u.groups : [];
    const groupChips = groups.length > 0
      ? groups.map((g) => `<span class="chip-tag">${escapeHtml(g)}</span>`).join("")
      : `<span class="text-muted">-</span>`;

    const safeUser = escapeHtml(u.username || "");

    return `
      <tr>
        <td class="mono-text font-bold">${safeUser}</td>
        <td class="mono-text text-secondary">${u.uid} / ${u.gid}</td>
        <td>${escapeHtml(u.name || "-")}</td>
        <td class="mono-text text-secondary">${escapeHtml(u.home || "-")}</td>
        <td class="mono-text">${escapeHtml(u.shell || "-")}</td>
        <td><div class="chips-wrap">${groupChips}</div></td>
        <td>${typeBadge}</td>
        <td>
          <div class="table-actions-cell">
            <button class="btn-action-sm btn-edit-user" data-user='${JSON.stringify({ username: u.username, shell: u.shell, groups: u.groups })}'>Edit</button>
            ${isSys ? "" : `<button class="btn-action-sm btn-action-danger btn-delete-user" data-username="${safeUser}">Delete</button>`}
          </div>
        </td>
      </tr>
    `;
  }).join("");

  tbody.querySelectorAll(".btn-edit-user").forEach((btn) => {
    btn.addEventListener("click", () => {
      try {
        const udata = JSON.parse(btn.getAttribute("data-user") || "{}");
        openEditUserModal(udata);
      } catch (e) {}
    });
  });

  tbody.querySelectorAll(".btn-delete-user").forEach((btn) => {
    btn.addEventListener("click", () => {
      const username = btn.getAttribute("data-username");
      if (username) handleDeleteUser(username);
    });
  });
}

async function fetchSystemGroups() {
  const refreshBtn = document.getElementById("refresh-users-btn");
  if (refreshBtn) refreshBtn.classList.add("loading");
  try {
    const resp = await apiFetch("/api/system/groups");
    if (!resp.ok) {
      if (resp.status === 404) {
        throw new Error(
          "Endpoint not found (HTTP 404). If you just updated Vigil, please restart the Vigil service ('sudo systemctl restart vigil' or 'vigil restart') to load backend endpoints."
        );
      }
      throw new Error(`Failed to fetch groups (HTTP ${resp.status})`);
    }
    const data = await resp.json();
    cachedGroupsData = data;
    renderSystemGroups(data);
  } catch (err) {
    console.error("fetchSystemGroups error:", err);
    showToast({
      title: "Groups Error",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  } finally {
    if (refreshBtn) refreshBtn.classList.remove("loading");
  }
}

function renderSystemGroups(data) {
  if (!data) return;

  const summary = data.summary || {};
  const totalCount = Number(summary.total_groups) || 0;
  const regularCount = Number(summary.regular_groups_count) || 0;

  const totalEl = document.getElementById("groups-total-count");
  if (totalEl) totalEl.textContent = String(totalCount);

  const regEl = document.getElementById("groups-regular-count");
  if (regEl) regEl.textContent = String(regularCount);

  const badgeEl = document.getElementById("subtab-groups-badge");
  if (badgeEl) badgeEl.textContent = String(regularCount);

  const tbody = document.getElementById("groups-table-body");
  if (!tbody) return;

  const groups = Array.isArray(data.groups) ? data.groups : [];
  const term = (groupsSearchTerm || "").toLowerCase().trim();

  const filtered = groups.filter((g) => {
    if (term) {
      const matchName = (g.name || "").toLowerCase().includes(term);
      const matchMember = Array.isArray(g.members) && g.members.some((m) => m.toLowerCase().includes(term));
      if (!matchName && !matchMember) return false;
    }
    return true;
  });

  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty-state">No matching groups found.</td></tr>`;
    return;
  }

  tbody.innerHTML = filtered.map((g) => {
    const members = Array.isArray(g.members) ? g.members : [];
    const memberChips = members.length > 0
      ? members.map((m) => `<span class="chip-tag chip-primary">${escapeHtml(m)}</span>`).join("")
      : `<span class="text-muted">No members</span>`;

    const safeGroup = escapeHtml(g.name || "");
    const isSys = Boolean(g.is_system);

    return `
      <tr>
        <td class="mono-text font-bold">${safeGroup}</td>
        <td class="mono-text text-secondary">${g.gid}</td>
        <td><div class="chips-wrap">${memberChips}</div></td>
        <td>
          <div class="table-actions-cell">
            <button class="btn-action-sm btn-manage-members" data-group="${safeGroup}">Members</button>
            ${isSys ? "" : `<button class="btn-action-sm btn-action-danger btn-delete-group" data-group="${safeGroup}">Delete</button>`}
          </div>
        </td>
      </tr>
    `;
  }).join("");

  tbody.querySelectorAll(".btn-manage-members").forEach((btn) => {
    btn.addEventListener("click", () => {
      const gname = btn.getAttribute("data-group");
      if (gname) openGroupMemberModal(gname);
    });
  });

  tbody.querySelectorAll(".btn-delete-group").forEach((btn) => {
    btn.addEventListener("click", () => {
      const gname = btn.getAttribute("data-group");
      if (gname) handleDeleteGroup(gname);
    });
  });
}

function openAddUserModal() {
  const modal = document.getElementById("modal-add-user");
  const form = document.getElementById("form-add-user");
  const feedback = document.getElementById("add-user-feedback");
  if (form) form.reset();
  const shellInput = document.getElementById("input-add-user-shell");
  if (shellInput) shellInput.value = "/bin/bash";
  const homeCheck = document.getElementById("input-add-user-home");
  if (homeCheck) homeCheck.checked = true;
  if (feedback) feedback.classList.add("hidden");
  if (modal) modal.classList.remove("hidden");
}

function closeAddUserModal() {
  const modal = document.getElementById("modal-add-user");
  if (modal) modal.classList.add("hidden");
}

async function handleAddUserSubmit(e) {
  e.preventDefault();
  const usernameInput = document.getElementById("input-add-user-name");
  const shellInput = document.getElementById("input-add-user-shell");
  const groupsInput = document.getElementById("input-add-user-groups");
  const homeCheck = document.getElementById("input-add-user-home");
  const feedback = document.getElementById("add-user-feedback");

  const username = (usernameInput?.value || "").trim();
  const shell = (shellInput?.value || "").trim() || "/bin/bash";
  const rawGroups = (groupsInput?.value || "").trim();
  const groups = rawGroups ? rawGroups.split(",").map((s) => s.trim()).filter(Boolean) : [];
  const create_home = homeCheck ? homeCheck.checked : true;

  try {
    const resp = await apiFetch("/api/system/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, shell, groups, create_home }),
    });
    const result = await resp.json();
    if (!resp.ok) {
      throw new Error(result.detail || `HTTP ${resp.status}`);
    }
    showToast({
      title: "User Created",
      message: `User '${username}' created successfully`,
      type: "success",
    });
    closeAddUserModal();
    fetchSystemUsers();
  } catch (err) {
    if (feedback) {
      feedback.textContent = err instanceof Error ? err.message : String(err);
      feedback.classList.remove("hidden");
    }
  }
}

function openEditUserModal(udata) {
  editingUser = udata;
  const modal = document.getElementById("modal-edit-user");
  const nameDisp = document.getElementById("edit-user-name-display");
  const shellInput = document.getElementById("input-edit-user-shell");
  const groupsInput = document.getElementById("input-edit-user-groups");
  const lockedCheck = document.getElementById("input-edit-user-locked");
  const feedback = document.getElementById("edit-user-feedback");

  if (nameDisp) nameDisp.textContent = udata.username;
  if (shellInput) shellInput.value = udata.shell || "/bin/bash";
  if (groupsInput) groupsInput.value = Array.isArray(udata.groups) ? udata.groups.join(",") : "";
  if (lockedCheck) lockedCheck.checked = false;
  if (feedback) feedback.classList.add("hidden");
  if (modal) modal.classList.remove("hidden");
}

function closeEditUserModal() {
  const modal = document.getElementById("modal-edit-user");
  if (modal) modal.classList.add("hidden");
  editingUser = null;
}

async function handleEditUserSubmit(e) {
  e.preventDefault();
  if (!editingUser) return;
  const shellInput = document.getElementById("input-edit-user-shell");
  const groupsInput = document.getElementById("input-edit-user-groups");
  const lockedCheck = document.getElementById("input-edit-user-locked");
  const feedback = document.getElementById("edit-user-feedback");

  const shell = (shellInput?.value || "").trim();
  const rawGroups = (groupsInput?.value || "").trim();
  const groups = rawGroups ? rawGroups.split(",").map((s) => s.trim()).filter(Boolean) : [];
  const locked = lockedCheck ? lockedCheck.checked : false;

  try {
    const resp = await apiFetch("/api/system/users/" + encodeURIComponent(editingUser.username), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ shell, groups, locked }),
    });
    const result = await resp.json();
    if (!resp.ok) {
      throw new Error(result.detail || `HTTP ${resp.status}`);
    }
    showToast({
      title: "User Updated",
      message: `User '${editingUser.username}' updated successfully`,
      type: "success",
    });
    closeEditUserModal();
    fetchSystemUsers();
  } catch (err) {
    if (feedback) {
      feedback.textContent = err instanceof Error ? err.message : String(err);
      feedback.classList.remove("hidden");
    }
  }
}

async function handleDeleteUser(username) {
  if (!confirm(`Are you sure you want to delete user '${username}'?`)) return;
  try {
    const resp = await apiFetch("/api/system/users/" + encodeURIComponent(username), {
      method: "DELETE",
    });
    const result = await resp.json();
    if (!resp.ok) {
      throw new Error(result.detail || `HTTP ${resp.status}`);
    }
    showToast({
      title: "User Deleted",
      message: `User '${username}' deleted successfully`,
      type: "warning",
    });
    fetchSystemUsers();
  } catch (err) {
    showToast({
      title: "Delete Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  }
}

function openAddGroupModal() {
  const modal = document.getElementById("modal-add-group");
  const form = document.getElementById("form-add-group");
  const feedback = document.getElementById("add-group-feedback");
  if (form) form.reset();
  if (feedback) feedback.classList.add("hidden");
  if (modal) modal.classList.remove("hidden");
}

function closeAddGroupModal() {
  const modal = document.getElementById("modal-add-group");
  if (modal) modal.classList.add("hidden");
}

async function handleAddGroupSubmit(e) {
  e.preventDefault();
  const nameInput = document.getElementById("input-add-group-name");
  const gidInput = document.getElementById("input-add-group-gid");
  const feedback = document.getElementById("add-group-feedback");

  const name = (nameInput?.value || "").trim();
  const gidVal = gidInput?.value ? parseInt(gidInput.value, 10) : null;

  try {
    const payload = { name };
    if (gidVal !== null && !isNaN(gidVal)) payload.gid = gidVal;
    const resp = await apiFetch("/api/system/groups", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await resp.json();
    if (!resp.ok) {
      throw new Error(result.detail || `HTTP ${resp.status}`);
    }
    showToast({
      title: "Group Created",
      message: `Group '${name}' created successfully`,
      type: "success",
    });
    closeAddGroupModal();
    fetchSystemGroups();
  } catch (err) {
    if (feedback) {
      feedback.textContent = err instanceof Error ? err.message : String(err);
      feedback.classList.remove("hidden");
    }
  }
}

function openGroupMemberModal(groupname) {
  managingGroup = { name: groupname };
  const modal = document.getElementById("modal-group-member");
  const form = document.getElementById("form-group-member");
  const nameDisp = document.getElementById("member-group-name-display");
  const feedback = document.getElementById("group-member-feedback");
  if (form) form.reset();
  if (nameDisp) nameDisp.textContent = groupname;
  if (feedback) feedback.classList.add("hidden");
  if (modal) modal.classList.remove("hidden");
}

function closeGroupMemberModal() {
  const modal = document.getElementById("modal-group-member");
  if (modal) modal.classList.add("hidden");
  managingGroup = null;
}

async function handleGroupMemberSubmit(e) {
  e.preventDefault();
  if (!managingGroup) return;
  const actionSelect = document.getElementById("input-member-action");
  const usernameInput = document.getElementById("input-member-username");
  const feedback = document.getElementById("group-member-feedback");

  const action = actionSelect?.value || "add";
  const username = (usernameInput?.value || "").trim();

  try {
    const resp = await apiFetch("/api/system/groups/" + encodeURIComponent(managingGroup.name) + "/members", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, username }),
    });
    const result = await resp.json();
    if (!resp.ok) {
      throw new Error(result.detail || `HTTP ${resp.status}`);
    }
    showToast({
      title: "Membership Updated",
      message: `User '${username}' ${action}ed to group '${managingGroup.name}'`,
      type: "success",
    });
    closeGroupMemberModal();
    fetchSystemGroups();
  } catch (err) {
    if (feedback) {
      feedback.textContent = err instanceof Error ? err.message : String(err);
      feedback.classList.remove("hidden");
    }
  }
}

async function handleDeleteGroup(groupname) {
  if (!confirm(`Are you sure you want to delete group '${groupname}'?`)) return;
  try {
    const resp = await apiFetch("/api/system/groups/" + encodeURIComponent(groupname), {
      method: "DELETE",
    });
    const result = await resp.json();
    if (!resp.ok) {
      throw new Error(result.detail || `HTTP ${resp.status}`);
    }
    showToast({
      title: "Group Deleted",
      message: `Group '${groupname}' deleted successfully`,
      type: "warning",
    });
    fetchSystemGroups();
  } catch (err) {
    showToast({
      title: "Delete Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  }
}

/* =========================================================================
 * Security & Audit Logs View (#view-audit)
 * ========================================================================= */
let cachedAuditData = null;
let auditSeverityFilter = "";
let auditSearchTerm = "";
let auditLimit = 50;
let auditOffset = 0;

let currentAuditSubtab = "security";

function switchAuditSubtab(subtab) {
  currentAuditSubtab = subtab;
  const isSecurity = subtab === "security";
  const secBtn = document.getElementById("subtab-audit-security-btn");
  const alertsBtn = document.getElementById("subtab-audit-alerts-btn");
  const secPanel = document.getElementById("audit-security-subtab-panel");
  const alertsPanel = document.getElementById("audit-alerts-subtab-panel");

  if (isSecurity) {
    secBtn?.classList.add("active");
    secBtn?.setAttribute("aria-selected", "true");
    alertsBtn?.classList.remove("active");
    alertsBtn?.setAttribute("aria-selected", "false");
    secPanel?.classList.remove("hidden");
    alertsPanel?.classList.add("hidden");
    fetchAuditLogs();
  } else {
    alertsBtn?.classList.add("active");
    alertsBtn?.setAttribute("aria-selected", "true");
    secBtn?.classList.remove("active");
    secBtn?.setAttribute("aria-selected", "false");
    alertsPanel?.classList.remove("hidden");
    secPanel?.classList.add("hidden");
    fetchNotificationHistory();
  }
}

async function fetchAuditLogs() {
  const refreshBtn = document.getElementById("refresh-audit-btn");
  if (refreshBtn) refreshBtn.classList.add("loading");
  try {
    const query = "?limit=" + encodeURIComponent(auditLimit) + "&offset=" + encodeURIComponent(auditOffset) + (auditSeverityFilter ? "&severity=" + encodeURIComponent(auditSeverityFilter) : "") + (auditSearchTerm ? "&q=" + encodeURIComponent(auditSearchTerm) : "");
    const resp = await apiFetch("/api/audit/logs" + query);
    if (!resp.ok) {
      if (resp.status === 404) {
        throw new Error(
          "Endpoint not found (HTTP 404). If you just updated Vigil, please restart the Vigil service ('sudo systemctl restart vigil' or 'vigil restart') to load backend endpoints."
        );
      }
      throw new Error(`Failed to fetch audit logs (HTTP ${resp.status})`);
    }
    const data = await resp.json();
    cachedAuditData = data;
    renderAuditLogs(data);
  } catch (err) {
    console.error("fetchAuditLogs error:", err);
    showToast({
      title: "Audit Logs Error",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  } finally {
    if (refreshBtn) refreshBtn.classList.remove("loading");
  }
}

function renderAuditLogs(data) {
  if (!data) return;

  const lastUpdatedEl = document.getElementById("audit-last-updated");
  if (lastUpdatedEl) {
    lastUpdatedEl.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  }

  const logs = Array.isArray(data.logs) ? data.logs : [];
  const total = Number(data.total) || logs.length;

  const totalEl = document.getElementById("audit-total-count");
  if (totalEl) totalEl.textContent = String(total);
  const secBadge = document.getElementById("subtab-audit-security-badge");
  if (secBadge) secBadge.textContent = String(total);

  const securityCount = logs.filter((l) => ["warning", "security"].includes(String(l.severity || "").toLowerCase())).length;
  const secEl = document.getElementById("audit-security-count");
  if (secEl) {
    secEl.textContent = `${securityCount} Flagged`;
    if (securityCount > 0) {
      secEl.className = "status-badge status-warning";
    } else {
      secEl.className = "status-badge status-ok";
    }
  }

  const tbody = document.getElementById("audit-table-body");
  if (!tbody) return;

  if (logs.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-state">No security audit logs recorded.</td></tr>`;
    return;
  }

  tbody.innerHTML = logs.map((log) => {
    const sev = String(log.severity || "info").toLowerCase();
    let badgeClass = "badge-audit-info";
    if (sev === "warning") badgeClass = "badge-audit-warning";
    else if (sev === "security") badgeClass = "badge-audit-security";

    const hasMeta = Boolean(log.metadata && Object.keys(log.metadata).length > 0);
    const metaJson = hasMeta ? JSON.stringify(log.metadata, null, 2) : "";

    return `
      <tr>
        <td class="mono-text text-secondary">${formatTimestampLocal(log.timestamp)}</td>
        <td><span class="status-badge ${badgeClass}">${sev.toUpperCase()}</span></td>
        <td><span class="kind-badge font-bold">${escapeHtml(log.event_type || "-")}</span></td>
        <td class="mono-text font-bold">${escapeHtml(log.actor || "system")}</td>
        <td class="mono-text text-secondary">${escapeHtml(log.ip_address || "--")}</td>
        <td>
          <div style="font-size: 0.82rem;">${escapeHtml(log.description || "-")}</div>
          ${hasMeta ? `<pre class="audit-meta-pre hidden" id="meta-log-${log.id}">${escapeHtml(metaJson)}</pre>` : ""}
        </td>
        <td>
          <div class="table-actions-cell">
            ${hasMeta
              ? `<button class="btn-action-sm btn-toggle-meta" data-id="${log.id}">Details</button>`
              : `<span class="text-muted" style="font-size: 0.75rem;">-</span>`
            }
          </div>
        </td>
      </tr>
    `;
  }).join("");

  tbody.querySelectorAll(".btn-toggle-meta").forEach((btn) => {
    btn.addEventListener("click", () => {
      const lid = btn.getAttribute("data-id");
      const pre = document.getElementById(`meta-log-${lid}`);
      if (pre) pre.classList.toggle("hidden");
    });
  });
}

/**
 * Switch Docker sub-tabs ('stacks', 'containers', 'images', 'volumes', 'networks', 'updates')
 */
function switchDockerSubtab(subtabName, updateHash = true) {
  const validSubtabs = ["stacks", "containers", "images", "volumes", "networks", "updates"];
  if (!validSubtabs.includes(subtabName)) {
    subtabName = "stacks";
  }
  currentDockerSubtab = subtabName;

  validSubtabs.forEach((st) => {
    const tabEl = document.getElementById(`subtab-docker-${st}`);
    const viewEl = document.getElementById(`subview-docker-${st}`);
    if (st === subtabName) {
      tabEl?.classList.add("active");
      tabEl?.setAttribute("aria-selected", "true");
      viewEl?.classList.remove("hidden");
    } else {
      tabEl?.classList.remove("active");
      tabEl?.setAttribute("aria-selected", "false");
      viewEl?.classList.add("hidden");
    }
  });

  if (updateHash && window.location.hash.startsWith("#containers")) {
    history.replaceState(null, "", `#containers/${subtabName}`);
  }

  if (isAuthenticated) {
    refreshDockerSubtab(subtabName);
  }

  sendWsSubscription(currentViewTab, currentDockerSubtab);
}

/**
 * Check if the active or specified Docker subtab request is currently in-flight.
 */
function isDockerSubtabInFlight(subtabName = currentDockerSubtab) {
  switch (subtabName) {
    case "stacks":
      return isFetchingDockerStacks;
    case "containers":
      return isFetchingDockerContainers;
    case "images":
      return isFetchingDockerImages;
    case "volumes":
      return isFetchingDockerVolumes;
    case "networks":
      return isFetchingDockerNetworks;
    case "updates":
      return isFetchingDockerUpdates;
    default:
      return false;
  }
}

/**
 * Throttled subtab refresh to prevent rapid WebSocket ticks from spamming HTTP requests.
 * Only the active "containers" subtab receives continuous live tick updates.
 * Updates, stacks, images, volumes, and networks are decoupled and fetched on tab switch
 * or after mutation actions, avoiding tab flickering and continuous heavy polling.
 */
function throttledRefreshDockerSubtab(subtabName = currentDockerSubtab) {
  if (subtabName !== "containers") return;
  if (isDockerSubtabInFlight("containers")) return;
  const now = Date.now();
  const elapsed = now - lastDockerSubtabFetchTime;
  if (elapsed >= DOCKER_SUBTAB_THROTTLE_MS) {
    if (dockerSubtabThrottleTimer) {
      clearTimeout(dockerSubtabThrottleTimer);
      dockerSubtabThrottleTimer = null;
    }
    refreshDockerSubtab("containers");
  } else if (!dockerSubtabThrottleTimer) {
    dockerSubtabThrottleTimer = setTimeout(() => {
      dockerSubtabThrottleTimer = null;
      if (currentViewTab === "containers" && currentDockerSubtab === "containers" && !isDockerSubtabInFlight("containers")) {
        refreshDockerSubtab("containers");
      }
    }, DOCKER_SUBTAB_THROTTLE_MS - elapsed);
  }
}

/**
 * Fetch data for active Docker subtab.
 */
function refreshDockerSubtab(subtabName) {
  lastDockerSubtabFetchTime = Date.now();
  switch (subtabName) {
    case "stacks":
      fetchDockerStacks();
      break;
    case "containers":
      fetchDockerContainers();
      break;
    case "images":
      fetchDockerImages();
      break;
    case "volumes":
      fetchDockerVolumes();
      break;
    case "networks":
      fetchDockerNetworks();
      break;
    case "updates":
      fetchDockerUpdatesConfig();
      fetchUpdateSchedule();
      if (!cachedDockerUpdates || cachedDockerUpdates.length === 0) {
        fetchDockerUpdates();
      } else {
        renderDockerUpdates(cachedDockerUpdates);
      }
      break;
  }
}

/**
 * Render Docker daemon status, quick stats, and navigation/subtab badges.
 */
function renderDockerStatus(data) {
  if (!data || typeof data !== "object") return;
  dockerStatusCache = data;

  const daemonBadge = document.getElementById("docker-daemon-badge");
  const installBanner = document.getElementById("docker-install-banner");
  const stoppedBanner = document.getElementById("docker-stopped-banner");
  const versionInfo = document.getElementById("docker-version-info");
  const lastUpdated = document.getElementById("docker-last-updated");

  if (daemonBadge) {
    daemonBadge.className = "status-badge";
    if (!data.installed) {
      daemonBadge.classList.add("status-failed");
      daemonBadge.textContent = "NOT INSTALLED";
    } else if (!data.running) {
      daemonBadge.classList.add("status-degraded");
      daemonBadge.textContent = "STOPPED";
    } else {
      daemonBadge.classList.add("status-clean");
      daemonBadge.textContent = "RUNNING";
    }
  }

  if (installBanner) {
    if (!data.installed) {
      installBanner.classList.remove("hidden");
    } else {
      installBanner.classList.add("hidden");
    }
  }

  if (stoppedBanner) {
    if (data.installed && !data.running) {
      stoppedBanner.classList.remove("hidden");
    } else {
      stoppedBanner.classList.add("hidden");
    }
  }

  if (versionInfo) {
    const v = data.version || "--";
    const cv = data.compose_version || "--";
    versionInfo.textContent = `Docker: ${v} | Compose: ${cv}`;
  }

  if (lastUpdated) {
    const now = new Date();
    lastUpdated.textContent = `Updated ${formatTimeOnly(now)}`;
  }

  const stats = data.stats || {};
  const stacksCount = stats.stacks ?? 0;
  const containersCount = stats.containers ?? 0;
  const runningCount = stats.running ?? 0;
  const stoppedCount = stats.stopped ?? 0;
  const imagesCount = stats.images ?? 0;
  const volumesCount = stats.volumes ?? 0;
  const networksCount = stats.networks ?? 0;

  const sVal = document.getElementById("docker-stat-stacks");
  const cVal = document.getElementById("docker-stat-containers");
  const rVal = document.getElementById("docker-stat-running");
  const stVal = document.getElementById("docker-stat-stopped");
  const cDetail = document.getElementById("docker-stat-containers-detail");
  const iVal = document.getElementById("docker-stat-images");
  const vVal = document.getElementById("docker-stat-volumes");
  const nVal = document.getElementById("docker-stat-networks");

  if (sVal) sVal.textContent = stacksCount;
  if (cVal) cVal.textContent = containersCount;
  if (rVal) rVal.textContent = runningCount;
  if (stVal) stVal.textContent = stoppedCount;
  if (cDetail && (!rVal || !stVal)) {
    cDetail.textContent = `${runningCount} running, ${stoppedCount} stopped`;
  }
  if (iVal) iVal.textContent = imagesCount;
  if (vVal) vVal.textContent = volumesCount;
  if (nVal) nVal.textContent = networksCount;

  // Main navigation tab badge: active (running) containers
  const navBadge = document.getElementById("nav-tab-containers-badge");
  if (navBadge) {
    navBadge.textContent = runningCount;
  }

  // Overview summary card
  const overviewDockerBadge = document.getElementById("overview-docker-badge");
  if (overviewDockerBadge) {
    let targetClass = "status-badge status-clean";
    let targetText = "RUNNING";
    if (!data.installed) {
      targetClass = "status-badge status-failed";
      targetText = "NOT INSTALLED";
    } else if (!data.running) {
      targetClass = "status-badge status-degraded";
      targetText = "STOPPED";
    }
    setBadgeIfChanged(overviewDockerBadge, null, targetClass, targetText);
  }
  const overviewDockerSummary = document.getElementById("overview-docker-summary");
  if (overviewDockerSummary) {
    setTextIfChanged(overviewDockerSummary, `${runningCount}/${containersCount} containers running • ${stacksCount} stacks`);
  }

  // Subtab badges
  const bStacks = document.getElementById("subtab-badge-stacks");
  const bContainers = document.getElementById("subtab-badge-containers");
  const bImages = document.getElementById("subtab-badge-images");
  const bVolumes = document.getElementById("subtab-badge-volumes");
  const bNetworks = document.getElementById("subtab-badge-networks");
  const bUpdates = document.getElementById("subtab-badge-updates");

  if (bStacks) bStacks.textContent = stacksCount;
  if (bContainers) bContainers.textContent = containersCount;
  if (bImages) bImages.textContent = imagesCount;
  if (bVolumes) bVolumes.textContent = volumesCount;
  if (bNetworks) bNetworks.textContent = networksCount;
  if (bUpdates && stats.updates !== undefined && stats.updates !== null) {
    bUpdates.textContent = stats.updates;
    if (stats.updates > 0) {
      bUpdates.classList.remove("hidden");
    } else {
      bUpdates.classList.add("hidden");
    }
  }

  const refreshBtn = document.getElementById("refresh-docker-btn");
  if (refreshBtn) refreshBtn.classList.remove("loading");
}

/**
 * Fetch Docker daemon status and quick stats.
 */
async function fetchDockerStatus() {
  if (isFetchingDockerStatus) return;
  isFetchingDockerStatus = true;
  const refreshBtn = document.getElementById("refresh-docker-btn");
  if (refreshBtn) refreshBtn.classList.add("loading");

  try {
    const resp = await apiFetch("/api/docker/status");
    if (!resp.ok) return;
    const data = await resp.json();
    renderDockerStatus(data);
  } catch (err) {
    console.warn("Failed to fetch Docker status:", err);
  } finally {
    isFetchingDockerStatus = false;
    if (refreshBtn) refreshBtn.classList.remove("loading");
  }
}

/**
 * Trigger Docker installation script when missing.
 */
async function handleInstallDocker() {
  const btn = document.getElementById("btn-install-docker");
  const feedback = document.getElementById("docker-install-feedback");
  if (btn) btn.disabled = true;

  if (feedback) {
    feedback.className = "auth-alert";
    feedback.textContent = "Triggering Docker installation script in background...";
    feedback.classList.remove("hidden");
  }

  try {
    const resp = await apiFetch("/api/docker/install", { method: "POST" });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      if (feedback) {
        feedback.classList.add("feedback-success");
        feedback.textContent = data.message || "Docker installation initiated. Checking status...";
      }
      setTimeout(fetchDockerStatus, 4000);
    } else {
      if (feedback) {
        feedback.classList.add("feedback-error");
        feedback.textContent = data.detail || "Failed to trigger Docker installation.";
      }
    }
  } catch (err) {
    if (feedback) {
      feedback.classList.add("feedback-error");
      feedback.textContent = `Error: ${err.message}`;
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

/**
 * Close any open actions dropdown menus across the dashboard.
 */
function closeAllActionsDropdowns() {
  document.querySelectorAll(".actions-dropdown-menu.show").forEach((menu) => {
    menu.classList.remove("show");
  });
}

/**
 * Global dropdown toggle, outside-click, and Escape key listener.
 */
function initActionsDropdowns() {
  document.addEventListener("click", (e) => {
    const moreBtn = e.target.closest(".btn-action-more");
    if (moreBtn) {
      e.stopPropagation();
      const dropdown = moreBtn.closest(".actions-dropdown");
      const menu = dropdown ? dropdown.querySelector(".actions-dropdown-menu") : null;
      const isAlreadyOpen = menu ? menu.classList.contains("show") : false;

      // Close any other open dropdown when opening
      closeAllActionsDropdowns();

      if (menu && !isAlreadyOpen) {
        menu.classList.add("show");
      }
      return;
    }

    const itemBtn = e.target.closest(".actions-dropdown-item");
    if (itemBtn) {
      // Ensure clicking any action item inside the dropdown closes the dropdown immediately
      closeAllActionsDropdowns();
      return;
    }

    // Close on click outside
    if (!e.target.closest(".actions-dropdown")) {
      closeAllActionsDropdowns();
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeAllActionsDropdowns();
    }
  });
}

/**
 * Render Docker Stacks table rows.
 */
function renderDockerStacks(stacks = cachedDockerStacks) {
  const tbody = document.getElementById("docker-stacks-tbody");
  if (!tbody) return;

  if (Array.isArray(stacks)) {
    cachedDockerStacks = stacks;
  }

  // Preserve open dropdown menu under the user's cursor during live stream updates
  if (tbody.querySelector?.(".actions-dropdown-menu.show")) {
    return;
  }

  if (!stacks || stacks.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty-state">No Compose stacks configured yet. Click 'Deploy New Stack' to add one.</td></tr>`;
    return;
  }

  tbody.innerHTML = stacks.map((s) => {
    const isRunning = s.status === "running" || (Number(s.running_count || 0) > 0);
    const statusClass = isRunning ? "status-clean" : (s.status === "stopped" || s.status === "down" ? "status-degraded" : "status-none");
    const isManaged = Boolean(s.managed);
    const managedBadge = isManaged
      ? `<span class="kind-badge kind-weekly" title="Managed in SQLite">Managed</span>`
      : `<span class="kind-badge" style="background: rgba(148, 163, 184, 0.15); color: #94a3b8;">External</span>`;
    
    const safeName = escapeHtml(s.name);
    let actionsContent = "";
    if (isManaged) {
      let quickActionsHtml = "";
      let dropdownActionsHtml = "";

      if (isRunning) {
        quickActionsHtml = `
          <button type="button" class="btn-action" data-stack-action="restart" data-stack="${safeName}">Restart</button>
          <button type="button" class="btn-action" data-stack-action="edit" data-stack="${safeName}">Edit</button>
        `;
        dropdownActionsHtml = `
          <button type="button" class="actions-dropdown-item" data-stack-action="stop" data-stack="${safeName}">Stop</button>
          <button type="button" class="actions-dropdown-item" data-stack-action="pull" data-stack="${safeName}">Pull</button>
          <button type="button" class="actions-dropdown-item" data-stack-action="down" data-stack="${safeName}">Down</button>
          <div class="actions-dropdown-divider"></div>
          <button type="button" class="actions-dropdown-item danger" data-stack-action="delete" data-stack="${safeName}">Delete</button>
        `;
      } else {
        quickActionsHtml = `
          <button type="button" class="btn-action btn-action-success" data-stack-action="start" data-stack="${safeName}">Start</button>
          <button type="button" class="btn-action" data-stack-action="edit" data-stack="${safeName}">Edit</button>
        `;
        dropdownActionsHtml = `
          <button type="button" class="actions-dropdown-item" data-stack-action="pull" data-stack="${safeName}">Pull</button>
          <button type="button" class="actions-dropdown-item" data-stack-action="down" data-stack="${safeName}">Down</button>
          <div class="actions-dropdown-divider"></div>
          <button type="button" class="actions-dropdown-item danger" data-stack-action="delete" data-stack="${safeName}">Delete</button>
        `;
      }

      actionsContent = `
        <div class="btn-action-group">
          ${quickActionsHtml}
          <div class="actions-dropdown">
            <button type="button" class="btn-action btn-action-more" aria-label="More actions" title="More actions">⋯</button>
            <div class="actions-dropdown-menu">
              ${dropdownActionsHtml}
            </div>
          </div>
        </div>
      `;
    } else {
      actionsContent = `<span class="kind-badge" style="background: rgba(148, 163, 184, 0.15); color: #94a3b8;" title="Managed outside SystemAlerts. Manage individual containers in Containers tab.">Read-Only (External)</span>`;
    }

    return `<tr>
      <td><strong>${safeName}</strong></td>
      <td>
        <span class="status-badge ${statusClass}">
          <span class="status-dot"></span>
          ${escapeHtml((s.status || "unknown").replace(/\s*\(\d+(?:\/\d+)?\)/g, "").trim())}
        </span>
      </td>
      <td>${Number(s.running_count || 0)} / ${Number(s.containers_count || 0)} running</td>
      <td>${managedBadge}</td>
      <td class="actions-col">${actionsContent}</td>
    </tr>`;
  }).join("");

  tbody.querySelectorAll("button[data-stack-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      closeAllActionsDropdowns();
      const action = btn.getAttribute("data-stack-action");
      const stackName = btn.getAttribute("data-stack");
      if (action === "edit") {
        openStackEditor(stackName);
      } else if (action === "delete") {
        handleDeleteStack(stackName);
      } else {
        handleStackAction(stackName, action, btn);
      }
    });
  });
}

/**
 * Fetch and render Docker Stacks.
 */
async function fetchDockerStacks() {
  const tbody = document.getElementById("docker-stacks-tbody");
  if (!tbody) return;
  if (isFetchingDockerStacks) return;
  isFetchingDockerStacks = true;

  try {
    const resp = await apiFetch("/api/docker/stacks");
    if (!resp.ok) return;
    const data = await resp.json();
    const stacks = data.stacks || [];
    cachedDockerStacks = stacks;

    const stacksKey = JSON.stringify(stacks);
    if (lastStacksRenderKey === stacksKey && tbody.children.length > 0) {
      return;
    }
    lastStacksRenderKey = stacksKey;

    renderDockerStacks(stacks);
  } catch (err) {
    console.warn("Failed to fetch Docker stacks:", err);
  } finally {
    isFetchingDockerStacks = false;
  }
}

/**
 * Execute stack action: start, stop, restart, down, pull.
 */
async function handleStackAction(stackName, action, buttonEl) {
  if (buttonEl) {
    buttonEl.disabled = true;
    buttonEl.textContent = "...";
  }

  try {
    const resp = await apiFetch(`/api/docker/stacks/${encodeURIComponent(stackName)}/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}));
      alert(`Stack action '${action}' failed: ${errData.detail || resp.statusText}`);
    }
  } catch (err) {
    alert(`Error executing stack action: ${err.message}`);
  } finally {
    lastStacksRenderKey = "";
    fetchDockerStacks();
    fetchDockerStatus();
  }
}

/**
 * Delete Compose stack with container teardown.
 */
async function handleDeleteStack(stackName) {
  if (!confirm(`Are you sure you want to delete stack "${stackName}"? This will tear down all its containers.`)) {
    return;
  }

  try {
    const resp = await apiFetch(`/api/docker/stacks/${encodeURIComponent(stackName)}?down=true`, {
      method: "DELETE",
    });
    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}));
      alert(`Failed to delete stack: ${errData.detail || resp.statusText}`);
    }
  } catch (err) {
    alert(`Error deleting stack: ${err.message}`);
  } finally {
    lastStacksRenderKey = "";
    fetchDockerStacks();
    fetchDockerStatus();
  }
}

/**
 * Ace Code Editor instances & state for Stack Editor Modal.
 */
let composeAceEditor = null;
let envAceEditor = null;
let isSyncingStackEditor = false;
let lastSyncedComposeAceValue = null;
let lastSyncedEnvAceValue = null;
let activeStackEnvMode = "kv";

/**
 * Update the Stack Editor footer status bar cursor position.
 */
function updateStackEditorStatusbar(line = 1, col = 1) {
  const posEl = document.getElementById("stack-editor-cursor-pos");
  if (posEl) {
    posEl.textContent = `Ln ${Math.max(1, line)}, Col ${Math.max(1, col)}`;
  }
}

/**
 * Set gutter annotations on the Compose YAML Ace Editor session if available.
 */
function setStackEditorAnnotations(annotations = []) {
  if (
    composeAceEditor &&
    composeAceEditor.session &&
    typeof composeAceEditor.session.setAnnotations === "function"
  ) {
    try {
      composeAceEditor.session.setAnnotations(annotations);
    } catch (err) {
      console.debug("Failed to set Ace annotations:", err);
    }
  }
}

/**
 * Initialize Ace Editor instances on #stack-yaml-ace-editor and #stack-env-ace-editor
 * with graceful fallback to <textarea> when Ace is not loaded (offline / Node VM tests).
 */
function initStackCodeEditors() {
  if (typeof window === "undefined" || !window.ace || typeof window.ace.edit !== "function") {
    return false;
  }

  const yamlContainer = document.getElementById("stack-yaml-ace-editor");
  const yamlWrapper = document.getElementById("stack-yaml-editor-wrapper");
  const yamlInput = document.getElementById("stack-yaml-input");

  if (yamlContainer && !composeAceEditor) {
    try {
      composeAceEditor = window.ace.edit("stack-yaml-ace-editor");
      composeAceEditor.setTheme("ace/theme/one_dark");
      composeAceEditor.session.setMode("ace/mode/yaml");
      composeAceEditor.setOptions({
        tabSize: 2,
        useSoftTabs: true,
        displayIndentGuides: true,
        showFoldWidgets: true,
        showLineNumbers: true,
        highlightActiveLine: true,
        showPrintMargin: false,
      });

      if (yamlInput && yamlInput.value) {
        composeAceEditor.setValue(yamlInput.value, -1);
        lastSyncedComposeAceValue = yamlInput.value;
      } else {
        lastSyncedComposeAceValue = composeAceEditor.getValue();
      }

      composeAceEditor.session.on("change", () => {
        if (isSyncingStackEditor) return;
        const val = composeAceEditor.getValue();
        lastSyncedComposeAceValue = val;
        const inputEl = document.getElementById("stack-yaml-input");
        if (inputEl) {
          inputEl.value = val;
        }
      });

      if (composeAceEditor.selection && typeof composeAceEditor.selection.on === "function") {
        composeAceEditor.selection.on("changeCursor", () => {
          const pos = composeAceEditor.getCursorPosition();
          if (pos) {
            updateStackEditorStatusbar(pos.row + 1, pos.column + 1);
          }
        });
      }

      if (composeAceEditor.commands && typeof composeAceEditor.commands.addCommand === "function") {
        composeAceEditor.commands.addCommand({
          name: "formatComposeYaml",
          bindKey: { win: "Ctrl-Shift-F", mac: "Command-Shift-F" },
          exec: () => {
            formatStackYaml();
          },
        });
      }

      yamlWrapper?.classList.add("ace-active");
    } catch (err) {
      console.warn("Ace Editor YAML init fallback:", err);
      composeAceEditor = null;
      yamlWrapper?.classList.remove("ace-active");
    }
  }

  const envContainer = document.getElementById("stack-env-ace-editor");
  const envWrapper = document.getElementById("stack-env-editor-wrapper");
  const envInput = document.getElementById("stack-env-input");

  if (envContainer && !envAceEditor) {
    try {
      envAceEditor = window.ace.edit("stack-env-ace-editor");
      envAceEditor.setTheme("ace/theme/one_dark");
      envAceEditor.session.setMode("ace/mode/sh");
      envAceEditor.setOptions({
        tabSize: 2,
        useSoftTabs: true,
        displayIndentGuides: true,
        showFoldWidgets: true,
        showLineNumbers: true,
        highlightActiveLine: true,
        showPrintMargin: false,
      });

      if (envInput && envInput.value) {
        envAceEditor.setValue(envInput.value, -1);
        lastSyncedEnvAceValue = envInput.value;
      } else {
        lastSyncedEnvAceValue = envAceEditor.getValue();
      }

      envAceEditor.session.on("change", () => {
        if (isSyncingStackEditor) return;
        const val = envAceEditor.getValue();
        lastSyncedEnvAceValue = val;
        const inputEl = document.getElementById("stack-env-input");
        if (inputEl) {
          inputEl.value = val;
        }
      });

      if (envAceEditor.commands && typeof envAceEditor.commands.addCommand === "function") {
        envAceEditor.commands.addCommand({
          name: "formatEnvContent",
          bindKey: { win: "Ctrl-Shift-F", mac: "Command-Shift-F" },
          exec: () => {
            formatStackYaml();
          },
        });
      }

      envWrapper?.classList.add("ace-active");
    } catch (err) {
      console.warn("Ace Editor .env init fallback:", err);
      envAceEditor = null;
      envWrapper?.classList.remove("ace-active");
    }
  }

  return !!(composeAceEditor || envAceEditor);
}

/**
 * Get current Compose YAML value from synced textarea / Ace editor.
 */
function getStackYamlValue() {
  const yamlInput = document.getElementById("stack-yaml-input");
  if (composeAceEditor && typeof composeAceEditor.getValue === "function") {
    if (yamlInput && yamlInput.value !== lastSyncedComposeAceValue) {
      isSyncingStackEditor = true;
      try {
        composeAceEditor.setValue(yamlInput.value, -1);
        lastSyncedComposeAceValue = yamlInput.value;
      } finally {
        isSyncingStackEditor = false;
      }
      return yamlInput.value;
    }
    const aceVal = composeAceEditor.getValue();
    if (yamlInput && yamlInput.value !== aceVal) {
      yamlInput.value = aceVal;
    }
    return aceVal;
  }
  return yamlInput?.value || "";
}

/**
 * Set Compose YAML value in both #stack-yaml-input and Ace editor.
 */
function setStackYamlValue(val) {
  const strVal = val ?? "";
  const yamlInput = document.getElementById("stack-yaml-input");
  if (yamlInput) {
    yamlInput.value = strVal;
  }
  if (composeAceEditor && typeof composeAceEditor.setValue === "function") {
    isSyncingStackEditor = true;
    try {
      composeAceEditor.setValue(strVal, -1);
      lastSyncedComposeAceValue = strVal;
      composeAceEditor.resize(true);
    } finally {
      isSyncingStackEditor = false;
    }
  }
}

/**
 * Parse raw .env file text into an array of { name: string, value: string }.
 * Ignores empty lines and comments (#). Strips surrounding quotes.
 */
function parseEnvTextToVariables(envText) {
  if (!envText || typeof envText !== "string") {
    return [{ name: "", value: "" }];
  }

  const lines = envText.split(/\r?\n/);
  const vars = [];

  for (let rawLine of lines) {
    let line = rawLine.trim();
    if (!line || line.startsWith("#")) {
      continue;
    }

    if (line.startsWith("export ")) {
      line = line.slice(7).trim();
    }

    const eqIdx = line.indexOf("=");
    if (eqIdx === -1) {
      if (line) {
        vars.push({ name: line, value: "" });
      }
    } else {
      const name = line.slice(0, eqIdx).trim();
      let value = line.slice(eqIdx + 1).trim();

      if (
        (value.startsWith('"') && value.endsWith('"') && value.length >= 2) ||
        (value.startsWith("'") && value.endsWith("'") && value.length >= 2)
      ) {
        value = value.slice(1, -1);
      }

      vars.push({ name, value });
    }
  }

  return vars.length > 0 ? vars : [{ name: "", value: "" }];
}

/**
 * Serialize an array of { name, value } back to canonical .env text.
 * Quotes values containing spaces, newlines, or quotes.
 */
function serializeVariablesToEnvText(vars) {
  if (!Array.isArray(vars)) return "";

  const lines = [];
  for (const item of vars) {
    if (!item) continue;
    const name = (item.name || "").trim();
    if (!name) continue;

    let val = item.value !== undefined && item.value !== null ? String(item.value) : "";
    if (val.includes(" ") || val.includes("\n") || val.includes("\t") || val.includes('"')) {
      val = `"${val.replace(/"/g, '\\"')}"`;
    }
    lines.push(`${name}=${val}`);
  }

  return lines.join("\n");
}

/**
 * Read current key-value pairs directly from the DOM table rows.
 */
function readVariablesFromKvTable() {
  const tbody = document.getElementById("stack-env-kv-tbody");
  if (!tbody || typeof tbody.querySelectorAll !== "function") return [];

  const rows = tbody.querySelectorAll(".env-kv-row");
  const vars = [];
  if (rows && typeof rows.forEach === "function") {
    rows.forEach((row) => {
      const keyInput = row.querySelector ? row.querySelector(".env-kv-key-input") : null;
      const valInput = row.querySelector ? row.querySelector(".env-kv-val-input") : null;
      if (keyInput && valInput) {
        vars.push({
          name: keyInput.value,
          value: valInput.value,
        });
      }
    });
  }
  return vars;
}

/**
 * Update the badge counter in the toolbar (e.g. "3 variables" or "1 of 3 variables").
 */
function updateEnvVariableCountBadge() {
  const tbody = document.getElementById("stack-env-kv-tbody");
  const searchInput = document.getElementById("stack-env-search");
  const query = (searchInput?.value || "").toLowerCase().trim();
  if (query) {
    handleEnvSearchInput({ target: searchInput });
    return;
  }
  const badge = document.getElementById("stack-env-count-badge");
  if (!tbody || !badge || typeof tbody.querySelectorAll !== "function") return;
  const rows = tbody.querySelectorAll(".env-kv-row");
  let count = 0;
  if (rows && typeof rows.forEach === "function") {
    rows.forEach((row) => {
      const key = row.querySelector ? row.querySelector(".env-kv-key-input")?.value?.trim() : null;
      if (key) count++;
    });
  }
  badge.textContent = `${count} variable${count === 1 ? "" : "s"}`;
}

/**
 * Update eye button icon and title for password mask toggle.
 */
function updateEyeIcon(btn, isPassword) {
  if (isPassword) {
    btn.title = "Show value";
    btn.setAttribute("aria-label", "Show value");
    btn.innerHTML = `
      <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path>
        <circle cx="12" cy="12" r="3"></circle>
      </svg>
    `;
  } else {
    btn.title = "Hide value";
    btn.setAttribute("aria-label", "Hide value");
    btn.innerHTML = `
      <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path>
        <line x1="1" y1="21" x2="23" y2="3"></line>
      </svg>
    `;
  }
}

/**
 * Create a single table row element for the Key-Value editor.
 */
function createEnvVariableRowElement(name = "", value = "") {
  const tr = document.createElement("tr");
  tr.className = "env-kv-row";

  // Key td
  const tdKey = document.createElement("td");
  const keyInput = document.createElement("input");
  keyInput.type = "text";
  keyInput.className = "env-kv-key-input";
  keyInput.placeholder = "VARIABLE_NAME";
  keyInput.value = name;
  keyInput.autocomplete = "off";
  keyInput.spellcheck = false;
  keyInput.addEventListener("input", () => {
    updateEnvVariableCountBadge();
  });
  tdKey.appendChild(keyInput);

  // Value td with secret eye toggle
  const tdVal = document.createElement("td");
  const valGroup = document.createElement("div");
  valGroup.className = "env-val-input-group";

  const valInput = document.createElement("input");
  valInput.type = "text";
  valInput.className = "env-kv-val-input";
  valInput.placeholder = "value";
  valInput.value = value;
  valInput.autocomplete = "off";
  valInput.spellcheck = false;
  valInput.addEventListener("input", () => {
    updateEnvVariableCountBadge();
  });

  const toggleBtn = document.createElement("button");
  toggleBtn.type = "button";
  toggleBtn.className = "btn-icon-subtle btn-toggle-secret";
  updateEyeIcon(toggleBtn, false);

  toggleBtn.addEventListener("click", () => {
    const isPw = valInput.type === "password";
    valInput.type = isPw ? "text" : "password";
    updateEyeIcon(toggleBtn, valInput.type === "password");
  });

  valGroup.appendChild(valInput);
  valGroup.appendChild(toggleBtn);
  tdVal.appendChild(valGroup);

  // Action td
  const tdAction = document.createElement("td");
  tdAction.style.textAlign = "right";

  const removeBtn = document.createElement("button");
  removeBtn.type = "button";
  removeBtn.className = "btn-remove-var";
  removeBtn.title = "Remove variable";
  removeBtn.setAttribute("aria-label", "Remove variable");
  removeBtn.textContent = "✕";
  removeBtn.addEventListener("click", () => {
    const tbody = tr.parentElement;
    tr.remove();
    updateEnvVariableCountBadge();
    if (tbody && tbody.querySelectorAll(".env-kv-row").length === 0) {
      addEnvVariableRow("", "");
    }
  });

  tdAction.appendChild(removeBtn);

  tr.appendChild(tdKey);
  tr.appendChild(tdVal);
  tr.appendChild(tdAction);

  return tr;
}

/**
 * Render Key-Value rows into #stack-env-kv-tbody.
 */
function renderEnvKeyValueTable(vars) {
  const tbody = document.getElementById("stack-env-kv-tbody");
  if (!tbody) return;

  tbody.innerHTML = "";
  const list = Array.isArray(vars) && vars.length > 0 ? vars : [{ name: "", value: "" }];

  for (const item of list) {
    const tr = createEnvVariableRowElement(item.name || "", item.value || "");
    tbody.appendChild(tr);
  }

  updateEnvVariableCountBadge();
}

/**
 * Add a new blank or pre-filled row to the visual Key-Value table.
 */
function addEnvVariableRow(name = "", value = "") {
  const tbody = document.getElementById("stack-env-kv-tbody");
  if (!tbody) return;

  const tr = createEnvVariableRowElement(name, value);
  tbody.appendChild(tr);
  updateEnvVariableCountBadge();

  const keyInput = tr.querySelector(".env-kv-key-input");
  keyInput?.focus();
}

/**
 * Filter variables in the Key-Value table by search query.
 */
function handleEnvSearchInput(e) {
  const query = (e?.target?.value || "").toLowerCase().trim();
  const tbody = document.getElementById("stack-env-kv-tbody");
  if (!tbody || typeof tbody.querySelectorAll !== "function") return;

  const rows = tbody.querySelectorAll(".env-kv-row");
  let visibleCount = 0;
  let totalCount = 0;

  if (rows && typeof rows.forEach === "function") {
    rows.forEach((row) => {
      const keyInput = row.querySelector ? row.querySelector(".env-kv-key-input") : null;
      const valInput = row.querySelector ? row.querySelector(".env-kv-val-input") : null;
      const key = (keyInput?.value || "").toLowerCase();
      const val = (valInput?.value || "").toLowerCase();
      const hasName = Boolean(keyInput?.value?.trim());

      if (hasName) {
        totalCount++;
      }

      if (!query || key.includes(query) || val.includes(query)) {
        if (row.style) row.style.display = "";
        if (hasName) visibleCount++;
      } else {
        if (row.style) row.style.display = "none";
      }
    });
  }

  const badge = document.getElementById("stack-env-count-badge");
  if (badge) {
    if (query) {
      badge.textContent = `${visibleCount} of ${totalCount} variables`;
    } else {
      badge.textContent = `${totalCount} variable${totalCount === 1 ? "" : "s"}`;
    }
  }
}

/**
 * Switch Stack Environment Variable Editor Mode: 'kv' or 'raw'.
 */
function switchStackEnvMode(mode) {
  activeStackEnvMode = mode === "raw" ? "raw" : "kv";

  const kvBtn = document.getElementById("stack-env-mode-kv-btn");
  const rawBtn = document.getElementById("stack-env-mode-raw-btn");
  const kvContainer = document.getElementById("stack-env-kv-container");
  const rawContainer = document.getElementById("stack-env-raw-container");

  if (activeStackEnvMode === "raw") {
    kvBtn?.classList.remove("active");
    kvBtn?.setAttribute("aria-selected", "false");
    rawBtn?.classList.add("active");
    rawBtn?.setAttribute("aria-selected", "true");

    // Sync KV rows -> Ace Editor
    const vars = readVariablesFromKvTable();
    const serialized = serializeVariablesToEnvText(vars);
    const envInput = document.getElementById("stack-env-input");
    if (envInput) envInput.value = serialized;
    if (envAceEditor && typeof envAceEditor.setValue === "function") {
      isSyncingStackEditor = true;
      try {
        envAceEditor.setValue(serialized, -1);
        lastSyncedEnvAceValue = serialized;
      } finally {
        isSyncingStackEditor = false;
      }
    }

    kvContainer?.classList.add("hidden");
    rawContainer?.classList.remove("hidden");
    if (envAceEditor && typeof envAceEditor.resize === "function") {
      envAceEditor.resize(true);
    }
  } else {
    rawBtn?.classList.remove("active");
    rawBtn?.setAttribute("aria-selected", "false");
    kvBtn?.classList.add("active");
    kvBtn?.setAttribute("aria-selected", "true");

    // Sync Ace Editor -> KV rows
    let currentRaw = "";
    if (envAceEditor && typeof envAceEditor.getValue === "function") {
      currentRaw = envAceEditor.getValue();
    } else {
      currentRaw = document.getElementById("stack-env-input")?.value || "";
    }
    const parsed = parseEnvTextToVariables(currentRaw);
    renderEnvKeyValueTable(parsed);

    rawContainer?.classList.add("hidden");
    kvContainer?.classList.remove("hidden");
  }
}

/**
 * Import environment variables from a local .env file.
 */
function handleImportEnvFile(e) {
  const file = e.target.files && e.target.files[0];
  if (!file) return;

  const reader = new FileReader();
  reader.onload = (event) => {
    try {
      const fileContent = event.target?.result || "";
      const importedVars = parseEnvTextToVariables(fileContent);
      const currentRaw = getStackEnvValue();
      const currentVars = parseEnvTextToVariables(currentRaw);
      const hasExisting = currentVars.some((v) => v.name && v.name.trim());

      let finalVars = [];
      if (hasExisting) {
        const shouldMerge = window.confirm(
          "Importing will merge with existing variables. Choose OK to merge or Cancel to replace."
        );
        if (shouldMerge) {
          const mergedMap = new Map();
          for (const item of currentVars) {
            if (item.name && item.name.trim()) {
              mergedMap.set(item.name.trim(), item.value);
            }
          }
          for (const item of importedVars) {
            if (item.name && item.name.trim()) {
              mergedMap.set(item.name.trim(), item.value);
            }
          }
          finalVars = Array.from(mergedMap.entries()).map(([name, value]) => ({ name, value }));
        } else {
          finalVars = importedVars.filter((v) => v.name && v.name.trim());
        }
      } else {
        finalVars = importedVars.filter((v) => v.name && v.name.trim());
      }

      if (finalVars.length === 0) {
        finalVars = [{ name: "", value: "" }];
      }

      const serialized = serializeVariablesToEnvText(finalVars);
      setStackEnvValue(serialized);

      showToast({
        title: "Environment Imported",
        message: `Successfully loaded ${finalVars.filter((v) => v.name && v.name.trim()).length} variable(s).`,
        type: "success",
      });
    } catch (err) {
      showToast({
        title: "Import Error",
        message: `Failed to import .env file: ${err.message}`,
        type: "error",
      });
    } finally {
      e.target.value = "";
    }
  };
  reader.onerror = () => {
    showToast({
      title: "File Read Error",
      message: "Could not read the selected file.",
      type: "error",
    });
    e.target.value = "";
  };
  reader.readAsText(file);
}

/**
 * Export current environment variables as a downloadable .env file.
 */
function exportStackEnvFile() {
  const envContent = getStackEnvValue();
  const stackNameInput = document.getElementById("stack-name-input");
  const stackName = stackNameInput?.value?.trim() || "stack";
  const filename = `${stackName}.env`;

  const blob = new Blob([envContent], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);

  showToast({
    title: "Environment Exported",
    message: `Exported variables to ${filename}`,
    type: "success",
  });
}

/**
 * Copy current environment variables to system clipboard.
 */
async function copyStackEnvToClipboard() {
  const envContent = getStackEnvValue();
  try {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      await navigator.clipboard.writeText(envContent);
    } else {
      const textarea = document.createElement("textarea");
      textarea.value = envContent;
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      document.body.removeChild(textarea);
    }
    showToast({
      title: "Copied to Clipboard",
      message: "Environment variables copied to clipboard.",
      type: "success",
    });
  } catch (err) {
    showToast({
      title: "Copy Failed",
      message: `Failed to copy: ${err.message}`,
      type: "error",
    });
  }
}

/**
 * Get current .env content from synced textarea / Ace editor or visual Key-Value table.
 */
function getStackEnvValue() {
  const kvTbody = document.getElementById("stack-env-kv-tbody");
  if (
    activeStackEnvMode === "kv" &&
    kvTbody &&
    typeof kvTbody.querySelectorAll === "function"
  ) {
    const rows = kvTbody.querySelectorAll(".env-kv-row");
    if (rows && rows.length > 0) {
      const vars = readVariablesFromKvTable();
      const serialized = serializeVariablesToEnvText(vars);
      const envInput = document.getElementById("stack-env-input");
      if (envInput && envInput.value !== serialized) {
        envInput.value = serialized;
      }
      if (envAceEditor && typeof envAceEditor.setValue === "function") {
        if (envAceEditor.getValue() !== serialized) {
          isSyncingStackEditor = true;
          try {
            envAceEditor.setValue(serialized, -1);
            lastSyncedEnvAceValue = serialized;
          } finally {
            isSyncingStackEditor = false;
          }
        }
      }
      return serialized;
    }
  }

  const envInput = document.getElementById("stack-env-input");
  if (envAceEditor && typeof envAceEditor.getValue === "function") {
    if (envInput && envInput.value !== lastSyncedEnvAceValue) {
      isSyncingStackEditor = true;
      try {
        envAceEditor.setValue(envInput.value, -1);
        lastSyncedEnvAceValue = envInput.value;
      } finally {
        isSyncingStackEditor = false;
      }
      return envInput.value;
    }
    const aceVal = envAceEditor.getValue();
    if (envInput && envInput.value !== aceVal) {
      envInput.value = aceVal;
    }
    return aceVal;
  }
  return envInput?.value || "";
}

/**
 * Set .env content in both #stack-env-input, Ace editor, and visual Key-Value table.
 */
function setStackEnvValue(val) {
  const strVal = val ?? "";
  const envInput = document.getElementById("stack-env-input");
  if (envInput) {
    envInput.value = strVal;
  }
  if (envAceEditor && typeof envAceEditor.setValue === "function") {
    isSyncingStackEditor = true;
    try {
      envAceEditor.setValue(strVal, -1);
      lastSyncedEnvAceValue = strVal;
      envAceEditor.resize(true);
    } finally {
      isSyncingStackEditor = false;
    }
  }
  const tbody = document.getElementById("stack-env-kv-tbody");
  if (tbody && typeof tbody.appendChild === "function") {
    renderEnvKeyValueTable(parseEnvTextToVariables(strVal));
  }
}

/**
 * Synchronize Ace editors from the underlying <textarea> values.
 */
function syncStackEditorsFromTextareas() {
  initStackCodeEditors();
  const yamlInput = document.getElementById("stack-yaml-input");
  const envInput = document.getElementById("stack-env-input");
  if (yamlInput) {
    setStackYamlValue(yamlInput.value);
  }
  if (envInput) {
    setStackEnvValue(envInput.value);
  }
}

/**
 * Switch Stack Editor tabs: 'compose' or 'env'.
 */
function switchStackEditorTab(tabName) {
  activeStackEditorTab = tabName;
  const tabBtnCompose = document.getElementById("tab-btn-compose");
  const tabBtnEnv = document.getElementById("tab-btn-env");
  const panelCompose = document.getElementById("editor-tab-panel-compose");
  const panelEnv = document.getElementById("editor-tab-panel-env");
  const modeInfo = document.getElementById("stack-editor-mode-info");

  if (tabName === "env") {
    tabBtnCompose?.classList.remove("active");
    tabBtnEnv?.classList.add("active");
    panelCompose?.classList.add("hidden");
    panelEnv?.classList.remove("hidden");
    if (modeInfo) modeInfo.textContent = "UTF-8 / .ENV";
    if (activeStackEnvMode === "raw" && envAceEditor && typeof envAceEditor.resize === "function") {
      envAceEditor.resize();
    }
  } else {
    tabBtnCompose?.classList.add("active");
    tabBtnEnv?.classList.remove("active");
    panelCompose?.classList.remove("hidden");
    panelEnv?.classList.add("hidden");
    if (modeInfo) modeInfo.textContent = "UTF-8 / YAML";
    if (composeAceEditor && typeof composeAceEditor.resize === "function") {
      composeAceEditor.resize();
    }
  }
}

/**
 * Render validation / formatting result into #stack-validation-status and update Ace annotations.
 */
function renderStackValidationResult(data, badgeTitle = "✓ Valid Compose YAML") {
  const statusBox = document.getElementById("stack-validation-status");
  if (!statusBox) return;

  if (data && data.valid) {
    const services = data.services || [];
    const ports = data.ports || [];
    const warnings = data.warnings || [];

    let html = `
      <div class="validation-header-row">
        <span class="validation-badge-valid">${escapeHtml(badgeTitle)}</span>
        <span class="validation-label">${services.length} service${services.length === 1 ? "" : "s"} detected</span>
      </div>
    `;

    if (services.length > 0 || ports.length > 0) {
      html += `<div class="validation-details-row">`;
      if (services.length > 0) {
        html +=
          `<span class="validation-label">Services:</span> ` +
          services.map((s) => `<span class="service-pill">${escapeHtml(s)}</span>`).join(" ");
      }
      if (ports.length > 0) {
        html +=
          `<span class="validation-label" style="margin-left:8px;">Ports:</span> ` +
          ports.map((p) => `<span class="port-pill">${escapeHtml(p)}</span>`).join(" ");
      }
      html += `</div>`;
    }

    if (warnings.length > 0) {
      html += `
        <ul class="validation-error-list" style="color: var(--state-degraded); margin-top: 6px;">
          ${warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}
        </ul>`;
      setStackEditorAnnotations(
        warnings.map((w) => ({
          row: 0,
          column: 0,
          text: String(w),
          type: "warning",
        }))
      );
    } else {
      setStackEditorAnnotations([]);
    }

    statusBox.innerHTML = html;
    statusBox.classList.remove("hidden");
  } else {
    const errors = data?.errors || [data?.detail || "Validation failed."];
    const errLine = data?.error_line;
    const errCol = data?.error_col;
    const lineInfoHtml = errLine
      ? `<span class="validation-label">Line ${escapeHtml(String(errLine))}${errCol ? `, Col ${escapeHtml(String(errCol))}` : ""}</span>`
      : "";

    statusBox.innerHTML = `
      <div class="validation-header-row">
        <span class="validation-badge-invalid">✕ Invalid Compose YAML</span>
        ${lineInfoHtml}
      </div>
      <ul class="validation-error-list">
        ${errors.map((e) => `<li>${escapeHtml(e)}</li>`).join("")}
      </ul>
    `;
    statusBox.classList.remove("hidden");

    setStackEditorAnnotations([
      {
        row: Math.max(0, (Number(errLine) || 1) - 1),
        column: Math.max(0, (Number(errCol) || 1) - 1),
        text: errors.join("; "),
        type: "error",
      },
    ]);
  }
}

/**
 * Validate Docker Compose YAML structure and ports via POST /api/docker/stacks/validate.
 */
async function validateStackYaml(overrideYaml = null) {
  const statusBox = document.getElementById("stack-validation-status");
  if (!statusBox) return;

  const yamlContent = overrideYaml !== null ? overrideYaml : getStackYamlValue();
  if (!yamlContent.trim()) {
    statusBox.className = "validation-status-box";
    statusBox.innerHTML = `
      <div class="validation-header-row">
        <span class="validation-badge-invalid">✕ Empty YAML</span>
        <span class="validation-label">Compose YAML content cannot be empty.</span>
      </div>`;
    statusBox.classList.remove("hidden");
    setStackEditorAnnotations([
      { row: 0, column: 0, text: "Compose YAML content cannot be empty.", type: "error" },
    ]);
    return;
  }

  statusBox.className = "validation-status-box";
  statusBox.innerHTML = `<span class="validation-label">Validating YAML...</span>`;
  statusBox.classList.remove("hidden");

  try {
    const resp = await apiFetch("/api/docker/stacks/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ compose_yaml: yamlContent }),
    });

    const data = await resp.json().catch(() => ({}));
    if (resp.ok && data.valid) {
      renderStackValidationResult(data, "✓ Valid Compose YAML");
    } else {
      renderStackValidationResult(data);
    }
  } catch (err) {
    statusBox.innerHTML = `
      <div class="validation-header-row">
        <span class="validation-badge-invalid">✕ Validation Error</span>
        <span class="validation-label">${escapeHtml(err.message)}</span>
      </div>`;
    statusBox.classList.remove("hidden");
  }
}

/**
 * Canonical Format & Beautify for Docker Compose YAML and .env via POST /api/docker/stacks/format.
 */
async function formatStackYaml() {
  const statusBox = document.getElementById("stack-validation-status");
  const yamlVal = getStackYamlValue();
  const envVal = getStackEnvValue();

  if (statusBox) {
    statusBox.className = "validation-status-box";
    statusBox.innerHTML = `<span class="validation-label">Formatting &amp; validating YAML...</span>`;
    statusBox.classList.remove("hidden");
  }

  try {
    const resp = await apiFetch("/api/docker/stacks/format", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ compose_yaml: yamlVal, env_content: envVal }),
    });

    const data = await resp.json().catch(() => ({}));
    if (resp.ok && data.valid) {
      if (data.formatted_yaml) {
        setStackYamlValue(data.formatted_yaml);
      }
      if (data.formatted_env !== undefined && data.formatted_env !== null) {
        setStackEnvValue(data.formatted_env);
      }
      renderStackValidationResult(data, "✓ Formatted & Valid Compose");
    } else {
      if (data.formatted_env !== undefined && data.formatted_env !== null && !yamlVal.trim()) {
        setStackEnvValue(data.formatted_env);
      }
      renderStackValidationResult(data);
    }
  } catch (err) {
    if (statusBox) {
      statusBox.innerHTML = `
        <div class="validation-header-row">
          <span class="validation-badge-invalid">✕ Format Error</span>
          <span class="validation-label">${escapeHtml(err.message)}</span>
        </div>`;
      statusBox.classList.remove("hidden");
    }
  }
}

/**
 * Structure-aware helper that merges a 1-click Compose snippet into existing YAML
 * without producing duplicate keys or placing service keys inside top-level networks/volumes.
 */
function applySnippetToComposeYaml(yamlText, snippetType) {
  const validTypes = new Set(["service", "ports", "volumes", "environment", "healthcheck", "network"]);
  if (!validTypes.has(snippetType)) return yamlText;

  const raw = (yamlText || "").replace(/\r\n/g, "\n");

  // Collect all host ports already referenced in the YAML
  const usedPorts = new Set();
  const portRegex = /(\d+):(\d+)(?::(\d+))?/g;
  let portMatch;
  while ((portMatch = portRegex.exec(raw)) !== null) {
    const hostPort = portMatch[3] !== undefined ? Number(portMatch[2]) : Number(portMatch[1]);
    if (Number.isFinite(hostPort) && hostPort > 0) {
      usedPorts.add(hostPort);
    }
  }

  const getNextAvailablePort = (startPort = 8080) => {
    let p = startPort;
    while (usedPorts.has(p)) {
      p += 1;
    }
    usedPorts.add(p);
    return p;
  };

  // Handle completely empty editor
  if (!raw.trim()) {
    const port = getNextAvailablePort(8080);
    switch (snippetType) {
      case "service":
        return `services:\n  app:\n    image: nginx:alpine\n    restart: unless-stopped\n    ports:\n      - "${port}:80"\n`;
      case "ports":
        return `services:\n  app:\n    image: nginx:alpine\n    ports:\n      - "${port}:80"\n`;
      case "volumes":
        return `services:\n  app:\n    image: nginx:alpine\n    volumes:\n      - ./data:/data\n`;
      case "environment":
        return `services:\n  app:\n    image: nginx:alpine\n    environment:\n      - TZ=UTC\n      - NODE_ENV=production\n`;
      case "healthcheck":
        return `services:\n  app:\n    image: nginx:alpine\n    healthcheck:\n      test: ["CMD", "curl", "-f", "http://localhost"]\n      interval: 30s\n      timeout: 5s\n      retries: 3\n`;
      case "network":
        return `services:\n  app:\n    image: nginx:alpine\n    networks:\n      - app-net\n\nnetworks:\n  app-net:\n    driver: bridge\n`;
      default:
        return raw;
    }
  }

  let working = raw;
  // Ensure a top-level services: section exists
  if (!/(?:^|\n)services\s*:/m.test(working)) {
    const port = snippetType === "service" ? getNextAvailablePort(8080) : null;
    const baseService =
      snippetType === "service"
        ? `services:\n  app:\n    image: nginx:alpine\n    restart: unless-stopped\n    ports:\n      - "${port}:80"\n\n`
        : `services:\n  app:\n    image: nginx:alpine\n    restart: unless-stopped\n\n`;
    working = baseService + working.trimStart();
    if (snippetType === "service") {
      return working.endsWith("\n") ? working : `${working}\n`;
    }
  }

  let lines = working.split("\n");
  if (lines.length > 0 && lines[lines.length - 1] === "") {
    lines.pop();
  }

  const parseStructure = (lineArr) => {
    const topSections = [];
    for (let i = 0; i < lineArr.length; i += 1) {
      const m = lineArr[i].match(/^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:/);
      if (m) {
        if (topSections.length > 0) {
          topSections[topSections.length - 1].endLine = i;
        }
        topSections.push({ key: m[1], startLine: i, endLine: lineArr.length });
      }
    }

    const servicesSec = topSections.find((s) => s.key === "services") || null;
    const networksSec = topSections.find((s) => s.key === "networks") || null;
    const services = [];

    if (servicesSec) {
      for (let i = servicesSec.startLine + 1; i < servicesSec.endLine; i += 1) {
        const sm = lineArr[i].match(/^  ([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?:\s*#.*|\s*)$/);
        if (sm) {
          if (services.length > 0) {
            services[services.length - 1].endLine = i;
          }
          services.push({
            name: sm[1],
            startLine: i,
            endLine: servicesSec.endLine,
            props: [],
          });
        }
      }

      for (const svc of services) {
        for (let i = svc.startLine + 1; i < svc.endLine; i += 1) {
          const pm = lineArr[i].match(/^    ([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:/);
          if (pm) {
            if (svc.props.length > 0) {
              svc.props[svc.props.length - 1].endLine = i;
            }
            svc.props.push({
              key: pm[1],
              startLine: i,
              endLine: svc.endLine,
            });
          }
        }
      }
    }

    return { topSections, servicesSec, networksSec, services };
  };

  const getInsertLine = (lineArr, startLine, endLine) => {
    let idx = endLine;
    while (idx > startLine + 1 && lineArr[idx - 1].trim() === "") {
      idx -= 1;
    }
    return idx;
  };

  const getSequenceIndent = (lineArr, startLine, endLine, fallback = "      ") => {
    for (let i = startLine + 1; i < endLine; i += 1) {
      const m = lineArr[i].match(/^(\s*)-\s+/);
      if (m) return m[1];
    }
    return fallback;
  };

  let struct = parseStructure(lines);

  // If services: exists but has no service entries yet, seed `app` for non-service snippets
  if (struct.servicesSec && struct.services.length === 0 && snippetType !== "service") {
    const insertIdx = getInsertLine(lines, struct.servicesSec.startLine, struct.servicesSec.endLine);
    lines.splice(insertIdx, 0, "  app:", "    image: nginx:alpine", "    restart: unless-stopped");
    struct = parseStructure(lines);
  }

  if (snippetType === "service") {
    const existingNames = new Set(struct.services.map((s) => s.name));
    let svcName = "app";
    if (existingNames.has("app")) {
      if (!existingNames.has("app-service")) {
        svcName = "app-service";
      } else {
        let n = 2;
        while (existingNames.has(`app-${n}`)) {
          n += 1;
        }
        svcName = `app-${n}`;
      }
    }
    const port = getNextAvailablePort(8080);
    const insertIdx = getInsertLine(lines, struct.servicesSec.startLine, struct.servicesSec.endLine);
    lines.splice(
      insertIdx,
      0,
      `  ${svcName}:`,
      "    image: nginx:alpine",
      "    restart: unless-stopped",
      "    ports:",
      `      - "${port}:80"`
    );
    return `${lines.join("\n")}\n`;
  }

  const targetSvc = struct.services[struct.services.length - 1];
  if (!targetSvc) return `${lines.join("\n")}\n`;

  if (snippetType === "ports") {
    const nextPort = getNextAvailablePort(8080);
    const portsProp = targetSvc.props.find((p) => p.key === "ports");
    if (portsProp) {
      const indent = getSequenceIndent(lines, portsProp.startLine, portsProp.endLine);
      const insertIdx = getInsertLine(lines, portsProp.startLine, portsProp.endLine);
      lines.splice(insertIdx, 0, `${indent}- "${nextPort}:80"`);
    } else {
      const insertIdx = getInsertLine(lines, targetSvc.startLine, targetSvc.endLine);
      lines.splice(insertIdx, 0, "    ports:", `      - "${nextPort}:80"`);
    }
    return `${lines.join("\n")}\n`;
  }

  if (snippetType === "volumes") {
    const svcText = lines.slice(targetSvc.startLine, targetSvc.endLine).join("\n");
    const candidates = ["./data:/data", "./config:/config"];
    let nextVol = candidates.find((c) => !svcText.includes(c));
    if (!nextVol) {
      let n = 2;
      while (svcText.includes(`./data-${n}:/data-${n}`)) {
        n += 1;
      }
      nextVol = `./data-${n}:/data-${n}`;
    }

    const volProp = targetSvc.props.find((p) => p.key === "volumes");
    if (volProp) {
      const indent = getSequenceIndent(lines, volProp.startLine, volProp.endLine);
      const insertIdx = getInsertLine(lines, volProp.startLine, volProp.endLine);
      lines.splice(insertIdx, 0, `${indent}- ${nextVol}`);
    } else {
      const insertIdx = getInsertLine(lines, targetSvc.startLine, targetSvc.endLine);
      lines.splice(insertIdx, 0, "    volumes:", `      - ${nextVol}`);
    }
    return `${lines.join("\n")}\n`;
  }

  if (snippetType === "environment") {
    const envProp = targetSvc.props.find((p) => p.key === "environment");
    if (envProp) {
      const envLines = lines.slice(envProp.startLine + 1, envProp.endLine);
      const envPresets = [
        ["TZ", "UTC"],
        ["NODE_ENV", "production"],
        ["PUID", "1000"],
        ["PGID", "1000"],
        ["LOG_LEVEL", "info"],
      ];
      const hasEnvKey = (k) => {
        const re = new RegExp(`^\\s*(?:-\\s*)?["']?${k}["']?\\s*[=:]`);
        return envLines.some((l) => re.test(l));
      };
      let chosen = envPresets.find(([k]) => !hasEnvKey(k));
      if (!chosen) {
        let n = 1;
        while (hasEnvKey(`APP_VAR_${n}`)) {
          n += 1;
        }
        chosen = [`APP_VAR_${n}`, "value"];
      }
      const [key, val] = chosen;
      const isMapSyntax =
        !envLines.some((l) => /^\s*-\s+/.test(l)) &&
        envLines.some((l) => /^\s+[A-Za-z0-9_.-]+\s*:/.test(l));
      const insertIdx = getInsertLine(lines, envProp.startLine, envProp.endLine);
      if (isMapSyntax) {
        const mapMatch = envLines.find((l) => /^(\s+)[A-Za-z0-9_.-]+\s*:/.test(l))?.match(/^(\s+)/);
        const mapIndent = mapMatch ? mapMatch[1] : "      ";
        lines.splice(insertIdx, 0, `${mapIndent}${key}: ${val}`);
      } else {
        const indent = getSequenceIndent(lines, envProp.startLine, envProp.endLine);
        lines.splice(insertIdx, 0, `${indent}- ${key}=${val}`);
      }
    } else {
      const insertIdx = getInsertLine(lines, targetSvc.startLine, targetSvc.endLine);
      lines.splice(
        insertIdx,
        0,
        "    environment:",
        "      - TZ=UTC",
        "      - NODE_ENV=production"
      );
    }
    return `${lines.join("\n")}\n`;
  }

  if (snippetType === "healthcheck") {
    const hcTarget =
      [...struct.services]
        .reverse()
        .find((s) => !s.props.some((p) => p.key === "healthcheck")) || targetSvc;
    const hasHc = hcTarget.props.some((p) => p.key === "healthcheck");
    if (!hasHc) {
      const insertIdx = getInsertLine(lines, hcTarget.startLine, hcTarget.endLine);
      lines.splice(
        insertIdx,
        0,
        "    healthcheck:",
        '      test: ["CMD", "curl", "-f", "http://localhost"]',
        "      interval: 30s",
        "      timeout: 5s",
        "      retries: 3"
      );
    }
    return `${lines.join("\n")}\n`;
  }

  if (snippetType === "network") {
    const existingNets = new Set();
    if (struct.networksSec) {
      for (let i = struct.networksSec.startLine + 1; i < struct.networksSec.endLine; i += 1) {
        const nm = lines[i].match(/^  ([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:/);
        if (nm) existingNets.add(nm[1]);
      }
    }
    let netName = "app-net";
    if (existingNets.has("app-net")) {
      let n = 2;
      while (existingNets.has(`app-net-${n}`)) {
        n += 1;
      }
      netName = `app-net-${n}`;
    }

    // Attach network to target service first
    const svcNetProp = targetSvc.props.find((p) => p.key === "networks");
    if (svcNetProp) {
      const propSlice = lines.slice(svcNetProp.startLine + 1, svcNetProp.endLine).join("\n");
      if (!propSlice.includes(netName)) {
        const indent = getSequenceIndent(lines, svcNetProp.startLine, svcNetProp.endLine);
        const insertIdx = getInsertLine(lines, svcNetProp.startLine, svcNetProp.endLine);
        lines.splice(insertIdx, 0, `${indent}- ${netName}`);
      }
    } else {
      const insertIdx = getInsertLine(lines, targetSvc.startLine, targetSvc.endLine);
      lines.splice(insertIdx, 0, "    networks:", `      - ${netName}`);
    }

    // Re-parse structure after modifying target service so top-level networks: line indices are exact
    struct = parseStructure(lines);
    if (struct.networksSec) {
      const insertIdx = getInsertLine(lines, struct.networksSec.startLine, struct.networksSec.endLine);
      lines.splice(insertIdx, 0, `  ${netName}:`, "    driver: bridge");
    } else {
      if (lines.length > 0 && lines[lines.length - 1].trim() !== "") {
        lines.push("");
      }
      lines.push("networks:", `  ${netName}:`, "    driver: bridge");
    }
    return `${lines.join("\n")}\n`;
  }

  return `${lines.join("\n")}\n`;
}

/**
 * Insert a 1-click Compose YAML snippet block into the editor.
 */
async function insertComposeSnippet(snippetType) {
  if (!snippetType) return;
  switchStackEditorTab("compose");
  const current = getStackYamlValue();
  const updated = applySnippetToComposeYaml(current, snippetType);
  if (typeof updated !== "string") return;
  setStackYamlValue(updated);
  await formatStackYaml();
}

/**
 * Fallback Textarea IDE Keyboard Ergonomics (2-space soft tabs, auto-indent on Enter,
 * Ctrl+/ comment toggle, Ctrl+Shift+F format).
 */
function handleComposeTextareaKeydown(e) {
  const textarea = e.target;
  if (!textarea || typeof textarea.value !== "string") return;

  const val = textarea.value;
  const start = textarea.selectionStart ?? 0;
  const end = textarea.selectionEnd ?? start;

  // Ctrl+Shift+F / Cmd+Shift+F / Alt+Shift+F -> Format YAML
  if ((e.ctrlKey || e.metaKey || e.altKey) && e.shiftKey && (e.key === "F" || e.key === "f")) {
    e.preventDefault();
    formatStackYaml();
    return;
  }

  // Ctrl+/ or Cmd+/ -> Toggle '# ' comment on current or selected lines
  if ((e.ctrlKey || e.metaKey) && e.key === "/") {
    e.preventDefault();
    const lineStart = val.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
    let lineEnd = val.indexOf("\n", end);
    if (lineEnd === -1) lineEnd = val.length;

    const selectedBlock = val.slice(lineStart, lineEnd);
    const lines = selectedBlock.split("\n");
    const allCommented = lines
      .filter((l) => l.trim().length > 0)
      .every((l) => /^\s*#/.test(l));

    const updatedLines = lines.map((line) => {
      if (allCommented) {
        return line.replace(/^(\s*)#\s?/, "$1");
      }
      const match = line.match(/^(\s*)(.*)$/);
      return `${match[1]}# ${match[2]}`;
    });

    const replacement = updatedLines.join("\n");
    textarea.value = val.slice(0, lineStart) + replacement + val.slice(lineEnd);
    textarea.selectionStart = lineStart;
    textarea.selectionEnd = lineStart + replacement.length;
    return;
  }

  // Tab / Shift+Tab -> 2-space indent or unindent
  if (e.key === "Tab") {
    e.preventDefault();
    if (start !== end || e.shiftKey) {
      const lineStart = val.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
      let lineEnd = val.indexOf("\n", end);
      if (lineEnd === -1) lineEnd = val.length;

      const selectedBlock = val.slice(lineStart, lineEnd);
      const lines = selectedBlock.split("\n");
      const updatedLines = lines.map((line) => {
        if (e.shiftKey) {
          return line.replace(/^ {1,2}/, "");
        }
        return "  " + line;
      });
      const replacement = updatedLines.join("\n");
      textarea.value = val.slice(0, lineStart) + replacement + val.slice(lineEnd);
      textarea.selectionStart = lineStart;
      textarea.selectionEnd = lineStart + replacement.length;
    } else {
      textarea.value = val.slice(0, start) + "  " + val.slice(end);
      textarea.selectionStart = textarea.selectionEnd = start + 2;
    }
    return;
  }

  // Enter -> Auto-indent to current line's indentation (+2 spaces if line ends with ':')
  if (e.key === "Enter" && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
    e.preventDefault();
    const lineStart = val.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
    const currentLineUpToCursor = val.slice(lineStart, start);
    const indentMatch = currentLineUpToCursor.match(/^(\s*)/);
    let indent = indentMatch ? indentMatch[1] : "";
    if (/:\s*$/.test(currentLineUpToCursor.trim())) {
      indent += "  ";
    }
    const insertion = "\n" + indent;
    textarea.value = val.slice(0, start) + insertion + val.slice(end);
    textarea.selectionStart = textarea.selectionEnd = start + insertion.length;
  }
}

/**
 * Open Stack Editor modal for creating or updating a stack.
 */
async function openStackEditor(stackName = null, prefill = null) {
  const modal = document.getElementById("stack-editor-modal");
  const title = document.getElementById("stack-editor-title");
  const nameInput = document.getElementById("stack-name-input");
  const saveText = document.getElementById("save-stack-text");
  const feedback = document.getElementById("stack-editor-feedback");
  const statusBox = document.getElementById("stack-validation-status");

  if (feedback) feedback.classList.add("hidden");
  if (statusBox) statusBox.classList.add("hidden");

  modal?.classList.remove("hidden");
  initStackCodeEditors();
  setStackEditorAnnotations([]);
  switchStackEditorTab("compose");
  switchStackEnvMode("kv");

  if (stackName) {
    if (title) title.textContent = `Edit Compose Stack: ${stackName}`;
    if (nameInput) {
      nameInput.value = stackName;
      nameInput.disabled = true;
    }
    if (saveText) saveText.textContent = "Update Stack";
    setStackYamlValue("Loading compose file...");
    setStackEnvValue("");

    try {
      const resp = await apiFetch(`/api/docker/stacks/${encodeURIComponent(stackName)}`);
      if (resp.ok) {
        const data = await resp.json();
        setStackYamlValue(data.stack?.compose_yaml || "");
        setStackEnvValue(data.stack?.env_content || "");
        if (data.stack?.compose_yaml) {
          validateStackYaml(data.stack.compose_yaml);
        }
      } else {
        setStackYamlValue("");
        setStackEnvValue("");
      }
    } catch {
      setStackYamlValue("");
      setStackEnvValue("");
    }
  } else if (prefill) {
    if (title) title.textContent = "Deploy New Compose Stack";
    if (nameInput) {
      nameInput.value = prefill.name || "";
      nameInput.disabled = false;
    }
    if (saveText) saveText.textContent = "Deploy Stack";
    setStackYamlValue(prefill.compose_yaml || "");
    setStackEnvValue(prefill.env_content || "");
    if (prefill.compose_yaml) {
      validateStackYaml(prefill.compose_yaml);
    }
    setTimeout(() => {
      if (composeAceEditor && typeof composeAceEditor.focus === "function") {
        composeAceEditor.focus();
      } else {
        document.getElementById("stack-yaml-input")?.focus();
      }
    }, 50);
  } else {
    if (title) title.textContent = "Deploy New Compose Stack";
    if (nameInput) {
      nameInput.value = "";
      nameInput.disabled = false;
    }
    if (saveText) saveText.textContent = "Deploy Stack";
    setStackYamlValue("");
    setStackEnvValue("");
  }

  modal?.classList.remove("hidden");
  if (composeAceEditor && typeof composeAceEditor.resize === "function") {
    composeAceEditor.resize(true);
  }
}

function closeStackEditor() {
  document.getElementById("stack-editor-modal")?.classList.add("hidden");
}

function insertExampleYaml() {
  const example = `services:
  app:
    image: nginx:alpine
    ports:
      - "8088:80"
    restart: unless-stopped
`;
  setStackYamlValue(example);
  validateStackYaml(example);
}

/**
 * Save & optionally deploy stack via POST or PUT /api/docker/stacks.
 */
async function handleSaveStack() {
  const nameInput = document.getElementById("stack-name-input");
  const deployToggle = document.getElementById("stack-deploy-toggle");
  const feedback = document.getElementById("stack-editor-feedback");
  const saveBtn = document.getElementById("save-stack-btn");

  const name = (nameInput?.value || "").trim();
  const compose_yaml = getStackYamlValue().trim();
  const env_content = getStackEnvValue();
  const deploy = !!deployToggle?.checked;
  const isEdit = !!nameInput?.disabled;

  if (!name || !/^[a-zA-Z0-9_-]+$/.test(name)) {
    if (feedback) {
      feedback.className = "auth-alert feedback-error";
      feedback.textContent = "Stack name must contain only letters, numbers, dashes, and underscores.";
      feedback.classList.remove("hidden");
    }
    return;
  }

  if (!compose_yaml) {
    if (feedback) {
      feedback.className = "auth-alert feedback-error";
      feedback.textContent = "Compose YAML cannot be empty.";
      feedback.classList.remove("hidden");
    }
    return;
  }

  if (saveBtn) saveBtn.disabled = true;

  try {
    let resp;
    if (isEdit) {
      resp = await apiFetch(`/api/docker/stacks/${encodeURIComponent(name)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, compose_yaml, env_content, deploy }),
      });
    } else {
      resp = await apiFetch("/api/docker/stacks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, compose_yaml, env_content, deploy }),
      });
    }

    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      lastStacksRenderKey = "";
      closeStackEditor();
      fetchDockerStacks();
      fetchDockerStatus();
    } else {
      if (feedback) {
        feedback.className = "auth-alert feedback-error";
        let errMsg = data.detail || "Failed to save Compose stack.";
        if (typeof errMsg === "object") {
          try {
            errMsg = JSON.stringify(errMsg, null, 2);
          } catch {
            errMsg = String(errMsg);
          }
        }
        feedback.textContent = errMsg;
        feedback.classList.remove("hidden");
        feedback.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    }
  } catch (err) {
    if (feedback) {
      feedback.className = "auth-alert feedback-error";
      feedback.textContent = `Error: ${err.message}`;
      feedback.classList.remove("hidden");
      feedback.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

/**
 * Get display icon for homelab app catalog items.
 */
function getAppCatalogIcon(icon) {
  const iconMap = {
    shield: "🛡️",
    activity: "📈",
    globe: "🌐",
    lock: "🔒",
    film: "🎬",
    home: "🏠",
    server: "🖥️",
    terminal: "💻",
    database: "🗄️",
  };
  return iconMap[icon] || "🚀";
}

/**
 * Fetch homelab application templates from backend catalog.
 */
async function fetchAppTemplates() {
  try {
    const resp = await apiFetch("/api/docker/templates");
    if (resp.ok) {
      const data = await resp.json();
      cachedAppTemplates = data.templates || [];
      renderAppCatalog();
    }
  } catch (err) {
    console.warn("Failed to fetch Docker app templates:", err);
  }
}

/**
 * Open the Homelab 1-Click App Catalog modal dialog.
 */
function openAppCatalog() {
  const modal = document.getElementById("app-catalog-modal");
  if (!modal) return;
  modal.classList.remove("hidden");
  if (!cachedAppTemplates || cachedAppTemplates.length === 0) {
    fetchAppTemplates();
  } else {
    renderAppCatalog();
  }
  const searchInput = document.getElementById("catalog-search-input");
  if (searchInput) {
    searchInput.focus();
  }
}

/**
 * Close the Homelab 1-Click App Catalog modal dialog.
 */
function closeAppCatalog() {
  const modal = document.getElementById("app-catalog-modal");
  if (modal) modal.classList.add("hidden");
}

/**
 * Render Homelab App Catalog cards with search and category filtering.
 */
function renderAppCatalog() {
  const grid = document.getElementById("app-catalog-grid");
  if (!grid) return;

  const searchInput = document.getElementById("catalog-search-input");
  const query = (searchInput?.value || "").trim().toLowerCase();

  const filtered = (cachedAppTemplates || []).filter((t) => {
    if (currentAppCatalogCategory && currentAppCatalogCategory !== "All") {
      if ((t.category || "").toLowerCase() !== currentAppCatalogCategory.toLowerCase()) {
        return false;
      }
    }
    if (query) {
      const matchName = (t.name || "").toLowerCase().includes(query);
      const matchCategory = (t.category || "").toLowerCase().includes(query);
      const matchDesc = (t.description || "").toLowerCase().includes(query);
      const matchPorts = (t.default_ports || []).some((p) => String(p).toLowerCase().includes(query));
      const matchYaml = (t.compose_yaml || "").toLowerCase().includes(query);
      if (!matchName && !matchCategory && !matchDesc && !matchPorts && !matchYaml) {
        return false;
      }
    }
    return true;
  });

  if (filtered.length === 0) {
    grid.innerHTML = `
      <div style="grid-column: 1 / -1; text-align: center; padding: 40px 20px; color: var(--text-secondary);">
        <p style="font-size: 1.1rem; margin-bottom: 6px;">No applications found matching filters.</p>
        <span style="font-size: 0.82rem; color: var(--text-muted);">Try clearing your search or switching categories.</span>
      </div>`;
    return;
  }

  grid.innerHTML = filtered
    .map((t) => {
      const icon = getAppCatalogIcon(t.icon);
      const ports = t.default_ports || [];
      const portsHtml =
        ports.length > 0
          ? `<div class="catalog-ports-row">${ports.map((p) => `<span class="catalog-port-tag">${escapeHtml(p)}</span>`).join("")}</div>`
          : "";

      return `
      <div class="catalog-card" data-template-id="${escapeHtml(t.id)}">
        <div>
          <div class="catalog-card-header">
            <div class="catalog-icon-box" aria-hidden="true">${icon}</div>
            <div class="catalog-meta">
              <div class="catalog-title-row">
                <span class="catalog-app-name" title="${escapeHtml(t.name)}">${escapeHtml(t.name)}</span>
                <span class="catalog-category-badge">${escapeHtml(t.category || "General")}</span>
              </div>
            </div>
          </div>
          <p class="catalog-desc" title="${escapeHtml(t.description || "")}">${escapeHtml(t.description || "")}</p>
          ${portsHtml}
        </div>
        <div class="catalog-card-actions">
          <button type="button" class="btn-catalog-deploy" data-template-action="deploy" data-template-id="${escapeHtml(t.id)}">
            <span>🚀</span> 1-Click Launch
          </button>
          <button type="button" class="btn-catalog-customize" data-template-action="customize" data-template-id="${escapeHtml(t.id)}">
            Customize
          </button>
        </div>
      </div>
    `;
    })
    .join("");

  grid.querySelectorAll("button[data-template-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const action = btn.getAttribute("data-template-action");
      const templateId = btn.getAttribute("data-template-id");
      const template = (cachedAppTemplates || []).find((t) => t.id === templateId);
      if (!template) return;

      if (action === "deploy") {
        handle1ClickLaunch(template);
      } else if (action === "customize") {
        handleCustomizeTemplate(template);
      }
    });
  });
}

/**
 * Handle 1-click launch deployment of an application template.
 */
async function handle1ClickLaunch(template) {
  let stackName = template.id || template.name.toLowerCase().replace(/[^a-z0-9_-]/g, "-");
  const existingNames = new Set((cachedDockerStacks || []).map((s) => s.name));
  if (existingNames.has(stackName)) {
    let suffix = 2;
    while (existingNames.has(`${stackName}-${suffix}`)) {
      suffix++;
    }
    stackName = `${stackName}-${suffix}`;
  }

  const confirmed = confirm(
    `Launch "${template.name}" as stack "${stackName}"?\n\nThis will deploy and start the Compose stack immediately.`
  );
  if (!confirmed) return;

  try {
    const resp = await apiFetch("/api/docker/stacks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: stackName,
        compose_yaml: template.compose_yaml,
        env_content: template.env_content || "",
        deploy: true,
      }),
    });

    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      closeAppCatalog();
      lastStacksRenderKey = "";
      fetchDockerStacks();
      fetchDockerStatus();
    } else {
      let errMsg = data.detail || "Failed to launch app stack.";
      if (typeof errMsg === "object") {
        try {
          errMsg = JSON.stringify(errMsg, null, 2);
        } catch {
          errMsg = String(errMsg);
        }
      }
      alert(`Launch error: ${errMsg}`);
    }
  } catch (err) {
    alert(`Failed to launch template: ${err.message}`);
  }
}

/**
 * Handle Customize & Deploy by opening the Stack Editor with template parameters prefilled.
 */
function handleCustomizeTemplate(template) {
  let stackName = template.id || template.name.toLowerCase().replace(/[^a-z0-9_-]/g, "-");
  const existingNames = new Set((cachedDockerStacks || []).map((s) => s.name));
  if (existingNames.has(stackName)) {
    let suffix = 2;
    while (existingNames.has(`${stackName}-${suffix}`)) {
      suffix++;
    }
    stackName = `${stackName}-${suffix}`;
  }

  closeAppCatalog();
  openStackEditor(null, {
    name: stackName,
    compose_yaml: template.compose_yaml,
    env_content: template.env_content || "",
  });
}

/**
 * Fetch and render Docker Containers.
 */
async function fetchDockerContainers(highlightCid = null) {
  const tbody = document.getElementById("docker-containers-tbody");
  if (!tbody) return;
  if (isFetchingDockerContainers) return;
  isFetchingDockerContainers = true;

  try {
    const resp = await apiFetch(`/api/docker/containers?all=true`);
    if (!resp.ok) return;
    const data = await resp.json();
    cachedDockerContainers = data.containers || [];

    // Populate stack filter dropdown only if options changed
    const stackFilterEl = document.getElementById("containers-filter-stack");
    if (stackFilterEl) {
      const currentSelected = stackFilterEl.value || "all";
      const stacks = new Set();
      cachedDockerContainers.forEach((c) => {
        if (c.stack && String(c.stack).trim() !== "") {
          stacks.add(String(c.stack).trim());
        }
      });
      const sortedStacks = Array.from(stacks).sort();
      let optionsHtml = `<option value="all">All Stacks</option><option value="standalone">Standalone (No Stack)</option>`;
      sortedStacks.forEach((st) => {
        const safeSt = escapeHtml(st);
        optionsHtml += `<option value="${safeSt}">${safeSt}</option>`;
      });
      if (stackFilterEl.dataset.renderedOptions !== optionsHtml) {
        stackFilterEl.dataset.renderedOptions = optionsHtml;
        stackFilterEl.innerHTML = optionsHtml;
        if (currentSelected === "all" || currentSelected === "standalone" || stacks.has(currentSelected)) {
          stackFilterEl.value = currentSelected;
        } else {
          stackFilterEl.value = "all";
        }
      }
    }

    renderDockerContainers(highlightCid);
  } catch (err) {
    console.warn("Failed to fetch Docker containers:", err);
  } finally {
    isFetchingDockerContainers = false;
  }
}

function renderDockerContainers(highlightCid = null, containers = null) {
  const tbody = document.getElementById("docker-containers-tbody");
  if (!tbody) return;

  if (containers) {
    cachedDockerContainers = containers;
  }

  // Preserve open dropdown menu under the user's cursor during live stream updates
  if (!highlightCid && tbody.querySelector?.(".actions-dropdown-menu.show")) {
    return;
  }

  const searchInput =
    document.getElementById("containers-search-input") ||
    document.getElementById("docker-container-search");
  const query = (searchInput?.value || "").trim().toLowerCase();

  const statusFilterEl = document.getElementById("containers-filter-status");
  const statusFilter = statusFilterEl?.value || "all";

  const stackFilterEl = document.getElementById("containers-filter-stack");
  const stackFilter = stackFilterEl?.value || "all";

  const renderKey = JSON.stringify({
    query,
    statusFilter,
    stackFilter,
    highlightCid,
    containers: cachedDockerContainers,
  });
  if (lastContainersRenderKey === renderKey && tbody.children.length > 0) {
    return;
  }
  lastContainersRenderKey = renderKey;

  let filtered = cachedDockerContainers;

  // 1. Text Search Filter
  if (query) {
    filtered = filtered.filter((c) =>
      (c.name || "").toLowerCase().includes(query) ||
      (c.id || "").toLowerCase().includes(query) ||
      (c.image || "").toLowerCase().includes(query) ||
      (c.stack || "").toLowerCase().includes(query) ||
      (c.ports || "").toLowerCase().includes(query)
    );
  }

  // 2. Status Filter: "all", "running", "stopped"
  if (statusFilter === "running") {
    filtered = filtered.filter((c) => {
      const st = (c.state || "").toLowerCase();
      const statusStr = (c.status || "").toLowerCase();
      return st === "running" || statusStr.includes("up");
    });
  } else if (statusFilter === "stopped") {
    filtered = filtered.filter((c) => {
      const st = (c.state || "").toLowerCase();
      const statusStr = (c.status || "").toLowerCase();
      return st !== "running" && !statusStr.includes("up");
    });
  }

  // 3. Stack Filter: "all", "standalone", or specific stack name
  if (stackFilter === "standalone") {
    filtered = filtered.filter((c) => !c.stack || String(c.stack).trim() === "");
  } else if (stackFilter !== "all") {
    filtered = filtered.filter((c) => String(c.stack).trim() === stackFilter);
  }

  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-state">${query || statusFilter !== "all" || stackFilter !== "all" ? "No containers matching current filters." : "No containers found."}</td></tr>`;
    return;
  }

  tbody.innerHTML = filtered.map((c) => {
    const state = (c.state || "").toLowerCase();
    const statusStr = (c.status || "").toLowerCase();
    let statusClass = "status-none";
    if (statusStr.includes("unhealthy")) {
      statusClass = "status-unhealthy";
    } else if (state === "running" || statusStr.includes("up")) {
      statusClass = statusStr.includes("starting") ? "status-recovering" : "status-clean";
    } else if (state === "paused" || state === "restarting") {
      statusClass = "status-degraded";
    } else if (state === "dead" || state === "failed" || state === "exited" || statusStr.includes("exit")) {
      statusClass = (statusStr.includes("exit (0)") || statusStr.includes("exited (0)")) ? "status-none" : "status-failed";
    }

    const isRunning = state === "running" || (statusStr.includes("up") && !state);
    const isPaused = state === "paused";

    const safeId = escapeHtml(c.id);
    const safeName = escapeHtml(c.name);
    const safeImage = escapeHtml(c.image);
    const safePorts = escapeHtml(c.ports || "-");
    const safeStack = escapeHtml(c.stack || "-");
    const safeStatus = escapeHtml(c.status || c.state || "unknown");

    const isHighlighted = highlightCid && (c.id === highlightCid || (c.name && c.name === highlightCid) || (highlightCid.includes(c.id)));
    const rowClass = isHighlighted ? ' class="row-highlight"' : "";

    let quickActionsHtml = "";
    let dropdownActionsHtml = "";

    if (isRunning) {
      quickActionsHtml = `
        <button type="button" class="btn-action" data-c-action="restart" data-cid="${safeId}">Restart</button>
        <button type="button" class="btn-action" data-c-action="logs" data-cid="${safeId}" data-cname="${safeName}">Logs</button>
        <button type="button" class="btn-action btn-action-sm btn-terminal" data-c-action="terminal" data-terminal-container-id="${safeId}" data-terminal-container-name="${safeName}" data-cid="${safeId}" data-cname="${safeName}" title="Open Web Terminal">Terminal</button>
      `;
      dropdownActionsHtml = `
        <button type="button" class="actions-dropdown-item" data-c-action="stop" data-cid="${safeId}">Stop</button>
        <button type="button" class="actions-dropdown-item" data-c-action="pause" data-cid="${safeId}">Pause</button>
        <div class="actions-dropdown-divider"></div>
        <button type="button" class="actions-dropdown-item danger" data-c-action="remove" data-cid="${safeId}" data-cname="${safeName}">Remove</button>
      `;
    } else if (isPaused) {
      quickActionsHtml = `
        <button type="button" class="btn-action btn-action-success" data-c-action="unpause" data-cid="${safeId}">Unpause</button>
        <button type="button" class="btn-action" data-c-action="logs" data-cid="${safeId}" data-cname="${safeName}">Logs</button>
        <button type="button" class="btn-action btn-action-sm btn-terminal" data-c-action="terminal" data-terminal-container-id="${safeId}" data-terminal-container-name="${safeName}" data-cid="${safeId}" data-cname="${safeName}" title="Open Web Terminal">Terminal</button>
      `;
      dropdownActionsHtml = `
        <button type="button" class="actions-dropdown-item" data-c-action="stop" data-cid="${safeId}">Stop</button>
        <div class="actions-dropdown-divider"></div>
        <button type="button" class="actions-dropdown-item danger" data-c-action="remove" data-cid="${safeId}" data-cname="${safeName}">Remove</button>
      `;
    } else {
      quickActionsHtml = `
        <button type="button" class="btn-action btn-action-success" data-c-action="start" data-cid="${safeId}">Start</button>
        <button type="button" class="btn-action" data-c-action="logs" data-cid="${safeId}" data-cname="${safeName}">Logs</button>
      `;
      dropdownActionsHtml = `
        <button type="button" class="actions-dropdown-item btn-terminal" data-c-action="terminal" data-terminal-container-id="${safeId}" data-terminal-container-name="${safeName}" data-cid="${safeId}" data-cname="${safeName}" title="Open Web Terminal">Terminal</button>
        <div class="actions-dropdown-divider"></div>
        <button type="button" class="actions-dropdown-item danger" data-c-action="remove" data-cid="${safeId}" data-cname="${safeName}">Remove</button>
      `;
    }

    return `<tr${rowClass}>
      <td><strong>${safeName}</strong></td>
      <td><span class="mono-text">${safeId}</span></td>
      <td><span class="mono-text" style="color: var(--accent-primary);">${safeImage}</span></td>
      <td>
        <span class="status-badge ${statusClass}">
          <span class="status-dot"></span>
          ${safeStatus}
        </span>
      </td>
      <td><span class="mono-text">${safePorts}</span></td>
      <td>${safeStack !== "-" ? `<span class="kind-badge kind-weekly">${safeStack}</span>` : "-"}</td>
      <td class="actions-col">
        <div class="btn-action-group">
          ${quickActionsHtml}
          <div class="actions-dropdown">
            <button type="button" class="btn-action btn-action-more" aria-label="More actions" title="More actions">⋯</button>
            <div class="actions-dropdown-menu">
              ${dropdownActionsHtml}
            </div>
          </div>
        </div>
      </td>
    </tr>`;
  }).join("");

  tbody.querySelectorAll("button[data-c-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      closeAllActionsDropdowns();
      const action = btn.getAttribute("data-c-action");
      const cid = btn.getAttribute("data-cid");
      const cname = btn.getAttribute("data-cname") || cid;
      if (action === "logs") {
        openContainerLogs(cid, cname);
      } else if (action === "terminal") {
        openContainerTerminal(cid, cname);
      } else if (action === "remove") {
        handleRemoveContainer(cid, cname);
      } else {
        handleContainerAction(cid, action, btn);
      }
    });
  });
}

/**
 * Execute container action: start, stop, restart, pause, unpause.
 */
async function handleContainerAction(containerId, action, buttonEl) {
  if (buttonEl) {
    buttonEl.disabled = true;
    buttonEl.textContent = "...";
  }

  try {
    const resp = await apiFetch(`/api/docker/containers/${encodeURIComponent(containerId)}/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(`Container action '${action}' failed: ${err.detail || resp.statusText}`);
    }
  } catch (err) {
    alert(`Error executing container action: ${err.message}`);
  } finally {
    lastContainersRenderKey = "";
    fetchDockerContainers();
    fetchDockerStatus();
  }
}

/**
 * Remove container.
 */
async function handleRemoveContainer(containerId, containerName) {
  if (!confirm(`Are you sure you want to remove container "${containerName}"?`)) {
    return;
  }

  try {
    const resp = await apiFetch(`/api/docker/containers/${encodeURIComponent(containerId)}/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "remove" }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(`Failed to remove container: ${err.detail || resp.statusText}`);
    }
  } catch (err) {
    alert(`Error removing container: ${err.message}`);
  } finally {
    lastContainersRenderKey = "";
    fetchDockerContainers();
    fetchDockerStatus();
  }
}

/**
 * Container Logs Modal Viewer.
 */
function openContainerLogs(containerId, containerName) {
  activeLogContainerId = containerId;
  activeLogContainerName = containerName;

  const modal = document.getElementById("container-logs-modal");
  const title = document.getElementById("container-logs-title");
  const subtitle = document.getElementById("container-logs-subtitle");
  const viewer = document.getElementById("container-logs-viewer");

  if (title) title.textContent = `Container Logs: ${containerName}`;
  if (subtitle) subtitle.textContent = `Container ID: ${containerId}`;
  if (viewer) viewer.textContent = "Loading logs...";

  modal?.classList.remove("hidden");
  fetchContainerLogs();
  sendWsLogSubscription(containerId);

  if (logsTimerId) {
    clearInterval(logsTimerId);
    logsTimerId = null;
  }

  const autoToggle = document.getElementById("logs-autorefresh-toggle");
  const isWsOpen =
    wsConnection &&
    typeof window.WebSocket === "function" &&
    wsConnection.readyState === window.WebSocket.OPEN;
  if (autoToggle?.checked && !isWsOpen) {
    logsTimerId = setInterval(fetchContainerLogs, 3000);
  }
}

function closeContainerLogs() {
  if (logsTimerId) {
    clearInterval(logsTimerId);
    logsTimerId = null;
  }
  activeLogContainerId = null;
  sendWsLogSubscription(null);
  document.getElementById("container-logs-modal")?.classList.add("hidden");
}

async function fetchContainerLogs() {
  if (!activeLogContainerId) return;
  if (isFetchingContainerLogs) return;
  isFetchingContainerLogs = true;

  const tailSelect = document.getElementById("logs-tail-select");
  const tsToggle = document.getElementById("logs-timestamps-toggle");
  const viewer = document.getElementById("container-logs-viewer");

  const tail = tailSelect ? tailSelect.value : "200";
  const timestamps = tsToggle ? !!tsToggle.checked : true;

  try {
    const resp = await apiFetch(`/api/docker/containers/${encodeURIComponent(activeLogContainerId)}/logs?tail=${tail}&timestamps=${timestamps}`);
    if (!resp.ok) {
      if (viewer) viewer.textContent = `Unable to fetch logs (HTTP ${resp.status})`;
      return;
    }
    const data = await resp.json();
    if (viewer) {
      const isScrolledToBottom = viewer.scrollHeight - viewer.clientHeight <= viewer.scrollTop + 50;
      viewer.textContent = data.logs || "(No log output emitted)";
      if (isScrolledToBottom) {
        viewer.scrollTop = viewer.scrollHeight;
      }
    }
  } catch (err) {
    if (viewer) viewer.textContent = `Error fetching logs: ${err.message}`;
  } finally {
    isFetchingContainerLogs = false;
  }
}

function copyContainerLogs() {
  const viewer = document.getElementById("container-logs-viewer");
  if (viewer && viewer.textContent) {
    navigator.clipboard.writeText(viewer.textContent).then(() => {
      const copyBtn = document.getElementById("copy-logs-btn");
      if (copyBtn) {
        const orig = copyBtn.textContent;
        copyBtn.textContent = "Copied!";
        setTimeout(() => { copyBtn.textContent = orig; }, 1500);
      }
    }).catch(() => {});
  }
}

function scrollLogsToBottom() {
  const viewer = document.getElementById("container-logs-viewer");
  if (viewer) {
    viewer.scrollTop = viewer.scrollHeight;
  }
}

/**
 * Container Interactive Web Terminal Controller & ANSI Engine.
 */
function openContainerTerminal(containerId, containerName, shell = "/bin/sh", user = "") {
  activeTerminalContainerId = containerId;
  activeTerminalContainerName = containerName || containerId;
  terminalUtf8Decoder = new TextDecoder("utf-8", { stream: true });

  const modal = document.getElementById("container-terminal-modal");
  const title = document.getElementById("container-terminal-title");
  const subtitle = document.getElementById("container-terminal-subtitle");
  const shellSelect = document.getElementById("container-terminal-shell-select");
  const userInput = document.getElementById("container-terminal-user-input");
  const screen = document.getElementById("container-terminal-screen");

  if (title) title.textContent = `Terminal: ${activeTerminalContainerName}`;
  if (subtitle) subtitle.textContent = `Container ID: ${containerId}`;
  if (shellSelect) shellSelect.value = shell;
  if (userInput) userInput.value = user;

  modal?.classList.remove("hidden");
  resetTerminalBuffer();
  connectContainerTerminal(containerId, activeTerminalContainerName, shell, user);

  setTimeout(() => {
    screen?.focus();
  }, 60);
}

function closeContainerTerminal() {
  if (terminalWs) {
    try {
      terminalWs.onclose = null;
      terminalWs.onerror = null;
      terminalWs.onmessage = null;
      terminalWs.close();
    } catch (_) {}
    terminalWs = null;
  }
  terminalUtf8Decoder = new TextDecoder("utf-8", { stream: true });
  activeTerminalContainerId = null;
  activeTerminalContainerName = "";

  const modal = document.getElementById("container-terminal-modal");
  modal?.classList.add("hidden");
  modal?.classList.remove("terminal-fullscreen");

  const fullscreenBtn = document.getElementById("btn-fullscreen-terminal");
  if (fullscreenBtn) fullscreenBtn.textContent = "Fullscreen";

  setTerminalStatus("disconnected", "Disconnected");
}

function connectContainerTerminal(containerId, containerName, shell = "/bin/sh", user = "") {
  if (terminalWs) {
    try {
      terminalWs.onclose = null;
      terminalWs.onerror = null;
      terminalWs.onmessage = null;
      terminalWs.close();
    } catch (_) {}
    terminalWs = null;
  }

  terminalUtf8Decoder = new TextDecoder("utf-8", { stream: true });
  setTerminalStatus("connecting", "Connecting...");

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const token = getStoredToken();
  const queryParams = new URLSearchParams();
  queryParams.set("shell", shell || "/bin/sh");
  if (user) queryParams.set("user", user);
  if (token) queryParams.set("token", token);

  const wsUrl = `${protocol}//${window.location.host}/api/docker/containers/${encodeURIComponent(containerId)}/terminal?${queryParams.toString()}`;

  try {
    terminalWs = new WebSocket(wsUrl);
  } catch (err) {
    console.error("Terminal WebSocket creation error:", err);
    setTerminalStatus("error", "Connection error");
    feedTerminalText(`\r\n\x1b[31mFailed to connect to terminal: ${err.message}\x1b[0m\r\n`);
    return;
  }

  terminalWs.binaryType = "arraybuffer";

  terminalWs.onopen = () => {
    setTerminalStatus("connected", "Connected");
    sendTerminalResize();
    const screen = document.getElementById("container-terminal-screen");
    screen?.focus();
  };

  terminalWs.onmessage = (event) => {
    let text = "";
    if (typeof event.data === "string") {
      text = event.data;
    } else if (event.data instanceof ArrayBuffer) {
      text = terminalUtf8Decoder.decode(new Uint8Array(event.data), { stream: true });
    }
    feedTerminalText(text);
  };

  terminalWs.onclose = (event) => {
    terminalWs = null;
    let reasonMsg = "Disconnected";
    if (event.code === 1008) {
      reasonMsg = "Session unauthenticated (1008)";
    } else if (event.code === 1011) {
      reasonMsg = "Container exec error (1011)";
    }
    setTerminalStatus("disconnected", reasonMsg);
    feedTerminalText(`\r\n\x1b[33m[Session disconnected (${reasonMsg})]\x1b[0m\r\n`);
  };

  terminalWs.onerror = () => {
    setTerminalStatus("error", "Terminal error");
  };
}

const connectTerminalWebSocket = connectContainerTerminal;

function setTerminalStatus(state, message) {
  const badge = document.getElementById("container-terminal-status");
  if (!badge) return;
  badge.className = "status-badge";
  if (state === "connecting") {
    badge.classList.add("status-recovering");
    badge.innerHTML = `<span class="status-dot pulse"></span> ${escapeHtml(message || "Connecting...")}`;
  } else if (state === "connected") {
    badge.classList.add("status-clean");
    badge.innerHTML = `<span class="status-dot"></span> ${escapeHtml(message || "Connected")}`;
  } else if (state === "error") {
    badge.classList.add("status-failed");
    badge.innerHTML = `<span class="status-dot"></span> ${escapeHtml(message || "Error")}`;
  } else {
    badge.classList.add("status-none");
    badge.innerHTML = `<span class="status-dot"></span> ${escapeHtml(message || "Disconnected")}`;
  }
}

function resetTerminalBuffer() {
  terminalLines = [[]];
  terminalCursorRow = 0;
  terminalCursorCol = 0;
  terminalCursorVisible = true;
  terminalSavedLines = null;
  terminalSavedCursorRow = 0;
  terminalSavedCursorCol = 0;
  resetTerminalStyle();
  terminalParserState = "NORMAL";
  terminalCsiParams = "";
  terminalOscParams = "";
  terminalPendingBuffer = "";
  terminalUtf8Decoder = new TextDecoder("utf-8", { stream: true });
  scheduleTerminalRender();
}

function resetTerminalStyle() {
  terminalCurrentStyle = {
    fg: null,
    bg: null,
    bold: false,
    dim: false,
    italic: false,
    underline: false,
    inverse: false,
  };
}

function clearTerminalScreen() {
  terminalLines = [[]];
  terminalCursorRow = 0;
  terminalCursorCol = 0;
  scheduleTerminalRender();
  const screen = document.getElementById("container-terminal-screen");
  screen?.focus();
}

function toggleTerminalFullscreen() {
  const modal = document.getElementById("container-terminal-modal");
  const fullscreenBtn = document.getElementById("btn-fullscreen-terminal");
  if (!modal) return;

  const isFullscreen = modal.classList.toggle("terminal-fullscreen");
  if (fullscreenBtn) {
    fullscreenBtn.textContent = isFullscreen ? "Exit Fullscreen" : "Fullscreen";
  }
  setTimeout(() => {
    sendTerminalResize();
    const screen = document.getElementById("container-terminal-screen");
    screen?.focus();
  }, 100);
}

function ensureTerminalRow(rowIndex) {
  while (terminalLines.length <= rowIndex) {
    terminalLines.push([]);
  }
}

function get256ColorClass(num, isBg) {
  const prefix = isBg ? "ansi-bg-" : "ansi-";
  if (num >= 0 && num <= 7) {
    const colors = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"];
    return `${prefix}${colors[num]}`;
  }
  if (num >= 8 && num <= 15) {
    const brights = [
      "bright-black",
      "bright-red",
      "bright-green",
      "bright-yellow",
      "bright-blue",
      "bright-magenta",
      "bright-cyan",
      "bright-white",
    ];
    return `${prefix}${brights[num - 8]}`;
  }
  if (num === 16) return `${prefix}black`;
  if (num === 231 || num === 255) return `${prefix}bright-white`;
  return `${prefix}white`;
}

function handleCsiCommand(cmd, params) {
  const isDecPrivate = params.startsWith("?");
  const cleanParams = params.replace(/^[?><=:]+/, "");
  const args = cleanParams.length > 0 ? cleanParams.split(";").map((x) => parseInt(x, 10)) : [];

  switch (cmd) {
    case "m": {
      if (args.length === 0 || (args.length === 1 && (isNaN(args[0]) || args[0] === 0))) {
        resetTerminalStyle();
        break;
      }
      for (let idx = 0; idx < args.length; idx++) {
        const code = isNaN(args[idx]) ? 0 : args[idx];
        if (code === 0) {
          resetTerminalStyle();
        } else if (code === 1) {
          terminalCurrentStyle.bold = true;
        } else if (code === 2) {
          terminalCurrentStyle.dim = true;
        } else if (code === 3) {
          terminalCurrentStyle.italic = true;
        } else if (code === 4) {
          terminalCurrentStyle.underline = true;
        } else if (code === 7) {
          terminalCurrentStyle.inverse = true;
        } else if (code === 22) {
          terminalCurrentStyle.bold = false;
          terminalCurrentStyle.dim = false;
        } else if (code === 23) {
          terminalCurrentStyle.italic = false;
        } else if (code === 24) {
          terminalCurrentStyle.underline = false;
        } else if (code === 27) {
          terminalCurrentStyle.inverse = false;
        } else if (code >= 30 && code <= 37) {
          const colors = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"];
          terminalCurrentStyle.fg = `ansi-${colors[code - 30]}`;
        } else if (code === 38) {
          if (args[idx + 1] === 5 && args[idx + 2] !== undefined) {
            terminalCurrentStyle.fg = get256ColorClass(args[idx + 2], false);
            idx += 2;
          }
        } else if (code === 39) {
          terminalCurrentStyle.fg = null;
        } else if (code >= 40 && code <= 47) {
          const colors = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"];
          terminalCurrentStyle.bg = `ansi-bg-${colors[code - 40]}`;
        } else if (code === 48) {
          if (args[idx + 1] === 5 && args[idx + 2] !== undefined) {
            terminalCurrentStyle.bg = get256ColorClass(args[idx + 2], true);
            idx += 2;
          }
        } else if (code === 49) {
          terminalCurrentStyle.bg = null;
        } else if (code >= 90 && code <= 97) {
          const brights = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"];
          terminalCurrentStyle.fg = `ansi-bright-${brights[code - 90]}`;
        } else if (code >= 100 && code <= 107) {
          const brights = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"];
          terminalCurrentStyle.bg = `ansi-bg-bright-${brights[code - 100]}`;
        }
      }
      break;
    }
    case "h":
    case "l": {
      const isSet = (cmd === "h");
      if (isDecPrivate) {
        for (const mode of args) {
          if (mode === 25) {
            terminalCursorVisible = isSet;
          } else if (mode === 1049 || mode === 47) {
            if (isSet) {
              terminalSavedLines = terminalLines.map((row) => [...row]);
              terminalSavedCursorRow = terminalCursorRow;
              terminalSavedCursorCol = terminalCursorCol;
              terminalLines = [[]];
              terminalCursorRow = 0;
              terminalCursorCol = 0;
            } else {
              if (terminalSavedLines) {
                terminalLines = terminalSavedLines;
                terminalCursorRow = terminalSavedCursorRow;
                terminalCursorCol = terminalSavedCursorCol;
                terminalSavedLines = null;
              }
            }
          }
          // DEC private sequences ?2004 (bracketed paste), ?1 (cursor keys),
          // ?7 (autowrap) are cleanly recognized and swallowed without side effects
        }
      }
      break;
    }
    case "H":
    case "f": {
      const r = args[0] ? args[0] - 1 : 0;
      const c = args[1] ? args[1] - 1 : 0;
      terminalCursorRow = Math.max(0, r);
      terminalCursorCol = Math.max(0, c);
      ensureTerminalRow(terminalCursorRow);
      break;
    }
    case "A": {
      const count = args[0] || 1;
      terminalCursorRow = Math.max(0, terminalCursorRow - count);
      break;
    }
    case "B": {
      const count = args[0] || 1;
      terminalCursorRow += count;
      ensureTerminalRow(terminalCursorRow);
      break;
    }
    case "C": {
      const count = args[0] || 1;
      terminalCursorCol += count;
      break;
    }
    case "D": {
      const count = args[0] || 1;
      terminalCursorCol = Math.max(0, terminalCursorCol - count);
      break;
    }
    case "G": {
      const c = (args[0] && args[0] > 0) ? args[0] - 1 : 0;
      terminalCursorCol = Math.max(0, c);
      break;
    }
    case "d": {
      const r = (args[0] && args[0] > 0) ? args[0] - 1 : 0;
      terminalCursorRow = Math.max(0, r);
      ensureTerminalRow(terminalCursorRow);
      break;
    }
    case "K": {
      const mode = args[0] || 0;
      ensureTerminalRow(terminalCursorRow);
      const row = terminalLines[terminalCursorRow];
      if (mode === 0) {
        if (terminalCursorCol < row.length) {
          row.length = terminalCursorCol;
        }
      } else if (mode === 1) {
        for (let j = 0; j <= terminalCursorCol && j < row.length; j++) {
          row[j] = { char: " " };
        }
      } else if (mode === 2) {
        terminalLines[terminalCursorRow] = [];
      }
      break;
    }
    case "J": {
      const mode = args[0] || 0;
      if (mode === 2 || mode === 3) {
        terminalLines = [[]];
        terminalCursorRow = 0;
        terminalCursorCol = 0;
      } else if (mode === 0) {
        terminalLines = terminalLines.slice(0, terminalCursorRow + 1);
        if (terminalLines[terminalCursorRow]) {
          terminalLines[terminalCursorRow] = terminalLines[terminalCursorRow].slice(0, terminalCursorCol);
        }
      }
      break;
    }
    case "s": {
      terminalSavedCursorRow = terminalCursorRow;
      terminalSavedCursorCol = terminalCursorCol;
      break;
    }
    case "u": {
      terminalCursorRow = terminalSavedCursorRow || 0;
      terminalCursorCol = terminalSavedCursorCol || 0;
      ensureTerminalRow(terminalCursorRow);
      break;
    }
    default:
      break;
  }
}

function feedTerminalText(text) {
  if (!text) return;
  text = terminalPendingBuffer + text;
  terminalPendingBuffer = "";

  let i = 0;
  const len = text.length;
  let seqStartIndex = -1;

  while (i < len) {
    const ch = text[i];

    if (ch === "\x1b" && terminalParserState !== "NORMAL") {
      terminalParserState = "ESCAPE";
      seqStartIndex = i;
      terminalCsiParams = "";
      terminalOscParams = "";
      i++;
      continue;
    }

    if (terminalParserState === "ESCAPE") {
      if (ch === "[") {
        terminalParserState = "CSI";
        terminalCsiParams = "";
        i++;
        continue;
      } else if (ch === "]") {
        terminalParserState = "OSC";
        terminalOscParams = "";
        i++;
        continue;
      } else if (ch === "(" || ch === ")") {
        if (i + 1 < len) {
          i += 2;
          terminalParserState = "NORMAL";
          seqStartIndex = -1;
          continue;
        } else {
          break;
        }
      } else if (ch === "7") {
        terminalSavedCursorRow = terminalCursorRow;
        terminalSavedCursorCol = terminalCursorCol;
        terminalParserState = "NORMAL";
        seqStartIndex = -1;
        i++;
        continue;
      } else if (ch === "8") {
        terminalCursorRow = terminalSavedCursorRow || 0;
        terminalCursorCol = terminalSavedCursorCol || 0;
        ensureTerminalRow(terminalCursorRow);
        terminalParserState = "NORMAL";
        seqStartIndex = -1;
        i++;
        continue;
      } else {
        terminalParserState = "NORMAL";
        seqStartIndex = -1;
        i++;
        continue;
      }
    }

    if (terminalParserState === "CSI") {
      if (
        (ch >= "0" && ch <= "9") ||
        ch === ";" ||
        ch === "?" ||
        ch === ":" ||
        ch === "<" ||
        ch === ">" ||
        ch === "="
      ) {
        terminalCsiParams += ch;
        i++;
        continue;
      }
      if (ch >= " " && ch <= "/") {
        terminalCsiParams += ch;
        i++;
        continue;
      }
      if (ch >= "@" && ch <= "~") {
        handleCsiCommand(ch, terminalCsiParams);
        terminalParserState = "NORMAL";
        terminalCsiParams = "";
        seqStartIndex = -1;
        i++;
        continue;
      }

      terminalParserState = "NORMAL";
      terminalCsiParams = "";
      seqStartIndex = -1;
      i++;
      continue;
    }

    if (terminalParserState === "OSC") {
      if (ch === "\x07") {
        terminalParserState = "NORMAL";
        seqStartIndex = -1;
        i++;
        continue;
      }
      if (ch === "\x1b" && i + 1 < len && text[i + 1] === "\\") {
        terminalParserState = "NORMAL";
        seqStartIndex = -1;
        i += 2;
        continue;
      }
      terminalOscParams += ch;
      i++;
      continue;
    }

    // NORMAL state
    if (ch === "\x1b") {
      terminalParserState = "ESCAPE";
      seqStartIndex = i;
      i++;
      continue;
    }

    if (ch === "\r") {
      terminalCursorCol = 0;
      i++;
      continue;
    }

    if (ch === "\n") {
      terminalCursorRow++;
      ensureTerminalRow(terminalCursorRow);
      terminalCursorCol = 0;
      i++;
      continue;
    }

    if (ch === "\b") {
      terminalCursorCol = Math.max(0, terminalCursorCol - 1);
      i++;
      continue;
    }

    if (ch === "\t") {
      terminalCursorCol = (Math.floor(terminalCursorCol / 8) + 1) * 8;
      i++;
      continue;
    }

    if (ch === "\x07") {
      i++;
      continue;
    }

    // Control Character Sanitization: drop non-printable < 32, DEL (0x7f), C1 codes (0x80 to 0x9f), and U+FFFD
    const code = ch.charCodeAt(0);
    if (code < 32 || code === 0x7f || (code >= 0x80 && code <= 0x9f) || code === 0xfffd) {
      i++;
      continue;
    }

    ensureTerminalRow(terminalCursorRow);
    const row = terminalLines[terminalCursorRow];
    while (row.length < terminalCursorCol) {
      row.push({ char: " " });
    }
    row[terminalCursorCol] = {
      char: ch,
      fg: terminalCurrentStyle.fg,
      bg: terminalCurrentStyle.bg,
      bold: terminalCurrentStyle.bold,
      dim: terminalCurrentStyle.dim,
      italic: terminalCurrentStyle.italic,
      underline: terminalCurrentStyle.underline,
      inverse: terminalCurrentStyle.inverse,
    };
    terminalCursorCol++;
    i++;
  }

  if (terminalParserState !== "NORMAL" && seqStartIndex >= 0) {
    terminalPendingBuffer = text.slice(seqStartIndex);
    terminalParserState = "NORMAL";
    if (terminalPendingBuffer.length > 256) {
      terminalPendingBuffer = "";
    }
  }

  if (terminalLines.length > 2000) {
    const diff = terminalLines.length - 2000;
    terminalLines.splice(0, diff);
    terminalCursorRow = Math.max(0, terminalCursorRow - diff);
  }

  scheduleTerminalRender();
}

function scheduleTerminalRender() {
  if (terminalRenderScheduled) return;
  terminalRenderScheduled = true;
  requestAnimationFrame(() => {
    terminalRenderScheduled = false;
    renderTerminalToDOM();
  });
}

function renderTerminalToDOM() {
  const screen = document.getElementById("container-terminal-screen");
  if (!screen) return;

  const htmlPieces = [];
  const totalRows = terminalLines.length;

  for (let r = 0; r < totalRows; r++) {
    const row = terminalLines[r] || [];
    const isCursorRow = r === terminalCursorRow;
    const maxCols = Math.max(row.length, isCursorRow ? terminalCursorCol + 1 : 0);

    let rowHtml = "";
    let currentClasses = "";
    let spanBuffer = "";

    function flushSpan() {
      if (spanBuffer.length > 0) {
        if (currentClasses) {
          rowHtml += `<span class="${currentClasses}">${spanBuffer}</span>`;
        } else {
          rowHtml += spanBuffer;
        }
        spanBuffer = "";
      }
    }

    for (let c = 0; c < maxCols; c++) {
      const cell = row[c] || { char: " " };
      const rawChar = cell.char || " ";
      const safeChar =
        rawChar === "<"
          ? "&lt;"
          : rawChar === ">"
          ? "&gt;"
          : rawChar === "&"
          ? "&amp;"
          : rawChar === '"'
          ? "&quot;"
          : rawChar;

      if (isCursorRow && c === terminalCursorCol) {
        flushSpan();
        if (terminalCursorVisible) {
          rowHtml += `<span class="terminal-cursor">${safeChar === " " ? "&nbsp;" : safeChar}</span>`;
        } else {
          rowHtml += safeChar === " " ? "&nbsp;" : safeChar;
        }
        continue;
      }

      const classes = [];
      if (cell.fg) classes.push(cell.fg);
      if (cell.bg) classes.push(cell.bg);
      if (cell.bold) classes.push("ansi-bold");
      if (cell.dim) classes.push("ansi-dim");
      if (cell.italic) classes.push("ansi-italic");
      if (cell.underline) classes.push("ansi-underline");
      if (cell.inverse) classes.push("ansi-inverse");
      const classStr = classes.join(" ");

      if (classStr !== currentClasses) {
        flushSpan();
        currentClasses = classStr;
      }

      spanBuffer += safeChar === " " ? " " : safeChar;
    }

    flushSpan();

    if (isCursorRow && terminalCursorCol >= maxCols) {
      if (terminalCursorVisible) {
        rowHtml += `<span class="terminal-cursor">&nbsp;</span>`;
      }
    }

    htmlPieces.push(`<div class="terminal-row">${rowHtml || "&nbsp;"}</div>`);
  }

  screen.innerHTML = htmlPieces.join("");
  screen.scrollTop = screen.scrollHeight;
}

function sendTerminalData(data) {
  if (!terminalWs || terminalWs.readyState !== WebSocket.OPEN) return;
  try {
    terminalWs.send(data);
  } catch (err) {
    console.debug("Failed to send terminal data:", err);
  }
}

function getTerminalDimensions() {
  const screen = document.getElementById("container-terminal-screen");
  if (!screen) return { cols: 80, rows: 24 };

  const probe = document.createElement("span");
  probe.style.fontFamily = "monospace";
  probe.style.fontSize = "0.86rem";
  probe.style.lineHeight = "1.42";
  probe.style.visibility = "hidden";
  probe.style.position = "absolute";
  probe.style.whiteSpace = "pre";
  probe.textContent = "MMMMMMMMMM";
  screen.appendChild(probe);

  const rect = probe.getBoundingClientRect();
  const charWidth = rect.width > 0 ? rect.width / 10 : 8.5;
  const charHeight = rect.height > 0 ? rect.height : 19;
  screen.removeChild(probe);

  const paddingX = 28;
  const paddingY = 28;
  const availW = Math.max(100, (screen.clientWidth || 800) - paddingX);
  const availH = Math.max(100, (screen.clientHeight || 400) - paddingY);

  const cols = Math.max(20, Math.min(300, Math.floor(availW / charWidth)));
  const rows = Math.max(5, Math.min(100, Math.floor(availH / charHeight)));
  return { cols, rows };
}

function sendTerminalResize() {
  if (!terminalWs || terminalWs.readyState !== WebSocket.OPEN) return;
  const dims = getTerminalDimensions();
  try {
    terminalWs.send(JSON.stringify({ type: "resize", cols: dims.cols, rows: dims.rows }));
  } catch (err) {
    console.debug("Terminal resize send error:", err);
  }
}

function handleTerminalKeyDown(e) {
  if (!terminalWs || terminalWs.readyState !== WebSocket.OPEN) return;

  if (
    e.key === "F12" ||
    e.key === "F5" ||
    (e.ctrlKey && e.shiftKey && ["I", "i", "C", "c", "R", "r"].includes(e.key))
  ) {
    return;
  }

  if (e.ctrlKey && !e.altKey && !e.metaKey) {
    const k = e.key.toLowerCase();
    if (k === "c") {
      const selection = window.getSelection();
      if (selection && selection.toString().length > 0) {
        return;
      }
      e.preventDefault();
      sendTerminalData("\x03");
      return;
    }
    if (k === "d") {
      e.preventDefault();
      sendTerminalData("\x04");
      return;
    }
    if (k === "z") {
      e.preventDefault();
      sendTerminalData("\x1a");
      return;
    }
    if (k === "l") {
      e.preventDefault();
      sendTerminalData("\x0c");
      clearTerminalScreen();
      return;
    }
    if (k === "a") {
      e.preventDefault();
      sendTerminalData("\x01");
      return;
    }
    if (k === "e") {
      e.preventDefault();
      sendTerminalData("\x05");
      return;
    }
    if (k === "u") {
      e.preventDefault();
      sendTerminalData("\x15");
      return;
    }
    if (k === "k") {
      e.preventDefault();
      sendTerminalData("\x0b");
      return;
    }
    if (k === "w") {
      e.preventDefault();
      sendTerminalData("\x17");
      return;
    }
  }

  if (e.key === "Enter") {
    e.preventDefault();
    sendTerminalData("\r");
    return;
  }
  if (e.key === "Backspace") {
    e.preventDefault();
    sendTerminalData("\x7f");
    return;
  }
  if (e.key === "Tab") {
    e.preventDefault();
    sendTerminalData("\t");
    return;
  }
  if (e.key === "Escape") {
    e.preventDefault();
    sendTerminalData("\x1b");
    return;
  }
  if (e.key === "ArrowUp") {
    e.preventDefault();
    sendTerminalData("\x1b[A");
    return;
  }
  if (e.key === "ArrowDown") {
    e.preventDefault();
    sendTerminalData("\x1b[B");
    return;
  }
  if (e.key === "ArrowRight") {
    e.preventDefault();
    sendTerminalData("\x1b[C");
    return;
  }
  if (e.key === "ArrowLeft") {
    e.preventDefault();
    sendTerminalData("\x1b[D");
    return;
  }
  if (e.key === "Home") {
    e.preventDefault();
    sendTerminalData("\x1b[H");
    return;
  }
  if (e.key === "End") {
    e.preventDefault();
    sendTerminalData("\x1b[F");
    return;
  }
  if (e.key === "PageUp") {
    e.preventDefault();
    sendTerminalData("\x1b[5~");
    return;
  }
  if (e.key === "PageDown") {
    e.preventDefault();
    sendTerminalData("\x1b[6~");
    return;
  }
  if (e.key === "Delete") {
    e.preventDefault();
    sendTerminalData("\x1b[3~");
    return;
  }

  if (!e.ctrlKey && !e.altKey && !e.metaKey && e.key.length === 1) {
    e.preventDefault();
    sendTerminalData(e.key);
  }
}

function handleTerminalPaste(e) {
  e.preventDefault();
  const clipboardData = e.clipboardData || window.clipboardData;
  const text = clipboardData ? clipboardData.getData("text") : "";
  if (text) {
    sendTerminalData(text);
  }
}

/**
 * Images Tab: Fetch, Pull, Prune, Remove.
 */
async function fetchDockerImages() {
  const tbody = document.getElementById("docker-images-tbody");
  if (!tbody) return;
  if (isFetchingDockerImages) return;
  isFetchingDockerImages = true;

  try {
    const resp = await apiFetch("/api/docker/images");
    if (!resp.ok) return;
    const data = await resp.json();
    const images = data.images || [];

    if (images.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No images found on host.</td></tr>`;
      return;
    }

    tbody.innerHTML = images.map((img) => {
      const safeId = escapeHtml(img.id);
      const safeRepo = escapeHtml(img.repository);
      const safeTag = escapeHtml(img.tag);
      const safeSize = escapeHtml(img.size);
      const safeCreated = escapeHtml(img.created);

      return `<tr>
        <td><span class="mono-text">${safeId}</span></td>
        <td><strong>${safeRepo}</strong></td>
        <td><span class="mono-text">${safeTag}</span></td>
        <td>${safeSize}</td>
        <td>${safeCreated}</td>
        <td class="actions-col">
          <button type="button" class="btn-action btn-action-danger" data-img-delete="${safeId}" data-img-tag="${safeRepo}:${safeTag}">Remove</button>
        </td>
      </tr>`;
    }).join("");

    tbody.querySelectorAll("button[data-img-delete]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = btn.getAttribute("data-img-delete");
        const tag = btn.getAttribute("data-img-tag");
        handleRemoveImage(id, tag);
      });
    });
  } catch (err) {
    console.warn("Failed to fetch Docker images:", err);
  } finally {
    isFetchingDockerImages = false;
  }
}

async function handlePullImage() {
  const input = document.getElementById("docker-image-pull-input");
  const btn = document.getElementById("btn-pull-image");
  const feedback = document.getElementById("docker-image-feedback");

  const tag = (input?.value || "").trim();
  if (!tag) {
    alert("Please enter an image name to pull (e.g. nginx:alpine)");
    return;
  }

  if (btn) {
    btn.disabled = true;
    btn.textContent = "Pulling...";
  }

  if (feedback) {
    feedback.className = "auth-alert";
    feedback.textContent = `Pulling image '${tag}'... This may take a minute.`;
    feedback.classList.remove("hidden");
  }

  try {
    const resp = await apiFetch("/api/docker/images/pull", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image: tag }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      if (feedback) {
        feedback.className = "auth-alert feedback-success";
        feedback.textContent = `Successfully pulled image '${tag}'.`;
      }
      if (input) input.value = "";
      fetchDockerImages();
      fetchDockerStatus();
    } else {
      if (feedback) {
        feedback.className = "auth-alert feedback-error";
        feedback.textContent = data.detail || `Failed to pull image '${tag}'.`;
      }
    }
  } catch (err) {
    if (feedback) {
      feedback.className = "auth-alert feedback-error";
      feedback.textContent = `Error: ${err.message}`;
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Pull Image";
    }
  }
}

async function handlePruneImages() {
  if (!confirm("Are you sure you want to prune all dangling and unused Docker images?")) {
    return;
  }

  const feedback = document.getElementById("docker-image-feedback");
  try {
    const resp = await apiFetch("/api/docker/images/prune", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ all: false }),
    });
    if (resp.ok) {
      if (feedback) {
        feedback.className = "auth-alert feedback-success";
        feedback.textContent = "Unused images pruned successfully.";
        feedback.classList.remove("hidden");
      }
      fetchDockerImages();
      fetchDockerStatus();
    }
  } catch (err) {
    alert(`Error: ${err.message}`);
  }
}

async function handleRemoveImage(imageId, imageTag) {
  if (!confirm(`Are you sure you want to remove image "${imageTag || imageId}"?`)) {
    return;
  }

  try {
    const resp = await apiFetch(`/api/docker/images/${encodeURIComponent(imageId)}`, {
      method: "DELETE",
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      alert(`Failed to remove image: ${data.detail || resp.statusText}`);
    }
  } catch (err) {
    alert(`Error: ${err.message}`);
  } finally {
    fetchDockerImages();
    fetchDockerStatus();
  }
}

/**
 * Volumes Tab: Fetch, Inspect, Remove.
 */
async function fetchDockerVolumes() {
  const tbody = document.getElementById("docker-volumes-tbody");
  if (!tbody) return;
  if (isFetchingDockerVolumes) return;
  isFetchingDockerVolumes = true;

  try {
    const resp = await apiFetch("/api/docker/volumes");
    if (!resp.ok) return;
    const data = await resp.json();
    const volumes = data.volumes || [];
    cachedDockerVolumes = volumes;

    if (volumes.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-state">No persistent volumes found.</td></tr>`;
      return;
    }

    tbody.innerHTML = volumes.map((v) => {
      const safeName = escapeHtml(v.name);
      const safeDriver = escapeHtml(v.driver || "local");
      const safeScope = escapeHtml(v.scope || "local");
      const safeSize = escapeHtml(v.size || "--");
      const safeMount = escapeHtml(v.mountpoint || "-");
      const safeCreated = formatDateTime(v.created_at);

      // Check attached containers
      const attached = [];
      if (Array.isArray(v.containers) && v.containers.length > 0) {
        attached.push(...v.containers);
      } else {
        cachedDockerContainers.forEach((c) => {
          if (c.ports && c.ports.includes(v.name)) attached.push(c.name || c.id);
        });
      }
      const inUseBadge = attached.length > 0
        ? `<span class="kind-badge kind-weekly" title="In use by: ${attached.map(escapeHtml).join(', ')}">${attached.length} in use</span>`
        : `<span style="color: var(--text-muted); font-size: 0.76rem;">Unattached</span>`;

      return `<tr data-vol-row="${safeName}">
        <td><strong class="vol-name-link" data-vol-inspect="${safeName}" style="cursor: pointer; color: var(--accent-primary);">${safeName}</strong></td>
        <td><span class="mono-text">${safeDriver}</span></td>
        <td>${safeScope}</td>
        <td>${safeSize}</td>
        <td>
          <div style="display: flex; flex-direction: column; gap: 3px;">
            <span class="mono-text" style="font-size: 0.74rem;" title="${safeMount}">${safeMount}</span>
            <div>${inUseBadge}</div>
          </div>
        </td>
        <td>${safeCreated}</td>
        <td class="actions-col">
          <div class="btn-action-group">
            <button type="button" class="btn-action" data-vol-inspect="${safeName}">Inspect</button>
            <button type="button" class="btn-action btn-action-danger" data-vol-delete="${safeName}">Remove</button>
          </div>
        </td>
      </tr>`;
    }).join("");

    tbody.querySelectorAll("button[data-vol-delete]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const name = btn.getAttribute("data-vol-delete");
        handleRemoveVolume(name);
      });
    });

    tbody.querySelectorAll("[data-vol-inspect]").forEach((el) => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        const name = el.getAttribute("data-vol-inspect");
        openVolumeDetails(name);
      });
    });

    tbody.querySelectorAll("tr[data-vol-row]").forEach((tr) => {
      tr.addEventListener("click", (e) => {
        if (e.target.closest("button") || e.target.closest("a")) return;
        const name = tr.getAttribute("data-vol-row");
        if (name) openVolumeDetails(name);
      });
    });
  } catch (err) {
    console.warn("Failed to fetch Docker volumes:", err);
  } finally {
    isFetchingDockerVolumes = false;
  }
}

async function handleRemoveVolume(volumeName) {
  if (!confirm(`Are you sure you want to remove volume "${volumeName}"? Data on this volume will be lost.`)) {
    return;
  }

  const feedback = document.getElementById("docker-volume-feedback");
  try {
    const resp = await apiFetch(`/api/docker/volumes/${encodeURIComponent(volumeName)}`, {
      method: "DELETE",
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      if (feedback) {
        feedback.className = "auth-alert feedback-success";
        feedback.textContent = `Volume "${volumeName}" removed successfully.`;
        feedback.classList.remove("hidden");
      }
      showToast({
        title: "Volume Removed",
        message: `Docker volume "${volumeName}" has been deleted.`,
        type: "success",
      });
    } else {
      const errMsg = data.detail || resp.statusText || "Failed to remove volume.";
      if (feedback) {
        feedback.className = "auth-alert feedback-error";
        feedback.textContent = `Failed to remove volume "${volumeName}": ${errMsg}`;
        feedback.classList.remove("hidden");
      }
      alert(`Failed to remove volume: ${errMsg}`);
    }
  } catch (err) {
    if (feedback) {
      feedback.className = "auth-alert feedback-error";
      feedback.textContent = `Error removing volume: ${err.message}`;
      feedback.classList.remove("hidden");
    }
    alert(`Error: ${err.message}`);
  } finally {
    fetchDockerVolumes();
    fetchDockerStatus();
  }
}

async function handlePruneVolumes() {
  if (!confirm("Are you sure you want to prune all unused Docker volumes? Unused volume data will be permanently deleted.")) {
    return;
  }

  const feedback = document.getElementById("docker-volume-feedback");
  try {
    const resp = await apiFetch("/api/docker/volumes/prune", {
      method: "POST",
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      if (feedback) {
        feedback.className = "auth-alert feedback-success";
        feedback.textContent = "Unused volumes pruned successfully.";
        feedback.classList.remove("hidden");
      }
      showToast({
        title: "Volumes Pruned",
        message: "Unused Docker volumes pruned successfully.",
        type: "success",
      });
      fetchDockerVolumes();
      fetchDockerStatus();
    } else {
      const errMsg = data.detail || resp.statusText || "Failed to prune volumes.";
      if (feedback) {
        feedback.className = "auth-alert feedback-error";
        feedback.textContent = errMsg;
        feedback.classList.remove("hidden");
      }
      showToast({
        title: "Prune Volumes Failed",
        message: errMsg,
        type: "danger",
      });
    }
  } catch (err) {
    if (feedback) {
      feedback.className = "auth-alert feedback-error";
      feedback.textContent = `Error: ${err.message}`;
      feedback.classList.remove("hidden");
    }
    alert(`Error: ${err.message}`);
  }
}

/**
 * Volume Details Modal: Inspection of volume attributes and attachments.
 */
async function openVolumeDetails(volumeName) {
  const modal = document.getElementById("volume-details-modal");
  const title = document.getElementById("volume-details-title");
  const nameEl = document.getElementById("vol-detail-name");
  const driverEl = document.getElementById("vol-detail-driver");
  const scopeEl = document.getElementById("vol-detail-scope");
  const mountEl = document.getElementById("vol-detail-mountpoint");
  const sizeEl = document.getElementById("vol-detail-size");
  const createdEl = document.getElementById("vol-detail-created");
  const labelsEl = document.getElementById("vol-detail-labels");
  const containersEl = document.getElementById("vol-detail-containers");

  if (title) title.textContent = `Volume: ${volumeName}`;
  if (nameEl) nameEl.textContent = volumeName;

  const cachedVol = cachedDockerVolumes.find((v) => v.name === volumeName) || {};
  if (driverEl) driverEl.textContent = cachedVol.driver || "--";
  if (scopeEl) scopeEl.textContent = cachedVol.scope || "--";
  if (mountEl) mountEl.textContent = cachedVol.mountpoint || "--";
  if (sizeEl) sizeEl.textContent = cachedVol.size || "N/A";
  if (createdEl) createdEl.textContent = formatDateTime(cachedVol.created_at);
  if (labelsEl) labelsEl.textContent = "None";
  if (containersEl) containersEl.textContent = "Scanning attached containers...";

  modal?.classList.remove("hidden");

  try {
    const resp = await apiFetch(`/api/docker/volumes/${encodeURIComponent(volumeName)}`);
    if (resp.ok) {
      const data = await resp.json();
      const v = data.volume || data;
      if (nameEl) nameEl.textContent = v.name || volumeName;
      if (driverEl) driverEl.textContent = v.driver || "--";
      if (scopeEl) scopeEl.textContent = v.scope || "--";
      if (mountEl) mountEl.textContent = v.mountpoint || "--";
      if (sizeEl) sizeEl.textContent = v.size || "N/A";
      if (createdEl) createdEl.textContent = formatDateTime(v.created_at || v.created);

      if (labelsEl) {
        if (v.labels && typeof v.labels === "object" && Object.keys(v.labels).length > 0) {
          labelsEl.innerHTML = Object.entries(v.labels)
            .map(([k, val]) => `<span class="kind-badge">${escapeHtml(k)}=${escapeHtml(val)}</span>`)
            .join(" ");
        } else {
          labelsEl.textContent = "None";
        }
      }

      if (containersEl) {
        const attached = Array.isArray(v.containers) ? v.containers : [];
        if (attached.length > 0) {
          containersEl.innerHTML = attached
            .map((c) => `<span class="kind-badge kind-weekly">${escapeHtml(typeof c === 'string' ? c : (c.name || c.id))}</span>`)
            .join(" ");
        } else {
          containersEl.textContent = "No containers currently attached to this volume.";
        }
      }
      return;
    }
  } catch (err) {
    console.debug("Could not fetch detailed volume endpoint:", err);
  }

  // Fallback to local scan
  if (containersEl) {
    const attached = [];
    cachedDockerContainers.forEach((c) => {
      if (c.ports && c.ports.includes(volumeName)) attached.push(c.name || c.id);
    });
    if (attached.length > 0) {
      containersEl.innerHTML = attached
        .map((c) => `<span class="kind-badge kind-weekly">${escapeHtml(c)}</span>`)
        .join(" ");
    } else {
      containersEl.textContent = "No containers currently attached to this volume.";
    }
  }
}

function closeVolumeDetails() {
  document.getElementById("volume-details-modal")?.classList.add("hidden");
}

/**
 * Networks Tab: Fetch, Remove.
 */
async function fetchDockerNetworks() {
  const tbody = document.getElementById("docker-networks-tbody");
  if (!tbody) return;
  if (isFetchingDockerNetworks) return;
  isFetchingDockerNetworks = true;

  try {
    const resp = await apiFetch("/api/docker/networks");
    if (!resp.ok) return;
    const data = await resp.json();
    const networks = data.networks || [];

    if (networks.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No networks found.</td></tr>`;
      return;
    }

    tbody.innerHTML = networks.map((n) => {
      const safeId = escapeHtml(n.id);
      const safeName = escapeHtml(n.name);
      const safeDriver = escapeHtml(n.driver);
      const safeScope = escapeHtml(n.scope);
      const safeInternal = escapeHtml(n.internal);

      const isDefault = ["bridge", "host", "none"].includes(n.name);

      return `<tr>
        <td><span class="mono-text">${safeId}</span></td>
        <td><strong>${safeName}</strong></td>
        <td><span class="mono-text">${safeDriver}</span></td>
        <td>${safeScope}</td>
        <td>${safeInternal}</td>
        <td class="actions-col">
          ${
            !isDefault
              ? `<button type="button" class="btn-action btn-action-danger" data-net-delete="${safeId}" data-net-name="${safeName}">Remove</button>`
              : `<span style="color: var(--text-muted); font-size: 0.76rem;">Default</span>`
          }
        </td>
      </tr>`;
    }).join("");

    tbody.querySelectorAll("button[data-net-delete]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = btn.getAttribute("data-net-delete");
        const name = btn.getAttribute("data-net-name");
        handleRemoveNetwork(id, name);
      });
    });
  } catch (err) {
    console.warn("Failed to fetch Docker networks:", err);
  } finally {
    isFetchingDockerNetworks = false;
  }
}

async function handleRemoveNetwork(networkId, networkName) {
  if (!confirm(`Are you sure you want to remove network "${networkName}"?`)) {
    return;
  }

  try {
    const resp = await apiFetch(`/api/docker/networks/${encodeURIComponent(networkId)}`, {
      method: "DELETE",
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      alert(`Failed to remove network: ${data.detail || resp.statusText}`);
    }
  } catch (err) {
    alert(`Error: ${err.message}`);
  } finally {
    fetchDockerNetworks();
    fetchDockerStatus();
  }
}

/**
 * Updates Tab: Fetch, Config, Apply.
 */
async function fetchDockerUpdatesConfig() {
  const freqSelect = document.getElementById("docker-auto-update-freq");
  if (!freqSelect) return;

  try {
    const resp = await apiFetch("/api/docker/updates/config");
    if (!resp.ok) return;
    const data = await resp.json();
    if (data && data.frequency) {
      freqSelect.value = data.frequency;
    }
  } catch (err) {
    console.debug("Failed to fetch Docker updates config:", err);
  }
}

function generateUpdateRowHtml(u) {
  const safeName = escapeHtml(u.container_name || u.container_id);
  const safeImg = escapeHtml(u.image);
  const safeTag = escapeHtml(u.image_tag || "latest");
  const safeVer = u.image_version ? escapeHtml(u.image_version) : "";
  const safeStack = escapeHtml(u.stack || "-");
  const safeCur = escapeHtml(u.current_id || "--");
  const safeLat = escapeHtml(u.latest_id || "--");

  const badgeHtml = u.update_available
    ? `<span class="status-badge status-degraded"><span class="status-dot"></span>Update Available</span>`
    : `<span class="status-badge status-clean"><span class="status-dot"></span>Up to date</span>`;

  // External stack detection: if stack is present and managed_stack is false or not updatable
  const isExternalStack = Boolean(
    u.stack && (u.managed_stack === false || (!u.managed_stack && (u.can_update === false || u.update_available)))
  );

  let actionHtml = "";
  if (isExternalStack) {
    actionHtml = `<span class="kind-badge external-stack-badge" style="color: var(--text-muted); border-color: var(--border-color);" title="This container belongs to an external stack and cannot be managed or updated here">External Stack</span>`;
  } else if (u.update_available && u.can_update !== false) {
    actionHtml = `<button type="button" class="btn-action btn-action-success" data-update-cid="${escapeHtml(u.container_id)}" data-update-stack="${escapeHtml(u.stack || '')}">Update</button>`;
  } else {
    actionHtml = `<span style="color: var(--text-muted); font-size: 0.8rem;">Current</span>`;
  }

  const tagVersionHtml = `
    <span class="kind-badge kind-tag">${safeTag}</span>
    ${safeVer ? `<div class="tag-version-detail mono-text">${safeVer}</div>` : ""}
  `;

  return `
    <td><strong>${safeName}</strong></td>
    <td><span class="mono-text" style="color: var(--accent-primary);">${safeImg}</span></td>
    <td>${tagVersionHtml}</td>
    <td>${safeStack !== "-" ? `<span class="kind-badge kind-weekly">${safeStack}</span>` : "-"}</td>
    <td><span class="mono-text">${safeCur}</span></td>
    <td><span class="mono-text">${safeLat}</span></td>
    <td>${badgeHtml}</td>
    <td class="actions-col">
      ${actionHtml}
    </td>
  `;
}

function renderDockerUpdates(updates) {
  const tbody = document.getElementById("docker-updates-tbody");
  if (!tbody) return;

  const renderKey = JSON.stringify(updates);
  if (lastDockerUpdatesRenderKey === renderKey && tbody.querySelectorAll("tr[data-container-id]").length === updates.length) {
    return;
  }

  if (!updates || updates.length === 0) {
    lastDockerUpdatesRenderKey = renderKey;
    tbody.innerHTML = `<tr><td colspan="8" class="empty-state">No running containers detected to scan for updates.</td></tr>`;
    return;
  }

  // Remove placeholder rows (empty-state, loading-state)
  tbody.querySelectorAll(".empty-state, .loading-state").forEach((el) => {
    const tr = el.closest("tr");
    if (tr) tr.remove();
  });

  const existingRowMap = new Map();
  tbody.querySelectorAll("tr[data-container-id]").forEach((tr) => {
    existingRowMap.set(tr.dataset.containerId, tr);
  });

  const activeCids = new Set();

  updates.forEach((u, index) => {
    const cid = String(u.container_id || index);
    activeCids.add(cid);

    const rowContent = generateUpdateRowHtml(u);
    const rowKey = `${u.container_name}|${u.image}|${u.image_tag}|${u.image_version}|${u.stack}|${u.current_id}|${u.latest_id}|${u.update_available}|${u.managed_stack}|${u.can_update}`;

    let row = existingRowMap.get(cid);
    if (row) {
      if (row.dataset.rowKey !== rowKey) {
        row.innerHTML = rowContent;
        row.dataset.rowKey = rowKey;
        row.setAttribute("data-row-key", rowKey);
      }
    } else {
      row = document.createElement("tr");
      row.dataset.containerId = cid;
      row.setAttribute("data-container-id", cid);
      row.dataset.rowKey = rowKey;
      row.setAttribute("data-row-key", rowKey);
      row.innerHTML = rowContent;
      existingRowMap.set(cid, row);
    }

    if (tbody.children[index] !== row) {
      tbody.insertBefore(row, tbody.children[index] || null);
    }
  });

  existingRowMap.forEach((row, cid) => {
    if (!activeCids.has(cid)) {
      row.remove();
    }
  });

  lastDockerUpdatesRenderKey = renderKey;
}

async function fetchDockerUpdates({ force = false, background = false } = {}) {
  const tbody = document.getElementById("docker-updates-tbody");
  if (!tbody) return;
  if (isFetchingDockerUpdates) return;
  isFetchingDockerUpdates = true;

  const btnCheckUpdates = document.getElementById("btn-check-docker-updates");
  let originalBtnHtml = "";
  const isViewingUpdatesTab = currentDockerSubtab === "updates";

  if (btnCheckUpdates && !background && isViewingUpdatesTab) {
    originalBtnHtml = btnCheckUpdates.innerHTML;
    btnCheckUpdates.disabled = true;
    btnCheckUpdates.classList.add("loading");
    btnCheckUpdates.innerHTML = `
      <svg class="refresh-icon" viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="display:inline-block; margin-right:6px; vertical-align:text-bottom;">
        <polyline points="23 4 23 10 17 10"></polyline>
        <polyline points="1 20 1 14 7 14"></polyline>
        <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path>
      </svg>
      <span>Scanning...</span>
    `;
  }

  // Only show an initial loading placeholder if tbody is completely empty on initial load
  const hasExistingRows = tbody.querySelectorAll("tr[data-container-id]").length > 0;
  if (!background && isViewingUpdatesTab && !hasExistingRows && (tbody.children.length === 0 || (!cachedDockerUpdates && tbody.querySelector(".empty-state")))) {
    tbody.innerHTML = `<tr><td colspan="8" class="loading-state" style="text-align:center; padding: 24px; color: var(--text-muted);"><span class="spinner" style="display:inline-block; margin-right:8px;"></span> Checking container images for updates...</td></tr>`;
  }

  try {
    const url = force ? "/api/docker/updates?force=true" : "/api/docker/updates";
    const resp = await apiFetch(url);
    if (!resp.ok) return;
    const data = await resp.json();

    const freqSelect = document.getElementById("docker-auto-update-freq");
    if (freqSelect && data.config?.frequency) {
      freqSelect.value = data.config.frequency;
    }

    const updates = Array.isArray(data.updates) ? data.updates : [];
    cachedDockerUpdates = updates;

    const hasAnyRawUpdates = updates.some((u) => Boolean(u.update_available));
    const updatesAvailableCount = updates.filter(
      (u) => Boolean(u.update_available) && u.can_update !== false
    ).length;

    const badge = document.getElementById("subtab-badge-updates");
    if (badge) {
      badge.textContent = updatesAvailableCount;
      if (updatesAvailableCount > 0) {
        badge.classList.remove("hidden");
      } else {
        badge.classList.add("hidden");
      }
    }

    const applyAllBtn = document.getElementById("btn-apply-all-updates");
    if (applyAllBtn) {
      if (updatesAvailableCount === 0) {
        applyAllBtn.disabled = true;
        if (hasAnyRawUpdates) {
          applyAllBtn.title = "No managed container updates available";
        } else {
          applyAllBtn.title = "No updates available to pull";
        }
      } else {
        applyAllBtn.disabled = false;
        applyAllBtn.title = "Apply updates for all containers";
      }
    }

    renderDockerUpdates(updates);
  } catch (err) {
    console.warn("Failed to fetch Docker updates:", err);
  } finally {
    isFetchingDockerUpdates = false;
    if (btnCheckUpdates) {
      btnCheckUpdates.disabled = false;
      btnCheckUpdates.classList.remove("loading");
      btnCheckUpdates.innerHTML = originalBtnHtml || "Check Now";
    }
  }
}

async function handleSaveUpdateFrequency() {
  const freqSelect = document.getElementById("docker-auto-update-freq");
  const feedback = document.getElementById("docker-updates-feedback");
  const freq = freqSelect?.value || "disabled";

  try {
    const resp = await apiFetch("/api/docker/updates/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ frequency: freq }),
    });
    if (resp.ok) {
      if (feedback) {
        feedback.className = "auth-alert feedback-success";
        feedback.textContent = `Auto-check set to ${freq}.`;
        feedback.classList.remove("hidden");
        setTimeout(() => {
          feedback.classList.add("hidden");
        }, 3500);
      }
    } else {
      const errData = await resp.json().catch(() => ({}));
      if (feedback) {
        feedback.className = "auth-alert feedback-error";
        feedback.textContent = `Failed to save auto-check frequency: ${errData.detail || resp.statusText}`;
        feedback.classList.remove("hidden");
      }
    }
  } catch (err) {
    if (feedback) {
      feedback.className = "auth-alert feedback-error";
      feedback.textContent = `Error: ${err.message}`;
      feedback.classList.remove("hidden");
    } else {
      alert(`Error: ${err.message}`);
    }
  }
}

async function handleApplyUpdates(containers = null, stacks = null) {
  const feedback = document.getElementById("docker-updates-feedback");
  if (feedback) {
    feedback.className = "auth-alert";
    feedback.textContent = "Applying container / stack updates...";
    feedback.classList.remove("hidden");
  }

  try {
    const resp = await apiFetch("/api/docker/updates/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ containers, stacks }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      if (feedback) {
        feedback.className = "auth-alert feedback-success";
        feedback.textContent = "Updates applied successfully.";
      }
      lastDockerUpdatesRenderKey = "";
      fetchDockerUpdates({ force: true });
      fetchDockerContainers();
      fetchDockerStatus();
    } else {
      if (feedback) {
        feedback.className = "auth-alert feedback-error";
        feedback.textContent = data.detail || "Failed to apply updates.";
      }
    }
  } catch (err) {
    if (feedback) {
      feedback.className = "auth-alert feedback-error";
      feedback.textContent = `Error: ${err.message}`;
    }
  }
}

async function fetchUpdateSchedule() {
  if (isFetchingUpdateSchedule) return;
  isFetchingUpdateSchedule = true;

  const enabledToggle = document.getElementById("schedule-enabled-toggle");
  const daySelect = document.getElementById("schedule-day-select");
  const timeInput = document.getElementById("schedule-time-input");
  const badge = document.getElementById("update-schedule-badge");
  const nextRunEl = document.getElementById("schedule-next-run");

  try {
    const resp = await apiFetch("/api/docker/updates/schedule");
    if (resp.ok) {
      const data = await resp.json();
      const schedule = data.schedule || data;
      if (enabledToggle && schedule.enabled !== undefined) {
        enabledToggle.checked = Boolean(schedule.enabled);
      }
      if (daySelect && schedule.day) daySelect.value = schedule.day;
      if (timeInput && schedule.time) timeInput.value = schedule.time;
      if (badge) {
        badge.className = "status-badge";
        if (schedule.enabled) {
          badge.classList.add("status-clean");
          badge.textContent = "ENABLED";
        } else {
          badge.classList.add("status-disabled");
          badge.textContent = "DISABLED";
        }
      }
      if (nextRunEl) {
        nextRunEl.textContent = schedule.next_run
          ? `Next run: ${formatDateTime(schedule.next_run)}`
          : (schedule.enabled ? `Scheduled (${schedule.day || 'daily'} at ${schedule.time || '03:00'})` : "Schedule disabled");
      }
      return;
    }
  } catch (err) {
    console.debug("Schedule API fetch error, checking local store:", err);
  } finally {
    isFetchingUpdateSchedule = false;
  }

  // Fallback from localStorage
  try {
    const stored = JSON.parse(localStorage.getItem("systemalerts_update_schedule") || "{}");
    if (stored) {
      if (enabledToggle && stored.enabled !== undefined) enabledToggle.checked = Boolean(stored.enabled);
      if (daySelect && stored.day) daySelect.value = stored.day;
      if (timeInput && stored.time) timeInput.value = stored.time;
      if (badge) {
        badge.className = "status-badge";
        if (stored.enabled) {
          badge.classList.add("status-clean");
          badge.textContent = "ENABLED";
        } else {
          badge.classList.add("status-disabled");
          badge.textContent = "DISABLED";
        }
      }
      if (nextRunEl) {
        nextRunEl.textContent = stored.enabled
          ? `Scheduled: ${stored.day || 'daily'} at ${stored.time || '03:00'}`
          : "Schedule disabled";
      }
    }
  } catch {}
}

async function handleSaveUpdateSchedule() {
  const enabledToggle = document.getElementById("schedule-enabled-toggle");
  const daySelect = document.getElementById("schedule-day-select");
  const timeInput = document.getElementById("schedule-time-input");
  const feedback = document.getElementById("schedule-feedback");
  const badge = document.getElementById("update-schedule-badge");
  const nextRunEl = document.getElementById("schedule-next-run");
  const saveBtn = document.getElementById("btn-save-schedule");

  const enabled = Boolean(enabledToggle?.checked);
  const day = daySelect?.value || "daily";
  const time = timeInput?.value || "03:00";

  const payload = { enabled, day, time };

  if (saveBtn) saveBtn.disabled = true;

  try {
    const resp = await apiFetch("/api/docker/updates/schedule", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (resp.ok) {
      const data = await resp.json().catch(() => ({}));
      if (feedback) {
        feedback.className = "auth-alert feedback-success";
        feedback.textContent = data.message || "Automated update schedule saved.";
        feedback.classList.remove("hidden");
      }
      if (badge) {
        badge.className = "status-badge";
        if (enabled) {
          badge.classList.add("status-clean");
          badge.textContent = "ENABLED";
        } else {
          badge.classList.add("status-disabled");
          badge.textContent = "DISABLED";
        }
      }
      if (nextRunEl) {
        nextRunEl.textContent = data.next_run
          ? `Next run: ${formatDateTime(data.next_run)}`
          : (enabled ? `Scheduled (${day} at ${time})` : "Schedule disabled");
      }
      return;
    }
  } catch (err) {
    console.debug("Schedule POST endpoint fallback:", err);
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }

  // Graceful fallback to localStorage
  try {
    localStorage.setItem("systemalerts_update_schedule", JSON.stringify(payload));
    if (feedback) {
      feedback.className = "auth-alert feedback-success";
      feedback.textContent = `Automated update schedule saved (${day} at ${time}).`;
      feedback.classList.remove("hidden");
    }
    if (badge) {
      badge.className = "status-badge";
      if (enabled) {
        badge.classList.add("status-clean");
        badge.textContent = "ENABLED";
      } else {
        badge.classList.add("status-disabled");
        badge.textContent = "DISABLED";
      }
    }
    if (nextRunEl) {
      nextRunEl.textContent = enabled ? `Scheduled: ${day} at ${time}` : "Schedule disabled";
    }
  } catch (err) {
    if (feedback) {
      feedback.className = "auth-alert feedback-error";
      feedback.textContent = `Error saving schedule: ${err.message}`;
      feedback.classList.remove("hidden");
    }
  }
}

/* =========================================================================
 * Packages & Updates Manager View (#view-packages)
 * ========================================================================= */
let currentPackagesSubtab = "updates";
let cachedPackageUpdates = null;
let cachedInstalledPackages = null;
let cachedPythonPackages = null;
let currentPkgSearchQuery = "";
let currentPkgPage = 1;
let currentPkgTotalPages = 1;
let pkgInstalledSearchDebounceTimer = null;
let lastUpgradeTargetPackage = null;
let lastPythonUpgradeTargetPackage = null;
let lastUpgradeModalType = "apt";

function switchPackagesSubtab(subtab) {
  currentPackagesSubtab = subtab || "updates";
  const btnUpdates = document.getElementById("subtab-pkg-updates-btn");
  const btnInstalled = document.getElementById("subtab-pkg-installed-btn");
  const btnPython = document.getElementById("subtab-pkg-python-btn");
  const panelUpdates = document.getElementById("pkg-updates-subtab-panel");
  const panelInstalled = document.getElementById("pkg-installed-subtab-panel");
  const panelPython = document.getElementById("pkg-python-subtab-panel");

  btnUpdates?.classList.remove("active");
  btnUpdates?.setAttribute("aria-selected", "false");
  btnInstalled?.classList.remove("active");
  btnInstalled?.setAttribute("aria-selected", "false");
  btnPython?.classList.remove("active");
  btnPython?.setAttribute("aria-selected", "false");

  panelUpdates?.classList.add("hidden");
  panelInstalled?.classList.add("hidden");
  panelPython?.classList.add("hidden");

  if (currentPackagesSubtab === "installed") {
    btnInstalled?.classList.add("active");
    btnInstalled?.setAttribute("aria-selected", "true");
    panelInstalled?.classList.remove("hidden");
    if (!cachedInstalledPackages) {
      fetchInstalledPackages(currentPkgSearchQuery, currentPkgPage);
    }
  } else if (currentPackagesSubtab === "python") {
    btnPython?.classList.add("active");
    btnPython?.setAttribute("aria-selected", "true");
    panelPython?.classList.remove("hidden");
    if (!cachedPythonPackages) {
      fetchPythonPackages(false);
    }
  } else {
    btnUpdates?.classList.add("active");
    btnUpdates?.setAttribute("aria-selected", "true");
    panelUpdates?.classList.remove("hidden");
    if (!cachedPackageUpdates) {
      fetchPackageUpdates();
    }
  }
}

function renderPackagesOverviewRibbon(rebootStatus, updatesData) {
  const badge = document.getElementById("overview-packages-badge");
  const summary = document.getElementById("overview-packages-summary");
  const navBadgePackages = document.getElementById("nav-badge-packages");

  const rebootRequired = Boolean(rebootStatus && rebootStatus.reboot_required);
  const totalUpdates = updatesData ? (Number(updatesData.total_updates) || 0) : 0;
  const securityUpdates = updatesData ? (Number(updatesData.security_updates) || 0) : 0;
  const standardUpdates = updatesData ? (Number(updatesData.standard_updates) || 0) : 0;

  if (navBadgePackages) {
    if (totalUpdates > 0 || rebootRequired) {
      navBadgePackages.textContent = totalUpdates > 99 ? "99+" : (totalUpdates > 0 ? String(totalUpdates) : "!");
      navBadgePackages.classList.remove("hidden");
      if (securityUpdates > 0 || rebootRequired) {
        navBadgePackages.className = "nav-tab-badge badge-crit";
      } else {
        navBadgePackages.className = "nav-tab-badge";
      }
    } else {
      navBadgePackages.classList.add("hidden");
    }
  }

  if (badge && summary) {
    if (rebootRequired) {
      setBadgeIfChanged(badge, null, "status-badge status-danger", "REBOOT REQ");
      const pkgCount = rebootStatus.packages?.length || 0;
      setTextIfChanged(summary, pkgCount > 0 ? `${pkgCount} package(s) require reboot` : "System restart required");
    } else if (totalUpdates > 0) {
      if (securityUpdates > 0) {
        setBadgeIfChanged(badge, null, "status-badge status-danger", `${totalUpdates} UPDATES`);
        setTextIfChanged(summary, `${securityUpdates} security, ${standardUpdates} standard`);
      } else {
        setBadgeIfChanged(badge, null, "status-badge status-warn", `${totalUpdates} UPDATES`);
        setTextIfChanged(summary, `${standardUpdates} update(s) available`);
      }
    } else {
      setBadgeIfChanged(badge, null, "status-badge status-ok", "CLEAN");
      setTextIfChanged(summary, "0 updates, system clean");
    }
  }

  renderSystemUpdateNotice({
    total: totalUpdates,
    security: securityUpdates,
    standard: standardUpdates,
    rebootRequired,
  });
}

/**
 * Render or update the dismissible system update notice bar (#system-update-notice-bar).
 */
function renderSystemUpdateNotice({ total, security, standard, rebootRequired }) {
  const bar = document.getElementById("system-update-notice-bar");
  const msgEl = document.getElementById("system-update-notice-message");
  const badgeEl = document.getElementById("update-indicator-badge");
  const navBadge2 = document.getElementById("nav-badge-packages");

  if (navBadge2) {
    if (total > 0 || rebootRequired) {
      navBadge2.textContent = total > 99 ? "99+" : (total > 0 ? String(total) : "!");
      navBadge2.classList.remove("hidden");
      if (security > 0 || rebootRequired) {
        navBadge2.className = "nav-tab-badge badge-crit";
      } else {
        navBadge2.className = "nav-tab-badge";
      }
    } else {
      navBadge2.classList.add("hidden");
    }
  }

  const stateSignature = `${total}:${security}:${rebootRequired}`;
  if (lastKnownUpdateSignature !== null && lastKnownUpdateSignature !== stateSignature) {
    systemUpdateNoticeDismissed = false;
  }
  lastKnownUpdateSignature = stateSignature;

  if (!bar) return;

  if ((total === 0 && !rebootRequired) || systemUpdateNoticeDismissed) {
    bar.classList.add("hidden");
    return;
  }

  bar.classList.remove("notice-reboot", "notice-security", "notice-updates");

  if (rebootRequired) {
    bar.classList.add("notice-reboot");
    if (badgeEl) {
      badgeEl.textContent = "Reboot Required";
      badgeEl.className = "update-indicator-badge badge-danger";
    }
    if (msgEl) {
      if (total > 0) {
        msgEl.textContent = `Reboot Required: A system restart is required to apply kernel or core updates (${total} packages available, ${security} security).`;
      } else {
        msgEl.textContent = "Reboot Required: A system restart is required to apply kernel or core updates.";
      }
    }
  } else if (total > 0) {
    if (security > 0) {
      bar.classList.add("notice-security");
      if (badgeEl) {
        badgeEl.textContent = "Security Updates";
        badgeEl.className = "update-indicator-badge badge-danger";
      }
    } else {
      bar.classList.add("notice-updates");
      if (badgeEl) {
        badgeEl.textContent = "Updates Available";
        badgeEl.className = "update-indicator-badge";
      }
    }
    if (msgEl) {
      msgEl.textContent = `System Updates Available: ${total} package${total === 1 ? "" : "s"} available${security > 0 ? ` (${security} security)` : ""}.`;
    }
  }

  bar.classList.remove("hidden");
}

/**
 * Background polling for system package and container updates.
 * Periodically polls /api/packages/summary every 5 minutes.
 */
async function pollSystemUpdates() {
  if (!isAuthenticated) return;
  try {
    const resp = await apiFetch("/api/packages/summary");
    if (!resp.ok) return;
    const summary = await resp.json();
    if (!summary || !summary.supported) return;

    const total = Number(summary.total) || 0;
    const security = Number(summary.security_count) || 0;
    const standard = Math.max(0, total - security);
    const rebootRequired = Boolean(summary.reboot_required);

    renderSystemUpdateNotice({
      total,
      security,
      standard,
      rebootRequired,
    });

    fetchDockerUpdates({ background: true }).catch(() => {});
  } catch (err) {
    console.debug("pollSystemUpdates silent error:", err);
  }
}

function startSystemUpdatePolling() {
  stopSystemUpdatePolling();
  pollSystemUpdates();
  systemUpdatePollTimerId = window.setInterval(pollSystemUpdates, SYSTEM_UPDATE_POLL_INTERVAL_MS);
}

function stopSystemUpdatePolling() {
  if (systemUpdatePollTimerId) {
    window.clearInterval(systemUpdatePollTimerId);
    systemUpdatePollTimerId = null;
  }
}

async function fetchPackageUpdates(force = false) {
  const refreshBtn = document.getElementById("btn-refresh-packages");
  if (refreshBtn) refreshBtn.classList.add("loading");
  try {
    const url = `/api/packages/updates${force ? "?force=true" : ""}`;
    const resp = await apiFetch(url);
    if (!resp.ok) {
      if (resp.status === 404) {
        throw new Error("Update manager endpoint not found (HTTP 404). Please ensure Vigil backend is up to date.");
      }
      throw new Error(`Failed to load updates (HTTP ${resp.status})`);
    }
    const data = await resp.json();
    cachedPackageUpdates = data;

    // Render metrics strip
    const totalEl = document.getElementById("pkg-count-total");
    const secEl = document.getElementById("pkg-count-security");
    const stdEl = document.getElementById("pkg-count-standard");
    const subtabBadge = document.getElementById("subtab-pkg-updates-badge");
    const lastUpdatedEl = document.getElementById("packages-last-updated");

    if (totalEl) totalEl.textContent = data.total_updates || 0;
    if (secEl) secEl.textContent = data.security_updates || 0;
    if (stdEl) stdEl.textContent = data.standard_updates || 0;
    if (subtabBadge) subtabBadge.textContent = data.total_updates || 0;
    if (lastUpdatedEl) {
      const stamp = data.checked_at || data.last_checked;
      if (stamp) {
        lastUpdatedEl.textContent = `Catalog checked ${formatRelativeTime(stamp)}`;
      } else {
        lastUpdatedEl.textContent = `Updated ${formatTimeOnly(new Date())}`;
      }
    }

    // Render reboot banner
    const rebootBanner = document.getElementById("pkg-reboot-banner");
    const rebootStatusText = document.getElementById("pkg-reboot-status-text");
    const rebootPkgsWrapper = document.getElementById("pkg-reboot-pkgs-wrapper");
    const rebootPkgsList = document.getElementById("pkg-reboot-pkgs-list");

    const reboot = data.reboot_status || {};
    if (reboot.reboot_required) {
      rebootBanner?.classList.remove("hidden");
      if (rebootStatusText) {
        rebootStatusText.textContent = "REQUIRED";
        rebootStatusText.className = "status-badge status-danger";
      }
      if (reboot.packages && reboot.packages.length > 0) {
        rebootPkgsWrapper?.classList.remove("hidden");
        if (rebootPkgsList) {
          rebootPkgsList.innerHTML = reboot.packages
            .map((pkg) => `<span class="reboot-pkg-chip">${escapeHtml(pkg)}</span>`)
            .join("");
        }
      } else {
        rebootPkgsWrapper?.classList.add("hidden");
      }
    } else {
      rebootBanner?.classList.add("hidden");
      if (rebootStatusText) {
        rebootStatusText.textContent = "CLEAN";
        rebootStatusText.className = "status-badge status-ok";
      }
    }

    renderPackagesOverviewRibbon(reboot, data);
    renderPackageUpdatesTable(data.updates || []);
  } catch (err) {
    console.error("fetchPackageUpdates error:", err);
    showToast({
      title: "Package Updates Error",
      message: err.message,
      type: "error",
    });
  } finally {
    if (refreshBtn) refreshBtn.classList.remove("loading");
  }
}

function renderPackageUpdatesTable(updates) {
  const tbody = document.getElementById("pkg-updates-tbody");
  const installAllBtn = document.getElementById("btn-install-all-packages");
  if (installAllBtn) {
    installAllBtn.disabled = !Array.isArray(updates) || updates.length === 0;
  }
  if (!tbody) return;

  const filter = (document.getElementById("pkg-updates-search")?.value || "").toLowerCase().trim();
  const filtered = filter
    ? updates.filter((u) => (u.package && u.package.toLowerCase().includes(filter)) || (u.origin && u.origin.toLowerCase().includes(filter)))
    : updates;

  if (filtered.length === 0) {
    if (updates.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty-state">All packages are up to date! System is clean.</td></tr>`;
    } else {
      tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No updates match filter "${escapeHtml(filter)}".</td></tr>`;
    }
    return;
  }

  tbody.innerHTML = filtered
    .map((item) => {
      const isSecurity = Boolean(item.is_security);
      const badgeClass = isSecurity ? "pkg-type-security" : "pkg-type-standard";
      const typeLabel = isSecurity ? "SECURITY" : "STANDARD";
      return `
        <tr>
          <td>
            <strong class="mono-text">${escapeHtml(item.package)}</strong>
            ${item.architecture ? `<span class="text-muted text-xs"> (${escapeHtml(item.architecture)})</span>` : ""}
          </td>
          <td><span class="${badgeClass}">${typeLabel}</span></td>
          <td class="mono-text">${escapeHtml(item.current_version || "--")}</td>
          <td class="mono-text font-bold text-ok">${escapeHtml(item.candidate_version || "--")}</td>
          <td class="text-sm text-muted">${escapeHtml(item.origin || "apt")}</td>
          <td style="text-align: right;">
            <div class="btn-action-group" style="justify-content: flex-end;">
              <button
                type="button"
                class="btn-sm btn-secondary btn-upgrade-single"
                data-pkg="${escapeHtml(item.package)}"
                title="Simulate upgrade for ${escapeHtml(item.package)}"
              >
                Simulate
              </button>
              <button
                type="button"
                class="btn-sm btn-primary btn-install-single"
                data-pkg="${escapeHtml(item.package)}"
                title="Install update for ${escapeHtml(item.package)}"
              >
                Install
              </button>
            </div>
          </td>
        </tr>
      `;
    })
    .join("");

  tbody.querySelectorAll(".btn-upgrade-single").forEach((btn) => {
    btn.addEventListener("click", () => {
      const pkg = btn.getAttribute("data-pkg");
      simulatePackageUpgrade(pkg);
    });
  });

  tbody.querySelectorAll(".btn-install-single").forEach((btn) => {
    btn.addEventListener("click", () => {
      const pkg = btn.getAttribute("data-pkg");
      installPackageUpgrade(pkg);
    });
  });
}

async function fetchInstalledPackages(query = "", page = 1) {
  currentPkgSearchQuery = query || "";
  currentPkgPage = page < 1 ? 1 : page;
  const tbody = document.getElementById("pkg-installed-tbody");
  if (tbody && currentPackagesSubtab === "installed" && !cachedInstalledPackages) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty-state">Loading installed packages...</td></tr>`;
  }

  try {
    const url = `/api/packages/installed?q=${encodeURIComponent(currentPkgSearchQuery)}&page=${currentPkgPage}&limit=50`;
    const resp = await apiFetch(url);
    if (!resp.ok) {
      throw new Error(`Failed to load installed packages (HTTP ${resp.status})`);
    }
    const data = await resp.json();
    cachedInstalledPackages = data;

    const countPill = document.getElementById("pkg-installed-count-pill");
    const subtabBadge = document.getElementById("subtab-pkg-installed-badge");
    const paginationInfo = document.getElementById("pkg-pagination-info");
    const pageIndicator = document.getElementById("pkg-page-indicator");
    const btnPrev = document.getElementById("btn-pkg-prev");
    const btnNext = document.getElementById("btn-pkg-next");

    const total = Number(data.total) || 0;
    const pageNum = Number(data.page) || 1;
    const limit = Number(data.limit) || 50;
    const totalPages = Math.max(1, Math.ceil(total / limit));
    currentPkgTotalPages = totalPages;

    if (countPill) countPill.textContent = `${total.toLocaleString()} package${total === 1 ? "" : "s"}`;
    if (subtabBadge) subtabBadge.textContent = total > 999 ? `${(total / 1000).toFixed(1)}k` : String(total);

    const startIdx = total === 0 ? 0 : (pageNum - 1) * limit + 1;
    const endIdx = Math.min(pageNum * limit, total);
    if (paginationInfo) {
      paginationInfo.textContent = `Showing ${startIdx}-${endIdx} of ${total.toLocaleString()}`;
    }
    if (pageIndicator) {
      pageIndicator.textContent = `Page ${pageNum} of ${totalPages}`;
    }
    if (btnPrev) btnPrev.disabled = pageNum <= 1;
    if (btnNext) btnNext.disabled = pageNum >= totalPages;

    renderInstalledPackagesTable(data.packages || []);
  } catch (err) {
    console.error("fetchInstalledPackages error:", err);
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty-state error-text">Error loading packages: ${escapeHtml(err.message)}</td></tr>`;
    }
    showToast({
      title: "Package Inventory Error",
      message: err.message,
      type: "error",
    });
  }
}

function renderInstalledPackagesTable(packages) {
  const tbody = document.getElementById("pkg-installed-tbody");
  if (!tbody) return;

  if (packages.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty-state">No installed packages found matching query.</td></tr>`;
    return;
  }

  tbody.innerHTML = packages
    .map((pkg) => `
      <tr>
        <td><strong class="mono-text">${escapeHtml(pkg.name)}</strong></td>
        <td class="mono-text">${escapeHtml(pkg.version)}</td>
        <td class="text-muted mono-text text-sm">${escapeHtml(pkg.architecture || "--")}</td>
        <td><span class="status-badge status-ok">${escapeHtml(pkg.status || "installed")}</span></td>
        <td class="text-sm text-muted">${escapeHtml(pkg.summary || "")}</td>
      </tr>
    `)
    .join("");
}

async function refreshAptCache() {
  const btn = document.getElementById("btn-refresh-apt-cache");
  if (btn) btn.classList.add("loading");
  showToast({
    title: "Updating Catalog",
    message: "Running apt-get update to refresh repository package lists...",
    type: "info",
  });

  try {
    const resp = await apiFetch("/api/packages/refresh", { method: "POST" });
    if (!resp.ok) {
      throw new Error(`Failed to update catalog (HTTP ${resp.status})`);
    }
    const result = await resp.json();
    if (result.success) {
      showToast({
        title: "Catalog Updated",
        message: "Successfully synchronized package indices.",
        type: "success",
      });
      await fetchPackageUpdates(true);
    } else {
      showToast({
        title: "Catalog Update Warning",
        message: result.error || result.output || "apt update finished with non-zero status.",
        type: "warning",
      });
    }
  } catch (err) {
    showToast({
      title: "Update Failed",
      message: err.message,
      type: "error",
    });
  } finally {
    if (btn) btn.classList.remove("loading");
  }
}

/**
 * Stream a response chunk-by-chunk into a terminal DOM element.
 * Supports web stream reader (browser fetch) and falls back to resp.text() / resp.json()
 * for Node VM and non-streaming environments.
 */
async function streamTerminalResponse(resp, terminal, onChunk) {
  if (!resp) return "";

  if (resp.body && typeof resp.body.getReader === "function") {
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let accumulated = "";

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const text = decoder.decode(value, { stream: true });
        if (text) {
          accumulated += text;
          if (terminal) {
            terminal.textContent += text;
            terminal.scrollTop = terminal.scrollHeight;
          }
          if (typeof onChunk === "function") {
            onChunk(text);
          }
        }
      }
      const finalChunk = decoder.decode();
      if (finalChunk) {
        accumulated += finalChunk;
        if (terminal) {
          terminal.textContent += finalChunk;
          terminal.scrollTop = terminal.scrollHeight;
        }
        if (typeof onChunk === "function") {
          onChunk(finalChunk);
        }
      }
    } catch (err) {
      console.error("Stream reader error:", err);
      throw err;
    }
    return accumulated;
  }

  // Graceful fallback for non-streaming or Node VM environments
  let output = "";
  if (typeof resp.text === "function") {
    try {
      output = await resp.text();
    } catch (_) {
      output = "";
    }
  }

  if (!output && typeof resp.json === "function") {
    try {
      const data = await resp.json();
      if (data && typeof data === "object") {
        if (data.success === false) {
          output = `[FAILED] ${data.error || data.detail || "Operation failed"}\n${data.output || ""}`;
        } else {
          output =
            data.output ||
            data.stdout ||
            data.detail ||
            data.error ||
            (typeof data === "string" ? data : JSON.stringify(data));
        }
      }
    } catch (_) {
      output = "";
    }
  } else if (output) {
    try {
      const parsed = JSON.parse(output);
      if (parsed && typeof parsed === "object") {
        if (parsed.success === false) {
          output = `[FAILED] ${parsed.error || parsed.detail || "Operation failed"}\n${parsed.output || ""}`;
        } else {
          output =
            parsed.output ||
            parsed.stdout ||
            parsed.detail ||
            parsed.error ||
            JSON.stringify(parsed);
        }
      }
    } catch (_) {
      // It is plain text, keep output as-is
    }
  }

  if (terminal) {
    terminal.textContent += output;
    terminal.scrollTop = terminal.scrollHeight;
  }
  if (typeof onChunk === "function") {
    onChunk(output);
  }
  return output;
}

async function simulatePackageUpgrade(packageName = null) {
  lastUpgradeTargetPackage = packageName;
  lastUpgradeModalType = "apt";
  const modal = document.getElementById("pkg-upgrade-modal");
  const modalTitle = document.getElementById("pkg-upgrade-modal-title");
  const modalSubtitle = document.getElementById("pkg-upgrade-modal-subtitle");
  const terminal = document.getElementById("pkg-upgrade-terminal");
  const installModalBtn = document.getElementById("btn-pkg-modal-install");

  if (modalTitle) {
    modalTitle.textContent = packageName
      ? `Simulate Upgrade: ${packageName}`
      : "Simulated System Upgrade";
  }
  if (modalSubtitle) {
    modalSubtitle.textContent = packageName
      ? `Dry run: apt-get -s install --only-upgrade ${packageName}`
      : "Dry run: apt-get -s dist-upgrade (safe, non-destructive)";
  }
  if (terminal) {
    terminal.textContent = "";
  }
  if (installModalBtn) {
    installModalBtn.textContent = "Install Now";
    installModalBtn.classList.add("hidden");
  }
  modal?.classList.remove("hidden");

  try {
    const payload = {
      packages: packageName ? [packageName] : [],
      dry_run: true,
      stream: true,
    };
    const resp = await apiFetch("/api/packages/upgrade?stream=true", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const errText = await (typeof resp.text === "function" ? resp.text() : resp.json()).catch(() => "");
      let errMsg = `Simulation request failed (HTTP ${resp.status})`;
      if (typeof errText === "object" && errText !== null) {
        errMsg = errText.detail || errText.error || errMsg;
      } else if (typeof errText === "string" && errText) {
        try {
          const parsed = JSON.parse(errText);
          if (parsed.detail || parsed.error) errMsg = parsed.detail || parsed.error;
        } catch (_) {
          errMsg = errText;
        }
      }
      throw new Error(errMsg);
    }
    await streamTerminalResponse(resp, terminal);
    if (installModalBtn) {
      installModalBtn.textContent = "Install Now";
      installModalBtn.classList.remove("hidden");
    }
  } catch (err) {
    if (terminal) {
      terminal.textContent += `\nError executing simulation: ${err.message}`;
    }
  }
}

async function installPackageUpgrade(packageName = null) {
  const confirmMsg = packageName
    ? `Are you sure you want to install update for "${packageName}"?`
    : "Are you sure you want to install all available package updates?";
  if (!confirm(confirmMsg)) {
    return;
  }

  lastUpgradeTargetPackage = packageName;
  lastUpgradeModalType = "apt";
  const modal = document.getElementById("pkg-upgrade-modal");
  const modalTitle = document.getElementById("pkg-upgrade-modal-title");
  const modalSubtitle = document.getElementById("pkg-upgrade-modal-subtitle");
  const terminal = document.getElementById("pkg-upgrade-terminal");
  const installModalBtn = document.getElementById("btn-pkg-modal-install");

  if (modalTitle) {
    modalTitle.textContent = packageName
      ? `Installing Package: ${packageName}`
      : "Installing System Updates";
  }
  if (modalSubtitle) {
    modalSubtitle.textContent = packageName
      ? `Executing: apt-get -y install --only-upgrade ${packageName}`
      : "Executing: apt-get -y dist-upgrade (non-interactive)";
  }
  if (terminal) {
    terminal.textContent = "";
  }
  installModalBtn?.classList.add("hidden");
  modal?.classList.remove("hidden");

  try {
    const payload = {
      packages: packageName ? [packageName] : [],
      dry_run: false,
      stream: true,
    };
    const resp = await apiFetch("/api/packages/upgrade?stream=true", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const errText = await (typeof resp.text === "function" ? resp.text() : resp.json()).catch(() => "");
      let errMsg = `Package upgrade failed (HTTP ${resp.status})`;
      if (typeof errText === "object" && errText !== null) {
        errMsg = errText.detail || errText.error || errMsg;
      } else if (typeof errText === "string" && errText) {
        try {
          const parsed = JSON.parse(errText);
          if (parsed.detail || parsed.error) errMsg = parsed.detail || parsed.error;
        } catch (_) {
          errMsg = errText;
        }
      }
      throw new Error(errMsg);
    }

    const output = await streamTerminalResponse(resp, terminal);

    const hasError = output.includes("[FAILED]") || output.includes("[ERROR]");
    if (!hasError) {
      showToast({
        title: "Package Upgrade Complete",
        message: packageName
          ? `Successfully upgraded ${packageName}.`
          : "All available package updates installed.",
        type: "success",
      });
    } else {
      showToast({
        title: "Package Upgrade Failed",
        message: "apt-get encountered errors or returned a non-zero exit code.",
        type: "danger",
      });
    }

    await fetchPackageUpdates(true);
    pollSystemUpdates();
  } catch (err) {
    if (terminal) {
      terminal.textContent += `\nError installing packages: ${err.message}`;
    }
    showToast({
      title: "Package Upgrade Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  }
}

async function fetchPythonPackages(checkPypi = false) {
  const tbody = document.getElementById("pkg-python-tbody");
  const btnCheck = document.getElementById("btn-check-pypi-updates");
  if (btnCheck && checkPypi) btnCheck.classList.add("loading");

  if (tbody && !cachedPythonPackages) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-state">Loading Vigil Python environment...</td></tr>';
  }

  if (checkPypi) {
    showToast({
      title: "Querying PyPI",
      message: "Checking PyPI repositories for compatible package updates...",
      type: "info",
    });
  }

  try {
    const url = checkPypi ? "/api/packages/python/check" : "/api/packages/python";
    const resp = await apiFetch(url, {
      method: checkPypi ? "POST" : "GET",
    });
    if (!resp.ok) {
      throw new Error(`Failed to load Python packages (HTTP ${resp.status})`);
    }
    const data = await resp.json();
    cachedPythonPackages = data;

    const runtimeEl = document.getElementById("pkg-py-runtime-version");
    const countDirectEl = document.getElementById("pkg-py-count-direct");
    const countUpdatesEl = document.getElementById("pkg-py-count-updates");
    const countHeldEl = document.getElementById("pkg-py-count-held");
    const subtabBadge = document.getElementById("subtab-pkg-python-badge");

    if (runtimeEl) {
      runtimeEl.textContent = `Python ${data.python_version || "--"}`;
    }
    if (countDirectEl) {
      countDirectEl.textContent = String(data.summary?.direct ?? 0);
    }
    if (countUpdatesEl) {
      countUpdatesEl.textContent = String(data.summary?.updates_available ?? 0);
    }
    if (countHeldEl) {
      countHeldEl.textContent = String(data.summary?.major_held ?? 0);
    }
    if (subtabBadge) {
      const updatesCount = Number(data.summary?.updates_available ?? 0);
      const totalCount = Number(data.summary?.total ?? 0);
      subtabBadge.textContent = String(updatesCount > 0 ? updatesCount : totalCount);
    }

    renderPythonPackagesTable();

    if (checkPypi) {
      showToast({
        title: "PyPI Check Complete",
        message: `Found ${data.summary?.updates_available ?? 0} safe updates available.`,
        type: "success",
      });
    }
  } catch (err) {
    console.error("fetchPythonPackages error:", err);
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty-state error-text">Error loading Python packages: ${escapeHtml(err.message)}</td></tr>`;
    }
    showToast({
      title: "Python Packages Error",
      message: err.message,
      type: "error",
    });
  } finally {
    if (btnCheck) btnCheck.classList.remove("loading");
  }
}

function renderPythonPackagesTable() {
  const tbody = document.getElementById("pkg-python-tbody");
  if (!tbody) return;

  const packages = cachedPythonPackages?.packages || [];
  if (!Array.isArray(packages) || packages.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-state">No Python packages found.</td></tr>';
    return;
  }

  const searchInput = document.getElementById("pkg-python-search");
  const query = (searchInput?.value || "").trim().toLowerCase();
  const roleSelect = document.getElementById("pkg-python-role-filter");
  const roleFilter = roleSelect?.value || "all";

  const filtered = packages.filter((pkg) => {
    if (roleFilter === "direct") {
      const isDirect = Boolean(pkg.is_direct ?? pkg.direct ?? (pkg.role === "direct"));
      if (!isDirect) return false;
    } else if (roleFilter === "updates") {
      if (!pkg.update_available) return false;
    }

    if (query) {
      const name = (pkg.name || "").toLowerCase();
      const constraint = (pkg.constraint || "").toLowerCase();
      const summary = (pkg.summary || "").toLowerCase();
      if (!name.includes(query) && !constraint.includes(query) && !summary.includes(query)) {
        return false;
      }
    }
    return true;
  });

  if (filtered.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-state">No Python packages found matching filter criteria.</td></tr>';
    return;
  }

  tbody.innerHTML = filtered
    .map((pkg) => {
      const isDirect = Boolean(pkg.is_direct ?? pkg.direct ?? (pkg.role === "direct"));
      const roleBadge = isDirect
        ? '<span class="status-badge status-ok">DIRECT</span>'
        : '<span class="status-badge status-muted">TRANSITIVE</span>';
      const constraintText = escapeHtml(pkg.constraint || "lockfile");

      const installedVer = escapeHtml(pkg.installed_version || "--");
      const latestCompat = escapeHtml(pkg.latest_compatible_version || pkg.installed_version || "--");
      const isHeld = Boolean(pkg.major_held);
      const heldNote = isHeld && pkg.latest_version
        ? ` <span class="text-xs text-muted font-normal">(v${escapeHtml(pkg.latest_version)} held)</span>`
        : "";

      let statusBadge = "";
      if (pkg.update_available) {
        statusBadge = '<span class="status-badge status-warn">UPDATE AVAILABLE</span>';
      } else if (pkg.major_held) {
        const heldTitle = escapeHtml(`Newer major version (${pkg.latest_version || ""}) held back by SemVer safety bound`);
        statusBadge = `<span class="status-badge status-ok" title="${heldTitle}">MAJOR HELD</span>`;
      } else {
        statusBadge = '<span class="status-badge status-ok">UP TO DATE</span>';
      }

      return `
        <tr>
          <td>
            <div>
              <strong class="mono-text">${escapeHtml(pkg.name)}</strong>
              ${pkg.summary ? `<div class="text-xs text-muted" style="margin-top: 2px;">${escapeHtml(pkg.summary)}</div>` : ""}
            </div>
          </td>
          <td>
            <div style="display: flex; flex-direction: column; gap: 4px; align-items: flex-start;">
              ${roleBadge}
              <span class="mono-text text-xs text-muted">${constraintText}</span>
            </div>
          </td>
          <td>
            <span class="mono-text">${installedVer}</span>
          </td>
          <td>
            <span class="mono-text font-bold ${pkg.update_available ? 'text-ok' : ''}">${latestCompat}</span>${heldNote}
          </td>
          <td>
            ${statusBadge}
          </td>
          <td style="text-align: right;">
            <div class="btn-action-group" style="justify-content: flex-end;">
              <button
                type="button"
                class="btn-sm btn-secondary btn-py-simulate"
                data-py-pkg="${escapeHtml(pkg.name)}"
                title="Simulate upgrade for ${escapeHtml(pkg.name)}"
              >
                Simulate
              </button>
              <button
                type="button"
                class="btn-sm btn-primary btn-py-upgrade"
                data-py-pkg="${escapeHtml(pkg.name)}"
                title="Safe update for ${escapeHtml(pkg.name)}"
              >
                Safe Update
              </button>
            </div>
          </td>
        </tr>
      `;
    })
    .join("");

  tbody.querySelectorAll(".btn-py-simulate").forEach((btn) => {
    btn.addEventListener("click", () => {
      const name = btn.getAttribute("data-py-pkg");
      simulatePythonPackageUpgrade(name);
    });
  });

  tbody.querySelectorAll(".btn-py-upgrade").forEach((btn) => {
    btn.addEventListener("click", () => {
      const name = btn.getAttribute("data-py-pkg");
      upgradePythonPackage(name);
    });
  });
}

async function simulatePythonPackageUpgrade(packageName = null) {
  lastPythonUpgradeTargetPackage = packageName;
  lastUpgradeModalType = "python";
  const modal = document.getElementById("pkg-upgrade-modal");
  const modalTitle = document.getElementById("pkg-upgrade-modal-title");
  const modalSubtitle = document.getElementById("pkg-upgrade-modal-subtitle");
  const terminal = document.getElementById("pkg-upgrade-terminal");
  const installModalBtn = document.getElementById("btn-pkg-modal-install");

  if (modalTitle) {
    modalTitle.textContent = packageName
      ? `Simulate Python Upgrade: ${packageName}`
      : "Simulated Python Environment Upgrade";
  }
  if (modalSubtitle) {
    modalSubtitle.textContent = packageName
      ? `Dry run: uv lock --dry-run -P ${packageName}`
      : "Dry run: uv lock --dry-run (safe, non-destructive resolution)";
  }
  if (terminal) {
    terminal.textContent = "";
  }
  installModalBtn?.classList.add("hidden");
  modal?.classList.remove("hidden");

  try {
    const payload = {
      packages: packageName ? [packageName] : null,
      dry_run: true,
      restart_service: false,
      stream: true,
    };
    const resp = await apiFetch("/api/packages/python/upgrade", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const errData = await (typeof resp.json === "function" ? resp.json() : resp.text()).catch(() => ({}));
      const errMsg =
        (typeof errData === "object" ? errData.detail || errData.error : errData) ||
        `Simulation request failed (HTTP ${resp.status})`;
      throw new Error(errMsg);
    }
    await streamTerminalResponse(resp, terminal);
    if (installModalBtn) {
      installModalBtn.textContent = "Safe Upgrade Now";
      installModalBtn.classList.remove("hidden");
    }
  } catch (err) {
    if (terminal) {
      terminal.textContent += `\nError executing simulation: ${err.message}`;
    }
  }
}

async function upgradePythonPackage(packageName = null) {
  const confirmMsg = packageName
    ? `Are you sure you want to safe-upgrade Python package "${packageName}"?`
    : "Are you sure you want to safe-upgrade all compatible Python packages with pre-restart smoke test & rollback protection?";
  if (!confirm(confirmMsg)) {
    return;
  }

  lastPythonUpgradeTargetPackage = packageName;
  lastUpgradeModalType = "python";
  const modal = document.getElementById("pkg-upgrade-modal");
  const modalTitle = document.getElementById("pkg-upgrade-modal-title");
  const modalSubtitle = document.getElementById("pkg-upgrade-modal-subtitle");
  const terminal = document.getElementById("pkg-upgrade-terminal");
  const installModalBtn = document.getElementById("btn-pkg-modal-install");
  const restartToggle = document.getElementById("pkg-python-restart-toggle");
  const shouldRestart = Boolean(restartToggle?.checked);

  if (modalTitle) {
    modalTitle.textContent = packageName
      ? `Safe Upgrading: ${packageName}`
      : "Safe Upgrading Python Environment";
  }
  if (modalSubtitle) {
    modalSubtitle.textContent = "uv sync with automated pre-restart smoke test & auto-rollback";
  }
  if (terminal) {
    terminal.textContent = "";
  }
  installModalBtn?.classList.add("hidden");
  modal?.classList.remove("hidden");

  try {
    const payload = {
      packages: packageName ? [packageName] : null,
      dry_run: false,
      restart_service: shouldRestart,
      stream: true,
    };
    const resp = await apiFetch("/api/packages/python/upgrade", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const errData = await (typeof resp.json === "function" ? resp.json() : resp.text()).catch(() => ({}));
      const errMsg =
        (typeof errData === "object" ? errData.detail || errData.error : errData) ||
        `Upgrade failed (HTTP ${resp.status})`;
      throw new Error(errMsg);
    }

    const output = await streamTerminalResponse(resp, terminal);

    const hasFailed =
      output.includes("[FAILED]") ||
      output.includes("[ERROR]") ||
      output.includes("[ROLLBACK]");
    if (!hasFailed) {
      showToast({
        title: "Python Package Upgraded",
        message: packageName ? `Successfully updated ${packageName}` : "Successfully updated Python environment",
        type: "success",
      });
    } else {
      showToast({
        title: "Upgrade Rolled Back",
        message: "Smoke test failed or upgrade error; previous uv.lock restored.",
        type: "error",
      });
    }

    await fetchPythonPackages(false);
  } catch (err) {
    if (terminal) {
      terminal.textContent += `\nError executing upgrade: ${err.message}`;
    }
    showToast({
      title: "Python Upgrade Error",
      message: err.message,
      type: "error",
    });
  }
}

function closePackageUpgradeModal() {
  document.getElementById("pkg-upgrade-modal")?.classList.add("hidden");
}

function refreshPackagesView() {
  fetchPackageUpdates();
  // Preload installed packages in background so its badge and list are preloaded
  fetchInstalledPackages(currentPkgSearchQuery, currentPkgPage);
  // Preload python packages in background
  fetchPythonPackages(false);
}

/* =========================================================================
 * Vigil Sentinel: Service Uptime & Health Probes Controllers
 * ========================================================================= */

let cachedProbesData = null;
let isFetchingProbes = false;

/**
 * Fetch all configured endpoint probes and summary metrics from GET /api/probes.
 */
async function fetchProbes(isUserTriggered = false) {
  if (isFetchingProbes) return;
  isFetchingProbes = true;

  const refreshBtn = document.getElementById("refresh-probes-btn");
  if (refreshBtn) refreshBtn.classList.add("loading");

  try {
    const resp = await apiFetch("/api/probes");
    if (!resp.ok) {
      throw new Error(`Failed to fetch probes (HTTP ${resp.status})`);
    }
    const data = await resp.json();
    cachedProbesData = data;
    renderProbes(data);
    if (isUserTriggered) {
      showToast({
        title: "Probes Refreshed",
        message: "Endpoint statuses and latency metrics updated.",
        type: "success",
      });
    }
  } catch (err) {
    console.error("fetchProbes error:", err);
    showToast({
      title: "Probes Sync Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  } finally {
    isFetchingProbes = false;
    if (refreshBtn) refreshBtn.classList.remove("loading");
  }
}

/**
 * Render probe cards, summary status ribbon, and empty state.
 */
function renderProbes(data) {
  if (!data || typeof data !== "object") return;

  const probes = Array.isArray(data.probes) ? data.probes : [];
  const summary = data.summary || {
    total: probes.length,
    up: probes.filter((p) => p.last_status === "up").length,
    down: probes.filter((p) => p.last_status === "down").length,
    avg_latency: 0,
  };

  // Update Status Ribbon Counters
  const totalCountEl = document.getElementById("probes-total-count");
  if (totalCountEl) totalCountEl.textContent = String(summary.total ?? probes.length);

  const upCountEl = document.getElementById("probes-up-count");
  if (upCountEl) upCountEl.textContent = String(summary.up ?? 0);

  const downCountEl = document.getElementById("probes-down-count");
  if (downCountEl) downCountEl.textContent = String(summary.down ?? 0);

  const avgLatencyEl = document.getElementById("probes-avg-latency");
  if (avgLatencyEl) {
    const avg = Number(summary.avg_latency || 0);
    avgLatencyEl.textContent = avg > 0 ? `${avg.toFixed(1)} ms` : "-- ms";
  }

  // Last Updated text
  const lastUpdatedEl = document.getElementById("probes-last-updated");
  if (lastUpdatedEl) {
    lastUpdatedEl.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  }

  // Sidebar badge
  const navBadge = document.getElementById("nav-tab-probes-badge");
  if (navBadge) {
    if (summary.down > 0) {
      navBadge.textContent = String(summary.down);
      navBadge.className = "nav-tab-badge danger";
      navBadge.classList.remove("hidden");
    } else if (summary.total > 0) {
      navBadge.textContent = String(summary.total);
      navBadge.className = "nav-tab-badge";
      navBadge.classList.remove("hidden");
    } else {
      navBadge.classList.add("hidden");
    }
  }

  const emptyState = document.getElementById("probes-empty-state");
  const cardsGrid = document.getElementById("probes-cards-grid");
  if (!cardsGrid) return;

  if (probes.length === 0) {
    emptyState?.classList.remove("hidden");
    cardsGrid.innerHTML = "";
    return;
  }

  emptyState?.classList.add("hidden");

  // Render cards
  cardsGrid.innerHTML = probes
    .map((probe) => {
      const status = probe.last_status || "pending";
      let statusBadgeClass = "status-muted";
      let statusLabel = "PENDING";
      if (status === "up") {
        statusBadgeClass = "status-ok";
        statusLabel = "OPERATIONAL";
      } else if (status === "down") {
        statusBadgeClass = "status-danger";
        statusLabel = "DOWN";
      } else if (status === "degraded") {
        statusBadgeClass = "status-warn";
        statusLabel = "DEGRADED";
      }

      const uptimePct = Number(probe.uptime_24h_percent ?? 100).toFixed(1);
      const latencyVal =
        probe.last_response_time_ms && Number(probe.last_response_time_ms) > 0
          ? `${Math.round(probe.last_response_time_ms)} ms`
          : "--";

      // SSL Days Remaining badge
      let sslBadgeHtml = "";
      if (probe.type === "http" && probe.ssl_check) {
        const days = probe.ssl_days_remaining;
        if (days !== null && days !== undefined) {
          if (days <= 0) {
            sslBadgeHtml = `<span class="probe-ssl-badge ssl-danger" title="SSL Certificate has expired!">SSL Expired</span>`;
          } else if (days <= 14) {
            sslBadgeHtml = `<span class="probe-ssl-badge ssl-danger" title="Expires in ${days} days — Renewal needed urgently!">SSL: ${days}d</span>`;
          } else if (days <= 30) {
            sslBadgeHtml = `<span class="probe-ssl-badge ssl-warning" title="Expires in ${days} days">SSL: ${days}d</span>`;
          } else {
            sslBadgeHtml = `<span class="probe-ssl-badge ssl-good" title="Certificate valid for ${days} days">SSL: ${days}d</span>`;
          }
        } else {
          sslBadgeHtml = `<span class="probe-ssl-badge ssl-warning" title="SSL certificate check pending">SSL: --</span>`;
        }
      }

      // 20-pip Heartbeat Bar
      const recentChecks = Array.isArray(probe.recent_checks) ? [...probe.recent_checks] : [];
      while (recentChecks.length < 20) {
        recentChecks.unshift("pending");
      }
      const last20 = recentChecks.slice(-20);
      const pipsHtml = last20
        .map((chk) => {
          const pipStatus = chk || "pending";
          const title = `Check: ${pipStatus.toUpperCase()}`;
          return `<div class="heartbeat-pip pip-${escapeHtml(pipStatus)}" title="${title}"></div>`;
        })
        .join("");

      const lastCheckedTime = probe.last_checked_at
        ? formatRelativeTime(probe.last_checked_at)
        : "Never checked";

      const cardStatusClass = `status-${status}`;
      const disabledClass = !probe.enabled ? "probe-disabled" : "";

      return `
        <article class="probe-card ${cardStatusClass} ${disabledClass}" id="probe-card-${probe.id}">
          <div class="probe-card-header">
            <div class="probe-header-title-col">
              <span class="probe-card-name" title="${escapeHtml(probe.name)}">${escapeHtml(probe.name)}</span>
              <span class="probe-card-target" title="${escapeHtml(probe.target)}">${escapeHtml(probe.target)}</span>
            </div>
            <div class="probe-header-badges-col">
              <span class="probe-type-badge type-${escapeHtml(probe.type)}">${escapeHtml(probe.type.toUpperCase())}</span>
              <span class="status-badge ${statusBadgeClass}">${statusLabel}</span>
            </div>
          </div>

          <div class="probe-metrics-row">
            <div class="probe-metric-item">
              <span class="probe-metric-label">24h Uptime</span>
              <span class="probe-metric-value">${uptimePct}%</span>
            </div>
            <div class="probe-metric-item">
              <span class="probe-metric-label">Latency</span>
              <span class="probe-metric-value">${latencyVal}</span>
            </div>
            <div class="probe-metric-item">
              <span class="probe-metric-label">Interval</span>
              <span class="probe-metric-value">${probe.interval_seconds || 60}s</span>
            </div>
            ${sslBadgeHtml ? `<div class="probe-metric-item"><span class="probe-metric-label">SSL Expiry</span>${sslBadgeHtml}</div>` : ""}
          </div>

          <div class="probe-heartbeat-container">
            <div class="probe-heartbeat-header">
              <span>Heartbeat History (last 20 checks)</span>
              <span>${escapeHtml(lastCheckedTime)}</span>
            </div>
            <div class="probe-heartbeat-bar" role="meter" aria-label="Recent check results">
              ${pipsHtml}
            </div>
          </div>

          <div class="probe-actions-bar">
            <div class="probe-actions-group">
              <button
                type="button"
                class="btn-probe-action"
                id="btn-probe-test-${probe.id}"
                data-action="test"
                data-id="${probe.id}"
                title="Immediately run diagnostic check"
              >
                <svg viewBox="0 0 24 24" width="13" height="13" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <polygon points="5 3 19 12 5 21 5 3"></polygon>
                </svg>
                <span>Check Now</span>
              </button>
            </div>
            <div class="probe-actions-group">
              <button
                type="button"
                class="btn-probe-action"
                id="btn-probe-edit-${probe.id}"
                data-action="edit"
                data-id="${probe.id}"
                title="Edit probe settings"
              >
                <svg viewBox="0 0 24 24" width="13" height="13" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
                  <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>
                </svg>
                <span>Edit</span>
              </button>
              <button
                type="button"
                class="btn-probe-action danger"
                id="btn-probe-delete-${probe.id}"
                data-action="delete"
                data-id="${probe.id}"
                title="Delete this probe"
              >
                <svg viewBox="0 0 24 24" width="13" height="13" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <polyline points="3 6 5 6 21 6"></polyline>
                  <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                </svg>
                <span>Delete</span>
              </button>
            </div>
          </div>
        </article>
      `;
    })
    .join("");
}

/**
 * Handle switching form input fields dynamically when Probe Type changes.
 */
function handleProbeTypeChange(selectedType) {
  const httpFields = document.getElementById("probe-form-http-fields");
  const targetInput = document.getElementById("probe-form-target");
  const targetHint = document.getElementById("probe-target-hint");

  if (selectedType === "http") {
    httpFields?.classList.remove("hidden");
    if (targetInput) targetInput.placeholder = "https://example.com or http://192.168.1.100:8080";
    if (targetHint) targetHint.textContent = "Full URL for HTTP/HTTPS (e.g. https://api.example.com/health).";
  } else if (selectedType === "tcp") {
    httpFields?.classList.add("hidden");
    if (targetInput) targetInput.placeholder = "hostname:port (e.g. 192.168.1.50:5432 or db.local:3306)";
    if (targetHint) targetHint.textContent = "Host and port separated by colon (e.g. 1.1.1.1:53).";
  } else if (selectedType === "ping") {
    httpFields?.classList.add("hidden");
    if (targetInput) targetInput.placeholder = "hostname or IP (e.g. 8.8.8.8 or gateway.local)";
    if (targetHint) targetHint.textContent = "Hostname or IP address for ICMP echo.";
  }
}

/**
 * Open the Add Probe modal.
 */
function openAddProbeModal() {
  const modal = document.getElementById("probe-modal");
  const title = document.getElementById("probe-modal-title");
  const idInput = document.getElementById("probe-form-id");
  const nameInput = document.getElementById("probe-form-name");
  const typeSelect = document.getElementById("probe-form-type");
  const targetInput = document.getElementById("probe-form-target");
  const intervalSelect = document.getElementById("probe-form-interval");
  const timeoutInput = document.getElementById("probe-form-timeout");
  const statusInput = document.getElementById("probe-form-status");
  const keywordInput = document.getElementById("probe-form-keyword");
  const sslCheckbox = document.getElementById("probe-form-ssl");
  const enabledCheckbox = document.getElementById("probe-form-enabled");
  const feedback = document.getElementById("probe-modal-feedback");

  if (!modal) return;

  if (title) title.textContent = "Add New Endpoint Probe";
  if (idInput) idInput.value = "";
  if (nameInput) nameInput.value = "";
  if (typeSelect) typeSelect.value = "http";
  if (targetInput) targetInput.value = "";
  if (intervalSelect) intervalSelect.value = "60";
  if (timeoutInput) timeoutInput.value = "5";
  if (statusInput) statusInput.value = "200";
  if (keywordInput) keywordInput.value = "";
  if (sslCheckbox) sslCheckbox.checked = true;
  if (enabledCheckbox) enabledCheckbox.checked = true;

  if (feedback) {
    feedback.textContent = "";
    feedback.classList.add("hidden");
  }

  handleProbeTypeChange("http");
  modal.classList.remove("hidden");
  nameInput?.focus();
}

/**
 * Open the Edit Probe modal with pre-populated values.
 */
function openEditProbeModal(probeId) {
  const modal = document.getElementById("probe-modal");
  const title = document.getElementById("probe-modal-title");
  const idInput = document.getElementById("probe-form-id");
  const nameInput = document.getElementById("probe-form-name");
  const typeSelect = document.getElementById("probe-form-type");
  const targetInput = document.getElementById("probe-form-target");
  const intervalSelect = document.getElementById("probe-form-interval");
  const timeoutInput = document.getElementById("probe-form-timeout");
  const statusInput = document.getElementById("probe-form-status");
  const keywordInput = document.getElementById("probe-form-keyword");
  const sslCheckbox = document.getElementById("probe-form-ssl");
  const enabledCheckbox = document.getElementById("probe-form-enabled");
  const feedback = document.getElementById("probe-modal-feedback");

  if (!modal) return;

  const probes = cachedProbesData?.probes || [];
  const probe = probes.find((p) => p.id === probeId);
  if (!probe) {
    showToast({
      title: "Probe Not Found",
      message: `Could not find probe #${probeId} in cache.`,
      type: "danger",
    });
    return;
  }

  if (title) title.textContent = `Edit Endpoint Probe: ${probe.name}`;
  if (idInput) idInput.value = String(probe.id);
  if (nameInput) nameInput.value = probe.name || "";
  if (typeSelect) typeSelect.value = probe.type || "http";
  if (targetInput) targetInput.value = probe.target || "";
  if (intervalSelect) intervalSelect.value = String(probe.interval_seconds || 60);
  if (timeoutInput) timeoutInput.value = String(probe.timeout_seconds || 5);
  if (statusInput) statusInput.value = String(probe.expected_status || 200);
  if (keywordInput) keywordInput.value = probe.keyword || "";
  if (sslCheckbox) sslCheckbox.checked = Boolean(probe.ssl_check);
  if (enabledCheckbox) enabledCheckbox.checked = Boolean(probe.enabled ?? true);

  if (feedback) {
    feedback.textContent = "";
    feedback.classList.add("hidden");
  }

  handleProbeTypeChange(probe.type || "http");
  modal.classList.remove("hidden");
  nameInput?.focus();
}

/**
 * Close the Add/Edit Probe modal.
 */
function closeProbeModal() {
  const modal = document.getElementById("probe-modal");
  if (modal) modal.classList.add("hidden");
}

/**
 * Handle form submission to create or update an endpoint probe.
 */
async function saveProbe(event) {
  return handleProbeFormSubmit(event);
}

async function handleProbeFormSubmit(event) {
  if (event) event.preventDefault();

  const idInput = document.getElementById("probe-form-id");
  const nameInput = document.getElementById("probe-form-name");
  const typeSelect = document.getElementById("probe-form-type");
  const targetInput = document.getElementById("probe-form-target");
  const intervalSelect = document.getElementById("probe-form-interval");
  const timeoutInput = document.getElementById("probe-form-timeout");
  const statusInput = document.getElementById("probe-form-status");
  const keywordInput = document.getElementById("probe-form-keyword");
  const sslCheckbox = document.getElementById("probe-form-ssl");
  const enabledCheckbox = document.getElementById("probe-form-enabled");
  const feedback = document.getElementById("probe-modal-feedback");
  const saveBtn = document.getElementById("btn-probe-modal-save");

  const probeId = idInput?.value ? parseInt(idInput.value, 10) : null;
  const name = (nameInput?.value || "").trim();
  const type = (typeSelect?.value || "http").trim().toLowerCase();
  let target = (targetInput?.value || "").trim();
  const interval_seconds = parseInt(intervalSelect?.value || "60", 10);
  const timeout_seconds = parseInt(timeoutInput?.value || "5", 10);
  const expected_status = parseInt(statusInput?.value || "200", 10);
  const keyword = (keywordInput?.value || "").trim();
  const ssl_check = Boolean(sslCheckbox?.checked);
  const enabled = Boolean(enabledCheckbox?.checked);

  if (!name) {
    if (feedback) {
      feedback.textContent = "Probe name is required.";
      feedback.classList.remove("hidden");
    }
    return;
  }

  if (!target) {
    if (feedback) {
      feedback.textContent = "Target endpoint is required.";
      feedback.classList.remove("hidden");
    }
    return;
  }

  if (type === "http" && !target.startsWith("http://") && !target.startsWith("https://")) {
    target = `https://${target}`;
    if (targetInput) targetInput.value = target;
  }

  const payload = {
    name,
    type,
    target,
    interval_seconds,
    timeout_seconds,
    expected_status,
    keyword,
    ssl_check,
    enabled,
  };

  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving...";
  }

  try {
    let resp;
    if (probeId) {
      resp = await apiFetch("/api/probes/" + probeId, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } else {
      resp = await apiFetch("/api/probes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    }

    const result = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(result.detail || `Server responded with status ${resp.status}`);
    }

    closeProbeModal();
    showToast({
      title: probeId ? "Probe Updated" : "Probe Created",
      message: `Endpoint probe '${name}' was successfully saved.`,
      type: "success",
    });
    await fetchProbes();
  } catch (err) {
    console.error("handleProbeFormSubmit error:", err);
    if (feedback) {
      feedback.textContent = err instanceof Error ? err.message : String(err);
      feedback.classList.remove("hidden");
    }
  } finally {
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.textContent = "Save Probe";
    }
  }
}

/**
 * Delete a configured probe after confirmation.
 */
async function deleteProbe(probeId) {
  const probes = cachedProbesData?.probes || [];
  const probe = probes.find((p) => p.id === probeId);
  const probeName = probe ? probe.name : `#${probeId}`;

  if (!confirm(`Are you sure you want to delete the probe '${probeName}'? All check history will be removed.`)) {
    return;
  }

  try {
    const resp = await apiFetch("/api/probes/" + probeId, {
      method: "DELETE",
    });
    const result = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(result.detail || `Failed to delete probe (HTTP ${resp.status})`);
    }

    showToast({
      title: "Probe Deleted",
      message: `Endpoint probe '${probeName}' has been removed.`,
      type: "success",
    });
    await fetchProbes();
  } catch (err) {
    console.error("deleteProbe error:", err);
    showToast({
      title: "Delete Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  }
}

/**
 * Execute an immediate test of a probe and display results in a toast.
 */
async function testProbe(probeId, btnEl) {
  if (btnEl) {
    btnEl.classList.add("loading");
    btnEl.disabled = true;
  }

  try {
    const resp = await apiFetch("/api/probes/" + probeId + "/test", {
      method: "POST",
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || `Test failed with HTTP ${resp.status}`);
    }

    const result = data.result || {};
    const status = (result.status || "unknown").toUpperCase();
    const latency = result.response_time_ms ? `${Math.round(result.response_time_ms)}ms` : "";
    const isUp = result.status === "up" || result.status === "degraded";

    showToast({
      title: `Probe Test: ${status}`,
      message: `Diagnostic test completed in ${latency || "--"}. Message: ${result.message || "OK"}`,
      type: isUp ? "success" : "danger",
    });

    await fetchProbes();
  } catch (err) {
    console.error("testProbe error:", err);
    showToast({
      title: "Probe Test Error",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  } finally {
    if (btnEl) {
      btnEl.classList.remove("loading");
      btnEl.disabled = false;
    }
  }
}

/* =========================================================================
 * Phase 3: Network Security, Open Ports & UFW Firewall Manager (#view-security)
 * ========================================================================= */

let cachedSecurityPortsData = null;
let cachedSecurityFirewallData = null;
let cachedSecuritySshData = null;
let isFetchingNetworkSecurity = false;

/**
 * Fetch open listening ports, UFW firewall status, and SSH intrusion/session telemetry from backend.
 */
async function fetchNetworkSecurity(isUserTriggered = false) {
  if (isFetchingNetworkSecurity) return;
  isFetchingNetworkSecurity = true;

  const refreshBtn = document.getElementById("refresh-security-btn");
  if (refreshBtn) refreshBtn.classList.add("loading");

  try {
    const [portsRes, firewallRes, sshRes] = await Promise.allSettled([
      apiFetch("/api/network/ports"),
      apiFetch("/api/network/firewall"),
      apiFetch("/api/network/ssh-security"),
    ]);

    let portsData = cachedSecurityPortsData;
    let firewallData = cachedSecurityFirewallData;
    let sshData = cachedSecuritySshData;

    if (portsRes.status === "fulfilled" && portsRes.value.ok) {
      portsData = await portsRes.value.json();
      cachedSecurityPortsData = portsData;
    } else if (portsRes.status === "fulfilled") {
      throw new Error(`Failed to fetch open ports (HTTP ${portsRes.value.status})`);
    } else {
      throw portsRes.reason;
    }

    if (firewallRes.status === "fulfilled" && firewallRes.value.ok) {
      firewallData = await firewallRes.value.json();
      cachedSecurityFirewallData = firewallData;
    } else if (firewallRes.status === "fulfilled") {
      throw new Error(`Failed to fetch firewall status (HTTP ${firewallRes.value.status})`);
    } else {
      throw firewallRes.reason;
    }

    if (sshRes.status === "fulfilled" && sshRes.value.ok) {
      sshData = await sshRes.value.json();
      cachedSecuritySshData = sshData;
    }

    renderNetworkSecurity(portsData, firewallData);
    if (sshData) {
      renderSshSecurity(sshData);
    }

    if (isUserTriggered) {
      showToast({
        title: "Network & Security Refreshed",
        message: "Listening ports, UFW firewall rules, and SSH security updated.",
        type: "success",
      });
    }
  } catch (err) {
    console.error("fetchNetworkSecurity error:", err);
    showToast({
      title: "Network Security Sync Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  } finally {
    isFetchingNetworkSecurity = false;
    if (refreshBtn) refreshBtn.classList.remove("loading");
  }
}

/**
 * Render Network Security status ribbon, open listening ports table, and UFW firewall table.
 */
function renderNetworkSecurity(portsData, firewallData) {
  const lastUpdatedEl = document.getElementById("security-last-updated");
  if (lastUpdatedEl) {
    lastUpdatedEl.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  }

  const ports = Array.isArray(portsData?.ports) ? portsData.ports : [];
  const summary = portsData?.summary || {
    total: ports.length,
    public: ports.filter((p) => p.is_public).length,
    local: ports.filter((p) => !p.is_public).length,
  };

  const totalPortsEl = document.getElementById("sec-total-ports");
  if (totalPortsEl) totalPortsEl.textContent = String(summary.total ?? ports.length);

  const publicPortsEl = document.getElementById("sec-public-ports");
  if (publicPortsEl) publicPortsEl.textContent = String(summary.public ?? 0);

  const localPortsEl = document.getElementById("sec-local-ports");
  if (localPortsEl) localPortsEl.textContent = String(summary.local ?? 0);

  const fwStatusBadge = document.getElementById("sec-firewall-status");
  if (fwStatusBadge) {
    if (!firewallData || !firewallData.available) {
      fwStatusBadge.textContent = "UNAVAILABLE";
      fwStatusBadge.className = "status-badge status-muted";
    } else if (firewallData.enabled) {
      fwStatusBadge.textContent = "ACTIVE";
      fwStatusBadge.className = "status-badge status-ok";
    } else {
      fwStatusBadge.textContent = "INACTIVE";
      fwStatusBadge.className = "status-badge status-warn";
    }
  }

  const fwPoliciesEl = document.getElementById("sec-firewall-policies");
  if (fwPoliciesEl) {
    if (!firewallData || !firewallData.available) {
      fwPoliciesEl.textContent = "UFW is not installed or unavailable on this host";
    } else {
      const inc = (firewallData.default_incoming || "unknown").toUpperCase();
      const out = (firewallData.default_outgoing || "unknown").toUpperCase();
      fwPoliciesEl.textContent = `Default Policies: Incoming ${inc} | Outgoing ${out}`;
    }
  }

  // Render Open Ports Table
  const portsTbody = document.getElementById("sec-ports-tbody");
  if (portsTbody) {
    if (ports.length === 0) {
      portsTbody.innerHTML = `<tr><td colspan="8" class="empty-state">No open listening sockets detected.</td></tr>`;
    } else {
      portsTbody.innerHTML = ports
        .map((p) => {
          const proto = String(p.proto || "tcp").toLowerCase();
          const portNum = p.port ?? "";
          const bindIp = p.ip || "0.0.0.0";
          const isPublic = Boolean(p.is_public);
          const exposureBadge = isPublic
            ? `<span class="exposure-badge exp-public">PUBLIC</span>`
            : `<span class="exposure-badge exp-local">LOCAL</span>`;
          const procName = p.process_name || "unknown";
          const pidText = p.pid ? ` (${p.pid})` : "";
          const user = p.user || "-";
          const containerHtml = p.container_name
            ? `<span class="chip-tag chip-primary">${escapeHtml(p.container_name)}</span>`
            : `<span class="text-muted">-</span>`;

          const searchBlob = `${proto} ${portNum} ${bindIp} ${isPublic ? "public" : "local"} ${procName} ${p.pid || ""} ${user} ${p.container_name || ""}`.toLowerCase();

          return `
            <tr data-search="${escapeHtml(searchBlob)}">
              <td><span class="kind-badge">${escapeHtml(proto.toUpperCase())}</span></td>
              <td class="mono-text font-bold">${escapeHtml(portNum)}</td>
              <td class="mono-text">${escapeHtml(bindIp)}</td>
              <td>${exposureBadge}</td>
              <td><span class="font-bold">${escapeHtml(procName)}</span><span class="mono-text text-secondary">${escapeHtml(pidText)}</span></td>
              <td class="mono-text text-secondary">${escapeHtml(user)}</td>
              <td>${containerHtml}</td>
              <td style="text-align: right;">
                <div class="table-actions-cell">
                  <button type="button" class="btn-icon-subtle btn-port-add-rule" title="Add UFW Rule" data-port="${escapeHtml(portNum)}" data-proto="${escapeHtml(proto)}">+ Rule</button>
                </div>
              </td>
            </tr>
          `;
        })
        .join("");

      portsTbody.querySelectorAll(".btn-port-add-rule").forEach((btn) => {
        btn.addEventListener("click", () => {
          const portVal = btn.getAttribute("data-port") || "";
          const protoVal = btn.getAttribute("data-proto") || "any";
          openAddFirewallRuleModal(portVal, protoVal);
        });
      });

      filterPortsTable();
    }
  }

  // Render UFW Firewall Rules Table
  const fwTbody = document.getElementById("sec-firewall-tbody");
  if (fwTbody) {
    if (!firewallData || !firewallData.available) {
      fwTbody.innerHTML = `<tr><td colspan="6" class="empty-state">UFW firewall is not available on this host.</td></tr>`;
    } else {
      const rules = Array.isArray(firewallData.rules) ? firewallData.rules : [];
      if (rules.length === 0) {
        const msg = firewallData.enabled
          ? "No UFW firewall rules configured."
          : "UFW firewall is inactive or has no rules configured.";
        fwTbody.innerHTML = `<tr><td colspan="6" class="empty-state">${escapeHtml(msg)}</td></tr>`;
      } else {
        fwTbody.innerHTML = rules
          .map((r) => {
            const actUpper = String(r.action || "ALLOW").toUpperCase();
            let actClass = "status-badge status-ok";
            if (actUpper.includes("DENY") || actUpper.includes("REJECT")) {
              actClass = "status-badge status-danger";
            } else if (actUpper.includes("LIMIT")) {
              actClass = "status-badge status-warn";
            }

            const protoSuffix =
              r.proto && r.proto !== "any" ? `/${r.proto}` : "";
            const toDisplay = `${r.to_port || "-"}${protoSuffix}`;
            const fromDisplay = r.from_ip || "Anywhere";
            const typeLabel = r.v6 ? "IPv6" : "IPv4";

            return `
              <tr>
                <td class="mono-text font-bold">${escapeHtml(r.index)}</td>
                <td><span class="${actClass}">${escapeHtml(actUpper)}</span></td>
                <td class="mono-text font-bold">${escapeHtml(toDisplay)}</td>
                <td class="mono-text">${escapeHtml(fromDisplay)}</td>
                <td><span class="kind-badge">${typeLabel}</span></td>
                <td style="text-align: right;">
                  <div class="table-actions-cell">
                    <button type="button" class="btn-icon-danger btn-firewall-delete-rule" data-index="${escapeHtml(r.index)}">Delete</button>
                  </div>
                </td>
              </tr>
            `;
          })
          .join("");

        fwTbody.querySelectorAll(".btn-firewall-delete-rule").forEach((btn) => {
          btn.addEventListener("click", () => {
            const idx = parseInt(btn.getAttribute("data-index") || "0", 10);
            if (idx > 0) {
              deleteFirewallRule(idx);
            }
          });
        });
      }
    }
  }
}

/**
 * Filter rows in #sec-ports-table by search text in #sec-ports-filter.
 */
function filterPortsTable() {
  const filterInput = document.getElementById("sec-ports-filter");
  const tbody = document.getElementById("sec-ports-tbody");
  if (!tbody) return;

  const query = (filterInput?.value || "").trim().toLowerCase();
  const rows = Array.from(tbody.querySelectorAll("tr[data-search]"));
  if (rows.length === 0) return;

  let visibleCount = 0;
  rows.forEach((row) => {
    const text = (row.getAttribute("data-search") || row.textContent || "").toLowerCase();
    const matches = !query || text.includes(query);
    row.classList.toggle("hidden", !matches);
    row.style.display = matches ? "" : "none";
    if (matches) visibleCount += 1;
  });

  const existingEmpty = tbody.querySelector("tr.sec-filter-empty-row");
  if (visibleCount === 0 && query) {
    if (!existingEmpty) {
      const emptyTr = document.createElement("tr");
      emptyTr.className = "sec-filter-empty-row";
      emptyTr.innerHTML = `<td colspan="8" class="empty-state">No listening ports match "${escapeHtml(query)}".</td>`;
      tbody.appendChild(emptyTr);
    }
  } else if (existingEmpty) {
    existingEmpty.remove();
  }
}

/**
 * Open the Add Firewall Rule Modal, pre-filling port and protocol if provided.
 */
function openAddFirewallRuleModal(port = "", proto = "any") {
  const modal = document.getElementById("firewall-modal");
  const actionSelect = document.getElementById("firewall-form-action");
  const portInput = document.getElementById("firewall-form-port");
  const protoSelect = document.getElementById("firewall-form-proto");
  const fromInput = document.getElementById("firewall-form-from");
  const feedback = document.getElementById("firewall-modal-feedback");

  if (!modal) return;

  if (actionSelect) actionSelect.value = "allow";
  if (portInput) portInput.value = port ? String(port) : "";
  if (protoSelect) {
    const cleanProto = String(proto || "any").toLowerCase();
    protoSelect.value = ["tcp", "udp", "any"].includes(cleanProto) ? cleanProto : "any";
  }
  if (fromInput) fromInput.value = "";
  if (feedback) {
    feedback.textContent = "";
    feedback.classList.add("hidden");
  }

  modal.classList.remove("hidden");
  portInput?.focus();
}

/**
 * Close the Add Firewall Rule Modal.
 */
function closeFirewallModal() {
  const modal = document.getElementById("firewall-modal");
  if (modal) modal.classList.add("hidden");
}

/**
 * Submit Add Firewall Rule form via POST /api/network/firewall/rules.
 */
async function handleFirewallFormSubmit(e) {
  if (e) e.preventDefault();

  const actionSelect = document.getElementById("firewall-form-action");
  const portInput = document.getElementById("firewall-form-port");
  const protoSelect = document.getElementById("firewall-form-proto");
  const fromInput = document.getElementById("firewall-form-from");
  const feedback = document.getElementById("firewall-modal-feedback");
  const saveBtn = document.getElementById("btn-firewall-modal-save");

  const action = (actionSelect?.value || "allow").trim().toLowerCase();
  const port = (portInput?.value || "").trim();
  const proto = (protoSelect?.value || "any").trim().toLowerCase();
  const rawFrom = (fromInput?.value || "").trim();
  const from_ip =
    !rawFrom || rawFrom.toLowerCase() === "anywhere" || rawFrom.toLowerCase() === "any"
      ? null
      : rawFrom;

  if (!port) {
    if (feedback) {
      feedback.textContent = "Port or service is required.";
      feedback.classList.remove("hidden");
    }
    return;
  }

  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.textContent = "Adding...";
  }

  try {
    const resp = await apiFetch("/api/network/firewall/rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, port, proto, from_ip }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.message || `Failed to add firewall rule (HTTP ${resp.status})`);
    }

    closeFirewallModal();
    showToast({
      title: "Firewall Rule Added",
      message: data.message || `UFW rule (${action.toUpperCase()} ${port}/${proto}) added.`,
      type: "success",
    });
    await fetchNetworkSecurity();
  } catch (err) {
    console.error("handleFirewallFormSubmit error:", err);
    if (feedback) {
      feedback.textContent = err instanceof Error ? err.message : String(err);
      feedback.classList.remove("hidden");
    }
  } finally {
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.textContent = "Add Rule";
    }
  }
}

/**
 * Delete a UFW firewall rule by index via DELETE /api/network/firewall/rules/{ruleIndex}.
 */
async function deleteFirewallRule(ruleIndex) {
  if (!ruleIndex) return;
  if (!confirm(`Are you sure you want to delete UFW firewall rule #${ruleIndex}?`)) {
    return;
  }

  try {
    const resp = await apiFetch("/api/network/firewall/rules/" + encodeURIComponent(ruleIndex), {
      method: "DELETE",
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.message || `Failed to delete rule #${ruleIndex} (HTTP ${resp.status})`);
    }

    showToast({
      title: "Firewall Rule Deleted",
      message: data.message || `UFW rule #${ruleIndex} removed.`,
      type: "success",
    });
    await fetchNetworkSecurity();
  } catch (err) {
    console.error("deleteFirewallRule error:", err);
    showToast({
      title: "Delete Rule Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  }
}

/**
 * Render SSH Intrusion Detection, Active Sessions, and IP Ban Manager panel (#view-security).
 */
function renderSshSecurity(sshData) {
  if (!sshData) return;

  const activeSessions = Array.isArray(sshData.active_sessions) ? sshData.active_sessions : [];
  const recentLogins = Array.isArray(sshData.recent_logins) ? sshData.recent_logins : [];
  const recentFailures = Array.isArray(sshData.recent_failures) ? sshData.recent_failures : [];
  const topOffenders = Array.isArray(sshData.top_offenders) ? sshData.top_offenders : [];
  const bannedIps = Array.isArray(sshData.banned_ips) ? sshData.banned_ips : [];
  const fail2ban = sshData.fail2ban || {};

  // 1. Summary Ribbon counters
  const activeCountEl = document.getElementById("sec-active-sessions-count");
  if (activeCountEl) {
    activeCountEl.textContent = String(activeSessions.length);
  }

  const failedTotal = Number(sshData.failed_attempts_total ?? fail2ban.total_failed ?? 0);
  const failedCountEl = document.getElementById("sec-failed-ssh-count");
  if (failedCountEl) {
    failedCountEl.textContent = String(failedTotal);
    failedCountEl.className = failedTotal > 0 ? "count-number count-warn" : "count-number count-ok";
  }

  const bannedCount = bannedIps.length || Number(fail2ban.currently_banned || 0);
  const bannedCountEl = document.getElementById("sec-banned-ips-count");
  if (bannedCountEl) {
    bannedCountEl.textContent = `${bannedCount} Banned`;
    bannedCountEl.className =
      bannedCount > 0 ? "status-badge status-danger" : "status-badge status-muted";
  }

  // 2. Fail2ban status badge
  const f2bBadge = document.getElementById("sec-fail2ban-badge");
  if (f2bBadge) {
    if (fail2ban.installed && fail2ban.active) {
      const f2bBanned = Number(fail2ban.currently_banned || 0);
      f2bBadge.textContent = `FAIL2BAN: ACTIVE (${f2bBanned} Banned)`;
      f2bBadge.className = "status-badge status-ok";
    } else if (fail2ban.installed && !fail2ban.active) {
      f2bBadge.textContent = "FAIL2BAN: INACTIVE / UFW ENFORCED";
      f2bBadge.className = "status-badge status-warn";
    } else {
      f2bBadge.textContent = "FAIL2BAN: NOT INSTALLED / UFW ENFORCED";
      f2bBadge.className = "status-badge status-info";
    }
  }

  // 3. Offending IPs & Banned Hosts Table (#sec-ssh-offenders-tbody)
  const offendersTbody = document.getElementById("sec-ssh-offenders-tbody");
  if (offendersTbody) {
    // Merge top_offenders and any manually banned IPs not in top_offenders
    const offenderMap = new Map();
    topOffenders.forEach((o) => {
      if (o && o.ip) {
        offenderMap.set(o.ip, {
          ip: o.ip,
          count: Number(o.count || 0),
          targeted_users: Array.isArray(o.targeted_users) ? o.targeted_users : [],
          last_seen: o.last_seen || "-",
          is_banned: Boolean(o.is_banned),
          ban_sources: Array.isArray(o.ban_sources) ? o.ban_sources : [],
        });
      }
    });

    bannedIps.forEach((b) => {
      const ipStr = typeof b === "string" ? b : b?.ip;
      if (!ipStr) return;
      const sources = Array.isArray(b?.sources) ? b.sources : ["ufw"];
      if (offenderMap.has(ipStr)) {
        const existing = offenderMap.get(ipStr);
        existing.is_banned = true;
        existing.ban_sources = Array.from(new Set([...existing.ban_sources, ...sources]));
      } else {
        offenderMap.set(ipStr, {
          ip: ipStr,
          count: 0,
          targeted_users: [],
          last_seen: "Manual / Firewall Rule",
          is_banned: true,
          ban_sources: sources,
        });
      }
    });

    const combinedOffenders = Array.from(offenderMap.values());

    if (combinedOffenders.length === 0) {
      offendersTbody.innerHTML = `<tr><td colspan="6" class="empty-state">No failed SSH login offenders or banned IPs detected.</td></tr>`;
    } else {
      offendersTbody.innerHTML = combinedOffenders
        .map((item) => {
          const ipSafe = escapeHtml(item.ip);
          const countDisplay = item.count > 0 ? String(item.count) : "-";
          const usersList =
            item.targeted_users && item.targeted_users.length > 0
              ? item.targeted_users.map((u) => `<span class="chip-tag">${escapeHtml(u)}</span>`).join(" ")
              : `<span class="text-muted">-</span>`;
          const lastSeenSafe = escapeHtml(item.last_seen || "-");

          let statusBadge = `<span class="status-badge status-warn">UNBLOCKED</span>`;
          if (item.is_banned) {
            const srcLabel =
              item.ban_sources && item.ban_sources.length > 0
                ? item.ban_sources.map((s) => String(s).toUpperCase()).join(" + ")
                : "UFW";
            statusBadge = `<span class="status-badge status-danger">BANNED (${escapeHtml(srcLabel)})</span>`;
          }

          const actionBtn = item.is_banned
            ? `<button type="button" class="btn-unban-ip" data-unban-ip="${ipSafe}" title="Unban ${ipSafe}">Unban IP</button>`
            : `<button type="button" class="btn-ban-ip" data-ban-ip="${ipSafe}" title="Ban ${ipSafe}">Ban IP</button>`;

          return `
            <tr>
              <td class="mono-text font-bold">${ipSafe}</td>
              <td class="mono-text font-bold">${escapeHtml(countDisplay)}</td>
              <td>${usersList}</td>
              <td class="mono-text text-secondary">${lastSeenSafe}</td>
              <td>${statusBadge}</td>
              <td style="text-align: right;">
                <div class="table-actions-cell">
                  ${actionBtn}
                </div>
              </td>
            </tr>
          `;
        })
        .join("");
    }
  }

  // 4. Active Login Sessions Table (#sec-sessions-tbody & #sec-sessions-badge)
  const sessionsBadge = document.getElementById("sec-sessions-badge");
  if (sessionsBadge) {
    sessionsBadge.textContent = `${activeSessions.length} Active`;
    sessionsBadge.className =
      activeSessions.length > 0 ? "status-badge status-ok" : "status-badge status-muted";
  }

  const sessionsTbody = document.getElementById("sec-sessions-tbody");
  if (sessionsTbody) {
    if (activeSessions.length === 0) {
      sessionsTbody.innerHTML = `<tr><td colspan="5" class="empty-state">No active interactive SSH or TTY login sessions.</td></tr>`;
    } else {
      sessionsTbody.innerHTML = activeSessions
        .map((s) => {
          const user = escapeHtml(s.username || "unknown");
          const tty = escapeHtml(s.terminal || "-");
          const host = escapeHtml(s.host || "local");
          const pid = s.pid ? escapeHtml(String(s.pid)) : "-";
          const started = escapeHtml(s.started_iso || "-");
          return `
            <tr>
              <td class="font-bold">${user}</td>
              <td><span class="kind-badge">${tty}</span></td>
              <td class="mono-text">${host}</td>
              <td class="mono-text text-secondary">${pid}</td>
              <td class="mono-text text-secondary">${started}</td>
            </tr>
          `;
        })
        .join("");
    }
  }

  // 5. Recent Failed Auth Events & Login History (#sec-recent-auth-tbody)
  const recentAuthTbody = document.getElementById("sec-recent-auth-tbody");
  if (recentAuthTbody) {
    const rows = [];

    recentFailures.slice(0, 15).forEach((f) => {
      rows.push({
        time: f.timestamp || "-",
        badge: `<span class="status-badge status-danger">AUTH FAIL</span>`,
        user: f.username || "unknown",
        host: f.ip || "-",
        details: f.reason || "Failed SSH authentication",
      });
    });

    recentLogins.slice(0, 15).forEach((l) => {
      rows.push({
        time: l.time_range || "-",
        badge: `<span class="status-badge status-ok">LOGIN</span>`,
        user: l.username || "unknown",
        host: l.host || "local",
        details: l.terminal ? `TTY ${l.terminal}` : l.raw || "Accepted session",
      });
    });

    if (rows.length === 0) {
      recentAuthTbody.innerHTML = `<tr><td colspan="5" class="empty-state">No recent authentication failures or login history recorded.</td></tr>`;
    } else {
      recentAuthTbody.innerHTML = rows
        .map(
          (r) => `
            <tr>
              <td class="mono-text text-secondary">${escapeHtml(r.time)}</td>
              <td>${r.badge}</td>
              <td class="font-bold">${escapeHtml(r.user)}</td>
              <td class="mono-text">${escapeHtml(r.host)}</td>
              <td class="text-secondary">${escapeHtml(r.details)}</td>
            </tr>
          `
        )
        .join("");
    }
  }
}

/**
 * Ban or Unban an IP address via POST /api/network/ssh-security/ban.
 */
async function submitSshIpBan(action, ip, buttonEl = null) {
  const cleanAction = String(action || "").trim().toLowerCase();
  const cleanIp = String(ip || "").trim();
  if (!cleanIp || !["ban", "unban"].includes(cleanAction)) {
    showToast({
      title: "Invalid IP Ban Request",
      message: "Please provide a valid IPv4/IPv6 address or CIDR to ban or unban.",
      type: "warning",
    });
    return;
  }

  const origText = buttonEl ? buttonEl.textContent : "";
  if (buttonEl) {
    buttonEl.disabled = true;
    buttonEl.textContent = cleanAction === "ban" ? "Banning..." : "Unbanning...";
  }

  try {
    const resp = await apiFetch("/api/network/ssh-security/ban", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: cleanAction, ip: cleanIp }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(
        data.detail || data.message || `Failed to ${cleanAction} IP ${cleanIp} (HTTP ${resp.status})`
      );
    }

    showToast({
      title: cleanAction === "ban" ? "IP Address Banned" : "IP Address Unbanned",
      message: data.message || `Successfully executed ${cleanAction.toUpperCase()} for ${cleanIp}.`,
      type: cleanAction === "ban" ? "warning" : "success",
    });

    if (data.ssh_security) {
      cachedSecuritySshData = data.ssh_security;
      renderSshSecurity(data.ssh_security);
    }
    if (data.firewall) {
      cachedSecurityFirewallData = data.firewall;
      renderNetworkSecurity(cachedSecurityPortsData, data.firewall);
    }
    if (!data.ssh_security || !data.firewall) {
      await fetchNetworkSecurity();
    }
  } catch (err) {
    console.error("submitSshIpBan error:", err);
    showToast({
      title: `${cleanAction === "ban" ? "Ban" : "Unban"} Failed`,
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  } finally {
    if (buttonEl) {
      buttonEl.disabled = false;
      buttonEl.textContent = origText;
    }
  }
}

/* =========================================================================
 * Phase 4: Docker Stack & Host Directory Backups + Unified Vigil Cron Scheduler
 * ========================================================================= */

let cachedBackupsData = null;
let cachedCronData = null;

let isBackupsAndCronRefreshing = false;

/**
 * Refresh both Backups and Cron Scheduler data.
 */
async function refreshBackupsAndCron(manual = false) {
  const refreshBtn = document.getElementById("backups-refresh-btn");
  if (refreshBtn && manual) {
    refreshBtn.classList.add("spinning");
  }
  if (isBackupsAndCronRefreshing) return;
  isBackupsAndCronRefreshing = true;
  try {
    await Promise.all([fetchBackups(), fetchCronJobs()]);
    const updatedEl = document.getElementById("backups-last-updated");
    if (updatedEl) {
      updatedEl.textContent = `Updated ${formatTimeOnly(new Date())}`;
    }
  } finally {
    isBackupsAndCronRefreshing = false;
    if (refreshBtn && manual) {
      setTimeout(() => refreshBtn.classList.remove("spinning"), 400);
    }
  }
}

/**
 * Update the sidebar badge #nav-badge-backups with active cron jobs or archive count.
 */
function updateBackupsNavBadge() {
  const badge = document.getElementById("nav-badge-backups");
  if (!badge) return;
  const jobs = Array.isArray(cachedCronData?.jobs) ? cachedCronData.jobs : [];
  const activeJobsCount = jobs.filter((j) => j.enabled).length;
  const totalArchives = Number(cachedBackupsData?.total_archives || 0);
  const count = activeJobsCount > 0 ? activeJobsCount : totalArchives;
  if (count > 0) {
    badge.textContent = String(count);
    badge.classList.remove("hidden");
  } else {
    badge.textContent = "0";
    badge.classList.add("hidden");
  }
}

/**
 * Fetch backup targets, stack folders, and archives via GET /api/backups.
 */
async function fetchBackups() {
  try {
    const resp = await apiFetch("/api/backups");
    if (!resp.ok) {
      throw new Error(`Failed to fetch backups (HTTP ${resp.status})`);
    }
    const data = await resp.json();
    cachedBackupsData = data;
    renderBackups(data);
    updateBackupsNavBadge();
  } catch (err) {
    console.error("fetchBackups error:", err);
  }
}

/**
 * Render Backups Summary Ribbon, Stack Selector, Targets Table, and Archives Table.
 */
function renderBackups(data) {
  if (!data) return;

  const savedTargets = Array.isArray(data.saved_targets) ? data.saved_targets : [];
  const archives = Array.isArray(data.archives) ? data.archives : [];

  // 1. Top Summary Ribbon
  const totalArchivesEl = document.getElementById("backups-total-archives");
  if (totalArchivesEl) {
    totalArchivesEl.textContent = String(data.total_archives ?? archives.length);
  }

  const totalSizeEl = document.getElementById("backups-total-size");
  if (totalSizeEl) {
    totalSizeEl.textContent = formatBytes(data.total_size_bytes ?? 0);
  }

  const targetsCountEl = document.getElementById("backups-stacks-count");
  if (targetsCountEl) {
    targetsCountEl.textContent = String(savedTargets.length);
  }

  const dirBadge = document.getElementById("backup-dir-badge");
  if (dirBadge && data.backup_dir) {
    dirBadge.textContent = data.backup_dir;
  }

  const dirInput = document.getElementById("backup-dir-input");
  if (dirInput && data.backup_dir && document.activeElement !== dirInput) {
    dirInput.value = data.backup_dir;
  }

  // 2. Configured Directory Targets Table (#backup-targets-tbody)
  const targetsTbody = document.getElementById("backup-targets-tbody");
  if (targetsTbody) {
    if (savedTargets.length === 0) {
      targetsTbody.innerHTML = `<tr><td colspan="4" class="empty-state">No saved directory backup targets found. Add a host directory above.</td></tr>`;
    } else {
      targetsTbody.innerHTML = savedTargets
        .map((t) => {
          const targetId = t.id;
          const name = t.name || t.target_name || "backup";
          const sourcePath = t.source_path || "";
          const retainCount = Number(t.retain_count || 7);
          const excludesList = Array.isArray(t.excludes) ? t.excludes.join(", ") : (t.excludes || "");
          const detailsParts = [`Retain: ${retainCount}`];
          if (excludesList) detailsParts.push(`Excludes: ${excludesList}`);

          return `
            <tr>
              <td class="font-bold">
                <span class="target-kind-badge kind-directory" style="margin-right: 6px;">DIR</span>
                ${escapeHtml(name)}
              </td>
              <td class="mono-text text-secondary">${escapeHtml(sourcePath)}</td>
              <td class="text-secondary" style="font-size: 0.8rem;">${escapeHtml(detailsParts.join(" • "))}</td>
              <td style="text-align: right;">
                <div class="table-actions-cell">
                  <button
                    type="button"
                    class="btn-backup-action"
                    data-backup-now="directory"
                    data-source-path="${escapeHtml(sourcePath)}"
                    data-target-name="${escapeHtml(name)}"
                    data-retain-count="${retainCount}"
                    data-excludes="${escapeHtml(excludesList)}"
                    title="Run backup now for ${escapeHtml(name)}"
                  >
                    1-Click Backup Now
                  </button>
                  <button
                    type="button"
                    class="btn-backup-action btn-schedule"
                    data-schedule-backup="directory"
                    data-source-path="${escapeHtml(sourcePath)}"
                    data-target-name="${escapeHtml(name)}"
                    data-retain-count="${retainCount}"
                    title="Schedule ${escapeHtml(name)} in Cron"
                  >
                    Schedule in Cron
                  </button>
                  <button
                    type="button"
                    class="btn-icon-danger"
                    data-delete-target-id="${escapeHtml(String(targetId))}"
                    title="Delete target ${escapeHtml(name)}"
                  >
                    Delete Target
                  </button>
                </div>
              </td>
            </tr>
          `;
        })
        .join("");
    }
  }

  // 4. Backup Archives Browser Table (#backup-archives-tbody)
  const archivesTbody = document.getElementById("backup-archives-tbody");
  if (archivesTbody) {
    if (archives.length === 0) {
      archivesTbody.innerHTML = `<tr><td colspan="7" class="empty-state">No backup archives created yet. Run a backup above or schedule a job in Cron.</td></tr>`;
    } else {
      archivesTbody.innerHTML = archives
        .map((a) => {
          const filename = a.filename || "";
          const filenameSafe = escapeHtml(filename);
          const tType = a.target_type === "stack" ? "STACK" : "DIRECTORY";
          const badgeClass = a.target_type === "stack" ? "kind-stack" : "kind-directory";
          const targetName = escapeHtml(a.target_name || "-");
          const sourcePath = escapeHtml(a.source_path || "-");
          const sizeFormatted = formatBytes(a.size_bytes ?? 0);
          const fileCount = Number(a.file_count || 0);
          const createdDisplay = escapeHtml(a.created_iso || formatDateTime(a.created_at));
          const shaShort = a.sha256 ? escapeHtml(String(a.sha256).slice(0, 12)) + "…" : "-";
          const shaFull = escapeHtml(a.sha256 || "");
          const downloadUrl = `/api/backups/archives/${encodeURIComponent(filename)}/download`;

          return `
            <tr>
              <td class="mono-text font-bold">${filenameSafe}</td>
              <td>
                <span class="target-kind-badge ${badgeClass}">${tType}</span>
                <span class="font-bold" style="margin-left: 6px;">${targetName}</span>
              </td>
              <td class="mono-text text-secondary">${sourcePath}</td>
              <td class="mono-text">${escapeHtml(sizeFormatted)} <span class="text-secondary">(${fileCount} files)</span></td>
              <td class="mono-text text-secondary">${createdDisplay}</td>
              <td class="mono-text text-secondary" title="SHA-256: ${shaFull}">${shaShort}</td>
              <td style="text-align: right;">
                <div class="table-actions-cell">
                  <a
                    href="${downloadUrl}"
                    class="btn-backup-action"
                    download="${filenameSafe}"
                    title="Download ${filenameSafe}"
                  >
                    Download
                  </a>
                  <button
                    type="button"
                    class="btn-backup-action btn-restore"
                    data-restore-archive="${filenameSafe}"
                    data-source-path="${sourcePath}"
                    title="Restore archive ${filenameSafe}"
                  >
                    Restore
                  </button>
                  <button
                    type="button"
                    class="btn-icon-danger"
                    data-delete-archive="${filenameSafe}"
                    title="Delete archive ${filenameSafe}"
                  >
                    Delete
                  </button>
                </div>
              </td>
            </tr>
          `;
        })
        .join("");
    }
  }
}

/**
 * Retained for backwards-compatibility; host directory backups are pure directory targets.
 */
function syncBackupFormWithSelectedStack() {
  // Pure directory backups mode active
}

/**
 * Read current values from #backup-create-form for directory backups.
 */
function getBackupFormPayload() {
  const source_path = (document.getElementById("backup-source-path")?.value || "").trim();
  const rawTargetName = (document.getElementById("backup-target-name")?.value || "").trim();
  const defaultTargetName = source_path ? source_path.split("/").filter(Boolean).pop() || "backup" : "backup";
  const target_name = rawTargetName || defaultTargetName;
  const rawExcludes = (document.getElementById("backup-excludes")?.value || "").trim();
  const excludes = rawExcludes
    ? rawExcludes
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean)
    : [];
  const retain_count = Math.max(
    1,
    parseInt(document.getElementById("backup-retain-count")?.value || "7", 10) || 7
  );

  return {
    target_type: "directory",
    source_path,
    target_name: target_name || "backup",
    name: target_name || "backup",
    excludes,
    stop_containers: false,
    retain_count,
  };
}

/**
 * Update the backup archives storage destination directory via POST /api/backups/config.
 */
async function saveBackupDirectoryConfig(buttonEl = null) {
  const dirInput = document.getElementById("backup-dir-input");
  const backup_dir = (dirInput?.value || "").trim();
  if (!backup_dir) {
    showToast({
      title: "Storage Path Required",
      message: "Please enter a valid directory path for backup archives.",
      type: "warning",
    });
    return;
  }

  const origText = buttonEl ? buttonEl.textContent : "";
  if (buttonEl) {
    buttonEl.disabled = true;
    buttonEl.textContent = "Saving...";
  }

  try {
    const resp = await apiFetch("/api/backups/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ backup_dir }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.message || `Failed to update backup directory (HTTP ${resp.status})`);
    }

    showToast({
      title: "Archive Path Updated",
      message: data.message || `Backup archive path saved to "${backup_dir}".`,
      type: "success",
    });

    const dirBadge = document.getElementById("backup-dir-badge");
    if (dirBadge) {
      dirBadge.textContent = data.backup_dir || backup_dir;
    }

    await fetchBackups();
  } catch (err) {
    console.error("saveBackupDirectoryConfig error:", err);
    showToast({
      title: "Save Archive Path Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  } finally {
    if (buttonEl) {
      buttonEl.disabled = false;
      buttonEl.textContent = origText;
    }
  }
}

/**
 * Execute a backup immediately via POST /api/backups/run.
 */
async function executeBackupRun(payload, buttonEl = null) {
  const origText = buttonEl ? buttonEl.textContent : "";
  if (buttonEl) {
    buttonEl.disabled = true;
    buttonEl.textContent = "Backing up...";
  }

  try {
    const resp = await apiFetch("/api/backups/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.message || `Backup failed (HTTP ${resp.status})`);
    }

    const archiveName = data.archive?.filename || payload.target_name || "archive";
    showToast({
      title: "Backup Completed",
      message: data.message || `Created backup archive ${archiveName}.`,
      type: "success",
    });
    await fetchBackups();
  } catch (err) {
    console.error("executeBackupRun error:", err);
    showToast({
      title: "Backup Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  } finally {
    if (buttonEl) {
      buttonEl.disabled = false;
      buttonEl.textContent = origText;
    }
  }
}

/**
 * Save a reusable backup target via POST /api/backups/targets.
 */
async function saveBackupTarget(buttonEl = null) {
  const payload = getBackupFormPayload();
  if (!payload.source_path) {
    showToast({
      title: "Source Path Required",
      message: "Please enter a host directory path to save as a backup target.",
      type: "warning",
    });
    return;
  }

  const origText = buttonEl ? buttonEl.textContent : "";
  if (buttonEl) {
    buttonEl.disabled = true;
    buttonEl.textContent = "Saving...";
  }

  try {
    const resp = await apiFetch("/api/backups/targets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.message || `Failed to save backup target (HTTP ${resp.status})`);
    }

    showToast({
      title: "Backup Target Saved",
      message: `Saved backup target "${payload.name}".`,
      type: "success",
    });
    await fetchBackups();
  } catch (err) {
    console.error("saveBackupTarget error:", err);
    showToast({
      title: "Save Target Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  } finally {
    if (buttonEl) {
      buttonEl.disabled = false;
      buttonEl.textContent = origText;
    }
  }
}

/**
 * Delete a saved backup target via DELETE /api/backups/targets/{target_id}.
 */
async function deleteBackupTarget(targetId) {
  if (!targetId) return;
  if (!confirm("Remove this saved backup target? (Existing archive files will not be deleted)")) {
    return;
  }
  try {
    const resp = await apiFetch("/api/backups/targets/" + encodeURIComponent(targetId), {
      method: "DELETE",
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.message || `Failed to delete target (HTTP ${resp.status})`);
    }
    showToast({
      title: "Target Removed",
      message: "Saved backup target removed.",
      type: "success",
    });
    await fetchBackups();
  } catch (err) {
    console.error("deleteBackupTarget error:", err);
    showToast({
      title: "Delete Target Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  }
}

/**
 * Restore a backup archive via POST /api/backups/archives/{filename}/restore.
 */
async function restoreBackupArchive(filename, defaultDest = "") {
  if (!filename) return;
  const destInput = prompt(
    `Restore backup archive "${filename}".\n\nEnter destination directory path (leave blank to restore to original source path ${defaultDest || ""}):`,
    defaultDest || ""
  );
  if (destInput === null) return;

  try {
    const resp = await apiFetch("/api/backups/archives/" + encodeURIComponent(filename) + "/restore", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ destination_path: destInput.trim() }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.message || `Failed to restore archive (HTTP ${resp.status})`);
    }
    showToast({
      title: "Archive Restored",
      message: data.message || `Successfully restored ${filename}.`,
      type: "success",
    });
  } catch (err) {
    console.error("restoreBackupArchive error:", err);
    showToast({
      title: "Restore Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  }
}

/**
 * Delete a backup archive via DELETE /api/backups/archives/{filename}.
 */
async function deleteBackupArchive(filename) {
  if (!filename) return;
  if (!confirm(`Permanently delete backup archive "${filename}" from disk?`)) {
    return;
  }
  try {
    const resp = await apiFetch("/api/backups/archives/" + encodeURIComponent(filename), {
      method: "DELETE",
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.message || `Failed to delete archive (HTTP ${resp.status})`);
    }
    showToast({
      title: "Archive Deleted",
      message: `Deleted backup archive ${filename}.`,
      type: "success",
    });
    await fetchBackups();
  } catch (err) {
    console.error("deleteBackupArchive error:", err);
    showToast({
      title: "Delete Archive Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  }
}

/**
 * Pre-fill the Cron Job form (#cron-job-form) for a given directory target and scroll to it.
 */
function prefillCronFormForTarget({ sourcePath = "", targetName = "", retainCount = 7 } = {}) {
  cancelEditCronJob();
  const jobNameInput = document.getElementById("cron-job-name");
  const taskTypeSelect = document.getElementById("cron-task-type");
  const pathInput = document.getElementById("cron-config-source-path");
  const retainInput = document.getElementById("cron-config-retain-count");

  const label = targetName || (sourcePath ? sourcePath.split("/").filter(Boolean).pop() : "Directory");

  if (jobNameInput) {
    jobNameInput.value = `Nightly ${label} Backup`;
  }
  if (taskTypeSelect) {
    taskTypeSelect.value = "backup.directory";
    updateCronFormTaskConfigVisibility();
  }
  if (pathInput) {
    pathInput.value = sourcePath || "";
  }
  if (retainInput && retainCount) {
    retainInput.value = String(retainCount);
  }

  const cronCard = document.getElementById("cron-scheduler-card");
  cronCard?.scrollIntoView({ behavior: "smooth", block: "start" });
  jobNameInput?.focus();
}

/**
 * Show/hide dynamic task configuration fields in #cron-job-form based on #cron-task-type.
 */
function updateCronFormTaskConfigVisibility() {
  const taskType = document.getElementById("cron-task-type")?.value || "backup.directory";
  const pathGroup = document.getElementById("cron-config-path-group");
  const retainGroup = document.getElementById("cron-config-retain-group");
  const pruneGroup = document.getElementById("cron-config-prune-group");

  const isDirBackup = taskType === "backup.directory";
  const isDockerPrune = taskType === "docker.prune";

  pathGroup?.classList.toggle("hidden", !isDirBackup);
  retainGroup?.classList.toggle("hidden", !isDirBackup);
  pruneGroup?.classList.toggle("hidden", !isDockerPrune);
}

/**
 * Handle manual task type changes, toggling dynamic inputs and auto-checking notifications for status digests.
 */
function handleCronTaskTypeChange() {
  updateCronFormTaskConfigVisibility();
  const taskType = document.getElementById("cron-task-type")?.value;
  if (taskType === "storage.smart_check" || taskType === "notifications.summary") {
    const notifySuccessCheckbox = document.getElementById("cron-notify-success");
    const notifyFailureCheckbox = document.getElementById("cron-notify-failure");
    if (notifySuccessCheckbox) notifySuccessCheckbox.checked = true;
    if (notifyFailureCheckbox) notifyFailureCheckbox.checked = true;
  }
}

/**
 * Populate #cron-job-form to edit an existing scheduled cron job.
 */
function editCronJob(jobId) {
  const jobs = Array.isArray(cachedCronData?.jobs) ? cachedCronData.jobs : [];
  const job = jobs.find((j) => String(j.id) === String(jobId));
  if (!job) {
    showToast({
      title: "Job Not Found",
      message: `Could not find scheduled job #${jobId} to edit.`,
      type: "warning",
    });
    return;
  }

  const idInput = document.getElementById("cron-job-id");
  const nameInput = document.getElementById("cron-job-name");
  const taskTypeSelect = document.getElementById("cron-task-type");
  const presetSelect = document.getElementById("cron-preset-select");
  const exprInput = document.getElementById("cron-expression-input");
  const pathInput = document.getElementById("cron-config-source-path");
  const retainInput = document.getElementById("cron-config-retain-count");
  const pruneVolCheckbox = document.getElementById("cron-config-prune-volumes");
  const notifyFailureCheckbox = document.getElementById("cron-notify-failure");
  const notifySuccessCheckbox = document.getElementById("cron-notify-success");
  const editBanner = document.getElementById("cron-job-edit-indicator");
  const editJobName = document.getElementById("cron-editing-job-name");
  const saveBtn = document.getElementById("cron-save-job-btn");

  if (idInput) idInput.value = String(job.id);
  if (nameInput) nameInput.value = job.name || "";
  if (taskTypeSelect) {
    taskTypeSelect.value = job.task_type || "backup.directory";
    updateCronFormTaskConfigVisibility();
  }

  const expr = job.cron_expression || "";
  if (exprInput) exprInput.value = expr;
  if (presetSelect) {
    const matchingOpt = Array.from(presetSelect.options).find((o) => o.value === expr);
    presetSelect.value = matchingOpt ? expr : "custom";
  }

  const cfg = job.task_config || {};
  if (pathInput) pathInput.value = cfg.source_path || "";
  if (retainInput) retainInput.value = String(cfg.retain_count ?? 7);
  if (pruneVolCheckbox) pruneVolCheckbox.checked = Boolean(cfg.prune_volumes);

  if (notifyFailureCheckbox) notifyFailureCheckbox.checked = Boolean(job.notify_on_failure);
  if (notifySuccessCheckbox) notifySuccessCheckbox.checked = Boolean(job.notify_on_success);

  if (editBanner) {
    editBanner.classList.remove("hidden");
    editBanner.style.display = "flex";
  }
  if (editJobName) editJobName.textContent = job.name || `Job #${job.id}`;
  if (saveBtn) saveBtn.textContent = "Update Scheduled Job";

  const formEl = document.getElementById("cron-job-form");
  formEl?.scrollIntoView({ behavior: "smooth", block: "start" });
  nameInput?.focus();
}

/**
 * Cancel editing and reset the Cron Job form to default creation mode.
 */
function cancelEditCronJob() {
  const idInput = document.getElementById("cron-job-id");
  const nameInput = document.getElementById("cron-job-name");
  const taskTypeSelect = document.getElementById("cron-task-type");
  const presetSelect = document.getElementById("cron-preset-select");
  const exprInput = document.getElementById("cron-expression-input");
  const pathInput = document.getElementById("cron-config-source-path");
  const retainInput = document.getElementById("cron-config-retain-count");
  const pruneVolCheckbox = document.getElementById("cron-config-prune-volumes");
  const notifyFailureCheckbox = document.getElementById("cron-notify-failure");
  const notifySuccessCheckbox = document.getElementById("cron-notify-success");
  const editBanner = document.getElementById("cron-job-edit-indicator");
  const editJobName = document.getElementById("cron-editing-job-name");
  const saveBtn = document.getElementById("cron-save-job-btn");

  if (idInput) idInput.value = "";
  if (nameInput) nameInput.value = "";
  if (taskTypeSelect) taskTypeSelect.value = "backup.directory";
  if (presetSelect) presetSelect.value = "0 2 * * *";
  if (exprInput) exprInput.value = "0 2 * * *";
  if (pathInput) pathInput.value = "";
  if (retainInput) retainInput.value = "7";
  if (pruneVolCheckbox) pruneVolCheckbox.checked = false;
  if (notifyFailureCheckbox) notifyFailureCheckbox.checked = true;
  if (notifySuccessCheckbox) notifySuccessCheckbox.checked = false;

  updateCronFormTaskConfigVisibility();

  if (editBanner) {
    editBanner.classList.add("hidden");
    editBanner.style.display = "none";
  }
  if (editJobName) editJobName.textContent = "--";
  if (saveBtn) saveBtn.textContent = "Save Scheduled Job";
}

/**
 * Fetch scheduled cron jobs and recent execution runs via GET /api/cron/jobs.
 */
async function fetchCronJobs() {
  try {
    const resp = await apiFetch("/api/cron/jobs");
    if (!resp.ok) {
      throw new Error(`Failed to fetch cron jobs (HTTP ${resp.status})`);
    }
    const data = await resp.json();
    cachedCronData = data;
    renderCronJobs(data);
    updateBackupsNavBadge();
  } catch (err) {
    console.error("fetchCronJobs error:", err);
  }
}

/**
 * Render Unified Cron Scheduler Jobs Table and Recent Execution Runs Table.
 */
function renderCronJobs(data) {
  if (!data) return;

  const jobs = Array.isArray(data.jobs) ? data.jobs : [];
  const recentRuns = Array.isArray(data.recent_runs) ? data.recent_runs : [];
  const supportedTypes = Array.isArray(data.supported_task_types) ? data.supported_task_types : [];

  const typeLabelMap = new Map();
  supportedTypes.forEach((t) => {
    if (t && t.type) typeLabelMap.set(t.type, t.label || t.type);
  });

  // Update summary ribbon active count
  const activeCount = jobs.filter((j) => j.enabled).length;
  const activeJobsEl = document.getElementById("cron-active-jobs-count");
  if (activeJobsEl) {
    activeJobsEl.textContent = `${activeCount} Active / ${jobs.length} Total`;
    activeJobsEl.className = activeCount > 0 ? "status-badge status-ok" : "status-badge status-muted";
  }

  // 1. Configured Cron Jobs Table (#cron-jobs-tbody)
  const jobsTbody = document.getElementById("cron-jobs-tbody");
  if (jobsTbody) {
    if (jobs.length === 0) {
      jobsTbody.innerHTML = `<tr><td colspan="7" class="empty-state">No scheduled cron jobs configured. Create a job above to automate backups or maintenance tasks.</td></tr>`;
    } else {
      jobsTbody.innerHTML = jobs
        .map((job) => {
          const jobId = escapeHtml(String(job.id));
          const nameSafe = escapeHtml(job.name || "Unnamed Job");
          const taskType = job.task_type || "";
          const taskLabel = escapeHtml(typeLabelMap.get(taskType) || taskType);
          const cfg = job.task_config || {};
          let targetDesc = "";
          if (taskType === "backup.directory") {
            targetDesc = `Path: ${cfg.source_path || "-"} (Retain ${cfg.retain_count ?? 7})`;
          } else if (taskType === "notifications.summary") {
            targetDesc = "System Health Digest Notification";
          } else if (taskType === "docker.prune" && cfg.prune_volumes) {
            targetDesc = "Include unused volumes";
          } else if (taskType === "packages.refresh") {
            targetDesc = "APT Package Cache Refresh";
          } else if (taskType === "storage.smart_check") {
            targetDesc = "Storage S.M.A.R.T. & RAID Diagnostics";
          }

          const cronExpr = escapeHtml(job.cron_expression || "");
          let notifBadgeHtml = "";
          if (job.notify_on_success && job.notify_on_failure) {
            notifBadgeHtml = '<span class="status-badge cron-notif-badge status-ok" title="Notifies on success and failure">🔔 Success + Failure</span>';
          } else if (job.notify_on_failure) {
            notifBadgeHtml = '<span class="status-badge cron-notif-badge status-muted" title="Notifies on failure only">🔔 Failure Only</span>';
          } else if (job.notify_on_success) {
            notifBadgeHtml = '<span class="status-badge cron-notif-badge status-info" title="Notifies on success only">🔔 Success Only</span>';
          } else {
            notifBadgeHtml = '<span class="status-badge cron-notif-badge status-muted" title="No notifications configured">🔕 None</span>';
          }

          let lastRunHtml = `<span class="text-muted">Never run</span>`;
          if (job.last_run_at || job.last_run_iso) {
            const lastTime = escapeHtml(job.last_run_iso || formatDateTime(job.last_run_at));
            const st = String(job.last_status || "unknown").toLowerCase();
            const stBadgeClass =
              st === "success" || st === "completed" || st === "ok"
                ? "status-ok"
                : st === "running"
                  ? "status-info"
                  : "status-danger";
            lastRunHtml = `
              <div>
                <span class="status-badge ${stBadgeClass}">${escapeHtml(st.toUpperCase())}</span>
                <span class="mono-text text-secondary" style="font-size: 0.78rem; margin-left: 6px;">${lastTime}</span>
              </div>
              ${job.last_output ? `<div class="text-secondary" style="font-size: 0.76rem; margin-top: 2px;">${escapeHtml(String(job.last_output).slice(0, 90))}</div>` : ""}
            `;
          }

          const enabledBadge = job.enabled
            ? `<span class="status-badge status-ok">ENABLED</span>`
            : `<span class="status-badge status-muted">DISABLED</span>`;

          const toggleLabel = job.enabled ? "Disable" : "Enable";

          return `
            <tr>
              <td class="font-bold">${nameSafe}</td>
              <td>
                <div class="font-bold">${taskLabel}</div>
                ${targetDesc ? `<div class="mono-text text-secondary" style="font-size: 0.78rem;">${escapeHtml(targetDesc)}</div>` : ""}
              </td>
              <td class="mono-text font-bold">${cronExpr}</td>
              <td>${notifBadgeHtml}</td>
              <td>${lastRunHtml}</td>
              <td>${enabledBadge}</td>
              <td style="text-align: right;">
                <div class="table-actions-cell">
                  <button
                    type="button"
                    class="btn-backup-action"
                    data-run-cron-id="${jobId}"
                    title="Execute job ${nameSafe} immediately"
                  >
                    Run Now
                  </button>
                  <button
                    type="button"
                    class="btn-backup-action btn-edit"
                    data-edit-cron-id="${jobId}"
                    title="Edit job ${nameSafe}"
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    class="btn-backup-action btn-schedule"
                    data-toggle-cron-id="${jobId}"
                    data-cron-enabled="${job.enabled ? "true" : "false"}"
                    title="${toggleLabel} job ${nameSafe}"
                  >
                    ${toggleLabel}
                  </button>
                  <button
                    type="button"
                    class="btn-icon-danger"
                    data-delete-cron-id="${jobId}"
                    title="Delete job ${nameSafe}"
                  >
                    Delete
                  </button>
                </div>
              </td>
            </tr>
          `;
        })
        .join("");
    }
  }

  // 2. Recent Cron Execution Log Table (#cron-runs-tbody)
  const runsTbody = document.getElementById("cron-runs-tbody");
  if (runsTbody) {
    if (recentRuns.length === 0) {
      runsTbody.innerHTML = `<tr><td colspan="7" class="empty-state">No recent cron job executions recorded.</td></tr>`;
    } else {
      runsTbody.innerHTML = recentRuns
        .map((r) => {
          const jobName = escapeHtml(r.job_name || `Job #${r.job_id || ""}`);
          const taskType = escapeHtml(typeLabelMap.get(r.task_type) || r.task_type || "-");
          const trigger = escapeHtml(String(r.trigger_source || "schedule").toUpperCase());
          const started = escapeHtml(formatDateTime(r.started_at));
          const durationMs = Number(r.duration_ms || 0);
          const durationStr = durationMs >= 1000 ? `${(durationMs / 1000).toFixed(2)}s` : `${durationMs}ms`;
          const st = String(r.status || "unknown").toLowerCase();
          const stClass =
            st === "success" || st === "completed" || st === "ok"
              ? "status-ok"
              : st === "running"
                ? "status-info"
                : "status-danger";
          const outputSafe = escapeHtml(r.output || "-");

          return `
            <tr>
              <td class="font-bold">${jobName}</td>
              <td>${taskType}</td>
              <td><span class="kind-badge">${trigger}</span></td>
              <td class="mono-text text-secondary">${started}</td>
              <td class="mono-text">${escapeHtml(durationStr)}</td>
              <td><span class="status-badge ${stClass}">${escapeHtml(st.toUpperCase())}</span></td>
              <td class="mono-text text-secondary" style="font-size: 0.78rem;">${outputSafe}</td>
            </tr>
          `;
        })
        .join("");
    }
  }
}

/**
 * Handle saving a new or updated cron job via POST /api/cron/jobs.
 */
async function handleSaveCronJob(e) {
  if (e) e.preventDefault();

  const editingJobId = document.getElementById("cron-job-id")?.value;
  const isEditing = Boolean(editingJobId);

  const name = (document.getElementById("cron-job-name")?.value || "").trim();
  const task_type = document.getElementById("cron-task-type")?.value || "backup.directory";
  const cron_expression = (document.getElementById("cron-expression-input")?.value || "").trim();
  const notify_on_failure = Boolean(document.getElementById("cron-notify-failure")?.checked);
  const notify_on_success = Boolean(document.getElementById("cron-notify-success")?.checked);
  const saveBtn = document.getElementById("cron-save-job-btn");

  if (!name || !cron_expression) {
    showToast({
      title: "Missing Fields",
      message: "Please provide both a Job Name and a 5-field Cron Expression.",
      type: "warning",
    });
    return;
  }

  const task_config = {};
  if (task_type === "backup.directory") {
    const srcPath = (document.getElementById("cron-config-source-path")?.value || "").trim();
    if (!srcPath) {
      showToast({
        title: "Directory Path Required",
        message: "Please specify a Directory Source Path for the Host Directory Backup job.",
        type: "warning",
      });
      return;
    }
    task_config.source_path = srcPath;
    task_config.target_name = name.replace(/\s+/g, "-").toLowerCase();
    task_config.retain_count = Math.max(
      1,
      parseInt(document.getElementById("cron-config-retain-count")?.value || "7", 10) || 7
    );
  } else if (task_type === "docker.prune") {
    task_config.prune_volumes = Boolean(document.getElementById("cron-config-prune-volumes")?.checked);
  }

  let isEnabled = true;
  if (isEditing) {
    const jobs = Array.isArray(cachedCronData?.jobs) ? cachedCronData.jobs : [];
    const existing = jobs.find((j) => String(j.id) === String(editingJobId));
    if (existing && typeof existing.enabled === "boolean") {
      isEnabled = existing.enabled;
    }
  }

  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.textContent = isEditing ? "Updating..." : "Saving...";
  }

  try {
    const resp = await apiFetch("/api/cron/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: isEditing ? Number(editingJobId) : null,
        name,
        task_type,
        cron_expression,
        task_config,
        enabled: isEnabled,
        notify_on_failure,
        notify_on_success,
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.message || `Failed to save cron job (HTTP ${resp.status})`);
    }

    showToast({
      title: isEditing ? "Scheduled Job Updated" : "Scheduled Job Saved",
      message: isEditing
        ? `Scheduled job "${name}" updated successfully.`
        : `Cron job "${name}" (${cron_expression}) saved.`,
      type: "success",
    });
    cancelEditCronJob();
    await fetchCronJobs();
  } catch (err) {
    console.error("handleSaveCronJob error:", err);
    showToast({
      title: isEditing ? "Update Cron Job Failed" : "Save Cron Job Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  } finally {
    if (saveBtn) {
      saveBtn.disabled = false;
      const stillEditing = Boolean(document.getElementById("cron-job-id")?.value);
      saveBtn.textContent = stillEditing ? "Update Scheduled Job" : "Save Scheduled Job";
    }
  }
}

/**
 * Enable or disable a cron job via POST /api/cron/jobs/{job_id}/toggle.
 */
async function toggleCronJob(jobId, enabled) {
  if (!jobId) return;
  try {
    const resp = await apiFetch("/api/cron/jobs/" + encodeURIComponent(jobId) + "/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: Boolean(enabled) }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.message || `Failed to toggle cron job (HTTP ${resp.status})`);
    }
    showToast({
      title: enabled ? "Cron Job Enabled" : "Cron Job Paused",
      message: `Scheduled job #${jobId} is now ${enabled ? "enabled" : "disabled"}.`,
      type: "success",
    });
    await fetchCronJobs();
  } catch (err) {
    console.error("toggleCronJob error:", err);
    showToast({
      title: "Toggle Cron Job Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  }
}

/**
 * Execute a cron job immediately via POST /api/cron/jobs/{job_id}/run.
 */
async function runCronJobNow(jobId, buttonEl = null) {
  if (!jobId) return;
  const origText = buttonEl ? buttonEl.textContent : "";
  if (buttonEl) {
    buttonEl.disabled = true;
    buttonEl.textContent = "Running...";
  }
  try {
    const resp = await apiFetch("/api/cron/jobs/" + encodeURIComponent(jobId) + "/run", {
      method: "POST",
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.message || `Failed to run cron job (HTTP ${resp.status})`);
    }
    showToast({
      title: "Cron Job Executed",
      message: data.output || data.message || `Job #${jobId} completed.`,
      type: data.status === "failed" ? "danger" : "success",
    });
    await refreshBackupsAndCron();
  } catch (err) {
    console.error("runCronJobNow error:", err);
    showToast({
      title: "Cron Execution Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  } finally {
    if (buttonEl) {
      buttonEl.disabled = false;
      buttonEl.textContent = origText;
    }
  }
}

/**
 * Delete a cron job via DELETE /api/cron/jobs/{job_id}.
 */
async function deleteCronJob(jobId) {
  if (!jobId) return;
  if (!confirm(`Delete scheduled cron job #${jobId}?`)) {
    return;
  }
  try {
    const resp = await apiFetch("/api/cron/jobs/" + encodeURIComponent(jobId), {
      method: "DELETE",
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.message || `Failed to delete cron job (HTTP ${resp.status})`);
    }
    showToast({
      title: "Cron Job Deleted",
      message: `Scheduled job #${jobId} removed.`,
      type: "success",
    });
    await fetchCronJobs();
  } catch (err) {
    console.error("deleteCronJob error:", err);
    showToast({
      title: "Delete Cron Job Failed",
      message: err instanceof Error ? err.message : String(err),
      type: "danger",
    });
  }
}

/**
 * Initialize listeners and run startup authentication check.
 */
function initDashboard() {
  // Main Navigation Tabs
  document
    .getElementById("nav-tab-overview")
    ?.addEventListener("click", () => switchViewTab("overview", true));

  document
    .getElementById("nav-tab-raid")
    ?.addEventListener("click", () => switchViewTab("raid", true));

  document
    .getElementById("nav-tab-containers")
    ?.addEventListener("click", () => switchViewTab("containers", true));

  document
    .getElementById("nav-tab-probes")
    ?.addEventListener("click", () => switchViewTab("probes", true));

  document
    .getElementById("nav-tab-services")
    ?.addEventListener("click", () => switchViewTab("services", true));

  document
    .getElementById("nav-tab-security")
    ?.addEventListener("click", () => switchViewTab("security", true));

  document
    .getElementById("nav-tab-processes")
    ?.addEventListener("click", () => switchViewTab("processes", true));

  document
    .getElementById("nav-tab-users")
    ?.addEventListener("click", () => switchViewTab("users", true));

  document
    .getElementById("nav-tab-packages")
    ?.addEventListener("click", () => switchViewTab("packages", true));

  document
    .getElementById("nav-tab-notifications")
    ?.addEventListener("click", () => switchViewTab("notifications", true));

  document
    .getElementById("nav-tab-audit")
    ?.addEventListener("click", () => switchViewTab("audit", true));

  document
    .getElementById("nav-tab-backups")
    ?.addEventListener("click", () => switchViewTab("backups", true));

  // Overview Quick Link Actions
  document
    .getElementById("btn-goto-raid")
    ?.addEventListener("click", () => switchViewTab("raid", true));

  document
    .getElementById("btn-goto-storage")
    ?.addEventListener("click", () => switchViewTab("raid", true));

  document
    .getElementById("btn-goto-containers")
    ?.addEventListener("click", () => switchViewTab("containers", true));

  document
    .getElementById("btn-goto-services")
    ?.addEventListener("click", () => switchViewTab("services", true));

  document
    .getElementById("btn-goto-packages")
    ?.addEventListener("click", () => switchViewTab("packages", true));

  document
    .getElementById("btn-goto-notifications")
    ?.addEventListener("click", () => switchViewTab("notifications", true));

  // Packages & Updates Listeners
  document
    .getElementById("btn-refresh-packages")
    ?.addEventListener("click", () => {
      if (currentPackagesSubtab === "python") {
        fetchPythonPackages(true);
      } else if (currentPackagesSubtab === "installed") {
        fetchInstalledPackages(currentPkgSearchQuery, currentPkgPage);
      } else {
        fetchPackageUpdates(true);
      }
    });

  document
    .getElementById("btn-refresh-apt-cache")
    ?.addEventListener("click", refreshAptCache);

  document
    .getElementById("btn-simulate-upgrade")
    ?.addEventListener("click", () => simulatePackageUpgrade());

  document
    .getElementById("btn-install-all-packages")
    ?.addEventListener("click", () => installPackageUpgrade(null));

  document
    .getElementById("btn-pkg-modal-install")
    ?.addEventListener("click", () => {
      if (lastUpgradeModalType === "python") {
        upgradePythonPackage(lastPythonUpgradeTargetPackage);
      } else {
        installPackageUpgrade(lastUpgradeTargetPackage);
      }
    });

  document
    .getElementById("btn-close-pkg-upgrade-modal")
    ?.addEventListener("click", closePackageUpgradeModal);

  document
    .getElementById("btn-dismiss-pkg-upgrade-modal")
    ?.addEventListener("click", closePackageUpgradeModal);

  document
    .getElementById("subtab-pkg-updates-btn")
    ?.addEventListener("click", () => switchPackagesSubtab("updates"));

  document
    .getElementById("subtab-pkg-installed-btn")
    ?.addEventListener("click", () => switchPackagesSubtab("installed"));

  document
    .getElementById("subtab-pkg-python-btn")
    ?.addEventListener("click", () => switchPackagesSubtab("python"));

  document
    .getElementById("pkg-python-search")
    ?.addEventListener("input", renderPythonPackagesTable);

  document
    .getElementById("pkg-python-role-filter")
    ?.addEventListener("change", renderPythonPackagesTable);

  document
    .getElementById("btn-check-pypi-updates")
    ?.addEventListener("click", () => fetchPythonPackages(true));

  document
    .getElementById("btn-upgrade-all-python")
    ?.addEventListener("click", () => upgradePythonPackage(null));

  document
    .getElementById("pkg-updates-search")
    ?.addEventListener("input", () => renderPackageUpdatesTable(cachedPackageUpdates?.updates || []));

  const pkgInstalledSearchInput = document.getElementById("pkg-installed-search");
  pkgInstalledSearchInput?.addEventListener("input", (e) => {
    clearTimeout(pkgInstalledSearchDebounceTimer);
    pkgInstalledSearchDebounceTimer = setTimeout(() => {
      fetchInstalledPackages(e.target.value.trim(), 1);
    }, 300);
  });

  document
    .getElementById("btn-pkg-prev")
    ?.addEventListener("click", () => {
      if (currentPkgPage > 1) {
        fetchInstalledPackages(currentPkgSearchQuery, currentPkgPage - 1);
      }
    });

  document
    .getElementById("btn-pkg-next")
    ?.addEventListener("click", () => {
      if (currentPkgPage < currentPkgTotalPages) {
        fetchInstalledPackages(currentPkgSearchQuery, currentPkgPage + 1);
      }
    });

  // Vigil Sentinel: Uptime & Health Probes Listeners
  document
    .getElementById("refresh-probes-btn")
    ?.addEventListener("click", () => fetchProbes(true));

  document
    .getElementById("btn-add-probe")
    ?.addEventListener("click", openAddProbeModal);

  document
    .getElementById("btn-empty-add-probe")
    ?.addEventListener("click", openAddProbeModal);

  document
    .getElementById("btn-probe-modal-close")
    ?.addEventListener("click", closeProbeModal);

  document
    .getElementById("btn-probe-modal-cancel")
    ?.addEventListener("click", closeProbeModal);

  document
    .getElementById("probe-form")
    ?.addEventListener("submit", handleProbeFormSubmit);

  document
    .getElementById("btn-probe-modal-save")
    ?.addEventListener("click", handleProbeFormSubmit);

  document
    .getElementById("probe-form-type")
    ?.addEventListener("change", (e) => handleProbeTypeChange(e.target.value));

  const probeModal = document.getElementById("probe-modal");
  probeModal?.addEventListener("click", (e) => {
    if (e.target === probeModal) closeProbeModal();
  });

  const probesCardsGrid = document.getElementById("probes-cards-grid");
  probesCardsGrid?.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const action = btn.getAttribute("data-action");
    const id = parseInt(btn.getAttribute("data-id"), 10);
    if (!id) return;

    if (action === "test") {
      testProbe(id, btn);
    } else if (action === "edit") {
      openEditProbeModal(id);
    } else if (action === "delete") {
      deleteProbe(id);
    }
  });

  // CPU Cores Drilldown Toggle
  const btnToggleCpuCores = document.getElementById("btn-toggle-cpu-cores");
  const cpuCoresDrilldown = document.getElementById("cpu-cores-drilldown");
  if (btnToggleCpuCores && cpuCoresDrilldown) {
    btnToggleCpuCores.addEventListener("click", () => {
      const isHidden = cpuCoresDrilldown.classList.toggle("hidden");
      btnToggleCpuCores.setAttribute("aria-expanded", String(!isHidden));
      btnToggleCpuCores.classList.toggle("expanded", !isHidden);
      cpuCoresDrilldown.setAttribute("aria-hidden", String(isHidden));
    });
  }

  // Desktop Collapsible Sidebar Toggle & Persistence
  const appShell = document.getElementById("app-shell");
  const btnToggleSidebar = document.getElementById("btn-toggle-sidebar");
  if (btnToggleSidebar && appShell) {
    const savedState = localStorage.getItem("vigil_sidebar_collapsed");
    if (savedState === "1") {
      appShell.classList.add("sidebar-collapsed");
      btnToggleSidebar.setAttribute("title", "Expand Sidebar");
      btnToggleSidebar.setAttribute("aria-label", "Expand Sidebar");
    }

    btnToggleSidebar.addEventListener("click", () => {
      const isCollapsed = appShell.classList.toggle("sidebar-collapsed");
      btnToggleSidebar.setAttribute("title", isCollapsed ? "Expand Sidebar" : "Collapse Sidebar");
      btnToggleSidebar.setAttribute("aria-label", isCollapsed ? "Expand Sidebar" : "Collapse Sidebar");
      localStorage.setItem("vigil_sidebar_collapsed", isCollapsed ? "1" : "0");
    });
  }

  // Mobile Sidebar Toggle & Backdrop
  const btnMobileNavToggle = document.getElementById("btn-mobile-nav-toggle");
  const mobileNavBackdrop = document.getElementById("mobile-nav-backdrop");
  const appSidebar = document.getElementById("app-sidebar");
  const sidebarBackdrop = document.getElementById("sidebar-backdrop");

  function closeMobileNavDrawer() {
    appSidebar?.classList.remove("mobile-open");
    appSidebar?.classList.remove("open");
    mobileNavBackdrop?.classList.remove("active");
    sidebarBackdrop?.classList.remove("active");
  }

  function toggleMobileNavDrawer() {
    if (appSidebar) {
      const isOpen = appSidebar.classList.toggle("mobile-open");
      mobileNavBackdrop?.classList.toggle("active", isOpen);
    }
  }

  btnMobileNavToggle?.addEventListener("click", toggleMobileNavDrawer);
  mobileNavBackdrop?.addEventListener("click", closeMobileNavDrawer);

  document
    .getElementById("sidebar-toggle-btn")
    ?.addEventListener("click", () => {
      const isOpen = appSidebar?.classList.toggle("open");
      sidebarBackdrop?.classList.toggle("active", isOpen);
    });

  document
    .getElementById("sidebar-backdrop")
    ?.addEventListener("click", closeMobileNavDrawer);

  // Auto-close mobile drawer when navigation links are clicked on mobile
  document.querySelectorAll(".sidebar-nav-item, .nav-link, .btn-sidebar-footer, .sidebar-footer button").forEach((el) => {
    el.addEventListener("click", () => {
      if (window.innerWidth <= 768) {
        closeMobileNavDrawer();
      }
    });
  });

  // Docker Sub-Tabs
  const subtabs = ["stacks", "containers", "images", "volumes", "networks", "updates"];
  subtabs.forEach((st) => {
    document
      .getElementById(`subtab-docker-${st}`)
      ?.addEventListener("click", () => switchDockerSubtab(st, true));
  });

  // Quick Stat Cards Navigation
  document.querySelectorAll(".docker-stat-card[data-subtab]").forEach((card) => {
    card.addEventListener("click", () => {
      const st = card.getAttribute("data-subtab");
      if (st) switchDockerSubtab(st, true);
    });
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        const st = card.getAttribute("data-subtab");
        if (st) switchDockerSubtab(st, true);
      }
    });
  });

  // Docker Actions & Buttons
  document
    .getElementById("btn-install-docker")
    ?.addEventListener("click", handleInstallDocker);

  document
    .getElementById("refresh-docker-btn")
    ?.addEventListener("click", () => {
      requestDockerRefresh();
    });

  // App Catalog
  document
    .getElementById("btn-app-catalog")
    ?.addEventListener("click", openAppCatalog);

  document
    .getElementById("close-app-catalog-btn")
    ?.addEventListener("click", closeAppCatalog);

  const appCatalogModal = document.getElementById("app-catalog-modal");
  appCatalogModal?.addEventListener("click", (e) => {
    if (e.target === appCatalogModal) {
      closeAppCatalog();
    }
  });

  document
    .getElementById("catalog-search-input")
    ?.addEventListener("input", renderAppCatalog);

  document
    .querySelectorAll("#catalog-category-filters .category-filter-btn")
    .forEach((btn) => {
      btn.addEventListener("click", () => {
        document
          .querySelectorAll("#catalog-category-filters .category-filter-btn")
          .forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        currentAppCatalogCategory = btn.getAttribute("data-category") || "All";
        renderAppCatalog();
      });
    });

  // Stacks
  document
    .getElementById("btn-new-stack")
    ?.addEventListener("click", () => openStackEditor());

  document
    .getElementById("close-stack-editor-btn")
    ?.addEventListener("click", closeStackEditor);

  document
    .getElementById("cancel-stack-editor-btn")
    ?.addEventListener("click", closeStackEditor);

  const stackEditorModal = document.getElementById("stack-editor-modal");
  stackEditorModal?.addEventListener("click", (e) => {
    if (e.target === stackEditorModal) {
      closeStackEditor();
    }
  });

  document
    .getElementById("save-stack-btn")
    ?.addEventListener("click", handleSaveStack);

  document
    .getElementById("stack-template-btn")
    ?.addEventListener("click", insertExampleYaml);

  document
    .getElementById("stack-validate-btn")
    ?.addEventListener("click", () => validateStackYaml());

  document
    .getElementById("stack-format-btn")
    ?.addEventListener("click", () => formatStackYaml());

  document
    .getElementById("stack-env-format-btn")
    ?.addEventListener("click", () => formatStackYaml());

  document
    .getElementById("stack-env-mode-kv-btn")
    ?.addEventListener("click", () => switchStackEnvMode("kv"));

  document
    .getElementById("stack-env-mode-raw-btn")
    ?.addEventListener("click", () => switchStackEnvMode("raw"));

  document
    .getElementById("stack-env-import-btn")
    ?.addEventListener("click", () => {
      document.getElementById("stack-env-file-input")?.click();
    });

  document
    .getElementById("stack-env-file-input")
    ?.addEventListener("change", handleImportEnvFile);

  document
    .getElementById("stack-env-export-btn")
    ?.addEventListener("click", exportStackEnvFile);

  document
    .getElementById("stack-env-copy-btn")
    ?.addEventListener("click", copyStackEnvToClipboard);

  document
    .getElementById("stack-env-add-var-btn")
    ?.addEventListener("click", () => addEnvVariableRow("", ""));

  document
    .getElementById("stack-env-search")
    ?.addEventListener("input", handleEnvSearchInput);

  const snippetToolbar = document.getElementById("stack-snippet-toolbar");
  snippetToolbar?.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-snippet]");
    if (!btn) return;
    e.preventDefault();
    const snippetType = btn.getAttribute("data-snippet");
    if (snippetType) {
      insertComposeSnippet(snippetType);
    }
  });

  const yamlTextarea = document.getElementById("stack-yaml-input");
  const envTextarea = document.getElementById("stack-env-input");
  const updateTextareaCursorStatus = (textarea) => {
    if (!textarea || typeof textarea.value !== "string") return;
    const pos = textarea.selectionStart ?? 0;
    const upToCursor = textarea.value.slice(0, pos);
    const lines = upToCursor.split("\n");
    const line = lines.length;
    const col = lines[lines.length - 1].length + 1;
    updateStackEditorStatusbar(line, col);
  };
  if (yamlTextarea) {
    yamlTextarea.addEventListener("keydown", handleComposeTextareaKeydown);
    yamlTextarea.addEventListener("keyup", () => updateTextareaCursorStatus(yamlTextarea));
    yamlTextarea.addEventListener("click", () => updateTextareaCursorStatus(yamlTextarea));
  }
  if (envTextarea) {
    envTextarea.addEventListener("keydown", handleComposeTextareaKeydown);
  }

  document
    .getElementById("tab-btn-compose")
    ?.addEventListener("click", () => switchStackEditorTab("compose"));

  document
    .getElementById("tab-btn-env")
    ?.addEventListener("click", () => switchStackEditorTab("env"));

  // Containers Filters & Search
  const searchInput =
    document.getElementById("containers-search-input") ||
    document.getElementById("docker-container-search");
  searchInput?.addEventListener("input", renderDockerContainers);

  document
    .getElementById("containers-filter-stack")
    ?.addEventListener("change", renderDockerContainers);

  document
    .getElementById("containers-filter-status")
    ?.addEventListener("change", renderDockerContainers);

  document
    .getElementById("docker-containers-all-toggle")
    ?.addEventListener("change", fetchDockerContainers);

  // Images
  document
    .getElementById("btn-pull-image")
    ?.addEventListener("click", handlePullImage);

  document
    .getElementById("btn-prune-images")
    ?.addEventListener("click", handlePruneImages);

  // Volumes
  document
    .getElementById("btn-prune-volumes")
    ?.addEventListener("click", handlePruneVolumes);

  // Updates & Schedule
  document
    .getElementById("docker-auto-update-freq")
    ?.addEventListener("change", handleSaveUpdateFrequency);

  document
    .getElementById("btn-save-update-freq")
    ?.addEventListener("click", handleSaveUpdateFrequency);

  document
    .getElementById("btn-check-docker-updates")
    ?.addEventListener("click", () => fetchDockerUpdates({ force: true }));

  document
    .getElementById("btn-apply-all-updates")
    ?.addEventListener("click", () => handleApplyUpdates());

  document
    .getElementById("docker-updates-tbody")
    ?.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-update-cid]");
      if (btn) {
        const cid = btn.getAttribute("data-update-cid");
        const stack = btn.getAttribute("data-update-stack");
        handleApplyUpdates(cid ? [cid] : null, stack ? [stack] : null);
      }
    });

  document
    .getElementById("btn-save-schedule")
    ?.addEventListener("click", handleSaveUpdateSchedule);

  document
    .getElementById("schedule-enabled-toggle")
    ?.addEventListener("change", (e) => {
      const badge = document.getElementById("update-schedule-badge");
      if (badge) {
        badge.className = "status-badge";
        if (e.target.checked) {
          badge.classList.add("status-clean");
          badge.textContent = "ENABLED";
        } else {
          badge.classList.add("status-disabled");
          badge.textContent = "DISABLED";
        }
      }
    });

  // Logs Modal
  document
    .getElementById("close-container-logs-btn")
    ?.addEventListener("click", closeContainerLogs);

  document
    .getElementById("refresh-logs-btn")
    ?.addEventListener("click", fetchContainerLogs);

  document
    .getElementById("copy-logs-btn")
    ?.addEventListener("click", copyContainerLogs);

  document
    .getElementById("scroll-bottom-logs-btn")
    ?.addEventListener("click", scrollLogsToBottom);

  document
    .getElementById("logs-tail-select")
    ?.addEventListener("change", fetchContainerLogs);

  document
    .getElementById("logs-timestamps-toggle")
    ?.addEventListener("change", fetchContainerLogs);

  document
    .getElementById("logs-autorefresh-toggle")
    ?.addEventListener("change", (e) => {
      if (logsTimerId) {
        clearInterval(logsTimerId);
        logsTimerId = null;
      }
      if (e.target.checked && activeLogContainerId) {
        sendWsLogSubscription(activeLogContainerId);
        const isWsOpen =
          wsConnection &&
          typeof window.WebSocket === "function" &&
          wsConnection.readyState === window.WebSocket.OPEN;
        if (!isWsOpen) {
          logsTimerId = setInterval(fetchContainerLogs, 3000);
        }
      } else {
        sendWsLogSubscription(null);
      }
    });

  // Volume Details Modal
  document
    .getElementById("close-volume-details-btn")
    ?.addEventListener("click", closeVolumeDetails);

  document
    .getElementById("close-vol-modal-btn")
    ?.addEventListener("click", closeVolumeDetails);

  // Container Terminal Modal
  document
    .getElementById("btn-close-terminal")
    ?.addEventListener("click", closeContainerTerminal);

  document
    .getElementById("btn-reconnect-terminal")
    ?.addEventListener("click", () => {
      if (activeTerminalContainerId) {
        const shell =
          document.getElementById("container-terminal-shell-select")?.value || "/bin/sh";
        const user =
          document.getElementById("container-terminal-user-input")?.value?.trim() || "";
        connectContainerTerminal(
          activeTerminalContainerId,
          activeTerminalContainerName,
          shell,
          user
        );
      }
    });

  document
    .getElementById("btn-clear-terminal")
    ?.addEventListener("click", clearTerminalScreen);

  document
    .getElementById("btn-fullscreen-terminal")
    ?.addEventListener("click", toggleTerminalFullscreen);

  document
    .getElementById("container-terminal-shell-select")
    ?.addEventListener("change", (e) => {
      if (activeTerminalContainerId) {
        const shell = e.target.value || "/bin/sh";
        const user =
          document.getElementById("container-terminal-user-input")?.value?.trim() || "";
        connectContainerTerminal(
          activeTerminalContainerId,
          activeTerminalContainerName,
          shell,
          user
        );
      }
    });

  document
    .getElementById("container-terminal-user-input")
    ?.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && activeTerminalContainerId) {
        e.preventDefault();
        const shell =
          document.getElementById("container-terminal-shell-select")?.value || "/bin/sh";
        const user = e.target.value.trim();
        connectContainerTerminal(
          activeTerminalContainerId,
          activeTerminalContainerName,
          shell,
          user
        );
      }
    });

  const terminalModal = document.getElementById("container-terminal-modal");
  terminalModal?.addEventListener("click", (e) => {
    if (e.target === terminalModal) {
      closeContainerTerminal();
    }
  });

  const terminalBody = document.getElementById("container-terminal-body");
  terminalBody?.addEventListener("click", () => {
    document.getElementById("container-terminal-screen")?.focus();
  });

  const terminalScreen = document.getElementById("container-terminal-screen");
  if (terminalScreen) {
    terminalScreen.addEventListener("keydown", handleTerminalKeyDown);
    terminalScreen.addEventListener("paste", handleTerminalPaste);
  }

  window.addEventListener("resize", () => {
    if (activeTerminalContainerId && terminalWs && terminalWs.readyState === WebSocket.OPEN) {
      sendTerminalResize();
    }
  });

  // Global delegation for data-terminal-container-id
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-terminal-container-id]");
    if (btn && !btn.disabled) {
      const cid = btn.getAttribute("data-terminal-container-id");
      const cname = btn.getAttribute("data-terminal-container-name") || cid;
      if (cid) {
        openContainerTerminal(cid, cname);
      }
    }
  });

  // Global Escape Key to dismiss modals
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (
        document.activeElement ===
        document.getElementById("container-terminal-screen")
      ) {
        return;
      }
      closeContainerTerminal();
      closeUpdateModal();
      closeSettingsModal();
      closeAppCatalog();
      closeStackEditor();
      closeContainerLogs();
      closeVolumeDetails();
      closeNotifChannelsModal();
      closeNotifScheduleModal();
      closeNotifThresholdsModal();
      closeSmartModal();
      closeFirewallModal();
    }
  });

  // Timezone Setting Change
  const tzSelect = document.getElementById("setting-timezone");
  if (tzSelect) {
    tzSelect.value = getStoredTimezone();
    const tzHint = document.getElementById("timezone-hint");
    if (tzHint) {
      tzHint.textContent = `Current resolved timezone: ${getEffectiveTimezone()}`;
    }
    tzSelect.addEventListener("change", (e) => {
      setStoredTimezone(e.target.value);
      if (tzHint) {
        tzHint.textContent = `Current resolved timezone: ${getEffectiveTimezone()}`;
      }
      handleManualRefresh();
      if (currentViewTab === "containers") {
        refreshDockerSubtab(currentDockerSubtab);
      }
    });
  }

  // Notification Center Modals Opening & Closing
  document.getElementById("btn-open-notif-channels")?.addEventListener("click", openNotifChannelsModal);
  document.getElementById("btn-close-notif-channels")?.addEventListener("click", closeNotifChannelsModal);
  document.getElementById("btn-cancel-notif-channels")?.addEventListener("click", closeNotifChannelsModal);

  document.getElementById("btn-open-notif-schedule")?.addEventListener("click", () => {
    switchViewTab("backups", true);
    const taskTypeSelect = document.getElementById("cron-task-type");
    if (taskTypeSelect) {
      taskTypeSelect.value = "notifications.summary";
      updateCronFormTaskConfigVisibility();
    }
    const jobNameInput = document.getElementById("cron-job-name");
    if (jobNameInput && !jobNameInput.value) {
      jobNameInput.value = "System Health Digest";
    }
    const cronCard = document.getElementById("cron-scheduler-card");
    cronCard?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  document.getElementById("btn-close-notif-schedule")?.addEventListener("click", closeNotifScheduleModal);
  document.getElementById("btn-cancel-notif-schedule")?.addEventListener("click", closeNotifScheduleModal);
  document.getElementById("btn-save-notif-schedule")?.addEventListener("click", async () => {
    await saveNotificationCenterConfig();
    closeNotifScheduleModal();
  });

  document.getElementById("btn-open-notif-thresholds")?.addEventListener("click", openNotifThresholdsModal);
  document.getElementById("btn-close-notif-thresholds")?.addEventListener("click", closeNotifThresholdsModal);
  document.getElementById("btn-cancel-notif-thresholds")?.addEventListener("click", closeNotifThresholdsModal);
  document.getElementById("btn-save-notif-thresholds")?.addEventListener("click", async () => {
    await saveNotificationCenterConfig();
    closeNotifThresholdsModal();
  });

  // Modal backdrop click handlers
  ["modal-notif-channels", "modal-notif-schedule", "modal-notif-thresholds"].forEach((modalId) => {
    const modalEl = document.getElementById(modalId);
    if (modalEl) {
      modalEl.addEventListener("click", (e) => {
        if (e.target === modalEl) modalEl.classList.add("hidden");
      });
    }
  });

  // Notifications & Notification Center
  document
    .getElementById("save-notifications-btn")
    ?.addEventListener("click", saveNotificationCenterConfig);

  document
    .getElementById("send-test-notification-btn")
    ?.addEventListener("click", handleSendTestNotification);

  document
    .getElementById("save-discord-btn")
    ?.addEventListener("click", handleSaveDiscordSettings);

  // Sync Discord legacy controls bidirectionally
  const notifDiscordToggle = document.getElementById("notif-channel-discord-enable");
  const discordLegacyToggle = document.getElementById("discord-enabled-toggle");
  if (notifDiscordToggle && discordLegacyToggle) {
    notifDiscordToggle.addEventListener("change", (e) => {
      discordLegacyToggle.checked = e.target.checked;
    });
    discordLegacyToggle.addEventListener("change", (e) => {
      notifDiscordToggle.checked = e.target.checked;
    });
  }

  const notifDiscordToken = document.getElementById("notif-discord-token-input");
  const discordLegacyToken = document.getElementById("discord-token-input");
  if (notifDiscordToken && discordLegacyToken) {
    notifDiscordToken.addEventListener("input", (e) => {
      discordLegacyToken.value = e.target.value;
    });
    discordLegacyToken.addEventListener("input", (e) => {
      notifDiscordToken.value = e.target.value;
    });
  }

  const notifDiscordChannel = document.getElementById("notif-discord-channel-input");
  const discordLegacyChannel = document.getElementById("discord-channel-input");
  if (notifDiscordChannel && discordLegacyChannel) {
    notifDiscordChannel.addEventListener("input", (e) => {
      discordLegacyChannel.value = e.target.value;
    });
    discordLegacyChannel.addEventListener("input", (e) => {
      notifDiscordChannel.value = e.target.value;
    });
  }

  // Schedule controls handlers
  const schedFreqSelect = document.getElementById("notif-schedule-freq");
  const schedDayGroup = document.getElementById("notif-schedule-day-group");
  if (schedFreqSelect) {
    schedFreqSelect.addEventListener("change", (e) => {
      if (schedDayGroup) {
        schedDayGroup.classList.toggle("hidden", e.target.value !== "weekly");
      }
      updateNextRunDisplay();
    });
  }
  document.getElementById("notif-schedule-day")?.addEventListener("change", () => updateNextRunDisplay());
  document.getElementById("notif-schedule-time")?.addEventListener("input", () => updateNextRunDisplay());
  document.getElementById("notif-schedule-enable")?.addEventListener("change", () => updateNextRunDisplay());

  // Threshold sliders and number inputs sync
  const wireThresholdInputs = (sliderId, numberId) => {
    const slider = document.getElementById(sliderId);
    const num = document.getElementById(numberId);
    if (!slider || !num) return;
    slider.addEventListener("input", (e) => {
      num.value = e.target.value;
    });
    num.addEventListener("input", (e) => {
      slider.value = e.target.value;
    });
  };
  wireThresholdInputs("notif-threshold-ram-slider", "notif-threshold-ram-value");
  wireThresholdInputs("notif-threshold-cpu-slider", "notif-threshold-cpu-value");
  wireThresholdInputs("notif-threshold-disk-slider", "notif-threshold-disk-value");

  window.addEventListener("hashchange", syncTabFromHash);
  syncTabFromHash();

  document
    .getElementById("refresh-btn")
    ?.addEventListener("click", () => handleManualRefresh());

  document
    .getElementById("system-interval-select")
    ?.addEventListener("change", handleIntervalChange);

  document
    .getElementById("raid-interval-select")
    ?.addEventListener("change", handleIntervalChange);

  document
    .getElementById("notifications-interval-select")
    ?.addEventListener("change", handleIntervalChange);

  document
    .getElementById("docker-interval-select")
    ?.addEventListener("change", handleIntervalChange);

  document
    .getElementById("refresh-system-btn")
    ?.addEventListener("click", () => requestModuleRefresh("system"));

  document
    .getElementById("refresh-raid-btn")
    ?.addEventListener("click", () => requestModuleRefresh("raid"));

  document
    .getElementById("refresh-notifications-btn")
    ?.addEventListener("click", () => requestModuleRefresh("notifications"));

  // Systemctl Services Listeners
  document
    .getElementById("refresh-services-btn")
    ?.addEventListener("click", fetchSystemServices);

  document
    .getElementById("services-search-input")
    ?.addEventListener("input", (e) => {
      servicesSearchTerm = e.target.value;
      if (cachedServicesData) renderSystemServices(cachedServicesData);
    });

  document
    .getElementById("services-state-filter")
    ?.addEventListener("change", (e) => {
      servicesFilterState = e.target.value;
      if (cachedServicesData) renderSystemServices(cachedServicesData);
    });

  document
    .getElementById("btn-sidebar-vigil-logs")
    ?.addEventListener("click", openVigilLogsModal);

  document
    .getElementById("service-logs-lines-select")
    ?.addEventListener("change", () => {
      if (currentViewingService) fetchServiceLogs(currentViewingService);
    });

  document
    .getElementById("btn-copy-service-logs")
    ?.addEventListener("click", async () => {
      const content = document.getElementById("service-logs-content");
      if (content && content.textContent) {
        try {
          await navigator.clipboard.writeText(content.textContent);
          showToast("Vigil service logs copied to clipboard", "success");
        } catch {
          showToast("Failed to copy logs to clipboard", "error");
        }
      }
    });

  document
    .getElementById("btn-close-service-logs")
    ?.addEventListener("click", closeServiceLogsModal);

  document
    .getElementById("btn-dismiss-service-logs")
    ?.addEventListener("click", closeServiceLogsModal);

  document
    .getElementById("btn-refresh-service-logs")
    ?.addEventListener("click", () => {
      if (currentViewingService) fetchServiceLogs(currentViewingService);
    });

  const serviceLogsModal = document.getElementById("service-logs-modal");
  serviceLogsModal?.addEventListener("click", (e) => {
    if (e.target === serviceLogsModal) closeServiceLogsModal();
  });

  // Process Explorer Listeners
  document
    .getElementById("refresh-processes-btn")
    ?.addEventListener("click", fetchProcesses);

  let procSearchDebounce = null;
  document
    .getElementById("processes-search-input")
    ?.addEventListener("input", (e) => {
      processesSearchTerm = e.target.value;
      clearTimeout(procSearchDebounce);
      procSearchDebounce = setTimeout(() => fetchProcesses(), 250);
    });

  document
    .getElementById("processes-sort-select")
    ?.addEventListener("change", (e) => {
      processesSort = e.target.value;
      fetchProcesses();
    });

  document
    .getElementById("processes-limit-select")
    ?.addEventListener("change", (e) => {
      processesLimit = parseInt(e.target.value, 10) || 50;
      fetchProcesses();
    });

  document
    .getElementById("btn-close-kill-modal")
    ?.addEventListener("click", closeProcessKillModal);

  document
    .getElementById("btn-cancel-kill-process")
    ?.addEventListener("click", closeProcessKillModal);

  document
    .getElementById("btn-confirm-kill-process")
    ?.addEventListener("click", handleKillProcess);

  const processKillModal = document.getElementById("process-kill-modal");
  processKillModal?.addEventListener("click", (e) => {
    if (e.target === processKillModal) closeProcessKillModal();
  });

  // Users & Groups Listeners
  document
    .getElementById("refresh-users-btn")
    ?.addEventListener("click", () => switchUsersSubtab(currentUsersSubtab));

  document
    .getElementById("subtab-users-btn")
    ?.addEventListener("click", () => switchUsersSubtab("users"));

  document
    .getElementById("subtab-groups-btn")
    ?.addEventListener("click", () => switchUsersSubtab("groups"));

  document
    .getElementById("users-regular-only-toggle")
    ?.addEventListener("change", (e) => {
      usersRegularOnly = Boolean(e.target.checked);
      if (cachedUsersData) renderSystemUsers(cachedUsersData);
    });

  document
    .getElementById("users-search-input")
    ?.addEventListener("input", (e) => {
      usersSearchTerm = e.target.value;
      if (cachedUsersData) renderSystemUsers(cachedUsersData);
    });

  document
    .getElementById("groups-search-input")
    ?.addEventListener("input", (e) => {
      groupsSearchTerm = e.target.value;
      if (cachedGroupsData) renderSystemGroups(cachedGroupsData);
    });

  document
    .getElementById("btn-open-add-user")
    ?.addEventListener("click", openAddUserModal);

  document
    .getElementById("btn-close-add-user")
    ?.addEventListener("click", closeAddUserModal);

  document
    .getElementById("btn-cancel-add-user")
    ?.addEventListener("click", closeAddUserModal);

  document
    .getElementById("form-add-user")
    ?.addEventListener("submit", handleAddUserSubmit);

  document
    .getElementById("btn-close-edit-user")
    ?.addEventListener("click", closeEditUserModal);

  document
    .getElementById("btn-cancel-edit-user")
    ?.addEventListener("click", closeEditUserModal);

  document
    .getElementById("form-edit-user")
    ?.addEventListener("submit", handleEditUserSubmit);

  document
    .getElementById("btn-open-add-group")
    ?.addEventListener("click", openAddGroupModal);

  document
    .getElementById("btn-close-add-group")
    ?.addEventListener("click", closeAddGroupModal);

  document
    .getElementById("btn-cancel-add-group")
    ?.addEventListener("click", closeAddGroupModal);

  document
    .getElementById("form-add-group")
    ?.addEventListener("submit", handleAddGroupSubmit);

  document
    .getElementById("btn-close-group-member")
    ?.addEventListener("click", closeGroupMemberModal);

  document
    .getElementById("btn-cancel-group-member")
    ?.addEventListener("click", closeGroupMemberModal);

  document
    .getElementById("form-group-member")
    ?.addEventListener("submit", handleGroupMemberSubmit);

  // Security & Audit Logs Listeners
  document
    .getElementById("refresh-audit-btn")
    ?.addEventListener("click", fetchAuditLogs);

  let auditSearchDebounce = null;
  document
    .getElementById("audit-search-input")
    ?.addEventListener("input", (e) => {
      auditSearchTerm = e.target.value;
      clearTimeout(auditSearchDebounce);
      auditSearchDebounce = setTimeout(() => fetchAuditLogs(), 250);
    });

  document
    .getElementById("audit-severity-filter")
    ?.addEventListener("change", (e) => {
      auditSeverityFilter = e.target.value;
      fetchAuditLogs();
    });

  document
    .getElementById("auth-setup-form")
    ?.addEventListener("submit", handleSetupSubmit);

  document
    .getElementById("auth-login-form")
    ?.addEventListener("submit", handleLoginSubmit);

  document
    .getElementById("btn-logout")
    ?.addEventListener("click", handleLogout);

  document
    .getElementById("logout-btn")
    ?.addEventListener("click", handleLogout);

  document
    .getElementById("nav-update-notice-btn")
    ?.addEventListener("click", openUpdateModal);

  document
    .getElementById("close-update-modal-btn")
    ?.addEventListener("click", closeUpdateModal);

  document
    .getElementById("modal-dismiss-btn")
    ?.addEventListener("click", closeUpdateModal);

  document
    .getElementById("modal-apply-update-btn")
    ?.addEventListener("click", applyReleaseUpdate);

  document
    .getElementById("modal-toggle-changelog-btn")
    ?.addEventListener("click", toggleFullChangelog);

  const updateModal = document.getElementById("update-modal");
  updateModal?.addEventListener("click", (e) => {
    if (e.target === updateModal) {
      closeUpdateModal();
    }
  });

  document
    .getElementById("check-update-btn")
    ?.addEventListener("click", () => checkForUpdates({ silent: false }));

  document
    .getElementById("apply-update-btn")
    ?.addEventListener("click", applyReleaseUpdate);

  document
    .getElementById("nav-settings-btn")
    ?.addEventListener("click", openSettingsModal);

  document
    .getElementById("settings-btn")
    ?.addEventListener("click", openSettingsModal);

  document
    .getElementById("close-settings-btn")
    ?.addEventListener("click", closeSettingsModal);

  document
    .getElementById("cancel-settings-btn")
    ?.addEventListener("click", closeSettingsModal);

  document
    .getElementById("settings-form")
    ?.addEventListener("submit", handleSettingsSubmit);

  // Settings Subtabs & System Diagnostics Handlers
  document
    .getElementById("subtab-settings-general")
    ?.addEventListener("click", () => switchSettingsSubtab("general"));

  document
    .getElementById("subtab-settings")
    ?.addEventListener("click", () => switchSettingsSubtab("diagnostics"));

  document
    .getElementById("subtab-settings-diagnostics")
    ?.addEventListener("click", () => switchSettingsSubtab("diagnostics"));

  document
    .getElementById("btn-run-diagnostics")
    ?.addEventListener("click", () => runSystemDiagnostics());

  // Notification Drawer Handlers
  document
    .getElementById("btn-topbar-notifications")
    ?.addEventListener("click", openNotificationDrawer);

  document
    .getElementById("btn-close-notification-drawer")
    ?.addEventListener("click", closeNotificationDrawer);

  document
    .getElementById("notification-drawer-backdrop")
    ?.addEventListener("click", closeNotificationDrawer);

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const drawer = document.getElementById("notification-drawer");
      if (drawer && drawer.classList.contains("open")) {
        closeNotificationDrawer();
      }
    }
  });

  document
    .getElementById("btn-drawer-refresh-history")
    ?.addEventListener("click", fetchNotificationHistoryDrawer);

  document
    .getElementById("btn-drawer-dismiss-all")
    ?.addEventListener("click", () => {
      const allRecords = cachedDrawerRecords || [];
      allRecords.forEach((entry) => {
        const id = String(entry.id ?? entry.sent_at ?? entry.timestamp ?? "");
        if (id) dismissedNotificationIds.add(id);
      });
      saveDismissedNotifications();
      renderDrawerNotificationList();
    });

  const setupDrawerFilterTab = (tabId, filterName) => {
    document.getElementById(tabId)?.addEventListener("click", () => {
      currentDrawerFilter = filterName;
      ["drawer-tab-all", "drawer-tab-alerts", "drawer-tab-app"].forEach((id) => {
        const el = document.getElementById(id);
        const isActive = id === tabId;
        el?.classList.toggle("active", isActive);
        el?.setAttribute("aria-selected", isActive ? "true" : "false");
      });
      renderDrawerNotificationList();
    });
  };
  setupDrawerFilterTab("drawer-tab-all", "all");
  setupDrawerFilterTab("drawer-tab-alerts", "alerts");
  setupDrawerFilterTab("drawer-tab-app", "application");

  // Audit view subtabs & redirect banner
  document
    .getElementById("subtab-audit-security-btn")
    ?.addEventListener("click", () => switchAuditSubtab("security"));
  document
    .getElementById("subtab-audit-alerts-btn")
    ?.addEventListener("click", () => switchAuditSubtab("alerts"));
  document
    .getElementById("btn-goto-alert-logs")
    ?.addEventListener("click", () => {
      switchViewTab("audit", true);
      switchAuditSubtab("alerts");
    });

  const navigateDrawerToCenter = () => {
    closeNotificationDrawer();
    switchViewTab("notifications", true);
  };

  document
    .getElementById("btn-drawer-goto-notifications")
    ?.addEventListener("click", navigateDrawerToCenter);

  document
    .getElementById("btn-drawer-footer-settings")
    ?.addEventListener("click", navigateDrawerToCenter);

  // System Update Notice Bar Handlers
  document
    .getElementById("btn-notice-view-updates")
    ?.addEventListener("click", () => switchViewTab("packages", true));

  document
    .getElementById("btn-dismiss-update-notice")
    ?.addEventListener("click", () => {
      systemUpdateNoticeDismissed = true;
      const bar = document.getElementById("system-update-notice-bar");
      if (bar) bar.classList.add("hidden");
    });

  // HiDPI / Window Resize Listener for Unified CPU Cores Canvas
  window.addEventListener("resize", () => {
    const unifiedCanvas =
      document.getElementById("cpu-cores-unified-canvas") ||
      document.querySelector(".cpu-core-canvas");
    if (unifiedCanvas && cpuCoresHistoryBuffers.length > 0) {
      const latestValues = cpuCoresHistoryBuffers.map((b) => (b.length > 0 ? b[b.length - 1] : 0));
      drawCpuCoreGraph(unifiedCanvas, cpuCoresHistoryBuffers, latestValues, activeHoveredCoreIndex);
    }
  });

  if (!localTickerTimerId) {
    localTickerTimerId = window.setInterval(updateLiveTickers, 1000);
  }

  // S.M.A.R.T. Health Diagnostics Modal & Member Disks Table Delegation
  const disksTbody = document.getElementById("active-disks-tbody");
  disksTbody?.addEventListener("click", (e) => {
    const btn = e.target.closest("button.smart-badge[data-disk]");
    if (btn) {
      const disk = btn.getAttribute("data-disk");
      if (disk) openSmartModal(disk);
    }
  });

  document
    .getElementById("btn-smart-modal-close")
    ?.addEventListener("click", closeSmartModal);

  document
    .getElementById("btn-smart-modal-cancel")
    ?.addEventListener("click", closeSmartModal);

  const smartModal = document.getElementById("smart-modal");
  smartModal?.addEventListener("click", (e) => {
    if (e.target === smartModal) closeSmartModal();
  });

  // Network & Security and UFW Firewall Modal Listeners
  document
    .getElementById("refresh-security-btn")
    ?.addEventListener("click", () => fetchNetworkSecurity(true));

  document
    .getElementById("sec-ports-filter")
    ?.addEventListener("input", filterPortsTable);

  document
    .getElementById("btn-add-firewall-rule")
    ?.addEventListener("click", () => openAddFirewallRuleModal("", "any"));

  document
    .getElementById("btn-firewall-modal-close")
    ?.addEventListener("click", closeFirewallModal);

  document
    .getElementById("btn-firewall-modal-cancel")
    ?.addEventListener("click", closeFirewallModal);

  document
    .getElementById("firewall-form")
    ?.addEventListener("submit", handleFirewallFormSubmit);

  const firewallModal = document.getElementById("firewall-modal");
  firewallModal?.addEventListener("click", (e) => {
    if (e.target === firewallModal) closeFirewallModal();
  });

  // SSH Intrusion & IP Ban Manager Listeners
  const banIpBtn = document.getElementById("sec-ban-ip-btn");
  const banIpInput = document.getElementById("sec-ban-ip-input");

  banIpBtn?.addEventListener("click", async () => {
    const ip = (banIpInput?.value || "").trim();
    if (!ip) {
      showToast({
        title: "IP Required",
        message: "Enter an IPv4/IPv6 address or CIDR to ban.",
        type: "warning",
      });
      banIpInput?.focus();
      return;
    }
    await submitSshIpBan("ban", ip, banIpBtn);
    if (banIpInput) banIpInput.value = "";
  });

  banIpInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      banIpBtn?.click();
    }
  });

  const sshOffendersTbody = document.getElementById("sec-ssh-offenders-tbody");
  sshOffendersTbody?.addEventListener("click", (e) => {
    const banBtn = e.target.closest("[data-ban-ip]");
    if (banBtn) {
      const ip = banBtn.getAttribute("data-ban-ip");
      if (ip) submitSshIpBan("ban", ip, banBtn);
      return;
    }
    const unbanBtn = e.target.closest("[data-unban-ip]");
    if (unbanBtn) {
      const ip = unbanBtn.getAttribute("data-unban-ip");
      if (ip) submitSshIpBan("unban", ip, unbanBtn);
    }
  });

  // Backups & Unified Vigil Cron Scheduler Listeners
  document
    .getElementById("backups-refresh-btn")
    ?.addEventListener("click", () => refreshBackupsAndCron(true));

  const backupDirSaveBtn = document.getElementById("backup-dir-save-btn");
  backupDirSaveBtn?.addEventListener("click", () => saveBackupDirectoryConfig(backupDirSaveBtn));

  document.getElementById("backup-dir-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      saveBackupDirectoryConfig(backupDirSaveBtn);
    }
  });

  const backupRunNowBtn = document.getElementById("backup-run-now-btn");
  backupRunNowBtn?.addEventListener("click", async () => {
    const payload = getBackupFormPayload();
    if (!payload.source_path) {
      showToast({
        title: "Directory Path Required",
        message: "Please enter a host directory path to back up.",
        type: "warning",
      });
      return;
    }
    await executeBackupRun(payload, backupRunNowBtn);
  });

  const backupSaveTargetBtn = document.getElementById("backup-save-target-btn");
  backupSaveTargetBtn?.addEventListener("click", () => saveBackupTarget(backupSaveTargetBtn));

  const backupTargetsTbody = document.getElementById("backup-targets-tbody");
  backupTargetsTbody?.addEventListener("click", (e) => {
    const runBtn = e.target.closest("[data-backup-now]");
    if (runBtn) {
      const source_path = runBtn.getAttribute("data-source-path") || "";
      const target_name = runBtn.getAttribute("data-target-name") || "backup";
      const retain_count = parseInt(runBtn.getAttribute("data-retain-count") || "7", 10) || 7;
      const rawExcludes = runBtn.getAttribute("data-excludes") || "";
      const excludes = rawExcludes
        ? rawExcludes
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean)
        : [];

      executeBackupRun(
        {
          target_type: "directory",
          source_path,
          target_name,
          name: target_name,
          excludes,
          stop_containers: false,
          retain_count,
        },
        runBtn
      );
      return;
    }

    const schedBtn = e.target.closest("[data-schedule-backup]");
    if (schedBtn) {
      prefillCronFormForTarget({
        sourcePath: schedBtn.getAttribute("data-source-path") || "",
        targetName: schedBtn.getAttribute("data-target-name") || "",
        retainCount: schedBtn.getAttribute("data-retain-count") || "7",
      });
      return;
    }

    const delTargetBtn = e.target.closest("[data-delete-target-id]");
    if (delTargetBtn) {
      const targetId = delTargetBtn.getAttribute("data-delete-target-id");
      if (targetId) deleteBackupTarget(targetId);
    }
  });

  const backupArchivesTbody = document.getElementById("backup-archives-tbody");
  backupArchivesTbody?.addEventListener("click", (e) => {
    const restoreBtn = e.target.closest("[data-restore-archive]");
    if (restoreBtn) {
      const filename = restoreBtn.getAttribute("data-restore-archive");
      const srcPath = restoreBtn.getAttribute("data-source-path") || "";
      if (filename) restoreBackupArchive(filename, srcPath === "-" ? "" : srcPath);
      return;
    }

    const delArchiveBtn = e.target.closest("[data-delete-archive]");
    if (delArchiveBtn) {
      const filename = delArchiveBtn.getAttribute("data-delete-archive");
      if (filename) deleteBackupArchive(filename);
    }
  });

  document
    .getElementById("cron-task-type")
    ?.addEventListener("change", handleCronTaskTypeChange);

  document
    .getElementById("cron-cancel-edit-btn")
    ?.addEventListener("click", cancelEditCronJob);

  const cronPresetSelect = document.getElementById("cron-preset-select");
  const cronExprInput = document.getElementById("cron-expression-input");
  cronPresetSelect?.addEventListener("change", (e) => {
    const val = e.target.value;
    if (val && val !== "custom" && cronExprInput) {
      cronExprInput.value = val;
    }
  });
  cronExprInput?.addEventListener("input", () => {
    if (!cronPresetSelect) return;
    const val = cronExprInput.value.trim();
    const matchingOpt = Array.from(cronPresetSelect.options).find((o) => o.value === val);
    cronPresetSelect.value = matchingOpt ? val : "custom";
  });

  document
    .getElementById("cron-job-form")
    ?.addEventListener("submit", handleSaveCronJob);

  const cronJobsTbody = document.getElementById("cron-jobs-tbody");
  cronJobsTbody?.addEventListener("click", (e) => {
    const runBtn = e.target.closest("[data-run-cron-id]");
    if (runBtn) {
      const jobId = runBtn.getAttribute("data-run-cron-id");
      if (jobId) runCronJobNow(jobId, runBtn);
      return;
    }

    const editBtn = e.target.closest("[data-edit-cron-id]");
    if (editBtn) {
      const jobId = editBtn.getAttribute("data-edit-cron-id");
      if (jobId) editCronJob(jobId);
      return;
    }

    const toggleBtn = e.target.closest("[data-toggle-cron-id]");
    if (toggleBtn) {
      const jobId = toggleBtn.getAttribute("data-toggle-cron-id");
      const currentlyEnabled = toggleBtn.getAttribute("data-cron-enabled") === "true";
      if (jobId) toggleCronJob(jobId, !currentlyEnabled);
      return;
    }

    const delBtn = e.target.closest("[data-delete-cron-id]");
    if (delBtn) {
      const jobId = delBtn.getAttribute("data-delete-cron-id");
      if (jobId) deleteCronJob(jobId);
    }
  });

  updateCronFormTaskConfigVisibility();

  initTelemetryTimeMachine();

  initActionsDropdowns();

  checkAuthStatus();

  startLiveRenderLoop();
}

document.addEventListener("DOMContentLoaded", initDashboard);

if (typeof window !== "undefined") {
  window.VigilLiveStore = VigilLiveStore;
  window.liveAnimState = liveAnimState;
  window.startLiveRenderLoop = startLiveRenderLoop;
  window.stopLiveRenderLoop = stopLiveRenderLoop;
  window.drawCpuCoreGraph = drawCpuCoreGraph;
  window.initSSEStream = initSSEStream;
  window.initSSEConnection = initSSEConnection;
  window.closeSSEStream = closeSSEStream;
  window.closeSSEConnection = closeSSEConnection;
  window.pollSystemStatus = pollSystemStatus;
  window.pollDockerUpdates = pollDockerUpdates;
  window.adjustAdaptivePolling = adjustAdaptivePolling;
  window.patchDockerContainerRowInPlace = patchDockerContainerRowInPlace;
  window.prependNotificationRow = prependNotificationRow;
  window.handleSSETelemetry = handleSSETelemetry;
  window.handleSSESnapshot = handleSSESnapshot;
  window.handleSSEDockerEvent = handleSSEDockerEvent;
  window.handleSSEModuleUpdate = handleSSEModuleUpdate;
  window.handleSSENotification = handleSSENotification;
  window.getSSEConnected = () => sseConnected;
  window.getSSESource = () => sseSource;
}
