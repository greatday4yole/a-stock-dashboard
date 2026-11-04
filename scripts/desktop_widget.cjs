const { app, BrowserWindow, ipcMain, screen, shell } = require("electron");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const schedule = require("./widget_schedule.cjs");

const DEFAULT_ORIGIN = "http://127.0.0.1:8765";
const FALLBACK_ORIGIN = "http://127.0.0.1:8766";
const DEFAULT_BOUNDS = { width: 180, height: 130 };
const MIN_BOUNDS = { width: 140, height: 100 };
const MAX_BOUNDS = { width: 900, height: 700 };
const RESIZE_DIRECTIONS = new Set(["n", "s", "e", "w", "ne", "nw", "se", "sw"]);
const DEFAULT_OPACITY = 0.6;
const VISIBILITY_POLL_MS = 20_000;
let widgetWindow = null;
let saveBoundsTimer = null;
let retryTimer = null;
let visibilityTimer = null;
let marketPhase = "";
let displayed = false;
let pointerDrag = null;
let resizeDrag = null;
let pinnedOnTop = true;
let activeOrigin = DEFAULT_ORIGIN;
let desktopAdapter = null;
let desktopAdapterLoadError = null;

if (process.platform === "darwin") {
  try {
    desktopAdapter = require(path.join(__dirname, "../data/runtime/desktop_window_level.node"));
  } catch (error) {
    desktopAdapterLoadError = error;
  }
}

function dashboardOrigin(value = process.env.DASHBOARD_ORIGIN || DEFAULT_ORIGIN) {
  const url = new URL(value);
  const loopback = url.hostname === "127.0.0.1" || url.hostname === "localhost" || url.hostname === "[::1]";
  if (url.protocol !== "http:" || !loopback || url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("DASHBOARD_ORIGIN 必须是仅含协议、主机和端口的本机 HTTP 地址");
  }
  return url.origin;
}

function probeOrigin(origin) {
  return new Promise((resolve) => {
    const request = http.get(`${origin}/widget.html`, { timeout: 800 }, (response) => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.on("timeout", () => {
      request.destroy();
      resolve(false);
    });
    request.on("error", () => resolve(false));
  });
}

async function resolveOrigin() {
  if (process.env.DASHBOARD_ORIGIN) return dashboardOrigin();
  for (const candidate of [DEFAULT_ORIGIN, FALLBACK_ORIGIN]) {
    if (await probeOrigin(candidate)) return candidate;
  }
  return DEFAULT_ORIGIN;
}

function boundsPath() {
  return path.join(app.getPath("userData"), "pnl-widget-window.json");
}

function readSavedState() {
  try {
    return JSON.parse(fs.readFileSync(boundsPath(), "utf8"));
  } catch {
    return {};
  }
}

function readSavedBounds() {
  const saved = readSavedState();
  const bounds = {};
  for (const key of ["x", "y", "width", "height"]) {
    if (Number.isFinite(saved[key])) bounds[key] = Math.round(saved[key]);
  }
  if (bounds.width < MIN_BOUNDS.width || bounds.width > MAX_BOUNDS.width
      || bounds.height < MIN_BOUNDS.height || bounds.height > MAX_BOUNDS.height) {
    delete bounds.width;
    delete bounds.height;
  }
  return bounds;
}

function readSavedPinned() {
  return readSavedState().pinned !== false;
}

function boundsAreVisible(bounds) {
  if (!Number.isFinite(bounds.x) || !Number.isFinite(bounds.y)) return false;
  const width = bounds.width || DEFAULT_BOUNDS.width;
  const height = bounds.height || DEFAULT_BOUNDS.height;
  return screen.getAllDisplays().some((display) => {
    const area = display.workArea;
    const overlapWidth = Math.min(bounds.x + width, area.x + area.width) - Math.max(bounds.x, area.x);
    const overlapHeight = Math.min(bounds.y + height, area.y + area.height) - Math.max(bounds.y, area.y);
    return overlapWidth >= 80 && overlapHeight >= 60;
  });
}

function initialBounds() {
  const saved = readSavedBounds();
  const bounds = { ...DEFAULT_BOUNDS, ...saved };
  if (boundsAreVisible(bounds)) return bounds;
  const area = screen.getPrimaryDisplay().workArea;
  return {
    ...bounds,
    x: Math.round(area.x + area.width - bounds.width - 20),
    y: Math.round(area.y + 20),
  };
}

function saveBounds() {
  if (!widgetWindow || widgetWindow.isDestroyed()) return;
  const bounds = widgetWindow.getBounds();
  const target = boundsPath();
  const temporary = `${target}.tmp`;
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(temporary, `${JSON.stringify({ ...bounds, pinned: pinnedOnTop })}\n`, "utf8");
    fs.renameSync(temporary, target);
  } catch (error) {
    console.warn(`无法保存悬浮窗位置：${error.message}`);
  }
}

