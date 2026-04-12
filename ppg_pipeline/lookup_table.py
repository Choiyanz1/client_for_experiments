from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Mapping, Optional


LOOKUP_FILENAME = "photodiode_current_lookup_table.json"


def build_photodiode_current_lookup_table(
    calibration: Mapping[str, object],
    runtime_config: Mapping[str, object],
) -> Dict[str, object]:
    """Build a paper-style incident-light lookup table from white-card calibration."""
    return {
        "device_config_hash": calibration["device_config_hash"],
        "reference_setup": "reflective_white_card",
        "combined_photodiodes": True,
        "lookup_units": "adc_counts_proxy",
        "sample_rate_hz": calibration.get("sample_rate_hz"),
        "wavelengths_nm": calibration.get("wavelengths_nm", [670, 770, 810, 850, 950]),
        "led_current_hex_by_wavelength": runtime_config.get("led_current_hex_by_wavelength", {}),
        "incident_reference_by_wavelength": calibration["i0_star"],
        "source_calibration_created_at": calibration.get("created_at"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "accepted": bool(calibration.get("accepted", False)),
    }


def default_lookup_table_path_for_calibration(calibration_path: str) -> Path:
    calibration_file = Path(calibration_path)
    return calibration_file.with_name(LOOKUP_FILENAME)


def resolve_incident_reference(
    calibration: Mapping[str, object],
    lookup_table: Optional[Mapping[str, object]],
) -> Dict[str, float]:
    if lookup_table is not None:
        return {
            str(wavelength): float(value)
            for wavelength, value in lookup_table["incident_reference_by_wavelength"].items()
        }
    return {
        str(wavelength): float(value)
        for wavelength, value in calibration["i0_star"].items()
    }
