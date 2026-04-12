from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class RawSample:
    serial: int
    timestamp_us: float
    host_timestamp: float
    raw_by_wavelength: Dict[int, float]
    parser_flags: List[str] = field(default_factory=list)


@dataclass
class QualityFlags:
    invalid_flags: List[str] = field(default_factory=list)
    warning_flags: List[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.invalid_flags


@dataclass
class WindowFeature:
    window_start_time: str
    window_end_time: str
    intensities: Dict[int, float]
    absorbance: Dict[int, float]
    relative_absorbance: Dict[int, float]
    slope_m: float
    intercept_b: float
    fit_r2: float
    quality: QualityFlags
    notes: str


@dataclass
class WindowDiagnostics:
    window_start_time: str
    window_end_time: str
    channel_slopes: Dict[int, float]
    anti_symmetry_score: float
    common_mode_score: float
    negative_absorbance_fraction: float
    correlation_matrix: Dict[str, float]
    warning_flags: List[str] = field(default_factory=list)
