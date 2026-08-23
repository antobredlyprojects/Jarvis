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
                 →  TTS queue thread  (Kokoro 82M + sounddevice.play)

Prerequisites:
  pip install torch torchaudio sounddevice soundfile kokoro
  pip install SpeechRecognition requests numpy pyperclip
  pip install pyautogui psutil mss pillow
=============================================================================
"""

import os, sys, time, uuid, json, queue
import tempfile, datetime, threading, subprocess, webbrowser
import numpy as np
import requests
import speech_recognition as sr
import sounddevice as sd
import soundfile as sf
import torch
from kokoro import KPipeline
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
KOKORO_VOICE    = "bm_george"     # British male — calm, composed, JARVIS-like
KOKORO_LANG     = "b"             # 'b' = British English
KOKORO_RATE     = 1.0             # speech speed multiplier (1.0 = normal)
KOKORO_SAMPLE   = 24000           # Kokoro outputs at 24kHz

SAMPLE_RATE     = 16000          # Silero VAD requires 16kHz
BLOCK_SIZE      = 512            # ~32ms per block at 16kHz
VAD_THRESHOLD   = 0.5            # Silero confidence threshold (0-1)
SPEECH_PAD_MS   = 300            # ms of silence after speech before cutting
MIN_SPEECH_MS   = 400            # ignore segments shorter than this
MAX_SPEECH_SEC  = 15             # hard cap on a single utterance

# ═══════════════════════════════════════════════════════════════════════════════
#  KOKORO TTS  — load pipeline once at startup
# ═══════════════════════════════════════════════════════════════════════════════
print("-> Loading Kokoro TTS pipeline...")
_kokoro_pipeline = KPipeline(lang_code=KOKORO_LANG)
print(f"   Voice: {KOKORO_VOICE} | Lang: British English | Rate: {KOKORO_RATE}x")

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
#  KOKORO TTS ENGINE — streaming synthesis, sentence by sentence
# ═══════════════════════════════════════════════════════════════════════════════
_speech_queue : queue.Queue = queue.Queue()   # complete sentences → TTS worker

def _synthesise_sentence(text: str) -> np.ndarray:
    """Synthesize a single sentence to numpy audio via Kokoro."""
    text = naturalize_text(text)
    all_audio = []
    for _, _, audio in _kokoro_pipeline(text, voice=KOKORO_VOICE, speed=KOKORO_RATE):
        if audio is not None and len(audio) > 0:
            all_audio.append(audio)
    if not all_audio:
        return np.array([], dtype=np.float32)
    return np.concatenate(all_audio)

def _tts_worker_fn():
    """Background worker: picks sentences from queue, synthesizes, plays immediately.
    
    This is the key to low-latency speech. As soon as the first sentence
    arrives from the streaming response, it gets synthesized and played
    while the rest of the response is still being generated.
    """
    while True:
        text = _speech_queue.get()
        if text is None:
            break
        try:
            audio = _synthesise_sentence(text)
            if len(audio) == 0:
                continue

            _interrupt_event.clear()
            set_state(State.SPEAKING)
            ui_event("status", "Speaking...")

            sd.play(audio, KOKORO_SAMPLE)
            while sd.get_stream().active:
                if _interrupt_event.is_set():
                    sd.stop()
                    break
                time.sleep(0.02)
        except Exception as e:
            print(f"[TTS] Synthesis failed: {e}")
        finally:
            _speech_queue.task_done()

    # When queue is done, return to listening
    _interrupt_event.clear()
    set_state(State.LISTENING)
    ui_event("listening")
    ui_event("status", "Listening...")

def speak(text: str):
    """Non-blocking — queues a sentence for TTS."""
    print(f"\n[JARVIS]: {text}")
    ui_event("jarvis", text)
    _speech_queue.put(text)

def speak_and_wait(text: str):
    """Queues text and waits until fully spoken."""
    speak(text)
    _speech_queue.join()

def interrupt_speech():
    """Stop current speech and clear the queue."""
    _interrupt_event.set()
    sd.stop()
    while not _speech_queue.empty():
        try:
            _speech_queue.get_nowait()
            _speech_queue.task_done()
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

# ── Window Management Commands ─────────────────────────────────────────────
def _find_windows_by_name(name: str) -> list:
    """Find window handles matching an app name using PowerShell/.NET."""
    if not HAS_PSUTIL:
        return []
    # Find the process ID(s) by name
    pids = []
    name_lower = name.lower()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if name_lower in (proc.info["name"] or "").lower():
                pids.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not pids:
        return []
    # Use PowerShell to find windows belonging to those PIDs
    pid_list = ",".join(str(p) for p in pids)
    script = f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class WinAPI {{
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
}}
"@
$pids = @({pid_list})
$results = @()
$callback = [WinAPI+EnumWindowsProc]{{
    param($hWnd, $lParam)
    if ([WinAPI]::IsWindowVisible($hWnd)) {{
        $pid = 0
        [WinAPI]::GetWindowThreadProcessId($hWnd, [ref]$pid) | Out-Null
        if ($pids -contains $pid) {{
            $sb = New-Object System.Text.StringBuilder 256
            [WinAPI]::GetWindowText($hWnd, $sb, 256) | Out-Null
            $title = $sb.ToString()
            if ($title) {{ $results += $title }}
        }}
    }}
    return $true
}}
[WinAPI]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
$results | ConvertTo-Json -Compress
"""
    out = _ps(script)
    if not out or out == "[]":
        return []
    try:
        data = json.loads(out)
        if isinstance(data, str): data = [data]
        return data
    except:
        return []

