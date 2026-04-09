#!/usr/bin/env python3
"""
GrooveBox Server v2.2 - Pi 4
- Monitor PipeWire SEMPRE ATTIVO (anche durante REC)
- 96kHz 24bit S24_3LE
- Storage su /mnt/groovebox/Registrazioni
- pw-record per registrazione
- PAUSE toggle SIGSTOP/SIGCONT
- FF/RW con sox
- VU meter disabilitato
"""

import os
import time
import signal
import logging
import threading
import subprocess
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, send_file, abort, request

load_dotenv()

# --- CONFIGURAZIONE ---------------------------------------------------

RECORDINGS_DIR = Path(os.getenv("RECORDINGS_DIR", "/mnt/groovebox/Registrazioni"))
PLAYBACK_CARD  = os.getenv("PLAYBACK_CARD", "plughw:CARD=Headphones,DEV=0")
SAMPLE_RATE    = int(os.getenv("SAMPLE_RATE", 96000))
CHANNELS       = int(os.getenv("CHANNELS", 2))
BIT_DEPTH      = os.getenv("BIT_DEPTH", "S24_3LE")
FF_RW_SECONDS  = int(os.getenv("FF_RW_SECONDS", 10))
PORT           = int(os.getenv("PORT", 5000))

BYTES_PER_SAMPLE = 3
BYTES_PER_FRAME  = BYTES_PER_SAMPLE * CHANNELS
WAV_HEADER_SIZE  = 44

PW_SOURCE = os.getenv("PW_SOURCE", "alsa_input.usb-Terratec_PhonoPreAmp_iVinyl-00.analog-stereo")
PW_SINK   = os.getenv("PW_SINK",   "alsa_output.platform-fe00b840.mailbox.stereo-fallback")

# --- LOGGING ----------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# --- APP --------------------------------------------------------------

app = Flask(__name__, static_folder=".")
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

# --- STATO -----------------------------------------------------------

state = {
    "status":        "stopped",
    "filename":      None,
    "filepath":      None,
    "seconds":       0,
    "level_l":       -60.0,
    "level_r":       -60.0,
    "filesize":       "",
    "play_filename": None,
    "play_seconds":  0,
    "play_duration": 0,
}

_lock            = threading.Lock()
_timer_t         = None
_rec_proc        = None
_play_proc       = None
_monitor_proc    = None
_play_paused     = False
_play_generation = 0

# --- UTILITY ---------------------------------------------------------

def _make_filename():
    return f"Vinile_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"

def _fmt_size(b):
    if b < 1024:      return f"{b} B"
    if b < 1024**2:   return f"{b/1024:.1f} KB"
    if b < 1024**3:   return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"

def _fmt_time(s):
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"

