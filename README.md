# jucopy

Just copy it while it is selected.

Automatically copies selected text to the clipboard — no extra key press needed.

---

## Chrome Extension (`chrome/`)

### Installation

1. Go to `chrome://extensions/` in your Chrome browser
2. Enable **Developer mode** (toggle in the top right)
3. Click **Load unpacked** and select the `chrome/` directory

### Usage

Once installed, any text you select on any webpage is automatically copied to your clipboard.

---

## Linux Desktop (`linux/`) — Ubuntu 24.04

Monitors the X11 primary selection and syncs it to the clipboard whenever it changes.

### Requirements

```bash
sudo apt install xclip
```

### Usage

```bash
python3 linux/jucopy.py
```

### Run at startup

Add the command above to your desktop session **Startup Applications** so it runs automatically when you log in.