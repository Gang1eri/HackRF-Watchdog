# Install (Windows 10/11)

## 1) Install HackRF tools

Install HackRF tools so `hackrf_sweep.exe` and `hackrf_info.exe` work.

Verify in PowerShell:

```powershell
hackrf_info
hackrf_sweep -h
```

## 2) Install Python

Install Python 3.x from python.org and ensure “Add Python to PATH” is checked.

## 3) Install Watchdog

Download/clone this repo, then in PowerShell:

```powershell
cd path\to\HackRF-Watchdog
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -U pip
pip install -r requirements-windows.txt
python main.py
```

## Notes

- If your HackRF isn’t detected, use Zadig to install the correct WinUSB driver for HackRF.