def _wav_duration(filepath):
    """Calcola la durata leggendo i parametri reali dall'header WAV."""
    try:
        with open(filepath, 'rb') as f:
            f.seek(22)
            channels = int.from_bytes(f.read(2), 'little')
            f.seek(24)
            sample_rate = int.from_bytes(f.read(4), 'little')
            f.seek(34)
            bits_per_sample = int.from_bytes(f.read(2), 'little')
            f.seek(40)
            data_size = int.from_bytes(f.read(4), 'little')
        bytes_per_frame = (bits_per_sample // 8) * channels
        if bytes_per_frame == 0 or sample_rate == 0:
            return 0
        total_frames = data_size // bytes_per_frame
        return total_frames // sample_rate
    except Exception:
        return 0

# --- MONITOR PIPEWIRE ------------------------------------------------

def _start_monitor():
    """Avvia loopback PipeWire Terratec -> Cuffie."""
    global _monitor_proc
    if _monitor_proc and _monitor_proc.poll() is None:
        return
    cmd = (
        f'pw-loopback '
        f'--capture-props="node.name={PW_SOURCE}" '
        f'--playback-props="node.name={PW_SINK}"'
    )
    log.info("MONITOR avvio...")
    _monitor_proc = subprocess.Popen(
        cmd, shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(0.5)
    if _monitor_proc.poll() is not None:
        log.error("MONITOR errore: processo terminato subito")
        _monitor_proc = None
    else:
        log.info("MONITOR attivo")

def _stop_monitor():
    """Ferma il loopback PipeWire."""
    global _monitor_proc
    if _monitor_proc and _monitor_proc.poll() is None:
        _monitor_proc.terminate()
        try:
            _monitor_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _monitor_proc.kill()
        log.info("MONITOR fermato")
    _monitor_proc = None
    subprocess.run(["pkill", "-f", "pw-loopback"], capture_output=True)
    time.sleep(0.3)

def _ensure_monitor():
    """Riavvia il monitor se non e' attivo."""
    global _monitor_proc
    if _monitor_proc is None or _monitor_proc.poll() is not None:
        _start_monitor()

# --- PW-RECORD -------------------------------------------------------

def _stop_arecord():
    """Ferma pw-record."""
    global _rec_proc
    if _rec_proc is not None:
        if _rec_proc.poll() is None:
            try:
                _rec_proc.send_signal(signal.SIGCONT)
                time.sleep(0.1)
            except Exception:
                pass
            _rec_proc.terminate()
            try:
                _rec_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _rec_proc.kill()
                _rec_proc.wait()
            log.info("REC pw-record fermato")
        _rec_proc = None
    subprocess.run(["pkill", "-f", "pw-record"], capture_output=True)
    time.sleep(0.3)

def _start_arecord(filepath):
    """Avvia pw-record via PipeWire."""
    global _rec_proc
    _stop_arecord()
    cmd = [
        "pw-record",
        "--target", PW_SOURCE,
        "--rate", str(SAMPLE_RATE),
        "--channels", str(CHANNELS),
        "--format", "s24",
        "--latency", "512/48000",
        str(filepath)
    ]
    log.info("REC %s", " ".join(cmd))
    _rec_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(0.5)
    if _rec_proc.poll() is not None:
        log.error("REC pw-record terminato subito")
        _rec_proc = None
        return False
    log.info("REC pw-record avviato")
    return True

def _pause_arecord():
    global _rec_proc
    if _rec_proc and _rec_proc.poll() is None:
        _rec_proc.send_signal(signal.SIGSTOP)
        log.info("PAUSE pw-record sospeso")

def _resume_arecord():
    global _rec_proc
    if _rec_proc and _rec_proc.poll() is None:
        _rec_proc.send_signal(signal.SIGCONT)
        log.info("RESUME pw-record ripreso")
        return True
    return False

# --- APLAY -----------------------------------------------------------

def _stop_aplay():
    """Ferma aplay e processi correlati."""
    global _play_proc
    if _play_proc is not None:
        if _play_proc.poll() is None:
            try:
                os.killpg(os.getpgid(_play_proc.pid), signal.SIGTERM)
            except Exception:
                pass
            _play_proc.terminate()
            try:
                _play_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _play_proc.kill()
                _play_proc.wait()
            log.info("PLAY aplay fermato")
        _play_proc = None
    subprocess.run(["pkill", "-f", f"aplay.*{PLAYBACK_CARD}"], capture_output=True)
    subprocess.run(["pkill", "-f", "sox.*Registrazioni"], capture_output=True)
    time.sleep(0.3)

def _pause_aplay():
    global _play_proc
    if _play_proc and _play_proc.poll() is None:
        try:
            os.killpg(os.getpgid(_play_proc.pid), signal.SIGSTOP)
        except Exception:
            _play_proc.send_signal(signal.SIGSTOP)
        log.info("PAUSE aplay sospeso")

def _resume_aplay():
    global _play_proc
    if _play_proc and _play_proc.poll() is None:
        try:
            os.killpg(os.getpgid(_play_proc.pid), signal.SIGCONT)
        except Exception:
            _play_proc.send_signal(signal.SIGCONT)
        log.info("RESUME aplay ripreso")
        return True
    return False

def _start_aplay(filepath, start_second=0):
    """Avvia riproduzione file WAV."""
    global _play_proc
    _stop_aplay()
    time.sleep(0.2)

    if start_second == 0:
        cmd = ["aplay", "-D", PLAYBACK_CARD, str(filepath)]
        log.info("PLAY %s", " ".join(cmd))
        try:
            _play_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid
            )
            return True
        except Exception as e:
            log.error("PLAY errore: %s", e)
            return False
    else:
        cmd = (
            f'sox "{filepath}" -t raw -r {SAMPLE_RATE} -c {CHANNELS} -e signed -b 16 - '
            f'trim {start_second} | '
            f'aplay -D {PLAYBACK_CARD} -r {SAMPLE_RATE} -c {CHANNELS} -f S16_LE -'
        )
        log.info("PLAY FF/RW da %s", _fmt_time(start_second))
        try:
            _play_proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid
            )
            return True
        except Exception as e:
            log.error("PLAY errore: %s", e)
            return False

