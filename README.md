# Camera Renamer

Merges photos from up to three cameras into a single chronological sequence and
renames them `PREFIX0001`, `PREFIX0002`, … into an output folder.

Built for multi-photographer shoots, where two or three bodies cover the same
event and the deliverable has to read as one continuous take rather than three
interleaved card dumps.

![Tk](https://img.shields.io/badge/GUI-tkinter-blue) ![Python](https://img.shields.io/badge/python-3.9%2B-blue)

---

## What it does

- **Chronological merge across cameras.** Every shot is sorted by its EXIF
  `DateTimeOriginal`, not by filename or card order, so shots from different
  bodies interleave in the order they were actually taken.
- **RAW + JPEG stay together.** Files sharing a base name in the same folder are
  treated as one shot and get the same sequence number — `HM_0001.JPG` and
  `HM_0001.RAF`.
- **Per-camera clock offset.** If one body's clock was out of sync, nudge it by
  ±3600 seconds (the ⚙ next to each camera slot) and the interleaving corrects
  itself.
- **Preview before you commit.** Preview mode prints the full rename plan and
  writes nothing.
- **Copy or move.** Copy is the default and leaves originals untouched. Move
  requires an explicit confirmation and always resets to off at launch.

## Supported formats

| Kind | Extensions |
|---|---|
| JPEG / HEIC | `.jpg` `.jpeg` `.heic` `.heif` |
| RAW | `.raf` `.arw` `.cr2` `.cr3` `.nef` `.dng` |
| Video (opt-in) | `.mov` `.mp4` |

HEIC/HEIF are treated as final images, not RAW — they're the iPhone default.

---

## Requirements

- **Python 3.9+**
- **Tk 8.6 or newer** — see the macOS note below, this one matters
- `piexif` — required
- `exifread` — optional but **strongly recommended**; it is the only way to read
  EXIF out of RAW containers. Without it, a RAW shot with no JPEG sibling falls
  back to file mtime, which silently breaks the chronological ordering this tool
  exists to produce. The app warns in the log when this happens.

```sh
pip install piexif exifread
```

---

## Running it

### macOS

**Do not run this with the system `python3`.** `/usr/bin/python3`
(CommandLineTools) links Apple's deprecated **Tk 8.5.9**, where native Aqua
widgets discard every colour the app sets — buttons render as white blobs on the
dark theme — and the layout clips.

```sh
brew install python@3.12 python-tk@3.12
/opt/homebrew/bin/python3.12 -m pip install --user --break-system-packages piexif exifread
/opt/homebrew/bin/python3.12 camera_renamer_gui.py
```

Or just double-click **`Camera Renamer.command`**, which pins the correct
interpreter for you.

> `--break-system-packages` is needed because Homebrew's Python is
> [PEP 668](https://peps.python.org/pep-0668/) externally-managed. Combined with
> `--user` it installs to `~/Library/Python/3.12/`, leaving Homebrew's own tree
> untouched so `brew upgrade` won't fight it.

### Windows

The python.org installer bundles a current Tk, so nothing special is needed:

```
py -m pip install piexif exifread
py camera_renamer_gui.py
```

### Linux

```sh
sudo apt install python3-tk        # or your distro's equivalent
pip install piexif exifread
python3 camera_renamer_gui.py
```

---

## How ordering is decided

For each shot, in order:

1. **EXIF `DateTimeOriginal`** via `piexif` — works for JPEG/HEIC.
2. **EXIF via `exifread`** — the RAW path. Skipped entirely if not installed.
3. **File mtime** — last resort, and unreliable: copying or transferring files
   rewrites it.

Sub-second precision (`SubSecTimeOriginal`) is used when present, so bursts keep
their true order. When two shots still tie, the original filename breaks it,
which preserves in-camera burst order.

Timestamps are read from the **JPEG member** of a RAW+JPEG pair where one
exists, since that avoids the RAW path entirely.

---

## Safety

The tool refuses to run when the output folder would endanger the sources:

- Output identical to any camera source folder
- Output nested **inside** a source folder
- A source folder nested **inside** the output folder
- Two camera slots pointing at the same folder (every shot processed twice)

Path comparison is **case-insensitive on Windows and macOS**, because both use
case-insensitive filesystems by default — `/Photos` and `/photos` are the same
folder, and treating them as distinct would let a move delete originals into the
folder it was reading from.

It also **never overwrites**. If any planned destination name already exists in
the output folder, the run aborts before writing anything, and Preview reports
the same clash up front.

macOS AppleDouble sidecars (`._NAME.JPG`, which FAT32/exFAT cards accrue once
Finder touches them) are skipped — they share the real file's extension and would
otherwise be counted and renamed as their own shots.

---

## Settings

Remembered between launches: camera and output folders, format options, prefix,
start number, clock offsets. **Move mode is deliberately not saved** — it always
starts off.

| OS | Location |
|---|---|
| macOS | `~/Library/Application Support/CameraRenamer/settings.json` |
| Windows | `%APPDATA%\CameraRenamer\settings.json` |
| Linux | `$XDG_CONFIG_HOME/CameraRenamer/settings.json` (or `~/.config/…`) |

Written as UTF-8. Folder names on these shoots are frequently Thai, and the
platform default encoding on Windows cannot represent them — the save would
throw and settings would silently never persist.

---

## Notes

Fonts are resolved at runtime to whichever Thai-capable face is actually
installed (`Thonburi` on macOS, `Leelawadee UI` on Windows, `Noto Sans Thai` on
Linux), falling back gracefully. Tk 8.6 has no complex-script shaping, so a font
that merely *contains* Thai glyphs will still misplace tone marks — the
preference lists exist to avoid exactly that.

Buttons and scrollbars are deliberately **not** `tk.Button` / `tk.Scrollbar`.
Both are native Aqua controls on macOS that ignore every colour set on them; the
app uses a `tk.Label`-based button and `ttk` scrollbars under the `clam` theme,
which Tk draws itself and which therefore theme consistently on all three
platforms.
