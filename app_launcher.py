#!/usr/bin/env python3
"""
app_launcher.py — Best-in-class Windows Application Launcher for J.A.R.V.I.S.

Fixes over previous version:
  - Proper source priority: startmenu > win32 > uwp > path > steam > alias
  - Query normalisation: strips version numbers, punctuation, common suffixes
  - Two-pass fuzzy: exact/prefix/token first, then scored fuzzy with tiebreaking
  - Alias keys are also fuzzy-matched (not just exact)
  - UWP AppIDs excluded from fuzzy name scoring (they're GUIDs, not names)
  - Win32 display names cleaned of version strings before matching
  - Steam [Steam] suffix stripped before matching
  - Minimum score raised to 72 with dynamic threshold per source
  - "open X" prefix stripped before matching
  - Bring-to-front gracefully degrades without win32gui

Discovery Sources (parallel threads):
  - Start Menu .lnk   (highest trust — user-installed, named by user)
  - Win32 registry    (uninstall keys × 3 hives)
  - UWP/Store         (Get-StartApps + Get-AppxPackage)
  - PATH executables  (Get-Command)
  - Steam games       (appmanifest_*.acf)
  - User aliases      (aliases.json — highest priority, checked first)

Usage:
    python app_launcher.py                        # index and show stats
    python app_launcher.py "spotify"              # launch app
    python app_launcher.py --list
    python app_launcher.py --refresh
    python app_launcher.py --add-alias discord "Discord PTB"
    python app_launcher.py --remove-alias discord
    python app_launcher.py --stats
"""

import sqlite3, json, os, sys, subprocess, threading, time, re
from pathlib import Path
from rapidfuzz import process as fuzz_proc, fuzz

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
ALIASES_FILE = os.path.join(BASE_DIR, "aliases.json")
CACHE_DB     = os.path.join(BASE_DIR, "apps_cache.db")
CACHE_TTL    = 86400    # 24 hours
INDEX_TIMEOUT = 30
LEGACY_DB    = os.path.join(BASE_DIR, "app_database.json")

# Source priority — lower index = higher priority in tiebreaking
SOURCE_PRIORITY = {
    "alias":     0,
    "startmenu": 1,
    "win32":     2,
    "uwp":       3,
    "path":      4,
    "steam":     5,
    "legacy":    6,
}

# Min fuzzy score to accept a match (WRatio 0-100)
FUZZ_THRESHOLD = 72

# Noise words to strip from both query and app names before matching
_NOISE = re.compile(
    r"\b(v?[\d]+[\.\d]*[\w]*|"          # version numbers: 1.2.3, v2, 64bit
    r"x64|x86|64.?bit|32.?bit|"
    r"edition|version|update|build|"
    r"microsoft|windows|app|application|"
    r"installer|setup|uninstaller|"
    r"helper|host|service|daemon|"
    r"\(beta\)|\(preview\)|\(insider\)|"
    r"ltsc|lts|pro|home|enterprise)\b",
    re.IGNORECASE
)

# Strip common launcher suffixes like " - Desktop App", " for Windows"
_SUFFIX = re.compile(
    r"\s*[-–]\s*(desktop app|for windows|for pc|pc|web|browser|client)$",
    re.IGNORECASE
)

# Strip "open X", "launch X", "run X", "start X" from voice queries
_OPEN_PREFIX = re.compile(
    r"^(open|launch|run|start|load|execute|bring up|pull up)\s+",
    re.IGNORECASE
)

def _clean(text: str) -> str:
    """Normalise a name or query for comparison."""
    t = text.strip()
    t = _OPEN_PREFIX.sub("", t)          # strip "open X"
    t = _SUFFIX.sub("", t)              # strip "- Desktop App"
    t = _NOISE.sub("", t)               # strip version/noise words
    t = re.sub(r"[^\w\s]", " ", t)      # remove punctuation
    t = re.sub(r"\s+", " ", t).strip()  # collapse whitespace
    return t.lower()

def _stem(path: str) -> str:
    """Return lowercased filename stem, cleaned."""
    return _clean(Path(path).stem)

