from __future__ import annotations

import math
from typing import Dict, Mapping, Optional, Sequence

from .calibration import estimate_saturation_ratio
from .models import QualityFlags, RawSample


def compute_quality_flags(
    window_samples: Sequence[RawSample],
    intensities: Mapping[int, float],
    absorbance: Mapping[int, float],
    spectral_fit: Mapping[str, float],
    diagnostics: Mapping[str, object],
    runtime_config: Mapping[str, object],
    expected_samples: int,
    previous_intensities: Optional[Mapping[int, float]] = None,
) -> QualityFlags:
    invalid_flags = []
    warning_flags = []

    if len(window_samples) < expected_samples:
        invalid_flags.append("missing_samples")

    parser_error_count = sum(1 for sample in window_samples if sample.parser_flags)
    if parser_error_count:
        invalid_flags.append("parser_error")

    if any(value <= 0 for value in intensities.values()):
        invalid_flags.append("non_positive_intensity")

    if any(not math.isfinite(value) for value in absorbance.values()):
        invalid_flags.append("invalid_absorbance")

    adc_clip_value = runtime_config["parser"].get("adc_clip_value")
    for wavelength in sorted(intensities):
        values = [sample.raw_by_wavelength[wavelength] for sample in window_samples]
        saturation_ratio = estimate_saturation_ratio(values, adc_clip_value)
        if saturation_ratio > 0.05:
            invalid_flags.append(f"saturation_{wavelength}")

        if _looks_detached(values):
            invalid_flags.append(f"detach_artifact_{wavelength}")

        if previous_intensities is not None and wavelength in previous_intensities:
            previous_value = float(previous_intensities[wavelength])
            current_value = float(intensities[wavelength])
            denominator = max(abs(previous_value), 1.0)
            relative_change = abs(current_value - previous_value) / denominator
            if relative_change > 0.20:
                warning_flags.append(f"window_jump_{wavelength}")

    anti_symmetry_score = float(diagnostics["anti_symmetry_score"])
    if anti_symmetry_score > 0.45:
        warning_flags.append("anti_symmetry")

    common_mode_score = float(diagnostics["common_mode_score"])
    if common_mode_score < 0.20:
        warning_flags.append("movement")

    if float(spectral_fit["fit_r2"]) < 0.55:
        warning_flags.append("low_fit_r2")

    return QualityFlags(
        invalid_flags=sorted(set(invalid_flags)),
        warning_flags=sorted(set(warning_flags)),
    )


def _looks_detached(values: Sequence[float]) -> bool:
    if len(values) < 3:
        return False
    dynamic_range = max(values) - min(values)
    return dynamic_range <= 1.0
