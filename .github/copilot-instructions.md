# jucopy – AI Coding Agent Instructions

## Project Overview

jucopy implements one concept ("select text → auto-copy to clipboard") across three independent, non-shared implementations:

| Directory | Target | Mechanism |
|---|---|---|
| `linux-desk/` | Linux desktop (Ubuntu 24.04 / Debian 12+) | eBPF uprobe on `libX11.so.6:XSetSelectionOwner()` |
| `linux/` | Linux desktop, no-root fallback | Polling `xclip -selection primary` every 300 ms |
| `chrome/` | Chrome/Chromium browser | `selectionchange` event + `navigator.clipboard.writeText()` |

**These components share no code.** Each directory is self-contained. `linux-desk/jucopy.py` is the flagship production implementation.

## linux-desk Architecture

The critical invariant: **the eBPF callback must never block**. All I/O is delegated to a consumer thread.

```
XSetSelectionOwner() → uprobe → is_sync_tool() filter → ringbuf_submit()
                                                              ↓
                                             handle_event() – put_nowait(True)
                                                              ↓
                                             _sync_worker thread (daemon)
                                                              ↓
                                             _run([xclip/xsel/wl-copy])
```

Key structural decisions:
- `queue.Queue(maxsize=1)` — only a "work needed" signal, not event data. Full queue means previous sync is pending → silently coalesced via `put_nowait`.
- **Dual eBPF programs**: `BPF_TEXT_RINGBUF` (kernel ≥ 5.8) and `BPF_TEXT_PERFBUF` (fallback). Selected at runtime by `get_kernel_version()`. Placeholder tokens `PRIMARY_ATOM_PLACEHOLDER` and `RINGBUF_PAGES_PLACEHOLDER` are replaced with `str.replace()` before compilation.
- **Comm filter `is_sync_tool()`** lives in eBPF (kernel-space), not Python. This prevents the feedback loop: xclip's own `XSetSelectionOwner(PRIMARY)` call would re-trigger a sync. Uses byte-by-byte char comparisons (not `bpf_strncmp()`, which requires kernel ≥ 5.17).
- **`contextlib.ExitStack`** in `run()` guarantees `b.cleanup()` and `stop_event.set()` run on any exit path including exceptions.

## Running & Debugging

```bash
# Always requires root for eBPF
sudo python3 linux-desk/jucopy.py --verbose

# If DISPLAY is lost under sudo:
sudo -E python3 linux-desk/jucopy.py
sudo DISPLAY=:0 python3 linux-desk/jucopy.py

# Install as systemd service
sudo bash linux-desk/install.sh
sudo systemctl enable --now jucopy
journalctl -u jucopy -f
```

No build step. No test suite. Runtime dependencies: `python3-bpfcc`, `libx11-6`, `xclip` (or `xsel`, `wl-clipboard`).

## Project-Specific Patterns

### `_run()` helper for subprocesses
Never use `**kwargs` dict unpacking with `subprocess.run()` — Pylance cannot resolve the overloads and produces ~50 errors. Always call the typed `_run(cmd, env, stdin_data)` helper:
```python
primary = _run(["xclip", "-o", "-selection", "primary"], env)
_run(["xclip", "-selection", "clipboard"], env, primary.stdout)
```

### Type annotations required on all functions
The project targets Pylance strict mode. Every function parameter needs an explicit annotation. Use `Optional[str]` (from `typing`) for nullable strings. BCC API calls that are untyped use `# type: ignore[union-attr]` or `# type: ignore[import-untyped]` locally — do not suppress entire files.

### `find_libx11()` resolution chain
Four-step cascade: ctypes.util → multiarch globs → `/proc/*/maps` scan (`find_libx11_from_proc_maps()`) → ldconfig fallback. When adding a new discovery method, insert it into this chain in `find_libx11()`, not as a standalone call.

### `setup_xauth()` must run before any X11 subprocess
Under `sudo`, `XAUTHORITY` is lost. `setup_xauth()` auto-detects it from `SUDO_USER`'s `~/.Xauthority` or `$XDG_RUNTIME_DIR/xauth_*`. Always called in `main()` before `sync_selection()` can be invoked.

### eBPF source modifications
Both `BPF_TEXT_RINGBUF` and `BPF_TEXT_PERFBUF` must stay in sync for logic changes (e.g. `is_sync_tool()` filter). They share identical C logic but differ only in output mechanism (`ringbuf_reserve/submit` vs `perf_submit`).

## chrome/ Extension

Minimal: `manifest.json` (MV3, `clipboardWrite` permission) + `content.js` (debounced `selectionchange` listener, 300 ms). Load unpacked from `chrome://extensions/` in developer mode.

## File Map

```
linux-desk/jucopy.py   — production daemon (eBPF + Python)
linux-desk/install.sh  — copies to /usr/local/bin, installs systemd unit
linux-desk/jucopy.service — systemd unit (requires manual XAUTHORITY path)
linux/jucopy.py        — legacy polling implementation (no eBPF, no root)
chrome/content.js      — browser extension entry point
chrome/manifest.json   — MV3 manifest
```

## Reply Language

Just respond in Chinese. Do not use English if i not specifically requested.