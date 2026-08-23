const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("jarvisAPI", {
  close:    ()        => ipcRenderer.send("window-close"),
  minimize: ()        => ipcRenderer.send("window-minimize"),
  pin:      (pinned)  => ipcRenderer.send("window-pin", pinned),
  getPos:   ()        => ipcRenderer.invoke("window-position"),
  move:     (x, y)   => ipcRenderer.send("window-move", { x, y }),
});
