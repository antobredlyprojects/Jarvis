#!/usr/bin/env python3
"""
=============================================================================
                  J.A.R.V.I.S. - Desktop Voice Assistant
=============================================================================
Architecture:
  ONE microphone  →  ONE sounddevice.InputStream
                 →  Silero VAD  (speech detection + barge-in)
                 →  SpeechRecognition (Google STT on captured segments)
                 →  Backend  (Groq AI)
                 →  TTS queue thread  (edge-tts + sounddevice.play)

Prerequisites:
  pip install torch torchaudio sounddevice soundfile edge-tts
  pip install SpeechRecognition requests numpy pyperclip
  pip install pyautogui psutil mss pillow
=============================================================================
"""

import os, sys, time, uuid, json, queue, asyncio
import tempfile, datetime, threading, subprocess, webbrowser
import numpy as np
import requests
import speech_recognition as sr
import edge_tts
import sounddevice as sd
import soundfile as sf
import torch
from app_launcher import launch_app

# ── Optional imports ──────────────────────────────────────────────────────────
try:    import pyperclip;  HAS_CLIPBOARD = True
except: HAS_CLIPBOARD = False
try:    import pyautogui;  pyautogui.FAILSAFE = True; HAS_PYAUTOGUI = True
except: HAS_PYAUTOGUI = False
try:    import psutil;     HAS_PSUTIL = True
except: HAS_PSUTIL = False
try:    import mss, mss.tools; HAS_MSS = True
except: HAS_MSS = False

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
SERVER_URL      = "http://localhost:3000"
TTS_VOICE       = "en-GB-RyanNeural"
TTS_RATE        = "-5%"           # slightly below normal — calm, composed
TTS_PITCH       = "-3Hz"           # slightly deeper — authoritative without booming

SAMPLE_RATE     = 16000          # Silero VAD requires 16kHz
BLOCK_SIZE      = 512            # ~32ms per block at 16kHz
VAD_THRESHOLD   = 0.5            # Silero confidence threshold (0-1)
SPEECH_PAD_MS   = 300            # ms of silence after speech before cutting
MIN_SPEECH_MS   = 400            # ignore segments shorter than this
MAX_SPEECH_SEC  = 15             # hard cap on a single utterance

# ═══════════════════════════════════════════════════════════════════════════════
#  SILERO VAD  — load once at startup
# ═══════════════════════════════════════════════════════════════════════════════
print("-> Loading Silero VAD model...")
_vad_model, _vad_utils = torch.hub.load(
    repo_or_dir = "snakers4/silero-vad",
    model       = "silero_vad",
    force_reload = False,
    onnx        = False,
)
_vad_model.eval()
_get_speech_ts = _vad_utils[0]

def vad_is_speech(chunk_np: np.ndarray) -> float:
    """Return Silero VAD confidence (0-1) for a 512-sample 16kHz mono chunk."""
    tensor = torch.from_numpy(chunk_np.astype(np.float32))
    with torch.no_grad():
        confidence = _vad_model(tensor, SAMPLE_RATE).item()
    return confidence

# ═══════════════════════════════════════════════════════════════════════════════
#  STATE MACHINE
# ═══════════════════════════════════════════════════════════════════════════════
# States:
#   LISTENING  — mic active, VAD watching, JARVIS silent
#   SPEAKING   — JARVIS TTS playing, mic paused (no recording to queue)
#   PROCESSING — STT / backend call in progress

class State:
    LISTENING  = "listening"
    SPEAKING   = "speaking"
    PROCESSING = "processing"

_state      = State.LISTENING
_state_lock = threading.Lock()

def set_state(s: str):
    global _state
    with _state_lock:
        _state = s

def get_state() -> str:
    with _state_lock:
        return _state

# ═══════════════════════════════════════════════════════════════════════════════
#  SHARED QUEUES
# ═══════════════════════════════════════════════════════════════════════════════
_speech_segment_queue : queue.Queue = queue.Queue()   # raw np arrays → STT thread
_tts_queue            : queue.Queue = queue.Queue()   # text → TTS thread
_interrupt_event      : threading.Event = threading.Event()

