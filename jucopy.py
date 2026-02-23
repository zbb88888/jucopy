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
import glob
import os
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Ignore duplicate selection events within this window (seconds).
DEBOUNCE_S = 0.05

# Timeout for the perf-buffer poll loop (milliseconds).
POLL_TIMEOUT_MS = 100

# ---------------------------------------------------------------------------
# eBPF program (compiled at runtime by BCC)
# ---------------------------------------------------------------------------
BPF_TEXT = r"""
#include <uapi/linux/ptrace.h>

struct event_t {
    u32 pid;
    char comm[16];
};

BPF_PERF_OUTPUT(selection_events);

/*
 * uprobe attached to XSetSelectionOwner() in libX11.so.6.
 * Called by X11/XWayland clients whenever they claim ownership of a
 * selection (PRIMARY when the user finishes highlighting text, CLIPBOARD
 * when the user presses Ctrl+C, etc.).
 */
int trace_xset_selection(struct pt_regs *ctx)
{
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


def find_libx11():
    """Return the absolute path of libX11.so.6 on this system."""
    # Common locations across x86-64, arm64, and multiarch layouts
    candidates = (
        glob.glob("/usr/lib/x86_64-linux-gnu/libX11.so.6")
        + glob.glob("/usr/lib/aarch64-linux-gnu/libX11.so.6")
        + glob.glob("/usr/lib/*/libX11.so.6")
        + glob.glob("/lib/*/libX11.so.6")
    )
    for path in candidates:
        if os.path.isfile(path):
            return path

    # Fallback: ask ldconfig
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

    last_sync = 0.0

    def handle_event(cpu, data, size):
        nonlocal last_sync
        event = b["selection_events"].event(data)
        now = time.monotonic()
        if now - last_sync < DEBOUNCE_S:
            return
        last_sync = now

        if verbose:
            comm = event.comm.decode("utf-8", errors="replace")
            print(f"Selection event: pid={event.pid} comm={comm!r}")

        sync_selection(display_env, verbose)

    b["selection_events"].open_perf_buffer(handle_event)

    print("jucopy is running – selected text is automatically copied to clipboard.")
    print("Press Ctrl+C to stop.\n")

    while True:
        b.perf_buffer_poll(timeout=POLL_TIMEOUT_MS)


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
