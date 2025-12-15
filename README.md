# HackRF Watchdog

HackRF Watchdog is a simple PyQt-based “RF tripwire” for the HackRF using `hackrf_sweep`.

Instead of a full-blown SDR GUI, this app focuses on:

- Sweeping one or more bands
- Estimating the noise floor
- Applying a threshold (relative or absolute)
- Applying a persistence/hold time
- Listing detections in a table (Frequency / Power / Age)
- Logging max levels per band over time
- Letting you pick which HackRF (by serial) to use

It’s meant to sit in the corner and **tell you when RF activity appears**, not to be a full spectrum/waterfall viewer.

---

## Features

### Core RF “Tripwire”
- Sweeps one or more bands using `hackrf_sweep`
- Local noise floor estimation (optional)
- Two threshold modes:
  - **Relative:** threshold is dB **above** noise floor (0–120 dB range)
  - **Absolute:** threshold is an absolute dB level (-150 to +50 dB range)
- **Effective threshold readout** showing the true absolute threshold in dB (noise + offset, or absolute)
- **Detection persistence / hold time (s)**: signal must stay above threshold for N seconds before triggering
- Detection table: Frequency / Power / Age
- Text log of max levels per sweep + errors

### Bands, presets, and resolution
- **Three configurable bands (A, B, C)** with enable toggles + start/stop MHz
- **Automatic bin width selection (default)**
  - “Auto” checkbox + “Max bins” target (e.g., 400) to avoid accidental “too many bins” settings
  - Advanced users can disable Auto and set bin width manually
- Improved presets:
  - **VHF Ham** (144–148 MHz)
  - **UHF Ham + GMRS/FRS** (420–470 MHz plus 462/467 MHz area)

### Device and calibration controls
- HackRF device selection by serial (via `hackrf_info`) so multiple instances can run (one per HackRF)
- **Bias-T / antenna power toggle** (best effort via `hackrf_biast`, and `-p 1` for sweeps)
- **Power calibration**
  - Antenna/LNA gain (dB) and feedline loss (dB)
  - Shows net offset (gain − loss) and applies it before thresholding
- **Frequency correction (ppm)** for aligning displayed/detected frequency

### UI / quality-of-life
- Dark mode toggle
- Configurable alarm sounds (“System beep”, “Soft ding”, “Short chirp”, “Alarm”) + “Beep on detection” toggle
- Debug / status panel (Idle/RUNNING, bin width, interval, bias-T state, and band status)
- Bottom layout uses a **resizable vertical splitter** so alarms/log/debug can be resized as needed

### ATAK / CoT Bridge (NEW)
- Separate ATAK Bridge window that launches with Watchdog
- Bridge enable/disable toggle
- Configurable destination host + port (multicast or unicast)
- “Send test” button + marker preview
- User group name/color + role fields
- Settings persist across restarts
- Bind local IP option (helpful on Windows when multicast routing picks the wrong adapter)

---

## Requirements (Windows)

Install these **before** running HackRF Watchdog.

### 1) HackRF USB driver (Zadig)
Install a WinUSB driver for the HackRF so Windows can access it.

- Zadig: https://zadig.akeo.ie/

**Quick verify (HackRF plugged in):**
```powershell
hackrf_info
```

If `hackrf_info` says “HackRF not found”, fix Zadig/driver before continuing.

---

### 2) HackRF Tools (hackrf_sweep / hackrf_info)
HackRF Watchdog calls `hackrf_sweep.exe` under the hood.

- HackRF tools: https://github.com/fl1ckje/HackRF-tools

**PATH requirement:** Add the folder that contains `hackrf_sweep.exe` **and** `libhackrf.dll` to your PATH.