def cmd_close_app(p):
    """Close an application by name."""
    name = p.get("app", "")
    if not name:
        return "Which app should I close?"
    if not HAS_PSUTIL:
        return "psutil not installed."
    killed = 0
    name_lower = name.lower()
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            if name_lower in (proc.info["name"] or "").lower():
                proc.terminate()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if killed:
        return f"Closed {name}."
    return f"No running process found for {name}."

def cmd_minimize_app(p):
    """Minimize an application window."""
    name = p.get("app", "")
    if not name:
        return "Which app should I minimize?"
    script = f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinAPI {{
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
}}
"@
$target = "{name.lower()}"
$pids = @()
Get-Process | Where-Object {{ $_.ProcessName -like "*$target*" }} | ForEach-Object {{ $pids += $_.Id }}
if ($pids.Count -eq 0) {{ Write-Output "NOT_FOUND"; exit }}
$found = $false
$callback = [WinAPI+EnumWindowsProc]{{
    param($hWnd, $lParam)
    if ([WinAPI]::IsWindowVisible($hWnd)) {{
        $pid = 0
        [WinAPI]::GetWindowThreadProcessId($hWnd, [ref]$pid) | Out-Null
        if ($pids -contains $pid) {{
            [WinAPI]::ShowWindow($hWnd, 6) | Out-Null  # SW_MINIMIZE = 6
            $found = $true
        }}
    }}
    return $true
}}
[WinAPI]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
if ($found) {{ Write-Output "OK" }} else {{ Write-Output "NOT_FOUND" }}
"""
    result = _ps(script)
    if result == "OK":
        return f"Minimized {name}."
    return f"Couldn't find a window for {name}."

def cmd_maximize_app(p):
    """Maximize an application window."""
    name = p.get("app", "")
    if not name:
        return "Which app should I maximize?"
    script = f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinAPI {{
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
}}
"@
$target = "{name.lower()}"
$pids = @()
Get-Process | Where-Object {{ $_.ProcessName -like "*$target*" }} | ForEach-Object {{ $pids += $_.Id }}
if ($pids.Count -eq 0) {{ Write-Output "NOT_FOUND"; exit }}
$found = $false
$callback = [WinAPI+EnumWindowsProc]{{
    param($hWnd, $lParam)
    if ([WinAPI]::IsWindowVisible($hWnd)) {{
        $pid = 0
        [WinAPI]::GetWindowThreadProcessId($hWnd, [ref]$pid) | Out-Null
        if ($pids -contains $pid) {{
            [WinAPI]::ShowWindow($hWnd, 3) | Out-Null  # SW_MAXIMIZE = 3
            $found = $true
        }}
    }}
    return $true
}}
[WinAPI]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
if ($found) {{ Write-Output "OK" }} else {{ Write-Output "NOT_FOUND" }}
"""
    result = _ps(script)
    if result == "OK":
        return f"Maximized {name}."
    return f"Couldn't find a window for {name}."

