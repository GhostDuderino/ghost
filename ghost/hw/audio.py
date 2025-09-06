import os
import subprocess
import threading
import logging
import shlex

SIM = os.getenv("GHOST_SIM") == "1"
log = logging.getLogger("GHOST.hw.audio")

# Single active player process guard
_play_lock = threading.Lock()
_play_proc = None

def _stop_current_locked():
    global _play_proc
    if _play_proc and _play_proc.poll() is None:
        try:
            _play_proc.terminate()
            try:
                _play_proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                _play_proc.kill()
        except Exception:
            pass
    _play_proc = None

def stop():
    """Stop any currently playing audio."""
    with _play_lock:
        _stop_current_locked()

def set_volume(level: int):
    """Set output volume. On Pi -> amixer PCM. On SIM -> just log (use Mac volume keys)."""
    if SIM:
        log.info(f"[SIM] set_volume({level}) (no-op on Mac; use system volume)")
        return
    try:
        # Card 0, control PCM as per your Pi config/services
        subprocess.run(["amixer", "-c", "0", "sset", "PCM", str(level)], check=False)
    except Exception as e:
        log.warning(f"amixer failed: {e}")

def play_wav(path: str):
    """
    Blocking play:
      - SIM (Mac): /usr/bin/afplay <path>
      - Pi:        /usr/bin/aplay -D default <path>
    Only one playback at a time; starting a new one stops the old.
    """
    global _play_proc
    with _play_lock:
        # stop any previous sound first
        _stop_current_locked()

        if SIM:
            cmd = ["/usr/bin/afplay", path]
        else:
            cmd = ["/usr/bin/aplay", "-D", "default", path]

        log.info(f"[audio] {' '.join(shlex.quote(c) for c in cmd)}")
        try:
            _play_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except FileNotFoundError as e:
            # Fallback: try without absolute path (PATH may have it)
            try:
                _play_proc = subprocess.Popen([cmd[0].split('/')[-1], *cmd[1:]],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except Exception as e2:
                log.warning(f"Audio start failed: {e2}")
                _play_proc = None
                return
        except Exception as e:
            log.warning(f"Audio start failed: {e}")
            _play_proc = None
            return

    # wait outside the lock so others can call stop()
    rc = None
    try:
        rc = _play_proc.wait()
    except Exception:
        pass
    finally:
        with _play_lock:
            # harvest stderr if nonzero
            try:
                if _play_proc:
                    _, err = _play_proc.communicate(timeout=0.1)
                    if rc not in (None, 0) and err:
                        log.debug(f"[audio stderr] {err.decode(errors='ignore').strip()}")
            except Exception:
                pass
            _play_proc = None
