## 快速開始（新電腦首次設定）

### macOS / Linux

```sh
chmod +x setup.sh
./setup.sh
```

腳本會檢查 Zig / 編譯器 / Python，建立 `.venv`、安裝 Python 套件並完成編譯。

### Windows

建議用 Git Bash / MSYS2 來跑 `setup.sh`：

```sh
bash setup.sh
```

前置需求（請先自行安裝）：Zig、Python 3.8+、以及 C/C++ 編譯器（MSVC Build Tools 或 MinGW）。

註：若使用 WSL，藍牙/USB 可能需要額外設定。

## 可用指令

```sh
zig build run          # 接收 BLE 資料並存檔 (Raw_data.csv)
zig build workout      # 運動標記 GUI
zig build running      # 跑步實驗標記 GUI
zig build ppg-check    # PPG 品質檢查
zig build ppg-check-cal # PPG 品質檢查（含校正）
```

## 快捷啟動（setup.sh 建立後可用）

```sh
./bin/imu-workout      # 直接啟動運動標記 GUI
./bin/imu-running      # 直接啟動跑步實驗 GUI
./bin/imu-record       # 直接啟動錄製
```