# ═══════════════════════════════════════════════════════════════════════════════
#  UI BRIDGE  (fire-and-forget, never blocks)
# ═══════════════════════════════════════════════════════════════════════════════
def ui_event(event_type: str, text: str = "", data=None):
    if data is None:
        data = {}
    def _post():
        try:
            requests.post(
                f"{SERVER_URL}/api/ui/event",
                json={"type": event_type, "text": text, "data": data},
                timeout=1,
            )
        except Exception:
            pass
    threading.Thread(target=_post, daemon=True).start()

def get_current_time():
    now = datetime.datetime.now()
    return now.strftime("%I:%M %p"), now.strftime("%A, %B %d %Y")

# ═══════════════════════════════════════════════════════════════════════════════
#  MIC THREAD  — ONE stream, Silero VAD on every block
#
#  Responsibilities:
#    1. When LISTENING: collect blocks, run VAD, accumulate speech segments,
#       push complete utterances to _speech_segment_queue
#    2. When SPEAKING:  run VAD — if user speaks → barge-in → interrupt TTS
#    3. When PROCESSING: discard audio (don't record while waiting for reply)
# ═══════════════════════════════════════════════════════════════════════════════

def mic_thread_fn():
    """Single microphone thread. Runs for the lifetime of the application."""

    pad_blocks    = int((SPEECH_PAD_MS / 1000) * SAMPLE_RATE / BLOCK_SIZE)
    max_blocks    = int(MAX_SPEECH_SEC * SAMPLE_RATE / BLOCK_SIZE)
    min_blocks    = int((MIN_SPEECH_MS / 1000) * SAMPLE_RATE / BLOCK_SIZE)

    # VAD state
    in_speech        = False
    silence_count    = 0
    speech_buffer    = []   # list of np arrays

    # Barge-in state  (used when SPEAKING)
    barge_consec     = 0
    BARGE_TRIGGER    = 4     # consecutive speech blocks to trigger barge-in

    def callback(indata, frames, time_info, status):
        nonlocal in_speech, silence_count, speech_buffer
        nonlocal barge_consec

        chunk = indata[:, 0].copy()   # mono
        state = get_state()

        # ── BARGE-IN (while JARVIS is speaking) ──────────────────────────────
        if state == State.SPEAKING:
            conf = vad_is_speech(chunk)
            if conf >= VAD_THRESHOLD:
                barge_consec += 1
                if barge_consec >= BARGE_TRIGGER:
                    print("[Barge-in] User interrupted JARVIS.")
                    _interrupt_event.set()
                    sd.stop()
                    barge_consec = 0
            else:
                barge_consec = max(0, barge_consec - 1)
            return   # don't accumulate speech while JARVIS talks

        barge_consec = 0   # reset when not speaking

        # ── DISCARD while PROCESSING ─────────────────────────────────────────
        if state == State.PROCESSING:
            return

        # ── ACCUMULATE SPEECH while LISTENING ────────────────────────────────
        conf = vad_is_speech(chunk)
        is_speech = conf >= VAD_THRESHOLD

        if is_speech:
            silence_count = 0
            if not in_speech:
                in_speech = True
                speech_buffer = []
            speech_buffer.append(chunk)

            # Hard cap — push immediately if too long
            if len(speech_buffer) >= max_blocks:
                audio = np.concatenate(speech_buffer)
                _speech_segment_queue.put(audio)
                speech_buffer = []
                in_speech = False

        else:
            if in_speech:
                silence_count += 1
                speech_buffer.append(chunk)   # include trailing silence (natural)
                if silence_count >= pad_blocks:
                    # End of utterance
                    if len(speech_buffer) >= min_blocks:
                        audio = np.concatenate(speech_buffer)
                        _speech_segment_queue.put(audio)
                    speech_buffer = []
                    in_speech = False
                    silence_count = 0

    with sd.InputStream(
        samplerate = SAMPLE_RATE,
        channels   = 1,
        dtype      = "float32",
        blocksize  = BLOCK_SIZE,
        callback   = callback,
    ):
        print("-> Microphone stream open (Silero VAD active).")
        while True:
            time.sleep(0.1)   # keep thread alive; callback drives everything

# ═══════════════════════════════════════════════════════════════════════════════
#  STT THREAD  — Google STT on each accumulated segment
# ═══════════════════════════════════════════════════════════════════════════════
_recognizer = sr.Recognizer()