# ═══════════════════════════════════════════════════════════════════════════════
#  SQLite
# ═══════════════════════════════════════════════════════════════════════════════
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(CACHE_DB, check_same_thread=False)
    c.execute("""CREATE TABLE IF NOT EXISTS apps (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL,
        clean_name   TEXT NOT NULL,
        executable   TEXT NOT NULL,
        source       TEXT NOT NULL,
        launch_cmd   TEXT NOT NULL,
        added_at     REAL NOT NULL,
        launch_count INTEGER DEFAULT 0,
        last_launched REAL DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    c.commit()
    return c

def _needs_refresh() -> bool:
    c = _conn()
    row = c.execute("SELECT value FROM meta WHERE key='last_indexed'").fetchone()
    c.close()
    if not row: return True
    return time.time() - float(row[0]) > CACHE_TTL

def _save(results: list, elapsed: float):
    c = _conn()
    c.execute("DELETE FROM apps")
    c.executemany(
        "INSERT INTO apps (name,clean_name,executable,source,launch_cmd,added_at) "
        "VALUES (:name,:clean_name,:executable,:source,:launch_cmd,:added_at)",
        results
    )
    c.execute("INSERT OR REPLACE INTO meta VALUES ('last_indexed',?)", (str(time.time()),))
    c.execute("INSERT OR REPLACE INTO meta VALUES ('index_duration',?)", (str(elapsed),))
    c.execute("INSERT OR REPLACE INTO meta VALUES ('app_count',?)", (str(len(results)),))
    c.commit()
    c.close()

def _load_apps() -> list:
    c = _conn()
    rows = c.execute(
        "SELECT name,clean_name,executable,source,launch_cmd FROM apps "
        "ORDER BY launch_count DESC, last_launched DESC"
    ).fetchall()
    c.close()
    return [{"name":r[0],"clean_name":r[1],"executable":r[2],
             "source":r[3],"launch_cmd":r[4]} for r in rows]

def _bump(exe: str):
    c = _conn()
    c.execute("UPDATE apps SET launch_count=launch_count+1, last_launched=? WHERE executable=?",
              (time.time(), exe))
    c.commit()
    c.close()

# ═══════════════════════════════════════════════════════════════════════════════
#  ALIASES
# ═══════════════════════════════════════════════════════════════════════════════
def _load_aliases() -> dict:
    if not os.path.exists(ALIASES_FILE): return {}
    try:
        with open(ALIASES_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except: return {}

def _save_aliases(a: dict):
    with open(ALIASES_FILE, "w", encoding="utf-8") as f:
        json.dump(a, f, indent=2)

def add_alias(alias: str, target: str) -> str:
    a = _load_aliases(); a[alias.lower().strip()] = target.strip(); _save_aliases(a)
    return f"Alias '{alias}' → '{target}' saved."

def remove_alias(alias: str) -> str:
    a = _load_aliases(); k = alias.lower().strip()
    if k in a:
        t = a.pop(k); _save_aliases(a); return f"Alias '{alias}' → '{t}' removed."
    return f"No alias '{alias}'."

# ═══════════════════════════════════════════════════════════════════════════════
#  DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════
def _ps(script: str, timeout: int = 15) -> str:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout, encoding="utf-8"
        )
        return r.stdout.strip()
    except: return ""

def _make(name, executable, source, launch_cmd=None) -> dict:
    return {
        "name": name.strip(),
        "clean_name": _clean(name),
        "executable": executable.strip(),
        "source": source,
        "launch_cmd": (launch_cmd or executable).strip(),
        "added_at": time.time(),
    }

def _index_startmenu() -> list:
    """Highest-trust source — Start Menu shortcuts named by the user/installer."""
    script = r"""
$dirs = @(
    [Environment]::GetFolderPath('CommonStartMenu') + '\Programs',
    [Environment]::GetFolderPath('StartMenu') + '\Programs'
)
$shell = New-Object -ComObject WScript.Shell
$results = @()
foreach ($dir in $dirs) {
    if (-not (Test-Path $dir)) { continue }
    Get-ChildItem $dir -Recurse -Filter '*.lnk' -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $s = $shell.CreateShortcut($_.FullName)
            $t = $s.TargetPath
            if ($t -and ($t -like '*.exe') -and (Test-Path $t)) {
                $results += [PSCustomObject]@{ Name = $_.BaseName; Exe = $t }
            }
        } catch {}
    }
}
[System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell) | Out-Null
$results | Sort-Object Name -Unique | ConvertTo-Json -Compress
"""
    out = _ps(script)
    if not out or out == "[]": return []
    try: data = json.loads(out)
    except: return []
    if isinstance(data, dict): data = [data]
    results = []
    seen = set()
    for a in data:
        name = (a.get("Name") or "").strip()
        exe  = (a.get("Exe") or "").strip()
        if not name or not exe: continue
        # Skip uninstallers, helpers, crash reporters
        if re.search(r"\b(uninstall|crash|helper|updater|setup|install|report)\b",
                     name, re.I): continue
        key = exe.lower()
        if key in seen: continue
        seen.add(key)
        results.append(_make(name, exe, "startmenu"))
    return results

def _index_win32() -> list:
    script = r"""
