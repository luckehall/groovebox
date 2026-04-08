#!/usr/bin/env python3
"""
GrooveBox Server v2.4 - Pi 4
Logica audio:
- STANDBY: monitor attivo sull'uscita selezionata (jack/bt)
- REC:     monitor attivo sull'uscita selezionata (jack/bt)
- PLAY:    monitor SPENTO, riproduzione sull'uscita selezionata (jack/bt)
- Selettore: JACK / BT (no OFF)
"""

import os
import time
import signal
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, send_file, abort, request

# --- CONFIGURAZIONE ---------------------------------------------------

RECORDINGS_DIR   = Path("/mnt/groovebox/Registrazioni")
PLAYBACK_JACK    = "plughw:CARD=Headphones,DEV=0"
SAMPLE_RATE      = 96000
CHANNELS         = 2
BIT_DEPTH        = "S24_3LE"
BYTES_PER_SAMPLE = 3
BYTES_PER_FRAME  = BYTES_PER_SAMPLE * CHANNELS
WAV_HEADER_SIZE  = 44
FF_RW_SECONDS    = 10

PW_SOURCE  = "alsa_input.usb-Terratec_PhonoPreAmp_iVinyl-00.analog-stereo"
PW_SINK    = "alsa_output.platform-fe00b840.mailbox.stereo-fallback"
PW_SINK_BT = "bluez_output.00_0E_9F_A4_F3_D4.1"

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
_output          = "jack"  # "jack" o "bt"

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

def _active_sink():
    """Restituisce il PipeWire sink attivo in base all'output selezionato."""
    return PW_SINK_BT if _output == "bt" else PW_SINK

# --- MONITOR PIPEWIRE ------------------------------------------------

def _start_monitor():
    """Avvia loopback PipeWire Terratec -> uscita selezionata."""
    global _monitor_proc
    if _monitor_proc and _monitor_proc.poll() is None:
        return
    sink = _active_sink()
    cmd = (
        f'pw-loopback '
        f'--capture-props="node.name={PW_SOURCE}" '
        f'--playback-props="node.name={sink}"'
    )
    print(f"[MONITOR] Avvio ({_output}) -> {sink}")
    _monitor_proc = subprocess.Popen(
        cmd, shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(0.5)
    if _monitor_proc.poll() is not None:
        print("[MONITOR] ERRORE!")
        _monitor_proc = None
    else:
        print("[MONITOR] Attivo.")

def _stop_monitor():
    """Ferma il loopback PipeWire."""
    global _monitor_proc
    if _monitor_proc and _monitor_proc.poll() is None:
        _monitor_proc.terminate()
        try:
            _monitor_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _monitor_proc.kill()
        print("[MONITOR] Fermato.")
    _monitor_proc = None
    subprocess.run(["pkill", "-f", "pw-loopback"], capture_output=True)
    time.sleep(0.3)

def _ensure_monitor():
    """Riavvia il monitor se non e' attivo (solo in STANDBY e REC)."""
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
            print("[REC] pw-record fermato.")
        _rec_proc = None
    subprocess.run(["pkill", "-f", "pw-record"], capture_output=True)
    time.sleep(0.3)

def _start_arecord(filepath):
    """Avvia pw-record via PipeWire a 24bit."""
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
    print(f"[REC] {' '.join(cmd)}")
    _rec_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(0.5)
    if _rec_proc.poll() is not None:
        print("[REC] ERRORE: pw-record terminato subito!")
        _rec_proc = None
        return False
    print("[REC] pw-record avviato (24bit).")
    return True

def _pause_arecord():
    global _rec_proc
    if _rec_proc and _rec_proc.poll() is None:
        _rec_proc.send_signal(signal.SIGSTOP)
        print("[PAUSE] pw-record sospeso.")

def _resume_arecord():
    global _rec_proc
    if _rec_proc and _rec_proc.poll() is None:
        _rec_proc.send_signal(signal.SIGCONT)
        print("[RESUME] pw-record ripreso.")
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
            print("[PLAY] aplay fermato.")
        _play_proc = None
    subprocess.run(["pkill", "-f", "aplay.*Headphones"], capture_output=True)
    subprocess.run(["pkill", "-f", "pw-play"], capture_output=True)
    subprocess.run(["pkill", "-f", "sox.*Registrazioni"], capture_output=True)
    time.sleep(0.3)

def _pause_aplay():
    global _play_proc
    if _play_proc and _play_proc.poll() is None:
        try:
            os.killpg(os.getpgid(_play_proc.pid), signal.SIGSTOP)
        except Exception:
            _play_proc.send_signal(signal.SIGSTOP)
        print("[PAUSE] aplay sospeso.")

def _resume_aplay():
    global _play_proc
    if _play_proc and _play_proc.poll() is None:
        try:
            os.killpg(os.getpgid(_play_proc.pid), signal.SIGCONT)
        except Exception:
            _play_proc.send_signal(signal.SIGCONT)
        print("[RESUME] aplay ripreso.")
        return True
    return False

def _start_aplay(filepath, start_second=0):
    """Avvia riproduzione file WAV sull'uscita selezionata."""
    global _play_proc
    _stop_aplay()
    time.sleep(0.2)

    use_bt = (_output == "bt")

    if start_second == 0:
        if use_bt:
            cmd = ["pw-play", f"--target={PW_SINK_BT}", str(filepath)]
        else:
            cmd = ["aplay", "-D", PLAYBACK_JACK, str(filepath)]
        print(f"[PLAY] {' '.join(cmd)}")
        try:
            _play_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid
            )
            return True
        except Exception as e:
            print(f"[PLAY] Errore: {e}")
            return False
    else:
        if use_bt:
            cmd = (
                f'sox "{filepath}" -t wav -p trim {start_second} | '
                f'pw-play --target={PW_SINK_BT} -'
            )
        else:
            cmd = (
                f'sox "{filepath}" -t raw -r {SAMPLE_RATE} -c {CHANNELS} -e signed -b 16 - '
                f'trim {start_second} | '
                f'aplay -D {PLAYBACK_JACK} -r {SAMPLE_RATE} -c {CHANNELS} -f S16_LE -'
            )
        print(f"[PLAY] FF/RW da {_fmt_time(start_second)} ({_output})")
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
            print(f"[PLAY] Errore: {e}")
            return False

