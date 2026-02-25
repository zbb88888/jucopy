#!/usr/bin/env python3
"""
jucopy - Automatically copy selected text to clipboard using eBPF

On Linux desktops (X11 and XWayland), selecting text puts it in the PRIMARY
selection but NOT in the CLIPBOARD.  jucopy uses an eBPF uprobe on
XSetSelectionOwner() inside libX11 to detect every selection change and
immediately syncs PRIMARY -> CLIPBOARD, so selected text is also pasteable
with Ctrl+V.

Requirements
------------
  - Linux kernel >= 5.8 (with eBPF ring-buffer support)
  - python3-bpfcc  (BCC Python bindings)
  - libx11-6       (libX11.so.6 must be present)
  - xclip OR xsel  (for X11 / XWayland)
  - wl-clipboard   (for Wayland-native apps, optional)
"""

import argparse
import ctypes
import ctypes.util
import glob
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEBOUNCE_S = 0.05
POLL_TIMEOUT_MS = 100
RINGBUF_PAGES = 8

# ---------------------------------------------------------------------------
# eBPF program (Ring-buffer only, for modern kernels)
# ---------------------------------------------------------------------------

BPF_TEXT = r"""
#include <uapi/linux/ptrace.h>

#define XA_PRIMARY_ATOM  PRIMARY_ATOM_PLACEHOLDER

struct event_t {
    u32 pid;
    char comm[16];
};

BPF_RINGBUF_OUTPUT(selection_events, RINGBUF_PAGES_PLACEHOLDER);

static __always_inline int is_sync_tool(char comm[16])
{
    /* xclip */
    if (comm[0]=='x' && comm[1]=='c' && comm[2]=='l' && comm[3]=='i' && comm[4]=='p' && comm[5]=='\0') return 1;
    /* xsel */
    if (comm[0]=='x' && comm[1]=='s' && comm[2]=='e' && comm[3]=='l' && comm[4]=='\0') return 1;
    /* wl-copy */
    if (comm[0]=='w' && comm[1]=='l' && comm[2]=='-' && comm[3]=='c' && comm[4]=='o' && comm[5]=='p' && comm[6]=='y' && comm[7]=='\0') return 1;
    return 0;
}

int trace_xset_selection(struct pt_regs *ctx)
{
    unsigned long selection = PT_REGS_PARM2(ctx);
    if (selection != XA_PRIMARY_ATOM) return 0;

    char comm[16] = {};
    bpf_get_current_comm(&comm, sizeof(comm));
    if (is_sync_tool(comm)) return 0;

    struct event_t *event = selection_events.ringbuf_reserve(sizeof(struct event_t));
    if (!event) return 0;

    event->pid = bpf_get_current_pid_tgid() >> 32;
    __builtin_memcpy(event->comm, comm, sizeof(event->comm));
    selection_events.ringbuf_submit(event, 0);
    return 0;
}
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def check_root() -> None:
    """Ensure the script is running with root privileges."""
    if os.geteuid() != 0:
        print("Error: root privileges required. Use: sudo python3 jucopy.py", file=sys.stderr)
        sys.exit(1)


def find_libx11() -> Optional[str]:
    """Find absolute path to libX11.so.6."""
    path = ctypes.util.find_library("X11")
    if path and os.path.isabs(path):
        return path

    # 1. Try ldconfig
    try:
        res = subprocess.check_output(["ldconfig", "-p"], text=True, timeout=2)
        for line in res.splitlines():
            if "libX11.so.6" in line and "=>" in line:
                return line.split("=>")[1].strip()
    except: pass

    # 2. Try scanning /proc/*/maps (useful for Snap/Flatpak/non-standard paths)
    for maps in glob.glob("/proc/[0-9]*/maps"):
        try:
            with open(maps, "r") as f:
                for line in f:
                    if "libX11.so.6" in line:
                        p = line.split()[-1]
                        if os.path.isfile(p): return p
        except: continue

    # 3. Common multiarch paths
    for p in ["/usr/lib/x86_64-linux-gnu/libX11.so.6", "/usr/lib/aarch64-linux-gnu/libX11.so.6"]:
        if os.path.exists(p): return p
    return None


def setup_xauth() -> None:
    """Detect and set XAUTHORITY for the current user."""
    if os.environ.get("XAUTHORITY"): return
    try:
        user = os.environ.get("SUDO_USER") or os.getlogin()
        if not user or user == "root": return

        # Check ~/.Xauthority
        path = os.path.expanduser(f"~{user}/.Xauthority")
        if os.path.exists(path):
            os.environ["XAUTHORITY"] = path
            return

        # Check /run/user/UID/xauth_*
        import pwd
        uid = pwd.getpwnam(user).pw_uid
        matches = glob.glob(f"/run/user/{uid}/xauth_*")
        if matches: os.environ["XAUTHORITY"] = matches[0]
    except: pass


def resolve_primary_atom(display: str, libx11_path: str) -> int:
    """Resolve the PRIMARY atom ID using libX11."""
    try:
        libx11 = ctypes.cdll.LoadLibrary(libx11_path)
        libx11.XOpenDisplay.restype = ctypes.c_void_p
        libx11.XInternAtom.restype = ctypes.c_ulong

        dpy = libx11.XOpenDisplay(display.encode())
        if not dpy: return 1
        atom = libx11.XInternAtom(dpy, b"PRIMARY", 0)
        libx11.XCloseDisplay(dpy)
        return int(atom) or 1
    except:
        return 1


def sync_selection(display: str, verbose: bool) -> None:
    """Copy PRIMARY selection to CLIPBOARD."""
    env = os.environ.copy()
    env["DISPLAY"] = display

    # helper to run with isolation
    def _run(cmd, input_data=None):
        return subprocess.run(cmd, input=input_data, env=env, capture_output=True, timeout=2, start_new_session=True)

    # 1. Try xclip
    try:
        res = _run(["xclip", "-o", "-selection", "primary"])
        if res.returncode == 0 and res.stdout.strip():
            _run(["xclip", "-selection", "clipboard"], res.stdout)
            if verbose: print(f"  Synced (xclip): {res.stdout.decode(errors='replace')[:50]!r}...")
            return
    except: pass

    # 2. Try xsel
    try:
        res = _run(["xsel", "--primary", "--output"])
        if res.returncode == 0 and res.stdout.strip():
            _run(["xsel", "--clipboard", "--input"], res.stdout)
            if verbose: print(f"  Synced (xsel): {res.stdout.decode(errors='replace')[:50]!r}...")
            return
    except: pass

    # 3. Try wl-clipboard
    if env.get("WAYLAND_DISPLAY"):
        try:
            res = _run(["wl-paste", "--primary", "--no-newline"])
            if res.returncode == 0 and res.stdout.strip():
                _run(["wl-copy"], res.stdout)
                if verbose: print(f"  Synced (wl-clipboard): {res.stdout.decode(errors='replace')[:50]!r}...")
                return
        except: pass


def _sync_worker(q: queue.Queue, display: str, verbose: bool, stop: threading.Event) -> None:
    """Background worker to perform sync operations."""
    while not stop.is_set():
        try:
            q.get(timeout=0.2)
            while not q.empty(): q.get_nowait() # Coalesce events
            sync_selection(display, verbose)
            time.sleep(DEBOUNCE_S)
        except queue.Empty: continue


def run(libx11: str, display: str, verbose: bool, atom: int) -> None:
    """Main eBPF loop."""
    try:
        from bcc import BPF
    except ImportError:
        print("Error: python3-bpfcc not installed.", file=sys.stderr)
        sys.exit(1)

    src = BPF_TEXT.replace("PRIMARY_ATOM_PLACEHOLDER", str(atom)).replace("RINGBUF_PAGES_PLACEHOLDER", str(RINGBUF_PAGES))
    b = BPF(text=src)
    b.attach_uprobe(name=libx11, sym="XSetSelectionOwner", fn_name="trace_xset_selection")

    sync_q: queue.Queue = queue.Queue(maxsize=1)
    stop_ev = threading.Event()
    worker = threading.Thread(target=_sync_worker, args=(sync_q, display, verbose, stop_ev), daemon=True)
    worker.start()

    def callback(cpu: Any, data: Any, size: Any) -> None:
        try: sync_q.put_nowait(True)
        except queue.Full: pass

    b["selection_events"].open_ring_buffer(callback)
    print(f"jucopy is running on {display} (eBPF mode). Press Ctrl+C to stop.")

    try:
        while True:
            b.ring_buffer_poll(timeout=POLL_TIMEOUT_MS)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        stop_ev.set()
        b.cleanup()


def main():
    parser = argparse.ArgumentParser(description="eBPF-based PRIMARY -> CLIPBOARD sync")
    parser.add_argument("--display", default=os.environ.get("DISPLAY", ":0"), help="X11 display")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()

    check_root()
    libx11 = find_libx11()
    if not libx11:
        print("Error: libX11.so.6 not found.", file=sys.stderr)
        sys.exit(1)

    setup_xauth()
    atom = resolve_primary_atom(args.display, libx11)
    run(libx11, args.display, args.verbose, atom)


if __name__ == "__main__":
    main()
