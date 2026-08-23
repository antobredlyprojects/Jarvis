const { app, BrowserWindow, ipcMain, screen, Tray, Menu, nativeImage, session } = require("electron");
const path = require("path");

// Prevent crashes from killing the app silently
process.on("uncaughtException", (err) => {
  console.error("[JARVIS] Uncaught exception:", err.message);
});
process.on("unhandledRejection", (reason) => {
  console.error("[JARVIS] Unhandled rejection:", reason);
});

let mainWindow;
let tray;

function createWindow() {
  const { width, height } = screen.getPrimaryDisplay().workAreaSize;

  mainWindow = new BrowserWindow({
    width: Math.min(1280, width),
    height: Math.min(780, height),
    x: Math.floor((width - Math.min(1280, width)) / 2),
    y: Math.floor((height - Math.min(780, height)) / 2),

    // Frameless floating overlay
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: true,
    hasShadow: false,
    skipTaskbar: false,

    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, "preload.js"),
    },
  });

  mainWindow.loadFile("renderer/index.html");

  // Remove default menu bar
  mainWindow.setMenuBarVisibility(false);
}

// ── Microphone permission ────────────────────────────────────────────────────
// Electron blocks getUserMedia by default — auto-grant for our own local app
function setupPermissions() {
  session.defaultSession.setPermissionRequestHandler((webContents, permission, callback) => {
    if (permission === "media") {
      callback(true); // allow microphone access for speech recognition
    } else {
      callback(false);
    }
  });
}

// ── Tray icon ─────────────────────────────────────────────────────────────────
function createTray() {
  // Create a simple 16x16 cyan dot as tray icon (no image file needed)
  const icon = nativeImage.createEmpty();
  tray = new Tray(icon);

  const menu = Menu.buildFromTemplate([
    { label: "J.A.R.V.I.S.", enabled: false },
    { type: "separator" },
    { label: "Show / Hide", click: () => {
      if (mainWindow.isVisible()) mainWindow.hide();
      else mainWindow.show();
    }},
    { label: "Always on Top", type: "checkbox", checked: true, click: (item) => {
      mainWindow.setAlwaysOnTop(item.checked);
    }},
    { type: "separator" },
    { label: "Quit", click: () => app.quit() },
  ]);

  tray.setToolTip("J.A.R.V.I.S.");
  tray.setContextMenu(menu);
  tray.on("click", () => {
    if (mainWindow.isVisible()) mainWindow.focus();
    else mainWindow.show();
  });
}

// ── IPC — window controls from renderer ──────────────────────────────────────
ipcMain.on("window-close",    () => app.quit());
ipcMain.on("window-minimize", () => mainWindow.minimize());
ipcMain.on("window-pin",      (_, pinned) => mainWindow.setAlwaysOnTop(pinned));
ipcMain.on("window-move",     (_, { x, y }) => mainWindow.setPosition(x, y));
ipcMain.handle("window-position", () => mainWindow.getPosition());

// ── App lifecycle ─────────────────────────────────────────────────────────────
app.whenReady().then(() => {
  setupPermissions();
  createWindow();
  createTray();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
