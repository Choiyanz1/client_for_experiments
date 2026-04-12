#!/usr/bin/env python3
"""
PPG Pre-flight Quality Check — verify optical signal quality before experiments.

Reads live PPG data from stdin (pipe from zig_bt_client) or a raw CSV file,
accumulates windows of samples, and reports per-channel quality metrics:
  - Mean intensity & trimmed mean
  - Coefficient of variation (CV)
  - Saturation ratio
  - Detach detection (flat signal)
  - Absorbance sanity (if calibration JSON is provided)

Usage:
  zig_bt_client --stdout --no-file | python3 tools/ppg_preflight_check.py
  zig_bt_client --stdout --no-file | python3 tools/ppg_preflight_check.py --calibration calibration/white_card_calibration.json
  python3 tools/ppg_preflight_check.py --input Raw_data.csv --follow
"""
from __future__ import annotations

import argparse
import collections
import math
import os
import sys
import time
from typing import Dict, List, Mapping, Optional, Sequence

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from ppg_pipeline.calibration import estimate_saturation_ratio
from ppg_pipeline.config import WAVELENGTHS_NM, load_runtime_config
from ppg_pipeline.features import compute_absorbance
from ppg_pipeline.io import _parse_line, follow_raw_stream, parse_stdin_stream
from ppg_pipeline.models import RawSample
from ppg_pipeline.stats import mean_std_cv, trimmed_mean

# ========================= ANSI colors =========================
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
CLEAR_SCREEN = "\033[2J\033[H"

# ========================= Thresholds =========================
MAX_CV = 0.20
MAX_SATURATION_RATIO = 0.05
MIN_DYNAMIC_RANGE = 2.0          # detach if range <= this
MIN_INTENSITY = 50.0             # suspicious if mean below this
MAX_ABSORBANCE = 5.0             # physiologically unreasonable
MIN_ABSORBANCE = -1.0


def color_status(ok: bool, text: str) -> str:
    return f"{GREEN}{text}{RESET}" if ok else f"{RED}{text}{RESET}"


def color_warn(text: str) -> str:
    return f"{YELLOW}{text}{RESET}"


def format_float(value: float, width: int = 10) -> str:
    if not math.isfinite(value):
        return "N/A".rjust(width)
    return f"{value:.2f}".rjust(width)


class PreflightChecker:
    def __init__(
        self,
        runtime_config: Mapping[str, object],
        calibration: Optional[Mapping[str, object]] = None,
        window_seconds: float = 3.0,
    ):
        self.runtime_config = runtime_config
        self.calibration = calibration
        self.sample_rate_hz = int(runtime_config["sample_rate_hz"])
        self.window_size = int(self.sample_rate_hz * window_seconds)
        self.buffer: List[RawSample] = []
        self.window_count = 0
        self.all_pass_count = 0

    def add_sample(self, sample: RawSample) -> None:
        self.buffer.append(sample)
        if len(self.buffer) >= self.window_size:
            self._evaluate_window(self.buffer[:self.window_size])
            del self.buffer[:self.window_size]

    def _evaluate_window(self, window: Sequence[RawSample]) -> None:
        self.window_count += 1
        results: Dict[int, Dict[str, object]] = {}
        all_ok = True

        for wl in WAVELENGTHS_NM:
            values = [s.raw_by_wavelength.get(wl, 0.0) for s in window]
            mu, std, cv = mean_std_cv(values)
            tm = trimmed_mean(values, trim_ratio=0.05)
            sat = estimate_saturation_ratio(values, self.runtime_config["parser"].get("adc_clip_value"))
            dyn_range = max(values) - min(values) if values else 0.0
            detached = dyn_range <= MIN_DYNAMIC_RANGE and len(values) >= 3

            issues: List[str] = []
            if mu <= 0:
                issues.append("non-positive")
            elif mu < MIN_INTENSITY:
                issues.append("low signal")
            if cv > MAX_CV:
                issues.append(f"CV={cv:.2f}")
            if sat > MAX_SATURATION_RATIO:
                issues.append(f"saturated({sat:.1%})")
            if detached:
                issues.append("DETACHED")

            ok = not issues
            if not ok:
                all_ok = False

            results[wl] = {
                "mean": mu,
                "std": std,
                "cv": cv,
                "trimmed_mean": tm,
                "saturation": sat,
                "dynamic_range": dyn_range,
                "detached": detached,
                "issues": issues,
                "ok": ok,
            }

        # Absorbance check (only if calibration is available)
        absorbance_results: Dict[int, float] = {}
        if self.calibration:
            try:
                intensities = {wl: results[wl]["trimmed_mean"] for wl in WAVELENGTHS_NM}
                i0_star = self.calibration.get("i0_star", {})
                absorbance_results = compute_absorbance(intensities, i0_star, eps=1.0)
                for wl, a_val in absorbance_results.items():
                    if math.isfinite(a_val):
                        if a_val > MAX_ABSORBANCE:
                            results[wl]["issues"].append(f"A={a_val:.2f} HIGH")
                            results[wl]["ok"] = False
                            all_ok = False
                        elif a_val < MIN_ABSORBANCE:
                            results[wl]["issues"].append(f"A={a_val:.2f} NEG")
                            results[wl]["ok"] = False
                            all_ok = False
                    else:
                        results[wl]["issues"].append("A=NaN")
                        results[wl]["ok"] = False
                        all_ok = False
            except Exception:
                pass

        if all_ok:
            self.all_pass_count += 1

        self._print_report(results, absorbance_results, all_ok)

    def _print_report(
        self,
        results: Dict[int, Dict[str, object]],
        absorbance: Dict[int, float],
        all_ok: bool,
    ) -> None:
        sys.stderr.write(CLEAR_SCREEN)
        sys.stderr.write(f"{BOLD}=== PPG Pre-flight Check  [window #{self.window_count}] ==={RESET}\n\n")

        # Header
        header = f"{'Channel':>10} {'Mean':>10} {'TrimMean':>10} {'Std':>10} {'CV':>10} {'Sat%':>8} {'DynRng':>10}"
        if absorbance:
            header += f" {'Absorb':>10}"
        header += f"  {'Status'}"
        sys.stderr.write(f"{DIM}{header}{RESET}\n")
        sys.stderr.write(f"{DIM}{'-' * len(header)}{'-' * 10}{RESET}\n")

        for wl in WAVELENGTHS_NM:
            r = results[wl]
            ok = r["ok"]
            status_str = color_status(ok, "PASS") if ok else color_status(False, "FAIL")
            issues_str = f"  {RED}{', '.join(r['issues'])}{RESET}" if r["issues"] else ""

            line = (
                f"{wl:>7} nm"
                f" {format_float(r['mean'])}"
                f" {format_float(r['trimmed_mean'])}"
                f" {format_float(r['std'])}"
                f" {format_float(r['cv'])}"
                f" {r['saturation']:>7.1%}"
                f" {format_float(r['dynamic_range'])}"
            )
            if absorbance:
                a_val = absorbance.get(wl, float("nan"))
                line += f" {format_float(a_val)}"
            line += f"  {status_str}{issues_str}"
            sys.stderr.write(line + "\n")

        # Overall
        sys.stderr.write("\n")
        overall = color_status(all_ok, "ALL PASS") if all_ok else color_status(False, "ISSUES DETECTED")
        sys.stderr.write(f"{BOLD}Overall: {overall}{RESET}")
        sys.stderr.write(f"   ({self.all_pass_count}/{self.window_count} windows passed)\n")

        if not self.calibration:
            sys.stderr.write(f"\n{DIM}Tip: provide --calibration to also check absorbance values{RESET}\n")

        sys.stderr.write(f"\n{DIM}Ctrl+C to stop.{RESET}\n")
        sys.stderr.flush()


