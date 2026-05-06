#!/usr/bin/env python3
"""
IMU Label GUI (PySide6)
Qt-based workout labeling UI that reuses backend logic from imu_workout_core.py.
"""

import argparse
import signal
import sys
import threading

try:
    from PySide6.QtCore import QEvent, QObject, QTimer, Qt
    from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
    from PySide6.QtWidgets import (
        QApplication,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QScrollArea,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError:
    print("[ERROR] PySide6 未安裝。請先執行: .venv/bin/python3 -m pip install PySide6")
    raise

import imu_workout_core as core


_CLR = {
    "bg": "#1e1e2e",
    "bg_light": "#2a2a3c",
    "bg_widget": "#313145",
    "fg": "#e0e0e0",
    "fg_dim": "#b8b8cc",
    "accent": "#7aa2f7",
    "btn": "#3a3a50",
    "btn_fg": "#e0e0e0",
    "btn_hover": "#4a4a60",
    "action": "#2980b9",
    "phase": "#27ae60",
    "rest": "#e67e22",
    "big_rest": "#9b59b6",
    "rpe": "#e74c3c",
    "rec_on": "#c0392b",
    "rec_pause": "#e67e22",
}


class WaveformWidget(QWidget):
    def __init__(self, title, line_colors, parent=None):
        super().__init__(parent)
        self.title = title
        self.line_colors = [QColor(c) for c in line_colors]
        self._series = []
        self._vmin = 0.0
        self._vmax = 0.0
        self.setMinimumSize(210, 76)

    def set_series(self, series):
        self._series = series
        all_vals = [v for line in series for v in line]
        if all_vals:
            vmin = min(all_vals)
            vmax = max(all_vals)
            margin = max(abs(vmax - vmin) * 0.1, 0.01)
            self._vmin = vmin - margin
            self._vmax = vmax + margin
        else:
            self._vmin = 0.0
            self._vmax = 0.0
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)

        r = self.rect()
        painter.fillRect(r, QColor("#1e1e1e"))
        painter.setPen(QPen(QColor("#3a3a50"), 1))
        painter.drawRect(r.adjusted(0, 0, -1, -1))

        if not self._series or not any(self._series):
            painter.setPen(QColor("#666666"))
            painter.drawText(r, Qt.AlignCenter, "waiting...")
            return

        pad_l, pad_r, pad_t, pad_b = 6, 6, 6, 6
        w = max(1, r.width() - pad_l - pad_r)
        h = max(1, r.height() - pad_t - pad_b)

        if self._vmin <= 0 <= self._vmax and self._vmax != self._vmin:
            y0 = pad_t + h * (1.0 - (0 - self._vmin) / (self._vmax - self._vmin))
            pen0 = QPen(QColor("#444444"), 1)
            pen0.setStyle(Qt.DashLine)
            painter.setPen(pen0)
            painter.drawLine(pad_l, int(y0), pad_l + w, int(y0))

        for i, data in enumerate(self._series):
            if len(data) < 2 or self._vmax == self._vmin:
                continue
            color = self.line_colors[min(i, len(self.line_colors) - 1)]
            painter.setPen(QPen(color, 1.4))
            path = QPainterPath()
            for idx, val in enumerate(data):
                x = pad_l + (idx / max(len(data) - 1, 1)) * w
                y = pad_t + h * (1.0 - (val - self._vmin) / (self._vmax - self._vmin))
                if idx == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            painter.drawPath(path)

        painter.setPen(QColor("#b8b8cc"))
        painter.drawText(
            r.adjusted(6, 2, -6, -2),
            Qt.AlignTop | Qt.AlignLeft,
            f"{self.title} [{self._vmin:.1f} ~ {self._vmax:.1f}]",
        )


class FocusClearFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress:
            fw = QApplication.focusWidget()
            if isinstance(fw, QLineEdit):
                pos = event.globalPosition().toPoint()
                clicked = QApplication.widgetAt(pos)
                if not isinstance(clicked, QLineEdit):
                    fw.clearFocus()
        return super().eventFilter(obj, event)


