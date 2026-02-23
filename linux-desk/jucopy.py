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
  eBPF uprobe (kernel)                 User-space
  ┌───────────────────────┐    ┌─────────────────────────────┐
  │ XSetSelectionOwner    │    │  ring-buffer callback       │
  │   ↓                   │    │    ↓ (non-blocking put)     │
  │ filter XA_PRIMARY     │───>│  Queue ──> sync_worker thr  │
  │ filter sync-tool comm │    │              ↓               │
  │ ringbuf_submit()      │    │     xclip/xsel/wl-copy      │
  └───────────────────────┘    └─────────────────────────────┘

  The comm filter in eBPF prevents the feedback loop where xclip/xsel's own
  XSetSelectionOwner(PRIMARY) call would re-trigger a sync, which could cause
  a runaway CPU spike with non-conforming clipboard managers.

Requirements
------------
  - Linux kernel >= 5.8 with eBPF ring-buffer support
    (falls back to perf-buffer on older kernels automatically)
  - python3-bpfcc  (BCC Python bindings)
  - libx11-6       (libX11.so.6 must be present)
  - xclip OR xsel  (for X11 / XWayland)
  - wl-clipboard   (for Wayland-native apps, optional)

Usage
-----
  sudo python3 jucopy.py [--display :0] [--verbose]
"""

import argparse
import contextlib
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

# Debounce window on the consumer side (seconds).
# The sync worker sleeps this long after each sync to coalesce rapid events.
DEBOUNCE_S = 0.05

# Timeout for the ring-buffer / perf-buffer poll loop (milliseconds).
POLL_TIMEOUT_MS = 100

# Ring-buffer size in pages (must be a power of 2, requires kernel >= 5.8).
RINGBUF_PAGES = 8

# Minimum kernel version that supports BPF_RINGBUF (5.8.0).
RINGBUF_MIN_KERNEL = (5, 8, 0)

# ---------------------------------------------------------------------------
# eBPF programs (compiled at runtime by BCC)
# ---------------------------------------------------------------------------

# Modern ring-buffer variant (kernel >= 5.8).
# Advantages over perf-buffer:
#   • Single shared memory pool → no per-CPU fragmentation
#   • Global event ordering guaranteed
#   • Lower overhead: no poll needed, wakeup-on-submit possible
#
# XA_PRIMARY_ATOM is replaced at runtime with the dynamically resolved value.
BPF_TEXT_RINGBUF = r"""
#include <uapi/linux/ptrace.h>

#define XA_PRIMARY_ATOM  PRIMARY_ATOM_PLACEHOLDER

struct event_t {
    u32 pid;
    char comm[16];
};

BPF_RINGBUF_OUTPUT(selection_events, RINGBUF_PAGES_PLACEHOLDER);

/*
 * Return 1 if the current task's comm matches a known clipboard sync tool.
 *
 * Filtering in kernel space prevents the feedback loop where xclip / xsel's
 * own XSetSelectionOwner(PRIMARY) call re-triggers a sync.  Without this,
 * a non-conforming clipboard manager that writes PRIMARY on every CLIPBOARD
 * update can cause a busy-loop that pegs CPU.
 *
 * We use explicit byte comparisons rather than bpf_strncmp() because the
 * latter is only available on kernel >= 5.17, and we want this to compile
 * all the way back to the perf-buffer fallback target (< 5.8).
 */
static __always_inline int is_sync_tool(char comm[16])
{
    /* xclip */
    if (comm[0]=='x' && comm[1]=='c' && comm[2]=='l' &&
        comm[3]=='i' && comm[4]=='p' && comm[5]=='\0')
        return 1;
    /* xsel */
    if (comm[0]=='x' && comm[1]=='s' && comm[2]=='e' &&
        comm[3]=='l' && comm[4]=='\0')
        return 1;
    /* wl-copy */
    if (comm[0]=='w' && comm[1]=='l' && comm[2]=='-' &&
        comm[3]=='c' && comm[4]=='o' && comm[5]=='p' &&
        comm[6]=='y' && comm[7]=='\0')
        return 1;
    /* wl-paste */
    if (comm[0]=='w' && comm[1]=='l' && comm[2]=='-' &&
        comm[3]=='p' && comm[4]=='a' && comm[5]=='s' &&
        comm[6]=='t' && comm[7]=='e' && comm[8]=='\0')
        return 1;
    return 0;
}

