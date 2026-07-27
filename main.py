import sys
import os
import json
import difflib
import re
import time
from math import floor

import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QGraphicsOpacityEffect,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QInputDialog
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QRect, QPoint
from PyQt5.QtGui import QPainter, QPen, QColor, QCursor

from PIL import ImageGrab
import cv2
import numpy as np
import pytesseract

from hud import Ui_MainWindow as hudUI
from mainwindow import Ui_MainWindow as mainUI
from fetch import parse

def get_app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(get_app_dir(), "config.json")
VALUES_PATH = os.path.join(get_app_dir(), "mm2_values.json")

SLOT_WIDTH = 70
SLOT_BOX_HEIGHT = 90
VALUE_PLATE_HEIGHT = 20
CORR_WIDGET_HEIGHT = 18

TOTAL_SLOT_HEIGHT = VALUE_PLATE_HEIGHT + SLOT_BOX_HEIGHT + CORR_WIDGET_HEIGHT

SUB_REGIONS = {
    "quantity": (0.60, 0.05, 1.0, 0.25),
    "chroma":   (0.0, 0.50, 0.55, 0.80),
    "name":     (0.0, 0.75, 1.0, 1.0),
}


class ValuesDatabase:
    def __init__(self):
        self.items = []
        self.by_name = {}
        self._load()

    def _load(self):
        if os.path.exists(VALUES_PATH):
            with open(VALUES_PATH, "r", encoding="utf-8") as f:
                self.items = json.load(f)
            self.by_name = {item["name"]: item for item in self.items}

    def reload(self):
        self._load()

    def find_exact(self, name):
        return self.by_name.get(name)

    def fuzzy_search(self, query, n=3, cutoff=0.6):
        names = list(self.by_name.keys())
        return difflib.get_close_matches(query, names, n=n, cutoff=cutoff)

    def try_variants(self, base_name):
        variants = [
            base_name,
            base_name + " (Gun)",
            base_name + " (Knife)",
        ]
        for v in variants:
            if v in self.by_name:
                return self.by_name[v]
        return None

    def has_variants(self, base_name):
        clean = base_name.replace(" (Gun)", "").replace(" (Knife)", "").strip()
        if not clean:
            return False
        gun_exists = (clean + " (Gun)") in self.by_name
        knife_exists = (clean + " (Knife)") in self.by_name
        return gun_exists or knife_exists