def stt_thread_fn():
    """Takes raw np audio from _speech_segment_queue, transcribes, processes."""
    while True:
        audio_np = _speech_segment_queue.get()
        if audio_np is None:
            break

        state = get_state()
        if state != State.LISTENING:
            _speech_segment_queue.task_done()
            continue

        set_state(State.PROCESSING)
        ui_event("status", "Recognising speech...")

        # Convert numpy float32 → SpeechRecognition AudioData
        audio_int16 = (audio_np * 32767).astype(np.int16)
        audio_data  = sr.AudioData(audio_int16.tobytes(), SAMPLE_RATE, 2)

        try:
            query = _recognizer.recognize_google(audio_data)
        except sr.UnknownValueError:
            print("[STT] Could not understand.")
            ui_event("status", "Didn't catch that...")
            set_state(State.LISTENING)
            ui_event("listening")
            _speech_segment_queue.task_done()
            continue
        except sr.RequestError as e:
            print(f"[STT Error]: {e}")
            speak("Speech recognition is having trouble.")
            set_state(State.LISTENING)
            _speech_segment_queue.task_done()
            continue

        print(f"\nYou: {query}")
        ui_event("user", query)
        process_query(query)
        _speech_segment_queue.task_done()

# ═══════════════════════════════════════════════════════════════════════════════
#  TEXT NATURALIZER — makes TTS output sound human, not robotic
# ═══════════════════════════════════════════════════════════════════════════════
import re as _re

# Numbers 0-1000 → words for natural pronunciation
_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
         "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
         "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]

def _num_to_words(n: int) -> str:
    """Convert 0-999 to words."""
    if n < 20: return _ONES[n]
    if n < 100:
        rest = _ONES[n % 10]
        return _TENS[n // 10] + ("-" + rest if rest else "")
    h = _ONES[n // 100] + " hundred"
    rest = n % 100
    return h + (" and " + _num_to_words(rest) if rest else "")

# Abbreviations that sound weird when read literally
_ABBREVS = {
    "Dr.": "Doctor",
    "Mr.": "Mister",
    "Mrs.": "Missus",
    "Ms.": "Miss",
    "St.": "Saint",
    "vs.": "versus",
    "vs": "versus",
    "etc.": "etcetera",
    "i.e.": "that is",
    "e.g.": "for example",
    "a.m.": "a m",
    "p.m.": "p m",
    "AM": "a m",
    "PM": "p m",
}

# Symbols that sound weird spoken literally
_SYMBOLS = {
    "&": "and",
    "%": "percent",
    "@": "at",
    "+": "plus",
    "=": "equals",
    "#": "hash",
    "~": "tilde",
    "\\": "",
}

def naturalize_text(text: str) -> str:
    """Transform text so edge-tts sounds human, not robotic.
    
    What this does:
    - Expands numbers to words ("42" → "forty two")
    - Normalizes abbreviations ("Dr." → "Doctor")
    - Cleans URLs and emails into speakable form
    - Replaces symbols with words
    - Adds natural rhythm markers
    - Removes markdown/formatting artifacts
    """
    if not text:
        return text

    t = text

    # 1. Remove markdown/formatting artifacts
    t = t.replace("**", "").replace("*", "")
    t = t.replace("`", "")
    t = t.replace("#", "")
    t = t.replace("---", "")
    t = t.replace("---", "")
    t = _re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)  # [text](url) → text

    # 2. Normalize URLs → "the website example dot com"
    def _clean_url(m):
        url = m.group(0)
        # Strip protocol
        url = _re.sub(r"^https?://", "", url)
        # Strip trailing slashes/path
        url = url.split("/")[0]
        # Split by dots and join naturally
        parts = url.split(".")
        # Remove www
        parts = [p for p in parts if p.lower() not in ("www", "com", "org", "net", "io")]
        return " ".join(parts) + " dot com" if parts else url
    t = _re.sub(r"https?://[^\s]+", _clean_url, t)

    # 3. Normalize emails → "user at domain dot com"
    def _clean_email(m):
        user, domain = m.group(1), m.group(2)
        domain = domain.replace(".", " dot ")
        return f"{user} at {domain}"
    t = _re.sub(r"([\w.+-]+)@([\w.-]+)", _clean_email, t)

    # 4. Expand abbreviations
    for abbr, expanded in _ABBREVS.items():
        t = t.replace(abbr, expanded)

    # 5. Replace symbols
    for sym, word in _SYMBOLS.items():
        t = t.replace(sym, " " + word + " " if word else " ")

    # 6. Convert numbers to words (standalone numbers and ordinals)
    def _replace_num(m):
        num_str = m.group(0)
        # Handle ordinals: 1st, 2nd, 3rd, 4th...
        if m.group(1).lower() in ("st", "nd", "rd", "th"):
            base = int(num_str[:-2])
            return _num_to_words(base) + " " + m.group(1).lower()
        n = int(num_str)
        if 0 <= n <= 999:
            return _num_to_words(n)
        return num_str  # too large, leave as-is
    t = _re.sub(r"(\d+)(st|nd|rd|th)\b", _replace_num, t, flags=_re.IGNORECASE)
    t = _re.sub(r"\b(\d{1,3})\b", _replace_num, t)

    # 7. Normalize time references: "3:00 PM" → "three p m"
    def _clean_time(m):
        hour, minute, ampm = m.group(1), m.group(2), m.group(3)
        h = _num_to_words(int(hour))
        if minute and int(minute) > 0:
            return f"{h} {_num_to_words(int(minute))}"
        return h + " o'clock"
    t = _re.sub(r"(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)\b", _clean_time, t)

    # 8. Collapse multiple spaces and trim
    t = _re.sub(r"\s+", " ", t).strip()

    # 9. Ensure sentences end with proper punctuation for natural pauses
    t = _re.sub(r"([.!?])([A-Z])", r"\1 \2", t)  # ensure space after sentence end

    return t


# ═══════════════════════════════════════════════════════════════════════════════
#  NATURAL SPEECH ENGINE — chunked synthesis with human rhythm
# ═══════════════════════════════════════════════════════════════════════════════
_tts_loop: asyncio.AbstractEventLoop = None
def _split_into_sentences(text: str) -> list:
    """Split text into natural sentence chunks for TTS.
    
    Edge-TTS can sound flat on long sentences. Breaking into chunks
    with slight pauses between them creates natural breathing rhythm.
    """
    # Split on sentence boundaries, keeping the punctuation
    chunks = _re.split(r"(?<=[.!?])\s+", text)
    # Merge very short chunks (< 15 chars) with previous
    merged = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        if merged and len(chunk) < 15:
            merged[-1] += " " + chunk
        else:
            merged.append(chunk)
    return merged if merged else [text]

def _make_silence(duration_ms: int = 180, sr: int = None) -> np.ndarray:
    """Generate a short silence buffer for natural pauses between phrases."""
    if sr is None:
        sr = SAMPLE_RATE
    n_samples = int(sr * duration_ms / 1000)
    return np.zeros(n_samples, dtype=np.float32)

async def _synthesise_chunk(text: str) -> tuple:
    """Synthesize a single text chunk to numpy audio + sample rate."""
    tmp = os.path.join(tempfile.gettempdir(), f"jarvis_{uuid.uuid4().hex}.mp3")
    try:
        comm = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH)
        await comm.save(tmp)
        audio_data, sr = sf.read(tmp, dtype="float32")
        return audio_data, sr
    finally:
        try: os.remove(tmp)
        except OSError: pass