/*
 * uprobe on XSetSelectionOwner() – ring-buffer variant.
 *
 * Function signature:
 *   int XSetSelectionOwner(Display *display, Atom selection,
 *                          Window owner, Time time);
 */
int trace_xset_selection(struct pt_regs *ctx)
{
    unsigned long selection = PT_REGS_PARM2(ctx);

    if (selection != XA_PRIMARY_ATOM)
        return 0;

    /* Reject events from our own sync tools to prevent feedback loops. */
    char comm[16] = {};
    bpf_get_current_comm(&comm, sizeof(comm));
    if (is_sync_tool(comm))
        return 0;

    struct event_t *event = selection_events.ringbuf_reserve(sizeof(struct event_t));
    if (!event)
        return 0;

    event->pid = bpf_get_current_pid_tgid() >> 32;
    __builtin_memcpy(event->comm, comm, sizeof(event->comm));

    selection_events.ringbuf_submit(event, 0);
    return 0;
}
"""

# Legacy perf-buffer variant (kernel < 5.8 fallback).
# Shares the same is_sync_tool() filter logic.
BPF_TEXT_PERFBUF = r"""
#include <uapi/linux/ptrace.h>

#define XA_PRIMARY_ATOM  PRIMARY_ATOM_PLACEHOLDER

struct event_t {
    u32 pid;
    char comm[16];
};

BPF_PERF_OUTPUT(selection_events);

static __always_inline int is_sync_tool(char comm[16])
{
    if (comm[0]=='x' && comm[1]=='c' && comm[2]=='l' &&
        comm[3]=='i' && comm[4]=='p' && comm[5]=='\0')
        return 1;
    if (comm[0]=='x' && comm[1]=='s' && comm[2]=='e' &&
        comm[3]=='l' && comm[4]=='\0')
        return 1;
    if (comm[0]=='w' && comm[1]=='l' && comm[2]=='-' &&
        comm[3]=='c' && comm[4]=='o' && comm[5]=='p' &&
        comm[6]=='y' && comm[7]=='\0')
        return 1;
    if (comm[0]=='w' && comm[1]=='l' && comm[2]=='-' &&
        comm[3]=='p' && comm[4]=='a' && comm[5]=='s' &&
        comm[6]=='t' && comm[7]=='e' && comm[8]=='\0')
        return 1;
    return 0;
}

