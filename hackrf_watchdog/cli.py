import sys
import click

from .config import DetectorConfig, SweepConfig, BandConfig, AlertConfig
from .sweep_backend import iter_sweep_frames, SweepBackendError
from .detect import detect_on_sweep_stream


def parse_band_option(band_str: str) -> BandConfig:
    """
    Parse band argument like:
      "name:center_hz:width_hz:threshold_db"
    Example:
      "ISM315:315e6:2e6:-40"
      "ISM915:915e6:8e6:-50"
    """
    parts = band_str.split(":")
    if len(parts) == 3:
        name = parts[0]
        center = float(parts[1])
        width = float(parts[2])
        threshold = -40.0
    elif len(parts) == 4:
        name = parts[0]
        center = float(parts[1])
        width = float(parts[2])
        threshold = float(parts[3])
    else:
        raise click.BadParameter(
            "Band must be 'name:center_hz:width_hz[:threshold_db]'"
        )

    return BandConfig(
        name=name,
        center_hz=center,
        width_hz=width,
        threshold_db=threshold,
    )


@click.command()
@click.option("--start-mhz", default=700.0, show_default=True, help="Sweep start frequency in MHz.")
@click.option("--stop-mhz", default=1300.0, show_default=True, help="Sweep stop frequency in MHz.")
@click.option("--bin-width-hz", default=250000.0, show_default=True, help="Bin width in Hz.")
@click.option(
    "--band",
    "bands",
    multiple=True,
    help="Band definition 'name:center_hz:width_hz[:threshold_db]'. "
         "You can pass this option multiple times.",
)
def main(start_mhz, stop_mhz, bin_width_hz, bands):
    """
    Hackrf-Watchdog: wideband HackRF sweep monitor with per-band alerts.
    """
    if not bands:
        bands = [
            "ISM315:315e6:2e6:-40",
            "ISM433:433.92e6:2e6:-45",
            "ISM915:915e6:8e6:-50",
        ]
        click.echo("No bands specified, using default ISM bands.", err=True)

    band_cfgs = [parse_band_option(b) for b in bands]

    sweep_cfg = SweepConfig(
        start_hz=start_mhz * 1e6,
        stop_hz=stop_mhz * 1e6,
        bin_width_hz=bin_width_hz,
    )

    det_cfg = DetectorConfig(
        sweep=sweep_cfg,
        bands=band_cfgs,
    )
    alert_cfg = AlertConfig()

    click.echo(
        f"Sweeping {start_mhz:.1f}-{stop_mhz:.1f} MHz "
        f"with bin width {bin_width_hz/1e3:.0f} kHz"
    )
    click.echo("Bands:")
    for b in band_cfgs:
        click.echo(
            f"  {b.name}: {b.center_hz/1e6:.3f} MHz, "
            f"width {b.width_hz/1e6:.3f} MHz, "
            f"threshold {b.threshold_db:.1f} dB"
        )

    try:
        sweep_iter = iter_sweep_frames(
            start_hz=det_cfg.sweep.start_hz,
            stop_hz=det_cfg.sweep.stop_hz,
            bin_width_hz=det_cfg.sweep.bin_width_hz,
        )
    except SweepBackendError as e:
        click.echo(f"Error starting hackrf_sweep: {e}", err=True)
        sys.exit(1)

    def on_alert(band, power_db):
        # Extra behavior can be added here later
        pass

    try:
        detect_on_sweep_stream(det_cfg, alert_cfg, sweep_iter, on_alert)
    except KeyboardInterrupt:
        click.echo("Stopped by user.", err=True)
