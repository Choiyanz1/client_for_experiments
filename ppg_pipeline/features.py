from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Dict, Mapping, Sequence

from .exceptions import CalibrationInvalidError
from .models import RawSample, WindowFeature
from .stats import linear_regression, trimmed_mean


def segment_windows(
    samples: Sequence[RawSample],
    sample_rate_hz: int,
    window_seconds: int = 10,
    hop_seconds: int = 10,
):
    window_size = sample_rate_hz * window_seconds
    hop_size = sample_rate_hz * hop_seconds
    windows = []
    for start in range(0, len(samples) - window_size + 1, hop_size):
        end = start + window_size
        windows.append(samples[start:end])
    return windows


def build_quasi_dc_windows(
    samples: Sequence[RawSample],
    sample_rate_hz: int,
    target_hz: float = 0.1,
):
    if target_hz <= 0:
        raise ValueError("target_hz must be positive")
    window_size = int(round(sample_rate_hz / target_hz))
    if window_size <= 0:
        raise ValueError("quasi-DC window size must be positive")

    windows = []
    for start in range(0, len(samples) - window_size + 1, window_size):
        end = start + window_size
        windows.append(samples[start:end])
    return windows


def compute_window_intensity(window_samples: Sequence[RawSample], trim_ratio: float = 0.05) -> Dict[int, float]:
    intensities: Dict[int, float] = {}
    for wavelength in [670, 770, 810, 850, 950]:
        values = [sample.raw_by_wavelength[wavelength] for sample in window_samples]
        intensities[wavelength] = trimmed_mean(values, trim_ratio=trim_ratio)
    return intensities


def compute_absorbance(
    intensities: Mapping[int, float],
    i0_star: Mapping[str, float],
    eps: float = 1.0,
) -> Dict[int, float]:
    absorbance: Dict[int, float] = {}
    for wavelength, intensity in intensities.items():
        i0_value = float(i0_star[str(wavelength)])
        if i0_value <= 0:
            raise CalibrationInvalidError(f"Invalid calibration I0* at wavelength {wavelength}")
        if intensity <= 0:
            absorbance[wavelength] = math.nan
            continue
        absorbance[wavelength] = -math.log((intensity + eps) / (i0_value + eps))
    return absorbance


def compute_spectral_slope(absorbance: Mapping[int, float]) -> Dict[str, float]:
    wavelengths = [670.0, 770.0, 810.0, 850.0, 950.0]
    y_values = [float(absorbance[int(wavelength)]) for wavelength in wavelengths]
    if any(not math.isfinite(value) for value in y_values):
        return {"slope_m": math.nan, "intercept_b": math.nan, "fit_r2": math.nan}

    slope, intercept, fit_r2 = linear_regression(wavelengths, y_values)
    return {"slope_m": slope, "intercept_b": intercept, "fit_r2": fit_r2}


def compute_relative_absorbance(
    absorbance: Mapping[int, float],
    reference_wavelength_nm: int = 810,
) -> Dict[int, float]:
    reference_value = float(absorbance[reference_wavelength_nm])
    return {
        wavelength: float(value) - reference_value
        for wavelength, value in sorted(absorbance.items())
    }


def format_window_time(timestamp_us: float) -> str:
    return datetime.fromtimestamp(timestamp_us / 1_000_000.0, tz=timezone.utc).isoformat()
