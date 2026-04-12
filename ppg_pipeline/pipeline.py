from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .diagnostics import compute_window_diagnostics
from .features import (
    build_quasi_dc_windows,
    compute_absorbance,
    compute_relative_absorbance,
    compute_spectral_slope,
    compute_window_intensity,
    format_window_time,
)
from .io import (
    append_diagnostics_csv_row,
    append_feature_csv_row,
    export_diagnostics_csv,
    export_features_csv,
    follow_raw_stream,
    load_photodiode_current_lookup_table,
    load_white_card_calibration,
    parse_raw_stream,
)
from .lookup_table import default_lookup_table_path_for_calibration, resolve_incident_reference
from .models import WindowDiagnostics, WindowFeature
from .quality import compute_quality_flags

LOGGER = logging.getLogger(__name__)


def extract_features_pipeline(
    input_path: str,
    calibration_path: str,
    output_path: str,
    diagnostics_output_path: Optional[str],
    runtime_config: Mapping[str, object],
    follow: bool = False,
    idle_timeout: float = 3.0,
    poll_interval: float = 0.25,
) -> Tuple[List[WindowFeature], List[WindowDiagnostics]]:
    calibration = load_white_card_calibration(calibration_path, runtime_config)
    lookup_table = _load_lookup_table(calibration_path, runtime_config)
    if follow:
        return _follow_and_extract(
            input_path=input_path,
            calibration=calibration,
            lookup_table=lookup_table,
            output_path=output_path,
            diagnostics_output_path=diagnostics_output_path,
            runtime_config=runtime_config,
            idle_timeout=idle_timeout,
            poll_interval=poll_interval,
        )

    samples = parse_raw_stream(input_path, runtime_config)
    features, diagnostics_rows = _extract_from_samples(samples, calibration, lookup_table, runtime_config)
    export_features_csv(features, output_path)
    if diagnostics_output_path:
        export_diagnostics_csv(diagnostics_rows, diagnostics_output_path)
    return features, diagnostics_rows


def _follow_and_extract(
    input_path: str,
    calibration: Mapping[str, object],
    lookup_table: Mapping[str, object] | None,
    output_path: str,
    diagnostics_output_path: Optional[str],
    runtime_config: Mapping[str, object],
    idle_timeout: float,
    poll_interval: float,
) -> Tuple[List[WindowFeature], List[WindowDiagnostics]]:
    sample_rate_hz = int(runtime_config["sample_rate_hz"])
    quasi_dc_target_hz = float(runtime_config["windowing"]["quasi_dc_target_hz"])
    window_size = int(round(sample_rate_hz / quasi_dc_target_hz))
    hop_size = window_size

    buffer = []
    features: List[WindowFeature] = []
    diagnostics_rows: List[WindowDiagnostics] = []
    wrote_feature_header = False
    wrote_diagnostics_header = False
    previous_intensities = None

    for sample in follow_raw_stream(
        input_path=input_path,
        runtime_config=runtime_config,
        idle_timeout=idle_timeout,
        poll_interval=poll_interval,
    ):
        buffer.append(sample)
        while len(buffer) >= window_size:
            window_samples = buffer[:window_size]
            feature, diagnostics = _build_feature(
                window_samples,
                calibration,
                lookup_table,
                runtime_config,
                previous_intensities=previous_intensities,
            )
            previous_intensities = dict(feature.intensities)
            features.append(feature)
            diagnostics_rows.append(diagnostics)
            append_feature_csv_row(feature, output_path, write_header=not wrote_feature_header)
            wrote_feature_header = True
            if diagnostics_output_path:
                append_diagnostics_csv_row(
                    diagnostics,
                    diagnostics_output_path,
                    write_header=not wrote_diagnostics_header,
                )
                wrote_diagnostics_header = True
            del buffer[:hop_size]

    return features, diagnostics_rows