def _play_monitor_loop(generation):
    """Monitora fine riproduzione. Si ferma se la generazione è cambiata."""
    global _play_proc, _play_paused, _play_generation
    start_time = time.time()
    with _lock:
        start_sec = state.get("play_seconds", 0)

    while True:
        if _play_generation != generation:
            break
        with _lock:
            if state["status"] not in ("playing", "paused"):
                break
        if not _play_paused:
            elapsed = int(time.time() - start_time)
            with _lock:
                if _play_generation == generation:
                    state["play_seconds"] = start_sec + elapsed
        if _play_proc and _play_proc.poll() is not None and not _play_paused:
            with _lock:
                if state["status"] == "playing" and _play_generation == generation:
                    state.update({
                        "status":       "stopped",
                        "play_seconds": 0,
                    })
            _ensure_monitor()
            log.info("PLAY riproduzione terminata")
            break
        time.sleep(0.5)

# --- TIMER -----------------------------------------------------------

def _timer_loop():
    while True:
        time.sleep(1)
        with _lock:
            if state["status"] == "recording":
                state["seconds"] += 1
                fpath = state.get("filepath")
                if fpath and Path(fpath).exists():
                    state["filesize"] = _fmt_size(Path(fpath).stat().st_size)
            else:
                break

def _start_timer():
    global _timer_t
    if _timer_t and _timer_t.is_alive():
        return
    _timer_t = threading.Thread(target=_timer_loop, daemon=True)
    _timer_t.start()

# --- RESUME DA PAUSA -------------------------------------------------

def _resume_from_pause():
    global _rec_proc
    if _rec_proc and _rec_proc.poll() is None:
        if _resume_arecord():
            with _lock:
                state["status"] = "recording"
            _start_timer()
            log.info("RESUME registrazione ripresa")
            return True
    return False

# --- API ENDPOINTS ---------------------------------------------------

@app.route("/")
def index():
    return send_file("groovebox.html")

@app.route("/api/status")
def api_status():
    with _lock:
        return jsonify(dict(state))

@app.route("/api/rec", methods=["POST"])
def api_rec():
    with _lock:
        current_status = state["status"]

    if current_status == "paused" and _rec_proc is not None:
        if _resume_from_pause():
            with _lock:
                return jsonify(dict(state))

    with _lock:
        if current_status == "recording":
            return jsonify({"error": "gia in registrazione"}), 400

    _stop_aplay()
    _stop_arecord()
    time.sleep(0.2)

    fname = _make_filename()
    fpath = RECORDINGS_DIR / fname

    with _lock:
        state.update({
            "filename":      fname,
            "filepath":      str(fpath),
            "status":        "recording",
            "seconds":       0,
            "filesize":      "0 B",
            "level_l":       -60.0,
            "level_r":       -60.0,
            "play_filename": None,
            "play_seconds":  0,
        })

    ok = _start_arecord(str(fpath))
    if not ok:
        with _lock:
            state["status"] = "stopped"
        return jsonify({"error": "pw-record non avviato"}), 500

    _ensure_monitor()
    _start_timer()
    log.info("REC avviata: %s", fname)
    with _lock:
        return jsonify(dict(state))

@app.route("/api/stop", methods=["POST"])
def api_stop():
    global _play_paused
    _play_paused = False
    _stop_arecord()
    _stop_aplay()
    _ensure_monitor()
    with _lock:
        state.update({
            "status":       "stopped",
            "seconds":      0,
            "level_l":      -60.0,
            "level_r":      -60.0,
            "play_seconds": 0,
        })
    log.info("STOP")
    with _lock:
        return jsonify(dict(state))

@app.route("/api/pause", methods=["POST"])
def api_pause():
    global _play_paused
    with _lock:
        current_status = state["status"]

    if current_status == "recording":
        _pause_arecord()
        with _lock:
            state["status"] = "paused"
        log.info("PAUSE registrazione in pausa")
        with _lock:
            return jsonify(dict(state))

    if current_status == "paused" and _rec_proc is not None and _play_proc is None:
        if _resume_from_pause():
            with _lock:
                return jsonify(dict(state))

    if current_status == "playing":
        _pause_aplay()
        _play_paused = True
        with _lock:
            state["status"] = "paused"
        log.info("PAUSE riproduzione in pausa")
        with _lock:
            return jsonify(dict(state))

    if current_status == "paused" and _play_proc is not None:
        if _resume_aplay():
            _play_paused = False
            with _lock:
                state["status"] = "playing"
            log.info("RESUME riproduzione ripresa")
            with _lock:
                return jsonify(dict(state))

    with _lock:
        return jsonify(dict(state))

