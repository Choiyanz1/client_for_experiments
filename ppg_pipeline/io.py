from __future__ import annotations

import csv
import json
import logging
import math
import time
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional

from .config import WAVELENGTHS_NM, compute_config_hash
from .exceptions import CalibrationInvalidError, ConfigMismatchError
from .lookup_table import LOOKUP_FILENAME
from .models import RawSample, WindowDiagnostics, WindowFeature

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Raw BLE stream parsing (positional CSV, no header)
# ---------------------------------------------------------------------------

def _parse_line(line: str, parser: Mapping[str, object]) -> Optional[RawSample]:
    """Parse a single positional CSV line from the luckfox BLE sender.

    Format: serial, sensor_type, sensor_ts, host_ts, ppg[0..4], ax..mz
    Returns a RawSample if the line is a valid PPG row, else None.
    """
    line = line.strip()
    if not line or line.startswith("serial"):
        return None

    cols = line.split(",")
    n = len(cols)
    col_serial = int(parser["col_serial"])
    col_type = int(parser["col_sensor_type"])
    col_ts = int(parser["col_sensor_ts"])
    col_host = int(parser["col_host_ts"])
    col_ppg = int(parser["col_ppg_start"])
    ppg_count = int(parser["ppg_channel_count"])

    if n < col_ppg + ppg_count:
        return None

    try:
        sensor_type = cols[col_type].strip()
        if sensor_type != parser["sensor_type_ppg"]:
            return None

        serial = int(cols[col_serial])
        timestamp_us = float(cols[col_ts])
        host_timestamp = float(cols[col_host])

        raw_by_wavelength: Dict[int, float] = {}
        parser_flags: List[str] = []
        for i, wl in enumerate(WAVELENGTHS_NM[:ppg_count]):
            try:
                raw_by_wavelength[wl] = float(cols[col_ppg + i])
            except (ValueError, IndexError):
                parser_flags.append(f"missing_ppg_{wl}")

        return RawSample(
            serial=serial,
            timestamp_us=timestamp_us,
            host_timestamp=host_timestamp,
            raw_by_wavelength=raw_by_wavelength,
            parser_flags=parser_flags,
        )
    except (ValueError, IndexError):
        return None


def parse_raw_stream(input_path: str, runtime_config: Mapping[str, object]) -> List[RawSample]:
    """Read a raw CSV file and return PPG-only RawSamples."""
    path = Path(input_path)
    if not path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")

    parser = runtime_config["parser"]
    samples: List[RawSample] = []
    prev_serial: Optional[int] = None
    prev_ts: Optional[float] = None

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            sample = _parse_line(line, parser)
            if sample is None:
                continue

            if prev_serial is not None:
                if sample.serial < prev_serial:
                    sample.parser_flags.append("serial_rollback")
                elif sample.serial - prev_serial > 1:
                    sample.parser_flags.append("serial_gap")

            if prev_ts is not None:
                if sample.timestamp_us < prev_ts:
                    sample.parser_flags.append("timestamp_rollback")
                elif sample.timestamp_us - prev_ts > (2.5 * 1_000_000.0 / float(runtime_config["sample_rate_hz"])):
                    sample.parser_flags.append("timestamp_gap")

            prev_serial = sample.serial
            prev_ts = sample.timestamp_us
            samples.append(sample)

    return samples


def parse_stdin_stream(runtime_config: Mapping[str, object]) -> Iterator[RawSample]:
    """Yield PPG RawSamples from stdin (pipe from zig_bt_client --stdout)."""
    import sys
    parser = runtime_config["parser"]
    for line in sys.stdin:
        sample = _parse_line(line, parser)
        if sample is not None:
            yield sample


def follow_raw_stream(
    input_path: str,
    runtime_config: Mapping[str, object],
    idle_timeout: float = 3.0,
    poll_interval: float = 0.25,
) -> Iterator[RawSample]:
    """Tail a growing raw CSV file and yield PPG samples."""
    processed_count = 0
    idle_started_at: Optional[float] = None
    path = Path(input_path)

    while True:
        if path.exists() and path.stat().st_size > 0:
            samples = parse_raw_stream(input_path, runtime_config)
        else:
            samples = []

        if len(samples) > processed_count:
            for sample in samples[processed_count:]:
                yield sample
            processed_count = len(samples)
            idle_started_at = None
        else:
            if processed_count > 0:
                idle_started_at = idle_started_at or time.monotonic()
                if time.monotonic() - idle_started_at >= idle_timeout:
                    break
            time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Calibration I/O
# ---------------------------------------------------------------------------