async def _synthesise_and_play(text: str):
    """Natural speech synthesis: chunk → synthesize → join with silence gaps.
    
    The key insight: human speech has natural pauses between sentences.
    A single long TTS pass sounds flat and robotic. By splitting into
    chunks, synthesizing each, and joining with silence gaps,
    the result sounds like a person actually breathing between thoughts.
    """
    # Naturalize the text first
    text = naturalize_text(text)

    # Split into sentence chunks
    chunks = _split_into_sentences(text)

    # Synthesize each chunk and concatenate with silence gaps
    all_audio = []
    detected_sr = None
    for i, chunk in enumerate(chunks):
        if _interrupt_event.is_set():
            return  # stop immediately if interrupted

        try:
            chunk_audio, sr = await _synthesise_chunk(chunk)
            all_audio.append(chunk_audio)
            if detected_sr is None:
                detected_sr = sr
        except Exception as e:
            print(f"[TTS] Chunk {i} failed: {e}")
            continue

        # Add a natural pause between sentences (not after the last)
        if i < len(chunks) - 1:
            # Longer pause after periods, shorter after commas
            pause_ms = 220 if chunk.rstrip().endswith((".", "!", "?")) else 120
            all_audio.append(_make_silence(pause_ms, detected_sr or SAMPLE_RATE))

    if not all_audio:
        print("[TTS] All chunks failed.")
        return

    if detected_sr is None:
        detected_sr = SAMPLE_RATE

    # Concatenate all chunks + silence into one audio stream
    full_audio = np.concatenate(all_audio)

    _interrupt_event.clear()
    set_state(State.SPEAKING)
    ui_event("status", "Speaking...")

    sd.play(full_audio, detected_sr)
    # Poll for interrupt instead of blocking with sd.wait()
    while sd.get_stream().active:
        if _interrupt_event.is_set():
            sd.stop()
            print("[TTS] Playback interrupted.")
            break
        time.sleep(0.02)

    _interrupt_event.clear()
    set_state(State.LISTENING)
    ui_event("listening")
    ui_event("status", "Listening...")

