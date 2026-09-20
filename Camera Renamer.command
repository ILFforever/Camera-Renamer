#!/bin/zsh
# Double-click to launch Camera Renamer.
#
# Must NOT run under /usr/bin/python3: the CommandLineTools Python links
# Apple's deprecated Tk 8.5.9, where native Aqua widgets ignore every colour
# the app sets and the layout clips. Homebrew's python@3.12 carries Tk 9.
PY=/opt/homebrew/bin/python3.12
APP="$(dirname "$0")/camera_renamer_gui.py"

if [ ! -x "$PY" ]; then
  echo "Homebrew python3.12 not found at $PY"
  echo "Install with:  brew install python@3.12 python-tk@3.12"
  read "?Press return to close."
  exit 1
fi

exec "$PY" "$APP"