function queueSaveBounds() {
  clearTimeout(saveBoundsTimer);
  saveBoundsTimer = setTimeout(saveBounds, 250);
}

function ownsSender(event) {
  return widgetWindow && !widgetWindow.isDestroyed() && event.sender === widgetWindow.webContents;
}

function validPointer(point) {
  return point && Number.isFinite(point.x) && Number.isFinite(point.y);
}

function applyFloatingMode(enabled) {
  if (!widgetWindow || widgetWindow.isDestroyed()) return;
  if (desktopAdapter) {
    desktopAdapter.setFloatingMode(widgetWindow.getNativeWindowHandle(), enabled);
  }
  widgetWindow.setAlwaysOnTop(enabled, "floating");
  if (enabled) {
    widgetWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  } else {
    widgetWindow.setVisibleOnAllWorkspaces(false, { visibleOnFullScreen: false });
  }
}

function applyVisibility() {
  if (!widgetWindow || widgetWindow.isDestroyed()) return;
  const visible = schedule.shouldDisplay(new Date(), marketPhase);
  if (visible === displayed) return;
  displayed = visible;
  if (visible) {
    applyFloatingMode(pinnedOnTop);
    widgetWindow.showInactive();
  } else {
    widgetWindow.hide();
    applyFloatingMode(false);
    pointerDrag = null;
    resizeDrag = null;
  }
}

function startVisibilityScheduler() {
  clearInterval(visibilityTimer);
  visibilityTimer = setInterval(applyVisibility, VISIBILITY_POLL_MS);
  applyVisibility();
}

function nativeWindowLevel() {
  if (!desktopAdapter || !widgetWindow || widgetWindow.isDestroyed()) return null;
  return desktopAdapter.getWindowLevel(widgetWindow.getNativeWindowHandle());
}

function loadWidget() {
  if (!widgetWindow || widgetWindow.isDestroyed()) return;
  void widgetWindow.loadURL(`${activeOrigin}/widget.html`).catch(() => {});
}