def tts_thread_fn():
    global _tts_loop
    _tts_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_tts_loop)
    while True:
        text = _tts_queue.get()
        if text is None:
            break
        _tts_loop.run_until_complete(_synthesise_and_play(text))
        _tts_queue.task_done()

def speak(text: str):
    """Non-blocking — queues text for TTS."""
    print(f"\n[JARVIS]: {text}")
    ui_event("jarvis", text)
    _tts_queue.put(text)

def speak_and_wait(text: str):
    """Queues text and waits until fully spoken."""
    speak(text)
    _tts_queue.join()

def interrupt_speech():
    """Stop current speech and clear the queue."""
    _interrupt_event.set()
    sd.stop()
    while not _tts_queue.empty():
        try:
            _tts_queue.get_nowait()
            _tts_queue.task_done()
        except queue.Empty:
            break

# ═══════════════════════════════════════════════════════════════════════════════
#  PC CONTROL COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════
def _ps(script: str, timeout: int = 5) -> str:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=timeout
    )
    return r.stdout.strip()


def cmd_open_app(p):
    return launch_app(p.get("app", ""))

def cmd_set_volume(p):
    try:
        level = max(0, min(100, int(p.get("level", 50))))
    except (ValueError, TypeError):
        level = 50
    steps = int(level / 2)
    _ps(f"$w=New-Object -ComObject WScript.Shell;1..50|%{{$w.SendKeys([char]174)}};1..{steps}|%{{$w.SendKeys([char]175)}}")
    return f"Volume set to {level} percent."

def cmd_mute(p):
    _ps("(New-Object -ComObject WScript.Shell).SendKeys([char]173)")
    return "Toggled mute."

def cmd_shutdown(p):
    try:
        d = max(10, min(3600, int(p.get("delay", 30))))
    except (ValueError, TypeError):
        d = 30
    subprocess.run(f"shutdown /s /t {d}", shell=True)
    return f"Shutting down in {d} seconds. Say cancel shutdown to abort."

def cmd_restart(p):
    try:
        d = max(10, min(3600, int(p.get("delay", 30))))
    except (ValueError, TypeError):
        d = 30
    subprocess.run(f"shutdown /r /t {d}", shell=True)
    return f"Restarting in {d} seconds."

def cmd_lock(p):
    subprocess.run("rundll32.exe user32.dll,LockWorkStation", shell=True)
    return "Workstation locked."

def cmd_screenshot(p):
    ts  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(os.path.expanduser("~"), "Desktop", f"screenshot_{ts}.png")
    if HAS_MSS:
        with mss.mss() as sct: sct.shot(output=out)
    else:
        try:
            from PIL import ImageGrab
            ImageGrab.grab().save(out)
        except:
            _ps(f"Add-Type -AssemblyName System.Windows.Forms;$b=New-Object System.Drawing.Bitmap([System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width,[System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height);$g=[System.Drawing.Graphics]::FromImage($b);$g.CopyFromScreen(0,0,0,0,$b.Size);$b.Save('{out}')", timeout=10)
    return "Screenshot saved to your desktop."

def cmd_mouse_click(p):
    if not HAS_PYAUTOGUI: return "pyautogui not installed."
    x,y,btn = p.get("x"), p.get("y"), p.get("button","left")
    if x is not None and y is not None: pyautogui.click(x,y,button=btn)
    else: pyautogui.click(button=btn)
    return "Clicked."

def cmd_mouse_move(p):
    if not HAS_PYAUTOGUI: return "pyautogui not installed."
    pyautogui.moveTo(p.get("x",0), p.get("y",0), duration=0.3)
    return f"Mouse moved."

