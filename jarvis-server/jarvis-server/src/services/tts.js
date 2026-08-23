const { spawn } = require("child_process");
const fs   = require("fs");
const path = require("path");
const os   = require("os");

const KOKORO_VOICE = process.env.KOKORO_VOICE || "bm_george";
const KOKORO_LANG  = process.env.KOKORO_LANG  || "b";       // British English
const KOKORO_RATE  = process.env.KOKORO_RATE  || "1.0";     // speech speed

// Path to the Python wrapper script (same directory as this file's parent)
const TTS_SCRIPT = path.join(__dirname, "..", "..", "tts_kokoro.py");

/**
 * Synthesise speech using Kokoro 82M (local, free).
 * Spawns a Python process that writes a WAV file, then reads it back.
 * Returns a Buffer of WAV audio data.
 */
function synthesise(text) {
  return new Promise((resolve, reject) => {
    const outPath = path.join(os.tmpdir(), `jarvis_tts_${Date.now()}_${Math.random().toString(36).slice(2)}.wav`);

    const args = [
      TTS_SCRIPT,
      "--output", outPath,
      "--voice", KOKORO_VOICE,
      "--lang", KOKORO_LANG,
      "--rate", KOKORO_RATE,
      text,
    ];

    // Try python3 first, fall back to python (for Windows compatibility)
    const pythonCmd = process.platform === "win32" ? "python" : "python3";
    const proc = spawn(pythonCmd, args);

    let stderr = "";
    proc.stderr.on("data", (d) => { stderr += d.toString(); });

    proc.on("close", (code) => {
      if (code !== 0) {
        fs.unlink(outPath, () => {}); // clean up
        return reject(new Error(`Kokoro TTS exited with code ${code}: ${stderr}`));
      }
      fs.readFile(outPath, (err, data) => {
        fs.unlink(outPath, () => {}); // clean up temp file
        if (err) return reject(err);
        resolve(data);
      });
    });

    proc.on("error", (err) => {
      reject(new Error(`Failed to spawn ${pythonCmd}: ${err.message}. Is Python installed and in PATH?`));
    });
  });
}

module.exports = { synthesise };
