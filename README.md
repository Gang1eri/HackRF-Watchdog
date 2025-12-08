# HackRF Watchdog

A PyQt-based sweep watchdog for HackRF, using `hackrf_sweep`.

## Features

- Sweep up to three configurable bands (A, B, C)
- Per-band start/stop frequencies and bin width
- Local noise floor estimation
- Threshold above noise or absolute threshold mode
- Detection persistence / hold time
- Detection table (Frequency, Power, Age)
- Device selection by HackRF serial (supports multiple boards, one per app instance)

## Quick start

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python main.py
