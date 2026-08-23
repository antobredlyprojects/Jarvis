const Groq = require("groq-sdk");
const fs   = require("fs");
const os   = require("os");
const path = require("path");

let client = null;

function initClient() {
  // Use the first available key from the multi-key system, or fall back to GROQ_API_KEY
  const apiKey = process.env.GROQ_API_KEY1 || process.env.GROQ_API_KEY
    || [process.env.GROQ_API_KEY2, process.env.GROQ_API_KEY3,
        process.env.GROQ_API_KEY4, process.env.GROQ_API_KEY5,
        process.env.GROQ_API_KEY6].find(k => k && k.trim());
  if (!apiKey) {
    throw new Error("No Groq API key found for Whisper transcription.");
  }
  client = new Groq({ apiKey });
}

/**
 * Transcribe an audio buffer using Groq's Whisper endpoint.
 * Groq free tier: whisper-large-v3-turbo is fast and free.
 * @param {Buffer} audioBuffer - raw audio bytes (webm/ogg/wav/mp3 etc)
 * @param {string} mimeType - e.g. "audio/webm"
 * @returns {Promise<string>} transcribed text
 */
async function transcribe(audioBuffer, mimeType = "audio/webm") {
  if (!client) initClient();

  // Groq SDK needs a file-like object — write to a temp file first
  const ext = mimeType.includes("webm") ? "webm"
            : mimeType.includes("ogg")  ? "ogg"
            : mimeType.includes("wav")  ? "wav"
            : "mp3";

  const tmpPath = path.join(os.tmpdir(), `jarvis_stt_${Date.now()}.${ext}`);
  fs.writeFileSync(tmpPath, audioBuffer);

  try {
    const transcription = await client.audio.transcriptions.create({
      file: fs.createReadStream(tmpPath),
      model: process.env.WHISPER_MODEL || "whisper-large-v3-turbo",
      language: "en",
      response_format: "json",
    });
    return (transcription.text || "").trim();
  } finally {
    fs.unlink(tmpPath, () => {});
  }
}

module.exports = { transcribe };