$paths = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
$skip = @('^Update for','Security Update','Hotfix','KB[0-9]',
          'Visual C\+\+','\.NET ','Windows SDK','Edge Update',
          'Update Health','Driver','Redistributable','Runtime')
$results = @()
foreach ($p in $paths) {
    Get-ItemProperty $p -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -and $_.DisplayIcon } |
    ForEach-Object {
        $skip_it = $false
        foreach ($pat in $skip) { if ($_.DisplayName -match $pat) { $skip_it=$true; break } }
        if ($skip_it) { return }
        $exe = ($_.DisplayIcon -split ',')[0].Trim('"')
        if ($exe -and $exe -like '*.exe' -and (Test-Path $exe)) {
            $results += [PSCustomObject]@{ Name=$_.DisplayName; Exe=$exe }
        }
    }
}
$results | Sort-Object Name -Unique | ConvertTo-Json -Compress
"""
    out = _ps(script)
    if not out or out == "[]": return []
    try: data = json.loads(out)
    except: return []
    if isinstance(data, dict): data = [data]
    seen = set()
    results = []
    for a in data:
        name = (a.get("Name") or "").strip()
        exe  = (a.get("Exe") or "").strip()
        if not name or not exe: continue
        if re.search(r"\b(uninstall|crash|helper|updater)\b", name, re.I): continue
        key = exe.lower()
        if key in seen: continue
        seen.add(key)
        results.append(_make(name, exe, "win32"))
    return results

def _index_uwp() -> list:
    script = r"""
$results = @()
Get-StartApps -ErrorAction SilentlyContinue | ForEach-Object {
    $results += [PSCustomObject]@{ Name=$_.Name; AppID=$_.AppID }
}
$results | ConvertTo-Json -Compress
"""
    out = _ps(script)
    if not out or out == "[]": return []
    try: data = json.loads(out)
    except: return []
    if isinstance(data, dict): data = [data]
    results = []
    for a in data:
        name  = (a.get("Name") or "").strip()
        appid = (a.get("AppID") or "").strip()
        if not name or not appid: continue
        if re.search(r"\b(runtime|framework|sdk|redistributable)\b", name, re.I): continue
        results.append(_make(name, appid, "uwp", appid))
    return results

def _index_path() -> list:
    script = r"""
Get-Command -CommandType Application |
    Where-Object { $_.Extension -eq '.exe' } |
    Select-Object Name,Source |
    ConvertTo-Json -Compress
"""
    out = _ps(script)
    if not out or out == "[]": return []
    try: data = json.loads(out)
    except: return []
    if isinstance(data, dict): data = [data]
    seen = set()
    results = []
    for a in data:
        name = (a.get("Name") or "").strip()
        src  = (a.get("Source") or "").strip()
        if not name or not src: continue
        key = src.lower()
        if key in seen: continue
        seen.add(key)
        display = Path(name).stem.replace("-"," ").replace("_"," ").title()
        results.append(_make(display, src, "path"))
    return results

def _index_steam() -> list:
    script = r"""