int trace_xset_selection(struct pt_regs *ctx)
{
    unsigned long selection = PT_REGS_PARM2(ctx);

    if (selection != XA_PRIMARY_ATOM)
        return 0;

    char comm[16] = {};
    bpf_get_current_comm(&comm, sizeof(comm));
    if (is_sync_tool(comm))
        return 0;

    struct event_t event = {};
    event.pid = bpf_get_current_pid_tgid() >> 32;
    __builtin_memcpy(event.comm, comm, sizeof(event.comm));
    selection_events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def check_root() -> None:
    """eBPF uprobes require CAP_SYS_ADMIN (root) on most kernels."""
    if os.geteuid() != 0:
        print(
            "Error: jucopy requires root privileges to load eBPF programs.\n"
            "Run with:  sudo python3 jucopy.py",
            file=sys.stderr,
        )
        sys.exit(1)


def check_display(display_env: Optional[str]) -> None:
    """Warn early if no DISPLAY is reachable (common under bare sudo)."""
    if not display_env:
        print(
            "Warning: DISPLAY is not set.  Under sudo the desktop session\n"
            "environment is often lost.  Pass --display explicitly or use:\n"
            "  sudo -E python3 jucopy.py\n"
            "  sudo DISPLAY=:0 python3 jucopy.py",
            file=sys.stderr,
        )


def check_x11_environment() -> None:
    """
    Exit early with a clear message when no X11 runtime is present.

    This protects headless nodes (e.g. servers without a GPU/display)
    from a confusing BCC/libX11 traceback.
    """
    if not find_libx11():
        print(
            "Error: libX11.so.6 not found.  This host appears to be headless\n"
            "or X11 libraries are not installed.  jucopy requires an X11\n"
            "desktop environment to function.\n"
            "Install with:  sudo apt install libx11-6",
            file=sys.stderr,
        )
        sys.exit(1)


def setup_xauth() -> None:
    """
    Automatically fix missing X11 authorisation when running under sudo.

    Under plain ``sudo``, the XAUTHORITY variable is typically lost, causing
    X11 clients to fail with ``No protocol specified``.  This function probes
    common locations for the real user's Xauthority file and sets the
    environment variable before any X11 subprocess is spawned.

    Resolution order:
      1. XAUTHORITY already set → nothing to do.
      2. $HOME/.Xauthority of the real (SUDO_USER) user.
      3. First matching xauth_* file under /run/user/<uid>/ (used by
         gdm3, lightdm and other modern display managers).
    """
    if os.environ.get("XAUTHORITY"):
        return  # already set, nothing to do

    try:
        real_user = os.environ.get("SUDO_USER") or os.getlogin()
    except OSError:
        real_user = None

    if not real_user or real_user == "root":
        return

    # --- 1. Classic ~/.Xauthority ---
    user_home = os.path.expanduser(f"~{real_user}")
    xauth_home = os.path.join(user_home, ".Xauthority")
    if os.path.exists(xauth_home):
        os.environ["XAUTHORITY"] = xauth_home
        return

    # --- 2. Modern display-manager path: /run/user/<uid>/xauth_* ---
    try:
        import pwd
        uid = pwd.getpwnam(real_user).pw_uid
        xauth_run_dir = f"/run/user/{uid}"
        matches = glob.glob(os.path.join(xauth_run_dir, "xauth_*"))
        if matches:
            os.environ["XAUTHORITY"] = matches[0]
    except (KeyError, ImportError):
        pass


def get_kernel_version() -> tuple[int, ...]:
    """Return the running kernel version as a (major, minor, patch) tuple."""
    try:
        raw = os.uname().release          # e.g. "6.8.0-51-generic"
        parts = raw.split("-")[0].split(".")
        return tuple(int(p) for p in parts[:3])
    except (ValueError, IndexError):
        return (0, 0, 0)


def resolve_primary_atom(display_env: Optional[str]) -> int:
    """
    Dynamically resolve the Atom ID for the ``PRIMARY`` string via libX11.

    XA_PRIMARY is guaranteed to be 1 in the X11 protocol, but we resolve it
    dynamically anyway so the value is always trustworthy even on non-standard
    or embedded X implementations.  Falls back to the protocol constant (1) if
    the library cannot be loaded.

    Returns an integer atom value.
    """
    FALLBACK = 1  # XA_PRIMARY from <X11/Xatom.h>

    libx11_path = find_libx11()
    if not libx11_path:
        return FALLBACK

    env_display = display_env or os.environ.get("DISPLAY", ":0")

    try:
        libx11 = ctypes.cdll.LoadLibrary(libx11_path)

        # XOpenDisplay(display_name: char*) -> Display*
        libx11.XOpenDisplay.restype = ctypes.c_void_p
        libx11.XOpenDisplay.argtypes = [ctypes.c_char_p]

        # XInternAtom(display, name, only_if_exists) -> Atom (unsigned long)
        libx11.XInternAtom.restype = ctypes.c_ulong
        libx11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]

        # XCloseDisplay(display) -> int
        libx11.XCloseDisplay.restype = ctypes.c_int
        libx11.XCloseDisplay.argtypes = [ctypes.c_void_p]

        dpy = libx11.XOpenDisplay(env_display.encode())
        if not dpy:
            return FALLBACK

        try:
            atom = libx11.XInternAtom(dpy, b"PRIMARY", 0)
            return int(atom) if atom else FALLBACK
        finally:
            libx11.XCloseDisplay(dpy)

    except OSError:
        return FALLBACK


def find_libx11_from_proc_maps(target_pid: Optional[int] = None) -> Optional[str]:
    """
    Discover libX11.so.6 by reading ``/proc/<pid>/maps`` of running processes.

    Unlike ``ldconfig``-based discovery, this returns the *actual* path that
    the dynamic linker resolved at load time for a live process, making it
    robust to:

    * Non-standard installation prefixes (``/opt``, ``/home/user/.local``, …)
    * Flatpak / Snap container mounts
    * Custom ``LD_LIBRARY_PATH`` overrides
    * Stale or absent ``ldconfig`` cache

    Parameters
    ----------
    target_pid:
        If given, only ``/proc/<target_pid>/maps`` is inspected – useful when
        the caller already knows the PID of the target X11 application.
        If ``None`` (default), all accessible ``/proc/*/maps`` are scanned
        and the first match is returned.

    Returns
    -------
    str | None
        Absolute path to ``libX11.so.6`` as mapped into a running process,
        or ``None`` if no match was found.
    """
    soname = "libX11.so.6"

    if target_pid is not None:
        maps_files: list[str] = [f"/proc/{target_pid}/maps"]
    else:
        maps_files = glob.glob("/proc/[0-9]*/maps")

    for maps_path in maps_files:
        try:
            with open(maps_path) as fh:
                for line in fh:
                    # /proc/<pid>/maps line format:
                    #   addr-addr perms offset dev inode [pathname]
                    # The optional pathname is field index 5.
                    fields = line.split()
                    if len(fields) < 6:
                        continue
                    path = fields[5]
                    # Match both the versioned soname (libX11.so.6) and any
                    # full version suffix (libX11.so.6.4.0).
                    if soname in path and os.path.isfile(path):
                        return path
        except (PermissionError, OSError):
            # Skip processes we cannot read (different user, already exited)
            continue

    return None


