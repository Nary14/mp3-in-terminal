#!/usr/bin/env python3
"""
Terminal Music Grid — v3.1 (Fixed Duplicate Sound)
- Visualiseur RÉEL via parec/pw-record
- Mode arrière-plan optimisé (zéro duplication de son)
- Contrôle via : tmg p, tmg n, tmg q
"""
import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

try:
    import urwid
except ImportError:
    print("pip install urwid")
    sys.exit(1)

try:
    import yt_dlp
except ImportError:
    print("pip install yt-dlp")
    sys.exit(1)

# ── Constantes ────────────────────────────────────────────────────────────────
ARCHIVE_SEARCH_URL    = "https://archive.org/advancedsearch.php"
ARCHIVE_METADATA_URL  = "https://archive.org/metadata"
SOUNDCLOUD_SEARCH_URL = "https://api-v2.soundcloud.com/search/tracks"
DEFAULT_QUERY         = "electronic"
MAX_TRACKS            = 120
SOUNDCLOUD_CLIENT_ID  = os.environ.get("SOUNDCLOUD_CLIENT_ID", "").strip()
CTRL_FIFO             = "/tmp/tmg_ctrl.fifo"
HUD_W                 = 44
HUD_ROWS              = 6

# ── État global ───────────────────────────────────────────────────────────────
tracks         = []
current_track  = None
current_index  = -1
player_process = None
is_paused      = False
visual_frame   = 0
current_query  = DEFAULT_QUERY
current_page   = 1
has_next_page  = False
active_source  = "YouTube"
main_loop      = None
body_walker    = None

# ── Couleurs et Barres ──────────────────────────────────────────────────────
RAINBOW_COLS = ["yellow", "yellow", "brown", "brown", "dark green", "dark green", "dark green", "dark cyan", "dark cyan", "dark blue", "dark blue", "dark magenta", "dark magenta", "dark red", "dark red"]
RAINBOW_DIM = ["brown", "brown", "dark red", "dark red", "dark blue", "dark blue", "dark blue", "dark blue", "dark blue", "dark blue", "dark blue", "dark blue", "dark blue", "dark blue", "dark blue"]
ANSI_RAINBOW = [226, 214, 208, 46, 48, 51, 27, 93, 201, 196]
ANSI_BARS    = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]

def _bar_attr(col_index, num_cols, dim=False):
    idx = int(col_index * (len(RAINBOW_COLS) - 1) / max(1, num_cols - 1))
    idx = max(0, min(len(RAINBOW_COLS) - 1, idx))
    return f"{'ref' if dim else 'bar'}_{idx}"

