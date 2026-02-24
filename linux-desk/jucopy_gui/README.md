# JuCopy GUI

A Flutter-based GUI for managing the JuCopy eBPF service on Ubuntu.

## Features

- Monitor service status (Active/Inactive/Failed)
- Start/Stop service with a toggle
- Enable/Disable autostart (systemd)
- Ubuntu Yaru theme integration

## Build Requirements

- Flutter SDK
- Build dependencies:
  ```bash
  sudo apt install libgtk-3-dev libblkid-dev liblzma-dev
  ```

## How to Build

```bash
flutter build linux --release
```

The output will be in `build/linux/x64/release/bundle/`.

## Installation

Use the top-level `install.sh` script:

```bash
sudo bash install.sh
```