def find_libx11() -> Optional[str]:
    """
    Return the absolute path of libX11.so.6 on this system.

    Resolution order:
      1. ctypes.util.find_library (delegates to ldconfig / ld.so.conf)
      2. Hardcoded glob patterns (multiarch: x86_64, aarch64, etc.)
      3. /proc/*/maps scan – finds the library as loaded by live X11 processes
         (survives non-standard prefixes, Flatpak, custom LD_LIBRARY_PATH)
      4. Explicit ldconfig -p parsing as last resort
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

    # --- 3. /proc/*/maps scan (live processes, non-standard prefixes) ---
    proc_path = find_libx11_from_proc_maps()
    if proc_path:
        return proc_path

    # --- 4. Explicit ldconfig fallback ---
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


def _run(
    cmd: list[str],
    env: dict[str, str],
    stdin_data: Optional[bytes] = None,
) -> subprocess.CompletedProcess[bytes]:
    """
    Run *cmd* as an isolated subprocess and return its output as bytes.

    ``start_new_session=True`` puts the child in its own process group so a
    hung clipboard tool cannot propagate signals to the daemon or block the
    worker thread beyond the 2-second timeout.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        timeout=2,
        env=env,
        start_new_session=True,
        input=stdin_data,
    )


def sync_selection(display_env: Optional[str], verbose: bool) -> None:
    """
    Copy the current PRIMARY selection into CLIPBOARD.

    Tries (in order): xclip, xsel, wl-clipboard.  Each subprocess is
    launched via :func:`_run` so a stalled tool cannot crash the daemon.
    """
    env: dict[str, str] = os.environ.copy()  # type: ignore[assignment]
    if display_env:
        env["DISPLAY"] = display_env

    # --- xclip ---
    try:
        primary = _run(["xclip", "-o", "-selection", "primary"], env)
        if primary.returncode == 0 and primary.stdout.strip():
            _run(["xclip", "-selection", "clipboard"], env, primary.stdout)
            if verbose:
                text = primary.stdout.decode("utf-8", errors="replace").strip()[:60]
                print(f"  [xclip] synced: {text!r}")
            return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # --- xsel ---
    try:
        primary = _run(["xsel", "--primary", "--output"], env)
        if primary.returncode == 0 and primary.stdout.strip():
            _run(["xsel", "--clipboard", "--input"], env, primary.stdout)
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
            primary = _run(["wl-paste", "--primary", "--no-newline"], env)
            if primary.returncode == 0 and primary.stdout.strip():
                _run(["wl-copy"], env, primary.stdout)
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

