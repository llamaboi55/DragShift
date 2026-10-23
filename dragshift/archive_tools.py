"""Inspect archive members without extracting or opening their files."""

from .i18n import tr, tr_items
from PySide6.QtWidgets import (QComboBox, QFormLayout, QHeaderView, QLabel, QListWidget,
                               QPushButton, QTableWidget, QTableWidgetItem)

from . import archives
from .util import human_size, UserError
from .video_tools import _AsyncTools


class ArchivePackWindow(_AsyncTools):
    tool_name = "Pack"

    def __init__(self, paths, notify):
        super().__init__("Pack files", paths, notify, (600, 460))
        self.subtitle.setText(tr(f"{len(self.paths)} selected item(s) · one archive"))
        selected = QListWidget()
        self.selected = selected
        selected.setAccessibleName(tr("Selected files and folders"))
        for path in self.paths[:100]:
            selected.addItem(tr(str(path) + ('/' if path.is_dir() else '')))
        if len(self.paths) > 100:
            selected.addItem(tr(f"… and {len(self.paths) - 100} more items"))
        self.body.addWidget(selected, 1)
        form = QFormLayout()
        self.target = QComboBox()
        self.target.addItems(tr_items(['ZIP', '7Z']))
        self.target.setAccessibleName(tr("Archive format"))
        form.addRow(tr("&Format"), self.target)
        self.body.addLayout(form)
        self.destination = QLabel()
        self.destination.setWordWrap(True)
        self.destination.setObjectName('dim')
        self.body.addWidget(self.destination)
        self.body.addWidget(self.error)
        self.body.addLayout(self.footer("Create archive", self.save))
        try:
            sources = archives.packing_sources(self.paths)
            archives.executable()
            self.destination.setText(tr("Save to: " + str(archives.packing_directory(sources)) +
                                     "\nExisting files are kept. Folders keep their structure."))
        except UserError as error:
            self.error.setText(tr(str(error)))
            self.primary_btn.setEnabled(False)

    def save(self):
        paths, target = list(self.paths), self.target.currentText()
        self.run(lambda: archives.pack(paths, target), f"Creating {target} archive…")

    def accepts_sources(self,paths):
        return bool(paths)

    def receive_files(self,paths):
        try:
            sources=archives.packing_sources(self.paths+list(paths))
            self.paths=list(sources)
            self.selected.clear()
            self.selected.addItems(tr_items([str(p)+('/' if p.is_dir() else '') for p in self.paths]))
            self.subtitle.setText(tr(f'{len(self.paths)} selected item(s) · one archive'))
            self.destination.setText(tr('Save to: '+str(archives.packing_directory(sources))))
            self.error.clear()
            self.primary_btn.setEnabled(True)
        except UserError as error:
            self.error.setText(tr(str(error)))

class ArchiveContentsWindow(_AsyncTools):
    def __init__(self, path, notify):
        super().__init__("Archive contents", [path], notify, (760, 580))
        self._source_list = False
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(tr_items(["Name", "Size"]))
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAccessibleName(tr("Files inside the selected archive"))
        self.body.addWidget(self.table, 1)
        self.body.addWidget(self.error)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.close)
        self.body.addWidget(close)
        path = self.path
        self.lookup(lambda: archives.entries(path), self._loaded)

    def _loaded(self, items):
        self.table.setRowCount(len(items))
        for row, entry in enumerate(items):
            self.table.setItem(row, 0, QTableWidgetItem(entry.name + ("/" if entry.is_dir else "")))
            self.table.setItem(row, 1, QTableWidgetItem("Folder" if entry.is_dir else human_size(entry.size)))