$sp = (Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Valve\Steam' `
    -Name InstallPath -ErrorAction SilentlyContinue).InstallPath
if (-not $sp) { return @() }
$results = @()
Get-ChildItem "$sp\steamapps" -Filter appmanifest_*.acf -ErrorAction SilentlyContinue |
ForEach-Object {
    $c = Get-Content $_.FullName -Raw
    if ($c -match '"name"\s+"([^"]+)"') {
        $gname = $matches[1]
        $appid = ($_.Name -replace 'appmanifest_|\.acf','').Trim()
        $results += [PSCustomObject]@{ Name=$gname; AppID=$appid }
    }
}
$results | ConvertTo-Json -Compress
"""
    out = _ps(script, timeout=10)
    if not out or out == "[]": return []
    try: data = json.loads(out)
    except: return []
    if isinstance(data, dict): data = [data]
    results = []
    for a in data:
        name  = (a.get("Name") or "").strip()
        appid = (a.get("AppID") or "").strip()
        if not name or not appid: continue
        launch = f"steam://rungameid/{appid}"
        results.append(_make(f"{name}", f"steam_{appid}", "steam", launch))
    return results

def _migrate_legacy() -> list:
    if not os.path.exists(LEGACY_DB): return []
    try:
        with open(LEGACY_DB, "r", encoding="utf-8") as f: data = json.load(f)
    except: return []
    results = []
    aliases = _load_aliases()
    for alias_key, info in data.items():
        name  = info.get("name", alias_key)
        appid = info.get("appid", "")
        if not name or not appid: continue
        is_uwp = "!" in appid or appid.startswith("Microsoft.")
        results.append(_make(name, appid, "uwp" if is_uwp else "legacy", appid))
        k = alias_key.lower().strip()
        if k and k != name.lower() and k not in aliases:
            aliases[k] = name
    _save_aliases(aliases)
    return results

# ═══════════════════════════════════════════════════════════════════════════════
#  INDEX
# ═══════════════════════════════════════════════════════════════════════════════
_index_lock = threading.Lock()
_index_ready = threading.Event()

def refresh_index(quiet: bool = False) -> int:
    with _index_lock:
        t0 = time.time()
        if not quiet: print("[AppLauncher] Indexing applications...")

        results = []
        lock = threading.Lock()

        def collect(fn, label):
            try:
                items = fn()
                with lock: results.extend(items)
                if not quiet: print(f"[AppLauncher]   {label}: {len(items)}")
            except Exception as e:
                if not quiet: print(f"[AppLauncher]   {label} ERROR: {e}")

        threads = [
            threading.Thread(target=collect, args=(_index_startmenu, "StartMenu"), daemon=True),
            threading.Thread(target=collect, args=(_index_win32, "Win32"),      daemon=True),
            threading.Thread(target=collect, args=(_index_uwp, "UWP"),          daemon=True),
            threading.Thread(target=collect, args=(_index_path, "PATH"),        daemon=True),
            threading.Thread(target=collect, args=(_index_steam, "Steam"),      daemon=True),
        ]
        for t in threads: t.start()
        for t in threads: t.join(timeout=INDEX_TIMEOUT)

        results.extend(_migrate_legacy())

        # Add alias entries so they show up in listing
        aliases = _load_aliases()
        for k, v in aliases.items():
            results.append(_make(k.title(), v, "alias", v))

        # Deduplicate: keep highest-priority source per executable
        seen = {}
        for app in results:
            key = app["executable"].lower()
            if key not in seen:
                seen[key] = app
            else:
                existing_prio = SOURCE_PRIORITY.get(seen[key]["source"], 99)
                new_prio      = SOURCE_PRIORITY.get(app["source"], 99)
                if new_prio < existing_prio:
                    seen[key] = app

        deduped = list(seen.values())
        elapsed = time.time() - t0
        _save(deduped, elapsed)
        _index_ready.set()

        if not quiet: print(f"[AppLauncher] Done: {len(deduped)} apps in {elapsed:.1f}s")
        return len(deduped)

def _ensure_indexed():
    """Block until index is available; refresh if stale."""
    if _needs_refresh():
        refresh_index(quiet=True)
    _index_ready.set()

# ═══════════════════════════════════════════════════════════════════════════════
#  RESOLVER  — the heart of the launcher
# ═══════════════════════════════════════════════════════════════════════════════
def resolve(query: str) -> dict | None:
    """
    Find the single best matching app for a natural-language query.
    Resolution order (first match wins):
      1. Alias exact match
      2. Alias fuzzy match  (score ≥ 85)
      3. App name exact match (clean)
      4. App name prefix match (clean, min 3 chars)
      5. Exe stem exact match
      6. Token set ratio ≥ 90 (handles "code" → "VS Code")
      7. Partial ratio ≥ 85 (handles "spotify" in "Spotify Music")
      8. WRatio fuzzy on name  (threshold dynamic)
      9. WRatio fuzzy on exe stem
     10. Substring in clean name
     11. Shell fallback
    """
    raw_q = query.strip()
    if not raw_q: return None

    # Strip open/launch prefix for the actual match query
    clean_q = _clean(raw_q)
    if not clean_q: return None

    _ensure_indexed()
    apps = _load_apps()
    aliases = _load_aliases()

    # ── 1. Alias exact ────────────────────────────────────────────────────────
    if clean_q in aliases:
        target = aliases[clean_q]
        # Find the app this alias points to
        for app in apps:
            if (app["executable"].lower() == target.lower()
                    or app["clean_name"] == _clean(target)
                    or app["name"].lower() == target.lower()):
                return app
        # Not in index — treat target as direct launch command
        return _make(raw_q, target, "alias", target)

    # ── 2. Alias fuzzy (for voice: "disc cord" → "discord" alias) ────────────
    if aliases:
        alias_keys = list(aliases.keys())
        best = fuzz_proc.extractOne(clean_q, alias_keys, scorer=fuzz.WRatio)
        if best and best[1] >= 85:
            target = aliases[best[0]]
            for app in apps:
                if (app["executable"].lower() == target.lower()
                        or app["clean_name"] == _clean(target)
                        or app["name"].lower() == target.lower()):
                    return app
            return _make(raw_q, target, "alias", target)

    if not apps: return None

    clean_names = [a["clean_name"] for a in apps]
    exe_stems   = [_stem(a["executable"]) for a in apps]

    # ── 3. Exact clean name ───────────────────────────────────────────────────
    for i, app in enumerate(apps):
        if clean_names[i] == clean_q:
            return app

    # ── 4. Prefix match (query is start of clean name, min 3 chars) ──────────
    if len(clean_q) >= 3:
        # Sort candidates by source priority so we get startmenu before path
        candidates = [
            (i, app) for i, app in enumerate(apps)
            if clean_names[i].startswith(clean_q)
        ]
        if candidates:
            candidates.sort(key=lambda x: SOURCE_PRIORITY.get(x[1]["source"], 99))
            return candidates[0][1]

    # ── 5. Exe stem exact ─────────────────────────────────────────────────────
    for i, app in enumerate(apps):
        if exe_stems[i] == clean_q:
            return app

    # ── 6. Token set ratio ≥ 90 ───────────────────────────────────────────────
    # "vs code" → "visual studio code", "chrome" → "google chrome"
    scored_token = []
    for i, app in enumerate(apps):
        s = fuzz.token_set_ratio(clean_q, clean_names[i])
        if s >= 90:
            scored_token.append((s, SOURCE_PRIORITY.get(app["source"], 99), i))
    if scored_token:
        scored_token.sort(key=lambda x: (x[0]*-1, x[1]))  # best score, then priority
        return apps[scored_token[0][2]]

    # ── 7. Partial ratio ≥ 85 ────────────────────────────────────────────────
    # "spotify" in "Spotify Music", "word" in "Microsoft Word"
    scored_partial = []
    for i, app in enumerate(apps):
        s = fuzz.partial_ratio(clean_q, clean_names[i])
        if s >= 85:
            scored_partial.append((s, SOURCE_PRIORITY.get(app["source"], 99), i))
    if scored_partial:
        scored_partial.sort(key=lambda x: (x[0]*-1, x[1]))
        return apps[scored_partial[0][2]]

    # ── 8. WRatio on clean name ───────────────────────────────────────────────
    # Dynamic threshold: shorter queries need higher confidence
    threshold = FUZZ_THRESHOLD + max(0, (4 - len(clean_q.split())) * 5)
    best_name = fuzz_proc.extractOne(clean_q, clean_names, scorer=fuzz.WRatio)
    best_exe  = fuzz_proc.extractOne(clean_q, exe_stems,   scorer=fuzz.WRatio)

    candidates = []
    if best_name and best_name[1] >= threshold:
        candidates.append((best_name[1], SOURCE_PRIORITY.get(apps[best_name[2]]["source"], 99), best_name[2]))
    if best_exe and best_exe[1] >= threshold:
        candidates.append((best_exe[1], SOURCE_PRIORITY.get(apps[best_exe[2]]["source"], 99), best_exe[2]))

    if candidates:
        candidates.sort(key=lambda x: (x[0]*-1, x[1]))
        return apps[candidates[0][2]]

    # ── 9. Substring in clean name ────────────────────────────────────────────
    if len(clean_q) >= 4:
        for i, app in enumerate(apps):
            if clean_q in clean_names[i]:
                return app

    # ── 10. No match ──────────────────────────────────────────────────────────
    return None

# ═══════════════════════════════════════════════════════════════════════════════
#  BRING TO FRONT
# ═══════════════════════════════════════════════════════════════════════════════
def _bring_to_front(exe_path: str) -> bool:
    """Focus running instance. Degrades gracefully without win32gui/psutil."""
    try:
        import psutil, win32gui, win32con, win32process
    except ImportError:
        return False
    exe_name = os.path.basename(exe_path).lower()
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            p_name = (proc.info["name"] or "").lower()
            p_exe  = (proc.info["exe"]  or "").lower()
            if exe_name != p_name and exe_path.lower() != p_exe:
                continue
            hwnd_ref = [None]
            def _cb(hwnd, _):
                if not win32gui.IsWindowVisible(hwnd): return True
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid == proc.info["pid"]:
                    hwnd_ref[0] = hwnd; return False
                return True
            win32gui.EnumWindows(_cb, None)
            if hwnd_ref[0]:
                if win32gui.IsIconic(hwnd_ref[0]):
                    win32gui.ShowWindow(hwnd_ref[0], win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd_ref[0])
                return True
        except Exception:
            continue
    return False

# ═══════════════════════════════════════════════════════════════════════════════
#  LAUNCHER
# ═══════════════════════════════════════════════════════════════════════════════
def launch_app(query: str) -> str:
    """
    Main entry point. Resolves query to an app and launches it.
    Returns a human-readable string suitable for JARVIS to speak.
    """
    if not query or not query.strip():
        return "Please tell me which app to open."

    app = resolve(query)

    if not app:
        # Last resort: try as a raw shell command
        try:
            subprocess.Popen(query.strip(), shell=True)
            return f"Trying to launch {query}."
        except Exception:
            return f"I couldn't find an app matching '{query}'."

    exe  = app["launch_cmd"]
    src  = app.get("source", "")
    name = app["name"]

    # Try to focus existing window before launching a new instance
    if src not in ("uwp", "steam") and os.path.isfile(exe):
        if _bring_to_front(exe):
            return f"Brought {name} to the front."

    try:
        if src == "uwp":
            subprocess.Popen(["explorer", f"shell:AppsFolder\\{exe}"],
                             creationflags=subprocess.DETACHED_PROCESS)
        elif src == "steam" or exe.startswith("steam://"):
            subprocess.Popen(["cmd", "/c", "start", "", exe], shell=False)
        else:
            subprocess.Popen([exe],
                             creationflags=subprocess.DETACHED_PROCESS,
                             close_fds=True)

        _bump(app.get("executable", exe))
        return f"Opening {name}."

    except Exception as e:
        # Shell fallback
        try:
            subprocess.Popen(exe, shell=True)
            _bump(app.get("executable", exe))
            return f"Opening {name}."
        except Exception as e2:
            return f"Failed to launch {name}: {e2}"

# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def list_apps(source=None) -> list:
    _ensure_indexed()
    apps = _load_apps()
    if source: apps = [a for a in apps if a["source"] == source]
    return apps

def get_stats() -> dict:
    _ensure_indexed()
    c = _conn()
    total    = c.execute("SELECT COUNT(*) FROM apps").fetchone()[0]
    by_src   = dict(c.execute("SELECT source, COUNT(*) FROM apps GROUP BY source").fetchall())
    last     = c.execute("SELECT value FROM meta WHERE key='last_indexed'").fetchone()
    duration = c.execute("SELECT value FROM meta WHERE key='index_duration'").fetchone()
    c.close()
    return {
        "total": total,
        "by_source": by_src,
        "last_indexed": last[0] if last else None,
        "index_duration": float(duration[0]) if duration else 0,
    }

# ═══════════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    if "--refresh" in sys.argv:
        print(f"✓ Indexed {refresh_index()} apps.")
        return
    if "--list" in sys.argv:
        src = None
        if "--source" in sys.argv:
            i = sys.argv.index("--source")
            if i+1 < len(sys.argv): src = sys.argv[i+1]
        apps = list_apps(src)
        print(f"{'Name':45s} {'Source':10s} Executable")
        print("-"*90)
        for a in sorted(apps, key=lambda x: x["name"].lower()):
            print(f"{a['name'][:45]:45s} {a['source']:10s} {a['executable'][:50]}")
        print(f"\nTotal: {len(apps)}")
        return
    if "--add-alias" in sys.argv:
        i = sys.argv.index("--add-alias")
        if i+2 < len(sys.argv): print(add_alias(sys.argv[i+1], sys.argv[i+2]))
        else: print("Usage: --add-alias <alias> <target>")
        return
    if "--remove-alias" in sys.argv:
        i = sys.argv.index("--remove-alias")
        if i+1 < len(sys.argv): print(remove_alias(sys.argv[i+1]))
        return
    if "--stats" in sys.argv:
        s = get_stats()
        print(f"Apps: {s['total']}")
        for src, n in sorted(s['by_source'].items()): print(f"  {src}: {n}")
        if s['last_indexed']:
            print(f"Last: {time.strftime('%Y-%m-%d %H:%M', time.localtime(float(s['last_indexed'])))}")
            print(f"Time: {s['index_duration']:.1f}s")
        return
    if "--resolve" in sys.argv:
        i = sys.argv.index("--resolve")
        if i+1 < len(sys.argv):
            q = " ".join(sys.argv[i+1:])
            app = resolve(q)
            if app:
                print(f"Match: {app['name']} [{app['source']}] → {app['launch_cmd']}")
            else:
                print(f"No match for '{q}'")
        return
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        print(launch_app(" ".join(sys.argv[1:])))
        return
    n = refresh_index()
    print(f"\nApp Launcher ready — {n} apps indexed.")
    print("\nCommands:")
    print(f"  python app_launcher.py <name>              Launch app")
    print(f"  python app_launcher.py --resolve <name>    Test resolution without launching")
    print(f"  python app_launcher.py --list              List all indexed apps")
    print(f"  python app_launcher.py --list --source uwp List UWP apps only")
    print(f"  python app_launcher.py --refresh           Force re-index")
    print(f"  python app_launcher.py --add-alias a b     Add alias a→b")
    print(f"  python app_launcher.py --remove-alias a    Remove alias")
    print(f"  python app_launcher.py --stats             Index statistics")

if __name__ == "__main__":
    main()