def cmd_type_text(p):
    if not HAS_PYAUTOGUI: return "pyautogui not installed."
    pyautogui.write(p.get("text",""), interval=0.03)
    return "Typed."

def cmd_hotkey(p):
    if not HAS_PYAUTOGUI: return "pyautogui not installed."
    keys = p.get("keys",[])
    if keys: pyautogui.hotkey(*keys)
    return f"Pressed {' + '.join(keys)}."

def cmd_open_url(p):
    url = p.get("url","")
    if not url.startswith("http"): url = "https://" + url
    webbrowser.open(url)
    return f"Opening {url}."

def cmd_search_web(p):
    q = p.get("query","")
    webbrowser.open(f"https://www.google.com/search?q={requests.utils.quote(q)}")
    return f"Searching for {q}."

def cmd_clipboard_read(p):
    if not HAS_CLIPBOARD: return "pyperclip not installed."
    c = pyperclip.paste()
    return f"Clipboard: {c[:200]}" if c else "Clipboard is empty."

def cmd_clipboard_write(p):
    if not HAS_CLIPBOARD: return "pyperclip not installed."
    pyperclip.copy(p.get("text",""))
    return "Copied to clipboard."

def cmd_list_processes(p):
    if not HAS_PSUTIL: return "psutil not installed."
    procs = sorted(psutil.process_iter(["name","pid","cpu_percent"]),
                   key=lambda x: x.info["cpu_percent"] or 0, reverse=True)[:6]
    return "Top processes: " + ", ".join(f"{x.info['name']} ({x.info['pid']})" for x in procs)

def cmd_kill_process(p):
    if not HAS_PSUTIL: return "psutil not installed."
    name, killed = p.get("name",""), 0
    for proc in psutil.process_iter(["name","pid"]):
        if name.lower() in proc.info["name"].lower():
            try: proc.kill(); killed += 1
            except: pass
    return f"Killed {killed} process(es)." if killed else "No matching process."

def cmd_system_info(p):
    if not HAS_PSUTIL: return "psutil not installed."
    cpu  = psutil.cpu_percent(interval=0.5)
    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return f"CPU {cpu}%, RAM {mem.percent}% used, Disk {disk.percent}% used."

def cmd_toast(p):
    title = p.get("title","J.A.R.V.I.S.")
    msg   = p.get("message","")
    # Escape single quotes for PowerShell safety
    title = str(title).replace("'", "''")
    msg   = str(msg).replace("'", "''")
    _ps(f"Add-Type -AssemblyName System.Windows.Forms;$n=New-Object System.Windows.Forms.NotifyIcon;$n.Icon=[System.Drawing.SystemIcons]::Information;$n.BalloonTipTitle='{title}';$n.BalloonTipText='{msg}';$n.Visible=$true;$n.ShowBalloonTip(4000);Start-Sleep -Milliseconds 4500;$n.Dispose()", timeout=6)
    return "Notification sent."

def cmd_cancel_shutdown(p):
    subprocess.run("shutdown /a", shell=True, capture_output=True)
    return "Shutdown cancelled."

def cmd_add_alias(p):
    from app_launcher import add_alias
    return add_alias(p.get("alias", ""), p.get("target", ""))

def cmd_remove_alias(p):
    from app_launcher import remove_alias
    return remove_alias(p.get("alias", ""))

def cmd_refresh_apps(p):
    from app_launcher import refresh_index
    n = refresh_index(quiet=True)
    return f"Refreshed app index: {n} apps found."

def cmd_create_file(p):
    file_path = p.get("path", "")
    content = p.get("content", "")
    if not file_path:
        return "No file path specified."
    # Expand ~ to home directory
    file_path = os.path.expanduser(file_path)
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"File created: {file_path}"
    except Exception as e:
        return f"Failed to create file: {e}"

def cmd_search_files(p):
    pattern = p.get("pattern", "")
    root = p.get("root", "~/Documents")
    if not pattern:
        return "No search pattern specified."
    root = os.path.expanduser(root)
    if not os.path.isdir(root):
        return f"Directory not found: {root}"
    matches = []
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in filenames:
            if pattern.lower() in fn.lower():
                matches.append(os.path.join(dirpath, fn))
        if len(matches) >= 10:
            break
    if not matches:
        return f"No files matching '{pattern}' in {root}."
    result = "Found: " + ", ".join(os.path.basename(m) for m in matches[:10])
    if len(matches) > 10:
        result += f" (and {len(matches) - 10} more)"
    return result

