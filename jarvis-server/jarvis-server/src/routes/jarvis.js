const express = require("express");
const multer  = require("multer");
const { queryJarvis, queryJarvisStream, resetConversation } = require("../services/groq");
const { loadMemory, saveMemory }         = require("../services/memory");
const { synthesise }                     = require("../services/tts");
const { transcribe }                     = require("../services/whisper");

const router = express.Router();
const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: 15 * 1024 * 1024 } });

// ── POST /api/jarvis/voice-query ─────────────────────────────────────────────
// Body: { "prompt": "...", "currentTime": "14:32", "currentDate": "Saturday, June 28 2026" }
router.post("/voice-query", async (req, res) => {
  const { prompt, currentTime, currentDate } = req.body;

  if (!prompt || typeof prompt !== "string" || prompt.trim() === "") {
    return res.status(400).json({ error: "Missing or invalid 'prompt'." });
  }

  console.log(`\n[QUERY] ${prompt}`);

  try {
    const response = await queryJarvis(prompt.trim(), { currentTime, currentDate });
    console.log(`[JARVIS] ${response}`);
    return res.json({ response });
  } catch (err) {
    console.error("[Groq Error]", err.message);
    if (err.message.includes("GROQ_API_KEY")) {
      return res.status(503).json({ response: "The API key hasn't been configured." });
    }
    return res.status(502).json({ response: "I hit a technical snag — give me a moment." });
  }
});

// ── POST /api/jarvis/voice-query-stream ─────────────────────────────────────
// Streams sentences as NDJSON for low-latency TTS
router.post("/voice-query-stream", async (req, res) => {
  const { prompt, currentTime, currentDate } = req.body;

  if (!prompt || typeof prompt !== "string" || prompt.trim() === "") {
    return res.status(400).json({ error: "Missing or invalid 'prompt'." });
  }

  console.log(`\n[QUERY-STREAM] ${prompt}`);

  res.set({
    "Content-Type":  "application/x-ndjson",
    "Cache-Control": "no-cache",
    "Connection":    "keep-alive",
  });

  try {
    for await (const chunk of queryJarvisStream(prompt.trim(), { currentTime, currentDate })) {
      res.write(JSON.stringify(chunk) + "\n");
    }
    res.end();
  } catch (err) {
    console.error("[Groq Stream Error]", err.message);
    res.write(JSON.stringify({ type: "sentence", text: "I hit a technical snag — give me a moment." }) + "\n");
    res.write(JSON.stringify({ type: "done" }) + "\n");
    res.end();
  }
});

// ── POST /api/jarvis/reset ───────────────────────────────────────────────────
router.post("/reset", (req, res) => {
  resetConversation();
  res.json({ message: "Conversation context reset. Long-term memory intact." });
});

// ── GET /api/jarvis/memory ───────────────────────────────────────────────────
router.get("/memory", (req, res) => res.json(loadMemory()));

// ── DELETE /api/jarvis/memory ────────────────────────────────────────────────
router.delete("/memory", (req, res) => {
  saveMemory({ facts: [] });
  res.json({ message: "Long-term memory wiped." });
});

// ── DELETE /api/jarvis/memory/:key ──────────────────────────────────────────
router.delete("/memory/:key", (req, res) => {
  const memory = loadMemory();
  const before = memory.facts.length;
  memory.facts = memory.facts.filter(
    f => f.key.toLowerCase() !== req.params.key.toLowerCase()
  );
  if (memory.facts.length === before) {
    return res.status(404).json({ error: `No memory with key: ${req.params.key}` });
  }
  saveMemory(memory);
  res.json({ message: `Memory "${req.params.key}" deleted.` });
});

// ── POST /api/jarvis/transcribe ──────────────────────────────────────────────
// Multipart form upload: field "audio" = recorded chunk (webm/ogg/wav)
// Returns: { "text": "transcribed speech" }
router.post("/transcribe", upload.single("audio"), async (req, res) => {
  if (!req.file) {
    return res.status(400).json({ error: "No audio file received." });
  }

  try {
    const text = await transcribe(req.file.buffer, req.file.mimetype);
    return res.json({ text });
  } catch (err) {
    console.error("[Whisper Error]", err.message);
    return res.status(502).json({ error: "Transcription failed.", detail: err.message });
  }
});

// ── POST /api/jarvis/speak ───────────────────────────────────────────────────
// Body: { "text": "..." }  →  Returns raw audio/mpeg bytes
router.post("/speak", async (req, res) => {
  const { text } = req.body;
  if (!text || typeof text !== "string" || text.trim() === "") {
    return res.status(400).json({ error: "Missing or invalid 'text'." });
  }

  try {
    const audioBuffer = await synthesise(text.trim());
    res.set("Content-Type", "audio/mpeg");
    res.set("Content-Length", audioBuffer.length);
    return res.send(audioBuffer);
  } catch (err) {
    console.error("[TTS Error]", err.message);
    return res.status(502).json({ error: "TTS synthesis failed.", detail: err.message });
  }
});

// ── GET /api/jarvis/status ───────────────────────────────────────────────────
router.get("/status", (req, res) => {
  const memory = loadMemory();
  res.json({
    jarvis: "online",
    provider: "Groq",
    groq_api_key_configured: !!process.env.GROQ_API_KEY,
    model: process.env.GROQ_MODEL || "openai/gpt-oss-120b",
    features: ["web_search", "system_commands", "time_awareness", "persistent_memory"],
    long_term_memory_facts: memory.facts.length,
    uptime_seconds: Math.floor(process.uptime()),
  });
});

module.exports = router;
