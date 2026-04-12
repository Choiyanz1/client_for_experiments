#!/usr/bin/env python3
"""
Running Experiment IMU Label GUI – standalone data collection tool.
Separate from the gym/fitness data collection (imu_label_gui.py).

Records:
  - Stage number (1-9, via number keys)
  - Speed/acceleration level (stage 3+, via 'a'/'d' keys)

Data columns (segmented files):
  pc_time, serial_num, sensor_ts, host_ts,
  ax, ay, az, gx, gy, gz, mx, my, mz,
  ppg_a, ppg_b, ppg_c, ppg_d, ppg_e

Folder structure:
  data_running/{subject}/stage{N}/data_{ts}.csv           (stage 1-2)
  data_running/{subject}/stage{N}/speed{M}/data_{ts}.csv  (stage 3+)

Usage (via pipe or stdin):
  zig_bt_client | python3 imu_running_gui.py
  python3 imu_running_gui.py --input /path/to/raw.csv
"""

import argparse
import collections
import csv
import os
import sys
import threading
import time
from datetime import datetime
import glob

import numpy as np
import imufusion

# Project root = parent of tools/
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
DATA_DIR = os.path.join(_PROJECT_ROOT, "data_running")

# ========================= Config =========================
# Headers for segmented files (only sensor data, labels encoded in folder path)
CSV_HEADERS = [
    "pc_time",
    "serial_num",
    "sensor_ts",
    "host_ts",
    "ax", "ay", "az",
    "gx", "gy", "gz",
    "mx", "my", "mz",
    "ppg_a", "ppg_b", "ppg_c", "ppg_d", "ppg_e",
]

# Headers for whole session file (sensor data + labels)
CSV_HEADERS_SESSION = [
    "pc_time",
    "serial_num",
    "sensor_ts",
    "host_ts",
    "ax", "ay", "az",
    "gx", "gy", "gz",
    "mx", "my", "mz",
    "ppg_a", "ppg_b", "ppg_c", "ppg_d", "ppg_e",
    "stage",
    "speed_level",
    "incline",
    "subject_id",
]

# ========================= Label state =========================
LABEL_DEFAULTS = {
    "subject_id": "",
    "stage": 0,
    "speed_level": 0.0,
    "incline": 0,  # 0 = no incline, >=1 = incline level
}
label_state = LABEL_DEFAULTS.copy()

label_lock = threading.Lock()
csv_lock = threading.Lock()

csv_files = {}  # key -> (file_handle, csv_writer, path, row_count)
whole_session_file = None  # (file_handle, csv_writer, path, row_count)
is_recording = False
recording_lock = threading.Lock()

# Latest IMU data for live preview
latest_imu_data = None
latest_imu_lock = threading.Lock()

# Ring buffers for waveform (store last N samples)
WAVEFORM_LEN = 200

def _make_xyz_buf():
    return {"x": collections.deque(maxlen=WAVEFORM_LEN),
            "y": collections.deque(maxlen=WAVEFORM_LEN),
            "z": collections.deque(maxlen=WAVEFORM_LEN)}

waveform_accel = _make_xyz_buf()
waveform_gyro  = _make_xyz_buf()
waveform_mag   = _make_xyz_buf()
# Fusion-derived waveforms
waveform_lin_accel   = _make_xyz_buf()
waveform_earth_accel = _make_xyz_buf()
waveform_euler       = _make_xyz_buf()
# PPG waveform buffers
waveform_ppg_a = {"x": collections.deque(maxlen=WAVEFORM_LEN),
                  "y": collections.deque(maxlen=WAVEFORM_LEN),
                  "z": collections.deque(maxlen=WAVEFORM_LEN)}
waveform_ppg_b = {"x": collections.deque(maxlen=WAVEFORM_LEN),
                  "y": collections.deque(maxlen=WAVEFORM_LEN),
                  "z": collections.deque(maxlen=WAVEFORM_LEN)}