def cmd_focus_app(p):
    """Bring an application window to the front."""
    name = p.get("app", "")
    if not name:
        return "Which app should I focus?"
    # Try the existing launcher's bring-to-front first
    try:
        from app_launcher import resolve
        app = resolve(name)
        if app and app.get("executable"):
            from app_launcher import _bring_to_front
            if _bring_to_front(app["executable"]):
                return f"Brought {app['name']} to the front."
    except Exception:
        pass
    # Fallback: PowerShell approach
    script = f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinAPI {{
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
}}
"@
$target = "{name.lower()}"
$pids = @()
Get-Process | Where-Object {{ $_.ProcessName -like "*$target*" }} | ForEach-Object {{ $pids += $_.Id }}
if ($pids.Count -eq 0) {{ Write-Output "NOT_FOUND"; exit }}
$found = $false
$callback = [WinAPI+EnumWindowsProc]{{
    param($hWnd, $lParam)
    if ([WinAPI]::IsWindowVisible($hWnd)) {{
        $pid = 0
        [WinAPI]::GetWindowThreadProcessId($hWnd, [ref]$pid) | Out-Null
        if ($pids -contains $pid) {{
            [WinAPI]::SetForegroundWindow($hWnd) | Out-Null
            $found = $true
        }}
    }}
    return $true
}}
[WinAPI]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
if ($found) {{ Write-Output "OK" }} else {{ Write-Output "NOT_FOUND" }}
"""
    result = _ps(script)
    if result == "OK":
        return f"Brought {name} to the front."
    return f"Couldn't find a window for {name}."

def cmd_restore_app(p):
    """Restore a minimized application window."""
    name = p.get("app", "")
    if not name:
        return "Which app should I restore?"
    script = f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinAPI {{
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
}}
"@
$target = "{name.lower()}"
$pids = @()
Get-Process | Where-Object {{ $_.ProcessName -like "*$target*" }} | ForEach-Object {{ $pids += $_.Id }}
if ($pids.Count -eq 0) {{ Write-Output "NOT_FOUND"; exit }}
$found = $false
$callback = [WinAPI+EnumWindowsProc]{{
    param($hWnd, $lParam)
    if ([WinAPI]::IsWindowVisible($hWnd)) {{
        $pid = 0
        [WinAPI]::GetWindowThreadProcessId($hWnd, [ref]$pid) | Out-Null
        if ($pids -contains $pid) {{
            [WinAPI]::ShowWindow($hWnd, 9) | Out-Null  # SW_RESTORE = 9
            [WinAPI]::SetForegroundWindow($hWnd) | Out-Null
            $found = $true
        }}
    }}
    return $true
}}
[WinAPI]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
if ($found) {{ Write-Output "OK" }} else {{ Write-Output "NOT_FOUND" }}
"""
    result = _ps(script)
    if result == "OK":
        return f"Restored {name}."
    return f"Couldn't find a window for {name}."

def cmd_resize_window(p):
    """Resize an application window."""
    name = p.get("app", "")
    width = p.get("width", 800)
    height = p.get("height", 600)
    if not name:
        return "Which app should I resize?"
    script = f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinAPI {{
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr hWnd, int x, int y, int w, int h, bool repaint);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
}}
"@
$target = "{name.lower()}"
$pids = @()
Get-Process | Where-Object {{ $_.ProcessName -like "*$target*" }} | ForEach-Object {{ $pids += $_.Id }}
if ($pids.Count -eq 0) {{ Write-Output "NOT_FOUND"; exit }}
$callback = [WinAPI+EnumWindowsProc]{{
    param($hWnd, $lParam)
    if ([WinAPI]::IsWindowVisible($hWnd)) {{
        $pid = 0
        [WinAPI]::GetWindowThreadProcessId($hWnd, [ref]$pid) | Out-Null
        if ($pids -contains $pid) {{
            [WinAPI]::MoveWindow($hWnd, 100, 100, {int(width)}, {int(height)}, $true) | Out-Null
            Write-Output "OK"
            return $false
        }}
    }}
    return $true
}}
[WinAPI]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
"""
    result = _ps(script)
    if "OK" in result:
        return f"Resized {name} to {width} by {height}."
    return f"Couldn't find a window for {name}."

def cmd_move_window(p):
    """Move an application window to a position."""
    name = p.get("app", "")
    x = p.get("x", 100)
    y = p.get("y", 100)
    if not name:
        return "Which app should I move?"
    script = f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinAPI {{
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr hWnd, int x, int y, int w, int h, bool repaint);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [StructLayout(LayoutKind.Sequential)] public struct RECT {{ public int Left, Top, Right, Bottom; }}
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
}}
"@
$target = "{name.lower()}"
$pids = @()
Get-Process | Where-Object {{ $_.ProcessName -like "*$target*" }} | ForEach-Object {{ $pids += $_.Id }}
if ($pids.Count -eq 0) {{ Write-Output "NOT_FOUND"; exit }}
$callback = [WinAPI+EnumWindowsProc]{{
    param($hWnd, $lParam)
    if ([WinAPI]::IsWindowVisible($hWnd)) {{
        $pid = 0
        [WinAPI]::GetWindowThreadProcessId($hWnd, [ref]$pid) | Out-Null
        if ($pids -contains $pid) {{
            $rect = New-Object WinAPI+RECT
            [WinAPI]::GetWindowRect($hWnd, [ref]$rect) | Out-Null
            $w = $rect.Right - $rect.Left
            $h = $rect.Bottom - $rect.Top
            [WinAPI]::MoveWindow($hWnd, {int(x)}, {int(y)}, $w, $h, $true) | Out-Null
            Write-Output "OK"
            return $false
        }}
    }}
    return $true
}}
[WinAPI]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
"""
    result = _ps(script)
    if "OK" in result:
        return f"Moved {name} to position {x}, {y}."
    return f"Couldn't find a window for {name}."

