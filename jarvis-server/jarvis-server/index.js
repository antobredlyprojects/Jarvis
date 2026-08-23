require("dotenv").config({ override: true });
const express = require("express");
const cors    = require("cors");
const helmet  = require("helmet");
const morgan  = require("morgan");
const { startBridge, broadcast } = require("./src/services/uiBridge");
const jarvisRouter = require("./src/routes/jarvis");

const app  = express();
const PORT = process.env.PORT || 3000;
const WS_PORT = process.env.WS_PORT || 8765;

// ── Middleware ──────────────────────────────────────────────────────────────
app.use(helmet({ contentSecurityPolicy: false }));
app.use(cors());
app.use(express.json());
app.use(morgan("dev"));

// ── Routes ──────────────────────────────────────────────────────────────────
app.use("/api/jarvis", jarvisRouter);

// ── UI Bridge HTTP endpoint ─────────────────────────────────────────────────
// Python posts events here → we broadcast to all Electron windows via WS
// event types: "listening" | "user" | "thinking" | "jarvis" | "status" | "system_cmd"
app.post("/api/ui/event", (req, res) => {
  const { type, text, data } = req.body;
  if (!type) return res.status(400).json({ error: "Missing event type." });
  broadcast({ type, text: text || "", data: data || {} });
  res.json({ ok: true });
});

// Health check
app.get("/", (req, res) => {
  res.json({
    status: "online",
    system: "J.A.R.V.I.S. Mainframe",
    version: "1.0.0",
    timestamp: new Date().toISOString(),
  });
});

// 404
app.use((req, res) => res.status(404).json({ error: "Endpoint not found." }));

// Error handler
app.use((err, req, res, next) => {
  console.error("[ERROR]", err.message);
  res.status(500).json({ error: "Internal server error.", detail: err.message });
});

// ── Start ────────────────────────────────────────────────────────────────────
app.listen(PORT, () => {
  console.log("=".repeat(60));
  console.log("      J.A.R.V.I.S. Mainframe — Online");
  console.log("=".repeat(60));
  console.log(`  HTTP    : http://localhost:${PORT}`);
  console.log(`  WS      : ws://localhost:${WS_PORT}  (Electron UI)`);
  console.log(`  Provider: Groq (free tier)`);
  console.log(`  Model   : ${process.env.GROQ_MODEL || "openai/gpt-oss-120b"}`);
  console.log("=".repeat(60));
});

// Start WebSocket bridge (Electron subscribes here)
startBridge(WS_PORT);
