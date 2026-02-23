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

## Linux Desktop — Ubuntu 24.04 (`linux-desk/`, eBPF)

**jucopy** uses eBPF to make text selection behave like a real "copy": whatever
you highlight with the mouse is immediately available for `Ctrl+V` paste —
no need to press `Ctrl+C` first.

### How it works

On Linux, selecting text places it in the **PRIMARY** selection (paste with
middle-click) but *not* in the **CLIPBOARD** (paste with `Ctrl+V`).

jucopy attaches an **eBPF uprobe** to the `XSetSelectionOwner()` function
inside `libX11.so.6`.  Every time an X11 or XWayland application claims
ownership of the PRIMARY selection (i.e. every time the user finishes
highlighting text), the uprobe fires and a user-space handler immediately
copies PRIMARY → CLIPBOARD using `xclip`.

```
highlight text
      │
      ▼
XSetSelectionOwner()  ← eBPF uprobe fires
      │
      ▼
jucopy user-space handler
      │
      ├─ xclip -o -selection primary
      │        │
      └─────── xclip -selection clipboard
```

### Requirements

| Dependency | Package (apt) | Purpose |
|---|---|---|
| Linux kernel ≥ 4.14 | *(kernel)* | eBPF support |
| BCC Python bindings | `python3-bpfcc` | Compile & load eBPF program |
| libX11 | `libx11-6` | uprobe target |
| xclip | `xclip` | Clipboard tool (X11/XWayland) |
| xsel *(optional)* | `xsel` | Alternative clipboard tool |
| wl-clipboard *(optional)* | `wl-clipboard` | Wayland PRIMARY sync |

### Quick start

```bash
# Install dependencies
sudo apt install python3-bpfcc libx11-6 xclip

# Run (requires root to load eBPF programs)
sudo python3 linux-desk/jucopy.py
```

Or use the installer:

```bash
sudo bash linux-desk/install.sh
sudo jucopy
```

### Usage

```
sudo python3 linux-desk/jucopy.py [--display DISPLAY] [--verbose]

Options:
  --display DISPLAY  X11 display (default: $DISPLAY or :0)
  -v, --verbose      Print each sync event
```

### Run as a service

```bash
sudo bash linux-desk/install.sh   # installs to /usr/local/bin and /etc/systemd/system/
sudo systemctl enable --now jucopy
```

### Wayland

For **XWayland** apps (most apps on Ubuntu 24.04 Wayland sessions), jucopy
works transparently because they still call `XSetSelectionOwner()`.

For fully **Wayland-native** apps, jucopy falls back to `wl-paste`/`wl-copy`
from the `wl-clipboard` package if it is installed.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `Error: BCC Python bindings not found` | `sudo apt install python3-bpfcc` |
| `Error: libX11.so.6 not found` | `sudo apt install libx11-6` |
| Clipboard not syncing | Install `xclip`: `sudo apt install xclip` |
| Permission denied | Run with `sudo` |

