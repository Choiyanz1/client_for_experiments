#!/usr/bin/env python3
"""
IMU Label GUI – standalone labelling tool.
Reads raw CSV from stdin (pipe) or file, shows a labelling panel,
and saves labelled IMU rows to CSV.

Usage (via zig build label):
  The build system pipes BLE data directly to this GUI via stdin.
"""

import argparse
import collections
import csv
import os
import sys
import threading
from datetime import datetime
import glob

import numpy as np
import imufusion

# Project root = parent of tools/
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
DATA_DIR = os.path.join(_PROJECT_ROOT, "data_workout")

# ========================= Config =========================
# Headers for full session file (all fields including phase)
CSV_HEADERS_FULL = [
    "pc_time",
    "serial_num",
    "sensor_ts",
    "host_ts",
    "ax", "ay", "az",
    "gx", "gy", "gz",
    "mx", "my", "mz",
    "action_type",
    "phase",
    "rep",
    "set",
    "rpe",
    "weight_kg",
    "subject_id",
    "ppg_a", "ppg_b", "ppg_c", "ppg_d", "ppg_e",
]

# Headers for segmented files (phase in row data, not in filename; no fusion)
CSV_HEADERS_SEGMENTED = [
    "pc_time",
    "serial_num",
    "sensor_ts",
    "host_ts",
    "ax", "ay", "az",
    "gx", "gy", "gz",
    "mx", "my", "mz",
    "phase",  # phase in row, not filename
    "rpe",
    "weight_kg",
    "ppg_a", "ppg_b", "ppg_c", "ppg_d", "ppg_e",
]

# ========================= Label state =========================
LABEL_DEFAULTS = {
    "subject_id": "",
    "weight_kg": 0.0,
    "action_type": "none",
    "phase": "none",
    "rep": 0,
    "set": 0,
    "rpe": 0,  # default is 0, valid values are 0-10
}
label_state = LABEL_DEFAULTS.copy()

# Rep/Set auto-increment tracking
rep_phase_tracker = {"concentric": False, "eccentric": False}  # Track if both phases completed for current rep

# Track inter-set rest state (toggle behavior)
inter_set_rest_active = False
inter_set_rest_lock = threading.Lock()

# Track action before big rest (to restore when big rest ends)
action_before_big_rest = None
big_rest_count = 0  # Counter to distinguish different big rest sessions
big_rest_lock = threading.Lock()

# Track files for current set (for RPE retroactive update)
current_set_file_paths = set()  # Set of file paths for the current set
last_set_file_paths = set()  # Set of file paths for the previous set
pending_rpe_for_last_set = None  # RPE value to apply to previous set when rest ends
set_files_lock = threading.Lock()

label_lock = threading.Lock()
csv_lock = threading.Lock()

# Whole-session RPE backfill is derived from segmented files at session end.
# This is crash-safe: even if the process dies mid-session, restarting and
# ending the session can still backfill whole-session RPE from on-disk segmented.

csv_files = {}  # key -> (file_handle, csv_writer, path, row_count)
whole_session_file = None  # (file_handle, csv_writer, path, row_count) for entire subject session
is_recording = False
recording_lock = threading.Lock()

# Latest IMU data for live preview
latest_imu_data = None  # (ax, ay, az, gx, gy, gz, mx, my, mz)
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
waveform_lin_accel  = _make_xyz_buf()   # linear acceleration (gravity removed, sensor frame)
waveform_earth_accel = _make_xyz_buf()  # earth-frame acceleration (gravity removed)
waveform_euler = _make_xyz_buf()        # roll/pitch/yaw (stored as x/y/z)
# PPG waveform buffers (ADPD4101 optical data) - 5 separate channels
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

# ========================= Fusion (xio Fusion) =========================
SAMPLE_RATE = 100  # approximate Hz

fusion_offset = imufusion.Offset(SAMPLE_RATE)
fusion_ahrs = imufusion.Ahrs()
fusion_ahrs.settings = imufusion.Settings(
    imufusion.CONVENTION_NWU,  # convention
    0.5,                       # gain
    2000,                      # gyroscope_range (dps)
    10,                        # acceleration_rejection (deg)
    30,                        # magnetic_rejection (deg) - increased to reduce drift from interference
    5 * SAMPLE_RATE,           # recovery_trigger_period (samples)
)
fusion_lock = threading.Lock()
prev_sensor_ts = None  # for delta_time


