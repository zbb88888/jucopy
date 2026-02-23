#!/usr/bin/env python3
"""jucopy for Linux desktop (Ubuntu 24.04)

Monitors the X11 primary selection and copies it to the clipboard
whenever it changes, so that selected text is immediately available
via Ctrl+V without any extra key press.

Requirements:
    sudo apt install xclip
    (Python 3 is included with Ubuntu 24.04)

Usage:
    python3 jucopy.py

Run at startup:
    Add the command above to your desktop session startup applications.
"""

import subprocess
import time

POLL_INTERVAL_SECONDS = 0.3


def get_primary():
    try:
        return subprocess.check_output(
            ["xclip", "-selection", "primary", "-o"],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="replace")
    except subprocess.CalledProcessError:
        return ""


def set_clipboard(text):
    result = subprocess.run(
        ["xclip", "-selection", "clipboard", "-i"],
        input=text.encode("utf-8"),
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def main():
    last = ""
    while True:
        current = get_primary()
        if current and current != last:
            set_clipboard(current)
            last = current
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