waveform_ppg_c = {"x": collections.deque(maxlen=WAVEFORM_LEN),
                  "y": collections.deque(maxlen=WAVEFORM_LEN),
                  "z": collections.deque(maxlen=WAVEFORM_LEN)}
waveform_ppg_d = {"x": collections.deque(maxlen=WAVEFORM_LEN),
                  "y": collections.deque(maxlen=WAVEFORM_LEN),
                  "z": collections.deque(maxlen=WAVEFORM_LEN)}
waveform_ppg_e = {"x": collections.deque(maxlen=WAVEFORM_LEN),
                  "y": collections.deque(maxlen=WAVEFORM_LEN),
                  "z": collections.deque(maxlen=WAVEFORM_LEN)}
waveform_lock = threading.Lock()

# ========================= Fusion (imufusion) =========================
SAMPLE_RATE = 100  # approximate Hz

fusion_offset = imufusion.Offset(SAMPLE_RATE)
fusion_ahrs = imufusion.Ahrs()
fusion_ahrs.settings = imufusion.Settings(
    imufusion.CONVENTION_NWU,
    0.5,        # gain
    2000,       # gyroscope_range (dps)
    10,         # acceleration_rejection (deg)
    30,         # magnetic_rejection (deg)
    5 * SAMPLE_RATE,  # recovery_trigger_period (samples)
)
fusion_lock = threading.Lock()
prev_sensor_ts = None


def get_label_snapshot():
    with label_lock:
        return label_state.copy()


def update_label_state(**kwargs):
    with label_lock:
        label_state.update(kwargs)


def reset_label_state():
    with label_lock:
        label_state.clear()
        label_state.update(LABEL_DEFAULTS)


# ========================= Helpers =========================
def _auto_next_subject_id(data_dir):
    """Scan data directory for existing subject folders and return next numeric ID."""
    if not os.path.isdir(data_dir):
        return "1000"
    existing = []
    for name in os.listdir(data_dir):
        if os.path.isdir(os.path.join(data_dir, name)):
            try:
                existing.append(int(name))
            except ValueError:
                pass
    if not existing:
        return "1000"
    return str(max(existing) + 1)


def _find_latest_session_file(data_dir, subject_id):
    """Find the latest running_session CSV for a subject (for resume)."""
    subj_dir = os.path.join(data_dir, subject_id)
    if not os.path.isdir(subj_dir):
        return None
    pattern = os.path.join(subj_dir, f"{subject_id}_running_session_*.csv")
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None


def _read_last_row_state(csv_path, label_cols):
    """Read the last data row of a session CSV and extract label state.
    Uses seek to efficiently read only the tail of large files."""
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            header_line = f.readline().strip()
            header = [h.strip() for h in header_line.split(',')]
            f.seek(0, 2)
            fsize = f.tell()
            f.seek(max(0, fsize - 8192))
            tail = f.read()
            lines = tail.strip().split('\n')
            last_line = None
            for line in reversed(lines):
                stripped = line.strip()
                if stripped and stripped != header_line:
                    last_line = stripped
                    break
            if not last_line:
                return None
            values = [v.strip() for v in last_line.split(',')]
            if len(values) < len(header):
                return None
            data = dict(zip(header, values))
            result = {}
            for col, default_val in label_cols.items():
                raw = data.get(col, "")
                try:
                    result[col] = type(default_val)(raw)
                except (ValueError, TypeError):
                    result[col] = default_val
            return result
    except Exception as e:
        print(f"[WARN] Could not read state from {csv_path}: {e}")
        return None


# ========================= CSV Storage =========================
def _get_csv_key(subject_id, stage, speed_level, incline):
    """Generate unique key for CSV file based on labels."""
    return (subject_id, stage, speed_level, incline)


