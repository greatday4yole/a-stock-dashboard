const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("desktopWidget", Object.freeze({
  getState: () => ipcRenderer.invoke("widget:get-state"),
  startDrag: (x, y) => ipcRenderer.send("widget:drag-start", { x: Number(x), y: Number(y) }),
  dragTo: (x, y) => ipcRenderer.send("widget:drag-move", { x: Number(x), y: Number(y) }),
  endDrag: () => ipcRenderer.send("widget:drag-end"),
  startResize: (direction, x, y) => ipcRenderer.send("widget:resize-start", {
    direction: String(direction || ""),
    x: Number(x),
    y: Number(y),
  }),
  resizeTo: (x, y) => ipcRenderer.send("widget:resize-move", { x: Number(x), y: Number(y) }),
  endResize: () => ipcRenderer.send("widget:resize-end"),
  setPinned: (pinned) => ipcRenderer.send("widget:set-pinned", Boolean(pinned)),
  setMarketPhase: (phase) => ipcRenderer.send("widget:set-market-phase", String(phase || "")),
  setOpacity: (opacity) => ipcRenderer.invoke("widget:set-opacity", Number(opacity)),
  openDashboard: () => ipcRenderer.invoke("widget:open-dashboard"),
  close: () => ipcRenderer.invoke("widget:close"),
}));
