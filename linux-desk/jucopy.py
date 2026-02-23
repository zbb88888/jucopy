#!/usr/bin/env python3
"""
jucopy - Automatically copy selected text to clipboard using eBPF

On Linux desktops (X11 and XWayland), selecting text puts it in the PRIMARY
selection but NOT in the CLIPBOARD.  jucopy uses an eBPF uprobe on
XSetSelectionOwner() inside libX11 to detect every selection change and
immediately syncs PRIMARY -> CLIPBOARD, so selected text is also pasteable
with Ctrl+V.

Architecture
------------
  eBPF uprobe (kernel)          User-space
  ┌──────────────────┐    ┌─────────────────────────────┐
  │ XSetSelectionOwner│    │  perf-buffer callback       │
  │   ↓               │    │    ↓ (non-blocking put)     │
  │ filter XA_PRIMARY │───>│  Queue ──> sync_worker thr  │
  │ perf_submit()     │    │              ↓               │
  └──────────────────┘    │     xclip/xsel/wl-copy      │
                           └─────────────────────────────┘

Requirements
------------
  - Linux kernel >= 4.14 with eBPF support
  - python3-bpfcc  (BCC Python bindings)
  - libx11-6       (libX11.so.6 must be present)
  - xclip OR xsel  (for X11 / XWayland)
  - wl-clipboard   (for Wayland-native apps, optional)

Usage
-----
  sudo python3 jucopy.py [--display :0] [--verbose]
"""

import argparse
import ctypes.util
import glob
import os
import queue
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Debounce window on the consumer side (seconds).
# The sync worker sleeps this long after each sync to coalesce rapid events.
DEBOUNCE_S = 0.05

# Timeout for the perf-buffer poll loop (milliseconds).
POLL_TIMEOUT_MS = 100

