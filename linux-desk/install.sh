#!/usr/bin/env bash
# install.sh – Install jucopy and its runtime dependencies on Ubuntu 24.04
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Please run as root:  sudo bash install.sh" >&2
    exit 1
fi

echo "==> Installing runtime dependencies..."
apt-get install -y \
    python3-bpfcc \
    libx11-6 \
    xclip

# wl-clipboard is optional (Wayland native apps)
apt-get install -y wl-clipboard 2>/dev/null || true

INSTALL_DIR="/usr/local/bin"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing jucopy to ${INSTALL_DIR}/jucopy..."
install -m 0755 "${SCRIPT_DIR}/jucopy.py" "${INSTALL_DIR}/jucopy"

# Install systemd service if systemd is available
if command -v systemctl &>/dev/null; then
    SERVICE_DIR="/etc/systemd/system"
    echo "==> Installing systemd service..."
    install -m 0644 "${SCRIPT_DIR}/jucopy.service" "${SERVICE_DIR}/jucopy.service"
    systemctl daemon-reload
    echo "    To enable at boot:  sudo systemctl enable --now jucopy"
fi

echo ""
echo "Installation complete."
echo "Run now:   sudo jucopy"
echo "Verbose:   sudo jucopy --verbose"