class SlotBoxWidget(QWidget):
    subregion_clicked = pyqtSignal(str)

    def __init__(self, border_color, parent=None):
        super().__init__(parent)
        self.border_color = border_color
        self.show_debug_borders = False
        self.setFixedSize(SLOT_WIDTH, SLOT_BOX_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        w = self.width()
        h = self.height()

        painter.setPen(QPen(self.border_color, 2))
        painter.drawRect(0, 0, w - 1, h - 1)

        if self.show_debug_borders:
            q_rel = SUB_REGIONS["quantity"]
            painter.setPen(QPen(QColor(255, 50, 50), 1, Qt.DashLine))
            painter.drawRect(
                int(q_rel[0] * w), int(q_rel[1] * h),
                int((q_rel[2] - q_rel[0]) * w) - 1, int((q_rel[3] - q_rel[1]) * h) - 1
            )

            c_rel = SUB_REGIONS["chroma"]
            painter.setPen(QPen(QColor(255, 0, 255), 1, Qt.DashLine))
            painter.drawRect(
                int(c_rel[0] * w), int(c_rel[1] * h),
                int((c_rel[2] - c_rel[0]) * w) - 1, int((c_rel[3] - c_rel[1]) * h) - 1
            )

            n_rel = SUB_REGIONS["name"]
            painter.setPen(QPen(QColor(0, 255, 128), 1, Qt.DashLine))
            painter.drawRect(
                int(n_rel[0] * w), int(n_rel[1] * h),
                int((n_rel[2] - n_rel[0]) * w) - 1, int((n_rel[3] - n_rel[1]) * h) - 1
            )


class TradeSlot(QWidget):
    value_changed = pyqtSignal()

    def __init__(self, values_db, border_color=QColor(125, 182, 255), value_at_bottom=False, parent=None):
        super().__init__(parent)
        self.values_db = values_db
        self.border_color = border_color
        self.value_at_bottom = value_at_bottom
        self.current_data = None
        self.current_value = 0
        self.qty = 1
        self.base_name = ""
        self.forced_suffix = ""
        self.is_chroma = False
        self.is_manual_override = False

        self.setFixedSize(SLOT_WIDTH, TOTAL_SLOT_HEIGHT)
        self._setup_ui()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.setAlignment(Qt.AlignTop)

        self.slot_box = SlotBoxWidget(self.border_color, self)
        self.slot_box.setFixedHeight(SLOT_BOX_HEIGHT)

        self.value_label = QLabel("0", self)
        self.value_label.setAlignment(Qt.AlignCenter)
        self.value_label.setFixedSize(SLOT_WIDTH, VALUE_PLATE_HEIGHT)
        self.value_label.setStyleSheet(f"""
            color: rgb(255, 255, 255);
            border: 1px solid {self.border_color.name()};
            background-color: rgb(58, 58, 58);
            font-size: 10px;
            font-weight: bold;
        """)

        self.corr_widget = QWidget(self)
        self.corr_widget.setFixedSize(SLOT_WIDTH, CORR_WIDGET_HEIGHT)
        self.corr_widget.setStyleSheet("background: transparent; border: none;")
        corr_layout = QHBoxLayout(self.corr_widget)
        corr_layout.setContentsMargins(0, 1, 0, 0)
        corr_layout.setSpacing(1)

        self.btn_gun = QPushButton("G")
        self.btn_knife = QPushButton("K")
        self.btn_none = QPushButton("-")

        for btn in (self.btn_gun, self.btn_knife, self.btn_none):
            btn.setStyleSheet("""
                color: white;
                background-color: rgb(40, 40, 40);
                border: 1px solid rgb(100, 100, 100);
                font-size: 8px;
                padding: 0px;
            """)
            btn.setFixedHeight(16)
            corr_layout.addWidget(btn)

        self.btn_gun.clicked.connect(lambda: self._apply_suffix(" (Gun)"))
        self.btn_knife.clicked.connect(lambda: self._apply_suffix(" (Knife)"))
        self.btn_none.clicked.connect(self.reset_manual_override)
        self.corr_widget.hide()

        if self.value_at_bottom:
            main_layout.addWidget(self.slot_box)
            main_layout.addWidget(self.value_label)
            main_layout.addWidget(self.corr_widget)
        else:
            main_layout.addWidget(self.value_label)
            main_layout.addWidget(self.slot_box)
            main_layout.addWidget(self.corr_widget)

    def reset_manual_override(self):
        self.is_manual_override = False
        self.forced_suffix = ""
        self.clear()

    def set_debug_borders(self, visible: bool):
        self.slot_box.show_debug_borders = visible
        self.slot_box.update()

    def set_item(self, name, is_chroma=False, qty=1, force=False):
        clean_name = name.strip() if name else ""
        if not clean_name:
            self.clear()
            return

        if self.is_manual_override and not force:
            return

        qty = max(1, int(qty))

        if self.base_name == clean_name and self.is_chroma == is_chroma and self.qty == qty:
            return

        self.base_name = clean_name
        self.is_chroma = is_chroma
        self.qty = qty

        self._resolve()

    def clear(self):
        self.current_data = None
        self.current_value = 0
        self.base_name = ""
        self.forced_suffix = ""
        self.is_chroma = False
        self.is_manual_override = False
        self.value_label.setText("0")
        self.corr_widget.hide()
        self.value_changed.emit()

    def set_scale(self, scale_pct):
        s = scale_pct / 100
        w = int(SLOT_WIDTH * s)
        h_val = int(VALUE_PLATE_HEIGHT * s)
        h_box = int(SLOT_BOX_HEIGHT * s)
        h_corr = int(CORR_WIDGET_HEIGHT * s)
        h_total = int(TOTAL_SLOT_HEIGHT * s)

        self.setFixedSize(w, h_total)
        self.value_label.setFixedSize(w, h_val)
        self.slot_box.setFixedSize(w, h_box)
        self.corr_widget.setFixedSize(w, h_corr)

        self.value_label.setStyleSheet(f"""
            color: rgb(255, 255, 255);
            border: 1px solid {self.border_color.name()};
            background-color: rgb(58, 58, 58);
            font-size: {max(8, int(10 * s))}px;
            font-weight: bold;
        """)

        for btn in (self.btn_gun, self.btn_knife, self.btn_none):
            btn.setFixedHeight(max(12, int(16 * s)))
            btn.setStyleSheet(f"""
                color: white;
                background-color: rgb(40, 40, 40);
                border: 1px solid rgb(100, 100, 100);
                font-size: {max(6, int(8 * s))}px;
                padding: 0px;
            """)

        self.update()

    def _resolve(self):
        if not self.base_name:
            self.clear()
            return

        clean = self.base_name.replace(" (Gun)", "").replace(" (Knife)", "").strip()

        if self.forced_suffix:
            lookup_name = clean + self.forced_suffix
        else:
            lookup_name = clean

        if self.is_chroma and not lookup_name.lower().startswith("chroma") and not lookup_name.lower().startswith("c."):
            lookup_name = "Chroma " + lookup_name

        data = self.values_db.find_exact(lookup_name)
        if data:
            self._set_found(data)
            return

        data = self.values_db.try_variants(lookup_name)
        if data:
            self._set_found(data)
            return

        matches = self.values_db.fuzzy_search(lookup_name)
        if matches:
            data = self.values_db.find_exact(matches[0])
            if data:
                self._set_found(data)
                return

        self.value_label.setText("?")
        self.corr_widget.show()
        self.current_data = None
        self.current_value = 0
        self.value_changed.emit()

    def _apply_suffix(self, suffix):
        self.forced_suffix = suffix
        if suffix:
            self.is_manual_override = True
        else:
            self.is_manual_override = False
        self._resolve()

    def _set_found(self, data):
        self.current_data = data
        raw_value = data.get("value", 0)
        self.current_value = raw_value * self.qty
        self.value_label.setText(str(self.current_value))

        if self.values_db.has_variants(data["name"]) or self.forced_suffix:
            self.corr_widget.show()
        else:
            self.corr_widget.hide()

        self.value_changed.emit()


class TradeScannerThread(QThread):
    slot_scanned = pyqtSignal(str, str, bool, int)

    def __init__(self, hud_window):
        super().__init__()
        self.hud_window = hud_window
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        while self.running:
            start_time = time.time()

            rects = self.hud_window.get_slot_global_rects()

            for slot_key, ((x1, y1), (x2, y2)) in rects.items():
                if x2 <= x1 or y2 <= y1:
                    continue

                w = x2 - x1
                h = y2 - y1

                try:
                    slot_img = ImageGrab.grab(bbox=(x1, y1, x2, y2))
                except Exception:
                    continue

                slot_np = np.array(slot_img)

                q_rel = SUB_REGIONS["quantity"]
                q_crop = slot_np[
                    int(q_rel[1] * h):int(q_rel[3] * h),
                    int(q_rel[0] * w):int(q_rel[2] * w)
                ]
                qty = self._parse_quantity(q_crop)

                c_rel = SUB_REGIONS["chroma"]
                c_crop = slot_np[
                    int(c_rel[1] * h):int(c_rel[3] * h),
                    int(c_rel[0] * w):int(c_rel[2] * w)
                ]
                is_chroma = self._parse_chroma(c_crop)

                n_rel = SUB_REGIONS["name"]
                n_crop = slot_np[
                    int(n_rel[1] * h):int(n_rel[3] * h),
                    int(n_rel[0] * w):int(n_rel[2] * w)
                ]
                name = self._parse_name(n_crop)

                self.slot_scanned.emit(slot_key, name, is_chroma, qty)

            elapsed = time.time() - start_time
            time.sleep(max(0.05, 0.5 - elapsed))

    def _parse_name(self, crop_rgb):
        if crop_rgb.size == 0:
            return ""
        try:
            hsv = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2HSV)
            s = hsv[:, :, 1]
            v = hsv[:, :, 2]

            white_mask = (v > 140) & (s < 90)
            thresh = np.zeros_like(s, dtype=np.uint8)
            thresh[white_mask] = 255

            scaled = cv2.resize(thresh, (0, 0), fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
            inv = cv2.bitwise_not(scaled)

            padded = cv2.copyMakeBorder(
                inv, 15, 15, 15, 15, 
                cv2.BORDER_CONSTANT, value=[255, 255, 255]
            )

            text = pytesseract.image_to_string(padded, config='--psm 7')
            clean_text = re.sub(r'[^a-zA-Z0-9\s\'\-]', '', text).strip()

            if len(clean_text) >= 2:
                return clean_text
        except Exception:
            pass
        return ""

    def _parse_quantity(self, crop_rgb):
        if crop_rgb.size == 0:
            return 1
        try:
            gray = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2GRAY)
            scaled = cv2.resize(gray, (0, 0), fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
            _, thresh = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            inv = cv2.bitwise_not(thresh)

            padded = cv2.copyMakeBorder(
                inv, 15, 15, 15, 15, 
                cv2.BORDER_CONSTANT, value=[255, 255, 255]
            )

            text = pytesseract.image_to_string(
                padded, 
                config='--psm 7 -c tessedit_char_whitelist=xX0123456789'
            )
            match = re.search(r'\d+', text)
            if not match:
                text_alt = pytesseract.image_to_string(
                    padded, 
                    config='--psm 8 -c tessedit_char_whitelist=xX0123456789'
                )
                match = re.search(r'\d+', text_alt)

            if match:
                return int(match.group())
        except Exception:
            pass
        return 1

    def _parse_chroma(self, crop_rgb):
        if crop_rgb.size == 0:
            return False
        try:
            hsv = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2HSV)
            sat = hsv[:, :, 1]
            hue = hsv[:, :, 0]

            if np.mean(sat) > 50 and np.std(hue) > 35:
                return True
        except Exception:
            pass
        return False