# ── AudioCapture ──────────────────────────────────────────────────────────────
class AudioCapture:
    CHUNK_BYTES = 4096
    def __init__(self, bars=20):
        self.bars = bars
        self.heights = [0.0] * bars
        self._proc = None
        self._thread = None
        self._active = False
        self._lock = threading.Lock()

    @staticmethod
    def available():
        return bool(shutil.which("parec") or shutil.which("pw-record"))

    def start(self):
        if self._active or not self.available(): return False
        self._active = True
        cmd = ["parec", "--raw", "--rate=44100", "--channels=1", "--format=s16le", "--latency-msec=20", "-d", "@DEFAULT_MONITOR@"] if shutil.which("parec") else ["pw-record", "--rate=44100", "--channels=1", "--format=s16", "--target=auto", "-"]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._active = False
        if self._proc:
            try: self._proc.terminate()
            except: pass
            self._proc = None

    def _read_loop(self):
        try:
            pipe = self._proc.stdout
            while self._active:
                raw = pipe.read(self.CHUNK_BYTES)
                if not raw: break
                n = len(raw) // 2
                samples = struct.unpack(f"<{n}h", raw[:n*2])
                cs = max(1, n // self.bars)
                result = []
                for i in range(self.bars):
                    band = samples[i*cs:(i+1)*cs]
                    rms = math.sqrt(sum(s*s for s in band)/len(band)) if band else 0
                    result.append(min(1.0, rms / 32768.0 * 4.0))
                with self._lock: self.heights = result
        except: pass

    def get(self, num_bars, max_h):
        with self._lock: src = list(self.heights)
        if not any(src): return None
        return [max(1, int(src[min(int(i*len(src)/num_bars), len(src)-1)] * max_h)) for i in range(num_bars)]

audio_capture = AudioCapture(bars=24)

# ── Moteur de Recherche (YouTube / Archive) ───────────────────────────────────
def fetch_tracks(query=DEFAULT_QUERY, page=1):
    offset = (page - 1) * MAX_TRACKS
    ydl_opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{MAX_TRACKS+offset}:{query}", download=False)
        entries = (info.get("entries") or [])[offset:]
    out = []
    for item in entries:
        if not item.get("id"): continue
        out.append({"source": "youtube", "identifier": item["id"], "title": item.get("title", "?"), "artist": item.get("uploader", "?"), "stream_url": None})
    return out, "YouTube", len(out) >= MAX_TRACKS

def resolve_stream_url(track):
    ydl_opts = {"quiet": True, "format": "bestaudio/best"}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={track['identifier']}", download=False)
        return info.get("url")

# ── Player ────────────────────────────────────────────────────────────────────
def stop_player():
    global player_process, is_paused
    if player_process and player_process.poll() is None:
        player_process.terminate()
    player_process = None
    is_paused = False

def toggle_pause(_button=None):
    global is_paused
    if not player_process: return
    if is_paused:
        player_process.send_signal(signal.SIGCONT)
        is_paused = False
        playpause_btn.set_label("⏸ Pause")
    else:
        player_process.send_signal(signal.SIGSTOP)
        is_paused = True
        playpause_btn.set_label("▶ Play")

def play_track(index):
    global current_track, current_index, visual_frame
    if not (0 <= index < len(tracks)): return
    stop_player()
    current_index, current_track, visual_frame = index, tracks[index], 0
    url = current_track.get("stream_url") or resolve_stream_url(current_track)
    current_track["stream_url"] = url
    if url:
        global player_process
        player_process = subprocess.Popen(["cvlc", "--play-and-exit", "--quiet", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        audio_capture.start()
        status_text.set_text(f"▶ {current_track['title']}")
        playpause_btn.set_label("⏸ Pause")

def play_next(_button=None):
    play_track(current_index + 1)
    refresh_grid()

# ── Widgets & UI ──────────────────────────────────────────────────────────────
header_text = urwid.Text("Terminal Music Grid")
page_text = urwid.Text("Page 1", align="center")
status_text = urwid.Text("Ready")
waveform_widget = urwid.WidgetPlaceholder(urwid.Text("Sélectionne une piste..."))
search_edit = urwid.Edit(caption="Search: ", edit_text=DEFAULT_QUERY)
playpause_btn = urwid.Button("▶ Play")

def render_waveform():
    if not current_track: return
    try: cols = main_loop.screen.get_cols_rows()[0] - 6
    except: cols = 80
    num_bars, bar_max = max(8, cols // 3), 16
    heights = audio_capture.get(num_bars, bar_max) or [1]*num_bars
    content = [urwid.Text([("vis_title", f"  ♪  {current_track['title'][:50]}")]), urwid.Divider()]
    for row in range(bar_max, 0, -1):
        content.append(urwid.Text([(_bar_attr(ci, num_bars), "██") if h >= row else ("vis_bg", "  ") for ci, h in enumerate(heights)]))
    waveform_widget.original_widget = urwid.Pile(content)

def tick(loop, _):
    if player_process and player_process.poll() is not None: play_next()
    render_waveform()
    loop.set_alarm_in(0.1, tick)

def build_grid_widgets():
    cards = []
    for i, t in enumerate(tracks):
        is_cur = (i == current_index)
        btn = urwid.Button("⏹ Stop" if is_cur else "▶ Play")
        urwid.connect_signal(btn, "click", lambda b, idx=i: (play_track(idx), refresh_grid()))
        card = urwid.LineBox(urwid.Pile([urwid.Text(t['title'][:30]), urwid.AttrMap(btn, None, focus_map="reversed")]))
        if is_cur: card = urwid.AttrMap(card, "active_card")
        cards.append(card)
    return urwid.GridFlow(cards, 34, 2, 1, "left")

def refresh_grid(): 
    if body_walker: body_walker[7] = build_grid_widgets()

# ── Daemon Mode (The "No-Duplicate" Fix) ──────────────────────────────────────
def _daemon_music_loop(start_idx, start_track):
    global current_index, current_track, player_process
    os.setsid()
    current_index, current_track = start_idx, start_track
    
    # On relance VLC immédiatement dans le fils
    if current_track and current_track.get("stream_url"):
        player_process = subprocess.Popen(["cvlc", "--play-and-exit", "--quiet", current_track["stream_url"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    cap = AudioCapture(bars=24)
    cap.start()
    
    stop_flag = False
    while not stop_flag:
        if player_process and player_process.poll() is not None:
            current_index += 1
            if current_index < len(tracks):
                current_track = tracks[current_index]
                url = current_track.get("stream_url") or resolve_stream_url(current_track)
                player_process = subprocess.Popen(["cvlc", "--play-and-exit", "--quiet", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else: stop_flag = True
        
        # Gestion HUD
        tw, _ = shutil.get_terminal_size()
        _draw_hud(_build_hud_lines(int(time.time()*10), False, cap), tw)
        
        # Lecture commandes FIFO
        try:
            with open(CTRL_FIFO, "r") as f:
                cmd = f.read().strip()
                if cmd == "q": stop_flag = True
                elif cmd == "n": player_process.terminate()
                elif cmd == "p": 
                    if player_process: player_process.send_signal(signal.SIGSTOP if not is_paused else signal.SIGCONT)
        except: pass
        time.sleep(0.1)
    
    if player_process: player_process.terminate()
    cap.stop()
    _clear_hud(tw)

def enter_background_mode():
    global current_index, current_track
    idx, track = current_index, current_track
    
    # CRITIQUE : Le parent arrête son VLC avant de Fork
    stop_player() 
    
    try: os.mkfifo(CTRL_FIFO)
    except: pass

    if os.fork() > 0:
        tw, th = shutil.get_terminal_size()
        sys.stdout.write(f"\033[{HUD_ROWS + 1};{th}r\033[{HUD_ROWS + 1};1H")
        sys.stdout.flush()
        print(f"\n🎵 Music en arrière-plan. Utilise 'tmg p/n/q' pour contrôler.")
        os._exit(0)
    else:
        _daemon_music_loop(idx, track)
        os._exit(0)

# ── HUD Drawing ───────────────────────────────────────────────────────────────
def _build_hud_lines(frame, paused, cap):
    inner = HUD_W - 2
    heights = cap.get(inner-4, 7) or [1]*(inner-4)
    bar_line = "".join([f"\033[38;5;{ANSI_RAINBOW[int(i*9/len(heights))]}m{ANSI_BARS[h]}\033[0m" for i, h in enumerate(heights)])
    title = (current_track['title'][:inner-10] if current_track else "---")
    return [f"╭{'─'*inner}╮", f"│ {'⏸' if paused else '▶'} {title} │", f"│  {bar_line}  │", f"│ tmg p/n/q to control │", f"╰{'─'*inner}╯"]

def _draw_hud(lines, tw):
    col = max(1, tw - HUD_W)
    sys.stdout.write("\0337" + "".join([f"\033[{i+1};{col}H{l}" for i, l in enumerate(lines)]) + "\0338")
    sys.stdout.flush()

def _clear_hud(tw):
    col = max(1, tw - HUD_W)
    sys.stdout.write("\0337" + "".join([f"\033[{i+1};{col}H{' '*(HUD_W+1)}" for i in range(HUD_ROWS)]) + "\0338")
    sys.stdout.flush()

# ── Main Loop ─────────────────────────────────────────────────────────────────
def main():
    global tracks, main_loop, body_walker
    tracks, _, _ = fetch_tracks(DEFAULT_QUERY)
    
    next_btn = urwid.Button("Suivant ⏭")
    urwid.connect_signal(next_btn, "click", play_next)
    urwid.connect_signal(playpause_btn, "click", toggle_pause)

    body = urwid.ListBox(urwid.SimpleFocusListWalker([
        header_text, urwid.Divider(),
        urwid.Columns([urwid.AttrMap(playpause_btn, "btn"), urwid.AttrMap(next_btn, "btn")]),
        status_text, urwid.Divider(),
        urwid.LineBox(waveform_widget), urwid.Divider(),
        urwid.Text("Recherches récentes :"), build_grid_widgets()
    ]))
    body_walker = body.body

    palette = [
        ("vis_title", "light cyan,bold", ""), ("vis_bg", "default", ""),
        ("active_card", "yellow", ""), ("btn", "black", "dark cyan"),
    ]
    for i, col in enumerate(RAINBOW_COLS): palette.append((f"bar_{i}", "black", col))

    main_loop = urwid.MainLoop(urwid.Frame(body), palette=palette, unhandled_input=lambda k: k in ('q','Q') and enter_background_mode(), screen=urwid.raw_display.Screen())
    main_loop.set_alarm_in(0.1, tick)
    
    try: main_loop.run()
    except: pass

if __name__ == "__main__":
    main()