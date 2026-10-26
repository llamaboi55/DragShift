from PySide6.QtCore import QFile
from dragshift import toasts


def test_undo_can_move_a_result_directory(tmp_path, monkeypatch):
    folder = tmp_path / "extracted"
    folder.mkdir()
    (folder / "file.txt").write_text("result")
    calls = []
    def move(path):
        calls.append(path)
        return True, "trash/result"
    monkeypatch.setattr(QFile, "moveToTrash", move)
    assert toasts.trash([folder]) == 1
    assert calls == [str(folder)]


def test_failed_trash_does_not_permanently_delete_result(tmp_path, monkeypatch):
    file = tmp_path / "result.txt"
    file.write_text("keep")
    monkeypatch.setattr(QFile, "moveToTrash", lambda _: (False, ""))
    assert toasts.trash([file]) == 0
    assert file.read_text() == "keep"