def check_and_auto_increment_rep():
    """Check if both concentric and eccentric completed, then auto-increment rep."""
    global rep_phase_tracker
    if rep_phase_tracker["concentric"] and rep_phase_tracker["eccentric"]:
        # Both phases completed, increment rep
        with label_lock:
            label_state["rep"] += 1
            current_rep = label_state["rep"]
            current_set = label_state["set"]
        print(f"[AUTO] Rep incremented to {current_rep} (Set {current_set})")
        # Reset tracker for next rep
        rep_phase_tracker = {"concentric": False, "eccentric": False}
        
        return True
    return False


def get_label_snapshot():
    with label_lock:
        return label_state.copy()


def update_label_state(**kwargs):
    with label_lock:
        label_state.update(kwargs)


def increment_label_state(key, step=1):
    with label_lock:
        label_state[key] += step


def update_rpe_in_files(file_paths, new_rpe):
    """Update RPE value in all rows of the given CSV files."""
    for path in file_paths:
        if not os.path.exists(path):
            continue
        try:
            # Read all rows
            rows = []
            with open(path, 'r', newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header is None:
                    continue
                # Find rpe column index
                try:
                    rpe_idx = header.index('rpe')
                except ValueError:
                    continue
                rows.append(header)
                for row in reader:
                    if len(row) > rpe_idx:
                        row[rpe_idx] = str(new_rpe)
                    rows.append(row)
            
            # Write back
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerows(rows)
            print(f"[RPE] Updated {len(rows)-1} rows in {path} to RPE {new_rpe}")
        except Exception as e:
            print(f"[WARN] Error updating RPE in {path}: {e}", file=sys.stderr)


def _extract_action_set_from_segmented_path(path):
    """Extract (action_type, set_idx) from segmented CSV path.
    Expected path pattern includes .../<action_type>/set{N}/rep*.csv"""
    parts = os.path.normpath(path).split(os.sep)
    for i, part in enumerate(parts):
        if part.startswith("set") and part[3:].isdigit() and i > 0:
            return parts[i - 1], int(part[3:])
    return None


def update_rpe_in_whole_session_by_segmented_paths(subject_id, file_paths, new_rpe):
    """Retroactively update RPE in whole_session CSV for matching action/set pairs.
    Matching pairs are inferred from segmented file paths of the last completed set."""
    target_pairs = set()
    for path in file_paths:
        parsed = _extract_action_set_from_segmented_path(path)
        if parsed is not None:
            target_pairs.add(parsed)

    if not target_pairs:
        return

    with csv_lock:
        global whole_session_file

        target_path = None
        had_open_target = False

        if whole_session_file:
            f_ws, writer_ws, path_ws, count_ws = whole_session_file
            parent_subject = os.path.basename(os.path.dirname(path_ws))
            if parent_subject == str(subject_id):
                target_path = path_ws
                try:
                    f_ws.close()
                except Exception:
                    pass
                whole_session_file = None
                had_open_target = True

        if target_path is None:
            target_path = _find_latest_session_file(DATA_DIR, subject_id)

        if not target_path or not os.path.exists(target_path):
            return

        try:
            rows = []
            with open(target_path, 'r', newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header is None:
                    return

                try:
                    rpe_idx = header.index('rpe')
                    action_idx = header.index('action_type')
                    set_idx = header.index('set')
                except ValueError:
                    return

                rows.append(header)
                updated_rows = 0
                for row in reader:
                    if len(row) > max(rpe_idx, action_idx, set_idx):
                        action_val = row[action_idx]
                        try:
                            set_val = int(row[set_idx])
                        except ValueError:
                            set_val = None
                        if set_val is not None and (action_val, set_val) in target_pairs:
                            row[rpe_idx] = str(new_rpe)
                            updated_rows += 1
                    rows.append(row)

            with open(target_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerows(rows)

            if updated_rows > 0:
                print(f"[RPE] Updated {updated_rows} rows in whole session {target_path} to RPE {new_rpe}")
        except Exception as e:
            print(f"[WARN] Error updating RPE in whole session {target_path}: {e}", file=sys.stderr)

        if had_open_target:
            whole_session_file = _open_whole_session_file(subject_id, resume_path=target_path)


def reset_label_state():
    global rep_phase_tracker, pending_rpe_for_last_set, big_rest_count
    with label_lock:
        label_state.clear()
        label_state.update(LABEL_DEFAULTS)
    rep_phase_tracker = {"concentric": False, "eccentric": False}
    pending_rpe_for_last_set = None
    big_rest_count = 0


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
    """Find the latest whole_session CSV for a subject (for resume)."""
    subj_dir = os.path.join(data_dir, subject_id)
    if not os.path.isdir(subj_dir):
        return None
    pattern = os.path.join(subj_dir, f"{subject_id}_whole_session_*.csv")
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

            # If the process crashes, the last line can be truncated.
            # Scan backwards until we find a complete, parseable row.
            for line in reversed(lines):
                stripped = line.strip()
                if not stripped or stripped == header_line:
                    continue
                values = [v.strip() for v in stripped.split(',')]
                if len(values) < len(header):
                    continue
                data = dict(zip(header, values))
                result = {}
                ok = True
                for col, default_val in label_cols.items():
                    raw = data.get(col, "")
                    try:
                        result[col] = type(default_val)(raw)
                    except (ValueError, TypeError):
                        # If one column can't be parsed, try an earlier row.
                        ok = False
                        break
                if ok:
                    return result
            return None
    except Exception as e:
        print(f"[WARN] Could not read state from {csv_path}: {e}")
        return None


def get_rpe_from_segmented(subject_id, action_type, set_idx):
    """Return RPE for (action_type,set_idx) by inspecting segmented rep files.

    Segmented rep files get rewritten by update_rpe_in_files(), so the first
    data row typically contains the correct RPE for the whole file.
    """
    try:
        mapping = _scan_segmented_rpe_by_action_set(subject_id)
        rpe_val = mapping.get((action_type, int(set_idx)))
        if rpe_val is None:
            return None
        try:
            return int(float(rpe_val))
        except (ValueError, TypeError):
            return None
    except Exception:
        return None


# ========================= CSV Storage (Hierarchical) =========================
def _get_csv_key(subject_id, action_type, rep, set, is_rest=False):
    """Generate unique key for CSV file based on labels."""
    return (subject_id, action_type, rep, set, is_rest)

def _get_csv_path(subject_id, action_type, rep, set, is_rest=False):
    """Generate file path based on labels.
    Structure:
    - Normal: data/{subject}/{action}/set{set}/rep{rep}_{timestamp}.csv
    - Inter-set rest: data/{subject}/{action}/rest_after_set{set}/rest_{timestamp}.csv
    - Big rest (action_type="big_rest"): data/{subject}/big_rest/session{N}/rest_{timestamp}.csv
    """
    ts = datetime.now().strftime('%H%M%S')
    
    if action_type == "big_rest":
        # Big rest: separate folder per session
        base_dir = os.path.join(DATA_DIR, subject_id, "big_rest", f"session{set}")
        os.makedirs(base_dir, exist_ok=True)
        filename = f"rest_{ts}.csv"
    elif is_rest:
        # Inter-set rest folder: rest_after_set{current_set}/
        base_dir = os.path.join(DATA_DIR, subject_id, action_type, f"rest_after_set{set}")
        os.makedirs(base_dir, exist_ok=True)
        filename = f"rest_{ts}.csv"
    else:
        # Normal folder: set{set}/
        base_dir = os.path.join(DATA_DIR, subject_id, action_type, f"set{set}")
        os.makedirs(base_dir, exist_ok=True)
        filename = f"rep{rep}_{ts}.csv"
    
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
    filename = f"{subject_id}_whole_session_{ts}.csv"
    path = os.path.join(base_dir, filename)
    f = open(path, "w", newline="", encoding="utf-8")
    writer = csv.writer(f)
    writer.writerow(CSV_HEADERS_FULL)
    f.flush()
    print(f"[INFO] Created whole session file: {path}")
    return (f, writer, path, 0)


def _scan_segmented_rpe_by_action_set(subject_id):
    """Scan segmented rep CSVs and build (action_type, set_idx) -> rpe mapping."""
    sid = str(subject_id)
    mapping = {}

    pattern = os.path.join(DATA_DIR, sid, "*", "set*", "rep*.csv")
    for path in glob.glob(pattern):
        parsed = _extract_action_set_from_segmented_path(path)
        if parsed is None:
            continue

        try:
            with open(path, 'r', newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header is None:
                    continue
                try:
                    rpe_idx = header.index('rpe')
                except ValueError:
                    continue
                # RPE is constant across the file after update_rpe_in_files().
                row = next(reader, None)
                if row is None or len(row) <= rpe_idx:
                    continue
                rpe_val = row[rpe_idx]
        except Exception:
            continue

        mapping[parsed] = rpe_val

    return mapping


def backfill_whole_session_rpe_from_segmented(subject_id, whole_session_path=None) -> None:
    """Backfill whole-session RPE using segmented files on disk.

    This is intentionally derived from segmented CSVs to be crash-safe.
    """
    sid = str(subject_id) if subject_id is not None else ""
    if not sid:
        return

    pair_to_rpe = _scan_segmented_rpe_by_action_set(sid)
    if not pair_to_rpe:
        return

    target_path = whole_session_path if whole_session_path and os.path.exists(whole_session_path) else None
    if target_path is None:
        target_path = _find_latest_session_file(DATA_DIR, sid)
    if not target_path or not os.path.exists(target_path):
        return

    try:
        with open(target_path, 'r', newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header is None:
                return

            try:
                rpe_idx = header.index('rpe')
                action_idx = header.index('action_type')
                set_idx = header.index('set')
            except ValueError:
                return

            rows = [header]
            updated_rows = 0
            for row in reader:
                if len(row) > max(rpe_idx, action_idx, set_idx):
                    action_val = row[action_idx]
                    try:
                        set_val = int(row[set_idx])
                    except ValueError:
                        set_val = None
                    if set_val is not None:
                        new_rpe = pair_to_rpe.get((action_val, set_val))
                        if new_rpe is not None:
                            row[rpe_idx] = str(new_rpe)
                            updated_rows += 1
                rows.append(row)

        with open(target_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerows(rows)

        if updated_rows > 0:
            print(f"[RPE] Backfilled {updated_rows} rows in whole session {target_path} from segmented")
    except Exception as e:
        print(f"[WARN] Error backfilling RPE in whole session {target_path}: {e}", file=sys.stderr)

def _open_csv_file(subject_id, action_type, rep, set, is_rest=False):
    """Open a new CSV file for the given label combination."""
    key = _get_csv_key(subject_id, action_type, rep, set, is_rest)
    path = _get_csv_path(subject_id, action_type, rep, set, is_rest)
    f = open(path, "w", newline="", encoding="utf-8")
    writer = csv.writer(f)
    writer.writerow(CSV_HEADERS_SEGMENTED)
    f.flush()
    return key, (f, writer, path, 0)

def close_all_csv(subject_id=None):
    """Close all open CSV files including whole session file."""
    global whole_session_file, current_set_file_paths, last_set_file_paths
    apply_subject_id = str(subject_id) if subject_id is not None else None
    apply_whole_session_path = None
    with csv_lock:
        # Close segmented files
        for key, (f, writer, path, count) in csv_files.items():
            try:
                f.close()
                print(f"[INFO] Closed: {path} ({count} rows)")
            except Exception as e:
                print(f"[WARN] Error closing file: {e}", file=sys.stderr)
        csv_files.clear()
        
        # Clear file tracking
        with set_files_lock:
            current_set_file_paths.clear()
            last_set_file_paths.clear()
        
        # Close whole session file
        if whole_session_file:
            try:
                f, writer, path, count = whole_session_file
                apply_whole_session_path = path
                apply_subject_id = os.path.basename(os.path.dirname(path))
                f.close()
                print(f"[INFO] Closed whole session file: {path} ({count} rows)")
            except Exception as e:
                print(f"[WARN] Error closing whole session file: {e}", file=sys.stderr)
            whole_session_file = None

    # Backfill whole-session RPE from segmented files after closing handles.
    try:
        backfill_whole_session_rpe_from_segmented(apply_subject_id, apply_whole_session_path)
    except Exception as e:
        print(f"[WARN] Failed to backfill whole-session RPE from segmented: {e}", file=sys.stderr)


def write_csv_row(serial_num, sensor_ts, host_ts, ppg_values_list, ax, ay, az, gx, gy, gz, mx, my, mz):
    """Write a single aligned row to CSV. Waveforms are updated in parse_and_process_line."""
    with recording_lock:
        if not is_recording:
            return

    labels = get_label_snapshot()
    pc_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

    # Get or create CSV writer based on current labels
    subject_id = labels["subject_id"]
    action_type = labels["action_type"]
    phase = labels["phase"]
    rep = labels["rep"]
    set_val = labels["set"]
    
    # Skip if no subject_id set
    if not subject_id or subject_id == "":
        return
    
    # Get or create the appropriate CSV file
    with csv_lock:
        # Also write to whole session file if it exists
        global whole_session_file, inter_set_rest_active
        if whole_session_file is None:
            # Create whole session file on first data write
            whole_session_file = _open_whole_session_file(subject_id)
        
        # Check if we're in inter-set rest mode
        with inter_set_rest_lock:
            is_rest = inter_set_rest_active
        
        # Write to whole session file (with all fields)
        if whole_session_file:
            f_ws, writer_ws, path_ws, count_ws = whole_session_file
            row_full = [
                pc_time,
                serial_num,
                sensor_ts,
                host_ts,
                ax, ay, az,
                gx, gy, gz,
                mx, my, mz,
                labels["action_type"],
                labels["phase"],
                labels["rep"],
                labels["set"],
                labels["rpe"],
                labels["weight_kg"],
                labels["subject_id"],
                *(ppg_values_list if len(ppg_values_list) == 5 else (ppg_values_list + [0.0] * 5)[:5]),
            ]
            writer_ws.writerow(row_full)
            f_ws.flush()
            whole_session_file = (f_ws, writer_ws, path_ws, count_ws + 1)
        
        key = _get_csv_key(subject_id, action_type, rep, set_val, is_rest)
        
        if key not in csv_files:
            # Create new file
            key, file_tuple = _open_csv_file(subject_id, action_type, rep, set_val, is_rest)
            csv_files[key] = file_tuple
            print(f"[INFO] Created new file: {file_tuple[2]}")
        
        f, writer, path, count = csv_files[key]
        
        # Track file path for current set (for RPE retroactive update)
        with set_files_lock:
            if not is_rest and action_type != "big_rest":
                # Only track non-rest files
                current_set_file_paths.add(path)
        
        # Row for segmented file (without fields already in path/filename)
        row_segmented = [
            pc_time,
            serial_num,
            sensor_ts,
            host_ts,
            ax, ay, az,
            gx, gy, gz,
            mx, my, mz,
            # phase included in row data (not in filename)
            phase,
            labels["rpe"],
            labels["weight_kg"],
            *ppg_values_list,
        ]
        writer.writerow(row_segmented)
        f.flush()
        
        # Update row count in dictionary
        csv_files[key] = (f, writer, path, count + 1)


# ========================= Data Alignment (PPG ↔ IMU linear interpolation) =====
# PPG and IMU arrive in alternating batches (~10 samples each).
# We keep a history of recent PPG samples and linearly interpolate PPG values
# at each IMU timestamp so every recorded row has real IMU + interpolated PPG.
# Waveforms for each sensor are updated only with their own real data (no zeros).

PPG_HISTORY_MAX = 200
ppg_history = collections.deque(maxlen=PPG_HISTORY_MAX)  # (host_ts, [5 floats])
ppg_history_lock = threading.Lock()


def interpolate_ppg(target_ts):
    """Linearly interpolate 5-channel PPG values at *target_ts*."""
    with ppg_history_lock:
        n = len(ppg_history)
        if n == 0:
            return [0.0] * 5
        if n == 1:
            return list(ppg_history[0][1])

        # ppg_history is in arrival order (monotonically increasing host_ts)
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

        # Linear interpolation
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

        # PPG values (columns 4-8)
        ppg_values = [float(cols[4 + i]) if 4 + i < n else 0.0 for i in range(5)]

        # IMU values (columns 9-17)
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

    # ---- Route by sensor type ------------------------------------------------
    if sensor_type == "PPG":
        # Store for interpolation
        is_valid = any(v != 0 for v in ppg_values)
        if is_valid:
            with ppg_history_lock:
                ppg_history.append((host_ts, ppg_values))
        # Update PPG waveforms with real data only (no zeros from IMU rows)
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

        # Interpolate PPG at this IMU timestamp
        interp_ppg = interpolate_ppg(host_ts)

        # Update IMU waveforms with real data only
        with waveform_lock:
            waveform_accel["x"].append(ax); waveform_accel["y"].append(ay); waveform_accel["z"].append(az)
            waveform_gyro["x"].append(gx);  waveform_gyro["y"].append(gy);  waveform_gyro["z"].append(gz)
            waveform_mag["x"].append(mx);   waveform_mag["y"].append(my);   waveform_mag["z"].append(mz)

        # Update latest IMU for live preview
        with latest_imu_lock:
            global latest_imu_data
            latest_imu_data = (ax, ay, az, gx, gy, gz, mx, my, mz)

        # Write combined row: real IMU + interpolated PPG
        write_csv_row(serial_num, sensor_ts, host_ts, interp_ppg,
                      ax, ay, az, gx, gy, gz, mx, my, mz)
        return None

    else:
        # Unknown type — write as-is
        ppg_for_write = ppg_values if any(ppg_values) else [0.0] * 5
        write_csv_row(serial_num, sensor_ts, host_ts, ppg_for_write,
                      ax, ay, az, gx, gy, gz, mx, my, mz)
        return None


# ========================= Data reader =========================
def stdin_reader():
    """Read IMU lines from stdin (pipe) line-by-line — no buffering delay."""
    for line in sys.stdin:
        raw = line.rstrip("\r\n")
        parse_and_process_line(raw)

def file_reader(path):
    """Tail a growing file and write IMU rows to CSV (fallback)."""
    import time
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
# logic for `tools/imu_workout_gui.py`.
