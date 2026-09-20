import os
import sys
import json
import queue
import shutil
from pathlib import Path
from datetime import datetime, timedelta
import piexif
try:
    # Optional: the only way to read EXIF out of RAW containers. Without it
    # RAW-only shots fall back to mtime, which silently breaks the ordering
    # this whole tool exists to produce — so its absence is reported, loudly.
    import exifread
except ImportError:
    exifread = None
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import tkinter.font as tkfont
import threading

# HEIC/HEIF are final images like JPEG (iPhone default), not a RAW format
JPEG_EXTS  = {'.jpg', '.jpeg', '.heic', '.heif'}
RAW_EXTS   = {'.raf', '.arw', '.cr2', '.cr3', '.nef', '.dng'}
VIDEO_EXTS = {'.mov', '.mp4'}

IS_WIN = os.name == 'nt'
IS_MAC = sys.platform == 'darwin'


def _settings_path():
    """Per-platform config location. %APPDATA% exists only on Windows, so
    reading it unconditionally dropped a stray CameraRenamer folder straight
    into $HOME on macOS/Linux."""
    if IS_WIN:
        base = Path(os.getenv('APPDATA') or Path.home())
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv('XDG_CONFIG_HOME') or (Path.home() / ".config"))
    return base / "CameraRenamer" / "settings.json"


SETTINGS_PATH = _settings_path()

# Earlier builds resolved the path differently and left settings behind in
# $HOME and next to the script. Read those once if the real one is missing.
LEGACY_SETTINGS_PATHS = [
    Path.home() / "CameraRenamer" / "settings.json",
    Path(__file__).resolve().parent / "CameraRenamer" / "settings.json",
]


# ── Unchanged helpers ─────────────────────────────────────────────

def normalize_path(path):
    p = Path(path).resolve().absolute()
    # Windows and macOS both use case-insensitive filesystems by default, so a
    # case-only difference between output and a source folder is the SAME
    # folder. Comparing case-sensitively there let that slip past the safety
    # checks below — in move mode that deletes the originals.
    if IS_WIN or IS_MAC:
        return str(p).lower()
    return str(p)


def validate_folders(camera_dirs, output_dir):
    if not output_dir:
        return False, "No output folder selected."
    if not camera_dirs:
        return False, "No camera folders selected."
    output_norm = normalize_path(output_dir)
    seen = {}
    for cam_num, cam_path in camera_dirs.items():
        cam_norm = normalize_path(cam_path)
        if cam_norm in seen:
            return False, (
                f"Camera {seen[cam_norm]} and Camera {cam_num} point to the SAME folder!\n"
                f"Every shot would be processed twice."
            )
        seen[cam_norm] = cam_num
        if output_norm == cam_norm:
            return False, (
                f"Output folder is the SAME as Camera {cam_num} folder!\n"
                f"This would cause DATA LOSS. Choose a different output folder."
            )
        if output_norm.startswith(cam_norm + os.sep):
            return False, f"Output folder is INSIDE Camera {cam_num} folder!"
        if cam_norm.startswith(output_norm + os.sep):
            return False, f"Camera {cam_num} folder is INSIDE the output folder!"
    return True, ""