COMMANDS = {
    "open_app":        cmd_open_app,
    "set_volume":      cmd_set_volume,
    "mute":            cmd_mute,
    "shutdown":        cmd_shutdown,
    "restart":         cmd_restart,
    "lock":            cmd_lock,
    "screenshot":      cmd_screenshot,
    "mouse_click":     cmd_mouse_click,
    "mouse_move":      cmd_mouse_move,
    "type_text":       cmd_type_text,
    "hotkey":          cmd_hotkey,
    "open_url":        cmd_open_url,
    "search_web":      cmd_search_web,
    "clipboard_read":  cmd_clipboard_read,
    "clipboard_write": cmd_clipboard_write,
    "list_processes":  cmd_list_processes,
    "kill_process":    cmd_kill_process,
    "system_info":     cmd_system_info,
    "toast":           cmd_toast,
    "cancel_shutdown": cmd_cancel_shutdown,
    "add_alias":       cmd_add_alias,
    "remove_alias":    cmd_remove_alias,
    "refresh_apps":    cmd_refresh_apps,
    "create_file":     cmd_create_file,
    "search_files":    cmd_search_files,
}

def handle_command(cmd: dict) -> str:
    fn = COMMANDS.get(cmd.get("command",""))
    if fn:
        try:    return fn(cmd.get("params", {}))
        except Exception as e: return f"Command error: {e}"
    return f"Unknown command: {cmd.get('command')}."

def try_parse_command(response: str):
    s = response.strip()
    start = s.find('{"action":"SYSTEM_COMMAND"')
    if start == -1: return None, None
    try:
        depth = end = 0
        for i, ch in enumerate(s[start:], start):
            if ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0: end = i; break
        cmd    = json.loads(s[start:end+1])
        prefix = s[:start].strip()
        return cmd, prefix or None
    except: return None, None

LOCAL_SHORTCUTS = {
    "cancel shutdown":      lambda: cmd_cancel_shutdown({}),
    "abort shutdown":       lambda: cmd_cancel_shutdown({}),
    "system info":          lambda: cmd_system_info({}),
    "read clipboard":       lambda: cmd_clipboard_read({}),
    "what's my clipboard":  lambda: cmd_clipboard_read({}),
    "take a screenshot":    lambda: cmd_screenshot({}),
    "take screenshot":      lambda: cmd_screenshot({}),
    "list processes":       lambda: cmd_list_processes({}),
}

# ═══════════════════════════════════════════════════════════════════════════════
#  QUERY PROCESSING
# ═══════════════════════════════════════════════════════════════════════════════
def process_query(query: str):
    """Called from STT thread. Sends to backend, speaks reply."""
    cleaned = query.strip().lower()

    # Exit
    if cleaned in {"exit", "quit", "shut down", "shutdown", "stop"}:
        speak_and_wait("Powering down. Have a good one.")
        os._exit(0)

    # Local shortcuts (no server round-trip)
    for phrase, fn in LOCAL_SHORTCUTS.items():
        if phrase in cleaned:
            result = fn()
            speak(result)
            set_state(State.LISTENING)
            ui_event("listening")
            return

    # Backend
    ui_event("thinking")
    ui_event("status", "Thinking...")
    current_time, current_date = get_current_time()

    try:
        r = requests.post(
            f"{SERVER_URL}/api/jarvis/voice-query",
            json={"prompt": query, "currentTime": current_time, "currentDate": current_date},
            timeout=45,
        )
        if r.status_code == 200:
            response_text = r.json().get("response", "No response.")
            cmd, prefix = try_parse_command(response_text)
            if cmd:
                if prefix: speak(prefix)
                ui_event("system_cmd", f"Executing: {cmd.get('command','')}", cmd)
                speak(handle_command(cmd))
            else:
                speak(response_text)
        else:
            speak(f"Server error — status {r.status_code}.")
    except requests.exceptions.ConnectionError:
        speak("Can't reach the server.")
    except requests.exceptions.Timeout:
        speak("The server took too long.")
    except Exception as e:
        print(f"[Request Error]: {e}")
        speak("There was a network issue.")

    set_state(State.LISTENING)
    ui_event("listening")
    ui_event("status", "Listening...")

