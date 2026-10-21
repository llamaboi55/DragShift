"""Confirm the order of a combined document before starting a background job."""
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QAbstractItemView, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton

from .window import GlassWindow


class CombineOrderWindow(GlassWindow):
    def __init__(self, paths: list[str], title: str, confirm: Callable[[list[str]], None]):
        super().__init__(title, size=(480, 400))
        self._confirm = confirm
        self._submitted = False
        hint = QLabel("Drag to reorder, or select a file and use the arrows.\nThe first file also determines the output folder.")
        hint.setWordWrap(True)
        hint.setObjectName("dim")
        self.body.addWidget(hint)
        self.files = QListWidget()
        self.files.setAccessibleName("Files in page order")
        self.files.setDragDropMode(QAbstractItemView.InternalMove)
        self.files.setDefaultDropAction(Qt.MoveAction)
        self.files.setSelectionMode(QAbstractItemView.SingleSelection)
        for path in paths:
            item = QListWidgetItem(self.files)
            item.setData(Qt.UserRole, str(path))
            item.setToolTip(str(path))
        self.body.addWidget(self.files, 1)
        row = QHBoxLayout()
        self.up = QPushButton("Move up")
        self.down = QPushButton("Move down")
        self.up.clicked.connect(lambda: self.move_selected(-1))
        self.down.clicked.connect(lambda: self.move_selected(1))
        row.addWidget(self.up)
        row.addWidget(self.down)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.close)
        row.addWidget(cancel)
        self.confirm = QPushButton("Create PDF")
        self.confirm.setObjectName("primary")
        self.confirm.setShortcut(QKeySequence(Qt.Key_Return))
        self.confirm.clicked.connect(self.accept_order)
        self.confirm.setEnabled(len(paths) >= 2)
        row.addWidget(self.confirm)
        self.body.addLayout(row)
        self.files.currentRowChanged.connect(self.refresh)
        self.files.model().rowsMoved.connect(self.refresh)
        self.files.setCurrentRow(0)
        self.refresh()

    def ordered_paths(self) -> list[str]:
        return [self.files.item(i).data(Qt.UserRole) for i in range(self.files.count())]

    def refresh(self, *_):
        for i, path in enumerate(self.ordered_paths()):
            self.files.item(i).setText(f"{i + 1}. {Path(path).name}")
        row = self.files.currentRow()
        self.up.setEnabled(row > 0)
        self.down.setEnabled(0 <= row < self.files.count() - 1)

    def move_selected(self, offset: int):
        row = self.files.currentRow()
        target = row + offset
        if row < 0 or not 0 <= target < self.files.count():
            return
        item = self.files.takeItem(row)
        self.files.insertItem(target, item)
        self.files.setCurrentRow(target)
        self.refresh()

    def accept_order(self):
        if self._submitted or self.files.count() < 2:
            return
        self._submitted = True
        self.confirm.setEnabled(False)
        self._confirm(self.ordered_paths())
        self.close()
