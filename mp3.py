#!/usr/bin/env python3
"""
Terminal Music Grid  — v3
- Visualiseur RÉEL via parec/pw-record (aucune installation requise)
- Mode arrière-plan : fork() → daemon music + HUD coin supérieur droit
  Le terminal est entièrement libre. Contrôle via FIFO :
      echo p > /tmp/tmg_ctrl.fifo   # pause/reprise
      echo n > /tmp/tmg_ctrl.fifo   # piste suivante
      echo q > /tmp/tmg_ctrl.fifo   # quitter
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

# ── Widgets globaux ───────────────────────────────────────────────────────────
header_text     = urwid.Text("Terminal Music Grid")
page_text       = urwid.Text("Page 1", align="center")
status_text     = urwid.Text("Ready")
waveform_widget = urwid.WidgetPlaceholder(
    urwid.Text("  Sélectionne une piste et clique sur ▶ Play")
)
search_edit   = urwid.Edit(caption="Search: ", edit_text=DEFAULT_QUERY)
playpause_btn = urwid.Button("▶ Play")

# ── Couleurs arc-en-ciel ──────────────────────────────────────────────────────
RAINBOW_COLS = [
    "yellow", "yellow", "brown", "brown",
    "dark green", "dark green", "dark green",
    "dark cyan", "dark cyan",
    "dark blue", "dark blue",
    "dark magenta", "dark magenta",
    "dark red", "dark red",
]
RAINBOW_DIM = [
    "brown", "brown", "dark red", "dark red",
    "dark blue", "dark blue", "dark blue", "dark blue",
    "dark blue", "dark blue", "dark blue", "dark blue",
    "dark blue", "dark blue", "dark blue",
]
ANSI_RAINBOW = [226, 214, 208, 46, 48, 51, 27, 93, 201, 196]
ANSI_BARS    = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]


def _bar_attr(col_index, num_cols, dim=False):
    idx = int(col_index * (len(RAINBOW_COLS) - 1) / max(1, num_cols - 1))
    idx = max(0, min(len(RAINBOW_COLS) - 1, idx))
    return f"{'ref' if dim else 'bar'}_{idx}"


# ── AudioCapture ──────────────────────────────────────────────────────────────
class AudioCapture:
    """
    Capture l'audio de sortie système via parec (PulseAudio) ou pw-record
    (PipeWire) — tous deux déjà présents sur tout Linux avec audio.
    Aucun sudo, aucun pip.  Calcul en pure Python (struct + math).
    """
    CHUNK_BYTES = 4096    # 2048 samples s16le ≈ 46 ms @ 44100 Hz
    SAMPLE_RATE = 44100

    def __init__(self, bars: int = 20):
        self.bars    = bars
        self.heights = [0.0] * bars
        self._proc   = None
        self._thread = None
        self._active = False
        self._lock   = threading.Lock()

    @staticmethod
    def available() -> bool:
        return bool(shutil.which("parec") or shutil.which("pw-record"))

    @staticmethod
    def _cmd() -> list:
        if shutil.which("parec"):
            return [
                "parec", "--raw",
                "--rate=44100", "--channels=1", "--format=s16le",
                "--latency-msec=20",
                "-d", "@DEFAULT_MONITOR@",
            ]
        return [
            "pw-record",
            "--rate=44100", "--channels=1", "--format=s16",
            "--target=auto", "-",
        ]

    def start(self) -> bool:
        if self._active:
            return True
        if not self.available():
            return False
        self._active = True
        self._proc   = subprocess.Popen(
            self._cmd(),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._active = False
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None

    def _read_loop(self):
        try:
            pipe = self._proc.stdout
            while self._active:
                raw = pipe.read(self.CHUNK_BYTES)
                if not raw:
                    break
                n       = len(raw) // 2
                samples = struct.unpack(f"<{n}h", raw[:n * 2])
                cs      = max(1, n // self.bars)
                result  = []
                for i in range(self.bars):
                    band = samples[i * cs:(i + 1) * cs]
                    if not band:
                        result.append(0.0)
                        continue
                    rms = math.sqrt(sum(s * s for s in band) / len(band))
                    # 32768 = max s16 ; ×4 pour amplifier visuellement
                    result.append(min(1.0, rms / 32768.0 * 4.0))
                with self._lock:
                    self.heights = result
        except Exception:
            pass

    def get(self, num_bars: int, max_h: int):
        """Liste d'int [0..max_h] ou None si pas de données (→ fallback math)."""
        with self._lock:
            src = list(self.heights)
        if not any(src):
            return None
        result = []
        for i in range(num_bars):
            idx = min(int(i * len(src) / max(1, num_bars)), len(src) - 1)
            result.append(max(1, int(src[idx] * max_h)))
        return result


