# HackRF Watchdog

HackRF Watchdog is a simple PyQt-based “RF tripwire” for the HackRF using `hackrf_sweep`.

Instead of a full-blown SDR GUI, this app focuses on:

- Sweeping one or more bands,
- Estimating the noise floor,
- Applying a threshold (relative or absolute),
- Applying a persistence/hold time,
- Listing “detections” in a table (Frequency / Power / Age),
- Logging max levels per band over time,
- Letting you pick which HackRF (by serial) to use.

It’s meant to sit in the corner and **tell you when RF activity appears**, not to be a full spectrum/waterfall viewer.

---

## Features

- **Three configurable bands (A, B, C)**  
  Each with start/stop frequency in MHz and enable checkbox.

- **Configurable bin width**  
  Controls the FFT bin width (2445–5,000,000 Hz).  
  Smaller = higher frequency resolution, slower sweeps.  
  Larger = coarser resolution, faster sweeps.

- **Local noise floor estimation**  
  Per-sweep noise floor is estimated from the lower 80% of power samples and smoothed over time.

- **Two threshold modes**  
  - *Relative*: Threshold (dB) above the estimated noise floor.  
  - *Absolute*: Threshold is an absolute dB value from `hackrf_sweep`.

- **Detection persistence (“hold time”)**  
  A signal must stay above threshold for a configurable number of seconds before it is considered a detection.  
  This prevents one-off spikes from triggering.

- **Detection table**  
  - One row per frequency that has ever met the detection rule since Start.  
  - Columns: `Frequency (MHz)`, `Power (dB)`, `Age (s)`  
  - `Age (s)` = time since this frequency was last seen above threshold.  
  - Rows are sorted by most recent (smallest age) first.

- **Text log**  
  - Logs the max power per sweep per band:  
    `Max: -61.9 dB at 902.500000 MHz (span 900.000-930.000 MHz)`  
  - Logs errors from `hackrf_sweep` (e.g. device busy, not found).

- **Device selection by serial**  
  - Dropdown lists connected HackRF boards (via `hackrf_info`) as  
    `HackRF 0 – SERIAL`  
    `HackRF 1 – SERIAL`  
  - You can run multiple instances of the app and bind each to a different board.

- **Dark mode toggle**  
  Simple dark theme for the UI.

---

## Requirements

- Python 3.10+ (tested on Windows)
- HackRF tools installed and in your PATH (`hackrf_info`, `hackrf_sweep`)
- A HackRF (or more than one, if running multiple instances)

Python dependencies (see `requirements.txt`):

- `PyQt5`

---

## Installation

```bash
# Clone the repository
git clone https://github.com/Gang1eri/HackRF-Watchdog.git
cd HackRF-Watchdog

# Create a virtual environment
python -m venv .venv

# Activate it (Windows)
.\.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

## Future work / ideas

- **Notifications for alarms**
  - Play a sound or show a popup when a new detection appears.
  - Optional per-band or per-frequency filters (only alert on certain ranges).
  - Rate limiting so you don't get spammed during noisy conditions.

- **ATAK integration**
  - Export detections as Cursor-on-Target (CoT) events.
  - Send events to a TAK server or directly to ATAK clients.
  - Possibly a lightweight companion script or plugin that subscribes to the watchdog and forwards alarms to ATAK.