def _extract_from_samples(
    samples: Sequence[object],
    calibration: Mapping[str, object],
    lookup_table: Mapping[str, object] | None,
    runtime_config: Mapping[str, object],
) -> Tuple[List[WindowFeature], List[WindowDiagnostics]]:
    sample_rate_hz = int(runtime_config["sample_rate_hz"])
    quasi_dc_target_hz = float(runtime_config["windowing"]["quasi_dc_target_hz"])

    features: List[WindowFeature] = []
    diagnostics_rows: List[WindowDiagnostics] = []
    previous_intensities = None
    for window_samples in build_quasi_dc_windows(samples, sample_rate_hz, quasi_dc_target_hz):
        feature, diagnostics = _build_feature(
            window_samples,
            calibration,
            lookup_table,
            runtime_config,
            previous_intensities=previous_intensities,
        )
        previous_intensities = dict(feature.intensities)
        features.append(feature)
        diagnostics_rows.append(diagnostics)
    return features, diagnostics_rows


def _build_feature(
    window_samples: Sequence[object],
    calibration: Mapping[str, object],
    lookup_table: Mapping[str, object] | None,
    runtime_config: Mapping[str, object],
    previous_intensities: Mapping[int, float] | None = None,
) -> Tuple[WindowFeature, WindowDiagnostics]:
    trim_ratio = float(runtime_config["windowing"]["trim_ratio"])
    absorbance_eps = float(runtime_config["windowing"]["absorbance_eps"])
    sample_rate_hz = int(runtime_config["sample_rate_hz"])
    quasi_dc_target_hz = float(runtime_config["windowing"]["quasi_dc_target_hz"])
    expected_samples = int(round(sample_rate_hz / quasi_dc_target_hz))

    intensities = compute_window_intensity(window_samples, trim_ratio=trim_ratio)
    incident_reference = resolve_incident_reference(calibration, lookup_table)
    absorbance = compute_absorbance(intensities, incident_reference, eps=absorbance_eps)
    relative_absorbance = compute_relative_absorbance(absorbance, reference_wavelength_nm=810)
    spectral_fit = compute_spectral_slope(absorbance)
    diagnostics_payload = compute_window_diagnostics(window_samples, absorbance=absorbance)
    quality = compute_quality_flags(
        window_samples=window_samples,
        intensities=intensities,
        absorbance=absorbance,
        spectral_fit=spectral_fit,
        diagnostics=diagnostics_payload,
        runtime_config=runtime_config,
        expected_samples=expected_samples,
        previous_intensities=previous_intensities,
    )

    notes = []
    if quality.invalid_flags:
        notes.append("invalid:" + ",".join(quality.invalid_flags))
    if quality.warning_flags:
        notes.append("warning:" + ",".join(quality.warning_flags))

    window_start = format_window_time(window_samples[0].timestamp_us)
    window_end = format_window_time(window_samples[-1].timestamp_us)
    feature = WindowFeature(
        window_start_time=window_start,
        window_end_time=window_end,
        intensities=intensities,
        absorbance=absorbance,
        relative_absorbance=relative_absorbance,
        slope_m=spectral_fit["slope_m"],
        intercept_b=spectral_fit["intercept_b"],
        fit_r2=spectral_fit["fit_r2"],
        quality=quality,
        notes=" | ".join(notes),
    )
    diagnostics = WindowDiagnostics(
        window_start_time=window_start,
        window_end_time=window_end,
        channel_slopes=diagnostics_payload["channel_slopes"],
        anti_symmetry_score=diagnostics_payload["anti_symmetry_score"],
        common_mode_score=diagnostics_payload["common_mode_score"],
        negative_absorbance_fraction=diagnostics_payload["negative_absorbance_fraction"],
        correlation_matrix=diagnostics_payload["correlation_matrix"],
        warning_flags=quality.warning_flags,
    )
    return feature, diagnostics


def _load_lookup_table(
    calibration_path: str,
    runtime_config: Mapping[str, object],
) -> Mapping[str, object] | None:
    lookup_path = default_lookup_table_path_for_calibration(calibration_path)
    if not lookup_path.exists():
        return None
    return load_photodiode_current_lookup_table(str(lookup_path), runtime_config)