audio_capture = AudioCapture(bars=24)


# ── YouTube ───────────────────────────────────────────────────────────────────
def fetch_tracks_youtube(query=DEFAULT_QUERY, max_tracks=MAX_TRACKS, page=1):
    offset   = (page - 1) * max_tracks
    ydl_opts = {
        "quiet": True, "no_warnings": True,
        "extract_flat": True, "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info    = ydl.extract_info(
            f"ytsearch{max_tracks + offset}:{query}", download=False
        )
        entries = (info.get("entries") or [])[offset:]
    out = []
    for item in entries:
        vid_id = item.get("id", "")
        if not vid_id:
            continue
        out.append({
            "source": "youtube", "identifier": vid_id,
            "title":  item.get("title", "?"),
            "artist": item.get("uploader") or item.get("channel", "?"),
            "stream_url": None,
        })
        if len(out) >= max_tracks:
            break
    return out, len(out) >= max_tracks


def resolve_stream_url_youtube(track):
    ydl_opts = {"quiet": True, "no_warnings": True, "format": "bestaudio/best"}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={track['identifier']}", download=False
        )
        return info.get("url")


# ── Archive.org ───────────────────────────────────────────────────────────────
def fetch_tracks_archive(query=DEFAULT_QUERY, max_tracks=MAX_TRACKS, page=1):
    qt     = (query or "").strip() or DEFAULT_QUERY
    params = urllib.parse.urlencode(
        {
            "q":    f"mediatype:(audio) AND (title:({qt}) OR creator:({qt}))",
            "fl[]": ["identifier", "title", "creator"],
            "rows": str(max_tracks), "page": str(page), "output": "json",
        },
        doseq=True,
    )
    req = urllib.request.Request(
        f"{ARCHIVE_SEARCH_URL}?{params}", headers={"User-Agent": "TMG/1.0"}
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        resp = json.loads(r.read().decode()).get("response", {})
    docs, num_found = resp.get("docs", []), int(resp.get("numFound", 0))
    out, seen = [], set()
    for item in docs:
        ident = item.get("identifier")
        if not ident or ident in seen:
            continue
        seen.add(ident)
        out.append({
            "identifier": ident,
            "title":  item.get("title", "?"),
            "artist": item.get("creator", "?"),
            "stream_url": None,
        })
        if len(out) >= max_tracks:
            break
    return out, page * max_tracks < num_found


def resolve_stream_url_archive(identifier):
    req = urllib.request.Request(
        f"{ARCHIVE_METADATA_URL}/{urllib.parse.quote(identifier)}",
        headers={"User-Agent": "TMG/1.0"},
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        payload = json.loads(r.read().decode())
    for f in payload.get("files", []):
        name, fmt = f.get("name", ""), (f.get("format") or "").lower()
        if "mp3" in fmt or name.lower().endswith(".mp3"):
            return (
                f"https://archive.org/download/"
                f"{urllib.parse.quote(identifier)}/{urllib.parse.quote(name)}"
            )
    return None


# ── SoundCloud ────────────────────────────────────────────────────────────────
def fetch_tracks_soundcloud(query=DEFAULT_QUERY, max_tracks=MAX_TRACKS, page=1):
    qt     = (query or "").strip() or DEFAULT_QUERY
    params = urllib.parse.urlencode({
        "q": qt, "limit": str(max_tracks),
        "offset": str(max(0, (page - 1) * max_tracks)),
        "linked_partitioning": "1", "client_id": SOUNDCLOUD_CLIENT_ID,
    })
    req = urllib.request.Request(
        f"{SOUNDCLOUD_SEARCH_URL}?{params}", headers={"User-Agent": "TMG/1.0"}
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        payload = json.loads(r.read().decode())
    out = []
    for item in payload.get("collection", []):
        tc = item.get("media", {}).get("transcodings", [])
        if not tc:
            continue
        out.append({
            "source": "soundcloud", "identifier": str(item.get("id", "")),
            "title":  item.get("title", "?"),
            "artist": item.get("user", {}).get("username", "?"),
            "transcodings": tc, "stream_url": None,
        })
        if len(out) >= max_tracks:
            break
    return out


def resolve_stream_url_soundcloud(track):
    ordered = sorted(
        track.get("transcodings") or [],
        key=lambda t: 0 if t.get("format", {}).get("protocol") == "progressive" else 1,
    )
    for trans in ordered:
        ep = trans.get("url")
        if not ep:
            continue
        try:
            req = urllib.request.Request(
                f"{ep}?{urllib.parse.urlencode({'client_id': SOUNDCLOUD_CLIENT_ID})}",
                headers={"User-Agent": "TMG/1.0"},
            )
            with urllib.request.urlopen(req, timeout=12) as r:
                url = json.loads(r.read().decode()).get("url")
            if url:
                return url
        except Exception:
            continue
    return None


# ── Dispatch ──────────────────────────────────────────────────────────────────
def fetch_tracks(query=DEFAULT_QUERY, max_tracks=MAX_TRACKS, page=1):
    if SOUNDCLOUD_CLIENT_ID:
        try:
            sc = fetch_tracks_soundcloud(query=query, max_tracks=max_tracks, page=page)
            if sc:
                return sc, "SoundCloud", len(sc) >= max_tracks
        except Exception:
            pass
    try:
        yt, has_more = fetch_tracks_youtube(query=query, max_tracks=max_tracks, page=page)
        if yt:
            return yt, "YouTube", has_more
    except Exception:
        pass
    ar, has_more = fetch_tracks_archive(query=query, max_tracks=max_tracks, page=page)
    return ar, "Archive", has_more


def resolve_stream_url(track):
    src = track.get("source", "")
    if src == "soundcloud":
        return resolve_stream_url_soundcloud(track)
    if src == "youtube":
        return resolve_stream_url_youtube(track)
    return resolve_stream_url_archive(track.get("identifier", ""))


# ── Player ────────────────────────────────────────────────────────────────────
def short(text, n):
    return text if len(text) <= n else text[:n - 3] + "..."


def stop_player():
    global player_process, is_paused
    if player_process and player_process.poll() is None:
        player_process.terminate()
    player_process = None
    is_paused      = False


def pause_player():
    global is_paused
    if player_process and player_process.poll() is None:
        player_process.send_signal(signal.SIGSTOP)
        is_paused = True


def resume_player():
    global is_paused
    if player_process and player_process.poll() is None:
        player_process.send_signal(signal.SIGCONT)
        is_paused = False


def toggle_pause(_button=None):
    if player_process is None:
        return
    if is_paused:
        resume_player()
        playpause_btn.set_label("⏸ Pause")
        status_text.set_text(f"▶ {current_track['title']}")
    else:
        pause_player()
        playpause_btn.set_label("▶ Play")
        status_text.set_text(f"⏸ {current_track['title']}")


def _start_cvlc(stream_url):
    global player_process
    player_process = subprocess.Popen(
        ["cvlc", "--play-and-exit", "--quiet", stream_url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def play_track(index):
    global current_track, current_index, visual_frame
    if not (0 <= index < len(tracks)):
        return
    stop_player()
    audio_capture.stop()
    visual_frame  = 0
    current_index = index
    current_track = tracks[index]

    stream_url = current_track.get("stream_url")
    if stream_url is None:
        status_text.set_text("Résolution du flux…")
        try:
            stream_url = resolve_stream_url(current_track)
        except Exception as e:
            status_text.set_text(f"Erreur: {e}")
        current_track["stream_url"] = stream_url

    if not stream_url:
        status_text.set_text("Flux introuvable — piste suivante…")
        play_next()
        return

    try:
        _start_cvlc(stream_url)
        audio_capture.start()
        playpause_btn.set_label("⏸ Pause")
        status_text.set_text(f"▶ {current_track['title']}")
    except FileNotFoundError:
        status_text.set_text("cvlc introuvable — sudo apt install vlc")

    render_waveform()


def play_next(_button=None):
    global current_index
    nxt = current_index + 1
    if nxt >= len(tracks):
        status_text.set_text("✓ Fin de la liste.")
        return
    play_track(nxt)
    refresh_grid()


def on_play_button(_button, index):
    play_track(index)
    refresh_grid()


# ── Visualiseur urwid ─────────────────────────────────────────────────────────
def _bar_heights_math(num_bars, max_height):
    t    = visual_frame * 0.35
    base = abs(hash(current_track["title"] if current_track else "x")) % 50
    out  = []
    for i in range(num_bars):
        v = (
            abs(math.sin(t * 1.1 + i * 0.18 + base * 0.1))
            * abs(math.cos(t * 0.7 + i * 0.09))
            + 0.4 * abs(math.sin(t * 2.3 + i * 0.25))
            + 0.15 * abs(math.sin(t * 0.4 + i * 0.06))
        )
        out.append(int(max(1, min(max_height, v * max_height / 1.55))))
    return out


def _bar_heights(num_bars, max_height, cap=None):
    result = (cap or audio_capture).get(num_bars, max_height)
    return result if result is not None else _bar_heights_math(num_bars, max_height)


def render_waveform():
    global visual_frame
    if current_track is None:
        waveform_widget.original_widget = urwid.Text(
            "  Sélectionne une piste et clique sur ▶ Play"
        )
        return

    try:
        cols = main_loop.screen.get_cols_rows()[0] - 6
    except Exception:
        cols = 120

    bar_w    = 2
    gap      = 1
    num_bars = max(8, cols // (bar_w + gap))
    bar_max  = 16
    ref_rows = 5
    BLOCK    = "█" * bar_w
    SPACE    = " " * bar_w

    if not is_paused:
        visual_frame += 1
    heights = _bar_heights(num_bars, bar_max)

    content = []
    src_tag = " [parec]" if audio_capture._active and audio_capture.available() else ""
    title_str = (
        f"  ♪  {short(current_track['title'], 52)}"
        f"  —  {short(current_track['artist'], 28)}{src_tag}"
    )
    content.append(urwid.Text([("vis_title", title_str)]))
    content.append(urwid.Divider())

    for row in range(bar_max, 0, -1):
        segs = []
        for ci, h in enumerate(heights):
            attr = _bar_attr(ci, num_bars)
            segs.append((attr if h >= row else "vis_bg", BLOCK))
            segs.append(("vis_bg", " " * gap))
        content.append(urwid.Text(segs))

    base_segs = []
    for ci in range(num_bars):
        base_segs.append((_bar_attr(ci, num_bars), "▄" * bar_w))
        base_segs.append(("vis_bg", " " * gap))
    content.append(urwid.Text(base_segs))

    for ref in range(1, ref_rows + 1):
        segs = []
        for ci, h in enumerate(heights):
            attr      = _bar_attr(ci, num_bars, dim=True)
            threshold = bar_max - ref * 2
            if h > threshold:
                char = ("▓" if ref <= 2 else ("▒" if ref == 3 else "░")) * bar_w
                segs.append((attr, char))
            else:
                segs.append(("vis_bg", SPACE))
            segs.append(("vis_bg", " " * gap))
        content.append(urwid.Text(segs))

    waveform_widget.original_widget = urwid.Pile(content)


# ── Tick ──────────────────────────────────────────────────────────────────────
def tick(loop, _):
    if player_process is not None and player_process.poll() is not None:
        play_next()
    render_waveform()
    loop.set_alarm_in(0.08, tick)


# ── Clavier ───────────────────────────────────────────────────────────────────
def on_key(key):
    if key in ("q", "Q"):
        raise urwid.ExitMainLoop()
    if key == "esc":
        stop_player()
        audio_capture.stop()
        raise urwid.ExitMainLoop()
    if key == "enter":
        run_search()
    if key in ("n", "N"):
        next_page()
    if key in ("p", "P"):
        prev_page()
    if key == " ":
        toggle_pause()


# ── Grille ────────────────────────────────────────────────────────────────────
def build_grid_widgets():
    if not tracks:
        return urwid.Text("Aucune piste.")
    cards = []
    for i, track in enumerate(tracks):
        is_cur = (i == current_index)
        src    = track.get("source", "archive").upper()
        title  = short(f"[{src}] {track['title']}", 30)
        artist = short(track["artist"], 30)
        btn    = urwid.Button("⏹ Stop" if is_cur else "▶ Play")
        urwid.connect_signal(btn, "click", on_play_button, i)
        pile = urwid.Pile([
            urwid.Text(("active_title" if is_cur else "card_title", title)),
            urwid.Text(artist),
            urwid.Divider("-"),
            urwid.AttrMap(btn, None, focus_map="reversed"),
        ])
        card = urwid.LineBox(pile)
        if is_cur:
            card = urwid.AttrMap(card, "active_card")
        cards.append(card)
    return urwid.GridFlow(cells=cards, cell_width=34, h_sep=2, v_sep=1, align="left")


def refresh_grid():
    if body_walker is None:
        return
    body_walker[7] = build_grid_widgets()


def refresh_header():
    header_text.set_text(
        f"♪ Terminal Music Grid [{active_source}] ({len(tracks)} pistes)  "
        f"|  Page {current_page}  |  Enter=chercher  |  n/p=pages  "
        f"|  Space=pause  |  q=fond  |  Esc=quitter"
    )
    page_text.set_text(f"Page {current_page}")


def load_page(page):
    global tracks, current_page, has_next_page, active_source
    current_page = max(1, page)
    status_text.set_text(f"Recherche : {current_query} (page {current_page})…")
    tracks, source_name, has_more = fetch_tracks(
        query=current_query, page=current_page
    )
    active_source = source_name
    has_next_page = has_more
    refresh_header()
    refresh_grid()
    status_text.set_text(
        f"✓ {len(tracks)} pistes  « {current_query} »  (page {current_page})"
    )


def run_search(_button=None):
    global current_query, current_page
    current_query = search_edit.get_edit_text().strip() or DEFAULT_QUERY
    current_page  = 1
    try:
        load_page(1)
    except Exception as e:
        status_text.set_text(f"Erreur: {e}")


def next_page(_button=None):
    if not has_next_page:
        status_text.set_text("Pas d'autre page.")
        return
    try:
        load_page(current_page + 1)
    except Exception as e:
        status_text.set_text(f"Erreur: {e}")


def prev_page(_button=None):
    if current_page <= 1:
        status_text.set_text("Déjà à la première page.")
        return
    try:
        load_page(current_page - 1)
    except Exception as e:
        status_text.set_text(f"Erreur: {e}")


# ── HUD partagé ───────────────────────────────────────────────────────────────
_ANSI_STRIP = re.compile(r"\033\[[0-9;]*m")


def _ansi_len(s):
    return len(_ANSI_STRIP.sub("", s))


def _pad(s, width):
    return s + " " * max(0, width - _ansi_len(s))


def _hud_bar_heights_math(nb, frame):
    t    = frame * 0.35
    base = abs(hash(current_track["title"] if current_track else "x")) % 50
    out  = []
    for i in range(nb):
        v = (
            abs(math.sin(t * 1.1 + i * 0.18 + base * 0.1))
            * abs(math.cos(t * 0.7 + i * 0.09))
            + 0.3 * abs(math.sin(t * 2.3 + i * 0.25))
        )
        out.append(int(max(0, min(7, v * 7 / 1.3))))
    return out


def _build_hud_lines(frame, paused, cap):
    inner   = HUD_W - 2
    nb_bars = inner - 4
    title   = short(current_track["title"]  if current_track else "─", inner - 10)
    artist  = short(current_track["artist"] if current_track else "",   inner - 4)

    raw_h   = cap.get(nb_bars, 7)
    heights = raw_h if raw_h is not None else _hud_bar_heights_math(nb_bars, frame)

    bar_line = ""
    for i, h in enumerate(heights):
        ci        = int(i * (len(ANSI_RAINBOW) - 1) / max(1, nb_bars - 1))
        c         = ANSI_RAINBOW[ci]
        bar_line += f"\033[38;5;{c}m{ANSI_BARS[h]}\033[0m"

    BC   = "\033[38;5;39m"
    DIM  = "\033[2m"
    BOLD = "\033[1m"
    YEL  = "\033[33m"
    RST  = "\033[0m"
    state = f"{YEL}⏸{RST}" if paused else f"{YEL}▶{RST}"
    nxt   = f"{DIM}⏭{RST}"

    def row(content):
        return f"{BC}│{RST} {_pad(content, inner - 1)}{BC}│{RST}"

    return [
        f"{BC}╭{'─' * (inner + 1)}╮{RST}",
        row(f"{state} {nxt}  {BOLD}{title}{RST}"),
        row(f"  {bar_line}"),
        row(f"  {DIM}{artist}{RST}"),
        row(f"  {DIM}p=pause  n=suivant  q=quitter{RST}"),
        f"{BC}╰{'─' * (inner + 1)}╯{RST}",
    ]


def _draw_hud(lines, term_w):
    """Dessine le HUD en haut à droite sans toucher au reste du terminal."""
    col = max(1, term_w - HUD_W)
    out = ["\0337"]                        # ESC 7 = save cursor (DEC VT100)
    for i, line in enumerate(lines):
        out.append(f"\033[{i + 1};{col}H")
        out.append(line)
    out.append("\0338")                    # ESC 8 = restore cursor
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def _clear_hud(term_w):
    col = max(1, term_w - HUD_W)
    out = ["\0337"]
    for i in range(HUD_ROWS):
        out.append(f"\033[{i + 1};{col}H")
        out.append(" " * (HUD_W + 2))
    out.append("\0338")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


# ── Daemon (mode arrière-plan) ────────────────────────────────────────────────
def _daemon_music_loop(index_to_resume=None, track_to_resume=None):
    """
    Tourne dans le processus fils (après fork).
    Gère musique, auto-next, HUD et commandes FIFO.
    
    Args:
        index_to_resume: index de la piste à reprendre (None = pas de reprise)
        track_to_resume: objet piste à reprendre (None = pas de reprise)
    """
    global player_process, current_index, current_track, is_paused

    # Réassigner les variables globales du fils avec les données du parent
    if index_to_resume is not None:
        current_index = index_to_resume
    if track_to_resume is not None:
        current_track = track_to_resume

    os.setsid()                                  # nouvelle session, immunisé SIGINT
    signal.signal(signal.SIGINT,  signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    cap       = AudioCapture(bars=24)
    cap.start()

    frame     = [0]
    paused    = [is_paused]
    stop_flag = [False]

    def _next():
        global player_process, current_index, current_track
        stop_player()
        current_index += 1
        if current_index >= len(tracks):
            return False
        current_track = tracks[current_index]
        url = current_track.get("stream_url")
        if url is None:
            try:
                url = resolve_stream_url(current_track)
                current_track["stream_url"] = url
            except Exception:
                url = None
        if url:
            player_process = subprocess.Popen(
                ["cvlc", "--play-and-exit", "--quiet", url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        return True

    def _handle(cmd):
        if cmd == "p":
            if paused[0]:
                resume_player(); paused[0] = False
            else:
                pause_player();  paused[0] = True
        elif cmd == "n":
            _next()
        elif cmd == "q":
            stop_flag[0] = True

    # Relance la piste sauvegardée si elle existe
    if track_to_resume is not None and track_to_resume.get("stream_url"):
        url = track_to_resume.get("stream_url")
        if url:
            player_process = subprocess.Popen(
                ["cvlc", "--play-and-exit", "--quiet", url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    # Thread HUD (80 ms)
    def _hud_thread():
        while not stop_flag[0]:
            if not paused[0]:
                frame[0] += 1
            if player_process is not None and player_process.poll() is not None:
                if not _next():
                    stop_flag[0] = True
                    return
            try:
                tw, _ = shutil.get_terminal_size()
                _draw_hud(_build_hud_lines(frame[0], paused[0], cap), tw)
            except Exception:
                pass
            time.sleep(0.08)

    threading.Thread(target=_hud_thread, daemon=True).start()

    # Boucle FIFO — bloque en attente de chaque `echo cmd > FIFO`
    while not stop_flag[0]:
        try:
            with open(CTRL_FIFO, "r") as fh:
                for line in fh:
                    cmd = line.strip()
                    if cmd:
                        _handle(cmd)
                    if stop_flag[0]:
                        break
        except OSError:
            time.sleep(0.2)

    # Nettoyage
    cap.stop()
    stop_player()
    try:
        tw, _ = shutil.get_terminal_size()
        _clear_hud(tw)
        sys.stdout.write("\033[r\n")     # reset scroll region
        sys.stdout.flush()
    except Exception:
        pass
    try:
        os.unlink(CTRL_FIFO)
    except Exception:
        pass


def enter_background_mode():
    """
    Fork → le fils devient le daemon music+HUD.
    Le parent :
      1. réserve les 6 premières lignes (scroll region ANSI)
      2. affiche les instructions de contrôle
      3. sort de Python → shell immédiatement disponible
    """
    global player_process, current_track, current_index

    # 1. Sauvegarde les infos AVANT le fork
    track_to_resume = current_track
    index_to_resume = current_index

    try:
        if os.path.exists(CTRL_FIFO):
            os.unlink(CTRL_FIFO)
        os.mkfifo(CTRL_FIFO)
    except Exception:
        pass

    # !!! CRITIQUE : On arrête le VLC du parent ici !!!
    # Sinon, le fils va hériter du processus et on ne pourra plus le tuer proprement
    stop_player()

    pid = os.fork()

    if pid > 0:
        # ── Parent : setup terminal puis exit ─────────────────────────────────
        tw, th = shutil.get_terminal_size()

        # Scroll region : lignes HUD_ROWS+1 → fin = zone shell
        # Les lignes 1..HUD_ROWS restent fixes pour le HUD du daemon
        sys.stdout.write(f"\033[{HUD_ROWS + 1};{th}r")    # set scroll region
        sys.stdout.write(f"\033[{HUD_ROWS + 1};1H")        # curseur sous le HUD
        sys.stdout.flush()

        print(f"\n  🎵  Music en arrière-plan  [daemon PID {pid}]")
        print(f"\n  Contrôles (tape dans ce terminal) :")
        print(f"    echo p > {CTRL_FIFO}   →  pause / reprise")
        print(f"    echo n > {CTRL_FIFO}   →  piste suivante")
        print(f"    echo q > {CTRL_FIFO}   →  tout arrêter\n")
        print(f"  Astuce — ajoute à ~/.bashrc pour une commande courte :")
        print(f"    tmg() {{ echo \"$1\" > {CTRL_FIFO}; }}")
        print(f"  Puis :  tmg p   tmg n   tmg q\n")

        os._exit(0)      # exit sans déclencher les atexit Python

    else:
        # ── Fils : daemon, relance la musique sauvegardée ─────────────────────
        _daemon_music_loop(index_to_resume, track_to_resume)
        os._exit(0)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global current_query, main_loop, body_walker

    current_query = DEFAULT_QUERY
    try:
        load_page(1)
    except Exception as exc:
        print(f"Échec du chargement : {exc}")
        sys.exit(1)

    search_btn = urwid.Button("🔍 Chercher")
    urwid.connect_signal(search_btn, "click", run_search)
    search_field = urwid.LineBox(
        urwid.AttrMap(search_edit, "search_box"),
        tlcorner="╭", trcorner="╮", blcorner="╰", brcorner="╯",
    )
    search_row = urwid.Columns(
        [
            ("weight", 4, urwid.AttrMap(search_field, "search_border")),
            ("fixed", 15, urwid.AttrMap(search_btn, None, focus_map="reversed")),
        ],
        dividechars=1,
    )

    urwid.connect_signal(playpause_btn, "click", toggle_pause)
    next_btn = urwid.Button("⏭ Suivant")
    urwid.connect_signal(next_btn, "click", play_next)
    ctrl_row = urwid.Columns(
        [
            ("fixed", 14, urwid.AttrMap(playpause_btn, "ctrl_btn", focus_map="reversed")),
            ("fixed", 14, urwid.AttrMap(next_btn,      "ctrl_btn", focus_map="reversed")),
            ("weight", 1, status_text),
        ],
        dividechars=1,
    )

    prev_btn = urwid.Button("◀ Préc")
    nxt_btn  = urwid.Button("Suiv ▶")
    urwid.connect_signal(prev_btn, "click", prev_page)
    urwid.connect_signal(nxt_btn,  "click", next_page)
    pager_row = urwid.Columns(
        [
            ("fixed", 10, urwid.AttrMap(prev_btn, None, focus_map="reversed")),
            ("weight",  1, page_text),
            ("fixed", 10, urwid.AttrMap(nxt_btn,  None, focus_map="reversed")),
        ],
        dividechars=2,
    )

    waveform_box = urwid.LineBox(waveform_widget, title="═ Visualiseur ═")
    grid         = build_grid_widgets()

    body = urwid.ListBox(
        urwid.SimpleFocusListWalker([
            header_text,
            search_row,
            ctrl_row,
            urwid.Divider("─"),
            waveform_box,
            urwid.Divider("─"),
            pager_row,
            grid,
        ])
    )
    body_walker = body.body

    audio_src = (
        "parec"     if shutil.which("parec")     else
        "pw-record" if shutil.which("pw-record") else
        "math (parec/pw-record absent)"
    )
    frame = urwid.Frame(
        body=urwid.Padding(body, left=1, right=1),
        footer=urwid.AttrMap(
            urwid.Text(
                f"  q=fond  Esc=stop+quit  Space=pause  n/p=pages"
                f"  │  visualiseur: {audio_src}"
            ),
            "footer",
        ),
    )

    palette = [
        ("reversed",      "standout",        ""),
        ("vis_title",     "light cyan,bold",  ""),
        ("vis_bg",        "default",          ""),
        ("title",         "light cyan,bold",  ""),
        ("active_title",  "yellow,bold",      ""),
        ("active_card",   "yellow",           ""),
        ("card_title",    "white",            ""),
        ("ctrl_btn",      "black",            "dark cyan"),
        ("search_box",    "white",            "default"),
        ("search_border", "light cyan",       "default"),
        ("footer",        "black",            "dark blue"),
    ]
    for i, (col, dim) in enumerate(zip(RAINBOW_COLS, RAINBOW_DIM)):
        palette.append((f"bar_{i}", "black", col))
        palette.append((f"ref_{i}", "dark blue", dim))

    screen = urwid.raw_display.Screen()
    screen.set_terminal_properties(colors=256)
    main_loop = urwid.MainLoop(
        frame, palette=palette,
        unhandled_input=on_key,
        handle_mouse=True,
        screen=screen,
    )
    main_loop.set_alarm_in(0.08, tick)

    try:
        main_loop.run()
    except Exception:
        pass

    if player_process is not None and player_process.poll() is None:
        enter_background_mode()   # fork daemon + retour shell
    else:
        audio_capture.stop()
        stop_player()
        print("\n Nary Tsiresy want to see you again, next time !")


if __name__ == "__main__":
    main()
