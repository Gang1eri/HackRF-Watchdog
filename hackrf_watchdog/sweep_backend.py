import subprocess
from typing import Iterable, List, Tuple, Dict, Generator


class SweepBackendError(Exception):
    """Custom exception for HackRF sweep backend errors."""
    pass


def parse_hackrf_sweep_line(line: str) -> Tuple[float, float, float, float, List[float]]:
    """
    Parse one line of hackrf_sweep output.

    Format from `hackrf_sweep -h`:
      date, time, hz_low, hz_high, hz_bin_width, num_samples, dB, dB, ...

    Returns:
      (timestamp_s, hz_low, hz_high, hz_bin_width, [power_dbm...])
    """
    parts = line.strip().split(",")
    if len(parts) < 7:
        raise ValueError(f"Not enough columns in sweep line: {line!r}")

    hz_low = float(parts[2].strip())
    hz_high = float(parts[3].strip())
    hz_bin_width = float(parts[4].strip())
    power_vals = [float(p.strip()) for p in parts[6:]]

    # Placeholder timestamp in seconds (can be improved later).
    timestamp_s = 0.0

    return timestamp_s, hz_low, hz_high, hz_bin_width, power_vals


def iter_sweep_frames(
    start_hz: float,
    stop_hz: float,
    bin_width_hz: float,
    extra_args: Iterable[str] = (),
) -> Generator[Dict, None, None]:
    """
    Launch hackrf_sweep and yield frames as dictionaries:

      {
        "timestamp_s": float,
        "low_hz": float,
        "high_hz": float,
        "bin_width_hz": float,
        "powers_dbm": [float, ...]
      }

    Raises SweepBackendError if hackrf_sweep exits without producing any data.
    Ensures the subprocess is always terminated and waited for.
    """
    # hackrf_sweep -f expects MHz min:max.
    start_mhz = start_hz / 1e6
    stop_mhz = stop_hz / 1e6

    cmd = [
        "hackrf_sweep",
        "-f",
        f"{int(round(start_mhz))}:{int(round(stop_mhz))}",
        "-w",
        str(int(bin_width_hz)),
        *extra_args,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        raise SweepBackendError(
            "hackrf_sweep executable not found. "
            "Make sure hackrf-tools is installed and hackrf_sweep is on your PATH."
        )

    assert proc.stdout is not None
    got_data = False

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            try:
                ts, low_hz, high_hz, bin_width, powers = parse_hackrf_sweep_line(line)
            except ValueError:
                continue

            got_data = True
            yield {
                "timestamp_s": ts,
                "low_hz": low_hz,
                "high_hz": high_hz,
                "bin_width_hz": bin_width,
                "powers_dbm": powers,
            }

        proc.wait()

        if not got_data:
            stderr_text = ""
            if proc.stderr is not None:
                try:
                    stderr_text = proc.stderr.read() or ""
                except Exception:
                    stderr_text = ""
            raise SweepBackendError(
                f"hackrf_sweep produced no data. Exit code: {proc.returncode}, "
                f"stderr: {stderr_text.strip()}"
            )

    finally:
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        except Exception:
            pass
