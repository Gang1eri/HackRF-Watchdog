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
  - Logs the max power per sweep per band, e.g.:  
    `Max: -61.9 dB at 902.500000 MHz (span 900.000-930.000 MHz)`  
  - Logs errors from `hackrf_sweep` (e.g. device busy, not found).

- **Device selection by serial**  
  - Dropdown lists connected HackRF boards (via `hackrf_info`) as:  
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
.\.venv\Scriptsctivate

# Install dependencies
pip install -r requirements.txt
```

---

## Running

Inside the repo (with the virtualenv active):

```bash
python main.py
```

To run **multiple instances** (e.g. one per HackRF):

- Open two terminals,
- Activate `.venv` in each,
- Run `python main.py` in each terminal.

In each window, use the **Device → HackRF** dropdown to pick a different board by serial.

---

## Usage overview

### Device selection

1. Plug in your HackRF(s).
2. In the app, go to the **Device** group.
3. Click **Refresh**.
4. Choose:
   - `Default (first HackRF)` – uses the first device found by `hackrf_sweep`, or
   - A specific device: `HackRF 0 – SERIAL`, `HackRF 1 – SERIAL`, etc.

If you run multiple instances, **bind each instance to a different serial** to avoid “device busy” or “HackRF not found” errors.

---

### Detection settings

- **Use local noise floor**  
  - On: Threshold is _dB above_ the estimated noise floor.  
    - Threshold range: `0.0` to `50.0`.  
    - Effective threshold = `noise_floor + threshold`.
  - Off: Threshold is an absolute level in dB.  
    - Threshold range: `-150.0` to `50.0`.

- **Persistence / hold time (s)**  
  - Minimum time a frequency must stay above threshold before becoming a detection.  
  - `0.0` → every above-threshold blip counts immediately.  
  - Larger values filter brief bursts and only admit longer-lasting signals.

- **Interval (ms)**  
  - Extra delay between sweep cycles.  
  - `0` → run sweeps back-to-back as fast as possible.

---

### Band configuration

For each band (A, B, C):

- Enable/disable,
- Set start/stop frequency (MHz),
- Use presets (VHF+UHF Ham, 915 MHz ISM, 2.4 GHz ISM, 5.8 GHz ISM), and apply to:
  - All bands, or
  - A specific band.

---

## Usage examples

### Example 1: Watch 2 m and 70 cm ham bands

Goal: Trip on activity in the 144–148 MHz and 420–450 MHz ham bands.

1. **Bands**
   - Band A: 144.0–148.0 MHz (enabled)
   - Band B: 420.0–450.0 MHz (enabled)
   - Band C: disabled
   - Bin width: `250000` Hz

2. **Detection settings**
   - Use local noise floor: ✅
   - Threshold (dB): `3.0` (≈ 3 dB above noise)
   - Persistence / hold time (s): `1.0`
   - Interval (ms): `0`

3. Hit **Start** and monitor:
   - **Log**: max levels and where they occur.
   - **Detection table**: frequencies that have crossed threshold and stayed there for ≥ 1 s.

This is good for things like seeing repeaters or long transmissions.

---

### Example 2: Detect a short-burst ISM transmitter (e.g. RNode at ~915.2 MHz)

Goal: Detect a relatively short transmission around 915 MHz with a lot of noise in the band.

1. **Bands**
   - Band A: 914.0–916.0 MHz (enabled)  
     (narrow span to exclude very noisy parts of the 900 MHz band)
   - Band B & C: disabled
   - Bin width: `250000` Hz

2. **Detection settings – debug mode (lenient)**
   - Use local noise floor: ✅
   - Threshold (dB): `1.0`
   - Persistence / hold time (s): `0.0`  
     (count every above-threshold hit, no hold-time requirement)
   - Interval (ms): `0`

3. Hit **Start** and trigger several short transmissions from the RNode.

4. Watch for:
   - **Log**: lines like  
     `Max: -27.0 dB at 915.071429 MHz (span 914.000-916.000 MHz)`
   - **Detection table**: rows around 915 MHz, such as  
     `915.071429`, `915.309524`, `915.547619` MHz, etc.

Because `hackrf_sweep` reports data in bins, you will not see exactly `915.200000 MHz`. Instead, you’ll see the **nearest bin centers** light up, e.g. 915.07 / 915.31 MHz.

Once you confirm the signal is being detected, you can tighten settings:

- Raise Threshold to `3–5 dB` if you get too many noise hits,
- Increase hold time to `0.5–1.0 s` if you only care about longer transmissions.

---

## Experimental spectrum/waterfall

There has been some experimentation with adding a spectrum+waterfall view similar to SDR tools. That code is not part of `main.py` yet, but may appear in future as a separate experimental module or in community forks.

If you’d like to contribute a robust spectrum/waterfall implementation, PRs and forks are welcome.

---

## Future work / ideas

- **Notifications for alarms**
  - Play a sound or show a popup when a new detection appears.
  - Optional per-band or per-frequency filters (only alert on certain ranges).
  - Rate limiting so you don't get spammed during noisy conditions.

- **ATAK integration**
  - Export detections as Cursor-on-Target (CoT) events.
  - Send events to a TAK server or directly to ATAK clients.
  - Possibly a lightweight companion script or plugin that subscribes to the watchdog and forwards alarms to ATAK.

---

## License

TBD. 