function createWidgetWindow() {
  widgetWindow = new BrowserWindow({
    ...initialBounds(),
    frame: false,
    transparent: true,
    resizable: true,
    minWidth: MIN_BOUNDS.width,
    minHeight: MIN_BOUNDS.height,
    maxWidth: MAX_BOUNDS.width,
    maxHeight: MAX_BOUNDS.height,
    maximizable: false,
    fullscreenable: false,
    alwaysOnTop: false,
    skipTaskbar: true,
    show: false,
    hasShadow: true,
    acceptFirstMouse: true,
    backgroundColor: "#00000000",
    webPreferences: {
      preload: path.join(__dirname, "desktop_widget_preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
    },
  });

  widgetWindow.setOpacity(DEFAULT_OPACITY);
  widgetWindow.on("ready-to-show", applyVisibility);
  widgetWindow.on("move", queueSaveBounds);
  widgetWindow.on("resize", queueSaveBounds);
  widgetWindow.on("close", saveBounds);
  widgetWindow.on("closed", () => {
    widgetWindow = null;
    displayed = false;
    pointerDrag = null;
    resizeDrag = null;
  });

  widgetWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  widgetWindow.webContents.on("will-navigate", (event, url) => {
    if (url !== `${activeOrigin}/widget.html`) {
      event.preventDefault();
      if (url.startsWith(`${activeOrigin}/`)) void shell.openExternal(url);
    }
  });
  widgetWindow.webContents.on("did-fail-load", (_event, errorCode, _description, validatedURL, isMainFrame) => {
    if (!isMainFrame || errorCode === -3 || !widgetWindow) return;
    applyVisibility();
    clearTimeout(retryTimer);
    retryTimer = setTimeout(() => void reloadWidget(), 5000);
    console.warn(`看板服务暂不可用，5 秒后重试：${validatedURL}`);
  });
  loadWidget();
}

async function reloadWidget() {
  if (!widgetWindow || widgetWindow.isDestroyed()) return;
  activeOrigin = await resolveOrigin();
  applyVisibility();
  loadWidget();
}

const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!widgetWindow) return;
    if (widgetWindow.isMinimized()) widgetWindow.restore();
    if (displayed) widgetWindow.showInactive();
  });

  app.whenReady().then(async () => {
    if (process.platform === "darwin" && !desktopAdapter) {
      throw new Error(`macOS 窗口层级适配未构建，请运行 npm run widget:build：${desktopAdapterLoadError?.message || "未知错误"}`);
    }
    activeOrigin = await resolveOrigin();
    pinnedOnTop = readSavedPinned();
    if (process.platform === "darwin" && app.dock) app.dock.hide();
    ipcMain.on("widget:drag-start", (event, point) => {
      if (!ownsSender(event) || !widgetWindow || !validPointer(point)) return;
      const [x, y] = widgetWindow.getPosition();
      pointerDrag = { windowX: x, windowY: y, pointerX: point.x, pointerY: point.y };
    });
    ipcMain.on("widget:drag-move", (event, point) => {
      if (!ownsSender(event) || !widgetWindow || !pointerDrag || !validPointer(point)) return;
      widgetWindow.setPosition(
        Math.round(pointerDrag.windowX + point.x - pointerDrag.pointerX),
        Math.round(pointerDrag.windowY + point.y - pointerDrag.pointerY),
      );
    });
    ipcMain.on("widget:drag-end", (event) => {
      if (!ownsSender(event)) return;
      pointerDrag = null;
      saveBounds();
    });
    ipcMain.on("widget:resize-start", (event, payload) => {
      if (!ownsSender(event) || !widgetWindow || !validPointer(payload)) return;
      const direction = String(payload.direction || "");
      if (!RESIZE_DIRECTIONS.has(direction)) return;
      resizeDrag = {
        direction,
        bounds: widgetWindow.getBounds(),
        pointerX: payload.x,
        pointerY: payload.y,
      };
    });
    ipcMain.on("widget:resize-move", (event, point) => {
      if (!ownsSender(event) || !widgetWindow || !resizeDrag || !validPointer(point)) return;
      const dx = point.x - resizeDrag.pointerX;
      const dy = point.y - resizeDrag.pointerY;
      const source = resizeDrag.bounds;
      const direction = resizeDrag.direction;
      let x = source.x;
      let y = source.y;
      let width = source.width;
      let height = source.height;
      if (direction.includes("e")) width = source.width + dx;
      if (direction.includes("s")) height = source.height + dy;
      if (direction.includes("w")) {
        width = source.width - dx;
        x = source.x + dx;
      }
      if (direction.includes("n")) {
        height = source.height - dy;
        y = source.y + dy;
      }
      if (width < MIN_BOUNDS.width) {
        if (direction.includes("w")) x = source.x + source.width - MIN_BOUNDS.width;
        width = MIN_BOUNDS.width;
      } else if (width > MAX_BOUNDS.width) {
        if (direction.includes("w")) x = source.x + source.width - MAX_BOUNDS.width;
        width = MAX_BOUNDS.width;
      }
      if (height < MIN_BOUNDS.height) {
        if (direction.includes("n")) y = source.y + source.height - MIN_BOUNDS.height;
        height = MIN_BOUNDS.height;
      } else if (height > MAX_BOUNDS.height) {
        if (direction.includes("n")) y = source.y + source.height - MAX_BOUNDS.height;
        height = MAX_BOUNDS.height;
      }
      widgetWindow.setBounds({
        x: Math.round(x),
        y: Math.round(y),
        width: Math.round(width),
        height: Math.round(height),
      });
    });
    ipcMain.on("widget:resize-end", (event) => {
      if (!ownsSender(event)) return;
      resizeDrag = null;
      saveBounds();
    });
    ipcMain.on("widget:set-pinned", (event, pinned) => {
      if (!ownsSender(event) || typeof pinned !== "boolean") return;
      pinnedOnTop = pinned;
      if (displayed) applyFloatingMode(pinnedOnTop);
      saveBounds();
    });
    ipcMain.on("widget:set-market-phase", (event, phase) => {
      if (!ownsSender(event) || typeof phase !== "string") return;
      marketPhase = phase.trim().toLowerCase();
      applyVisibility();
    });
    ipcMain.handle("widget:get-state", (event) => {
      if (!ownsSender(event)) throw new Error("无效组件窗口");
      return {
        visible: displayed,
        marketPhase,
        pinned: pinnedOnTop,
        windowLevel: nativeWindowLevel(),
        bounds: widgetWindow.getBounds(),
        opacity: widgetWindow.getOpacity(),
        origin: activeOrigin,
      };
    });
    ipcMain.handle("widget:set-opacity", (event, opacity) => {
      if (!ownsSender(event) || typeof opacity !== "number" || !Number.isFinite(opacity)) {
        throw new Error("无效透明度请求");
      }
      const normalized = Math.min(1, Math.max(0.35, opacity));
      widgetWindow.setOpacity(normalized);
      return widgetWindow.getOpacity();
    });
    ipcMain.handle("widget:open-dashboard", (event) => {
      if (!ownsSender(event)) throw new Error("无效组件窗口");
      return shell.openExternal(activeOrigin);
    });
    ipcMain.handle("widget:close", (event) => {
      if (!ownsSender(event)) throw new Error("无效组件窗口");
      widgetWindow.close();
    });
    createWidgetWindow();
    startVisibilityScheduler();
  }).catch((error) => {
    console.error(error);
    app.exit(1);
  });

  app.on("activate", () => {
    if (displayed) widgetWindow?.showInactive();
  });
  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", () => {
    clearTimeout(retryTimer);
    clearTimeout(saveBoundsTimer);
    clearInterval(visibilityTimer);
    saveBounds();
  });
}