class CombinedCameraRenamer:
    def __init__(self, output_dir, keep_raw=True, move_files=False, recurse=False,
                 include_video=False, prefix="DSCF", start_num=1, camera_offsets=None):
        self.camera_sources = {}
        self.all_files = []
        self.output_dir = Path(output_dir)
        self.keep_raw = keep_raw
        self.move_files = move_files
        self.recurse = recurse
        self.include_video = include_video
        self.prefix = prefix
        self.start_num = start_num
        # {camera_num: seconds} — corrects for cameras whose clocks weren't
        # in sync, applied on top of each shot's EXIF/mtime timestamp
        self.camera_offsets = camera_offsets or {}

    def add_camera_source(self, camera_num, source_path):
        self.camera_sources[camera_num] = Path(source_path)

    def _seq_name(self, idx, suffix):
        seq = self.start_num + idx - 1
        return f"{self.prefix}{str(seq).zfill(4)}{suffix}"

    @staticmethod
    def _subsec_to_us(s):
        # EXIF SubSecTimeOriginal is fractional seconds as digits, e.g. "057"
        s = s.strip()[:6]
        return int(s.ljust(6, '0')) if s.isdigit() else 0

    def get_timestamp(self, file_path):
        try:
            exif = piexif.load(str(file_path)).get("Exif", {})
            if piexif.ExifIFD.DateTimeOriginal in exif:
                date_str = exif[piexif.ExifIFD.DateTimeOriginal].decode()
                ts = datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
                sub = exif.get(piexif.ExifIFD.SubSecTimeOriginal)
                if sub:
                    ts = ts.replace(microsecond=self._subsec_to_us(sub.decode()))
                return ts
        except Exception:
            pass
        # piexif can't parse RAW containers (RAF/ARW/CR2/NEF/DNG) — try exifread if installed
        if exifread is None:
            return datetime.fromtimestamp(file_path.stat().st_mtime)
        try:
            with open(file_path, 'rb') as f:
                tags = exifread.process_file(f, details=False)
            tag = tags.get('EXIF DateTimeOriginal')
            if tag:
                ts = datetime.strptime(str(tag), "%Y:%m:%d %H:%M:%S")
                sub = tags.get('EXIF SubSecTimeOriginal')
                if sub:
                    ts = ts.replace(microsecond=self._subsec_to_us(str(sub)))
                return ts
        except Exception:
            pass
        return datetime.fromtimestamp(file_path.stat().st_mtime)

    def collect_and_sort_files(self, gui):
        allowed = JPEG_EXTS
        if self.keep_raw:
            allowed = allowed | RAW_EXTS
        if self.include_video:
            allowed = allowed | VIDEO_EXTS
        file_groups = {}
        for camera_num, source_path in sorted(self.camera_sources.items()):
            if not source_path.exists():
                gui.log(f"Camera {camera_num} path does not exist: {source_path}", "err")
                return False
            walker = source_path.rglob('*') if self.recurse else source_path.iterdir()
            files_found = sorted(
                p for p in walker
                # skip dotfiles: FAT32/exFAT camera cards accrue a "._name" AppleDouble
                # sidecar per real file once macOS touches them, and it shares the same
                # extension, so it would otherwise be counted and renamed as its own shot
                if p.is_file() and not p.name.startswith('.') and p.suffix.lower() in allowed
            )
            gui.log(f"Camera {camera_num}: {len(files_found)} files found", "ok")
            for file_path in files_found:
                # Parent in the key: a RAW+JPEG pair shares a folder, but a
                # counter-reset duplicate in another subfolder is a new shot
                key = (camera_num, str(file_path.parent), file_path.stem.upper())
                if key not in file_groups:
                    file_groups[key] = {
                        'camera': camera_num,
                        'original_name': file_path.stem,
                        'files': []
                    }
                file_groups[key]['files'].append(file_path)

        if not file_groups:
            gui.log("No files found in any camera folder.", "err")
            return False

        # Timestamp from the JPEG member when available — RAW EXIF is unreadable
        # without exifread, and its mtime fallback breaks chronological ordering
        raw_only = 0
        for group in file_groups.values():
            if gui.cancelled():
                gui.log("Cancelled while reading timestamps.", "warn")
                return False
            jpegs = [f for f in group['files'] if f.suffix.lower() in JPEG_EXTS]
            if not jpegs and exifread is None:
                raw_only += 1
            ts = self.get_timestamp(jpegs[0] if jpegs else group['files'][0])
            offset = self.camera_offsets.get(group['camera'], 0)
            if offset:
                ts += timedelta(seconds=offset)
            group['timestamp'] = ts

        # Filename tie-break keeps bursts in in-camera order when timestamps match
        self.all_files = sorted(
            file_groups.values(),
            key=lambda x: (x['timestamp'], x['original_name'], x['camera'])
        )
        if raw_only:
            gui.log(
                f"⚠  {raw_only} RAW-only shot(s) had no readable EXIF — the "
                f"'exifread' module is not installed, so file mtime was used.", "warn"
            )
            gui.log("   Ordering may be wrong. Install with:  pip3 install exifread", "warn")

        total = sum(len(g['files']) for g in self.all_files)
        gui.log(f"Unique shots: {len(self.all_files)}   Total files: {total}", "info")
        return True

    def _existing_destinations(self):
        """Planned destination names that already exist in the output folder."""
        if not self.output_dir.exists():
            return []
        clashes = []
        for idx, group in enumerate(self.all_files, 1):
            for file_path in group['files']:
                dest = self.output_dir / self._seq_name(idx, file_path.suffix)
                if dest.exists():
                    clashes.append(dest.name)
        return clashes

    def rename_and_copy(self, copy_files, gui):
        # Checked in BOTH modes: running it only before a real copy meant a
        # preview reported success and the actual run then aborted on the
        # very same folder, which is the opposite of what a preview is for.
        existing = self._existing_destinations()

        if not copy_files:
            gui.log("PREVIEW MODE \u2014 no files will be written", "warn")
            if existing:
                gui.log(
                    f"\u26a0  {len(existing)} destination name(s) already exist in the "
                    f"output folder (e.g. {existing[0]}) \u2014 Process would abort.", "warn"
                )
                gui.log("Choose an empty output folder, or raise Start #.", "warn")
        else:
            if existing:
                gui.log(
                    f"ABORTED \u2014 {len(existing)} destination file(s) already exist "
                    f"in the output folder (e.g. {existing[0]}).", "err"
                )
                gui.log("Choose an empty output folder to avoid overwriting files.", "err")
                gui.finish()
                return

            self.output_dir.mkdir(parents=True, exist_ok=True)
            action = "Moving" if self.move_files else "Copying"
            gui.log(f"{action} \u2192 {self.output_dir}", "info")

        total = len(self.all_files)
        for idx, group in enumerate(self.all_files, 1):
            if gui.cancelled():
                gui.log(f"Cancelled — {idx - 1} of {total} shots processed.", "warn")
                gui.finish()
                return
            ts = group['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
            for file_path in sorted(group['files']):
                new_name = self._seq_name(idx, file_path.suffix)
                gui.log(
                    f"[{idx:4d}]  CAM{group['camera']}  {ts}  {file_path.name} \u2192 {new_name}",
                    "dim"
                )
                if copy_files:
                    new_path = self.output_dir / new_name
                    if self.move_files:
                        shutil.move(str(file_path), str(new_path))
                    else:
                        shutil.copy2(str(file_path), str(new_path))
            gui.set_progress(idx / total)

        msg = (
            f"Done \u2014 {len(self.all_files)} shots processed \u2192 {self.output_dir}"
            if copy_files else
            f"Preview complete \u2014 {len(self.all_files)} shots would be processed"
        )
        gui.log(msg, "ok")
        gui.finish()


# ── Design tokens ─────────────────────────────────────────────────

BG     = "#141414"
PANEL  = "#1c1c1c"
CARD   = "#232323"
RAISED = "#2b2b2b"
BORDER = "#333333"
ACCENT = "#d4952a"   # warm amber — photo-tool feel
OK     = "#5a9e5a"
WARN   = "#c8892a"
ERR    = "#cc4444"
TXT    = "#e2e2e2"
SUB    = "#888888"
DIM    = "#505050"
LOG_BG = "#0f0f0f"

# Font families are resolved at runtime by init_fonts() to prefer Thai-capable
# faces. Camera/output folder names are often Thai; Segoe UI misplaces Thai tone
# marks and vowels under Tk 8.6 (no complex-script shaping) and Consolas has no
# Thai glyphs at all. Leelawadee UI matches Segoe UI metrics closely but renders
# Thai correctly, so the Latin look is virtually unchanged.
# macOS/Linux faces come first on those platforms — the list was Windows-only,
# so every name missed and Tk fell back to a generic default with no Thai face.
if IS_MAC:
    _UI_FONT_PREFS   = ("Thonburi", "Helvetica Neue", "Lucida Grande")
    _MONO_FONT_PREFS = ("SF Mono", "Menlo", "Monaco", "Courier New")
elif IS_WIN:
    _UI_FONT_PREFS   = ("Leelawadee UI", "Tahoma", "Segoe UI")
    _MONO_FONT_PREFS = ("Consolas", "Cascadia Mono", "Courier New")
else:
    _UI_FONT_PREFS   = ("Noto Sans Thai UI", "Noto Sans Thai", "DejaVu Sans")
    _MONO_FONT_PREFS = ("Noto Sans Mono", "DejaVu Sans Mono", "Liberation Mono")

FUI  = _UI_FONT_PREFS[-1]    # resolved in init_fonts()
MONO = _MONO_FONT_PREFS[-1]  # resolved in init_fonts()
F_H    = (FUI,  12, "bold")
F_SEC  = (FUI,   7, "bold")
F_BODY = (FUI,   9)
F_SM   = (FUI,   8)
F_LOG  = (MONO,  9)


def init_fonts():
    """Resolve UI/mono font families to Thai-capable faces that are actually
    installed, then rebuild the shared font tuples. Requires a live Tk root
    (tkfont.families() needs one). Falls back to the originals if resolution
    fails, so the app never breaks on an unusual system."""
    global FUI, MONO, F_H, F_SEC, F_BODY, F_SM, F_LOG
    try:
        available = {name.lower() for name in tkfont.families()}
    except Exception:
        available = set()

    def pick(prefs, current):
        for name in prefs:
            if name.lower() in available:
                return name
        return current

    FUI  = pick(_UI_FONT_PREFS, FUI)
    MONO = pick(_MONO_FONT_PREFS, MONO)
    F_H    = (FUI, 12, "bold")
    F_SEC  = (FUI,  7, "bold")
    F_BODY = (FUI,  9)
    F_SM   = (FUI,  8)
    F_LOG  = (MONO, 9)


def _btn(parent, text, cmd, variant="normal"):
    styles = {
        "normal":  dict(bg=RAISED,    fg=SUB,  abg="#353535", afg=TXT),
        "primary": dict(bg=ACCENT,    fg="#fff", abg="#b87820", afg="#fff"),
        "ghost":   dict(bg=PANEL,     fg=DIM,  abg=CARD,     afg=SUB),
        "danger":  dict(bg="#3a1a1a", fg=ERR,  abg="#4a2222", afg=ERR),
    }
    s = styles.get(variant, styles["normal"])
    # NOT a tk.Button: on macOS that is a native Aqua control which ignores
    # bg/fg entirely, so every button rendered as a white blob on the dark
    # theme. A Label is drawn by Tk itself and honours colours everywhere.
    b = tk.Label(
        parent, text=text,
        bg=s["bg"], fg=s["fg"],
        relief="flat", bd=0, padx=10, pady=5,
        font=F_BODY, cursor="hand2",
    )

    def _enter(_):
        b.config(bg=s["abg"], fg=s["afg"])

    def _leave(_):
        b.config(bg=s["bg"], fg=s["fg"])

    def _press(_):
        b.config(bg=s["bg"], fg=s["afg"])

    def _release(e):
        b.config(bg=s["abg"], fg=s["afg"])
        # fire only if released over the widget, matching real button behaviour
        if 0 <= e.x < b.winfo_width() and 0 <= e.y < b.winfo_height():
            cmd()

    b.bind("<Enter>", _enter)
    b.bind("<Leave>", _leave)
    b.bind("<Button-1>", _press)
    b.bind("<ButtonRelease-1>", _release)
    return b


# ── GUI ───────────────────────────────────────────────────────────

class CameraRenamerGUI:
    def __init__(self, root):
        self.root = root
        init_fonts()  # resolve Thai-capable fonts now that a Tk root exists
        self.root.title("Camera Renamer")
        # Sized for macOS metrics, which are taller/wider than the Windows ones
        # this was laid out against — at 860x530 the action row fell off the
        # bottom and the Move-files warning was cut off on the right.
        self.root.geometry("940x640")
        self.root.minsize(820, 600)
        self.root.configure(bg=BG)
        self.root.resizable(True, True)

        self.camera_dirs   = {}
        self.output_dir    = None
        self.keep_raw      = tk.BooleanVar(value=True)
        self.move_files    = tk.BooleanVar(value=False)
        self.include_sub   = tk.BooleanVar(value=False)
        self.include_video = tk.BooleanVar(value=False)
        self.prefix        = tk.StringVar(value="DSCF")
        self.start_num     = tk.StringVar(value="1")
        self.cam_offsets   = {1: 0, 2: 0, 3: 0}
        self._running      = False
        self._cancel_evt   = threading.Event()
        self._log_q        = queue.Queue()
        self._alive        = True
        self._drain_id     = None
        self._warned_raw   = False

        self._setup_style()
        self._build()
        self._drain_log()
        self._load_settings()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Style ─────────────────────────────────────────────────────

    def _setup_style(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(
            "App.Horizontal.TProgressbar",
            troughcolor=BORDER, background=ACCENT,
            borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT,
        )
        # tk.Scrollbar is another native Aqua control on macOS and ignored every
        # colour set on it, leaving white bands down the edges of the dark log.
        # ttk under the "clam" theme is drawn by Tk, so these actually apply.
        for orient in ("Vertical", "Horizontal"):
            s.configure(
                f"App.{orient}.TScrollbar",
                troughcolor=PANEL, background=RAISED,
                bordercolor=PANEL, arrowcolor=SUB,
                lightcolor=RAISED, darkcolor=RAISED,
                borderwidth=0, relief="flat",
            )
            s.map(
                f"App.{orient}.TScrollbar",
                background=[("active", "#3a3a3a")],
                arrowcolor=[("active", TXT)],
            )

    # ── Layout skeleton ───────────────────────────────────────────

    def _build(self):
        # Header
        hdr = tk.Frame(self.root, bg=PANEL, height=38)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="Camera Renamer", font=F_H,  fg=TXT, bg=PANEL).pack(side="left", padx=14, pady=9)
        tk.Label(hdr, text="multi-camera sequential renaming", font=F_SM, fg=DIM, bg=PANEL).pack(side="left", pady=12)
        info_btn = _btn(hdr, "?", self._show_info, "normal")
        info_btn.config(font=(FUI, 9, "bold"), padx=8, pady=2)
        info_btn.pack(side="right", padx=10, pady=6)
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

        # Body: left config | separator | right log
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg=PANEL, width=352)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Frame(body, bg=BORDER, width=1).pack(side="left", fill="y")
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        self._build_config(left)
        self._build_log(right)

        # Status bar
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")
        self._build_statusbar()

    # ── Left panel ────────────────────────────────────────────────

    def _sec(self, parent, label):
        f = tk.Frame(parent, bg=PANEL)
        f.pack(fill="x", padx=12, pady=(9, 2))
        tk.Label(f, text=label, font=F_SEC, fg=DIM, bg=PANEL).pack(side="left")

    def _build_config(self, parent):
        # Packed FIRST, to the bottom, so the action row reserves its strip
        # before anything else claims height. Packed last (as it was) it is the
        # first thing clipped when the column overflows — on macOS, where the
        # native font metrics are taller, that hid Preview/Process completely
        # and left no way to start a run.
        tk.Frame(parent, bg=BORDER, height=1).pack(side="bottom", fill="x")
        btns = tk.Frame(parent, bg=PANEL, padx=12, pady=9)
        btns.pack(side="bottom", fill="x")
        _btn(btns, "Clear All", self._clear_all, "danger").pack(side="left")
        tk.Frame(btns, bg=PANEL).pack(side="left", expand=True)
        _btn(btns, "Preview",   lambda: self._run(True),  "normal").pack(side="left", padx=(0, 5))
        _btn(btns, "Process ▶", lambda: self._run(False), "primary").pack(side="left")

        self._sec(parent, "CAMERA SOURCES")
        self.cam_rows = {}
        for i in range(1, 4):
            self.cam_rows[i] = self._cam_row(parent, i)

        self._sec(parent, "OUTPUT FOLDER")
        self._out_row(parent)

        self._sec(parent, "OUTPUT NAMING")
        naming = tk.Frame(parent, bg=CARD)
        naming.pack(fill="x", padx=12, pady=1)
        ninner = tk.Frame(naming, bg=CARD, padx=10, pady=6)
        ninner.pack(fill="x")
        tk.Label(ninner, text="Prefix", font=F_SM, fg=SUB, bg=CARD).pack(side="left")
        pfx = tk.Entry(
            ninner, textvariable=self.prefix, width=8, font=F_SM,
            bg=RAISED, fg=TXT, insertbackground=TXT, relief="flat",
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        pfx.pack(side="left", padx=(6, 14), ipady=2)
        tk.Label(ninner, text="Start #", font=F_SM, fg=SUB, bg=CARD).pack(side="left")
        stn = tk.Entry(
            ninner, textvariable=self.start_num, width=6, font=F_SM,
            bg=RAISED, fg=TXT, insertbackground=TXT, relief="flat",
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        stn.pack(side="left", padx=(6, 0), ipady=2)

        self._sec(parent, "OPTIONS")
        opts = tk.Frame(parent, bg=CARD, padx=10, pady=6)
        opts.pack(fill="x", padx=12, pady=1)
        for text, var, fg in [
            ("Keep RAW files", self.keep_raw, SUB),
            ("Include subfolders  (e.g. DCIM/100_FUJI)", self.include_sub, SUB),
            ("Include video clips  (MOV / MP4)", self.include_video, SUB),
            ("Move files instead of copy  \u26a0 originals deleted",  self.move_files, WARN),
        ]:
            tk.Checkbutton(
                opts, text=text, variable=var,
                bg=CARD, fg=fg, activebackground=CARD, activeforeground=TXT,
                selectcolor=CARD, font=F_SM, highlightthickness=0, bd=0, cursor="hand2",
            ).pack(anchor="w", pady=1)

        # Inline warning
        warn = tk.Frame(parent, bg="#241a08", padx=10, pady=5)
        warn.pack(fill="x", padx=12, pady=(7, 0))
        tk.Label(
            warn,
            text="\u26a0  Output must differ from all source folders",
            font=F_SM, fg=WARN, bg="#241a08",
        ).pack(anchor="w")

        # Takes up whatever height is left over the bottom-packed action row
        tk.Frame(parent, bg=PANEL).pack(fill="both", expand=True)

    def _cam_row(self, parent, n):
        card = tk.Frame(parent, bg=CARD)
        card.pack(fill="x", padx=12, pady=1)
        row = tk.Frame(card, bg=CARD, padx=8, pady=6)
        row.pack(fill="x")

        cv  = tk.Canvas(row, width=8, height=8, bg=CARD, highlightthickness=0)
        cv.pack(side="left", padx=(0, 6))
        dot = cv.create_oval(0, 0, 8, 8, fill=DIM, outline="")

        tk.Label(row, text=f"CAM {n}", font=(FUI, 8, "bold"), fg=SUB, bg=CARD, width=5, anchor="w").pack(side="left")

        lbl = tk.Label(row, text="not set", fg=DIM, bg=CARD, font=F_SM, anchor="w")
        lbl.pack(side="left", fill="x", expand=True, padx=(3, 4))

        xb = _btn(row, "\u2715", lambda x=n: self._clr_cam(x), "ghost")
        xb.config(padx=4, pady=2, font=(FUI, 8))
        xb.pack(side="right", padx=(1, 0))
        brw = _btn(row, "Browse", lambda x=n: self._sel_cam(x), "normal")
        brw.config(pady=2, padx=7, font=F_SM)
        brw.pack(side="right")
        gear = _btn(row, "\u2699", lambda x=n: self._open_cam_settings(x), "ghost")
        gear.config(padx=4, pady=2, font=(FUI, 8))
        gear.pack(side="right", padx=(1, 3))

        return {"cv": cv, "dot": dot, "lbl": lbl, "gear": gear}

    def _out_row(self, parent):
        card = tk.Frame(parent, bg=CARD)
        card.pack(fill="x", padx=12, pady=1)
        row = tk.Frame(card, bg=CARD, padx=8, pady=6)
        row.pack(fill="x")

        self._out_cv  = tk.Canvas(row, width=8, height=8, bg=CARD, highlightthickness=0)
        self._out_cv.pack(side="left", padx=(0, 6))
        self._out_dot = self._out_cv.create_oval(0, 0, 8, 8, fill=DIM, outline="")

        tk.Label(row, text="Output", font=(FUI, 8, "bold"), fg=SUB, bg=CARD, width=5, anchor="w").pack(side="left")

        self.out_lbl = tk.Label(row, text="not set", fg=DIM, bg=CARD, font=F_SM, anchor="w")
        self.out_lbl.pack(side="left", fill="x", expand=True, padx=(3, 4))

        xb = _btn(row, "\u2715", self._clr_out, "ghost")
        xb.config(padx=4, pady=2, font=(FUI, 8))
        xb.pack(side="right", padx=(1, 0))
        brw = _btn(row, "Browse", self._sel_out, "normal")
        brw.config(pady=2, padx=7, font=F_SM)
        brw.pack(side="right")

    # ── Right panel (log) ─────────────────────────────────────────

    def _build_log(self, parent):
        lhdr = tk.Frame(parent, bg=CARD, height=28)
        lhdr.pack(fill="x")
        lhdr.pack_propagate(False)
        lh = tk.Frame(lhdr, bg=CARD)
        lh.pack(fill="both", expand=True, padx=10)
        tk.Label(lh, text="LOG", font=F_SEC, fg=DIM, bg=CARD).pack(side="left", pady=8)
        clr = _btn(lh, "Clear", self._clr_log, "ghost")
        clr.config(pady=1, padx=6, font=F_SM)
        clr.pack(side="right", pady=3)
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x")

        frame = tk.Frame(parent, bg=LOG_BG)
        frame.pack(fill="both", expand=True)

        vsb = ttk.Scrollbar(frame, orient="vertical",   style="App.Vertical.TScrollbar")
        hsb = ttk.Scrollbar(frame, orient="horizontal", style="App.Horizontal.TScrollbar")
        vsb.pack(side="right",  fill="y")
        hsb.pack(side="bottom", fill="x")

        self.log_box = tk.Text(
            frame,
            state="disabled",
            bg=LOG_BG, fg=SUB,
            font=F_LOG, wrap="none",
            relief="flat", bd=0, padx=12, pady=8,
            insertbackground=TXT,
            selectbackground=BORDER,
            yscrollcommand=vsb.set,
            xscrollcommand=hsb.set,
        )
        vsb.config(command=self.log_box.yview)
        hsb.config(command=self.log_box.xview)
        self.log_box.pack(fill="both", expand=True)

        self.log_box.tag_config("info", foreground=SUB)
        self.log_box.tag_config("ok",   foreground=OK)
        self.log_box.tag_config("err",  foreground=ERR)
        self.log_box.tag_config("warn", foreground=WARN)
        self.log_box.tag_config("dim",  foreground=DIM)
        self.log_box.tag_config("head", foreground=ACCENT, font=(FUI, 9, "bold"))

    # ── Status bar ────────────────────────────────────────────────

    def _build_statusbar(self):
        sb = tk.Frame(self.root, bg=PANEL, height=22)
        sb.pack(fill="x")
        sb.pack_propagate(False)
        inner = tk.Frame(sb, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=12)

        self._status = tk.Label(inner, text="Select camera folders to begin", font=F_SM, fg=DIM, bg=PANEL)
        self._status.pack(side="left", pady=3)

        self._prog_pct = tk.Label(inner, text="", font=F_SM, fg=ACCENT, bg=PANEL, width=4)
        self._prog = ttk.Progressbar(inner, length=110, mode="determinate", style="App.Horizontal.TProgressbar")
        self._cancel_btn = _btn(inner, "Cancel", self._request_cancel, "danger")
        self._cancel_btn.config(pady=1, padx=8, font=F_SM)

        # hidden until processing
        self._prog_pct.pack_forget()
        self._prog.pack_forget()

    def _request_cancel(self):
        if self._running:
            self._cancel_evt.set()
            self._status.config(text="Cancelling…", fg=WARN)

    # ── Camera / output actions ───────────────────────────────────

    def _dot(self, cv, dot_id, active):
        cv.itemconfig(dot_id, fill=(OK if active else DIM))

    def _set_cam(self, n, f):
        self.camera_dirs[n] = f
        disp = f if len(f) <= 37 else "\u2026" + f[-34:]
        self.cam_rows[n]["lbl"].config(text=disp, fg=TXT)
        self._dot(self.cam_rows[n]["cv"], self.cam_rows[n]["dot"], True)
        self._refresh_status()

    def _sel_cam(self, n):
        f = filedialog.askdirectory(title=f"Select Camera {n} folder")
        if f:
            self._set_cam(n, f)
            self.log(f"Camera {n} \u2192 {f}", "info")

    def _clr_cam(self, n):
        self.camera_dirs.pop(n, None)
        self.cam_rows[n]["lbl"].config(text="not set", fg=DIM)
        self._dot(self.cam_rows[n]["cv"], self.cam_rows[n]["dot"], False)
        self._refresh_status()

    def _update_gear(self, n):
        offset = self.cam_offsets.get(n, 0)
        self.cam_rows[n]["gear"].config(text="⚙•" if offset else "⚙")

    def _open_cam_settings(self, n):
        win = tk.Toplevel(self.root)
        win.title(f"Camera {n} Settings")
        win.configure(bg=PANEL)
        win.resizable(False, False)
        win.grab_set()

        self.root.update_idletasks()
        rx = self.root.winfo_x() + self.root.winfo_width()  // 2
        ry = self.root.winfo_y() + self.root.winfo_height() // 2
        win.geometry(f"300x180+{rx - 150}+{ry - 90}")

        hdr = tk.Frame(win, bg=CARD, height=36)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text=f"Camera {n}  —  Clock Offset", font=(FUI, 9, "bold"),
                 fg=TXT, bg=CARD).pack(side="left", padx=12, pady=9)
        tk.Frame(win, bg=BORDER, height=1).pack(fill="x")

        body = tk.Frame(win, bg=PANEL, padx=16, pady=14)
        body.pack(fill="both", expand=True)
        tk.Label(
            body,
            text="Corrects a camera whose clock wasn't in sync with the "
                 "others, so shots interleave in the right order.",
            font=F_SM, fg=SUB, bg=PANEL, wraplength=260, justify="left", anchor="w",
        ).pack(fill="x")

        row = tk.Frame(body, bg=PANEL)
        row.pack(fill="x", pady=(12, 0))
        tk.Label(row, text="Offset (seconds)", font=F_SM, fg=SUB, bg=PANEL).pack(side="left")
        val = tk.StringVar(value=str(self.cam_offsets.get(n, 0)))
        sp = tk.Spinbox(
            row, from_=-3600, to=3600, increment=1, textvariable=val, width=8,
            font=F_SM, bg=RAISED, fg=TXT, insertbackground=TXT, relief="flat",
            buttonbackground=RAISED, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        sp.pack(side="right", ipady=2)

        hint = tk.Label(body, text="", font=F_SM, fg=ERR, bg=PANEL, anchor="w")
        hint.pack(fill="x", pady=(4, 0))

        def _save():
            try:
                secs = int(val.get())
            except ValueError:
                hint.config(text="Enter a whole number of seconds.")
                return
            self.cam_offsets[n] = secs
            self._update_gear(n)
            win.destroy()

        tk.Frame(win, bg=BORDER, height=1).pack(fill="x", side="bottom")
        foot = tk.Frame(win, bg=CARD, height=38)
        foot.pack(fill="x", side="bottom")
        foot.pack_propagate(False)
        save = _btn(foot, "Save", _save, "primary")
        save.config(pady=3, padx=12, font=F_SM)
        save.pack(side="right", padx=(6, 12), pady=6)
        cancel = _btn(foot, "Cancel", win.destroy, "normal")
        cancel.config(pady=3, padx=12, font=F_SM)
        cancel.pack(side="right", pady=6)

    def _set_out(self, f):
        self.output_dir = f
        disp = f if len(f) <= 37 else "\u2026" + f[-34:]
        self.out_lbl.config(text=disp, fg=TXT)
        self._dot(self._out_cv, self._out_dot, True)
        self._refresh_status()

    def _sel_out(self):
        f = filedialog.askdirectory(title="Select Output folder")
        if f:
            self._set_out(f)
            self.log(f"Output \u2192 {f}", "info")

    def _clr_out(self):
        self.output_dir = None
        self.out_lbl.config(text="not set", fg=DIM)
        self._dot(self._out_cv, self._out_dot, False)
        self._refresh_status()

    def _clear_all(self):
        # The worker reads camera_dirs/output_dir as it goes — clearing them
        # mid-run left it copying into a folder the UI no longer showed
        if self._running:
            messagebox.showinfo("Busy", "Processing is still running — cancel it first.")
            return
        for i in range(1, 4):
            self._clr_cam(i)
        self._clr_out()
        self._clr_log()

    def _clr_log(self):
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", tk.END)
        self.log_box.config(state="disabled")

    def _refresh_status(self):
        n = len(self.camera_dirs)
        has_out = self.output_dir is not None
        if n == 0 and not has_out:
            self._status.config(text="Select camera folders to begin", fg=DIM)
        elif not has_out:
            self._status.config(text=f"{n} source{'s' if n > 1 else ''} selected \u2014 set output folder", fg=SUB)
        elif n == 0:
            self._status.config(text="Output set \u2014 select at least one camera source", fg=SUB)
        else:
            self._status.config(text=f"{n} camera{'s' if n > 1 else ''}, output set \u2014 ready", fg=OK)

    # ── Logging (thread-safe) ─────────────────────────────────────

    def log(self, msg, tag="info"):
        # Queued, not written directly: per-file after(0) callbacks flood the
        # event loop on large imports; _drain_log flushes in batches instead
        self._log_q.put((msg, tag))

    def _drain_log(self):
        items = []
        while True:
            try:
                items.append(self._log_q.get_nowait())
            except queue.Empty:
                break
        if items and self._alive:
            self.log_box.config(state="normal")
            for msg, tag in items:
                self.log_box.insert(tk.END, msg + "\n", tag)
            self.log_box.see(tk.END)
            self.log_box.config(state="disabled")
        if self._alive:
            self._drain_id = self.root.after(80, self._drain_log)

    def cancelled(self):
        return self._cancel_evt.is_set()

    def _post(self, fn):
        """Schedule fn on the Tk loop from a worker thread. After the window is
        gone `after` raises (RuntimeError/TclError) — a background import that
        outlived the window used to die with a traceback on every update."""
        if not self._alive:
            return
        try:
            self.root.after(0, fn)
        except Exception:
            pass

    # ── Progress (thread-safe) ────────────────────────────────────

    def set_progress(self, frac):
        pct = int(frac * 100)
        def _update():
            if not self._alive:
                return
            self._prog["value"] = pct
            self._prog_pct.config(text=f"{pct}%")
        self._post(_update)

    def finish(self):
        def _done():
            self._running = False
            if not self._alive:
                return
            self._prog.pack_forget()
            self._prog_pct.pack_forget()
            self._cancel_btn.pack_forget()
            self._refresh_status()
        self._post(_done)

    # ── Run ───────────────────────────────────────────────────────

    def _run(self, preview):
        if self._running:
            return

        if not self.camera_dirs:
            messagebox.showerror("Error", "Select at least one camera folder.")
            return
        if not self.output_dir:
            messagebox.showerror("Error", "Select an output folder.")
            return

        ok, msg = validate_folders(self.camera_dirs, self.output_dir)
        if not ok:
            messagebox.showerror("Safety Error", msg)
            self.log(msg, "err")
            return

        prefix = self.prefix.get().strip()
        invalid_chars = set('\\/:*?"<>|')
        if not prefix or any(c in invalid_chars for c in prefix):
            messagebox.showerror(
                "Invalid Prefix",
                "Prefix can't be empty or contain \\ / : * ? \" < > |"
            )
            return
        try:
            start_num = int(self.start_num.get())
            if start_num < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Start Number", "Start # must be a whole number ≥ 1.")
            return

        if not preview and self.move_files.get():
            if not messagebox.askyesno(
                "Confirm Move",
                "MOVE files (not copy)?\n\nOriginals will be permanently deleted from source folders.\n\nContinue?"
            ):
                self.log("Move cancelled.", "warn")
                return

        self._clr_log()
        self.log("\u2500" * 58, "head")
        self.log("PREVIEW MODE" if preview else "PROCESSING", "head")
        self.log("\u2500" * 58, "head")

        self._running = True
        self._cancel_evt.clear()
        self._status.config(text="Processing\u2026", fg=ACCENT)
        self._prog["value"] = 0
        self._prog_pct.config(text="0%")
        self._cancel_btn.pack(side="right", padx=(6, 0), pady=3)
        self._prog.pack(side="right", padx=(0, 4), pady=3)
        self._prog_pct.pack(side="right", pady=3)

        threading.Thread(target=self._worker, args=(preview,), daemon=True).start()

    def _worker(self, preview):
        try:
            renamer = CombinedCameraRenamer(
                output_dir=self.output_dir,
                keep_raw=self.keep_raw.get(),
                move_files=self.move_files.get(),
                recurse=self.include_sub.get(),
                include_video=self.include_video.get(),
                prefix=self.prefix.get().strip(),
                start_num=int(self.start_num.get()),
                camera_offsets=dict(self.cam_offsets),
            )
            for cam_num, cam_path in self.camera_dirs.items():
                renamer.add_camera_source(cam_num, cam_path)

            if not renamer.collect_and_sort_files(self):
                self.finish()
                return

            renamer.rename_and_copy(copy_files=not preview, gui=self)
        except Exception as e:
            # capture now: `e` is deleted when the except block exits,
            # before the deferred lambda runs on the Tk event loop
            msg = str(e)
            self.log(f"Error: {msg}", "err")
            self.root.after(0, lambda: messagebox.showerror("Error", msg))
            self.finish()

    # ── Settings persistence ─────────────────────────────────────

    def _save_settings(self):
        data = {
            "camera_dirs": {str(k): v for k, v in self.camera_dirs.items()},
            "output_dir": self.output_dir,
            "keep_raw": self.keep_raw.get(),
            "include_sub": self.include_sub.get(),
            "include_video": self.include_video.get(),
            "prefix": self.prefix.get(),
            "start_num": self.start_num.get(),
            "cam_offsets": {str(k): v for k, v in self.cam_offsets.items()},
            # move_files intentionally NOT saved — always start in safe copy mode
        }
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            # encoding is explicit: folder names here are often Thai, and the
            # platform default (cp1252/cp874 on Windows) can't encode them —
            # the write raised and settings silently never persisted
            SETTINGS_PATH.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass  # best-effort — losing remembered settings isn't worth alarming the user

    def _load_settings(self):
        data = None
        for path in [SETTINGS_PATH] + LEGACY_SETTINGS_PATHS:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                break
            except Exception:
                continue
        if data is None:
            return

        for k, v in data.get("camera_dirs", {}).items():
            n = int(k)
            if n in self.cam_rows and v and Path(v).exists():
                self._set_cam(n, v)

        out = data.get("output_dir")
        if out and Path(out).exists():
            self._set_out(out)

        self.keep_raw.set(data.get("keep_raw", True))
        self.include_sub.set(data.get("include_sub", False))
        self.include_video.set(data.get("include_video", False))
        self.prefix.set(data.get("prefix", "DSCF"))
        self.start_num.set(str(data.get("start_num", "1")))

        for k, v in data.get("cam_offsets", {}).items():
            n = int(k)
            if n in self.cam_offsets:
                self.cam_offsets[n] = v
                self._update_gear(n)

    def _on_close(self):
        # A move that's mid-flight has files in transit — closing now would
        # leave the copy half-done with no record of where it stopped.
        if self._running:
            if not messagebox.askyesno(
                "Still processing",
                "Files are still being processed.\n\nQuit anyway? "
                "The remaining files will not be transferred."
            ):
                return
            self._cancel_evt.set()

        self._save_settings()
        self._alive = False
        if self._drain_id is not None:
            try:
                self.root.after_cancel(self._drain_id)
            except Exception:
                pass
        self.root.destroy()

    # ── Info dialog ───────────────────────────────────────────────

    def _show_info(self):
        win = tk.Toplevel(self.root)
        win.title("About / Help")
        win.configure(bg=PANEL)
        win.resizable(False, False)
        win.grab_set()

        # Center relative to main window
        self.root.update_idletasks()
        rx = self.root.winfo_x() + self.root.winfo_width()  // 2
        ry = self.root.winfo_y() + self.root.winfo_height() // 2
        win.geometry(f"480x520+{rx - 240}+{ry - 260}")

        # ── Header strip
        hdr = tk.Frame(win, bg=CARD, height=44)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="Camera Renamer  \u2014  Help & Reference",
                 font=(FUI, 10, "bold"), fg=TXT, bg=CARD).pack(side="left", padx=14, pady=12)
        tk.Frame(win, bg=BORDER, height=1).pack(fill="x")

        # ── Scrollable content
        canvas = tk.Canvas(win, bg=PANEL, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview,
                            style="App.Vertical.TScrollbar")
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)

        content = tk.Frame(canvas, bg=PANEL)
        cw = canvas.create_window((0, 0), window=content, anchor="nw")

        def _on_resize(e):
            canvas.itemconfig(cw, width=e.width)
        canvas.bind("<Configure>", _on_resize)
        content.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        def _wheel(e):
            # Wheel deltas are per-platform: Windows sends multiples of 120,
            # macOS sends small counts (±1..±3), X11 sends Button-4/5 instead.
            # Flooring by 120 everywhere made scroll-up a no-op on macOS.
            if e.num == 4:
                step = -1
            elif e.num == 5:
                step = 1
            elif IS_WIN:
                step = -(e.delta // 120)
            else:
                step = -e.delta
            canvas.yview_scroll(step, "units")

        # bind_all is global — release it when the dialog closes, or scrolling
        # in the main window would hit the destroyed canvas and raise TclError
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            canvas.bind_all(seq, _wheel)

        def _release(e):
            if e.widget is win:
                for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                    canvas.unbind_all(seq)
        win.bind("<Destroy>", _release)

        # ── Section helper
        def section(title):
            f = tk.Frame(content, bg=PANEL)
            f.pack(fill="x", padx=16, pady=(14, 4))
            tk.Label(f, text=title, font=F_SEC, fg=DIM, bg=PANEL).pack(side="left")
            tk.Frame(f, bg=BORDER, height=1).pack(side="left", fill="x", expand=True, padx=(8, 0))

        def para(text, fg=SUB, bold=False):
            font = (FUI, 9, "bold") if bold else F_SM
            tk.Label(content, text=text, font=font, fg=fg, bg=PANEL,
                     wraplength=436, justify="left", anchor="w").pack(fill="x", padx=16, pady=1)

        def row_pair(label, value, val_fg=SUB):
            f = tk.Frame(content, bg=CARD)
            f.pack(fill="x", padx=16, pady=1)
            inner = tk.Frame(f, bg=CARD, padx=10, pady=5)
            inner.pack(fill="x")
            tk.Label(inner, text=label, font=(FUI, 8, "bold"), fg=TXT, bg=CARD, width=14, anchor="w").pack(side="left")
            tk.Label(inner, text=value, font=F_SM, fg=val_fg, bg=CARD, anchor="w").pack(side="left", fill="x", expand=True)

        def spacer(h=4):
            tk.Frame(content, bg=PANEL, height=h).pack()

        # ── HOW IT WORKS
        section("HOW IT WORKS")
        para("Point each camera slot at a source folder. The tool reads the EXIF "
             "DateTimeOriginal tag from every photo and sorts all shots across all "
             "cameras into a single chronological sequence. Files are then renamed "
             "DSCF0001, DSCF0002 \u2026 into the output folder.", fg=SUB)
        spacer()
        para("RAW + JPEG pairs that share the same base name are treated as one "
             "shot and receive the same sequence number, keeping them together.", fg=SUB)

        # ── SUPPORTED FORMATS
        section("SUPPORTED FORMATS")
        row_pair("JPEG / HEIC", ".jpg  .jpeg  .heic  .heif")
        row_pair("Fujifilm RAW", ".raf",  val_fg=ACCENT)
        row_pair("Sony RAW",    ".arw",  val_fg=ACCENT)
        row_pair("Canon RAW",   ".cr2  .cr3",  val_fg=ACCENT)
        row_pair("Nikon RAW",   ".nef",  val_fg=ACCENT)
        row_pair("Adobe DNG",   ".dng",  val_fg=ACCENT)
        row_pair("Video",       ".mov  .mp4", val_fg=WARN)
        spacer()
        para("RAW formats are highlighted above. Un-tick \u201cKeep RAW files\u201d "
             "to skip them and output JPEG/HEIC only. Video is off by default "
             "\u2014 tick \u201cInclude video clips\u201d to fold MOV/MP4 into "
             "the same sequence.", fg=DIM)
        spacer()
        para("Tick \u201cInclude subfolders\u201d to scan nested folders such as "
             "DCIM/100_FUJI instead of only the top level.", fg=DIM)

        # ── TIMESTAMP PRIORITY
        section("TIMESTAMP PRIORITY")
        row_pair("1st choice", "EXIF DateTimeOriginal tag")
        row_pair("Fallback",   "File modification time (mtime)", val_fg=WARN)
        spacer()
        para("Using file mtime as fallback can cause incorrect ordering if files "
             "were copied or transferred — always shoot with correct camera clocks.", fg=DIM)

        # ── OUTPUT NAMING
        section("OUTPUT NAMING")
        row_pair("Pattern",   "PREFIX####  (zero-padded to 4 digits)")
        row_pair("Example",   "DSCF0001.jpg   DSCF0001.RAF", val_fg=OK)
        row_pair("Overflow",  "DSCF10000, DSCF10001 \u2026 (5+ digits when needed)")
        spacer()
        para("Prefix and start number are set in the OUTPUT NAMING panel on "
             "the left \u2014 handy for continuing a sequence into a folder "
             "that already has numbered shots from an earlier session.", fg=DIM)

        # \u2500\u2500 CAMERA CLOCK OFFSET
        section("CAMERA CLOCK OFFSET")
        para("Click the \u2699 next to a camera slot to nudge that camera's "
             "timestamps by a number of seconds (positive or negative). "
             "Use it when two camera clocks weren't in sync, so shots from "
             "both still interleave in the order they were actually taken.", fg=SUB)

        # \u2500\u2500 REMEMBERED SETTINGS
        section("REMEMBERED SETTINGS")
        para("Camera/output folders, format options, prefix, start number "
             "and clock offsets are remembered between launches. \u201cMove "
             "files\u201d always resets to off on startup as a safety default.", fg=DIM)

        # ── COPY VS MOVE
        section("COPY  VS  MOVE")
        para("\u201cCopy\u201d (default) duplicates files and leaves originals untouched. "
             "\u201cMove\u201d transfers files and deletes the originals from source folders. "
             "Move cannot be undone \u2014 a confirmation prompt is shown before it runs.", fg=SUB)

        # ── SAFETY RULES
        section("SAFETY RULES")
        para("\u26a0  The output folder must be different from all source folders.",
             fg=WARN, bold=True)
        spacer(2)
        para("\u2022  Output cannot equal any camera source folder.",   fg=DIM)
        para("\u2022  Output cannot be nested inside a source folder.", fg=DIM)
        para("\u2022  A source folder cannot be nested inside output.", fg=DIM)

        spacer(10)

        # ── Close button
        tk.Frame(win, bg=BORDER, height=1).pack(fill="x")
        foot = tk.Frame(win, bg=CARD, height=38)
        foot.pack(fill="x")
        foot.pack_propagate(False)
        close = _btn(foot, "Close", win.destroy, "normal")
        close.config(pady=3, padx=12, font=F_SM)
        close.pack(side="right", padx=12, pady=6)


# ── Entry point ───────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    app = CameraRenamerGUI(root)
    root.mainloop()