# ---------------------------------------------------------------------------
# eBPF program (compiled at runtime by BCC)
# ---------------------------------------------------------------------------
BPF_TEXT = r"""
#include <uapi/linux/ptrace.h>

/* X11 predefined Atom values (from <X11/Xatom.h>) */
#define XA_PRIMARY  1

struct event_t {
    u32 pid;
    char comm[16];
};

BPF_PERF_OUTPUT(selection_events);

/*
 * uprobe attached to XSetSelectionOwner() in libX11.so.6.
 *
 * Function signature:
 *   int XSetSelectionOwner(Display *display, Atom selection,
 *                          Window owner, Time time);
 *
 * We only care about PRIMARY selection changes (Atom == 1).  Filtering
 * in kernel space avoids unnecessary context switches for CLIPBOARD or
 * other custom selections (e.g. SECONDARY, manager atoms).
 */
int trace_xset_selection(struct pt_regs *ctx)
{
    /* 2nd argument: Atom selection.
     * PT_REGS_PARM2 abstracts away register differences across
     * architectures (x86_64: %rsi, aarch64: x1, etc.). */
    unsigned long selection = PT_REGS_PARM2(ctx);

    if (selection != XA_PRIMARY)
        return 0;

    struct event_t event = {};
    event.pid = bpf_get_current_pid_tgid() >> 32;
    bpf_get_current_comm(&event.comm, sizeof(event.comm));
    selection_events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def check_root():
    """eBPF uprobes require CAP_SYS_ADMIN (root) on most kernels."""
    if os.geteuid() != 0:
        print(
            "Error: jucopy requires root privileges to load eBPF programs.\n"
            "Run with:  sudo python3 jucopy.py",
            file=sys.stderr,
        )
        sys.exit(1)


def check_display(display_env):
    """Warn early if no DISPLAY is reachable (common under bare sudo)."""
    if not display_env:
        print(
            "Warning: DISPLAY is not set.  Under sudo the desktop session\n"
            "environment is often lost.  Pass --display explicitly or use:\n"
            "  sudo -E python3 jucopy.py\n"
            "  sudo DISPLAY=:0 python3 jucopy.py",
            file=sys.stderr,
        )


def find_libx11():
    """
    Return the absolute path of libX11.so.6 on this system.

    Resolution order:
      1. ctypes.util.find_library (delegates to ldconfig / ld.so.conf)
      2. Hardcoded glob patterns (multiarch: x86_64, aarch64, etc.)
      3. Explicit ldconfig -p parsing as last resort
    """
    # --- 1. Standard library lookup (most portable) ---
    name = ctypes.util.find_library("X11")
    if name:
        # find_library may return a bare soname like "libX11.so.6"; resolve
        # to an absolute path via the dynamic linker search.
        if os.path.isabs(name) and os.path.isfile(name):
            return name
        # Try to resolve soname → absolute path via ldconfig
        try:
            result = subprocess.run(
                ["ldconfig", "-p"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if name in line and "=>" in line:
                    path = line.split("=>")[1].strip()
                    if os.path.isfile(path):
                        return path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # --- 2. Hardcoded multiarch glob patterns ---
    candidates = (
        glob.glob("/usr/lib/x86_64-linux-gnu/libX11.so.6")
        + glob.glob("/usr/lib/aarch64-linux-gnu/libX11.so.6")
        + glob.glob("/usr/lib/*/libX11.so.6")
        + glob.glob("/lib/*/libX11.so.6")
    )
    for path in candidates:
        if os.path.isfile(path):
            return path

    # --- 3. Explicit ldconfig fallback ---
    try:
        result = subprocess.run(
            ["ldconfig", "-p"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if "libX11.so.6" in line and "=>" in line:
                path = line.split("=>")[1].strip()
                if os.path.isfile(path):
                    return path
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return None


def sync_selection(display_env, verbose):
    """
    Copy the current PRIMARY selection into CLIPBOARD.

    Tries (in order): xclip, xsel, wl-clipboard.
    Silently ignores errors so a missed sync does not crash the daemon.
    """
    env = os.environ.copy()
    if display_env:
        env["DISPLAY"] = display_env

    # --- xclip ---
    try:
        primary = subprocess.run(
            ["xclip", "-o", "-selection", "primary"],
            capture_output=True,
            timeout=2,
            env=env,
        )
        if primary.returncode == 0 and primary.stdout.strip():
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=primary.stdout,
                capture_output=True,
                timeout=2,
                env=env,
            )
            if verbose:
                text = primary.stdout.decode("utf-8", errors="replace").strip()[:60]
                print(f"  [xclip] synced: {text!r}")
            return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # --- xsel ---
    try:
        primary = subprocess.run(
            ["xsel", "--primary", "--output"],
            capture_output=True,
            timeout=2,
            env=env,
        )
        if primary.returncode == 0 and primary.stdout.strip():
            subprocess.run(
                ["xsel", "--clipboard", "--input"],
                input=primary.stdout,
                capture_output=True,
                timeout=2,
                env=env,
            )
            if verbose:
                text = primary.stdout.decode("utf-8", errors="replace").strip()[:60]
                print(f"  [xsel] synced: {text!r}")
            return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # --- wl-clipboard (Wayland PRIMARY selection) ---
    wayland_display = os.environ.get("WAYLAND_DISPLAY") or env.get("WAYLAND_DISPLAY")
    if wayland_display:
        try:
            primary = subprocess.run(
                ["wl-paste", "--primary", "--no-newline"],
                capture_output=True,
                timeout=2,
                env=env,
            )
            if primary.returncode == 0 and primary.stdout.strip():
                subprocess.run(
                    ["wl-copy"],
                    input=primary.stdout,
                    capture_output=True,
                    timeout=2,
                    env=env,
                )
                if verbose:
                    text = primary.stdout.decode("utf-8", errors="replace").strip()[:60]
                    print(f"  [wl-clipboard] synced: {text!r}")
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    if verbose:
        print("  Warning: no clipboard tool found (install xclip, xsel, or wl-clipboard)")


# ---------------------------------------------------------------------------
# Async sync worker (producer/consumer pattern)
# ---------------------------------------------------------------------------

def _sync_worker(sync_q, display_env, verbose):
    """
    Consumer thread: drains the queue and performs the actual clipboard sync.

    Debouncing is done here on the consumer side so that rapid-fire events
    are coalesced into a single subprocess invocation.
    """
    while True:
        # Block until at least one event is available.
        sync_q.get()

        # Drain any queued duplicates before doing work.
        while not sync_q.empty():
            try:
                sync_q.get_nowait()
            except queue.Empty:
                break

        sync_selection(display_env, verbose)

        # Post-sync debounce: sleep briefly so the next burst is coalesced.
        time.sleep(DEBOUNCE_S)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(libx11_path, display_env, verbose):
    try:
        from bcc import BPF  # noqa: import inside function for cleaner error msg
    except ImportError:
        print(
            "Error: BCC Python bindings not found.\n"
            "Install with:  sudo apt install python3-bpfcc",
            file=sys.stderr,
        )
        sys.exit(1)

    b = BPF(text=BPF_TEXT)
    b.attach_uprobe(
        name=libx11_path,
        sym="XSetSelectionOwner",
        fn_name="trace_xset_selection",
    )

    if verbose:
        print(f"Attached eBPF uprobe to XSetSelectionOwner in {libx11_path}")

    # --- Start async sync worker ---
    # maxsize=1: we only need a "sync needed" signal; if the queue is full
    # the previous signal hasn't been consumed yet, so we skip (coalesce).
    sync_q = queue.Queue(maxsize=1)
    worker = threading.Thread(
        target=_sync_worker,
        args=(sync_q, display_env, verbose),
        daemon=True,
        name="jucopy-sync-worker",
    )
    worker.start()

    def handle_event(cpu, data, size):
        """Perf-buffer callback: enqueue a sync signal (non-blocking)."""
        event = b["selection_events"].event(data)
        if verbose:
            comm = event.comm.decode("utf-8", errors="replace")
            print(f"Selection event: pid={event.pid} comm={comm!r}")
        try:
            sync_q.put_nowait(True)
        except queue.Full:
            # Previous sync still pending — this event is coalesced.
            pass

    b["selection_events"].open_perf_buffer(handle_event)

    print("jucopy is running – selected text is automatically copied to clipboard.")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            b.perf_buffer_poll(timeout=POLL_TIMEOUT_MS)
    finally:
        # Explicit cleanup: detach uprobe and free eBPF resources.
        if verbose:
            print("Detaching eBPF uprobe...")
        b.cleanup()


def main():
    parser = argparse.ArgumentParser(
        description="Automatically copy selected text to clipboard using eBPF"
    )
    parser.add_argument(
        "--display",
        default=os.environ.get("DISPLAY", ":0"),
        metavar="DISPLAY",
        help="X11 display to use (default: $DISPLAY or :0)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print each sync event to stdout",
    )
    args = parser.parse_args()

    check_root()
    check_display(args.display)

    libx11 = find_libx11()
    if not libx11:
        print(
            "Error: libX11.so.6 not found.\n"
            "Install with:  sudo apt install libx11-6",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.verbose:
        print(f"Found libX11 at: {libx11}")

    try:
        run(libx11, args.display, args.verbose)
    except KeyboardInterrupt:
        print("\nStopping jucopy.")


if __name__ == "__main__":
    main()
