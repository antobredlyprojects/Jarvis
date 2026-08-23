const { WebSocketServer } = require("ws");

let wss = null;
const clients = new Set();

/**
 * Start the WebSocket server on a given port.
 * Electron connects here to receive real-time UI events from Python.
 */
function startBridge(port = 8765) {
  wss = new WebSocketServer({ port });

  wss.on("connection", (ws) => {
    clients.add(ws);
    console.log(`[UI Bridge] Electron connected. Total clients: ${clients.size}`);

    ws.on("close", () => {
      clients.delete(ws);
      console.log(`[UI Bridge] Client disconnected. Total: ${clients.size}`);
    });

    ws.on("error", (err) => {
      console.error("[UI Bridge] WebSocket error:", err.message);
      clients.delete(ws);
    });
  });

  console.log(`[UI Bridge] WebSocket server listening on ws://localhost:${port}`);
}

/**
 * Broadcast an event to all connected Electron windows.
 * event: { type: "user"|"jarvis"|"status"|"listening", text: "..." }
 */
function broadcast(event) {
  if (!clients.size) return;
  const msg = JSON.stringify(event);
  for (const ws of clients) {
    try { ws.send(msg); } catch (e) { clients.delete(ws); }
  }
}

module.exports = { startBridge, broadcast };