def _sync_worker(
    sync_q: queue.Queue[bool],
    display_env: Optional[str],
    verbose: bool,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """
    Consumer thread: drains the queue and performs the actual clipboard sync.

    Debouncing is done here on the consumer side so that rapid-fire events
    are coalesced into a single subprocess invocation.

    The optional ``stop_event`` (threading.Event) allows the main loop to
    signal a clean shutdown so the worker exits promptly instead of blocking
    indefinitely on sync_q.get().
    """
    while True:
        if stop_event is not None:
            # Use a timeout so we can check stop_event periodically.
            try:
                sync_q.get(timeout=0.2)
            except queue.Empty:
                if stop_event.is_set():
                    break
                continue
        else:
            sync_q.get()

        if stop_event is not None and stop_event.is_set():
            break

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

def run(libx11_path: str, display_env: Optional[str], verbose: bool, primary_atom: int) -> None:
    """
    Attach an eBPF uprobe to XSetSelectionOwner() and start the sync loop.

    Automatically selects between the modern ring-buffer backend (kernel >=
    5.8) and the legacy perf-buffer backend, so the same binary works across
    all supported kernel versions.

    An ExitStack ensures the eBPF resources and the worker thread stop-event
    are cleaned up regardless of how the function exits.
    """
    try:
        from bcc import BPF  # type: ignore[import-untyped]  # no stubs available
    except ImportError:
        print(
            "Error: BCC Python bindings not found.\n"
            "Install with:  sudo apt install python3-bpfcc",
            file=sys.stderr,
        )
        sys.exit(1)

    kver = get_kernel_version()
    use_ringbuf = kver >= RINGBUF_MIN_KERNEL
    if verbose:
        kver_str = ".".join(str(x) for x in kver)
        backend = "ring-buffer" if use_ringbuf else "perf-buffer"
        print(f"Kernel {kver_str} detected – using {backend} backend.")
        print(f"PRIMARY atom resolved to: {primary_atom}")

    # Fill in the atom value and ring-buffer size at compile time.
    bpf_src = (BPF_TEXT_RINGBUF if use_ringbuf else BPF_TEXT_PERFBUF)
    bpf_src = bpf_src.replace("PRIMARY_ATOM_PLACEHOLDER", str(primary_atom))
    bpf_src = bpf_src.replace("RINGBUF_PAGES_PLACEHOLDER", str(RINGBUF_PAGES))

    stop_event = threading.Event()

    with contextlib.ExitStack() as stack:
        # --- Compile & load eBPF program ---
        b = BPF(text=bpf_src)
        stack.callback(b.cleanup)   # guaranteed cleanup on any exit path

        b.attach_uprobe(  # type: ignore[union-attr]
            name=libx11_path.encode(),
            sym=b"XSetSelectionOwner",
            fn_name=b"trace_xset_selection",
        )

        if verbose:
            print(f"Attached eBPF uprobe to XSetSelectionOwner in {libx11_path}")

        # --- Start async sync worker ---
        # maxsize=1: we only need a "sync needed" signal; if the queue is full
        # the previous signal hasn't been consumed yet, so we skip (coalesce).
        sync_q: queue.Queue[bool] = queue.Queue(maxsize=1)

        worker = threading.Thread(
            target=_sync_worker,
            args=(sync_q, display_env, verbose, stop_event),
            daemon=True,
            name="jucopy-sync-worker",
        )
        worker.start()
        # Signal the worker to stop when we leave the ExitStack context.
        stack.callback(stop_event.set)

        # --- Wire up perf/ring-buffer callback ---
        def handle_event(cpu: Any, data: Any, size: Any) -> None:
            """Callback: enqueue a sync signal (non-blocking)."""
            event = b["selection_events"].event(data)  # type: ignore[index]
            if verbose:
                comm: str = event.comm.decode("utf-8", errors="replace")  # type: ignore[union-attr]
                print(f"Selection event: pid={event.pid} comm={comm!r}")  # type: ignore[union-attr]
            try:
                sync_q.put_nowait(True)
            except queue.Full:
                pass  # coalesced

        if use_ringbuf:
            b["selection_events"].open_ring_buffer(handle_event)  # type: ignore[union-attr]
        else:
            b["selection_events"].open_perf_buffer(handle_event)  # type: ignore[union-attr]

        print("jucopy is running – selected text is automatically copied to clipboard.")
        print("Press Ctrl+C to stop.\n")

        try:
            while True:
                if use_ringbuf:
                    b.ring_buffer_poll(timeout=POLL_TIMEOUT_MS)
                else:
                    b.perf_buffer_poll(timeout=POLL_TIMEOUT_MS)
        except KeyboardInterrupt:
            pass
        # ExitStack __exit__ fires here: stop_event.set() then b.cleanup()

    if verbose:
        print("Detaching eBPF uprobe... done.")


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

    # --- Headless / no-X11 node guard ---
    # Exit gracefully rather than crashing deep inside BCC if no X11 is present.
    check_x11_environment()

    # --- Fix XAUTHORITY before any X11 subprocess is spawned ---
    setup_xauth()

    check_display(args.display)

    libx11 = find_libx11()
    # find_libx11 is already guaranteed non-None by check_x11_environment(),
    # but be explicit for clarity.
    if not libx11:
        print(
            "Error: libX11.so.6 not found.\n"
            "Install with:  sudo apt install libx11-6",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.verbose:
        print(f"Found libX11 at: {libx11}")

    # --- Dynamically resolve PRIMARY atom via libX11 ---
    primary_atom = resolve_primary_atom(args.display)
    if args.verbose:
        print(f"PRIMARY atom ID: {primary_atom}")

    run(libx11, args.display, args.verbose, primary_atom)
    print("\nStopping jucopy.")


if __name__ == "__main__":
    main()
