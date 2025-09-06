import sys
import os
# Use dummy audio on Mac SIM so pygame doesn't error; Pi will override via amixer/aplay.
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

# Decide SIM right away (used by conditional imports below)
SIM = os.getenv("GHOST_SIM") == "1"
if SIM:
    os.environ["SDL_AUDIODRIVER"] = "dummy"

import time
import random
import threading
import logging
import logging.handlers
from pathlib import Path
from PIL import Image
import subprocess
import queue

# hardware abstraction layers
from ghost.hw import audio as hw_audio
from ghost.hw import display as hw_display
from ghost.hw import buttons as hw_buttons

# Only import pygame & RPi.GPIO on the Pi (avoid on Mac/SIM)
if not SIM:
    import pygame
    import RPi.GPIO as GPIO

if SIM:
    # simple shim so references don't NameError in type hints/logs
    class _GPIOShim: pass
    GPIO = _GPIOShim()

# ──────────────────────────────────────────────────────────────────────────────
# Low-level I2S guard: ensure GPIO18 is the I2S BCLK (ALT0) before playing
# ──────────────────────────────────────────────────────────────────────────────
def ensure_bclk():
    if SIM:
        return
    try:
        out = subprocess.check_output(["pinctrl", "get", "18"], text=True)
        if "a0" not in out.lower():
            subprocess.run(["pinctrl", "set", "18", "a0"], check=False)
            logging.info("Forced GPIO18 to ALT0 (PCM_CLK)")
    except Exception as e:
        logging.debug(f"ensure_bclk skipped: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Audio backend selection
# ──────────────────────────────────────────────────────────────────────────────
USE_APLAY = True           # ← use aplay (known-good)
APLAY_DEVICE = "hw:0,0"    # ← matches your working command
current_volume = 255  # Full volume at boot
VOLUME_HIGH = 255
VOLUME_LOW = 100

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
log_dir = Path(__file__).parent / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
log_path = log_dir / "ghost.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(log_path, maxBytes=10_000_000, backupCount=1),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("GHOST")

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────
#Animation
FRAME_RATE = 0.083
IDLE_TIMEOUT = 300

# Event engine constants
MIN_PULSE_MS   = 20      # ignore pulses shorter than this (debounce/gold-plating)
TAP_DECISION   = 0.70    # wait this long after release before deciding single/double/5x
HOLD_MS_BY_BTN = {"B1": 800, "B2": 3000}  # ms: B2 requires a 3s hold
CHORD_GRACE_MS = 180     # window to consider near-simultaneous press as a chord
last_chord_time = 0.0    # Track timing of last chord to suppress late tap decisions
CHORD_TAP_SUPPRESS_WINDOW = 0.75  # seconds

#Volume control
def set_pcm_volume(value: int):
    try:
        hw_audio.set_volume(value)
        log.info(f"PCM volume set to {value}")
    except Exception as e:
        log.warning(f"Failed to set PCM volume via hw adapter: {e}")

set_pcm_volume(current_volume)

# ──────────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────────
ASSETS = Path(__file__).parent / "assets"
AUDIO_PATH = ASSETS / "audio"
ANIM_PATH = ASSETS / "animations"

# ──────────────────────────────────────────────────────────────────────────────
# Audio init (adapter handles playback on Mac via afplay, on Pi via aplay)
# ──────────────────────────────────────────────────────────────────────────────
try:
    log.info(f"SDL_AUDIODRIVER={os.environ.get('SDL_AUDIODRIVER')}")
    log.info(f"AUDIODEV={os.environ.get('AUDIODEV')}")
    log.info("Audio backend: ghost.hw.audio (SIM→afplay, Pi→aplay)")
except Exception as e:
    log.error(f"Audio init log failed: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Display
# ──────────────────────────────────────────────────────────────────────────────
# serial = spi(port=0, device=0, gpio_DC=25, gpio_RST=24, bus_speed_hz=40000000)
# device = st7789(serial_interface=serial, width=240, height=240, rotate=0)
#above is old way

# ──────────────────────────────────────────────────────────────────────────────
# GPIO
# ──────────────────────────────────────────────────────────────────────────────
if not SIM:
    GPIO.setmode(GPIO.BCM)
    BUTTON1_PIN = 17
    BUTTON2_PIN = 27
    GPIO.setup(BUTTON1_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(BUTTON2_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    BACKLIGHT_PIN = 23
    GPIO.setup(BACKLIGHT_PIN, GPIO.OUT)
    GPIO.output(BACKLIGHT_PIN, GPIO.HIGH)
else:
    # Dummy placeholders so references don’t crash in SIM path
    BUTTON1_PIN = 17
    BUTTON2_PIN = 27
    BACKLIGHT_PIN = 23

# ──────────────────────────────────────────────────────────────────────────────
# Globals
# ──────────────────────────────────────────────────────────────────────────────
interrupt_requested = threading.Event()
shutting_down = threading.Event()
preloaded = {}

# only allow one aplay at a time (prevents rare overlap / device busy)
aplay_lock = threading.Lock()

# unified button event queue
event_queue = queue.Queue()

# Track last time each button triggered a HOLD (used to suppress immediate 5-tap shutdown)
last_hold_time = {"B1": 0.0, "B2": 0.0}

# ──────────────────────────────────────────────────────────────────────────────
# Soft-shuffle helper: allows repeats but biases toward unheard/underplayed items
#   - Maintains a small recent-history window to avoid immediate repeats
#   - Increases weight the longer an item goes unplayed and the fewer total plays
#   - You can "observe" an externally-forced pick (e.g., billy.wav first) so
#     the internal history stays consistent
# ──────────────────────────────────────────────────────────────────────────────
class WeightedHistoryShuffler:
    def __init__(
        self,
        items,
        recent_window: int = 2,
        unheard_boost: float = 2.0,
        age_boost: float = 0.15,
        recent_penalty: float = 0.10,
        min_weight: float = 0.05,
    ):
        self.items = list(items)
        if not self.items:
            raise ValueError("Shuffler has no items")

        # play tracking
        self._plays = {i: 0 for i in self.items}
        self._last_pick_idx = {i: -10**9 for i in self.items}  # "long ago"
        self._pick_counter = 0

        # history window to prevent immediate repeats
        from collections import deque
        self._recent = deque(maxlen=recent_window)

        # tuning knobs
        self._unheard_boost = unheard_boost
        self._age_boost = age_boost
        self._recent_penalty = recent_penalty
        self._min_weight = min_weight

    def _weight(self, item: str) -> float:
        # age in "picks since last played"
        age = max(0, self._pick_counter - self._last_pick_idx[item])
        unheard = 1.0 if self._plays[item] == 0 else 0.0
        # base preference: inverse with plays so underplayed items get love
        base = 1.0 / (1.0 + self._plays[item])

        # build weight
        w = base * (1.0 + self._unheard_boost * unheard) * (1.0 + self._age_boost * age)

        # small penalty if the item is in the recent window
        if item in self._recent:
            w *= max(0.0, 1.0 - self._recent_penalty)

        # never drop to zero
        return max(self._min_weight, w)

    def _note_pick(self, item: str):
        self._plays[item] += 1
        self._last_pick_idx[item] = self._pick_counter
        self._recent.append(item)
        self._pick_counter += 1

    def observe(self, item: str):
        """Record an externally chosen item as if it were picked (keeps history sane)."""
        if item not in self._plays:
            raise ValueError(f"Unknown item observed: {item}")
        self._note_pick(item)

    def next(self) -> str:
        weights = [self._weight(i) for i in self.items]
        choice = random.choices(self.items, weights=weights, k=1)[0]
        self._note_pick(choice)
        return choice

# ──────────────────────────────────────────────────────────────────────────────
# Assets
# ──────────────────────────────────────────────────────────────────────────────
def preload_idle_assets():
    log.info("Preloading IdleState assets")
    preloaded["idle_animator"] = SpriteAnimator(ANIM_PATH / "idle.bmp", 5, 36)

def preload_sing_assets():
    log.info("Preloading SingState assets")
    songs = [
    "songs/erismorn.wav", "songs/closingtime.wav", "songs/mulan.wav", "songs/onmyown.wav",
    "songs/dreadnaught.wav", "songs/boomsmash.wav", "songs/billy.wav", "songs/awokenqueen.wav"
]
    preloaded["sing_files"] = songs
    preloaded["sing_shuffle"] = preloaded.get("sing_shuffle") or WeightedHistoryShuffler(
        songs,
        recent_window=2,     # avoids immediate repeats
        unheard_boost=2.0,   # strong push to hear unplayed tracks
        age_boost=0.15,      # gentle push the longer it's been
        recent_penalty=0.10, # small penalty to items heard very recently
    )
    # Flag to force billy.wav first-after-boot
    if "sing_first_done" not in preloaded:
        preloaded["sing_first_done"] = False


    if not USE_APLAY:
        preloaded["sing_sounds"] = {
            name: pygame.mixer.Sound(str(AUDIO_PATH / name)) for name in songs
        }
    log.info(f"Sing assets ready: {len(songs)} file(s), shuffled playback")

def preload_quip_assets():
    log.info("Preloading QuipState assets")
    preloaded["quip_animator"] = SpriteAnimator(ANIM_PATH / "active.bmp", 10, 36)

    # List your quip files here
    quip_files = [
    "quips/shaxlasers.wav", "quips/smells.wav", "quips/wewin.wav", "quips/decapitation.wav",
    "quips/lesssorry.wav", "quips/vextrap.wav", "quips/scarymonster.wav", "quips/staydown.wav", "quips/cruciblenext.wav",
    "quips/noarms.wav", "quips/elevator.wav", "quips/occamlaser.wav", "quips/victorydance.wav", "quips/setup.wav",
    "quips/zavalalove.wav", "quips/handlandslaser.wav", "quips/view.wav", "quips/allyourlimbs.wav"
]
    preloaded["quip_files"] = quip_files

    preloaded["quip_audio_shuffle"] = preloaded.get("quip_audio_shuffle") or WeightedHistoryShuffler(
        quip_files,
        recent_window=2,
        unheard_boost=1.5,
        age_boost=0.12,
        recent_penalty=0.10,
    )
    preloaded["quip_col_shuffle"]   = preloaded.get("quip_col_shuffle")   or WeightedHistoryShuffler(
        list(range(10)),
        recent_window=1,    # columns can repeat a bit more freely
        unheard_boost=0.0,  # columns aren't "unheard" in audio sense
        age_boost=0.08,
        recent_penalty=0.10,
    )

    # For pygame path only, you can optionally pre-load Sound objects (not needed for aplay)
    if not USE_APLAY:
        preloaded["quip_sounds"] = {
            name: pygame.mixer.Sound(str(AUDIO_PATH / name)) for name in quip_files
        }

    log.info(f"Quip assets ready: {len(quip_files)} files, 10 columns, shuffled playback")

def preload_result_assets():
    log.info("Preloading dice result sheet")
    preloaded["result_animator"] = SpriteAnimator(
        ANIM_PATH / "results.bmp",
        columns=100,
        frames_per_column=7  # 0..6
    )

# ──────────────────────────────────────────────────────────────────────────────
# Animator with trimmed logs
# ──────────────────────────────────────────────────────────────────────────────
class SpriteAnimator:
    def __init__(self, bmp_path, columns, frames_per_column):
        self.image = Image.open(bmp_path).convert("RGB")
        self.frame_width = 240
        self.frame_height = 240
        self.columns = columns
        self.frames_per_column = frames_per_column
        self.filename = os.path.basename(bmp_path)

    def get_frame(self, col, row):
        x = col * self.frame_width
        y = row * self.frame_height
        return self.image.crop((x, y, x + self.frame_width, y + self.frame_height))

    def play_column(self, col, start=0, end=None, loop=False, interruptable=False, hold_last=False):
        if end is None:
            end = self.frames_per_column
        index = start
        # one-line log per call
        log.info(f"[anim {self.filename}] col={col} range={start}->{end} loop={loop} interruptable={interruptable}")
        while not interrupt_requested.is_set():
            frame = self.get_frame(col, index)
            hw_display.show_image(device, frame)
            # keep the SIM responsive (must be main thread)
            if SIM:
                pump_sim_inputs_once()
                dispatch_events()
            time.sleep(FRAME_RATE)
            index += 1
            if index >= end:
                if loop:
                    index = start
                else:
                    break
            if interruptable and interrupt_requested.is_set():
                break
        if hold_last and index > 0:
            frame = self.get_frame(col, index - 1)
            hw_display.show_image(device, frame)
        return index

# ──────────────────────────────────────────────────────────────────────────────
# Audio
# ──────────────────────────────────────────────────────────────────────────────
def play_audio(filename, interruptable=False):
    """
    Unified playback through the hardware adapter.
    - SIM (Mac): afplay
    - Pi: aplay
    """
    path = AUDIO_PATH / filename
    if not path.exists():
        log.warning(f"Audio file not found: {path}")
        return
    if not SIM:
        ensure_bclk()
    try:
        hw_audio.play_wav(str(path))  # SIM→afplay, Pi→aplay
        return
    except Exception as e:
        log.warning(f"Audio playback failed via hw adapter: {e}")

    # pygame fallback ONLY on Pi
    if not SIM:
        try:
            sound = pygame.mixer.Sound(str(path))
            sound.set_volume(1.0)
            audio_channel.set_volume(1.0)
            audio_channel.play(sound)
            while audio_channel.get_busy():
                if interruptable and interrupt_requested.is_set():
                    log.info("Audio interrupted (pygame)")
                    audio_channel.stop()
                    return
                time.sleep(0.05)
            log.info("Audio finished (pygame)")
        except Exception as e:
            log.error(f"pygame playback failed: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# State base & manager
# ──────────────────────────────────────────────────────────────────────────────
class State:
    def run(self):
        pass
    def handle_event(self, evt: str):
        pass

class StateManager:
    def __init__(self):
        self.state = None
        self.thread = None
        self.lock = threading.Lock()
        self.next_state = None
        self._inline_running = False

    def set_state(self, state_cls):
        with self.lock:
            interrupt_requested.set()
            if self.thread and self.thread.is_alive() and threading.current_thread() != self.thread:
                log.info(f"Waiting for {self.state.__class__.__name__} to stop")
                self.thread.join()
            interrupt_requested.clear()

            # Drop queued events
            try:
                while True:
                    event_queue.get_nowait()
            except Exception:
                pass

            log.info(f"Entering {state_cls.__name__}")
            self.state = state_cls()

            if SIM:
                # Don't start a thread on macOS — run on the main thread
                self.thread = None
            else:
                self.thread = threading.Thread(target=self.state.run, daemon=True)
                self.thread.start()

manager = StateManager()

# ──────────────────────────────────────────────────────────────────────────────
# States
# ──────────────────────────────────────────────────────────────────────────────
class BootState(State):
    def run(self):
        ensure_bclk()
        animator = SpriteAnimator(ANIM_PATH / "awaken.bmp", 1, 36)

        # play audio & animation in parallel
        def anim():
            animator.play_column(0)

        def audio():
            play_audio("quips/eyesupguardian.wav", interruptable=False)

        log.info("Boot: starting audio + animation concurrently")
        t_audio = threading.Thread(target=audio, daemon=True)
        t_audio.start()
        if SIM:
            # Animate inline on main thread
            animator.play_column(0)
            t_audio.join()
        else:
            # Pi: thread is fine
            t_anim = threading.Thread(target=lambda: animator.play_column(0), daemon=True)
            t_anim.start()
            t_anim.join()
            t_audio.join()

        manager.next_state = IdleState

class IdleState(State):
    def run(self):
        if "idle_animator" not in preloaded:
            log.warning("IdleState assets not preloaded — loading now")
            preload_idle_assets()

        animator = preloaded["idle_animator"]
        while not interrupt_requested.is_set():
            col = random.randint(0, 4)
            log.info(f"Idle: choosing column {col}")
            animator.play_column(col, interruptable=True)

    def handle_event(self, evt: str):
        if evt == "B1_TAP":
            log.info("Dispatcher: Idle + B1_TAP → SingState")
            manager.set_state(SingState)
        elif evt == "B2_TAP":
            log.info("Dispatcher: Idle + B2_TAP → QuipState")
            manager.set_state(QuipState)

class SingState(State):
    def run(self):
        log.info("Entering Sing state")

        # Make sure assets/shuffler are ready
        if "sing_shuffle" not in preloaded:
            log.warning("Sing assets not preloaded — loading now")
            preload_sing_assets()

        # First pick after boot: force billy.wav, then hand control to soft-shuffler
        if not preloaded.get("sing_first_done", False):
            name = "songs/billy.wav"
            preloaded["sing_first_done"] = True
            # Tell the shuffler we "played" billy so it won't immediately repeat
            try:
                preloaded["sing_shuffle"].observe(name)
            except Exception:
                pass
        else:
            name = preloaded["sing_shuffle"].next()
        log.info(f"Sing: selected song '{name}'")

        # Use dedicated animator for singing
        animator = SpriteAnimator(ANIM_PATH / "sing.bmp", 6, 36)

        audio_done = threading.Event()
        column_lock = threading.Lock()
        current_col = [random.randint(0, 5)]
        frame_index = 0

        def animate():
            nonlocal frame_index
            while not interrupt_requested.is_set() and not audio_done.is_set():
                with column_lock:
                    current_col[0] = random.randint(0, 5)
                    log.info(f"Sing: choosing column {current_col[0]}")
                    frame_index = 0
                returned_index = animator.play_column(
                    current_col[0],
                    start=frame_index,
                    end=animator.frames_per_column,
                    interruptable=True
                )
                frame_index = min(returned_index, animator.frames_per_column)

        def play_audio_and_flag():
            log.info(f"Playing song: {name}")
            play_audio(name, interruptable=True)
            if not interrupt_requested.is_set():
                audio_done.set()

        # Start animation + audio
        audio_thread = threading.Thread(target=play_audio_and_flag, daemon=True)
        audio_thread.start()
        if SIM:
            # Animate inline on main thread while audio plays
            while not interrupt_requested.is_set() and not audio_done.is_set():
                with column_lock:
                    current_col[0] = random.randint(0, 5)
                    log.info(f"Sing: choosing column {current_col[0]}")
                    frame_index = 0
                returned_index = animator.play_column(
                    current_col[0],
                    start=frame_index,
                    end=animator.frames_per_column,
                    interruptable=True
                )
                frame_index = min(returned_index, animator.frames_per_column)
            audio_thread.join()
        else:
            anim_thread = threading.Thread(target=animate, daemon=True)
            anim_thread.start()
            audio_thread.join()
            anim_thread.join()

        if interrupt_requested.is_set():
            log.info("Interrupt detected in SingState — exiting early")
            manager.next_state = IdleState
            return

        # Play the remaining frames in the last column
        with column_lock:
            animator.play_column(
                current_col[0],
                start=frame_index,
                end=animator.frames_per_column,
                interruptable=False
            )

        manager.next_state = IdleState

class SleepState(State):
    """
    Fully unified (event-driven) sleep:
      - Enter: dispatcher routes B1_5TAP from Idle into SleepState
      - While asleep: input_monitor keeps posting normal events (no special-casing)
      - Wake: either 5 taps are recognized by input_monitor -> B1_5TAP, or
              we count B1_TAPs locally and wake on 5.
    """
    def __init__(self):
        self.wake_taps = 0
        self.last_tap_time = 0.0
        self.sleep_tap_window = 0.6
        self.settle_until = time.monotonic() + 0.5  # ignore edges that caused entry. OG .25
        self.wake_requested = False

    def run(self):
        global device

        log.info("Entering SleepState — turning off backlight")
        try:
            GPIO.output(BACKLIGHT_PIN, GPIO.LOW)
        except Exception:
            pass

        # Do nothing here—just stay alive until we get a wake event.
        # All wake logic happens in handle_event().
        while not shutting_down.is_set() and not self.wake_requested:
            time.sleep(0.05)
        
        log.info("SleepState exiting — handoff complete")

    def _wake(self, source: str):
        if self.wake_requested:
            log.info(f"Ignoring duplicate wake request via {source}")
            return  # Already waking or shutting down
        self.wake_requested = True


        log.info(f"Waking up from SleepState via {source} — turning on backlight and rebooting UI")

        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(BACKLIGHT_PIN, GPIO.OUT)
            GPIO.output(BACKLIGHT_PIN, GPIO.HIGH)
        except Exception as e:
            log.debug(f"SleepState: backlight re-assert failed: {e}")

        global device
        try:
            try:
                if device and hasattr(device, "cleanup"):
                    device.cleanup()
            except Exception as e:
                log.debug(f"SleepState: device.cleanup() skipped: {e}")

            device = hw_display.init_display()
        except Exception as e:
            log.warning(f"SleepState: display reinit failed: {e}")

        interrupt_requested.set()
        manager.next_state = BootState

    def handle_event(self, evt: str):
        # Ignore any events for a brief moment after entering sleep
        if time.monotonic() < self.settle_until:
            return

        # Direct wake if the event engine already recognized a 5-tap
        if evt == "B1_5TAP":
            self._wake("B1_5TAP")
            return

        # Otherwise, we also support counting single taps locally while asleep
        if evt == "B1_TAP":
            now = time.monotonic()
            if now - self.last_tap_time < self.sleep_tap_window:
                self.wake_taps += 1
            else:
                self.wake_taps = 1
            self.last_tap_time = now
            log.info(f"Sleep tap {self.wake_taps}/5")
            if self.wake_taps >= 5:
                self._wake("5×B1_TAP")
            return

        # Ignore everything else while sleeping
        # (e.g., chords, B2 taps/doubles) to prevent accidental wake

class ShutdownState(State):
    def __init__(self):
        self.shutdown_requested = False

    def run(self):
        log.info("Entering ShutdownState — turning off backlight and shutting down system safely")

        # Initial backlight off
        try:
            GPIO.output(BACKLIGHT_PIN, GPIO.LOW)
        except Exception:
            pass

        if not self.shutdown_requested:
            self.shutdown_requested = True
            try:
                # Pulse RESET pin (assumed GPIO 24)
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(24, GPIO.OUT)
                GPIO.output(24, GPIO.LOW)
                time.sleep(0.1)
                GPIO.output(24, GPIO.HIGH)
                log.info("Display RESET pin pulsed to blank screen before shutdown")
            except Exception as e:
                log.warning(f"Could not reset display: {e}")

            try:
                # Redundant backlight off again (after RESET, in case it turned on)
                GPIO.output(BACKLIGHT_PIN, GPIO.LOW)
                log.info("Backlight explicitly turned off again before shutdown")
            except Exception as e:
                log.warning(f"Second backlight-off failed: {e}")

            try:
                log.info("Executing system shutdown...")
                os.system("sudo shutdown now")
            except Exception as e:
                log.error(f"Shutdown command failed: {e}")

        # Stay alive until the system shuts down (or interrupted)
        while not shutting_down.is_set():
            time.sleep(0.1)

        log.info("ShutdownState exiting (should never happen unless interrupted)")

class StoryState(State):
    """
    Uses the same active.bmp animator as Quip via preloaded['quip_animator'].
    Plays: assets/audio/fx/story.wav
    Behavior: shuffle columns repeatedly while audio plays; when audio ends, finish the current column and return to Idle.
    """
    def run(self):
        log.info("Entering StoryState")

        if "quip_animator" not in preloaded:
            log.warning("StoryState: quip_animator not preloaded — loading now")
            preload_quip_assets()

        animator = preloaded["quip_animator"]

        audio_done = threading.Event()
        current_col = [random.randint(0, animator.columns - 1)]
        frame_index = 0

        def animate():
            nonlocal frame_index
            # Keep playing columns until audio_done is set
            while not interrupt_requested.is_set() and not audio_done.is_set():
                frame_index = 0
                col = current_col[0]
                frame_index = animator.play_column(
                    col,
                    start=0,
                    end=animator.frames_per_column,
                    interruptable=True
                )
                if audio_done.is_set() or interrupt_requested.is_set():
                    break
                # pick a new column for the next loop
                current_col[0] = random.randint(0, animator.columns - 1)

            # Audio ended (or was interrupted): finish the current column cleanly
            if not interrupt_requested.is_set() and frame_index < animator.frames_per_column:
                animator.play_column(
                    current_col[0],
                    start=frame_index,
                    end=animator.frames_per_column,
                    interruptable=False
                )

        def play_and_flag():
            play_audio("fx/story.wav", interruptable=True)
            if not interrupt_requested.is_set():
                audio_done.set()

        t_anim = threading.Thread(target=animate, daemon=True)
        t_audio = threading.Thread(target=play_and_flag, daemon=True)
        t_anim.start()
        t_audio.start()
        t_audio.join()
        t_anim.join()

        manager.next_state = IdleState

class DiceState(State):
    # Map selected_index → max value for that die
    MAX_VALS = [20, 12, 10, 8, 6, 4, 100]

    def __init__(self):
        # --- core data ---
        self.dice_names = ["d20", "d12", "d10", "d8", "d6", "d4", "d100"]
        self.selected_index = 0
        self.mode = "selection"          # "selection" or "result"
        self.last_idle_log = 0.0
        self.last_result = None          # keep last rolled value for context/logs

        # --- settle window to ignore stale edges from the entry chord ---
        self.settle_until = time.monotonic() + 0.25

        # --- assets ---
        if "result_animator" not in preloaded:
            preload_result_assets()
        # dice.bmp has 23 frames/col: 0..22 (0..3 preview, 4..22 roll)
        self.dice_animator = SpriteAnimator(
            ANIM_PATH / "dice.bmp",
            columns=7,
            frames_per_column=23
        )
        # results.bmp preloaded to 7 frames/col: 0..6
        self.result_animator = preloaded["result_animator"]

    def _show_selection(self):
        log.info(f"Dice: preview column {self.selected_index}")
        # Preview = frames 0..3, hold last
        self.dice_animator.play_column(
            self.selected_index,
            start=0,
            end=4,
            interruptable=False,
            hold_last=True
        )

    def _play_result_strip(self, start_frame=3):
        """
        Play the result strip from start_frame through frame 6 (inclusive).
        - start_frame=0 → play full strip 0..6 (would be used if you wanted full replay)
        - start_frame=3 → play tail 3..6 (used before returning to selection or reroll)
        """
        if self.last_result is None:
            return
        col = self.last_result - 1  # 1..100 → 0..99
        start = 0 if start_frame <= 0 else (3 if start_frame <= 3 else start_frame)
        log.info(f"Dice: playing result strip (frames {start}..6)")
        self.result_animator.play_column(
            col,
            start=start,
            end=7,             # end exclusive → plays up to frame 6
            interruptable=False,
            hold_last=False
        )

    def _roll_current(self):
        """
        Rolls the currently selected die:
        - Plays roll sfx (fx/roll.wav)
        - Plays dice.bmp frames 4..22 for the selected column
        - Shows results.bmp frames 0..3 (hold on 3)
        - Enters 'result' mode
        """
        name = self.dice_names[self.selected_index]
        log.info(f"Dice: rolling {name}")

        audio_done = threading.Event()

        def play_roll_audio():
            play_audio("fx/roll.wav", interruptable=True)
            audio_done.set()

        audio_thread = threading.Thread(target=play_roll_audio, daemon=True)
        audio_thread.start()

        # Animate roll (frames 4..22). If user interrupts, bail gracefully.
        log.info(f"Dice: rolling anim col {self.selected_index} (4->23)")
        self.dice_animator.play_column(
            self.selected_index,
            start=4,
            end=23,                # 0..22 → end=23
            interruptable=True
        )

        audio_thread.join()
        if interrupt_requested.is_set():
            log.info("Dice: interrupted during roll → remain in selection")
            self.mode = "selection"
            self._show_selection()
            return

        # Decide a result and show first half (0..3, hold)
        self.last_result = random.randint(1, self.MAX_VALS[self.selected_index])
        log.info(f"Dice: displaying result {self.last_result} (frames 0..3, hold)")
        self.result_animator.play_column(
            self.last_result - 1,  # column
            start=0,
            end=4,                  # show 0..3 and hold on 3
            interruptable=False,
            hold_last=True
        )
        self.mode = "result"

        # --- Result SFX: fire when result is actually shown ---
        try:
            if self.last_result == 1:
                log.info("Dice: rolled a 1 → guardian down SFX")
                play_audio("fx/guardiandown.wav", interruptable=True)
            elif self.last_result == self.MAX_VALS[self.selected_index]:
                log.info("Dice: rolled max → crit SFX")
                play_audio("fx/crit.wav", interruptable=True)
        except Exception as e:
            log.warning(f"Result SFX failed: {e}")


    def run(self):
        log.info("Entering Dice Mode")
        # show the initial selection immediately
        self.mode = "selection"
        self._show_selection()

        # idle loop just breathes; events come via handle_event()
        while not interrupt_requested.is_set():
            now = time.monotonic()
            if now - self.last_idle_log >= 1.0:
                log.info(f"Dice: waiting for input… (mode={self.mode}, die={self.dice_names[self.selected_index]})")
                self.last_idle_log = now
            time.sleep(0.05)

        log.info("Interrupt detected in DiceState — exiting")
        manager.next_state = IdleState

    def handle_event(self, evt: str):
        # ignore events until settle window expires (prevents phantom roll on entry)
        if time.monotonic() < self.settle_until:
            return

        # In DiceState: B1_DOUBLE does nothing; B2_DOUBLE jumps to d20 selection.
        if evt == "B1_DOUBLE":
            log.info("Dice: B1_DOUBLE ignored")
            if interrupt_requested.is_set():
                interrupt_requested.clear()
            return

        if evt == "B2_DOUBLE":
            log.info("Dice: B2_DOUBLE → jump to d20 selection")
            if interrupt_requested.is_set():
                interrupt_requested.clear()
            self.selected_index = 0  # d20
            self.mode = "selection"
            self._show_selection()
            return

        # --- Selection mode actions ---
        if self.mode == "selection":
            if evt == "B1_TAP":
                # Roll the currently selected die and go to result mode
                self._roll_current()
                return

            if evt == "B2_TAP":
                # Cycle to next die (with a quick rewind)
                log.info("Dice: selection → next die")
                for i in reversed(range(4)):  # 3,2,1,0
                    frame = self.dice_animator.get_frame(self.selected_index, i)
                    hw_display.show_image(device, frame)
                    time.sleep(FRAME_RATE)
                self.selected_index = (self.selected_index + 1) % len(self.dice_names)
                log.info(f"Dice: selected {self.dice_names[self.selected_index]}")
                self._show_selection()
                return

            return  # ignore others

        # --- Result mode actions ---
        if self.mode == "result":
            if evt == "B1_TAP":
                # Finish just the tail of the current result (frames 3..6),
                # then start the new roll.
                self._play_result_strip(start_frame=3)
                self._roll_current()
                return

            if evt == "B2_TAP":
                # Finish tail (3..6), then return to selection on the same die.
                self._play_result_strip(start_frame=3)
                log.info("Dice: result → back to selection")
                self.mode = "selection"
                self._show_selection()
                return

            return  # ignore others


class QuipState(State):
    def run(self):
        log.info("Entering Quip state")

        if ("quip_animator" not in preloaded or
            "quip_audio_shuffle" not in preloaded or
            "quip_col_shuffle" not in preloaded):
            log.warning("Quip assets not preloaded — loading now")
            preload_quip_assets()

        animator = preloaded["quip_animator"]
        name = preloaded["quip_audio_shuffle"].next()
        log.info(f"Quip: audio {name}")

        audio_done = threading.Event()
        current_col = [preloaded["quip_col_shuffle"].next()]
        frame_index = 0

        def animate():
            nonlocal frame_index
            while not interrupt_requested.is_set() and not audio_done.is_set():
                frame_index = 0
                col = current_col[0]
                frame_index = animator.play_column(
                    col,
                    start=0,
                    end=animator.frames_per_column,
                    interruptable=True
                )
                if audio_done.is_set() or interrupt_requested.is_set():
                    break
                # next column (no repeats until cycle completes)
                current_col[0] = preloaded["quip_col_shuffle"].next()

            # Finish the current column after audio ends (if mid-column)
            if not interrupt_requested.is_set() and frame_index < animator.frames_per_column:
                animator.play_column(
                    current_col[0],
                    start=frame_index,
                    end=animator.frames_per_column,
                    interruptable=False
                )

        def play_audio_and_flag():
            play_audio(name, interruptable=True)
            if not interrupt_requested.is_set():
                audio_done.set()

        anim_thread = threading.Thread(target=animate, daemon=True)
        audio_thread = threading.Thread(target=play_audio_and_flag, daemon=True)
        audio_thread.start()

        if SIM:
            while not interrupt_requested.is_set() and not audio_done.is_set():
                frame_index = animator.play_column(
                    current_col[0],
                    start=0,
                    end=animator.frames_per_column,
                    interruptable=True
                )
                if audio_done.is_set() or interrupt_requested.is_set():
                    break
                current_col[0] = preloaded["quip_col_shuffle"].next()

            # Finish current column if needed
            if not interrupt_requested.is_set() and frame_index < animator.frames_per_column:
                animator.play_column(
                    current_col[0],
                    start=frame_index,
                    end=animator.frames_per_column,
                    interruptable=False
                )
            audio_thread.join()
        else:
            anim_thread = threading.Thread(target=animate, daemon=True)
            anim_thread.start()
            audio_thread.join()
            anim_thread.join()

        manager.next_state = IdleState

# ──────────────────────────────────────────────────────────────────────────────
# Unified edge-based input → event queue (DEBUG-INSTRUMENTED)
# ──────────────────────────────────────────────────────────────────────────────
def pump_sim_inputs_once():
    """Poll SIM keyboard once (must be called from main thread on macOS)."""
    try:
        ev = hw_buttons.poll_buttons() or {}
        for name, truthy in ev.items():
            if truthy:
                post_event(name)
    except Exception as e:
        log.debug(f"SIM input poll error: {e}")

def post_event(name: str):
    event_queue.put(name)

# --- Edge queue and callback ---
edge_queue = queue.Queue()

def _edge_cb(channel: int):
    # NOTE: pressed == HIGH for NC + PUD_UP wiring
    level_high = GPIO.input(channel) == GPIO.HIGH
    btn = "B1" if channel == BUTTON1_PIN else "B2"
    t = time.monotonic()
    # Queue the logical edge; PRESS when level goes HIGH, RELEASE when LOW
    edge_queue.put((btn, level_high, t))

def _register_edge_callbacks():
    # If a previous run registered events, clear them first
    for pin in (BUTTON1_PIN, BUTTON2_PIN):
        try:
            GPIO.remove_event_detect(pin)
        except Exception:
            pass
    GPIO.add_event_detect(BUTTON1_PIN, GPIO.BOTH, callback=_edge_cb, bouncetime=5)
    GPIO.add_event_detect(BUTTON2_PIN, GPIO.BOTH, callback=_edge_cb, bouncetime=5)

# Do the registration once after GPIO.setup() (only on real Pi)
if not SIM:
    _register_edge_callbacks()

# Return True if a chord was posted or attempted very recently
def chord_recent():
    return (time.monotonic() - last_chord_time) < CHORD_TAP_SUPPRESS_WINDOW

def input_monitor():
    # Per-button timing/state
    press_time = {"B1": None, "B2": None}   # last press-down time (monotonic)
    tap_count  = {"B1": 0,    "B2": 0}      # taps within current decision window
    decide_at  = {"B1": None, "B2": None}   # absolute time when we decide TAP/DOUBLE/5x
    down       = {"B1": False,"B2": False}  # current "pressed" state as seen by our logic

    # Cooldown to suppress taps briefly after a HOLD (prevents spurious 5-tap)
    tap_suppress_until = {"B1": 0.0, "B2": 0.0}
    hold_cooldown_sec = 1.5

    # We also update the global last_hold_time when any HOLD fires
    global last_hold_time

    # Chord helper
    chord_armed = True
    chord_pending = False
    chord_detect_time = None  # for timeout
    CHORD_TIMEOUT = 1.0       # seconds
    global last_chord_time

    # Helper to post with a consistent log
    def emit(evt: str):
        log.info(f"POST {evt}")
        post_event(evt)

    # Decide helper: translate tap_count → event
    def decide(button: str):
        n = tap_count[button]
        tap_count[button] = 0
        decide_at[button] = None
        if button == "B1" and n >= 5:
            log.info(f"DECIDE {button}: {n} taps → B1_5TAP")
            emit("B1_5TAP")
        elif button == "B2" and n >= 5:
            log.info(f"DECIDE {button}: {n} taps → B2_5TAP")
            emit("B2_5TAP")
        elif n >= 2:
            log.info(f"DECIDE {button}: {n} taps → {button}_DOUBLE")
            emit(f"{button}_DOUBLE")
        elif n == 1:
            log.info(f"DECIDE {button}: {n} tap  → {button}_TAP")
            emit(f"{button}_TAP")
        else:
            log.info(f"DECIDE {button}: {n} taps → (nothing)")

    # Throttled raw sampling log (DEBUG only)
    last_raw_log = 0.0
    RAW_LOG_PERIOD = 0.25  # seconds

    while not shutting_down.is_set():
        # Optional raw read (useful to spot wiring/mode issues)
        now = time.monotonic()
        if log.isEnabledFor(logging.DEBUG) and (now - last_raw_log) >= RAW_LOG_PERIOD:
            raw_b1 = GPIO.input(BUTTON1_PIN) == GPIO.HIGH
            raw_b2 = GPIO.input(BUTTON2_PIN) == GPIO.HIGH
            log.debug(f"RAW B1={int(raw_b1)} RAW B2={int(raw_b2)} (1=pressed)")
            last_raw_log = now

        # 1) Drain exactly one queued edge to keep latency low
        try:
            btn, is_press, t = edge_queue.get(timeout=0.05)
        except queue.Empty:
            btn = None

        # 2) Process one edge if present
        if btn:
            other = "B2" if btn == "B1" else "B1"

            if is_press:
                # PRESS edge (rising to HIGH)
                down[btn] = True
                press_time[btn] = t
                log.info(f"PRESS {btn}")

                # Chord detect: both are down, and their press times are close
                if down[other] and press_time[other] is not None:
                    dt = abs(press_time[btn] - press_time[other])
                    if dt <= (CHORD_GRACE_MS / 1000.0) and chord_armed:
                        log.info(f"CHORD pending (dt={dt*1000:.1f}ms) — waiting for release")
                        chord_pending = True
                        chord_detect_time = time.monotonic()
                        chord_armed = False  # disarm to avoid duplicates

            else:
                # RELEASE edge (falling to LOW)
                if press_time[btn] is None:
                    # We missed the press edge somehow; still mark up->down transition
                    if down[btn]:
                        log.warning(f"RELEASE {btn} detected but press_time was None — forcing release")
                    down[btn] = False
                    press_time[btn] = None
                    decide_at[btn] = None
                    tap_count[btn] = 0
                else:
                    # Clamp stale timestamp (can happen around resync)
                    if t < press_time[btn]:
                        log.debug(f"Stale RELEASE for {btn} (edge timestamp < press_time) — clamping")
                        t = press_time[btn]

                    pulse_ms = (t - press_time[btn]) * 1000.0
                    down[btn] = False
                    press_time[btn] = None
                    log.info(f"RELEASE {btn} (pulse={pulse_ms:.1f}ms)")

                    # Re-arm chord once both are up
                    if not down["B1"] and not down["B2"]:
                        if chord_pending:
                            log.info(f"CHORD released → POST B1B2_CHORD")
                            emit("B1B2_CHORD")
                            # Clear pending tap logic — don't let B1/B2 post late taps
                            tap_count["B1"] = 0
                            tap_count["B2"] = 0
                            decide_at["B1"] = None
                            decide_at["B2"] = None

                            # Record time to suppress near-future taps
                            last_chord_time = time.monotonic()

                            chord_pending = False
                            chord_detect_time = None
                        chord_armed = True

                    # Ignore very short blips
                    if pulse_ms < MIN_PULSE_MS:
                        log.info(f"IGNORED {btn}: too short (<{MIN_PULSE_MS}ms)")
                    # HOLD?
                    elif pulse_ms >= HOLD_MS_BY_BTN[btn]:
                        log.info(f"HOLD {btn} detected (≥{HOLD_MS_BY_BTN[btn]}ms)")
                        # Holds cancel any in-flight tap decision for that button
                        tap_count[btn] = 0
                        decide_at[btn] = None

                        # Start tap cooldown & remember last hold time
                        tap_suppress_until[btn] = time.monotonic() + hold_cooldown_sec
                        last_hold_time[btn] = time.monotonic()

                        if btn == "B1":
                            if chord_recent():
                                log.info("Suppressed B1_HOLD — chord was pending or recent")
                            else:
                                emit("B1_HOLD")
                        else:
                            emit(f"{btn}_HOLD")
                    else:
                        # Count a tap and schedule decision — but only if no chord is pending
                        if not chord_pending and not chord_recent():
                            if time.monotonic() >= tap_suppress_until[btn]:
                                tap_count[btn] += 1
                                decide_at[btn] = t + TAP_DECISION
                                log.info(
                                    f"Tap tallied: {btn} total={tap_count[btn]} "
                                    f"(decision in {TAP_DECISION:.2f}s)"
                                )
                            else:
                                log.info(f"Tap suppressed for {btn} — within hold cooldown")
                        else:
                            log.info(f"Suppressed tap tally for {btn} — chord pending")

        # 3) Finalize any deferred tap decisions whose timers expired
        now2 = time.monotonic()
        for b in ("B1", "B2"):
            if decide_at[b] is not None and now2 >= decide_at[b]:
                # Suppress tap if shortly after chord
                if now2 - last_chord_time < CHORD_TAP_SUPPRESS_WINDOW:
                    log.info(f"Suppressed {b}_TAP — within {CHORD_TAP_SUPPRESS_WINDOW*1000:.0f}ms of chord")
                    tap_count[b] = 0
                    decide_at[b] = None
                    continue
                decide(b)

        # 4) Timeout check for chord pending
        if chord_pending and chord_detect_time:
            if time.monotonic() - chord_detect_time > CHORD_TIMEOUT:
                log.info("CHORD timeout — discarding pending chord")
                chord_pending = False
                chord_detect_time = None
                chord_armed = True

        # 4b) Self-healing edge resync (debounced + guarded)
        RESYNC_SAMPLE_MS = 30       # raw mismatch must persist this long
        RESYNC_COOLDOWN_MS = 120    # min gap between synthesized edges per button

        # one-time init on first loop
        if not hasattr(input_monitor, "_resync_init"):
            input_monitor._resync_init = True
            last_raw = {"B1": GPIO.input(BUTTON1_PIN) == GPIO.HIGH,
                        "B2": GPIO.input(BUTTON2_PIN) == GPIO.HIGH}
            raw_stable_since = {"B1": time.monotonic(), "B2": time.monotonic()}
            last_synth_time = {"B1": 0.0, "B2": 0.0}
            setattr(input_monitor, "_last_raw", last_raw)
            setattr(input_monitor, "_raw_stable_since", raw_stable_since)
            setattr(input_monitor, "_last_synth_time", last_synth_time)

        # load refs
        last_raw = getattr(input_monitor, "_last_raw")
        raw_stable_since = getattr(input_monitor, "_raw_stable_since")
        last_synth_time = getattr(input_monitor, "_last_synth_time")

        # update raw stability tracking
        raw_now = {
            "B1": GPIO.input(BUTTON1_PIN) == GPIO.HIGH,
            "B2": GPIO.input(BUTTON2_PIN) == GPIO.HIGH,
        }
        now_mon = time.monotonic()
        for btn in ("B1", "B2"):
            if raw_now[btn] != last_raw[btn]:
                last_raw[btn] = raw_now[btn]
                raw_stable_since[btn] = now_mon  # reset stability timer

        def can_synthesize(btn: str) -> bool:
            # require raw mismatch to be stable long enough and respect cooldown
            stable_ms = (now_mon - raw_stable_since[btn]) * 1000.0
            since_last_syn_ms = (now_mon - last_synth_time[btn]) * 1000.0
            return stable_ms >= RESYNC_SAMPLE_MS and since_last_syn_ms >= RESYNC_COOLDOWN_MS

        for btn in ("B1", "B2"):
            other = "B2" if btn == "B1" else "B1"

            # Synthesize PRESS if raw says pressed but we think it's up
            if raw_now[btn] and not down[btn] and can_synthesize(btn):
                down[btn] = True
                press_time[btn] = now_mon  # may be later than queued RELEASE; guarded below
                last_synth_time[btn] = now_mon
                log.warning(f"SYN-PRESS {btn} (resync)")
                # chord detect window
                if down[other] and press_time[other] is not None:
                    dt = abs(press_time[btn] - press_time[other])
                    if dt <= (CHORD_GRACE_MS / 1000.0) and chord_armed:
                        log.info(f"SYN-CHORD pending (dt={dt*1000:.1f}ms) — waiting for release")
                        chord_pending = True
                        chord_detect_time = now_mon
                        chord_armed = False

            # Synthesize RELEASE if raw says not pressed but we think it's down
            if not raw_now[btn] and down[btn] and can_synthesize(btn):
                t_now = now_mon
                press_at = press_time[btn] if press_time[btn] is not None else t_now
                pulse_ms = max(0.0, (t_now - press_at) * 1000.0)  # clamp; never negative
                down[btn] = False
                last_synth_time[btn] = t_now
                log.warning(f"SYN-RELEASE {btn} (pulse={pulse_ms:.1f}ms resync)")

                # Re-arm chord once both are up
                if not down["B1"] and not down["B2"]:
                    if chord_pending:
                        log.info("SYN-CHORD released → POST B1B2_CHORD")
                        emit("B1B2_CHORD")
                        tap_count["B1"] = 0; tap_count["B2"] = 0
                        decide_at["B1"] = None; decide_at["B2"] = None
                        last_chord_time = time.monotonic()
                        chord_pending = False
                        chord_detect_time = None
                    chord_armed = True

                # Classify the synthesized pulse
                if pulse_ms < MIN_PULSE_MS:
                    log.info(f"SYN-IGNORED {btn}: too short (<{MIN_PULSE_MS}ms)")
                elif pulse_ms >= HOLD_MS_BY_BTN[btn]:
                    log.info(f"SYN-HOLD {btn} detected (≥{HOLD_MS_BY_BTN[btn]}ms)")
                    tap_count[btn] = 0
                    decide_at[btn] = None

                    # Start tap cooldown & remember last hold time
                    tap_suppress_until[btn] = time.monotonic() + hold_cooldown_sec
                    last_hold_time[btn] = time.monotonic()

                    if btn == "B1":
                        if chord_recent():
                            log.info("Suppressed SYN B1_HOLD — chord was pending or recent")
                        else:
                            emit("B1_HOLD")
                    else:
                        emit(f"{btn}_HOLD")
                else:
                    if not chord_pending and not chord_recent():
                        if time.monotonic() >= tap_suppress_until[btn]:
                            tap_count[btn] += 1
                            decide_at[btn] = t_now + TAP_DECISION
                            log.info(f"SYN Tap tallied: {btn} total={tap_count[btn]} (decision in {TAP_DECISION:.2f}s)")
                        else:
                            log.info(f"SYN tap suppressed for {btn} — within hold cooldown")
                    else:
                        log.info(f"SYN: Suppressed tap tally for {btn} — chord pending")
                press_time[btn] = None  # clear after use


# ──────────────────────────────────────────────────────────────────────────────
# Dispatcher: consume events and route to the active state
# ──────────────────────────────────────────────────────────────────────────────
def dispatch_events():
    while True:
        try:
            evt = event_queue.get_nowait()
        except queue.Empty:
            break

        # ── Global actions first (with Sleep-aware guards) ──
        if evt == "B1B2_CHORD":
            # Ignore chords while sleeping; otherwise toggle Dice
            if isinstance(manager.state, SleepState):
                log.info("Dispatcher: chord ignored in SleepState")
                continue
            log.info("Dispatcher: chord B1+B2 → toggle Dice")
            if isinstance(manager.state, DiceState):
                manager.set_state(IdleState)
            else:
                manager.set_state(DiceState)
            continue
        if evt == "B1_HOLD":
            log.info("Dispatcher: B1_HOLD → toggle volume")
            try:
                global current_volume
                current_volume = VOLUME_LOW if current_volume == VOLUME_HIGH else VOLUME_HIGH
                set_pcm_volume(current_volume)
                play_audio("fx/beep.wav", interruptable=False)
            except Exception as e:
                log.warning(f"Volume toggle failed: {e}")
            continue
    
        if evt == "B2_HOLD":
            log.info("Dispatcher: B2_HOLD → StoryState")
            manager.set_state(StoryState)
            continue

        if evt == "B1_5TAP":
            if not isinstance(manager.state, SleepState):
                log.info("Dispatcher: B1 5-tap → SleepState (with SFX)")
                # Stop any current animation/audio, then play the sleep cue.
                interrupt_requested.set()
                try:
                    # Block until SFX completes so the user hears it before the screen goes dark.
                    play_audio("fx/revive.wav", interruptable=False)
                except Exception as e:
                    log.warning(f"Sleep SFX failed: {e}")

                manager.set_state(SleepState)
                continue
            # If already in SleepState, fall through so SleepState.handle_event() can wake

        elif evt == "B2_5TAP":
            # Safety: ignore a shutdown 5‑tap immediately after a B2 hold
            HOLD_TAP_IGNORE_SEC = 2.0
            since_hold = time.monotonic() - last_hold_time.get("B2", 0.0)
            if since_hold < HOLD_TAP_IGNORE_SEC:
                log.info(f"Dispatcher: B2_5TAP ignored (within {HOLD_TAP_IGNORE_SEC:.1f}s of B2_HOLD)")
                continue

            log.info("Dispatcher: B2 5-tap → ShutdownState (with SFX)")
            # Stop any current animation/audio, then play the shutdown cue.
            interrupt_requested.set()
            try:
                # Block until SFX completes so it finishes before shutdown begins.
                play_audio("fx/guardiandown.wav", interruptable=False)
            except Exception as e:
                log.warning(f"Shutdown SFX failed: {e}")

            manager.set_state(ShutdownState)
            continue


        if evt in ("B1_DOUBLE", "B2_DOUBLE"):
            # In DiceState, doubles are handled internally (no global interrupt).
            if isinstance(manager.state, DiceState):
                log.info(f"Dispatcher: {evt} in DiceState → no global interrupt")
            else:
                log.info(f"Dispatcher: {evt} → interrupt_requested")
                interrupt_requested.set()
            # fall-through to state handler


        # ── Forward to the active state's handler ──
        try:
            state = manager.state
            if state:
                state.handle_event(evt)
        except Exception as e:
            log.warning(f"State event handling error for {evt}: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Idle watchdog (unchanged)
# ──────────────────────────────────────────────────────────────────────────────
def idle_watchdog():
    last_action = time.monotonic()
    while not shutting_down.is_set():
        if isinstance(manager.state, IdleState):
            if time.monotonic() - last_action > IDLE_TIMEOUT:
                log.info("Idle timeout — entering SleepState")
                manager.set_state(SleepState)
        else:
            last_action = time.monotonic()
        time.sleep(5)

# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        # Create window (SIM) / init panel (Pi) on main thread
        device = hw_display.init_display()
        manager.set_state(BootState)

        def delayed_preload():
            time.sleep(1)
            preload_idle_assets()
            preload_sing_assets()
            preload_quip_assets()
            preload_result_assets()   # ensure DiceState is instant

        threading.Thread(target=delayed_preload, daemon=True).start()
        if not SIM:
            threading.Thread(target=input_monitor, daemon=True).start()
        try:
            set_pcm_volume(current_volume)
        except Exception as e:
            log.warning(f"Initial volume set failed: {e}")

        # Main scheduler loop
        while not shutting_down.is_set():
            if SIM:
                pump_sim_inputs_once()  # keep SDL happy (must be main thread)
            dispatch_events()

            # Consume any pending state switch first
            if manager.next_state:
                manager.set_state(manager.next_state)
                manager.next_state = None
                try:
                    while True:
                        event_queue.get_nowait()
                except queue.Empty:
                    pass

            # On macOS SIM we don't spawn state threads.
            # Drive the active state's run() inline on the main thread.
            if SIM and manager.state and manager.thread is None and not manager._inline_running:
                manager._inline_running = True
                try:
                    manager.state.run()  # blocks until the state finishes or is interrupted
                finally:
                    manager._inline_running = False

            # Avoid a hot spin when nothing is running inline
            time.sleep(0.01 if SIM else 0.05)

    except KeyboardInterrupt:
        log.info("KeyboardInterrupt received — shutting down")

    finally:
        log.info("Stopping threads and releasing devices")

        # 1) Tell threads/states to stop
        shutting_down.set()
        interrupt_requested.set()
        manager.next_state = None

        # 2) Wait for current state thread to finish (short timeout)
        try:
            if manager.thread and manager.thread.is_alive():
                manager.thread.join(timeout=2.0)
        except Exception:
            pass

        # 3) Stop any playing audio and release ALSA
        try:
            hw_audio.stop()
        except Exception:
            pass

        # 4) Turn off backlight (optional)
        try:
            GPIO.output(BACKLIGHT_PIN, GPIO.LOW)
        except Exception:
            pass

        # 5) Clean up the display before GPIO cleanup
        try:
            if device and hasattr(device, "cleanup"):
                device.cleanup()
        except Exception as e:
            log.warning(f"Device cleanup failed: {e}")

        # 6) Finally, release GPIO
        if not SIM:
            try:
                GPIO.cleanup()
            except Exception:
                pass
