# J.A.R.V.I.S.

A desktop voice assistant that actually sounds like a person. Speaks with a British accent, has opinions, and can control your entire PC — all running locally for free.

## How It Works

Three services talk to each other:

```
You speak
  → Mic picks it up (Silero VAD detects speech)
  → Google STT transcribes it (free, no API key)
  → Groq AI thinks about it (streaming — starts answering before it's done thinking)
  → Kokoro 82M speaks it back (local neural TTS, British male voice)
  → You hear JARVIS respond within ~1 second
```

JARVIS can also **control your PC** — open/close/minimize/maximize apps, manage windows, volume, screenshots, keyboard shortcuts, file operations, and chain multiple commands together ("open browser and play Ed Sheeran on YouTube").

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/antobredlyprojects/Jarvis.git
cd Jarvis
```

### 2. Server (Node.js)

```bash
cd jarvis-server/jarvis-server
npm install

# Copy the env template and add your Groq API key
cp .env.example .env
# Edit .env — you need a GROQ_API_KEY (free at https://console.groq.com)

npm start
```

Server runs on `http://localhost:3000`.

### 3. Voice Core (Python)

```bash
# From the project root
pip install -r requirements.txt

# First run downloads Silero VAD model (~40MB)
python jarvis_desktop.py
```

### 4. Electron HUD (optional)

```bash
cd jarvis-electron/jarvis-electron
npm install
npm start
```

A transparent overlay appears on your desktop showing the conversation.

## What JARVIS Can Do

**Voice interaction**
- Always listening via Silero VAD (no wake word needed)
- Barge-in: talk over JARVIS to interrupt him
- Streaming TTS: he starts speaking within ~1 second

**PC control** (30+ commands)
- `open_app` / `close_app` / `focus_app` / `minimize_app` / `maximize_app`
- `set_volume` / `mute`
- `screenshot` / `lock` / `shutdown` / `restart`
- `mouse_click` / `mouse_move` / `type_text` / `hotkey`
- `search_web` / `open_url`
- Multi-step: "open browser and play Ed Sheeran on YouTube" → 5 sequential commands

**Personality**
- Has opinions ("VS Code, sir? I would have suggested IntelliJ")
- Pushes back ("I'd advise against it, but I'm not the one clicking Install")
- Reads your mood from how you talk
- References what he knows about you naturally

**Memory**
- Extracts facts from conversations and remembers them
- Recalls your name, preferences, location across sessions

## Tech Stack

| Layer | What | Why |
|---|---|---|
| Voice I/O | Python + sounddevice + Silero VAD | Real-time mic capture, speech detection |
| Speech-to-Text | Google STT | Free, no API key, good accuracy |
| Text-to-Speech | Kokoro 82M (local) | Free, no internet needed, British male voice |
| AI Brain | Groq API + Gemini fallback | Fast inference, multi-key rotation for rate limits |
| Server | Node.js + Express | API, streaming, memory system |
| GUI | Electron | Transparent desktop overlay |
| PC Control | PowerShell + Win32 API | Direct window management, no C extensions |

## Configuration

All config lives in `jarvis-server/jarvis-server/.env`:

```env
GROQ_API_KEY=your_key_here        # Free at console.groq.com
GROQ_MODEL=openai/gpt-oss-120b    # Or llama-3.3-70b-versatile
KOKORO_VOICE=bm_george            # British male (bf_alice for female)
KOKORO_RATE=1.0                   # Speech speed multiplier
```

Voice core settings are at the top of `jarvis_desktop.py` — sample rate, VAD threshold, barge-in sensitivity, etc.

## Project Structure

```
Jarvis/
├── jarvis_desktop.py          # Voice core — mic, VAD, STT, TTS, PC commands
├── app_launcher.py            # App indexing and launch logic
├── app_manager.py             # App database management
├── aliases.json               # Custom app aliases
├── requirements.txt           # Python dependencies
├── jarvis-server/
│   └── jarvis-server/
│       ├── index.js           # Express server entry point
│       ├── .env               # API keys and config (not in git)
│       ├── .env.example       # Config template
│       ├── tts_kokoro.py      # Server-side TTS wrapper
│       ├── memory.json        # Extracted conversation facts
│       └── src/
│           ├── routes/
│           │   └── jarvis.js  # API endpoints
│           └── services/
│               ├── groq.js    # LLM brain + streaming + memory extraction
│               ├── memory.js  # Fact extraction and storage
│               ├── tts.js     # Server-side TTS
│               ├── whisper.js # Audio transcription
│               ├── gemini.js  # Fallback LLM
│               └── uiBridge.js # WebSocket for real-time UI
└── jarvis-electron/
    └── jarvis-electron/
        ├── main.js            # Electron main process
        ├── preload.js         # IPC bridge
        └── renderer/
            └── index.html     # HUD overlay (chat bubble + controls)
```

## License

Personal project by [Anto Bredly](https://github.com/antobredlyprojects).