class UpdateThread(QThread):
    finished_ok = pyqtSignal(list)
    finished_err = pyqtSignal(str)

    def run(self):
        try:
            result = parse()
            if result is None:
                self.finished_err.emit("Parse failed")
            else:
                self.finished_ok.emit(result)
        except Exception as e:
            self.finished_err.emit(str(e))


class HUDWindow(QMainWindow, hudUI):
    position_changed = pyqtSignal()

    def __init__(self, values_db):
        super().__init__()
        self.values_db = values_db
        self.current_opacity = 1.0
        self.last_lbutton_state = False
        self.setupUi(self)

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
            Qt.SubWindow | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.dragged_widget = None
        self.drag_offset = None

        self._replace_slots()

        for slot in self.your_slots + self.their_slots:
            slot.value_changed.connect(self.recalculate_totals)

        self.hover_timer = QTimer(self)
        self.hover_timer.timeout.connect(self.check_global_mouse_hover)
        self.hover_timer.start(30)

    def _replace_slots(self):
        while self.youritems.count():
            item = self.youritems.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.your_slots = []
        self.slots_dict = {}

        for i in range(1, 5):
            slot = TradeSlot(
                self.values_db, 
                border_color=QColor(125, 182, 255), 
                value_at_bottom=False, 
                parent=self.horizontalLayoutWidget
            )
            self.youritems.addWidget(slot)
            self.your_slots.append(slot)
            self.slots_dict[f"youritem{i}"] = slot

        while self.theiritems.count():
            item = self.theiritems.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.their_slots = []
        for i in range(1, 5):
            slot = TradeSlot(
                self.values_db, 
                border_color=QColor(255, 120, 150), 
                value_at_bottom=True, 
                parent=self.horizontalLayoutWidget_2
            )
            self.theiritems.addWidget(slot)
            self.their_slots.append(slot)
            self.slots_dict[f"theiritem{i}"] = slot

        total_w = SLOT_WIDTH * 4
        self.horizontalLayoutWidget.resize(total_w, TOTAL_SLOT_HEIGHT)
        self.horizontalLayoutWidget_2.resize(total_w, TOTAL_SLOT_HEIGHT)

    def check_global_mouse_hover(self):
        cursor_pos = QCursor.pos()
        ratio = self.devicePixelRatioF()

        is_lbutton_down = bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
        just_clicked = is_lbutton_down and not self.last_lbutton_state
        self.last_lbutton_state = is_lbutton_down

        hovered_slot = None

        for name, slot in self.slots_dict.items():
            box = slot.slot_box
            top_left = box.mapToGlobal(QPoint(0, 0))

            box_rect = QRect(
                int(top_left.x() / ratio), int(top_left.y() / ratio),
                int(box.width()), int(box.height())
            )

            if box_rect.contains(cursor_pos):
                hovered_slot = slot

                if just_clicked:
                    rel_x = (cursor_pos.x() - box_rect.x()) / box_rect.width()
                    rel_y = (cursor_pos.y() - box_rect.y()) / box_rect.height()

                    clicked_region = "name"
                    for reg_name, (rx1, ry1, rx2, ry2) in SUB_REGIONS.items():
                        if rx1 <= rel_x <= rx2 and ry1 <= rel_y <= ry2:
                            clicked_region = reg_name
                            break

                    slot.prompt_subregion_input(clicked_region)

            if slot.corr_widget.isVisible() and just_clicked:
                corr = slot.corr_widget
                corr_top_left = corr.mapToGlobal(QPoint(0, 0))
                corr_rect = QRect(
                    int(corr_top_left.x() / ratio), int(corr_top_left.y() / ratio),
                    int(corr.width()), int(corr.height())
                )

                if corr_rect.contains(cursor_pos):
                    rel_x = (cursor_pos.x() - corr_rect.x()) / corr_rect.width()
                    if rel_x < 0.33:
                        slot._apply_suffix(" (Gun)")
                    elif rel_x < 0.66:
                        slot._apply_suffix(" (Knife)")
                    else:
                        slot.reset_manual_override()

        self.update_item_info(hovered_slot)

    def update_slot_from_scanner(self, slot_key, name, is_chroma, qty):
        slot = self.slots_dict.get(slot_key)
        if slot:
            slot.set_item(name, is_chroma, qty)

    def update_item_info(self, slot):
        if slot is None or not slot.base_name:
            self.iteminfo.setText("The info will display here as you hover on an item!")
            return

        data = slot.current_data
        chroma_tag = " [Chroma]" if slot.is_chroma else ""
        manual_tag = " [Manual]" if slot.is_manual_override else ""

        if data:
            info_text = (
                f"<b>{data['name']}{chroma_tag}{manual_tag}</b><br>"
                f"• Quantity: x{slot.qty}<br>"
                f"• Value: <b>{slot.current_value}</b> (Base: {data.get('value', 0)})<br>"
                f"• Type: {str(data.get('type', 'N/A')).capitalize()}<br>"
                f"• Demand: {data.get('demand', 'N/A')} | Rarity: {data.get('rarity', 'N/A')}<br>"
                f"• Stability: {data.get('stability', 'N/A')}"
            )
        else:
            info_text = (
                f"<b>{slot.base_name}{chroma_tag}{manual_tag}</b><br>"
                f"• Quantity: x{slot.qty}<br>"
                f"• Value: Unknown (?)"
            )

        self.iteminfo.setText(info_text)

    def recalculate_totals(self):
        your_total = sum(s.current_value for s in self.your_slots)
        their_total = sum(s.current_value for s in self.their_slots)
        profit = their_total - your_total

        self.yourtotal.setText(f"Your total: {your_total} values")
        self.theirtotal.setText(f"Their total: {their_total} values")

        color = "#00ff00" if profit >= 0 else "#ff4444"
        self.profit.setText(f"Profit: {profit} values")
        self.profit.setStyleSheet(f"""
            color: {color};
            border: 2px solid rgb(125, 182, 255);
            background-color: rgb(58, 58, 58);
        """)

    def get_slot_global_rects(self):
        rects = {}
        ratio = self.devicePixelRatioF()
        for name, slot in self.slots_dict.items():
            box = slot.slot_box
            top_left = box.mapToGlobal(box.rect().topLeft())
            bottom_right = box.mapToGlobal(box.rect().bottomRight())
            rects[name] = (
                (int(top_left.x() * ratio), int(top_left.y() * ratio)),
                (int(bottom_right.x() * ratio), int(bottom_right.y() * ratio))
            )
        return rects

    def get_positions(self):
        widgets = [
            "horizontalLayoutWidget",
            "horizontalLayoutWidget_2",
            "yourtotal",
            "theirtotal",
            "profit",
            "iteminfo"
        ]
        pos_dict = {}
        for name in widgets:
            if hasattr(self, name):
                w = getattr(self, name)
                pos_dict[name] = [w.x(), w.y()]
        return pos_dict

    def restore_positions(self, pos_dict):
        for name, pos in pos_dict.items():
            if hasattr(self, name):
                widget = getattr(self, name)
                if isinstance(pos, (list, tuple)) and len(pos) == 2:
                    widget.move(pos[0], pos[1])

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            child = self.childAt(event.pos())
            if child:
                parent = child
                while parent and parent != self:
                    if parent in [
                        self.horizontalLayoutWidget,
                        self.horizontalLayoutWidget_2,
                        self.yourtotal,
                        self.theirtotal,
                        self.profit,
                        self.iteminfo,
                    ]:
                        self.dragged_widget = parent
                        self.drag_offset = event.pos() - parent.pos()
                        break
                    parent = parent.parentWidget()

    def mouseMoveEvent(self, event):
        if self.dragged_widget and self.drag_offset:
            new_pos = event.pos() - self.drag_offset
            self.dragged_widget.move(new_pos)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self.dragged_widget:
                self.position_changed.emit()
            self.dragged_widget = None
            self.drag_offset = None


