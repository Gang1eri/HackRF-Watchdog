# Install (Raspberry Pi OS Bookworm, 64-bit)

These instructions assume Raspberry Pi OS (Debian 12 Bookworm) **with Desktop**.

## 1) System prerequisites

Update OS:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

Install HackRF tools and Python/Qt deps (PyQt5 via apt for best compatibility):

```bash
sudo apt install -y \
  hackrf \
  python3 python3-venv python3-pip git \
  python3-pyqt5 python3-pyqt5.qtmultimedia \
  python3-pyqtgraph
```

## 2) HackRF udev permissions (no sudo needed)

Create udev rule:

```bash
sudo nano /etc/udev/rules.d/53-hackrf.rules
```

Paste this single line into the file:

```udev
ATTR{idVendor}=="1d50", ATTR{idProduct}=="6089", MODE="660", GROUP="plugdev"
```

Apply and add your user to `plugdev`:

```bash
sudo usermod -aG plugdev $USER
sudo udevadm control --reload-rules
sudo udevadm trigger
sudo reboot
```

Verify:

```bash
hackrf_info
hackrf_sweep -h
python3 -c "from PyQt5.QtMultimedia import QSoundEffect; print('QtMultimedia OK')"
```

## 3) Install Watchdog

Clone:

```bash
cd ~
git clone https://github.com/Gang1eri/HackRF-Watchdog hackrf-watchdog
cd ~/hackrf-watchdog
```

Create venv (uses apt-installed PyQt5):

```bash
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
python -m pip install -U pip
```

Install Python deps:

```bash
pip install -r requirements-pi.txt
```

Run:

```bash
python3 main.py
```

## 4) Desktop shortcut (optional)

This repo includes Linux launcher assets under `packaging/linux/`.

- `packaging/linux/run_watchdog.sh`
- `packaging/linux/HackRF-Watchdog.desktop`

**Note:** edit `Exec=` and `Path=` in the `.desktop` file to match your username and install folder.
