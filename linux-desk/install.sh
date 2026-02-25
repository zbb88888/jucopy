#!/usr/bin/env bash
# install.sh – Install jucopy on Ubuntu 24.04
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Error: please run as root (sudo bash install.sh)" >&2
    exit 1
fi

echo "==> Installing runtime dependencies..."
apt-get update
apt-get install -y python3-bpfcc libx11-6 xclip wl-clipboard 2>/dev/null || \
apt-get install -y python3-bpfcc libx11-6 xclip

INSTALL_DIR="/usr/local/bin"
OPT_DIR="/opt/jucopy"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing jucopy service to ${INSTALL_DIR}/jucopy..."
install -m 0755 "${SCRIPT_DIR}/jucopy.py" "${INSTALL_DIR}/jucopy"

if command -v systemctl &>/dev/null; then
    echo "==> Installing systemd service..."
    install -m 0644 "${SCRIPT_DIR}/jucopy.service" "/etc/systemd/system/jucopy.service"
    systemctl daemon-reload
    echo "    To start: sudo systemctl enable --now jucopy"
fi

if command -v flutter &>/dev/null; then
    echo "==> Building and installing GUI..."
    # Install build deps for Flutter
    apt-get install -y libgtk-3-dev libblkid-dev liblzma-dev

    (
        cd "${SCRIPT_DIR}/jucopy_gui"
        flutter build linux --release
    )

    mkdir -p "${OPT_DIR}"
    cp -r "${SCRIPT_DIR}/jucopy_gui/build/linux/x64/release/bundle/." "${OPT_DIR}/"
    ln -sf "${OPT_DIR}/jucopy_gui" "${INSTALL_DIR}/jucopy-gui"

    install -m 0644 "${SCRIPT_DIR}/jucopy.desktop" "/usr/share/applications/jucopy.desktop"
    sed -i 's/^Icon=.*/Icon=edit-copy/' "/usr/share/applications/jucopy.desktop"
fi

echo "==> Done."