def _play_monitor_loop(generation):
    """Monitora fine riproduzione."""
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
            # Riaccendi monitor dopo fine PLAY
            _ensure_monitor()
            print("[PLAY] Riproduzione terminata.")
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
            print("[RESUME] Registrazione ripresa.")
            return True
    return False

# --- API ENDPOINTS ---------------------------------------------------

@app.route("/")
def index():
    return send_file("groovebox.html")

@app.route("/api/status")
def api_status():
    with _lock:
        s = dict(state)
    s["output"] = _output
    return jsonify(s)

@app.route("/api/output", methods=["POST"])
def api_output():
    """Cambia uscita audio: jack o bt."""
    global _output
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "jack")
    if mode not in ("jack", "bt"):
        mode = "jack"
    _output = mode
    print(f"[OUTPUT] Selezionato: {mode}")

    with _lock:
        current_status = state["status"]

    if current_status == "playing":
        # Durante PLAY: riavvia riproduzione sul nuovo output
        with _lock:
            fpath    = state.get("filepath")
            play_sec = state.get("play_seconds", 0)
        _stop_aplay()
        time.sleep(0.2)
        _start_aplay(fpath, start_second=play_sec)
    else:
        # STANDBY o REC: riavvia monitor sul nuovo output
        _stop_monitor()
        _start_monitor()

    return jsonify({"output": _output})

@app.route("/api/rec", methods=["POST"])
def api_rec():
    with _lock:
        current_status = state["status"]

    if current_status == "paused" and _rec_proc is not None:
        if _resume_from_pause():
            _ensure_monitor()
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
    print(f"[REC] Avviata: {fname}")
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
    print("[STOP]")
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
        print("[PAUSE] Registrazione in pausa.")
        with _lock:
            return jsonify(dict(state))

    if current_status == "paused" and _rec_proc is not None and _play_proc is None:
        if _resume_from_pause():
            _ensure_monitor()
            with _lock:
                return jsonify(dict(state))

    if current_status == "playing":
        _pause_aplay()
        _play_paused = True
        with _lock:
            state["status"] = "paused"
        print("[PAUSE] Riproduzione in pausa.")
        with _lock:
            return jsonify(dict(state))

    if current_status == "paused" and _play_proc is not None:
        if _resume_aplay():
            _play_paused = False
            with _lock:
                state["status"] = "playing"
            print("[RESUME] Riproduzione ripresa.")
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
    _stop_monitor()    # Monitor spento durante PLAY
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
    print(f"[PLAY] Avviato: {filename} ({_output})")
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

    print(f"[FF] -> {_fmt_time(new_sec)}")
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

    print(f"[RW] -> {_fmt_time(new_sec)}")
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

@app.route("/manifest.json")
def manifest():
    return send_file("manifest.json", mimetype="application/json")

@app.route("/icon-192.png")
def icon192():
    return send_file("icon-192.png", mimetype="image/png")

@app.route("/icon-512.png")
def icon512():
    return send_file("icon-512.png", mimetype="image/png")

@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    _stop_arecord()
    _stop_aplay()
    _stop_monitor()
    print("[SHUTDOWN] Spegnimento in corso...")
    threading.Thread(
        target=lambda: (time.sleep(2), os.system("sudo shutdown -h now")),
        daemon=True
    ).start()
    return jsonify({"status": "shutting down"})

# --- AVVIO -----------------------------------------------------------

if __name__ == "__main__":
    print("=" * 50)
    print("  GrooveBox Server v2.4 - Pi 4")
    print(f"  Input:    {PW_SOURCE}")
    print(f"  Jack:     {PLAYBACK_JACK}")
    print(f"  BT:       {PW_SINK_BT}")
    print(f"  Files:    {RECORDINGS_DIR}")
    print(f"  Rate:     {SAMPLE_RATE}Hz | {BIT_DEPTH}")
    print("=" * 50)
    for i in range(3):
        _start_monitor()
        if _monitor_proc and _monitor_proc.poll() is None:
            break
        print(f"[MONITOR] Retry {i+1}/3...")
        time.sleep(3)
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)
