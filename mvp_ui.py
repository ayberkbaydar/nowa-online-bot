import json
import os
import threading
import time
from datetime import datetime
import subprocess

import re
import pytesseract
from PIL import Image, ImageOps

import cv2
import numpy as np

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QStandardItemModel, QStandardItem, QPixmap
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QComboBox, QMessageBox, QGroupBox, QSpinBox, QPlainTextEdit
)

from appium import webdriver
from appium.options.android import UiAutomator2Options

import urllib.request
import urllib.error


UDID = "89U7N18410001296"
APPIUM_URL = "http://127.0.0.1:4723"
APP_PACKAGE = "com.NOWAGames.NOWAOnlineWorldA"
APP_ACTIVITY = "com.unity3d.player.UnityPlayerActivity"

CONFIG_PATH = "config.json"
SHOTS_DIR = "shots"
TARGETS_PATH = "targets.json"

def ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def load_config():
    default_cfg = {
        "tap_delay_ms": 250,
        "loop_delay_ms": 800,
        "coords": {
            "select_item_upgrade": None,
            "select_fast_upgrade": None,
            "select_accessory_upgrade": None,
            "select_close": None,

            "preview_button": None,
            "upgrade_button": None,
            "cancel_button": None,

            "upgrade_popup_ok": None,
            "upgrade_popup_cancel": None,

            "item_upgrade_close": None,

            "inventory_top_left": None,
            "inventory_bottom_right": None,

            "coords_roi_top_left": None,
            "coords_roi_bottom_right": None,

            "move_anchor": None,
        }
    }

    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(default_cfg, f, indent=2)
        return default_cfg

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    cfg.setdefault("coords", {})
    for k, v in default_cfg["coords"].items():
        cfg["coords"].setdefault(k, v)

    cfg.setdefault("tap_delay_ms", 250)
    cfg.setdefault("loop_delay_ms", 800)

    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def ensure_dirs():
    os.makedirs(SHOTS_DIR, exist_ok=True)

def load_targets():
    """
    targets.json -> dict[str, tuple[int,int]]
    Dosya yoksa boş dict döner.
    """
    if not os.path.exists(TARGETS_PATH):
        return {}

    with open(TARGETS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    targets = {}
    for name, xy in data.items():
        if not isinstance(xy, (list, tuple)) or len(xy) != 2:
            continue
        targets[name] = (int(xy[0]), int(xy[1]))
    return targets

def inv_slot_center(tl, br, rows=4, cols=7, row=0, col=0):
    tlx, tly = tl["x"], tl["y"]
    brx, bry = br["x"], br["y"]
    dx = (brx - tlx) / (cols - 1)
    dy = (bry - tly) / (rows - 1)
    x = int(round(tlx + col * dx))
    y = int(round(tly + row * dy))
    return x, y


def ocr_read_coords_from_image(image_path: str, roi_tl: dict, roi_br: dict):
    """
    ROI içinden koordinatı okur.
    Geri dönüş:
      - ((x,y), raw_text) veya (None, raw_text)
    """
    img = Image.open(image_path).convert("RGB")
    x1, y1 = int(roi_tl["x"]), int(roi_tl["y"])
    x2, y2 = int(roi_br["x"]), int(roi_br["y"])

    left, right = sorted([x1, x2])
    top, bottom = sorted([y1, y2])

    crop = img.crop((left, top, right, bottom))

    # OCR için büyütme
    scale = 4
    crop = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)

    # Debug: ROI crop kaydet
    debug_crop_path = os.path.join(SHOTS_DIR, f"{ts()}_coords_roi_crop.png")
    crop.save(debug_crop_path)

    # Grayscale + kontrast
    gray = ImageOps.grayscale(crop)
    gray = ImageOps.autocontrast(gray)

    def binarize(im, thr):
        return im.point(lambda p: 255 if p > thr else 0, mode="1")

    candidates = [
        ("thr170", binarize(gray, 170)),
        ("thr200", binarize(gray, 200)),
        ("inv_thr170", ImageOps.invert(gray).point(lambda p: 255 if p > 170 else 0, mode="1")),
        ("inv_thr200", ImageOps.invert(gray).point(lambda p: 255 if p > 200 else 0, mode="1")),
    ]

    best_raw = ""
    for tag, im in candidates:
        # Debug: binarized görüntü
        debug_bin_path = os.path.join(SHOTS_DIR, f"{ts()}_{tag}_coords_bin.png")
        im.convert("L").save(debug_bin_path)

        raw = pytesseract.image_to_string(
            im,
            config="--psm 7 -c tessedit_char_whitelist=0123456789,"
        ).strip()

        raw_clean = raw.replace(" ", "").replace("\n", "")
        m = re.search(r"(\d+),(\d+)", raw_clean)
        if m:
            return (int(m.group(1)), int(m.group(2))), f"{tag}:{raw_clean}"

        if len(raw_clean) > len(best_raw):
            best_raw = f"{tag}:{raw_clean}"

    return None, best_raw


