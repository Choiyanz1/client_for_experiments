from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence

from .config import compute_config_hash
from .io import parse_raw_stream
from .stats import mean_std_cv, median, trimmed_mean

LOGGER = logging.getLogger(__name__)

DEFAULT_CALIBRATION_THRESHOLDS = {
    "max_cv": 0.20,
    "max_cross_run_rel_diff": 0.15,
    "max_saturation_ratio": 0.05,
    "min_run_coverage": 0.80,
}


def run_white_card_calibration(
    input_paths: Sequence[str],
    runtime_config: Mapping[str, object],
    trim_ratio: float = 0.05,
    min_runs: int = 3,
    run_duration_sec: int = 30,
    thresholds: Mapping[str, float] = DEFAULT_CALIBRATION_THRESHOLDS,
) -> Dict[str, object]:
    sample_rate_hz = int(runtime_config["sample_rate_hz"])
    expected_samples = sample_rate_hz * int(run_duration_sec)
    run_stats: List[Dict[str, object]] = []
    accepted_trimmed_means: Dict[int, List[float]] = {
        670: [],
        770: [],
        810: [],
        850: [],
        950: [],
    }

    for input_path in input_paths:
        samples = parse_raw_stream(input_path, runtime_config)
        run_stat = _compute_run_stats(
            input_path=input_path,
            samples=samples,
            expected_samples=expected_samples,
            trim_ratio=trim_ratio,
            adc_clip_value=runtime_config["parser"].get("adc_clip_value"),
            thresholds=thresholds,
        )
        run_stats.append(run_stat)
        if run_stat["accepted"]:
            for wavelength, channel_stats in run_stat["channel_stats"].items():
                accepted_trimmed_means[int(wavelength)].append(channel_stats["trimmed_mean"])

    overall_invalid_reasons: List[str] = []
    accepted_run_count = sum(1 for run in run_stats if run["accepted"])
    if accepted_run_count < min_runs:
        overall_invalid_reasons.append(f"insufficient_accepted_runs<{min_runs}")

    i0_star: Dict[str, float] = {}
    for wavelength, trimmed_values in accepted_trimmed_means.items():
        if not trimmed_values:
            overall_invalid_reasons.append(f"missing_accepted_run_for_{wavelength}")
            continue
        reference = median(trimmed_values)
        i0_star[str(wavelength)] = reference
        relative_diffs = [abs(value - reference) / reference if reference else 0.0 for value in trimmed_values]
        if relative_diffs and max(relative_diffs) > thresholds["max_cross_run_rel_diff"]:
            overall_invalid_reasons.append(f"cross_run_difference_too_large_{wavelength}")

    accepted = not overall_invalid_reasons
    result = {
        "device_config_hash": compute_config_hash(runtime_config),
        "sample_rate_hz": sample_rate_hz,
        "wavelengths_nm": [670, 770, 810, 850, 950],
        "i0_star": i0_star,
        "run_stats": run_stats,
        "accepted": accepted,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "trim_ratio": trim_ratio,
        "run_duration_sec": run_duration_sec,
        "invalid_reasons": overall_invalid_reasons,
    }
    return result


def _compute_run_stats(
    input_path: str,
    samples: Sequence[object],
    expected_samples: int,
    trim_ratio: float,
    adc_clip_value: object,
    thresholds: Mapping[str, float],
) -> Dict[str, object]:
    channel_stats: Dict[str, Dict[str, float]] = {}
    invalid_reasons: List[str] = []
    duration_sec = 0.0

    if samples:
        duration_sec = (samples[-1].timestamp_us - samples[0].timestamp_us) / 1_000_000.0

    if len(samples) < expected_samples * thresholds["min_run_coverage"]:
        invalid_reasons.append("insufficient_samples")

    for wavelength in [670, 770, 810, 850, 950]:
        values = [sample.raw_by_wavelength[wavelength] for sample in samples if wavelength in sample.raw_by_wavelength]
        if not values:
            invalid_reasons.append(f"missing_channel_{wavelength}")
            continue

        mean_value, std_value, cv_value = mean_std_cv(values)
        trimmed_value = trimmed_mean(values, trim_ratio=trim_ratio)
        saturation_ratio = estimate_saturation_ratio(values, adc_clip_value)
        channel_invalids: List[str] = []
        if mean_value <= 0:
            channel_invalids.append("mean_non_positive")
        if cv_value > thresholds["max_cv"]:
            channel_invalids.append("cv_too_high")
        if saturation_ratio > thresholds["max_saturation_ratio"]:
            channel_invalids.append("saturation")
        if channel_invalids:
            invalid_reasons.extend(f"{reason}_{wavelength}" for reason in channel_invalids)

        channel_stats[str(wavelength)] = {
            "mean": mean_value,
            "std": std_value,
            "trimmed_mean": trimmed_value,
            "cv": cv_value,
            "saturation_ratio": saturation_ratio,
        }

    accepted = not invalid_reasons
    return {
        "path": str(Path(input_path)),
        "sample_count": len(samples),
        "duration_sec": duration_sec,
        "channel_stats": channel_stats,
        "accepted": accepted,
        "invalid_reasons": invalid_reasons,
    }


def estimate_saturation_ratio(values: Sequence[float], adc_clip_value: object) -> float:
    if not values:
        return 0.0

    if adc_clip_value not in (None, "", 0):
        clip_value = float(adc_clip_value)
        cutoff = clip_value * 0.995
        return sum(1 for value in values if value >= cutoff) / float(len(values))

    channel_max = max(values)
    top_hits = sum(1 for value in values if value >= channel_max)
    return top_hits / float(len(values))