# ═══════════════════════════════════════════════════════════════════════════════
#  PROACTIVE GREETING — time-aware, personality-driven
# ═══════════════════════════════════════════════════════════════════════════════
def _get_proactive_greeting() -> str:
    """Return a JARVIS-appropriate greeting based on time of day."""
    hour = datetime.datetime.now().hour
    name = "sir"
    # Try to get the user's name from memory
    try:
        mem_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "jarvis-server", "jarvis-server", "memory.json")
        if os.path.exists(mem_path):
            import json as _json
            with open(mem_path, "r") as f:
                mem = _json.load(f)
            for fact in mem.get("facts", []):
                if fact.get("key", "") == "username" and fact.get("value"):
                    name = fact["value"]
                    break
    except Exception:
        pass

    if 5 <= hour < 8:
        return f"Good morning, {name}. Up early today."
    elif 8 <= hour < 12:
        return f"Good morning, {name}. What are we working on today?"
    elif 12 <= hour < 14:
        return f"Afternoon, {name}. Hope you've eaten."
    elif 14 <= hour < 17:
        return f"Good afternoon, {name}. Systems are ready."
    elif 17 <= hour < 20:
        return f"Good evening, {name}. What can I do for you?"
    elif 20 <= hour < 23:
        return f"Evening, {name}. Still at it, I see."
    else:
        return f"Burning the midnight oil again, {name}? I'm here if you need me."


# ═══════════════════════════════════════════════════════════════════════════════
#  PROACTIVE MONITOR — watches for interesting events to comment on
# ═══════════════════════════════════════════════════════════════════════════════
_last_idle_check = time.time()
_idle_threshold = 1800  # 30 minutes of silence
_has_greeted_idle = False

def _proactive_monitor_fn():
    """Background thread that monitors system state and makes observations."""
    global _last_idle_check, _has_greeted_idle
    time.sleep(60)  # wait a minute before first check

    while True:
        try:
            now = time.time()
            state = get_state()

            # Only make proactive comments when LISTENING (not during speech/processing)
            if state == State.LISTENING:
                # Idle check — if user has been silent for a while
                if now - _last_idle_check > _idle_threshold and not _has_greeted_idle:
                    _has_greeted_idle = True
                    hour = datetime.datetime.now().hour
                    if 23 <= hour or hour < 5:
                        speak("Still here when you need me, sir.")
                    elif 12 <= hour < 14:
                        speak("Perhaps a break is in order, sir.")

            # Reset idle flag when user speaks
            if state == State.PROCESSING:
                _last_idle_check = now
                _has_greeted_idle = False

        except Exception:
            pass

        time.sleep(30)  # check every 30 seconds


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    global _last_idle_check
    print("=" * 60)
    print("      J.A.R.V.I.S.  —  Silero VAD Edition")
    print("=" * 60)

    # TTS thread
    tts_t = threading.Thread(target=tts_thread_fn, daemon=True, name="TTS")
    tts_t.start()

    # Mic thread (single stream, runs forever)
    mic_t = threading.Thread(target=mic_thread_fn, daemon=True, name="MIC")
    mic_t.start()

    # STT thread
    stt_t = threading.Thread(target=stt_thread_fn, daemon=True, name="STT")
    stt_t.start()

    # Proactive monitor thread
    monitor_t = threading.Thread(target=_proactive_monitor_fn, daemon=True, name="Monitor")
    monitor_t.start()

    print(f"\n-> Server   : {SERVER_URL}")
    print(f"-> Voice    : {TTS_VOICE}")
    print(f"-> VAD      : Silero (threshold={VAD_THRESHOLD})")
    print(f"-> Commands : {len(COMMANDS)} loaded")
    print("\nSay 'exit' or 'shut down' to stop.")
    print("=" * 60)

    # Pre-index apps in background (first launch_app will wait if not done)
    from app_launcher import refresh_index as _preindex
    threading.Thread(target=_preindex, args=(True,), daemon=True, name="AppIndex").start()

    # Time-aware greeting instead of generic "Systems online"
    _last_idle_check = time.time()  # reset idle timer on startup
    greeting = _get_proactive_greeting()
    speak_and_wait(greeting)
    ui_event("status", "Listening...")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        speak_and_wait("Interrupted. Shutting down.")

    _tts_queue.put(None)
    tts_t.join(timeout=2)


if __name__ == "__main__":
    main()