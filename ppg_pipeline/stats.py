from __future__ import annotations

import math
from typing import Dict, Sequence, Tuple


def trimmed_mean(values: Sequence[float], trim_ratio: float = 0.05) -> float:
    if not values:
        raise ValueError("trimmed_mean requires at least one value")
    ordered = sorted(values)
    trim_count = int(len(ordered) * trim_ratio)
    if trim_count * 2 >= len(ordered):
        trim_count = 0
    trimmed = ordered[trim_count : len(ordered) - trim_count]
    return sum(trimmed) / float(len(trimmed))


def mean_std_cv(values: Sequence[float]) -> Tuple[float, float, float]:
    if not values:
        raise ValueError("mean_std_cv requires at least one value")
    mu = sum(values) / float(len(values))
    variance = sum((value - mu) ** 2 for value in values) / float(len(values))
    std = math.sqrt(variance)
    cv = std / mu if mu != 0 else math.inf
    return mu, std, cv


def median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("median requires at least one value")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def linear_regression(x_values: Sequence[float], y_values: Sequence[float]) -> Tuple[float, float, float]:
    if len(x_values) != len(y_values):
        raise ValueError("x and y must have the same length")
    if len(x_values) < 2:
        raise ValueError("at least two points are required")

    x_mean = sum(x_values) / float(len(x_values))
    y_mean = sum(y_values) / float(len(y_values))
    ss_xx = sum((value - x_mean) ** 2 for value in x_values)
    if ss_xx == 0:
        raise ValueError("x values must not be constant")

    ss_xy = sum((x_value - x_mean) * (y_value - y_mean) for x_value, y_value in zip(x_values, y_values))
    slope = ss_xy / ss_xx
    intercept = y_mean - slope * x_mean

    ss_tot = sum((y_value - y_mean) ** 2 for y_value in y_values)
    ss_res = sum((y_value - (slope * x_value + intercept)) ** 2 for x_value, y_value in zip(x_values, y_values))
    if ss_tot == 0:
        r2 = 1.0 if ss_res == 0 else 0.0
    else:
        r2 = 1.0 - (ss_res / ss_tot)
    return slope, intercept, r2


def pearson_correlation(x_values: Sequence[float], y_values: Sequence[float]) -> float:
    if len(x_values) != len(y_values):
        raise ValueError("x and y must have the same length")
    if len(x_values) < 2:
        return 0.0

    x_mean = sum(x_values) / float(len(x_values))
    y_mean = sum(y_values) / float(len(y_values))
    numerator = sum((x_value - x_mean) * (y_value - y_mean) for x_value, y_value in zip(x_values, y_values))
    denominator_x = math.sqrt(sum((x_value - x_mean) ** 2 for x_value in x_values))
    denominator_y = math.sqrt(sum((y_value - y_mean) ** 2 for y_value in y_values))
    denominator = denominator_x * denominator_y
    if denominator == 0:
        return 0.0
    return numerator / denominator


def flatten_correlation_matrix(series_by_channel: Dict[int, Sequence[float]]) -> Dict[str, float]:
    wavelengths = sorted(series_by_channel)
    output: Dict[str, float] = {}
    for left_index, left_nm in enumerate(wavelengths):
        for right_nm in wavelengths[left_index + 1 :]:
            key = f"{left_nm}_{right_nm}"
            output[key] = pearson_correlation(series_by_channel[left_nm], series_by_channel[right_nm])
    return output
