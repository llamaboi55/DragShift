"""Create real archives and recover exactly the selected files, without changing them."""
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from dragshift import archives, video
from dragshift.app import DragShiftApp
from dragshift.radial import Item
from dragshift.report import summarize
from dragshift.util import CONFIG, UserError


@pytest.fixture(autouse=True)
def output_setting():
    old = CONFIG['output']
    CONFIG['output'] = 'next'
    yield
    CONFIG['output'] = old


@pytest.fixture
def engine():
    try:
        archives.executable()
    except UserError as error:
        pytest.skip(str(error))


@pytest.mark.parametrize('target', ['ZIP', '7Z'])
def test_pack_mixed_selection_preserves_names_folders_and_originals(tmp_path, target, engine):
    folder = tmp_path / 'Папка.v1'
    (folder / 'empty').mkdir(parents=True)
    (folder / 'nested').mkdir()
    (folder / 'nested' / "Тест '音'.txt").write_bytes(b'hello')
    file = tmp_path / '@track.wav'
    file.write_bytes(b'audio bytes')
    extra = tmp_path / '-unknown.xyz'
    extra.write_bytes(b'other bytes')
    out = archives.pack([folder, file, extra], target)[0]
    assert out.suffix == '.' + target.lower() and out.parent == tmp_path
    recovered = archives.extract(out)[0]
    assert (recovered / folder.name / 'empty').is_dir()
    assert (recovered / folder.name / 'nested' / "Тест '音'.txt").read_bytes() == b'hello'
    assert (recovered / file.name).read_bytes() == file.read_bytes() == b'audio bytes'
    assert (recovered / extra.name).read_bytes() == extra.read_bytes() == b'other bytes'
    assert not list(tmp_path.glob('.~pack-*'))
    assert not list(tmp_path.glob('.~video-*'))


@pytest.mark.parametrize('mode', ['next', 'folder'])
def test_pack_empty_folder_with_suffix_and_unique_output(tmp_path, mode, engine):
    folder = tmp_path / 'project.v1'
    folder.mkdir()
    CONFIG['output'] = mode
    a = archives.pack([folder], 'ZIP')[0]
    b = archives.pack([folder], 'ZIP')[0]
    assert a != b and a.name == 'project.v1.zip'
    assert a.parent == (tmp_path / 'DragShift' if mode == 'folder' else tmp_path)
    assert (archives.extract(a)[0] / folder.name).is_dir()
    assert list(folder.iterdir()) == []


def test_output_subfolder_never_places_archive_inside_selected_folder(tmp_path, engine):
    folder = tmp_path / 'DragShift'
    folder.mkdir()
    (folder / 'keep.txt').write_bytes(b'keep')
    CONFIG['output'] = 'folder'
    out = archives.pack([folder], '7Z')[0]
    assert out.parent == tmp_path
    assert {p.name for p in folder.iterdir()} == {'keep.txt'}


def test_overlapping_selection_is_packed_once(tmp_path, engine):
    folder = tmp_path / 'folder'
    folder.mkdir()
    file = folder / 'file.txt'
    file.write_bytes(b'keep')
    out = archives.pack([file, folder, file], 'ZIP')[0]
    assert {e.name for e in archives.entries(out)} == {'folder', 'folder/file.txt'}


def test_duplicate_root_names_are_reported_without_losing_files(tmp_path, engine):
    paths = [tmp_path / name / 'same.txt' for name in ('a', 'b')]
    for i, path in enumerate(paths):
        path.parent.mkdir()
        path.write_bytes(bytes([i]))
    with pytest.raises(UserError, match='same name|collid'):
        archives.pack(paths, 'ZIP')
    assert {p.name for p in tmp_path.iterdir()} == {'a', 'b'}


@pytest.mark.parametrize('phase', ['Preparing files', 'Packing archive'])
def test_cancel_staging_or_compression_cleans_every_partial(tmp_path, phase, engine):
    source = tmp_path / 'file.txt'
    source.write_bytes(b'keep' * 1024)
    cancel = threading.Event()
    def progress(value, message):
        if message.startswith(phase):
            cancel.set()
    with video.job_context(video.Control(cancel=cancel, progress=progress)):
        with pytest.raises(video.Cancelled):
            archives.pack([source], '7Z')
    assert set(tmp_path.iterdir()) == {source} and source.read_bytes() == b'keep' * 1024


def test_pack_rejects_missing_empty_and_invalid_selection(tmp_path, engine):
    for paths, target in [([], 'ZIP'), ([tmp_path / 'missing'], 'ZIP'), ([tmp_path], 'RAR')]:
        with pytest.raises(UserError):
            archives.pack(paths, target)


def test_pack_rejects_links_in_selected_tree(tmp_path, engine):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'original').write_bytes(b'keep')
    try:
        (source / 'link').symlink_to(source / 'original')
    except OSError:
        pytest.skip('Creating symlinks requires Windows developer mode or permission')
    with pytest.raises(UserError, match='[Ll]ink'):
        archives.pack([source], 'ZIP')
    assert set(tmp_path.iterdir()) == {source}


def test_staging_limits_apply_before_publishing(tmp_path, monkeypatch, engine):
    file = tmp_path / 'big.txt'
    file.write_bytes(b'123456')
    monkeypatch.setattr(archives, 'MAX_EXTRACTED_BYTES', 5)
    with pytest.raises(UserError, match='large|limit'):
        archives.pack([file], 'ZIP')
    assert set(tmp_path.iterdir()) == {file}


def test_file_timestamp_is_preserved(tmp_path, engine):
    import zipfile
    source = tmp_path / 'dated.txt'
    source.write_bytes(b'old document')
    os.utime(source, (1577836800, 1577836800))
    out = archives.pack([source], 'ZIP')[0]
    with zipfile.ZipFile(out) as archive:
        assert archive.getinfo('dated.txt').date_time[0] == 2020


def test_every_file_category_and_folder_has_pack_action(tmp_path):
    folder = tmp_path / 'folder.pdf'
    folder.mkdir()
    for source in [folder, *[tmp_path / ('file.' + ext) for ext in ('png', 'mp4', 'wav', 'pdf', 'docx', 'txt', 'zip', 'xyz')]]:
        rings = DragShiftApp.build_rings(None, [str(source)])
        assert any(item.key == 'tool:Pack' for item in rings.get('Tools', []))
    assert set(DragShiftApp.build_rings(None, [str(folder)])) == {'Tools'}


def test_pack_route_keeps_every_selected_category(qt_session, tmp_path):
    paths = [str(tmp_path / ('file.' + ext)) for ext in ('png', 'wav', 'xyz')]
    for path in paths:
        Path(path).write_bytes(b'x')
    windows = []
    controller = SimpleNamespace(open_window=windows.append, notify=lambda *_: None)
    DragShiftApp.run_item(controller, Item('Pack', key='tool:Pack'), paths)
    window = windows[0]
    assert list(map(str, window.paths)) == paths
    assert window.target.currentText() == 'ZIP'
    calls = []
    window.submit = lambda *args: calls.append(args)
    window.target.setCurrentText('7Z')
    window.save()
    assert calls[0][0] == 'tool:Pack' and calls[0][1] == paths
    window.close()


def test_pack_result_is_archive_creation_even_for_audio_source(tmp_path):
    result = summarize('tool:Pack', [tmp_path / 'sound.wav'], [tmp_path / 'sound.zip'])
    assert result.title == 'Archive created'
