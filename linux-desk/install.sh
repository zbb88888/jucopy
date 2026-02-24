#!/usr/bin/env bash
# install.sh – Install jucopy and its runtime dependencies on Ubuntu 24.04
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Please run as root:  sudo bash install.sh" >&2
    exit 1
fi

echo "==> Installing runtime dependencies..."
apt-get update
apt-get install -y \
    python3-bpfcc \
    libx11-6 \
    xclip \
    libgtk-3-dev \
    libblkid-dev \
    liblzma-dev

# wl-clipboard is optional (Wayland native apps)
apt-get install -y wl-clipboard 2>/dev/null || true

INSTALL_DIR="/usr/local/bin"
OPT_DIR="/opt/jucopy"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing jucopy (eBPF service) to ${INSTALL_DIR}/jucopy..."
install -m 0755 "${SCRIPT_DIR}/jucopy.py" "${INSTALL_DIR}/jucopy"

# Install systemd service if systemd is available
if command -v systemctl &>/dev/null; then
    SERVICE_DIR="/etc/systemd/system"
    echo "==> Installing systemd service..."
    install -m 0644 "${SCRIPT_DIR}/jucopy.service" "${SERVICE_DIR}/jucopy.service"
    systemctl daemon-reload
    echo "    To enable at boot:  sudo systemctl enable --now jucopy"
fi

# Build and Install GUI
if command -v flutter &>/dev/null; then
    echo "==> Building JuCopy GUI (Flutter)..."
    (
        cd "${SCRIPT_DIR}/jucopy_gui"
        flutter build linux --release
    )

    echo "==> Installing JuCopy GUI to ${OPT_DIR}..."
    mkdir -p "${OPT_DIR}"
    cp -r "${SCRIPT_DIR}/jucopy_gui/build/linux/x64/release/bundle/." "${OPT_DIR}/"

    echo "==> Creating symlink to ${INSTALL_DIR}/jucopy-gui..."
    ln -sf "${OPT_DIR}/jucopy_gui" "${INSTALL_DIR}/jucopy-gui"

    echo "==> Installing Desktop entry..."
    DESKTOP_DIR="/usr/share/applications"
    # Update Exec path in .desktop if necessary, but jucopy-gui is in PATH now
    install -m 0644 "${SCRIPT_DIR}/jucopy.desktop" "${DESKTOP_DIR}/jucopy.desktop"

    # Optional: Use a system icon if we don't have one
    sed -i 's/^Icon=.*/Icon=edit-copy/' "${DESKTOP_DIR}/jucopy.desktop"
else
    echo "Warning: flutter not found, skipping GUI build."
fi

echo ""
echo "Installation complete."
echo "Run service:  sudo systemctl start jucopy"
echo "Run GUI:      jucopy-gui"