class BotSignals(QObject):
    log = Signal(str)
    shot = Signal(str)

class UiSignals(QObject):
    log = Signal(str)

class NowaBot:
    def __init__(self, signals: BotSignals):
        self.signals = signals
        self.driver = None
        self.running = False
        self.thread = None

    def _log(self, msg: str):
        self.signals.log.emit(msg)

    def adb_launch_nowa(self):
        subprocess.run(["adb", "-s", UDID, "shell", "input", "keyevent", "224"], check=False)
        subprocess.run(["adb", "-s", UDID, "shell", "wm", "dismiss-keyguard"], check=False)

        cmd = ["adb", "-s", UDID, "shell", "am", "start", "-n", f"{APP_PACKAGE}/{APP_ACTIVITY}"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        self._log(f"ADB launch rc={r.returncode}")
        if r.stdout.strip():
            self._log("ADB stdout: " + r.stdout.strip())
        if r.stderr.strip():
            self._log("ADB stderr: " + r.stderr.strip())

    def connect(self):
        opts = UiAutomator2Options()
        opts.platform_name = "Android"
        opts.automation_name = "UiAutomator2"
        opts.udid = UDID

        opts.no_reset = True
        opts.new_command_timeout = 180
        opts.set_capability("disableWindowAnimation", True)
        opts.set_capability("ignoreHiddenApiPolicyError", True)

        self.driver = webdriver.Remote(APPIUM_URL, options=opts)
        self._log(f"Session started: {self.driver.session_id}")

        self.adb_launch_nowa()

        try:
            time.sleep(2)
            self._log(f"Foreground: {self.driver.current_package} / {self.driver.current_activity}")
        except Exception as e:
            self._log(f"Foreground check failed: {e}")

    def is_alive(self) -> bool:
        try:
            return self.driver is not None and self.driver.session_id is not None
        except Exception:
            return False

    def disconnect(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
            self._log("Session ended.")

    def take_screenshot(self, label="screen"):
        if not self.is_alive():
            self._log("Session not alive. Reconnecting...")
            self.disconnect()
            self.connect()

        try:
            path = os.path.join(SHOTS_DIR, f"{ts()}_{label}.png")
            self.driver.get_screenshot_as_file(path)
            self._log(f"Saved screenshot: {path}")
            self.signals.shot.emit(path)
            return path
        except Exception as e:
            self._log(f"Screenshot failed: {e}. Retrying with fresh session...")
            self.disconnect()
            self.connect()
            path = os.path.join(SHOTS_DIR, f"{ts()}_{label}_retry.png")
            self.driver.get_screenshot_as_file(path)
            self._log(f"Saved screenshot: {path}")
            self.signals.shot.emit(path)
            return path

    def tap(self, x: int, y: int, delay_ms: int):
        if not self.driver:
            raise RuntimeError("Driver not connected")
        self.driver.execute_script("mobile: clickGesture", {"x": int(x), "y": int(y)})
        time.sleep(delay_ms / 1000.0)

    def start_loop(self):
        if self.running:
            return
        if not self.is_alive():
            self.connect()

        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self._log("Bot loop started.")

    def stop_loop(self):
        self.running = False
        self._log("Bot loop stopping...")

    def _find_template_in_roi(
        self,
        screen_path: str,
        template_path: str,
        roi=(0.20, 0.25, 0.80, 0.80),
        threshold: float = 0.78
    ):
        screen = cv2.imread(screen_path, cv2.IMREAD_COLOR)
        templ = cv2.imread(template_path, cv2.IMREAD_COLOR)
        if screen is None or templ is None:
            raise RuntimeError("Screen/template okunamadı")

        H, W = screen.shape[:2]
        x1 = int(W * roi[0]); y1 = int(H * roi[1])
        x2 = int(W * roi[2]); y2 = int(H * roi[3])

        crop = screen[y1:y2, x1:x2]

        crop_g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        templ_g = cv2.cvtColor(templ, cv2.COLOR_BGR2GRAY)

        res = cv2.matchTemplate(crop_g, templ_g, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)

        if max_val < threshold:
            return None

        th, tw = templ_g.shape[:2]
        cx = x1 + max_loc[0] + tw // 2
        cy = y1 + max_loc[1] + th // 2
        return (int(cx), int(cy), float(max_val))

    def find_and_tap_open(
        self,
        template_path="assets/open_template.jpg",
        threshold: float = 0.78,
        roi=(0.20, 0.25, 0.80, 0.80),
        tap_delay_ms: int = 250
    ):
        shot = self.take_screenshot("find_open")
        hit = self._find_template_in_roi(shot, template_path, roi=roi, threshold=threshold)

        if not hit:
            self._log("OPEN not found (below threshold).")
            return False

        x, y, score = hit
        self._log(f"OPEN found at ({x},{y}) score={score:.3f} -> tapping")
        self.tap(x, y, tap_delay_ms)
        return True

    def _loop(self):
        cfg = load_config()
        coords = cfg["coords"]
        tap_delay = int(cfg.get("tap_delay_ms", 250))
        loop_delay = int(cfg.get("loop_delay_ms", 800))

        if not coords.get("upgrade_button"):
            self._log("ERROR: upgrade_button koordinatı set değil.")
            self.running = False
            return

        while self.running:
            try:
                ux, uy = coords["upgrade_button"]["x"], coords["upgrade_button"]["y"]
                self.tap(ux, uy, tap_delay)

                ok = coords.get("upgrade_popup_ok")
                if ok:
                    self.tap(ok["x"], ok["y"], tap_delay)

                time.sleep(loop_delay / 1000.0)

            except Exception as e:
                self._log(f"ERROR in loop: {e}")
                self.running = False
                break

        self._log("Bot loop stopped.")

    def hold(self, x: int, y: int, duration_ms: int = 600):
        if not self.driver:
            raise RuntimeError("Driver not connected")
        # Uzun basma (joystick çıkarmak için)
        self.driver.execute_script(
            "mobile: longClickGesture",
            {"x": int(x), "y": int(y), "duration": int(duration_ms)}
        )

    def drag(self, start_x: int, start_y: int, end_x: int, end_y: int, duration_ms: int = 800):
        if not self.driver:
            raise RuntimeError("Driver not connected")
        # Joystick yönü için sürükleme
        self.driver.execute_script(
            "mobile: dragGesture",
            {
                "startX": int(start_x),
                "startY": int(start_y),
                "endX": int(end_x),
                "endY": int(end_y),
                "duration": int(duration_ms)
            }
        )

    def _http_json(self, method: str, url: str, payload: dict | None):
        data = None
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                return resp.status, body
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTPError {e.code} {e.reason}: {body}") from e
        except Exception as e:
            raise RuntimeError(f"Request failed: {e}") from e


    def _post_w3c_actions_http(self, actions_payload: list):
        """
        Appium'a direkt HTTP ile /session/{id}/actions gönderir.
        Selenium/Appium client uyumsuzluklarını bypass eder.
        """
        if not self.driver or not self.driver.session_id:
            raise RuntimeError("No active session")

        sid = self.driver.session_id
        base = APPIUM_URL.rstrip("/")  # ör: http://127.0.0.1:4723
        post_url = f"{base}/session/{sid}/actions"
        delete_url = f"{base}/session/{sid}/actions"

        # POST actions
        self._http_json("POST", post_url, {"actions": actions_payload})
        # CLEAR actions (DELETE)
        self._http_json("DELETE", delete_url, None)


    def joystick_drag_w3c_raw(self, start_x: int, start_y: int, end_x: int, end_y: int, move_ms: int = 900):
        """
        Gerçek parmak: down -> pause -> move -> pause -> up
        """
        actions = [
            {
                "type": "pointer",
                "id": "finger1",
                "parameters": {"pointerType": "touch"},
                "actions": [
                    {"type": "pointerMove", "duration": 0, "x": int(start_x), "y": int(start_y)},
                    {"type": "pointerDown", "button": 0},
                    {"type": "pause", "duration": 300},
                    {"type": "pointerMove", "duration": int(move_ms), "x": int(end_x), "y": int(end_y)},
                    {"type": "pause", "duration": 350},
                    {"type": "pointerUp", "button": 0},
                ],
            }
        ]
        self._post_w3c_actions_http(actions)


    




class ClickableImage(QLabel):
    clicked = Signal(int, int)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(int(event.position().x()), int(event.position().y()))


class App(QWidget):
    def __init__(self):
        self.ui = UiSignals()
        self.ui.log.connect(self.append_log)
        super().__init__()
        ensure_dirs()
        self.cfg = load_config()

        self.signals = BotSignals()
        self.bot = NowaBot(self.signals)

        self.nav_running = False
        self.nav_thread = None

        self.setWindowTitle("Nowa Bot MVP")
        self.resize(1400, 950)

        self.image = ClickableImage("Screenshot yok. 'Screenshot' bas.")
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setStyleSheet("border: 1px solid #444;")
        self.image.clicked.connect(self.on_image_click)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log_view.setStyleSheet("font-family: monospace;")
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFixedHeight(180)
        self.log_view.appendPlainText("Log:")

        # Grouped ComboBox (Calibration)
        self.mode = QComboBox()
        model = QStandardItemModel()
        self.mode.setModel(model)

        def add_group(m, title, items):
            header = QStandardItem(title)
            header.setEnabled(False)
            header.setSelectable(False)
            header.setData("header", Qt.UserRole)
            m.appendRow(header)

            for key in items:
                item = QStandardItem(f"  {key}")
                item.setData(key, Qt.UserRole)
                m.appendRow(item)

            sep = QStandardItem("────────────")
            sep.setEnabled(False)
            sep.setSelectable(False)
            m.appendRow(sep)

        add_group(model, "[Menu]", [
            "select_item_upgrade",
            "select_fast_upgrade",
            "select_accessory_upgrade",
            "select_close"
        ])
        add_group(model, "[Actions]", [
            "preview_button",
            "upgrade_button",
            "cancel_button"
        ])
        add_group(model, "[Popup]", [
            "upgrade_popup_ok",
            "upgrade_popup_cancel"
        ])
        add_group(model, "[Close]", [
            "item_upgrade_close"
        ])
        add_group(model, "[Inventory]", [
            "inventory_top_left",
            "inventory_bottom_right"
        ])
        add_group(model, "[Coords]", [
            "coords_roi_top_left",
            "coords_roi_bottom_right"
        ])
        add_group(model, "[Movement]", [
            "move_anchor"
        ])

        model.removeRow(model.rowCount() - 1)
        self.mode.setCurrentIndex(1)

        # Test combobox
        self.test_mode = QComboBox()
        test_model = QStandardItemModel()
        self.test_mode.setModel(test_model)

        add_group(test_model, "[Menu]", [
            "select_item_upgrade",
            "select_fast_upgrade",
            "select_accessory_upgrade",
            "select_close"
        ])
        add_group(test_model, "[Actions]", [
            "preview_button",
            "upgrade_button",
            "cancel_button"
        ])
        add_group(test_model, "[Popup]", [
            "upgrade_popup_ok",
            "upgrade_popup_cancel"
        ])
        add_group(test_model, "[Close]", [
            "item_upgrade_close"
        ])
        add_group(test_model, "[Inventory]", [
            "inventory_top_left",
            "inventory_bottom_right"
        ])
        add_group(test_model, "[Coords]", [
            "coords_roi_top_left",
            "coords_roi_bottom_right"
        ])
        add_group(test_model, "[Movement]", [
            "move_anchor"
        ])

        test_model.removeRow(test_model.rowCount() - 1)
        self.test_mode.setCurrentIndex(1)

        # Buttons
        btn_connect = QPushButton("Connect + Launch Nowa")
        btn_connect.clicked.connect(self.on_connect)

        btn_shot = QPushButton("Screenshot")
        btn_shot.clicked.connect(self.on_screenshot)

        btn_find_open = QPushButton("Find & Tap Open")
        btn_find_open.clicked.connect(self.on_find_open)

        btn_read_coords = QPushButton("Read Coords")
        btn_read_coords.clicked.connect(self.on_read_coords)

        btn_test = QPushButton("Test Taps")
        btn_test.clicked.connect(self.on_test_taps)

        btn_start = QPushButton("Start Loop")
        btn_start.clicked.connect(self.on_start)

        btn_stop = QPushButton("Stop Loop")
        btn_stop.clicked.connect(self.on_stop)

        self.slot_idx = QSpinBox()
        self.slot_idx.setRange(0, 27)
        self.slot_idx.setValue(0)

        btn_tap_slot = QPushButton("Tap Slot")
        btn_tap_slot.clicked.connect(self.on_tap_slot)

        btn_hold_move = QPushButton("Hold Move")
        btn_hold_move.clicked.connect(self.on_hold_move)

        btn_move_right = QPushButton("Move Test → Right")
        btn_move_right.clicked.connect(self.on_move_test_right)

        btn_move_up = QPushButton("Move Test → Up")
        btn_move_up.clicked.connect(self.on_move_test_up)

        self.targets = load_targets()
        self.target_box = QComboBox()
        self.target_box.addItems(list(self.targets.keys()))

        btn_nav = QPushButton("Navigate")
        btn_nav.clicked.connect(self.on_navigate)

        btn_nav_stop = QPushButton("Stop Nav")
        btn_nav_stop.clicked.connect(self.on_stop_nav)

        # Layout
        top = QHBoxLayout()
        top.addWidget(btn_connect)
        top.addWidget(btn_shot)

        top.addSpacing(12)
        top.addWidget(btn_hold_move)
        top.addWidget(btn_move_right)
        top.addWidget(btn_move_up)

        top.addSpacing(12)
        top.addWidget(QLabel("Target:"))
        top.addWidget(self.target_box)
        top.addWidget(btn_nav)
        top.addWidget(btn_nav_stop)

        top.addSpacing(12)
        top.addWidget(btn_find_open)
        top.addWidget(btn_read_coords)

        top.addSpacing(12)
        top.addWidget(QLabel("Test hedefi:"))
        top.addWidget(self.test_mode)
        top.addWidget(btn_test)

        top.addSpacing(12)
        top.addWidget(QLabel("Slot:"))
        top.addWidget(self.slot_idx)
        top.addWidget(btn_tap_slot)

        top.addSpacing(20)
        top.addWidget(QLabel("Kalibrasyon hedefi:"))
        top.addWidget(self.mode)

        top.addStretch()
        top.addWidget(btn_start)
        top.addWidget(btn_stop)

        box = QGroupBox("Ekranda tıklayarak koordinat kaydet")
        vbox = QVBoxLayout(box)
        vbox.addWidget(QLabel(
            "1) Screenshot al\n"
            "2) Kalibrasyon hedefini seç\n"
            "3) Ekran görüntüsünde ilgili butona tıkla"
        ))
        vbox.addWidget(self.image)

        root = QVBoxLayout()
        root.addLayout(top)
        root.addWidget(box, 1)
        root.addWidget(self.log_view, 0)
        self.setLayout(root)

        # Signals
        self.signals.log.connect(self.append_log)
        self.signals.shot.connect(self.set_image)

        self.current_pixmap = None
        self.current_shot_path = None

    def append_log(self, msg: str):
        self.log_view.appendPlainText(msg)
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def on_connect(self):
        try:
            self.bot.connect()
            QMessageBox.information(self, "OK", "Connected + Launched.")
        except Exception as e:
            QMessageBox.critical(self, "Connect error", repr(e))

    def on_screenshot(self):
        try:
            if not self.bot.is_alive():
                QMessageBox.information(self, "Info", "Önce Connect + Launch Nowa yapıyorum.")
                self.bot.connect()
            self.current_shot_path = self.bot.take_screenshot("ui")
        except Exception as e:
            QMessageBox.critical(self, "Screenshot error", str(e))

    def on_find_open(self):
        def worker():
            try:
                if not self.bot.is_alive():
                    self.ui.log.emit("Session yok, Connect ediyorum...")
                    self.bot.connect()

                cfg = load_config()
                tap_delay = int(cfg.get("tap_delay_ms", 250))

                ok = self.bot.find_and_tap_open(
                    template_path="assets/open_template.jpg",
                    threshold=0.78,
                    roi=(0.20, 0.25, 0.80, 0.80),
                    tap_delay_ms=tap_delay
                )
                self.ui.log.emit("FindOpen: OK" if ok else "FindOpen: NOT FOUND")

            except Exception as e:
                self.ui.log.emit(f"FindOpen ERROR: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def on_test_taps(self):
        def worker():
            try:
                if not self.bot.is_alive():
                    self.ui.log.emit("Session yok, Connect ediyorum...")
                    self.bot.connect()

                cfg = load_config()
                coords = cfg.get("coords", {})
                tap_delay = int(cfg.get("tap_delay_ms", 250))

                idx = self.test_mode.currentIndex()
                item = self.test_mode.model().item(idx)
                key = item.data(Qt.UserRole) if item else None

                if not key:
                    self.ui.log.emit("Geçersiz test seçimi (grup başlığı/separator).")
                    return

                pt = coords.get(key)
                if not pt:
                    self.ui.log.emit(f"Coord yok: {key} (önce kalibre et)")
                    return

                self.ui.log.emit(f"=== Test Tap: {key} ({pt['x']},{pt['y']}) ===")
                self.bot.take_screenshot(f"before_{key}")

                self.bot.tap(int(pt["x"]), int(pt["y"]), tap_delay)
                time.sleep(0.35)

                self.bot.take_screenshot(f"after_{key}")
                self.ui.log.emit(f"=== OK: {key} ===")

            except Exception as e:
                self.ui.log.emit(f"TEST ERROR: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def on_tap_slot(self):
        def worker():
            try:
                if not self.bot.is_alive():
                    self.ui.log.emit("Session yok, Connect ediyorum...")
                    self.bot.connect()

                cfg = load_config()
                coords = cfg.get("coords", {})
                tl = coords.get("inventory_top_left")
                br = coords.get("inventory_bottom_right")

                if not tl or not br:
                    self.ui.log.emit("ERROR: inventory_top_left / inventory_bottom_right kalibre edilmemiş.")
                    return

                idx = int(self.slot_idx.value())
                row = idx // 7
                col = idx % 7

                x, y = inv_slot_center(tl, br, rows=4, cols=7, row=row, col=col)

                tap_delay = int(cfg.get("tap_delay_ms", 250))
                self.ui.log.emit(f"TAP SLOT idx={idx} (r={row}, c={col}) -> ({x},{y})")

                self.bot.take_screenshot(f"before_slot_{idx}")
                self.bot.tap(x, y, tap_delay)
                time.sleep(0.35)
                self.bot.take_screenshot(f"after_slot_{idx}")

            except Exception as e:
                self.ui.log.emit(f"TapSlot ERROR: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def on_read_coords(self):
        def worker():
            try:
                if not self.bot.is_alive():
                    self.ui.log.emit("Session yok, Connect ediyorum...")
                    self.bot.connect()

                cfg = load_config()
                coords = cfg.get("coords", {})
                tl = coords.get("coords_roi_top_left")
                br = coords.get("coords_roi_bottom_right")

                if not tl or not br:
                    self.ui.log.emit("ERROR: coords_roi_top_left / coords_roi_bottom_right kalibre edilmemiş.")
                    return

                shot = self.bot.take_screenshot("coords")
                res, raw = ocr_read_coords_from_image(shot, tl, br)
                if not res:
                    self.ui.log.emit(
                        f"Coords OCR FAILED. raw='{raw}'. ROI'yi küçült veya yazının etrafındaki ikonları dışarıda bırak."
                    )
                    return

                x, y = res
                self.ui.log.emit(f"Coords: {x},{y} (raw='{raw}')")

            except Exception as e:
                self.ui.log.emit(f"ReadCoords ERROR: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def set_image(self, path: str):
        pm = QPixmap(path)
        if pm.isNull():
            self.append_log("Failed to load screenshot into UI.")
            return
        self.current_pixmap = pm
        scaled = pm.scaled(self.image.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.image.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.current_pixmap:
            scaled = self.current_pixmap.scaled(self.image.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.image.setPixmap(scaled)

    def on_image_click(self, x: int, y: int):
        if not self.current_pixmap:
            self.append_log("Önce Screenshot al.")
            return

        label_w, label_h = self.image.width(), self.image.height()
        img_w, img_h = self.current_pixmap.width(), self.current_pixmap.height()

        scale = min(label_w / img_w, label_h / img_h)
        disp_w, disp_h = img_w * scale, img_h * scale
        offset_x = (label_w - disp_w) / 2
        offset_y = (label_h - disp_h) / 2

        ix = (x - offset_x) / scale
        iy = (y - offset_y) / scale

        if ix < 0 or iy < 0 or ix > img_w or iy > img_h:
            self.append_log("Tık görüntü dışında.")
            return

        idx = self.mode.currentIndex()
        item = self.mode.model().item(idx)
        target = item.data(Qt.UserRole)

        if not target:
            self.append_log("Geçersiz seçim (grup başlığı/separator).")
            return

        self.cfg["coords"][target] = {"x": int(ix), "y": int(iy)}
        save_config(self.cfg)
        self.append_log(f"Saved coord {target}: x={int(ix)} y={int(iy)}")

    def on_start(self):
        try:
            self.bot.start_loop()
        except Exception as e:
            QMessageBox.critical(self, "Start error", str(e))

    def on_stop(self):
        self.bot.stop_loop()

    def closeEvent(self, event):
        try:
            self.bot.stop_loop()
            self.bot.disconnect()
        finally:
            event.accept()

    def _get_move_anchor(self):
        cfg = load_config()
        coords = cfg.get("coords", {})
        anchor = coords.get("move_anchor")
        if not anchor:
            return None
        return anchor
    
    def on_hold_move(self):
        def worker():
            try:
                if not self.bot.is_alive():
                    self.ui.log.emit("Session yok, Connect ediyorum...")
                    self.bot.connect()

                anchor = self._get_move_anchor()
                if not anchor:
                    self.ui.log.emit("ERROR: move_anchor kalibre edilmemiş. Kalibrasyon hedefinden seçip tıkla.")
                    return

                self.ui.log.emit(f"HOLD move_anchor at ({anchor['x']},{anchor['y']})")
                self.bot.hold(anchor["x"], anchor["y"], duration_ms=700)
                self.bot.take_screenshot("hold_move")

            except Exception as e:
                self.ui.log.emit(f"HoldMove ERROR: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def on_move_test_right(self):
        self._run_move_test(direction="right")

    def on_move_test_up(self):
        self._run_move_test(direction="up")

    def _run_move_test(self, direction: str):
        def worker():
            try:
                if not self.bot.is_alive():
                    self.ui.log.emit("Session yok, Connect ediyorum...")
                    self.bot.connect()

                anchor = self._get_move_anchor()
                if not anchor:
                    self.ui.log.emit("ERROR: move_anchor kalibre edilmemiş.")
                    return

                # Başlangıç koordinatını oku
                cfg = load_config()
                coords = cfg.get("coords", {})
                tl = coords.get("coords_roi_top_left")
                br = coords.get("coords_roi_bottom_right")
                if not tl or not br:
                    self.ui.log.emit("ERROR: coords ROI kalibre edilmemiş (coords_roi_top_left/bottom_right).")
                    return

                shot1 = self.bot.take_screenshot("move_test_before")
                res1, raw1 = ocr_read_coords_from_image(shot1, tl, br)
                if not res1:
                    self.ui.log.emit(f"OCR before FAILED raw='{raw1}'")
                    return
                x1, y1 = res1

                ax, ay = anchor["x"], anchor["y"]
                R = 300  # joystick sürükleme mesafesi (gerekirse 120-220 arası oynarız)

                if direction == "right":
                    ex, ey = ax + R, ay
                elif direction == "up":
                    ex, ey = ax, ay - R
                else:
                    self.ui.log.emit("Unknown direction")
                    return

                self.ui.log.emit(f"MoveTest {direction}: from {x1},{y1} (raw={raw1})")
                self.ui.log.emit(f"Drag: ({ax},{ay}) -> ({ex},{ey})")

                self.bot.joystick_drag_w3c_raw(ax, ay, ex, ey, move_ms=900)

                # Koordinatlar seyrek güncelleniyor dedin: biraz bekleyelim
                time.sleep(1.3)

                shot2 = self.bot.take_screenshot("move_test_after")
                res2, raw2 = ocr_read_coords_from_image(shot2, tl, br)
                if not res2:
                    self.ui.log.emit(f"OCR after FAILED raw='{raw2}'")
                    return
                x2, y2 = res2

                dx = x2 - x1
                dy = y2 - y1
                self.ui.log.emit(f"After: {x2},{y2} (raw={raw2})  Δx={dx} Δy={dy}")

            except Exception as e:
                self.ui.log.emit(f"MoveTest ERROR: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def read_coords_value(self):
        """
        OCR ile mevcut koordinatı okur ve (x,y) döndürür.
        Başarısızsa (None, raw) döner.
        """
        cfg = load_config()
        coords = cfg.get("coords", {})
        tl = coords.get("coords_roi_top_left")
        br = coords.get("coords_roi_bottom_right")
        if not tl or not br:
            return None, "ROI not calibrated"

        shot = self.bot.take_screenshot("nav_coords")
        res, raw = ocr_read_coords_from_image(shot, tl, br)
        if not res:
            return None, raw
        return res, raw
    
    def on_stop_nav(self):
        self.nav_running = False
        if hasattr(self, "ui"):
            self.ui.log.emit("Nav: stop requested")
        else:
            # fallback
            self.append_log("Nav: stop requested")


    def on_navigate(self):
        def worker():
            try:
                if not self.bot.is_alive():
                    self.ui.log.emit("Nav: session yok, Connect ediyorum...")
                    self.bot.connect()

                cfg = load_config()
                c = cfg.get("coords", {})
                anchor = c.get("move_anchor")
                if not anchor:
                    self.ui.log.emit("Nav ERROR: move_anchor kalibre edilmemiş.")
                    return

                key = self.target_box.currentText()
                if not key or key not in self.targets:
                    self.ui.log.emit("Nav ERROR: Target seçili değil veya targets.json bulunamadı.")
                    return

                tx, ty = self.targets[key]


                # parametreler (MVP default)
                R = 300          # joystick çekme mesafesi
                step_ms = 950    # her adım yürüsün (seyrek update için)
                settle_s = 1.25  # yürüdükten sonra coords update bekle
                arrive_eps = 2   # hedefe varma eşiği

                self.nav_running = True
                self.ui.log.emit(f"Nav: START -> {key} target=({tx},{ty})")

                # döngü
                max_iters = 120  # güvenlik
                it = 0

                while self.nav_running and it < max_iters:
                    it += 1

                    res, raw = self.read_coords_value()
                    if not res:
                        self.ui.log.emit(f"Nav OCR FAIL raw='{raw}' (iter={it})")
                        time.sleep(0.6)
                        continue

                    x, y = res
                    dx = tx - x
                    dy = ty - y

                    self.ui.log.emit(f"Nav iter={it} cur=({x},{y}) dx={dx} dy={dy} raw={raw}")

                    # vardık mı?
                    if abs(dx) <= arrive_eps and abs(dy) <= arrive_eps:
                        self.ui.log.emit("Nav: ARRIVED ✅")
                        self.nav_running = False
                        break

                    ax, ay = int(anchor["x"]), int(anchor["y"])

                    # Hangi ekseni önce düzeltelim?
                    # büyük farkı önce kapatmak daha hızlı
                    if abs(dx) >= abs(dy):
                        # X düzelt
                        if dx > 0:
                            # Up drag (X artırır)
                            ex, ey = ax, ay - R
                            self.ui.log.emit("Nav move: UP (increase X)")
                        else:
                            # Down drag (X azaltır)
                            ex, ey = ax, ay + R
                            self.ui.log.emit("Nav move: DOWN (decrease X)")
                    else:
                        # Y düzelt
                        if dy > 0:
                            # Left drag (Y artırır)
                            ex, ey = ax - R, ay
                            self.ui.log.emit("Nav move: LEFT (increase Y)")
                        else:
                            # Right drag (Y azaltır)
                            ex, ey = ax + R, ay
                            self.ui.log.emit("Nav move: RIGHT (decrease Y)")

                    # joystick drag (HTTP raw actions kullanan metodun)
                    self.bot.joystick_drag_w3c_raw(ax, ay, ex, ey, move_ms=step_ms)

                    # update bekle
                    time.sleep(settle_s)

                if it >= max_iters:
                    self.ui.log.emit("Nav: stopped (max_iters reached)")
                self.nav_running = False

            except Exception as e:
                self.nav_running = False
                self.ui.log.emit(f"Nav ERROR: {e}")

        # aynı anda iki nav thread açılmasın
        if self.nav_running:
            self.ui.log.emit("Nav already running.")
            return

        self.nav_thread = threading.Thread(target=worker, daemon=True)
        self.nav_thread.start()


def main():
    app = QApplication([])
    w = App()
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
