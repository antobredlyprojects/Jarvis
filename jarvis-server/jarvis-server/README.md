# J.A.R.V.I.S. Mainframe — Backend Server

Express + Groq (Llama 3.3) backend for the JARVIS desktop assistant, with persistent memory, web search, and server-side text-to-speech.

## 🚀 Quick Start

### 1. Get a FREE Groq API Key
1. Go to **https://console.groq.com**
2. Sign up → API Keys → Create Key
3. No credit card required

### 2. Install Python TTS engine (for voice output)
```bash
pip install edge-tts --break-system-packages
```
This powers the `/api/jarvis/speak` endpoint, which the Electron app calls to generate spoken replies in a natural British voice (en-GB-RyanNeural). Make sure `python3` (or `python` on Windows) is on your PATH.

### 3. Configure the Server
```bash
# Clone / copy the server folder, then:
cd jarvis-server

# Copy the env template
cp .env.example .env

# Open .env and paste your key:
# GEMINI_API_KEY=AIza...your_key_here
```

### 3. Install & Run
```bash
npm install
npm start
```

The server starts at **http://localhost:3000**

---

## 📡 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Health check |
| `POST` | `/api/jarvis/voice-query` | Main voice query (used by desktop client) |
| `GET` | `/api/jarvis/status` | Check AI service status |
| `POST` | `/api/jarvis/reset` | Reset conversation history |

### Voice Query — Request / Response

**Request:**
```json
POST /api/jarvis/voice-query
Content-Type: application/json

{ "prompt": "What is the speed of light?" }
```

**Response:**
```json
{ "response": "The speed of light in a vacuum is approximately 299,792 kilometres per second, Sir." }
```

---

## 🔧 Connect the Desktop Client

In your `jarvis_client.py`, change the `SERVER_URL`:

```python
SERVER_URL = "http://localhost:3000"
```

Or if running on another machine, replace `localhost` with the server's IP address.

---

## 📁 Project Structure

```
jarvis-server/
├── index.js                  # Express app entry point
├── .env                      # Your API key (never commit this)
├── .env.example              # Template
└── src/
    ├── routes/
    │   └── jarvis.js         # /api/jarvis/* routes
    └── services/
        └── gemini.js         # Gemini AI + JARVIS personality
```

---

## 🌐 Deploy for Free (Optional)

To use the desktop client from anywhere (not just localhost):

- **Railway**: https://railway.app — connect GitHub repo, set `GEMINI_API_KEY` env var, deploy in 2 minutes
- **Render**: https://render.com — free tier, similar process

After deploying, update `SERVER_URL` in the desktop client with your live URL.