def _get_csv_path(subject_id, stage, speed_level, incline):
    """Generate file path based on labels.
    Structure:
    - Stage 1,2,4,5: data_running/{subject}/stage{N}/data_{timestamp}.csv
    - Stage 3 (speed): data_running/{subject}/stage3/speed{M}/data_{ts}.csv
    - Stage 3 (incline): data_running/{subject}/stage3/incline{L}/data_{ts}.csv
    """
    ts = datetime.now().strftime('%H%M%S')

    if stage == 3:
        if incline > 0:
            base_dir = os.path.join(DATA_DIR, subject_id, f"stage{stage}", f"incline{incline}")
        elif speed_level > 0:
            # Format speed: 8.0 -> "8.0", 8.5 -> "8.5"
            speed_str = f"{speed_level:.1f}"
            base_dir = os.path.join(DATA_DIR, subject_id, f"stage{stage}", f"speed{speed_str}")
        else:
            base_dir = os.path.join(DATA_DIR, subject_id, f"stage{stage}")
    else:
        base_dir = os.path.join(DATA_DIR, subject_id, f"stage{stage}")
    os.makedirs(base_dir, exist_ok=True)
    filename = f"data_{ts}.csv"
    return os.path.join(base_dir, filename)


def _open_whole_session_file(subject_id, resume_path=None):
    """Open a single CSV file for the entire subject session.
    If resume_path is given, append to existing file instead of creating new."""
    if resume_path and os.path.exists(resume_path):
        with open(resume_path, 'r', encoding='utf-8') as tmp:
            existing_rows = sum(1 for _ in tmp) - 1
        f = open(resume_path, "a", newline="", encoding="utf-8")
        writer = csv.writer(f)
        f.flush()
        print(f"[INFO] Resumed whole session file: {resume_path} ({existing_rows} existing rows)")
        return (f, writer, resume_path, existing_rows)
    base_dir = os.path.join(DATA_DIR, subject_id)
    os.makedirs(base_dir, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{subject_id}_running_session_{ts}.csv"
    path = os.path.join(base_dir, filename)
    f = open(path, "w", newline="", encoding="utf-8")
    writer = csv.writer(f)
    writer.writerow(CSV_HEADERS_SESSION)
    f.flush()
    print(f"[INFO] Created whole session file: {path}")
    return (f, writer, path, 0)


def _open_csv_file(subject_id, stage, speed_level, incline):
    """Open a new CSV file for the given label combination."""
    key = _get_csv_key(subject_id, stage, speed_level, incline)
    path = _get_csv_path(subject_id, stage, speed_level, incline)
    f = open(path, "w", newline="", encoding="utf-8")
    writer = csv.writer(f)
    writer.writerow(CSV_HEADERS)
    f.flush()
    return key, (f, writer, path, 0)


def close_all_csv():
    """Close all open CSV files including whole session file."""
    global whole_session_file
    with csv_lock:
        for key, (f, writer, path, count) in csv_files.items():
            try:
                f.close()
                print(f"[INFO] Closed: {path} ({count} rows)")
            except Exception as e:
                print(f"[WARN] Error closing file: {e}", file=sys.stderr)
        csv_files.clear()

        if whole_session_file:
            try:
                f, writer, path, count = whole_session_file
                f.close()
                print(f"[INFO] Closed whole session file: {path} ({count} rows)")
            except Exception as e:
                print(f"[WARN] Error closing whole session file: {e}", file=sys.stderr)
            whole_session_file = None


def write_csv_row(serial_num, sensor_ts, host_ts, ppg_values_list, ax, ay, az, gx, gy, gz, mx, my, mz):
    """Write a single aligned row to CSV."""
    with recording_lock:
        if not is_recording:
            return

    labels = get_label_snapshot()
    pc_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

    subject_id = labels["subject_id"]
    stage = labels["stage"]
    speed_level = labels["speed_level"]
    incline = labels["incline"]

    if not subject_id:
        return

    with csv_lock:
        global whole_session_file
        if whole_session_file is None:
            whole_session_file = _open_whole_session_file(subject_id)

        # Write to whole session file (with labels)
        if whole_session_file:
            f_ws, writer_ws, path_ws, count_ws = whole_session_file
            ppg5 = ppg_values_list if len(ppg_values_list) == 5 else (ppg_values_list + [0.0] * 5)[:5]
            row_session = [
                pc_time, serial_num, sensor_ts, host_ts,
                ax, ay, az, gx, gy, gz, mx, my, mz,
                *ppg5,
                stage, speed_level, incline, subject_id,
            ]
            writer_ws.writerow(row_session)
            f_ws.flush()
            whole_session_file = (f_ws, writer_ws, path_ws, count_ws + 1)

        # Get or create segmented file
        key = _get_csv_key(subject_id, stage, speed_level, incline)

        if key not in csv_files:
            key, file_tuple = _open_csv_file(subject_id, stage, speed_level, incline)
            csv_files[key] = file_tuple
            print(f"[INFO] Created new file: {file_tuple[2]}")

        f, writer, path, count = csv_files[key]

        row = [
            pc_time, serial_num, sensor_ts, host_ts,
            ax, ay, az, gx, gy, gz, mx, my, mz,
            *ppg_values_list,
        ]
        writer.writerow(row)
        f.flush()
        csv_files[key] = (f, writer, path, count + 1)


# ========================= Data Alignment (PPG ↔ IMU linear interpolation) =====
PPG_HISTORY_MAX = 200
ppg_history = collections.deque(maxlen=PPG_HISTORY_MAX)
ppg_history_lock = threading.Lock()


def interpolate_ppg(target_ts):
    """Linearly interpolate 5-channel PPG values at *target_ts*."""
    with ppg_history_lock:
        n = len(ppg_history)
        if n == 0:
            return [0.0] * 5
        if n == 1:
            return list(ppg_history[0][1])

        before = None
        after = None
        for ts, vals in ppg_history:
            if ts <= target_ts:
                before = (ts, vals)
            if ts >= target_ts and after is None:
                after = (ts, vals)

        if before is None and after is not None:
            return list(after[1])
        if after is None and before is not None:
            return list(before[1])
        if before is None and after is None:
            return [0.0] * 5
        if before[0] == after[0]:
            return list(before[1])

        t_range = after[0] - before[0]
        t_frac = (target_ts - before[0]) / t_range
        return [b + (a - b) * t_frac for b, a in zip(before[1], after[1])]


def parse_and_process_line(line):
    """
    Parse a raw CSV line from zig_bt_client.
    Format: serial, type, ts, host_ts, ppg_a..e, ax, ay, az, gx, gy, gz, mx, my, mz (18 cols)
    PPG rows  → store in history, update PPG waveforms only.
    IMU rows  → interpolate PPG at IMU timestamp, update IMU waveforms, write CSV.
    """
    if not line or line.startswith("serial_num"):
        return None

    cols = [c.strip() for c in line.split(",")]
    n = len(cols)

    if n < 14:
        return None

    try:
        serial_num = cols[0]
        sensor_type = cols[1] if n > 1 else ""
        sensor_ts = cols[2]
        host_ts = int(cols[3])

        ppg_values = [float(cols[4 + i]) if 4 + i < n else 0.0 for i in range(5)]

        imu_start = 9
        if n >= imu_start + 6:
            ax, ay, az = float(cols[imu_start]), float(cols[imu_start + 1]), float(cols[imu_start + 2])
            gx, gy, gz = float(cols[imu_start + 3]), float(cols[imu_start + 4]), float(cols[imu_start + 5])
            if n >= imu_start + 9:
                mx, my, mz = float(cols[imu_start + 6]), float(cols[imu_start + 7]), float(cols[imu_start + 8])
            else:
                mx = my = mz = 0.0
        else:
            ax = ay = az = gx = gy = gz = mx = my = mz = 0.0

    except (ValueError, IndexError):
        return None

    if sensor_type == "PPG":
        is_valid = any(v != 0 for v in ppg_values)
        if is_valid:
            with ppg_history_lock:
                ppg_history.append((host_ts, ppg_values))
        with waveform_lock:
            waveform_ppg_a["x"].append(ppg_values[0])
            waveform_ppg_b["x"].append(ppg_values[1] if len(ppg_values) > 1 else 0.0)
            waveform_ppg_c["x"].append(ppg_values[2] if len(ppg_values) > 2 else 0.0)
            waveform_ppg_d["x"].append(ppg_values[3] if len(ppg_values) > 3 else 0.0)
            waveform_ppg_e["x"].append(ppg_values[4] if len(ppg_values) > 4 else 0.0)
        return None

    elif sensor_type == "IMU":
        is_valid = (ax != 0 or ay != 0 or az != 0 or gx != 0 or gy != 0 or gz != 0)
        if not is_valid:
            return None

        interp_ppg = interpolate_ppg(host_ts)

        with waveform_lock:
            waveform_accel["x"].append(ax); waveform_accel["y"].append(ay); waveform_accel["z"].append(az)
            waveform_gyro["x"].append(gx);  waveform_gyro["y"].append(gy);  waveform_gyro["z"].append(gz)
            waveform_mag["x"].append(mx);   waveform_mag["y"].append(my);   waveform_mag["z"].append(mz)

        with latest_imu_lock:
            global latest_imu_data
            latest_imu_data = (ax, ay, az, gx, gy, gz, mx, my, mz)

        # Fusion
        global prev_sensor_ts
        with fusion_lock:
            try:
                ts_val = float(sensor_ts)
            except (ValueError, TypeError):
                ts_val = None

            if ts_val is not None and prev_sensor_ts is not None:
                dt = (ts_val - prev_sensor_ts) / 1000.0
                if 0 < dt < 0.5:
                    gyro_off = fusion_offset.update(imufusion.vector(gx, gy, gz))
                    fusion_ahrs.update(gyro_off,
                                       imufusion.vector(ax, ay, az),
                                       imufusion.vector(mx, my, mz),
                                       dt)
                    la = fusion_ahrs.linear_acceleration
                    ea = fusion_ahrs.earth_acceleration
                    euler = fusion_ahrs.quaternion.to_euler()
                    with waveform_lock:
                        waveform_lin_accel["x"].append(la.x)
                        waveform_lin_accel["y"].append(la.y)
                        waveform_lin_accel["z"].append(la.z)
                        waveform_earth_accel["x"].append(ea.x)
                        waveform_earth_accel["y"].append(ea.y)
                        waveform_earth_accel["z"].append(ea.z)
                        waveform_euler["x"].append(euler.x)
                        waveform_euler["y"].append(euler.y)
                        waveform_euler["z"].append(euler.z)

            if ts_val is not None:
                prev_sensor_ts = ts_val

        write_csv_row(serial_num, sensor_ts, host_ts, interp_ppg,
                      ax, ay, az, gx, gy, gz, mx, my, mz)
        return None

    else:
        ppg_for_write = ppg_values if any(ppg_values) else [0.0] * 5
        write_csv_row(serial_num, sensor_ts, host_ts, ppg_for_write,
                      ax, ay, az, gx, gy, gz, mx, my, mz)
        return None


# ========================= Data reader =========================
def stdin_reader():
    """Read IMU lines from stdin (pipe) line-by-line."""
    for line in sys.stdin:
        raw = line.rstrip("\r\n")
        parse_and_process_line(raw)


def file_reader(path):
    """Tail a growing file and process rows (fallback)."""
    for _ in range(300):
        if os.path.exists(path):
            break
        time.sleep(0.1)

    with open(path, "r") as f:
        buf = ""
        while True:
            chunk = f.read(8192)
            if not chunk:
                time.sleep(0.05)
                continue
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                raw = line.rstrip("\r")
                parse_and_process_line(raw)

# Legacy tkinter GUI has been removed. This module provides backend-only
# logic for `tools/imu_running_gui.py`.
