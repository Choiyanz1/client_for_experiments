from __future__ import annotations

from typing import Dict, Mapping, Sequence

from .models import RawSample, WindowDiagnostics
from .stats import flatten_correlation_matrix, linear_regression, pearson_correlation


def compute_window_diagnostics(
    window_samples: Sequence[RawSample],
    absorbance: Mapping[int, float] | None = None,
) -> Dict[str, object]:
    series_by_channel: Dict[int, list] = {
        670: [],
        770: [],
        810: [],
        850: [],
        950: [],
    }

    for sample in window_samples:
        for wavelength in series_by_channel:
            series_by_channel[wavelength].append(sample.raw_by_wavelength[wavelength])

    time_axis = list(range(len(window_samples)))
    channel_slopes: Dict[int, float] = {}
    for wavelength, values in series_by_channel.items():
        slope, _, _ = linear_regression(time_axis, values)
        channel_slopes[wavelength] = slope

    anti_symmetry_score = compute_anti_symmetry_score(channel_slopes)
    common_mode_score = compute_common_mode_score(series_by_channel)
    correlation_matrix = flatten_correlation_matrix(series_by_channel)
    negative_absorbance_fraction = compute_negative_absorbance_fraction(absorbance or {})

    return {
        "channel_slopes": channel_slopes,
        "anti_symmetry_score": anti_symmetry_score,
        "common_mode_score": common_mode_score,
        "negative_absorbance_fraction": negative_absorbance_fraction,
        "correlation_matrix": correlation_matrix,
    }


def compute_anti_symmetry_score(channel_slopes: Mapping[int, float]) -> float:
    total_magnitude = sum(abs(slope) for slope in channel_slopes.values())
    if total_magnitude == 0:
        return 0.0
    return 1.0 - abs(sum(channel_slopes.values())) / total_magnitude


def compute_common_mode_score(series_by_channel: Mapping[int, Sequence[float]]) -> float:
    if not series_by_channel:
        return 0.0

    wavelengths = sorted(series_by_channel)
    sample_count = len(series_by_channel[wavelengths[0]])
    if sample_count < 2:
        return 0.0

    common_signal = []
    for index in range(sample_count):
        common_signal.append(
            sum(series_by_channel[wavelength][index] for wavelength in wavelengths) / float(len(wavelengths))
        )

    correlations = [pearson_correlation(series_by_channel[wavelength], common_signal) for wavelength in wavelengths]
    return sum(correlations) / float(len(correlations))


def compute_negative_absorbance_fraction(absorbance: Mapping[int, float]) -> float:
    if not absorbance:
        return 0.0
    values = [value for value in absorbance.values() if value == value]
    if not values:
        return 0.0
    return sum(1 for value in values if value < 0.0) / float(len(values))