class LabelGUIQt(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("IMU Label Tool (Qt)")
        self.resize(1180, 860)

        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._hide_toast)

        self._stopwatch_timer = QTimer(self)
        self._stopwatch_timer.setInterval(100)
        self._stopwatch_timer.timeout.connect(self._tick_stopwatch)
        self._stopwatch_elapsed_ms = 0

        self._focus_filter = FocusClearFilter()
        QApplication.instance().installEventFilter(self._focus_filter)

        self._build_ui()
        self._bind_shortcuts()

        self._periodic_timer = QTimer(self)
        self._periodic_timer.timeout.connect(self._periodic_update)
        self._periodic_timer.start(80)

        self.update_status()
        self._refresh_rec_button_state()

    def _build_ui(self):
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget {{
                background: {_CLR['bg']};
                color: {_CLR['fg']};
                font-family: Helvetica;
                font-size: 13px;
            }}
            QGroupBox {{
                border: 1px solid {_CLR['btn']};
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 8px;
                background: {_CLR['bg_light']};
                color: {_CLR['accent']};
                font-weight: 700;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }}
            QLabel.dim {{
                color: {_CLR['fg_dim']};
            }}
            QLineEdit {{
                background: {_CLR['bg_widget']};
                color: {_CLR['fg']};
                border: 1px solid {_CLR['btn']};
                border-radius: 6px;
                padding: 6px 8px;
            }}
            QPushButton {{
                background: {_CLR['btn']};
                color: {_CLR['btn_fg']};
                border: 1px solid {_CLR['btn']};
                border-radius: 6px;
                padding: 6px 10px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {_CLR['btn_hover']};
            }}
            """
        )

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(8, 8, 8, 8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        content = QWidget()
        self.content_layout = QVBoxLayout(content)
        self.content_layout.setSpacing(8)

        self.toast_label = QLabel("")
        self.toast_label.setAlignment(Qt.AlignCenter)
        self.toast_label.setStyleSheet(f"background:{_CLR['bg']}; color:white; font-weight:700; padding:0px;")
        self.content_layout.addWidget(self.toast_label)

        status_box = QGroupBox("目前標記狀態")
        status_layout = QVBoxLayout(status_box)
        self.status_label = QLabel("")
        self.status_label.setFont(QFont("Menlo", 11))
        self.status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        status_layout.addWidget(self.status_label)
        self.content_layout.addWidget(status_box)

        rec_box = QGroupBox("錄製控制")
        rec_layout = QHBoxLayout(rec_box)
        self.rec_btn = QPushButton("⏺ REC Start")
        self.rec_btn.clicked.connect(self.toggle_recording)
        self.rec_status_label = QLabel("[STOP] Not Recording")
        self.row_count_label = QLabel("已記錄: 0 筆")
        self.csv_name_label = QLabel("")
        self.csv_name_label.setProperty("class", "dim")
        rec_layout.addWidget(self.rec_btn)
        rec_layout.addWidget(self.rec_status_label)
        rec_layout.addWidget(self.row_count_label)
        rec_layout.addWidget(self.csv_name_label, 1)

        timer_box = QGroupBox("計時器")
        timer_layout = QHBoxLayout(timer_box)
        self.stopwatch_label = QLabel("00:00.0")
        self.stopwatch_label.setFont(QFont("Menlo", 18, QFont.Bold))
        self.stopwatch_label.setStyleSheet(f"color: {_CLR['accent']};")
        self.stopwatch_toggle_btn = QPushButton("開始")
        self.stopwatch_toggle_btn.clicked.connect(self.toggle_stopwatch)
        stopwatch_reset_btn = QPushButton("重置")
        stopwatch_reset_btn.clicked.connect(self.reset_stopwatch)
        timer_layout.addWidget(self.stopwatch_label)
        timer_layout.addWidget(self.stopwatch_toggle_btn)
        timer_layout.addWidget(stopwatch_reset_btn)
        timer_layout.addStretch(1)

        rec_timer_row = QHBoxLayout()
        rec_timer_row.addWidget(rec_box, 3)
        rec_timer_row.addWidget(timer_box, 2)
        self.content_layout.addLayout(rec_timer_row)

        wave_box = QGroupBox("IMU & PPG Waveforms")
        wave_layout = QVBoxLayout(wave_box)

        self.wave_widgets = {}

        imu_row = QHBoxLayout()
        for title in ("Accel (g)", "Gyro (dps)", "Mag (uT)"):
            widget = WaveformWidget(title, ["#e74c3c", "#2ecc71", "#3498db"])
            self.wave_widgets[title] = widget
            imu_row.addWidget(widget)
        wave_layout.addLayout(imu_row)

        ppg_row = QHBoxLayout()
        for title in ("PPG A", "PPG B", "PPG C", "PPG D", "PPG E"):
            widget = WaveformWidget(title, ["#ffffff"])
            self.wave_widgets[title] = widget
            ppg_row.addWidget(widget)
        wave_layout.addLayout(ppg_row)

        legend = QHBoxLayout()
        for text, color in (("  X  ", "#e74c3c"), ("  Y  ", "#2ecc71"), ("  Z  ", "#3498db")):
            lbl = QLabel(text)
            lbl.setStyleSheet(f"background:#1e1e1e; color:{color}; font-family:Menlo; font-weight:700;")
            legend.addWidget(lbl)
        legend.addStretch(1)
        wave_layout.addLayout(legend)

        self.content_layout.addWidget(wave_box)

        subject_box = QGroupBox("受試者 ID")
        subject_layout = QHBoxLayout(subject_box)
        next_id = core._auto_next_subject_id(core.DATA_DIR)
        core.update_label_state(subject_id=next_id)
        self.subject_id_entry = QLineEdit(next_id)
        self.subject_id_entry.returnPressed.connect(self.apply_subject_id)
        apply_subject_btn = QPushButton("套用受試者 ID")
        apply_subject_btn.clicked.connect(self.apply_subject_id)
        suggestion = QLabel(f"(建議: {next_id})")
        suggestion.setProperty("class", "dim")
        subject_layout.addWidget(self.subject_id_entry)
        subject_layout.addWidget(apply_subject_btn)
        subject_layout.addWidget(suggestion)

        weight_box = QGroupBox("重量設定 (kg)")
        weight_layout = QHBoxLayout(weight_box)
        self.weight_entry = QLineEdit("0.0")
        self.weight_entry.returnPressed.connect(self.apply_weight)
        apply_weight_btn = QPushButton("套用重量")
        apply_weight_btn.clicked.connect(self.apply_weight)
        weight_layout.addWidget(self.weight_entry)
        weight_layout.addWidget(apply_weight_btn)

        subject_weight_row = QHBoxLayout()
        subject_weight_row.addWidget(subject_box, 2)
        subject_weight_row.addWidget(weight_box, 1)
        self.content_layout.addLayout(subject_weight_row)

        self._action_btns = {}
        action_box = QGroupBox("動作型態")
        action_layout = QGridLayout(action_box)
        action_buttons = [
            ("無", "none"),
            ("休息", "rest"),
            ("啞鈴臥推", "db_bench_press"),
            ("單手啞鈴划船", "one_arm_db_row"),
            ("啞鈴羅馬尼亞硬舉", "db_rdl"),
            ("啞鈴負重卷腹", "db_weighted_crunch"),
            ("啞鈴肩推", "db_shoulder_press"),
            ("啞鈴二頭彎舉", "db_biceps_curl"),
            ("啞鈴三頭彎舉", "db_triceps_curl"),
            ("啞鈴深蹲", "db_squat"),
        ]
        self._default_action = next((val for _, val in action_buttons if val not in ("none", "rest")), "none")
        core.update_label_state(action_type=self._default_action)
        for idx, (text, val) in enumerate(action_buttons):
            btn = QPushButton(text)
            btn.clicked.connect(lambda checked=False, v=val: self.set_action(v))
            self._action_btns[val] = btn
            action_layout.addWidget(btn, idx // 5, idx % 5)
        self._highlight_action(self._default_action)
        self.content_layout.addWidget(action_box)

        self._phase_btns = {}
        phase_box = QGroupBox("動作階段")
        phase_layout = QHBoxLayout(phase_box)
        phase_buttons = [
            ("None", "none"),
            ("向心 Concentric", "concentric"),
            ("離心 Eccentric", "eccentric"),
            ("組間休息", "inter_set_rest"),
            ("大休息", "big_rest"),
        ]
        for text, val in phase_buttons:
            btn = QPushButton(text)
            btn.clicked.connect(lambda checked=False, v=val: self.set_phase(v))
            self._phase_btns[val] = btn
            phase_layout.addWidget(btn)
        self.content_layout.addWidget(phase_box)

        self._rpe_btns = {}
        rpe_box = QGroupBox("主觀疲勞量表 RPE (0-10)")
        rpe_layout = QHBoxLayout(rpe_box)
        self.rpe_label = QLabel("RPE: 0")
        self.rpe_label.setStyleSheet(f"color: {_CLR['rpe']}; font-weight: 700; font-size: 16px;")
        rpe_layout.addWidget(self.rpe_label)
        rpe_layout.addWidget(QLabel("按數字鍵 0-9 設定:"))
        for i in range(10):
            btn = QPushButton(str(i))
            btn.setFixedWidth(40)
            btn.clicked.connect(lambda checked=False, v=i: self.set_rpe(v))
            self._rpe_btns[i] = btn
            rpe_layout.addWidget(btn)
        clear_btn = QPushButton("清除")
        clear_btn.clicked.connect(self.clear_rpe)
        rpe_layout.addWidget(clear_btn)
        self.content_layout.addWidget(rpe_box)

        quick_box = QGroupBox("快速控制")
        quick_layout = QHBoxLayout(quick_box)
        clear_labels_btn = QPushButton("清空標記")
        clear_labels_btn.clicked.connect(self.clear_labels)
        new_subject_btn = QPushButton("[NEW] 新受試者")
        new_subject_btn.clicked.connect(self.new_subject)
        quick_layout.addWidget(clear_labels_btn)
        quick_layout.addWidget(new_subject_btn)

        info_box = QGroupBox("快捷鍵")
        info_layout = QVBoxLayout(info_box)
        info = QLabel(
            "c = 向心(concentric)    e = 離心(eccentric)\n"
            "h = 組間休息(Toggle)    b = 大休息(Toggle)\n"
            "x = clear labels\n"
            "0-9 = RPE 主觀疲勞量表    - = 清除 RPE\n"
            "space = 暫停/繼續錄製    Esc = 移除輸入框焦點\n"
            "n = 結束受試者（套用RPE後關閉檔案）"
        )
        info.setWordWrap(True)
        info.setFont(QFont("Menlo", 10))
        info_layout.addWidget(info)

        quick_info_row = QHBoxLayout()
        quick_info_row.addWidget(quick_box, 1)
        quick_info_row.addWidget(info_box, 2)
        self.content_layout.addLayout(quick_info_row)

        self.content_layout.addStretch(1)
        scroll.setWidget(content)
        root_layout.addWidget(scroll)
        self.setCentralWidget(root)

    def _bind_shortcuts(self):
        def bind(key, cb):
            sc = QShortcut(QKeySequence(key), self)
            sc.activated.connect(lambda: self._trigger_if_not_entry(cb))

        bind("C", lambda: self.set_phase("concentric"))
        bind("E", lambda: self.set_phase("eccentric"))
        bind("H", lambda: self.set_phase("inter_set_rest"))
        bind("B", lambda: self.set_phase("big_rest"))
        bind("X", self.clear_labels)
        bind("N", self.new_subject)
        bind("Space", self.toggle_recording)
        bind("-", self.clear_rpe)
        bind("Escape", self._clear_input_focus)
        for digit in "0123456789":
            bind(digit, lambda v=int(digit): self.set_rpe(v))

    def _trigger_if_not_entry(self, callback):
        if isinstance(QApplication.focusWidget(), QLineEdit):
            return
        callback()

    def _clear_input_focus(self):
        fw = QApplication.focusWidget()
        if isinstance(fw, QLineEdit):
            fw.clearFocus()

    def _set_button_color(self, btn, color=None):
        if color:
            btn.setStyleSheet(
                f"QPushButton {{background:{color}; color:white; border:2px solid {color}; border-radius:6px; padding:6px 10px; font-weight:700;}}"
            )
        else:
            btn.setStyleSheet("")

    def _highlight_action(self, active_val):
        for val, btn in self._action_btns.items():
            self._set_button_color(btn, _CLR["action"] if val == active_val else None)

    def _highlight_phase(self, active_val):
        with core.inter_set_rest_lock:
            rest_on = core.inter_set_rest_active
        in_big_rest = core.label_state.get("action_type") == "big_rest"
        for val, btn in self._phase_btns.items():
            if val == "inter_set_rest" and rest_on:
                self._set_button_color(btn, _CLR["rest"])
            elif val == "big_rest" and in_big_rest:
                self._set_button_color(btn, _CLR["big_rest"])
            elif val == active_val:
                self._set_button_color(btn, _CLR["phase"])
            else:
                self._set_button_color(btn, None)

    def _highlight_rpe(self, active_val):
        for val, btn in self._rpe_btns.items():
            self._set_button_color(btn, _CLR["rpe"] if val == active_val else None)

    def _reset_all_highlights(self):
        for btn in self._action_btns.values():
            self._set_button_color(btn, None)
        for btn in self._phase_btns.values():
            self._set_button_color(btn, None)
        for btn in self._rpe_btns.values():
            self._set_button_color(btn, None)

    def _show_toast(self, msg, color="#27ae60", duration=2500):
        self.toast_label.setText(f"  {msg}  ")
        self.toast_label.setStyleSheet(
            f"background:{color}; color:white; font-weight:700; padding:6px; border-radius:6px;"
        )
        self._toast_timer.start(duration)

    def _hide_toast(self):
        self.toast_label.setText("")
        self.toast_label.setStyleSheet(f"background:{_CLR['bg']}; color:white; padding:0px;")

    def _refresh_with_state(self, **kwargs):
        core.update_label_state(**kwargs)
        self.update_status()

    def _tick_stopwatch(self):
        self._stopwatch_elapsed_ms += self._stopwatch_timer.interval()
        self._update_stopwatch_text()

    def _update_stopwatch_text(self):
        total_tenths = self._stopwatch_elapsed_ms // 100
        minutes = total_tenths // 600
        seconds = (total_tenths // 10) % 60
        tenths = total_tenths % 10
        self.stopwatch_label.setText(f"{minutes:02d}:{seconds:02d}.{tenths}")

    def toggle_stopwatch(self):
        if self._stopwatch_timer.isActive():
            self._stopwatch_timer.stop()
            self.stopwatch_toggle_btn.setText("開始")
        else:
            self._stopwatch_timer.start()
            self.stopwatch_toggle_btn.setText("暫停")

    def reset_stopwatch(self):
        self._stopwatch_timer.stop()
        self._stopwatch_elapsed_ms = 0
        self._update_stopwatch_text()
        self.stopwatch_toggle_btn.setText("開始")

    def _start_stopwatch(self):
        if not self._stopwatch_timer.isActive():
            self._stopwatch_timer.start()
        self.stopwatch_toggle_btn.setText("暫停")

    def _current_csv_preview_path(self):
        labels = core.get_label_snapshot()
        subject_id = labels.get("subject_id")
        action_type = labels.get("action_type", "none")
        set_idx = labels.get("set", 0)

        if not subject_id:
            return ""

        with core.inter_set_rest_lock:
            in_inter_rest = core.inter_set_rest_active

        if action_type == "big_rest":
            path = f"data/{subject_id}/big_rest/session{set_idx}/"
        elif not action_type or action_type == "none":
            path = f"data/{subject_id}/"
        elif in_inter_rest:
            path = f"data/{subject_id}/{action_type}/rest_after_set{set_idx}/"
        else:
            path = f"data/{subject_id}/{action_type}/set{set_idx}/"

        return path

    def _sync_csv_preview_path(self, prefix=""):
        path = self._current_csv_preview_path()
        if not path:
            self.csv_name_label.setText("")
            return

        self.csv_name_label.setText(f"{prefix}{path}")

    def _refresh_rec_button_state(self):
        with core.recording_lock:
            rec = core.is_recording
        if rec:
            self._set_button_color(self.rec_btn, _CLR["rec_on"])
        elif self.rec_status_label.text().startswith("[PAUSE]"):
            self._set_button_color(self.rec_btn, _CLR["rec_pause"])
        else:
            self._set_button_color(self.rec_btn, None)

    def _periodic_update(self):
        total_rows = 0
        with core.csv_lock:
            for _, (_, _, _, count) in core.csv_files.items():
                total_rows += count
            if core.whole_session_file:
                total_rows += core.whole_session_file[3]
        self.row_count_label.setText(f"已記錄: {total_rows} 筆")

        with core.waveform_lock:
            accel = [list(core.waveform_accel[axis]) for axis in ("x", "y", "z")]
            gyro = [list(core.waveform_gyro[axis]) for axis in ("x", "y", "z")]
            mag = [list(core.waveform_mag[axis]) for axis in ("x", "y", "z")]
            ppg_a = [list(core.waveform_ppg_a["x"])]
            ppg_b = [list(core.waveform_ppg_b["x"])]
            ppg_c = [list(core.waveform_ppg_c["x"])]
            ppg_d = [list(core.waveform_ppg_d["x"])]
            ppg_e = [list(core.waveform_ppg_e["x"])]

        self.wave_widgets["Accel (g)"].set_series(accel)
        self.wave_widgets["Gyro (dps)"].set_series(gyro)
        self.wave_widgets["Mag (uT)"].set_series(mag)
        self.wave_widgets["PPG A"].set_series(ppg_a)
        self.wave_widgets["PPG B"].set_series(ppg_b)
        self.wave_widgets["PPG C"].set_series(ppg_c)
        self.wave_widgets["PPG D"].set_series(ppg_d)
        self.wave_widgets["PPG E"].set_series(ppg_e)

        labels = core.get_label_snapshot()
        self._highlight_phase(labels["phase"])
        self._refresh_rec_button_state()

    def update_status(self):
        labels = core.get_label_snapshot()
        rpe_val = labels["rpe"]

        rest_indicator = ""
        with core.inter_set_rest_lock:
            if core.inter_set_rest_active:
                rest_indicator = " [組間休息中]"

        action_display = labels["action_type"]
        with core.big_rest_lock:
            if labels["action_type"] == "big_rest" and core.action_before_big_rest:
                action_display = f"big_rest (下一個動作: {core.action_before_big_rest})"

        text = (
            f"subject_id  : {labels['subject_id']}\n"
            f"weight_kg   : {labels['weight_kg']}\n"
            f"action_type : {action_display}\n"
            f"phase       : {labels['phase']}{rest_indicator}\n"
            f"rep         : {labels['rep']}\n"
            f"set         : {labels['set']}\n"
            f"RPE         : {rpe_val}"
        )
        self.status_label.setText(text)

    def toggle_recording(self):
        with core.recording_lock:
            if core.is_recording:
                core.is_recording = False
                self.rec_status_label.setText("[PAUSE] 暫停中")
                self.rec_btn.setText("⏺ REC Resume")
                self._set_button_color(self.rec_btn, _CLR["rec_pause"])
                self._show_toast("錄製已暫停", "#e67e22")
            else:
                labels = core.get_label_snapshot()
                if not labels.get("subject_id"):
                    self.rec_status_label.setText("[ERROR] Set Subject ID first!")
                    self._show_toast("請先設定受試者 ID！", "#e74c3c", 3000)
                    return
                if not labels.get("action_type") or labels["action_type"] == "none":
                    self._show_toast("提醒：尚未設定動作型態", "#e67e22", 3000)
                if float(labels.get("weight_kg", 0.0)) <= 0.0:
                    self._show_toast("提醒：尚未設定重量", "#e67e22", 3000)
                core.is_recording = True
                self.rec_status_label.setText("** RECORDING **")
                self.rec_btn.setText("⏸ PAUSE")
                self._set_button_color(self.rec_btn, _CLR["rec_on"])
                self._sync_csv_preview_path(prefix="-> ")
                self._show_toast("開始錄製", "#27ae60")

    def set_action(self, value):
        prev_action = core.label_state.get("action_type", "none")

        with core.big_rest_lock:
            in_big_rest = prev_action == "big_rest"

        if in_big_rest and value != "big_rest":
            with core.big_rest_lock:
                core.action_before_big_rest = value
            self._refresh_with_state()
            labels = core.get_label_snapshot()
            subj = labels["subject_id"]
            if subj:
                self.csv_name_label.setText(f"[預覽] data/{subj}/{value}/ (大休息中)")
            self._show_toast(f"預覽下一動作: {value}", "#9b59b6", 2000)
            return

        if value != prev_action:
            with core.label_lock:
                core.label_state["set"] = 0
                core.label_state["rep"] = 0
            with core.inter_set_rest_lock:
                was_in_inter_rest = core.inter_set_rest_active
                core.inter_set_rest_active = False
            if was_in_inter_rest and core.pending_rpe_for_last_set is not None and core.last_set_file_paths:
                core.update_rpe_in_files(core.last_set_file_paths, core.pending_rpe_for_last_set)
                core.pending_rpe_for_last_set = None
            with core.set_files_lock:
                core.current_set_file_paths.clear()
                core.last_set_file_paths.clear()

        self._refresh_with_state(action_type=value)

        labels = core.get_label_snapshot()
        self._sync_csv_preview_path()
        self._highlight_action(value)
        path = self._current_csv_preview_path()
        self._show_toast(f"動作設定: {value} → {path}", "#2980b9", 1800)

    def set_phase(self, value):
        if value == "inter_set_rest":
            apply_rpe = None
            apply_paths = None

            with core.inter_set_rest_lock:
                if core.inter_set_rest_active:
                    if core.pending_rpe_for_last_set is not None and core.last_set_file_paths:
                        apply_rpe = core.pending_rpe_for_last_set
                        apply_paths = core.last_set_file_paths.copy()
                        core.pending_rpe_for_last_set = None
                    with core.set_files_lock:
                        core.last_set_file_paths = core.current_set_file_paths.copy()
                        core.current_set_file_paths.clear()
                    with core.label_lock:
                        core.label_state["set"] += 1
                        core.label_state["rep"] = 0
                    core.inter_set_rest_active = False
                else:
                    with core.set_files_lock:
                        core.last_set_file_paths = core.current_set_file_paths.copy()
                    core.inter_set_rest_active = True

            # IMPORTANT: do not do heavy I/O while holding inter_set_rest_lock.
            # Whole-session RPE backfill is applied at session end (derived from segmented).
            if apply_rpe is not None and apply_paths:
                core.update_rpe_in_files(apply_paths, apply_rpe)
            core.rep_phase_tracker = {"concentric": False, "eccentric": False}
            self._refresh_with_state(phase=value)
            self._sync_csv_preview_path()
            self._highlight_phase(value)
            with core.inter_set_rest_lock:
                rest_now = core.inter_set_rest_active
            if rest_now:
                self._start_stopwatch()
                path = self._current_csv_preview_path()
                self._show_toast(f"組間休息開始 → {path}", "#e67e22", 1800)
            else:
                self.reset_stopwatch()
                path = self._current_csv_preview_path()
                self._show_toast(f"組間休息結束 → 下一組 ({path})", "#27ae60", 1800)
            return

        if value == "big_rest":
            current_action = core.label_state.get("action_type", "none")
            with core.big_rest_lock:
                if current_action == "big_rest":
                    if core.pending_rpe_for_last_set is not None and core.last_set_file_paths:
                        core.update_rpe_in_files(core.last_set_file_paths, core.pending_rpe_for_last_set)
                        core.pending_rpe_for_last_set = None
                    with core.set_files_lock:
                        core.last_set_file_paths.clear()
                        core.current_set_file_paths.clear()
                    if core.action_before_big_rest:
                        restored_action = core.action_before_big_rest
                        core.action_before_big_rest = None
                    else:
                        restored_action = "none"
                        core.action_before_big_rest = None
                    with core.label_lock:
                        core.label_state["action_type"] = restored_action
                        core.label_state["set"] = 0
                        core.label_state["rep"] = 0
                else:
                    with core.inter_set_rest_lock:
                        was_in_inter_rest = core.inter_set_rest_active
                        core.inter_set_rest_active = False
                    with core.set_files_lock:
                        if not was_in_inter_rest:
                            core.last_set_file_paths = core.current_set_file_paths.copy()
                        core.current_set_file_paths.clear()
                    core.action_before_big_rest = current_action
                    session_num = core.big_rest_count
                    core.big_rest_count += 1
                    with core.label_lock:
                        core.label_state["action_type"] = "big_rest"
                        core.label_state["set"] = session_num
                        core.label_state["rep"] = 0
            core.rep_phase_tracker = {"concentric": False, "eccentric": False}
            self._refresh_with_state(phase="none")
            labels = core.get_label_snapshot()
            act = labels["action_type"]
            self._sync_csv_preview_path()
            if act == "big_rest":
                self._highlight_phase("big_rest")
                self._start_stopwatch()
                path = self._current_csv_preview_path()
                self._show_toast(f"大休息開始 → {path}", "#9b59b6", 1800)
            else:
                self._highlight_action(act)
                self.reset_stopwatch()
                path = self._current_csv_preview_path()
                self._show_toast(f"大休息結束 → {act} ({path})", "#27ae60", 1800)
            return

        # Enforce phase order for rep completion: concentric -> eccentric.
        # - Ignore eccentric selections before any concentric in the current rep.
        # - Only increment rep when transitioning back to concentric (start next rep)
        #   so both phases stay within the same rep file.
        auto_incremented = False
        if value == "concentric":
            auto_incremented = core.check_and_auto_increment_rep(next_phase="concentric")
            core.rep_phase_tracker["concentric"] = True
            self._refresh_with_state(phase=value)
        elif value == "eccentric":
            if not core.rep_phase_tracker.get("concentric", False):
                return
            core.rep_phase_tracker["eccentric"] = True
            self._refresh_with_state(phase=value)
        else:
            self._refresh_with_state(phase=value)

        if auto_incremented:
            self.update_status()
        self._highlight_phase(value)
        self._show_toast(f"階段: {value}", "#27ae60", 1500)

    def apply_weight(self):
        try:
            w = float(self.weight_entry.text().strip())
            self._refresh_with_state(weight_kg=w)
            self._show_toast(f"重量已設定: {w} kg", "#3498db", 2000)
        except ValueError:
            QMessageBox.warning(self, "警告", "重量必須是數字！")

    def apply_subject_id(self):
        sid = self.subject_id_entry.text().strip()
        if not sid:
            QMessageBox.warning(self, "警告", "受試者 ID 不能為空！")
            return

        with core.recording_lock:
            if core.is_recording:
                confirm = QMessageBox.question(
                    self,
                    "確認",
                    f"正在錄製中！確定要切換受試者為 {sid}？\n（建議先按 [NEW] 結束當前受試者）",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if confirm != QMessageBox.Yes:
                    return

        resume_path = core._find_latest_session_file(core.DATA_DIR, sid)
        if resume_path:
            last_state = core._read_last_row_state(
                resume_path,
                {"action_type": "none", "phase": "none", "rep": 0, "set": 0, "rpe": 0, "weight_kg": 0.0},
            )
            state_info = ""
            if last_state:
                state_info = (
                    f"\n\n上次狀態:\n"
                    f"  動作: {last_state['action_type']}\n"
                    f"  組: {last_state['set']}, 次: {last_state['rep']}\n"
                    f"  重量: {last_state['weight_kg']} kg, RPE: {last_state['rpe']}"
                )
            ans = QMessageBox.question(
                self,
                "發現既有數據",
                f"受試者 {sid} 已有數據：\n{resume_path.split('/')[-1]}{state_info}\n\n要接續收集數據嗎？\n（是＝接續，否＝建立新檔案）",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ans == QMessageBox.Yes:
                with core.csv_lock:
                    if core.whole_session_file:
                        f_old, _, _, _ = core.whole_session_file
                        f_old.close()
                    core.whole_session_file = core._open_whole_session_file(sid, resume_path=resume_path)
                if last_state:
                    resumed_phase = last_state.get("phase", "none")
                    with core.label_lock:
                        core.label_state["action_type"] = last_state["action_type"]
                        core.label_state["phase"] = resumed_phase
                        core.label_state["set"] = last_state["set"]
                        core.label_state["rep"] = last_state["rep"]
                        core.label_state["rpe"] = last_state["rpe"]
                        core.label_state["weight_kg"] = last_state["weight_kg"]

                    # If the previous run crashed before whole-session RPE backfill,
                    # the last row's RPE may be stale. Segmented files are the source
                    # of truth for final whole-session backfill.
                    seg_rpe = core.get_rpe_from_segmented(
                        sid,
                        last_state["action_type"],
                        last_state["set"],
                    )
                    if seg_rpe is not None:
                        with core.label_lock:
                            core.label_state["rpe"] = seg_rpe
                        last_state["rpe"] = seg_rpe
                    with core.inter_set_rest_lock:
                        core.inter_set_rest_active = resumed_phase == "inter_set_rest"
                    self.weight_entry.setText(str(last_state["weight_kg"]))
                    self.rpe_label.setText(f"RPE: {last_state['rpe']}")
                    self._highlight_action(last_state["action_type"])
                    self._highlight_phase(resumed_phase)
                    self._highlight_rpe(last_state["rpe"])
                    self._sync_csv_preview_path(prefix="[接續] ")
                act = last_state["action_type"] if last_state else "?"
                phase = last_state.get("phase", "none") if last_state else "?"
                path = self._current_csv_preview_path()
                self._show_toast(f"接續收集 {sid}: {act}/{phase} → {path}", "#e67e22", 2200)
            else:
                self._show_toast(f"受試者 {sid} 將建立新檔案", "#27ae60")
        else:
            self._show_toast(f"受試者 ID 已設定: {sid}", "#27ae60")

        self._refresh_with_state(subject_id=sid)

    def set_rpe(self, value):
        with core.inter_set_rest_lock:
            in_inter_rest = core.inter_set_rest_active
        with core.big_rest_lock:
            in_big_rest = core.label_state.get("action_type") == "big_rest"

        if in_inter_rest or in_big_rest:
            core.pending_rpe_for_last_set = value

        self._refresh_with_state(rpe=value)
        self.rpe_label.setText(f"RPE: {value}")
        self._highlight_rpe(value)
        self._show_toast(f"RPE: {value}", "#e74c3c", 1500)

    def clear_rpe(self):
        self._refresh_with_state(rpe=0)
        self.rpe_label.setText("RPE: 0")
        self._highlight_rpe(-1)
        self._show_toast("RPE 已清除", "#e67e22", 1200)

    def clear_labels(self):
        with core.recording_lock:
            if core.is_recording:
                ans = QMessageBox.question(
                    self,
                    "確認清空",
                    "正在錄製中！確定要清空所有標記？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if ans != QMessageBox.Yes:
                    return

        core.reset_label_state()
        core.update_label_state(action_type=self._default_action)
        with core.inter_set_rest_lock:
            core.inter_set_rest_active = False
        with core.big_rest_lock:
            core.action_before_big_rest = None

        self.weight_entry.setText("0.0")
        self.subject_id_entry.setText("")
        self.rpe_label.setText("RPE: 0")
        self._highlight_action(self._default_action)
        self._highlight_phase("none")
        self._highlight_rpe(-1)
        self.update_status()
        self._show_toast("標記已清空", "#e74c3c")

    def new_subject(self):
        with core.recording_lock:
            currently_recording = core.is_recording

        if currently_recording:
            ans = QMessageBox.question(
                self,
                "確認結束受試者",
                "正在錄製中！確定要結束當前受試者？\n（將停止錄製並關閉所有檔案）",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ans != QMessageBox.Yes:
                return

        if core.pending_rpe_for_last_set is not None:
            with core.set_files_lock:
                if core.last_set_file_paths:
                    core.update_rpe_in_files(core.last_set_file_paths, core.pending_rpe_for_last_set)
            core.pending_rpe_for_last_set = None

        with core.recording_lock:
            if core.is_recording:
                core.is_recording = False
                self.rec_status_label.setText("[STOP] Stopped (prev subject)")
                self.rec_btn.setText("⏺ REC Start")
                self._set_button_color(self.rec_btn, None)
                self.csv_name_label.setText("")

        # Always close session files (and backfill whole-session RPE from segmented)
        # even if we resumed a session without currently recording.
        if core.whole_session_file or core.csv_files:
            core.close_all_csv()
            self.rec_status_label.setText("[STOP] Stopped (prev subject)")
            self.rec_btn.setText("⏺ REC Start")
            self._set_button_color(self.rec_btn, None)
            self.csv_name_label.setText("")

        with core.fusion_lock:
            core.fusion_ahrs.reset()
            core.prev_sensor_ts = None

        core.whole_session_file = None
        with core.inter_set_rest_lock:
            core.inter_set_rest_active = False
        with core.big_rest_lock:
            core.action_before_big_rest = None

        next_id = core._auto_next_subject_id(core.DATA_DIR)
        core.reset_label_state()
        self.weight_entry.setText("0.0")
        self.subject_id_entry.setText(next_id)
        core.update_label_state(subject_id=next_id, action_type=self._default_action)
        self.rpe_label.setText("RPE: 0")
        self._highlight_action(self._default_action)
        self._highlight_phase("none")
        self._highlight_rpe(-1)
        self.update_status()
        self.rec_status_label.setText("[READY] Press REC to start new subject")
        self._show_toast(f"受試者已結束，下一位 ID: {next_id}", "#9b59b6")

    def closeEvent(self, event):
        with core.recording_lock:
            core.is_recording = False
        core.close_all_csv()
        super().closeEvent(event)


def main():
    ap = argparse.ArgumentParser(description="IMU Label GUI (PySide6)")
    ap.add_argument(
        "--input",
        default="-",
        help="Path to file to read raw IMU CSV from, or '-' for stdin (default: stdin)",
    )
    args = ap.parse_args()

    if args.input == "-":
        reader = threading.Thread(target=core.stdin_reader, daemon=True)
    else:
        reader = threading.Thread(target=core.file_reader, args=(args.input,), daemon=True)
    reader.start()

    app = QApplication(sys.argv)

    # Allow Ctrl+C to terminate the Qt event loop
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    # Qt blocks Python signal handling; a short timer lets Python check signals
    _sig_timer = QTimer()
    _sig_timer.start(200)
    _sig_timer.timeout.connect(lambda: None)

    gui = LabelGUIQt()
    gui.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