def cmd_fullscreen_app(p):
    """Toggle fullscreen for an application using F11."""
    name = p.get("app", "")
    if name:
        # Focus the app first, then send F11
        focus_result = cmd_focus_app({"app": name})
        if "Couldn't" in focus_result:
            return focus_result
        time.sleep(0.3)
    if HAS_PYAUTOGUI:
        pyautogui.press("f11")
        return f"Toggled fullscreen{' for ' + name if name else ''}."
    return "pyautogui not installed."

COMMANDS = {
    # App lifecycle
    "open_app":        cmd_open_app,
    "close_app":       cmd_close_app,
    "focus_app":       cmd_focus_app,
    "minimize_app":    cmd_minimize_app,
    "maximize_app":    cmd_maximize_app,
    "restore_app":     cmd_restore_app,
    "fullscreen_app":  cmd_fullscreen_app,
    "resize_window":   cmd_resize_window,
    "move_window":     cmd_move_window,
    # Volume & audio
    "set_volume":      cmd_set_volume,
    "mute":            cmd_mute,
    # Mouse & keyboard
    "mouse_click":     cmd_mouse_click,
    "mouse_move":      cmd_mouse_move,
    "type_text":       cmd_type_text,
    "hotkey":          cmd_hotkey,
    # Browser & web
    "open_url":        cmd_open_url,
    "search_web":      cmd_search_web,
    # Clipboard
    "clipboard_read":  cmd_clipboard_read,
    "clipboard_write": cmd_clipboard_write,
    # Processes
    "list_processes":  cmd_list_processes,
    "kill_process":    cmd_kill_process,
    # System
    "system_info":     cmd_system_info,
    "screenshot":      cmd_screenshot,
    "lock":            cmd_lock,
    "shutdown":        cmd_shutdown,
    "restart":         cmd_restart,
    "cancel_shutdown": cmd_cancel_shutdown,
    "toast":           cmd_toast,
    # Files
    "create_file":     cmd_create_file,
    "search_files":    cmd_search_files,
    # Aliases
    "add_alias":       cmd_add_alias,
    "remove_alias":    cmd_remove_alias,
    "refresh_apps":    cmd_refresh_apps,
}

def handle_command(cmd: dict) -> str:
    """Execute a single system command. Returns the spoken result."""
    fn = COMMANDS.get(cmd.get("command",""))
    if fn:
        try:    return fn(cmd.get("params", {}))
        except Exception as e: return f"Command error: {e}"
    return f"Unknown command: {cmd.get('command')}."

def handle_commands(cmds: list) -> str:
    """Execute multiple commands sequentially with short delays between them.
    Returns a combined spoken summary.
    """
    results = []
    for i, cmd in enumerate(cmds):
        if _interrupt_event.is_set():
            results.append("Interrupted.")
            break
        result = handle_command(cmd)
        results.append(result)
        # Small delay between commands (except after the last one)
        if i < len(cmds) - 1:
            time.sleep(0.5)
    return "; ".join(results)

