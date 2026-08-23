const { spawn } = require("child_process");
const fs   = require("fs");
const path = require("path");
const os   = require("os");

const TTS_VOICE = process.env.TTS_VOICE || "en-GB-RyanNeural";
const TTS_RATE  = process.env.TTS_RATE  || "-10%";
const TTS_PITCH = process.env.TTS_PITCH || "-5Hz";

/**
 * Synthesise speech using the edge-tts Python CLI.
 * Requires: pip install edge-tts --break-system-packages
 * Returns a Buffer of MP3 audio data.
 */
function synthesise(text) {
  return new Promise((resolve, reject) => {
    const outPath = path.join(os.tmpdir(), `jarvis_tts_${Date.now()}_${Math.random().toString(36).slice(2)}.mp3`);

    const args = [
      "-m", "edge_tts",
      "--voice", TTS_VOICE,
      `--rate=${TTS_RATE}`,
      `--pitch=${TTS_PITCH}`,
      "--text", text,
      "--write-media", outPath,
    ];

    // Try python3 first, fall back to python (for Windows compatibility)
    const pythonCmd = process.platform === "win32" ? "python" : "python3";
    const proc = spawn(pythonCmd, args);

    let stderr = "";
    proc.stderr.on("data", (d) => { stderr += d.toString(); });

    proc.on("close", (code) => {
      if (code !== 0) {
        return reject(new Error(`edge-tts exited with code ${code}: ${stderr}`));
      }
      fs.readFile(outPath, (err, data) => {
        // Clean up temp file regardless of outcome
        fs.unlink(outPath, () => {});
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