def load_calibration_if_available(path: Optional[str], runtime_config: Mapping[str, object]):
    if not path:
        return None
    import json
    from pathlib import Path as P
    p = P(path)
    if not p.exists():
        sys.stderr.write(f"{YELLOW}Warning: calibration file not found: {path}{RESET}\n")
        return None
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PPG pre-flight optical quality check.")
    parser.add_argument("--input", default=None, help="Raw CSV file path (alternative to stdin).")
    parser.add_argument("--follow", action="store_true", help="Tail a growing CSV file.")
    parser.add_argument("--calibration", default=None, help="white_card_calibration.json for absorbance check.")
    parser.add_argument("--config", default=None, help="Optional runtime config JSON override.")
    parser.add_argument("--window-seconds", type=float, default=3.0, help="Window duration in seconds (default 3).")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runtime_config = load_runtime_config(args.config)
    calibration = load_calibration_if_available(args.calibration, runtime_config)
    checker = PreflightChecker(runtime_config, calibration, window_seconds=args.window_seconds)

    sys.stderr.write(f"{BOLD}PPG Pre-flight Check — waiting for data...{RESET}\n")
    if calibration:
        sys.stderr.write(f"{GREEN}Calibration loaded. Absorbance checking enabled.{RESET}\n")
    else:
        sys.stderr.write(f"{YELLOW}No calibration. Raw signal quality only.{RESET}\n")
    sys.stderr.flush()

    try:
        if args.input:
            if args.follow:
                for sample in follow_raw_stream(args.input, runtime_config, idle_timeout=60.0, poll_interval=0.1):
                    checker.add_sample(sample)
            else:
                from ppg_pipeline.io import parse_raw_stream
                samples = parse_raw_stream(args.input, runtime_config)
                for sample in samples:
                    checker.add_sample(sample)
        else:
            # Read from stdin (pipe from zig_bt_client --stdout)
            for sample in parse_stdin_stream(runtime_config):
                checker.add_sample(sample)
    except KeyboardInterrupt:
        pass

    sys.stderr.write(f"\n{BOLD}Done. {checker.all_pass_count}/{checker.window_count} windows passed.{RESET}\n")
    return 0 if checker.window_count > 0 and checker.all_pass_count == checker.window_count else 1


if __name__ == "__main__":
    sys.exit(main())
