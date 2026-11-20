#!/bin/bash
# Runs the app with Python on a Mac (for whoever maintains it). Managers should use the built app.
cd "$(dirname "$0")"
if [ ! -x .venv-mac/bin/python ]; then
  echo "First run: setting up. This takes a minute..."
  PY=$(command -v python3.12 || command -v python3) || { echo "Install Python 3.12 first."; read -r _; exit 1; }
  "$PY" -m venv .venv-mac && .venv-mac/bin/python -m pip install -r requirements.txt
fi
.venv-mac/bin/python app.py "$@"
