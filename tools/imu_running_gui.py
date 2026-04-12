#!/usr/bin/env python3
"""
Running experiment GUI (PySide6).
Qt-based UI that reuses backend logic from imu_running_core.py.
"""

import argparse
import os
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

import imu_running_core as core


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
    "stage": "#27ae60",
    "speed": "#e67e22",
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


class RunningLabelGUIQt(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("跑步實驗 - IMU Data Collection (Qt)")
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
        self.rec_btn = QPushButton("[REC] Start")
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

        stage_box = QGroupBox("階段控制 (按數字鍵 1-5)")
        stage_layout = QHBoxLayout(stage_box)
        self.stage_label = QLabel("階段: 0")
        self.stage_label.setStyleSheet(f"color: {_CLR['stage']}; font-size:16px; font-weight:700;")
        stage_layout.addWidget(self.stage_label)
        self._stage_btns = {}
        for i in range(1, 6):
            btn = QPushButton(str(i))
            btn.setFixedWidth(40)
            btn.clicked.connect(lambda checked=False, v=i: self.set_stage(v))
            self._stage_btns[i] = btn
            stage_layout.addWidget(btn)
        stage_layout.addStretch(1)

        subject_stage_row = QHBoxLayout()
        subject_stage_row.addWidget(subject_box, 2)
        subject_stage_row.addWidget(stage_box, 2)
        self.content_layout.addLayout(subject_stage_row)

        speed_box = QGroupBox("加速控制 (僅階段 3 有效，初始 8.0，每次 +0.5，最高 12.0，之後加坡度)")
        speed_layout = QHBoxLayout(speed_box)
        self.speed_label = QLabel("速度: N/A")
        self.speed_label.setStyleSheet(f"color: {_CLR['speed']}; font-size:16px; font-weight:700;")
        speed_layout.addWidget(self.speed_label)
        speed_btn = QPushButton("加速 +0.5 (a)")
        speed_btn.clicked.connect(self.inc_speed)
        speed_layout.addWidget(speed_btn)
        speed_layout.addStretch(1)
        self.content_layout.addWidget(speed_box)

        quick_box = QGroupBox("快速控制")
        quick_layout = QHBoxLayout(quick_box)
        clear_btn = QPushButton("清空標記")
        clear_btn.clicked.connect(self.clear_labels)
        new_btn = QPushButton("[NEW] 新受試者")
        new_btn.clicked.connect(self.new_subject)
        quick_layout.addWidget(clear_btn)
        quick_layout.addWidget(new_btn)
        quick_layout.addStretch(1)

        info_box = QGroupBox("快捷鍵")
        info_layout = QVBoxLayout(info_box)
        info = QLabel(
            "1-5 = 設定階段\n"
            "a = 加速 +0.5 (階段=3，8.0→12.0，超過12→加坡度)\n"
            "n = 結束受試者（關閉檔案）    x = 清空標記\n"
            "space = 暫停/繼續錄製"
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

        for i in range(1, 6):
            bind(str(i), lambda v=i: self.set_stage(v))
        bind("A", self.inc_speed)
        bind("N", self.new_subject)
        bind("X", self.clear_labels)
        bind("Space", self.toggle_recording)
        bind("Escape", self._clear_input_focus)

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

    def _highlight_stage(self, active_val):
        for val, btn in self._stage_btns.items():
            self._set_button_color(btn, _CLR["stage"] if val == active_val else None)

    def _show_toast(self, msg, color="#27ae60", duration=2500):
        self.toast_label.setText(f"  {msg}  ")
        self.toast_label.setStyleSheet(
            f"background:{color}; color:white; font-weight:700; padding:6px; border-radius:6px;"
        )
        self._toast_timer.start(duration)

    def _hide_toast(self):
        self.toast_label.setText("")
        self.toast_label.setStyleSheet(f"background:{_CLR['bg']}; color:white; padding:0px;")

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

    def _stop_stopwatch(self):
        if self._stopwatch_timer.isActive():
            self._stopwatch_timer.stop()
        self.stopwatch_toggle_btn.setText("開始")

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

        self._refresh_rec_button_state()

    def update_status(self):
        labels = core.get_label_snapshot()
        stage = labels["stage"]
        speed = labels["speed_level"]
        incline = labels["incline"]

        if stage == 3:
            if incline > 0:
                speed_text = f"  |  加坡度: {incline} (速度: {speed:.1f})"
            else:
                speed_text = f"  |  速度: {speed:.1f}"
        else:
            speed_text = ""

        self.status_label.setText(
            f"受試者: {labels['subject_id'] or '(未設定)'}  |  階段: {stage}{speed_text}"
        )
        self.stage_label.setText(f"階段: {stage}")
        if stage == 3:
            if incline > 0:
                self.speed_label.setText(f"加坡度: {incline} (速度: {speed:.1f})")
            else:
                self.speed_label.setText(f"速度: {speed:.1f}")
        else:
            self.speed_label.setText("速度: N/A (僅階段3)")

        self._highlight_stage(stage)

    def _update_csv_path_display(self):
        labels = core.get_label_snapshot()
        subj = labels["subject_id"]
        stage = labels["stage"]
        speed = labels["speed_level"]
        incline = labels["incline"]
        if subj:
            if stage == 3 and incline > 0:
                self.csv_name_label.setText(f"data_running/{subj}/stage{stage}/incline{incline}/")
            elif stage == 3 and speed > 0:
                self.csv_name_label.setText(f"data_running/{subj}/stage{stage}/speed{speed:.1f}/")
            else:
                self.csv_name_label.setText(f"data_running/{subj}/stage{stage}/")

    def apply_subject_id(self):
        sid = self.subject_id_entry.text().strip()
        if not sid:
            QMessageBox.warning(self, "警告", "受試者 ID 不能為空！")
            return

        with core.recording_lock:
            if core.is_recording:
                ans = QMessageBox.question(
                    self,
                    "確認",
                    f"正在錄製中！確定要切換受試者為 {sid}？\n（建議先按 [NEW] 結束當前受試者）",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if ans != QMessageBox.Yes:
                    return

        resume_path = core._find_latest_session_file(core.DATA_DIR, sid)
        if resume_path:
            last_state = core._read_last_row_state(resume_path, {"stage": 0, "speed_level": 0.0, "incline": 0})
            state_info = ""
            if last_state:
                state_info = f"\n\n上次狀態: 階段 {last_state['stage']}"
                if last_state["stage"] == 3:
                    state_info += f", 速度 {last_state['speed_level']:.1f}"
                    if last_state["incline"] > 0:
                        state_info += f", 坡度 {last_state['incline']}"
            ans = QMessageBox.question(
                self,
                "發現既有數據",
                f"受試者 {sid} 已有數據：\n{os.path.basename(resume_path)}{state_info}\n\n要接續收集數據嗎？\n（是＝接續，否＝建立新檔案）",
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
                    with core.label_lock:
                        core.label_state["stage"] = last_state["stage"]
                        core.label_state["speed_level"] = last_state["speed_level"]
                        core.label_state["incline"] = last_state["incline"]
                    self._highlight_stage(last_state["stage"])
                stage_str = str(last_state["stage"]) if last_state else "?"
                self._show_toast(f"接續收集 {sid}: 階段 {stage_str}", "#e67e22")
            else:
                self._show_toast(f"受試者 {sid} 將建立新檔案", "#27ae60")
        else:
            self._show_toast(f"受試者 ID 已設定: {sid}", "#27ae60")

        core.update_label_state(subject_id=sid)
        self.update_status()
        self._update_csv_path_display()

    def set_stage(self, value):
        if value < 1 or value > 5:
            return
        with core.label_lock:
            core.label_state["stage"] = value
            if value == 3:
                core.label_state["speed_level"] = 8.0
                core.label_state["incline"] = 0
            else:
                core.label_state["speed_level"] = 0.0
                core.label_state["incline"] = 0
        self._highlight_stage(value)
        self._show_toast(f"階段設定: {value}", "#27ae60", 1500)
        self.update_status()
        self._update_csv_path_display()

    def inc_speed(self):
        with core.label_lock:
            if core.label_state["stage"] != 3:
                self._show_toast("僅階段 3 可加速", "#e74c3c", 2000)
                return
            if core.label_state["incline"] > 0:
                core.label_state["incline"] += 1
                msg = f"坡度 +1 → 坡度 {core.label_state['incline']}"
            elif core.label_state["speed_level"] >= 12.0:
                core.label_state["incline"] = 1
                msg = "速度已達12.0，開始加坡度 → 坡度 1"
            else:
                core.label_state["speed_level"] += 0.5
                msg = f"速度 +0.5 → {core.label_state['speed_level']:.1f}"
        self._show_toast(msg, "#e67e22", 1500)
        self.update_status()
        self._update_csv_path_display()

    def toggle_recording(self):
        with core.recording_lock:
            if core.is_recording:
                core.is_recording = False
                self.rec_status_label.setText("[PAUSE] 暫停中")
                self.rec_btn.setText("[REC] Resume")
                self._set_button_color(self.rec_btn, _CLR["rec_pause"])
                self._stop_stopwatch()
                self._show_toast("錄製已暫停", "#e67e22")
            else:
                labels = core.get_label_snapshot()
                if not labels.get("subject_id"):
                    self.rec_status_label.setText("[ERROR] 請先設定受試者 ID!")
                    self._show_toast("請先設定受試者 ID！", "#e74c3c", 3000)
                    return
                if labels.get("stage", 0) == 0:
                    self._show_toast("提醒：尚未設定階段", "#e67e22", 3000)
                core.is_recording = True
                self.rec_status_label.setText("** RECORDING **")
                self.rec_btn.setText("[PAUSE] Pause")
                self._set_button_color(self.rec_btn, _CLR["rec_on"])
                self._update_csv_path_display()
                self._start_stopwatch()
                self._show_toast("開始錄製", "#27ae60")

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
        self.subject_id_entry.setText("")
        self.update_status()
        self._highlight_stage(0)
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

        with core.recording_lock:
            if core.is_recording:
                core.is_recording = False
                self.rec_status_label.setText("[STOP] Stopped (prev subject)")
                self.rec_btn.setText("[REC] Start")
                self._set_button_color(self.rec_btn, None)
                self.csv_name_label.setText("")

        # Always close session files (and segmented files) even if we resumed a
        # session without currently recording.
        if core.whole_session_file or core.csv_files:
            core.close_all_csv()
            self.rec_status_label.setText("[STOP] Stopped (prev subject)")
            self.rec_btn.setText("[REC] Start")
            self._set_button_color(self.rec_btn, None)
            self.csv_name_label.setText("")

        with core.fusion_lock:
            core.fusion_ahrs.reset()
            core.prev_sensor_ts = None

        core.whole_session_file = None
        self.reset_stopwatch()
        next_id = core._auto_next_subject_id(core.DATA_DIR)
        core.reset_label_state()
        self.subject_id_entry.setText(next_id)
        core.update_label_state(subject_id=next_id)
        self.update_status()
        self._update_csv_path_display()
        self.rec_status_label.setText("[READY] Press REC to start new subject")
        self._show_toast(f"受試者已結束，下一位 ID: {next_id}", "#9b59b6")

    def closeEvent(self, event):
        with core.recording_lock:
            core.is_recording = False
        core.close_all_csv()
        super().closeEvent(event)


def main():
    ap = argparse.ArgumentParser(description="Running Experiment IMU Label GUI (PySide6)")
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
    gui = RunningLabelGUIQt()
    gui.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
