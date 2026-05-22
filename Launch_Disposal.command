#!/bin/bash
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python3 not found. Please install Python first."
    read -n 1 -s -r -p "Press any key to close..."
    exit 1
fi

python3 -m pip install -r requirements_disposal.txt --quiet
python3 disposal_app.py