def try_parse_commands(response: str) -> tuple:
    """Extract system commands from LLM response.
    
    Supports two formats:
    1. Single: {"action":"SYSTEM_COMMAND", "command":"...", "params":{}}
    2. Multi:  [{"action":"SYSTEM_COMMAND", ...}, {"action":"SYSTEM_COMMAND", ...}]
    
    Returns (commands_list, prefix_text).
    commands_list is empty if no commands found.
    """
    s = response.strip()
    
    # Try to find a JSON array of commands first
    arr_start = s.find('[{"action":"SYSTEM_COMMAND"')
    if arr_start == -1:
        arr_start = s.find('[{"action": "SYSTEM_COMMAND"')
    
    if arr_start != -1:
        # Find matching closing bracket
        depth = 0
        arr_end = -1
        for i, ch in enumerate(s[arr_start:], arr_start):
            if ch == "[": depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0: arr_end = i; break
        if arr_end != -1:
            try:
                cmds = json.loads(s[arr_start:arr_end + 1])
                if isinstance(cmds, list):
                    prefix = s[:arr_start].strip()
                    # Filter valid commands
                    valid = [c for c in cmds if c.get("action") == "SYSTEM_COMMAND"]
                    if valid:
                        return valid, prefix or None
            except json.JSONDecodeError:
                pass
    
    # Fall back to single command
    cmd, prefix = try_parse_command_single(s)
    if cmd:
        return [cmd], prefix
    return [], None

def try_parse_command_single(response: str):
    """Parse a single SYSTEM_COMMAND from response text."""
    s = response.strip()
    start = s.find('{"action":"SYSTEM_COMMAND"')
    if start == -1:
        start = s.find('{"action": "SYSTEM_COMMAND"')
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

def try_parse_command(response: str):
    """Backward-compatible: returns single command or None."""
    cmds, prefix = try_parse_commands(response)
    return cmds[0] if cmds else None, prefix

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
#  QUERY PROCESSING — streaming: speaks first sentence while rest generates
# ═══════════════════════════════════════════════════════════════════════════════
def process_query(query: str):
    """Called from STT thread. Streams from backend, speaks each sentence as it arrives."""
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

    # Backend — streaming endpoint
    ui_event("thinking")
    ui_event("status", "Thinking...")
    current_time, current_date = get_current_time()

    try:
        with requests.post(
            f"{SERVER_URL}/api/jarvis/voice-query-stream",
            json={"prompt": query, "currentTime": current_time, "currentDate": current_date},
            stream=True,
            timeout=60,
        ) as r:
            if r.status_code != 200:
                speak(f"Server error — status {r.status_code}.")
                set_state(State.LISTENING)
                ui_event("listening")
                return

            # Read NDJSON stream — each line is a sentence
            pending_commands = []
            pending_prefix = None
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if chunk.get("type") == "done":
                    break

                if chunk.get("type") == "sentence":
                    text = chunk["text"].strip()
                    if not text:
                        continue

                    # Check for system commands (single or multi)
                    cmds, prefix = try_parse_commands(text)
                    if cmds:
                        pending_commands.extend(cmds)
                        if prefix and not pending_prefix:
                            pending_prefix = prefix
                    elif pending_commands:
                        # Commands already queued, this is extra text — skip
                        pass
                    else:
                        # Normal speech — queue for immediate synthesis
                        speak(text)

            # Execute any system commands after speech finishes
            if pending_commands:
                _speech_queue.join()  # wait for prior speech to finish
                if pending_prefix:
                    speak(pending_prefix)
                for cmd in pending_commands:
                    ui_event("system_cmd", f"Executing: {cmd.get('command','')}", cmd)
                speak(handle_commands(pending_commands))

    except requests.exceptions.ConnectionError:
        speak("Can't reach the server.")
    except requests.exceptions.Timeout:
        speak("The server took too long.")
    except Exception as e:
        print(f"[Request Error]: {e}")
        speak("There was a network issue.")

    # Wait for all queued speech to finish before returning to listening
    _speech_queue.join()
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

    # TTS worker thread (streaming — speaks each sentence as it arrives)
    tts_t = threading.Thread(target=_tts_worker_fn, daemon=True, name="TTS")
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
    print(f"-> Voice    : Kokoro 82M ({KOKORO_VOICE})")
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

    _speech_queue.put(None)
    tts_t.join(timeout=2)


if __name__ == "__main__":
    main()