> Note: Depending on how/where you installed HackRF tools, this may be something like:
> `C:\HackRF\` or `C:\HackRF\bin\` or `C:\Program Files\HackRF\bin\`.

**Quick verify:**
```powershell
hackrf_info
hackrf_sweep -f 88:108 -w 2000000
```

---

### 3) Python
- Python 3.10+ (tested on Windows)

**Quick verify:**
```powershell
python --version
python -m pip --version
```

> Note: If PowerShell blocks venv activation ("running scripts is disabled"), either:
> - Use CMD activation: `.venv\Scripts\activate.bat`
> - OR allow scripts for your user:
>   ```powershell
>   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
>   ```
>   Close PowerShell, reopen it, and try activation again:
>   ```powershell
>   .\.venv\Scripts\Activate.ps1
>   ```

---

### 4) Git (optional)
Git is recommended for cloning/updating the repo.

- Git for Windows: https://git-scm.com/download/win

During installation, select:
**"Git from the command line and also from 3rd-party software"**

**Quick verify:**
```powershell
git --version
```

**No-Git option:** You can also use GitHub → **Code → Download ZIP**, then unzip.

---

### 5) Hardware
- A HackRF One (or more than one, if running multiple instances)
https://rtotech.org/
---

## Installation

### Option A (recommended): Clone with Git
```powershell
git clone https://github.com/Gang1eri/HackRF-Watchdog.git
cd HackRF-Watchdog
```

### Option B: Download ZIP (no Git)
1. GitHub → **Code** → **Download ZIP**
2. Unzip
3. Open a terminal in the unzipped folder (the one containing `main.py`)

---

## Create a virtual environment + install dependencies

From inside the repo folder:

```powershell
python -m venv .venv
python -m pip install --upgrade pip
```

### Activate the venv (choose ONE)

**PowerShell:**
```powershell
.\.venv\Scripts\Activate.ps1
```

**Command Prompt (always works, no policy changes):**
```bat
.venv\Scripts\activate.bat
```

### Install Python dependencies
With the venv active:

```powershell
pip install -r requirements.txt
```

---

## Running

Inside the repo (with the virtualenv active):

```powershell
python main.py
```

### Multiple instances (one per HackRF)
- Open two terminals
- Activate `.venv` in each
- Run `python main.py` in each terminal
- In each window, choose a different device in the **Device → HackRF** dropdown

---

## Usage overview

### Device selection
1. Plug in your HackRF(s)
2. In the app, go to the **Device** group
3. Click **Refresh**
4. Choose:
   - `Default (first HackRF)` or
   - A specific device: `HackRF 0 – SERIAL`, `HackRF 1 – SERIAL`, etc.

If you run multiple instances, bind each instance to a different serial to avoid “device busy” errors.

---

### Detection settings

- **Use local noise floor**
  - On: threshold is dB above estimated noise floor
  - Off: threshold is an absolute dB value

- **Persistence / hold time (s)**
  - Minimum time a frequency must stay above threshold before becoming a detection
  - `0.0` = trigger immediately on any above-threshold hit

- **Interval (ms)**
  - Extra delay between sweep cycles
  - `0` = run sweeps back-to-back as fast as possible

---

### Band configuration
For each band (A, B, C):
- Enable/disable
- Set start/stop frequency (MHz)
- Use presets (VHF Ham, UHF Ham + GMRS/FRS, 915 MHz ISM, 2.4 GHz ISM, 5.8 GHz ISM)
- Bin width can be set manually or automatically via Auto + Max bins

---

## Troubleshooting (common)

### `git` is not recognized
Install Git for Windows and reopen your terminal:
https://git-scm.com/download/win

### `running scripts is disabled on this system`
Either:
- Use CMD activation: `.venv\Scripts\activate.bat`
- OR allow venv activation for your user:
  ```powershell
  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
  ```

### `hackrf_info` works but Watchdog can’t run sweeps
Confirm `hackrf_sweep` works in the same terminal:
```powershell
hackrf_sweep -f 88:108 -w 2000000
```

### `hackrf_open() failed: HackRF not found (-5)`
Usually driver (Zadig/WinUSB) issue. Re-run Zadig and confirm WinUSB is installed for the HackRF device.

---

## Future work / ideas

- Optional spectrum/waterfall module
- Better ATAK “auto-detect” helpers (show all local IPv4s and warn about VPN/virtual adapters)
- Raspberry Pi / Linux packaging + desktop launcher

---

## License

TBD.