class MainWindow(QMainWindow, mainUI):
    def __init__(self):
        super().__init__()
        self.setupUi(self)

        self.values_db = ValuesDatabase()
        self.hud_window = HUDWindow(self.values_db)

        self.scanner_thread = TradeScannerThread(self.hud_window)
        self.scanner_thread.slot_scanned.connect(self.hud_window.update_slot_from_scanner)
        self.scanner_thread.start()

        self.baseX = SLOT_WIDTH * 4
        self.baseY = TOTAL_SLOT_HEIGHT

        self.editMode = False

        self.hudscale.valueChanged.connect(self.on_scale_changed)
        self.hudopacity.valueChanged.connect(self.on_opacity_changed)
        self.hud_window.position_changed.connect(self.save_config)

        self.editmode.clicked.connect(self.toggle_hud_edit)
        self.update.clicked.connect(self.run_update)

        if self.values_db.items:
            self.lastupdate.setText(f"Loaded {len(self.values_db.items)} items")
        else:
            self.lastupdate.setText("No data (update the values plz)")

        self.load_config()

    def on_scale_changed(self, value):
        self.edit_hud_scale(value)
        self.save_config()

    def on_opacity_changed(self, value):
        self.edit_hud_opacity(value)
        self.save_config()

    def edit_hud_scale(self, value):
        s = value / 100
        new_w = int(self.baseX * s)
        new_h = int(self.baseY * s)

        self.hud_window.horizontalLayoutWidget.resize(new_w, new_h)
        self.hud_window.horizontalLayoutWidget_2.resize(new_w, new_h)

        for slot in self.hud_window.your_slots + self.hud_window.their_slots:
            slot.set_scale(value)

    def edit_hud_opacity(self, value):
        self.hud_window.current_opacity = value / 100
        self.hud_window.setWindowOpacity(self.hud_window.current_opacity)

    def toggle_hud_edit(self):
        if not self.editMode:
            self.editMode = True
            self.editmode.setText("Exit edit mode")
            self.hud_window.setWindowFlags(
                self.hud_window.windowFlags() & ~Qt.WindowTransparentForInput
            )
            for slot in self.hud_window.your_slots + self.hud_window.their_slots:
                slot.set_debug_borders(True)
            self.hud_window.show()
        else:
            self.editMode = False
            self.editmode.setText("Enter edit mode")
            self.hud_window.setWindowFlags(
                self.hud_window.windowFlags() | Qt.WindowTransparentForInput
            )
            for slot in self.hud_window.your_slots + self.hud_window.their_slots:
                slot.set_debug_borders(False)
            self.hud_window.show()

    def save_config(self):
        tesseract_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    tesseract_path = cfg.get("tesseract", tesseract_path)
            except Exception:
                pass

        config_data = {
            "tesseract": tesseract_path,
            "scale": self.hudscale.value(),
            "opacity": self.hudopacity.value(),
            "positions": self.hud_window.get_positions(),
        }

        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"config save error: {e}")

    def load_config(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)

                pytesseract.pytesseract.tesseract_cmd = cfg.get("tesseract", r"C:\Program Files\Tesseract-OCR\tesseract.exe")

                self.hudscale.blockSignals(True)
                self.hudopacity.blockSignals(True)

                scale_val = cfg.get("scale", 100)
                opacity_val = cfg.get("opacity", 100)

                self.hudscale.setValue(scale_val)
                self.hudopacity.setValue(opacity_val)

                self.edit_hud_scale(scale_val)
                self.edit_hud_opacity(opacity_val)

                pos_dict = cfg.get("positions", {})
                if pos_dict:
                    self.hud_window.restore_positions(pos_dict)

                self.hudscale.blockSignals(False)
                self.hudopacity.blockSignals(False)
            except Exception as e:
                print(f"config load error: {e}")

    def run_update(self):
        self.update.setEnabled(False)
        self.update.setText("Updating...")
        self.lastupdate.setText("Fetching values...")

        self.thread = UpdateThread()
        self.thread.finished_ok.connect(self._on_update_ok)
        self.thread.finished_err.connect(self._on_update_err)
        self.thread.start()

    def _on_update_ok(self, data):
        self.values_db.reload()
        self.lastupdate.setText(f"Updated: {len(data)} items")
        self.update.setText("Update values")
        self.update.setEnabled(True)

    def _on_update_err(self, msg):
        self.lastupdate.setText(f"Error: {msg}")
        print(msg)
        self.update.setText("Update values")
        self.update.setEnabled(True)

    def closeEvent(self, event):
        self.save_config()
        self.scanner_thread.stop()
        self.scanner_thread.wait()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)

    main_menu = MainWindow()
    main_menu.show()
    main_menu.hud_window.show()

    sys.exit(app.exec_())