@app.route("/api/play", methods=["POST"])
def api_play():
    global _play_paused, _play_generation
    data = request.get_json(silent=True) or {}
    filename = data.get("filename")

    if not filename:
        return api_files()

    fpath = RECORDINGS_DIR / filename
    if not fpath.exists():
        return jsonify({"error": "file non trovato"}), 404

    _stop_arecord()
    _stop_monitor()
    _stop_aplay()
    _play_paused = False
    time.sleep(0.2)

    duration = _wav_duration(str(fpath))
    filesize = _fmt_size(fpath.stat().st_size)

    with _lock:
        state.update({
            "status":        "playing",
            "filepath":      str(fpath),
            "play_filename": filename,
            "play_seconds":  0,
            "play_duration": duration,
            "filesize":      filesize,
            "level_l":       -60.0,
            "level_r":       -60.0,
        })

    ok = _start_aplay(str(fpath), start_second=0)
    if not ok:
        with _lock:
            state["status"] = "stopped"
        _ensure_monitor()
        return jsonify({"error": "aplay non avviato"}), 500

    _play_generation += 1
    gen = _play_generation
    t = threading.Thread(target=_play_monitor_loop, args=(gen,), daemon=True)
    t.start()
    log.info("PLAY avviato: %s", filename)
    with _lock:
        return jsonify(dict(state))

@app.route("/api/ff", methods=["POST"])
def api_ff():
    global _play_paused, _play_generation
    with _lock:
        if state["status"] not in ("playing", "paused"):
            return jsonify(dict(state))
        fpath    = state.get("filepath")
        play_sec = state.get("play_seconds", 0)
        duration = state.get("play_duration", 0)

    new_sec = min(play_sec + FF_RW_SECONDS, max(0, duration - 5))
    _stop_aplay()
    _play_paused = False
    time.sleep(0.3)

    ok = _start_aplay(fpath, start_second=new_sec)
    if ok:
        _play_generation += 1
        gen = _play_generation
        with _lock:
            state["play_seconds"] = new_sec
            state["status"] = "playing"
        t = threading.Thread(target=_play_monitor_loop, args=(gen,), daemon=True)
        t.start()

    log.info("FF -> %s", _fmt_time(new_sec))
    with _lock:
        return jsonify(dict(state))

@app.route("/api/rw", methods=["POST"])
def api_rw():
    global _play_paused, _play_generation
    with _lock:
        if state["status"] not in ("playing", "paused"):
            return jsonify(dict(state))
        fpath    = state.get("filepath")
        play_sec = state.get("play_seconds", 0)

    new_sec = max(0, play_sec - FF_RW_SECONDS)
    _stop_aplay()
    _play_paused = False
    time.sleep(0.3)

    ok = _start_aplay(fpath, start_second=new_sec)
    if ok:
        _play_generation += 1
        gen = _play_generation
        with _lock:
            state["play_seconds"] = new_sec
            state["status"] = "playing"
        t = threading.Thread(target=_play_monitor_loop, args=(gen,), daemon=True)
        t.start()

    log.info("RW -> %s", _fmt_time(new_sec))
    with _lock:
        return jsonify(dict(state))

@app.route("/api/files")
def api_files():
    files = []
    for f in sorted(RECORDINGS_DIR.glob("*.wav"),
                    key=lambda x: x.stat().st_mtime,
                    reverse=True):
        st = f.stat()
        dur = _wav_duration(str(f))
        files.append({
            "name":     f.name,
            "size":     _fmt_size(st.st_size),
            "size_b":   st.st_size,
            "date":     datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "duration": _fmt_time(dur),
        })
    return jsonify(files)

@app.route("/api/files/<filename>")
def api_download(filename):
    fpath = RECORDINGS_DIR / filename
    if not fpath.exists():
        abort(404)
    return send_file(str(fpath), as_attachment=True)

@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    _stop_arecord()
    _stop_aplay()
    _stop_monitor()
    log.info("SHUTDOWN spegnimento in corso")
    threading.Thread(
        target=lambda: (time.sleep(2), os.system("sudo shutdown -h now")),
        daemon=True
    ).start()
    return jsonify({"status": "shutting down"})

# --- AVVIO -----------------------------------------------------------

if __name__ == "__main__":
    log.info("=" * 50)
    log.info("  GrooveBox Server v2.2 - Pi 4")
    log.info("  Input:    PipeWire -> %s", PW_SOURCE)
    log.info("  Output:   %s", PLAYBACK_CARD)
    log.info("  Monitor:  SEMPRE ATTIVO")
    log.info("  Files:    %s", RECORDINGS_DIR)
    log.info("  Rate:     %sHz | %s", SAMPLE_RATE, BIT_DEPTH)
    log.info("=" * 50)
    for i in range(3):
        _start_monitor()
        if _monitor_proc and _monitor_proc.poll() is None:
            break
        log.warning("MONITOR retry %d/3...", i + 1)
        time.sleep(3)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
