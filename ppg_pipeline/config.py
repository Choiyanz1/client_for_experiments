from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

DEFAULT_RUNTIME_CONFIG: Dict[str, Any] = {
    "sample_rate_hz": 100,
    "wavelength_mapping_nm_by_column": {
        "ppg_A": 670,
        "ppg_B": 770,
        "ppg_C": 810,
        "ppg_D": 850,
        "ppg_E": 950,
    },
    "led_current_hex_by_wavelength": {
        "670": "0x4f",
        "770": "0x4f",
        "810": "0x4f",
        "850": "0x4f",
        "950": "0x4f",
    },
    "tia_gain_by_wavelength": {
        "670": "KOHM12_5",
        "770": "KOHM12_5",
        "810": "KOHM12_5",
        "850": "KOHM12_5",
        "950": "KOHM12_5",
    },
    "pulse_width_us_by_wavelength": {
        "670": "0x2",
        "770": "0x2",
        "810": "0x2",
        "850": "0x2",
        "950": "0x2",
    },
    "integration_width_by_wavelength": {
        "670": "0x3",
        "770": "0x3",
        "810": "0x3",
        "850": "0x3",
        "950": "0x3",
    },
    "repeats": 1,
    "subtract": "0x0",
    "reverse_integration": "0x0",
    "adc_offset": 0,
    # Positional parser for raw BLE stream from luckfox sender.
    # Raw line format (no header):
    #   serial, sensor_type, sensor_ts, host_ts, ppg[0..4], ax..mz
    # sensor_type is "PPG" or "IMU".
    "parser": {
        "col_serial": 0,
        "col_sensor_type": 1,
        "col_sensor_ts": 2,
        "col_host_ts": 3,
        "col_ppg_start": 4,
        "ppg_channel_count": 5,
        "sensor_type_ppg": "PPG",
        "sensor_type_imu": "IMU",
        "adc_clip_value": None,
    },
    "windowing": {
        "window_seconds": 10,
        "hop_seconds": 10,
        "quasi_dc_target_hz": 0.1,
        "trim_ratio": 0.05,
        "absorbance_eps": 1.0,
        "alpha_scaling_enabled": False,
    },
}


# Ordered wavelength list matching the 5 ADPD4101 timeslots (ppg[0]..ppg[4]).
WAVELENGTHS_NM = [670, 770, 810, 850, 950]


def load_runtime_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load the runtime configuration used for calibration and feature extraction."""
    config = copy.deepcopy(DEFAULT_RUNTIME_CONFIG)
    if not config_path:
        return config

    with Path(config_path).open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)

    return _deep_merge(config, loaded)


def compute_config_hash(config: Mapping[str, Any]) -> str:
    payload = json.dumps(_normalize_for_hash(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _deep_merge(base: Dict[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            base[key] = _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _normalize_for_hash(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _normalize_for_hash(val) for key, val in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, list):
        return [_normalize_for_hash(item) for item in value]
    return value
