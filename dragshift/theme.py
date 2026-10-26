"""Quiet native typography and neutral surfaces shared by DragShift windows."""
from __future__ import annotations

import sys
from pathlib import Path

PANEL = "#fbfbfa"
TEXT = "#252724"
MUTED = "#4f514d"
BORDER = "#e2e3de"
HOVER = "#eeefeb"
SELECTED = "#e6e8e2"
ACCENT = TEXT
GOOD = "#3e5742"
ERROR = "#774943"

_QSS = f"""
* {{ color: {TEXT}; font-family: "@FONT@"; font-size: 10pt;
    selection-background-color: {SELECTED}; selection-color: {TEXT}; }}
QWidget#glassRoot {{ background: transparent; }}
QLabel {{ background: transparent; }}
QLabel#title {{ font-size: 11pt; font-weight: 600; }}
QLabel#dim, QLabel.dim {{ color: {MUTED}; font-size: 9pt; }}
QLabel#section {{ color: {MUTED}; font-size: 9pt; font-weight: 600; }}
QLabel#big {{ font-size: 17pt; font-weight: 600; }}
QLabel#good {{ color: {GOOD}; font-weight: 600; }}
QLabel#warn, QLabel#error, QLabel#videoError {{ color: {ERROR}; }}
QPushButton {{ background: #ffffff; color: {TEXT}; border: 1px solid {BORDER};
    border-radius: 6px; padding: 6px 12px; min-height: 20px; }}
QPushButton:hover {{ background: {HOVER}; }}
QPushButton:pressed, QPushButton:checked {{ background: {SELECTED}; border-color: {MUTED}; }}
QPushButton:focus {{ border-color: {ACCENT}; }}
QPushButton#primary {{ background: {ACCENT}; color: #ffffff; border-color: {ACCENT};
    font-weight: 600; padding: 7px 16px; }}
QPushButton#primary:hover {{ background: #3d403b; border-color: #3d403b; }}
QPushButton#primary:pressed {{ background: #181a17; border-color: #181a17; }}
QPushButton#primary:focus {{ border: 2px solid {MUTED}; padding: 6px 15px; }}
QPushButton:disabled, QPushButton#primary:disabled {{ color: {MUTED}; background: {HOVER};
    border: 1px solid {BORDER}; }}
QPushButton#chrome {{ background: transparent; color: {MUTED}; border: 1px solid transparent;
    padding: 2px 6px; min-width: 22px; min-height: 22px; font-size: 13pt; }}
QPushButton#chrome:hover {{ background: {HOVER}; color: {TEXT}; }}
QPushButton#chrome:focus {{ border-color: {ACCENT}; }}
QPushButton#swatch {{ border-radius: 4px; padding: 0; min-width: 24px; max-width: 24px;
    min-height: 24px; max-height: 24px; }}
QPushButton#swatch:checked {{ border: 2px solid {ACCENT}; }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {MUTED}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: #ffffff; border: 1px solid {MUTED};
    width: 14px; height: 14px; margin: -6px 0; border-radius: 7px; }}
QSlider::handle:horizontal:hover, QSlider::handle:horizontal:focus {{ border: 2px solid {ACCENT}; }}
QSlider::sub-page:horizontal:disabled {{ background: {BORDER}; }}
QSpinBox, QDoubleSpinBox, QLineEdit, QComboBox, QPlainTextEdit, QTextEdit {{
    color: {TEXT}; background: #ffffff; border: 1px solid {BORDER};
    border-radius: 6px; padding: 5px 8px; }}
QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {ACCENT}; }}
QSpinBox:disabled, QDoubleSpinBox:disabled, QLineEdit:disabled, QComboBox:disabled,
QPlainTextEdit:disabled, QTextEdit:disabled {{ color: {MUTED}; background: {HOVER}; }}
QSpinBox QLineEdit, QDoubleSpinBox QLineEdit {{ border: none; padding: 0; background: transparent; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 28px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border;
    subcontrol-position: top right; width: 22px; border-left: 1px solid {BORDER};
    border-top-right-radius: 6px; background: transparent; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border;
    subcontrol-position: bottom right; width: 22px; border-left: 1px solid {BORDER};
    border-bottom-right-radius: 6px; background: transparent; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{ background: {HOVER}; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("@ASSETS@/control-chevron-up.xpm");
    width: 12px; height: 8px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url("@ASSETS@/control-chevron-down.xpm");
    width: 12px; height: 8px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right;
    border: none; width: 26px; }}
QComboBox::down-arrow {{ image: url("@ASSETS@/control-chevron-down.xpm"); width: 12px; height: 8px; }}
QComboBox QAbstractItemView {{ background: #ffffff; color: {TEXT}; border: 1px solid {BORDER};
    selection-background-color: {SELECTED}; selection-color: {TEXT}; }}
QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 3px;
    border: 1px solid {MUTED}; background: #ffffff; }}
QCheckBox::indicator:checked, QCheckBox::indicator:indeterminate {{ background: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ image: url("@ASSETS@/control-check.xpm"); }}
QCheckBox::indicator:hover, QCheckBox::indicator:focus {{ border: 1px solid {ACCENT}; }}
QCheckBox::indicator:disabled {{ background: {HOVER}; border-color: {BORDER}; }}
QCheckBox::indicator:checked:disabled {{ background: {MUTED}; border-color: {MUTED}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {MUTED}; }}
QRadioButton::indicator {{ width: 14px; height: 14px; border-radius: 7px;
    border: 1px solid {MUTED}; background: #ffffff; }}
QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QFrame#card {{ background: #ffffff; border: 1px solid {BORDER}; border-radius: 8px; }}
QTableWidget, QTableView, QListWidget, QListView {{ background: #ffffff; color: {TEXT};
    alternate-background-color: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px;
    gridline-color: {BORDER}; selection-background-color: {SELECTED}; selection-color: {TEXT}; }}
QListWidget::item {{ padding: 6px 8px; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ color: {MUTED}; background: {HOVER}; border: none;
    border-bottom: 1px solid {BORDER}; padding: 6px; font-weight: 600; }}
QTableCornerButton::section {{ background: {HOVER}; border: none; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; }}
QScrollBar::handle {{ background: {BORDER}; border-radius: 4px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {MUTED}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QProgressBar {{ color: {TEXT}; background: {HOVER}; border: 1px solid {BORDER};
    border-radius: 6px; text-align: center; min-height: 22px; }}
QProgressBar::chunk {{ background: {SELECTED}; border-radius: 5px; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; background: #ffffff; }}
QTabBar::tab {{ color: {MUTED}; background: {PANEL}; padding: 7px 12px; }}
QTabBar::tab:selected {{ color: {TEXT}; background: #ffffff; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover {{ background: {HOVER}; }}
QToolTip {{ color: {TEXT}; background: {PANEL}; border: 1px solid {BORDER};
    border-radius: 6px; padding: 6px; }}
QMenu {{ color: {TEXT}; background: {PANEL}; border: 1px solid {BORDER};
    border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {SELECTED}; }}
QMenu::item:disabled {{ color: {MUTED}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 6px; }}
"""


def qss() -> str:
    """Resolve the system font after QApplication is available."""
    if sys.platform == "win32":
        family = "Segoe UI"
    else:
        from PySide6.QtGui import QFontDatabase
        family = QFontDatabase.systemFont(QFontDatabase.GeneralFont).family()
    assets = Path(__file__).with_name("assets").as_posix()
    return _QSS.replace("@FONT@", family).replace("@ASSETS@", assets)


def __getattr__(name):
    if name == "QSS":
        return qss()
    raise AttributeError(name)
