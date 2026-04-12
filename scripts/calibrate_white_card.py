#!/usr/bin/env python3
"""Build white-card calibration from one or more raw CSV runs.

Usage:
  python3 scripts/calibrate_white_card.py --input white1.csv white2.csv white3.csv --output calibration/white_card_calibration.json
  python3 scripts/calibrate_white_card.py --input-dir calibration/white_card_runs/ --output calibration/white_card_calibration.json
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ppg_pipeline.calibration import run_white_card_calibration
from ppg_pipeline.config import load_runtime_config
from ppg_pipeline.io import save_json, save_white_card_calibration
from ppg_pipeline.lookup_table import build_photodiode_current_lookup_table
from ppg_pipeline.normalization import build_relative_lookup_table


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build white-card calibration from one or more raw CSV runs.")
    parser.add_argument("--input", nargs="*", default=[], help="One or more white-card raw CSV files.")
    parser.add_argument("--input-dir", help="Directory containing white-card CSV runs.", default=None)
    parser.add_argument("--output", required=True, help="Calibration JSON output path.")
    parser.add_argument(
        "--lookup-output",
        default=None,
        help="Optional relative lookup table JSON path. Defaults beside calibration JSON.",
    )
    parser.add_argument(
        "--current-lookup-output",
        default=None,
        help="Optional paper-style photodiode current lookup JSON path. Defaults beside calibration JSON.",
    )
    parser.add_argument("--config", default=None, help="Optional runtime config JSON override.")
    parser.add_argument("--trim-ratio", type=float, default=0.05, help="Trim ratio for robust mean.")
    parser.add_argument("--run-duration-sec", type=int, default=30, help="Expected run duration in seconds.")
    parser.add_argument("--min-runs", type=int, default=3, help="Minimum accepted white-card runs.")
    parser.add_argument("--log-level", default="INFO", help="Logging level.")
    return parser.parse_args()


def collect_input_paths(args: argparse.Namespace) -> list:
    paths = [Path(path) for path in args.input]
    if args.input_dir:
        paths.extend(sorted(Path(args.input_dir).glob("*.csv")))
    deduped = []
    seen = set()
    for path in paths:
        resolved = str(path)
        if resolved not in seen:
            seen.add(resolved)
            deduped.append(resolved)
    return deduped


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    input_paths = collect_input_paths(args)
    if not input_paths:
        logging.error("No white-card input CSV files were provided.")
        return 2

    runtime_config = load_runtime_config(args.config)
    calibration = run_white_card_calibration(
        input_paths=input_paths,
        runtime_config=runtime_config,
        trim_ratio=args.trim_ratio,
        min_runs=args.min_runs,
        run_duration_sec=args.run_duration_sec,
    )
    save_white_card_calibration(calibration, args.output)
    if calibration["accepted"]:
        relative_lookup_output = args.lookup_output or str(Path(args.output).with_name("photodiode_relative_lookup_table.json"))
        relative_lookup_table = build_relative_lookup_table(calibration, reference_wavelength_nm=810)
        save_json(relative_lookup_table, relative_lookup_output)
        logging.info("Saved relative lookup table to %s", relative_lookup_output)

        current_lookup_output = args.current_lookup_output or str(Path(args.output).with_name("photodiode_current_lookup_table.json"))
        current_lookup_table = build_photodiode_current_lookup_table(calibration, runtime_config)
        save_json(current_lookup_table, current_lookup_output)
        logging.info("Saved photodiode current lookup table to %s", current_lookup_output)

    logging.info("Saved calibration to %s", args.output)
    if not calibration["accepted"]:
        logging.error("Calibration was rejected: %s", calibration["invalid_reasons"])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
