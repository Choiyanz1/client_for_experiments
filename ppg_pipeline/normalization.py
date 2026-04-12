from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Mapping


def build_relative_lookup_table(
    calibration: Mapping[str, object],
    reference_wavelength_nm: int = 810,
) -> Dict[str, object]:
    i0_star = calibration["i0_star"]
    reference_key = str(reference_wavelength_nm)
    reference_value = float(i0_star[reference_key])
    if reference_value <= 0:
        raise ValueError(f"Reference I0* must be positive for {reference_wavelength_nm} nm")

    scale_by_wavelength = {}
    for wavelength, value in sorted(i0_star.items(), key=lambda item: int(item[0])):
        scale_by_wavelength[str(wavelength)] = float(value) / reference_value

    return {
        "device_config_hash": calibration["device_config_hash"],
        "reference_wavelength_nm": reference_wavelength_nm,
        "sample_rate_hz": calibration.get("sample_rate_hz"),
        "wavelengths_nm": calibration.get("wavelengths_nm", [670, 770, 810, 850, 950]),
        "scale_by_wavelength": scale_by_wavelength,
        "source_calibration_created_at": calibration.get("created_at"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "accepted": bool(calibration.get("accepted", False)),
    }