def save_white_card_calibration(calibration: Mapping[str, object], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(calibration, handle, indent=2, sort_keys=True)
        handle.write("\n")


def save_json(payload: Mapping[str, object], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def load_white_card_calibration(
    calibration_path: str,
    runtime_config: Mapping[str, object],
) -> Dict[str, object]:
    path = Path(calibration_path)
    with path.open("r", encoding="utf-8") as handle:
        calibration = json.load(handle)

    expected_hash = compute_config_hash(runtime_config)
    actual_hash = calibration.get("device_config_hash")
    if actual_hash != expected_hash:
        raise ConfigMismatchError(
            f"Calibration config hash mismatch: expected {expected_hash}, got {actual_hash}"
        )

    i0_star = calibration.get("i0_star", {})
    if not isinstance(i0_star, dict):
        raise CalibrationInvalidError("Calibration file is missing i0_star")

    for wavelength, value in i0_star.items():
        if value is None or float(value) <= 0:
            raise CalibrationInvalidError(f"Calibration I0* is invalid for wavelength {wavelength}")

    return calibration


def load_photodiode_current_lookup_table(
    lookup_path: str,
    runtime_config: Mapping[str, object],
) -> Dict[str, object]:
    path = Path(lookup_path)
    with path.open("r", encoding="utf-8") as handle:
        lookup_table = json.load(handle)

    expected_hash = compute_config_hash(runtime_config)
    actual_hash = lookup_table.get("device_config_hash")
    if actual_hash != expected_hash:
        raise ConfigMismatchError(
            f"Lookup table config hash mismatch: expected {expected_hash}, got {actual_hash}"
        )

    references = lookup_table.get("incident_reference_by_wavelength", {})
    if not isinstance(references, dict):
        raise CalibrationInvalidError("Lookup table is missing incident_reference_by_wavelength")

    for wavelength, value in references.items():
        if value is None or float(value) <= 0:
            raise CalibrationInvalidError(f"Lookup incident reference is invalid for wavelength {wavelength}")

    return lookup_table


# ---------------------------------------------------------------------------
# Feature / diagnostics CSV export
# ---------------------------------------------------------------------------

def export_features_csv(features: Iterable[WindowFeature], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_feature_fieldnames())
        writer.writeheader()
        for feature in features:
            writer.writerow(_feature_to_row(feature))


def append_feature_csv_row(feature: WindowFeature, output_path: str, write_header: bool) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_feature_fieldnames())
        if write_header:
            writer.writeheader()
        writer.writerow(_feature_to_row(feature))


def export_diagnostics_csv(diagnostics_rows: Iterable[WindowDiagnostics], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_diagnostic_fieldnames())
        writer.writeheader()
        for row in diagnostics_rows:
            writer.writerow(_diagnostic_to_row(row))


def append_diagnostics_csv_row(row: WindowDiagnostics, output_path: str, write_header: bool) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_diagnostic_fieldnames())
        if write_header:
            writer.writeheader()
        writer.writerow(_diagnostic_to_row(row))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _feature_fieldnames() -> List[str]:
    wavelengths = ["670", "770", "810", "850", "950"]
    return (
        ["window_start_time", "window_end_time"]
        + [f"I{wavelength}" for wavelength in wavelengths]
        + [f"A{wavelength}" for wavelength in wavelengths]
        + [f"Arel{wavelength}_810" for wavelength in wavelengths]
        + ["slope_m", "intercept_b", "fit_r2", "is_valid", "invalid_flags", "warning_flags", "quality_flag", "notes"]
    )


def _feature_to_row(feature: WindowFeature) -> Dict[str, object]:
    row: Dict[str, object] = {
        "window_start_time": feature.window_start_time,
        "window_end_time": feature.window_end_time,
        "slope_m": feature.slope_m,
        "intercept_b": feature.intercept_b,
        "fit_r2": feature.fit_r2,
        "is_valid": feature.quality.is_valid,
        "invalid_flags": ";".join(feature.quality.invalid_flags),
        "warning_flags": ";".join(feature.quality.warning_flags),
        "quality_flag": "valid" if feature.quality.is_valid else "invalid",
        "notes": feature.notes,
    }
    for wavelength, value in sorted(feature.intensities.items()):
        row[f"I{wavelength}"] = value
    for wavelength, value in sorted(feature.absorbance.items()):
        row[f"A{wavelength}"] = value
    for wavelength, value in sorted(feature.relative_absorbance.items()):
        row[f"Arel{wavelength}_810"] = value
    return row


def _diagnostic_fieldnames() -> List[str]:
    pair_keys = [
        "670_770", "670_810", "670_850", "670_950",
        "770_810", "770_850", "770_950",
        "810_850", "810_950",
        "850_950",
    ]
    return (
        ["window_start_time", "window_end_time"]
        + [f"slope_{wavelength}" for wavelength in ("670", "770", "810", "850", "950")]
        + ["anti_symmetry_score", "common_mode_score", "negative_absorbance_fraction"]
        + [f"corr_{key}" for key in pair_keys]
        + ["warning_flags"]
    )


def _diagnostic_to_row(row: WindowDiagnostics) -> Dict[str, object]:
    output: Dict[str, object] = {
        "window_start_time": row.window_start_time,
        "window_end_time": row.window_end_time,
        "anti_symmetry_score": row.anti_symmetry_score,
        "common_mode_score": row.common_mode_score,
        "negative_absorbance_fraction": row.negative_absorbance_fraction,
        "warning_flags": ";".join(row.warning_flags),
    }
    for wavelength, value in sorted(row.channel_slopes.items()):
        output[f"slope_{wavelength}"] = value
    for key, value in sorted(row.correlation_matrix.items()):
        output[f"corr_{key}"] = value
    return output
