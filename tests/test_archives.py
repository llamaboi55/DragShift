"""Real 7-Zip round trips and pre-extraction safety tests; no user archives."""
import hashlib
import io
import stat
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest

from dragshift import archives, video
from dragshift.util import CONFIG, UserError


@pytest.fixture(autouse=True)
def engine_and_output():
    try:
        archives.executable()
    except UserError as error:
        pytest.skip(str(error))
    old = CONFIG["output"]
    CONFIG["output"] = "next"
    yield
    CONFIG["output"] = old


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_zip(path, files=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in (files or {"folder/Тест '音'.txt": b"hello", "root.txt": b"world"}).items():
            archive.writestr(name, content)
        archive.writestr("empty/", b"")
    return path


def tree(folder):
    return {p.relative_to(folder).as_posix(): digest(p) for p in folder.rglob("*") if p.is_file()}


def test_round_trip_unicode_paths_hashes_and_originals(tmp_path):
    source = make_zip(tmp_path / "Архив '音'.zip")
    before = digest(source)
    members = archives.entries(source)
    assert {e.name for e in members} == {"folder/Тест '音'.txt", "root.txt", "empty"}
    assert next(e for e in members if e.name == "root.txt").size == 5
    assert next(e for e in members if e.name == "empty").is_dir
    first = archives.extract(source)[0]
    converted = archives.convert_archive(source, "7Z")[0]
    assert converted.suffix == ".7z" and converted != source
    tar = archives.convert_archive(converted, "TAR")[0]
    assert tar.suffix == ".tar"
    second = archives.extract(tar)[0]
    assert tree(first) == tree(second)
    assert (second / "empty").is_dir()
    assert digest(source) == before
    assert not list(tmp_path.glob(".~archive-*"))
    assert not list(tmp_path.glob(".~video-*"))


def test_extract_never_merges_or_overwrites_existing_folder(tmp_path):
    source = make_zip(tmp_path / "example.zip")
    first = archives.extract(source)[0]
    marker = first / "root.txt"
    marker.write_text("keep this", encoding="utf-8")
    second = archives.extract(source)[0]
    assert first != second and first.parent == second.parent == tmp_path
    assert marker.read_text(encoding="utf-8") == "keep this"
    assert (second / "root.txt").read_bytes() == b"world"


@pytest.mark.parametrize("name", ["../outside.txt", "folder/../../outside.txt", "C:/outside.txt",
    "/outside.txt", "\\\\server\\share\\outside.txt", "folder\\..\\..\\outside.txt",
    "good.txt:stream", "folder/CON.txt", "folder/trailing. /file", "folder/../file"])
def test_reject_unsafe_paths_before_extraction(tmp_path, name, monkeypatch):
    source = make_zip(tmp_path / "unsafe.zip", {name: b"unsafe"})
    called = []
    monkeypatch.setattr(video, "run_process", lambda *args, **kwargs: called.append(args))
    before = set(tmp_path.iterdir())
    with pytest.raises(UserError):
        archives.extract(source)
    assert not called
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize("kind", ["zip-symlink", "tar-symlink", "tar-hardlink"])
def test_reject_links_before_extraction(tmp_path, kind, monkeypatch):
    if kind == "zip-symlink":
        source = tmp_path / "links.zip"
        with zipfile.ZipFile(source, "w") as archive:
            link = zipfile.ZipInfo("shortcut")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "../outside")
    else:
        source = tmp_path / "links.tar"
        with tarfile.open(source, "w") as archive:
            target = tarfile.TarInfo("target.txt")
            target.size = 5
            archive.addfile(target, io.BytesIO(b"hello"))
            link = tarfile.TarInfo("shortcut")
            link.type = tarfile.SYMTYPE if kind == "tar-symlink" else tarfile.LNKTYPE
            link.linkname = "target.txt"
            archive.addfile(link)
    called = []
    monkeypatch.setattr(video, "run_process", lambda *args, **kwargs: called.append(args))
    with pytest.raises(UserError, match="[Ll]ink"):
        archives.extract(source)
    assert not called
    assert set(tmp_path.iterdir()) == {source}


@pytest.mark.parametrize("limit,value", [("MAX_ENTRIES", 1), ("MAX_EXTRACTED_BYTES", 3)])
def test_expansion_limits_apply_before_extract(tmp_path, monkeypatch, limit, value):
    source = make_zip(tmp_path / "large.zip")
    monkeypatch.setattr(archives, limit, value)
    with pytest.raises(UserError, match="limit|large|many"):
        archives.extract(source)
    assert set(tmp_path.iterdir()) == {source}


@pytest.mark.parametrize("operation", [archives.entries, archives.extract,
    lambda source: archives.convert_archive(source, "7Z")])
def test_corrupt_archive_does_not_publish(tmp_path, operation):
    source = tmp_path / "broken.zip"
    source.write_bytes(b"this is not an archive")
    with pytest.raises(UserError, match="[Aa]rchive"):
        operation(source)
    assert set(tmp_path.iterdir()) == {source}


def test_conversion_cancel_cleans_stage_and_partial_output(tmp_path):
    source = make_zip(tmp_path / "cancel.zip")
    before = digest(source)
    cancel = threading.Event()
    packing = []

    def update(value, message):
        if "Packing archive" in message:
            packing.append(message)
            cancel.set()

    with video.job_context(video.Control(cancel=cancel, progress=update)):
        with pytest.raises(video.Cancelled):
            archives.convert_archive(source, "7Z")
    assert packing
    assert digest(source) == before
    assert set(tmp_path.iterdir()) == {source}


def test_precancel_has_no_side_effects(tmp_path):
    source = make_zip(tmp_path / "cancel.zip")
    cancel = threading.Event()
    cancel.set()
    with video.job_context(video.Control(cancel=cancel)):
        with pytest.raises(video.Cancelled):
            archives.extract(source)
    assert set(tmp_path.iterdir()) == {source}


def test_output_folder_and_collision(tmp_path):
    source = make_zip(tmp_path / "example.zip")
    CONFIG["output"] = "folder"
    first = archives.convert_archive(source, "ZIP")[0]
    second = archives.convert_archive(source, "ZIP")[0]
    assert first.parent.name == "DragShift" and first != second
    assert first.is_file() and second.is_file()
    # Extraction always creates a new sibling directory, independent of export settings.
    assert archives.extract(source)[0].parent == tmp_path


def test_unknown_target_is_rejected(tmp_path):
    source = make_zip(tmp_path / "test.zip")
    with pytest.raises(UserError, match="format"):
        archives.convert_archive(source, "RAR")
    assert set(tmp_path.iterdir()) == {source}


def test_engine_override(monkeypatch):
    engine = archives.executable()
    monkeypatch.setenv("DRAGSHIFT_7Z", engine)
    assert Path(archives.executable()).resolve() == Path(engine).resolve()


def test_empty_archive_round_trip(tmp_path):
    source = tmp_path / "empty.zip"
    with zipfile.ZipFile(source, "w"):
        pass
    assert archives.entries(source) == []
    for target in archives.TARGETS:
        converted = archives.convert_archive(source, target)[0]
        assert archives.entries(converted) == []
        folder = archives.extract(converted)[0]
        assert folder.is_dir() and list(folder.iterdir()) == []


@pytest.mark.parametrize("names", [("same.txt", "SAME.txt"), ("file", "file/inside.txt")])
def test_reject_windows_path_collisions(tmp_path, names):
    source = make_zip(tmp_path / "collision.zip", dict.fromkeys(names, b"x"))
    with pytest.raises(UserError, match="collid"):
        archives.extract(source)
    assert set(tmp_path.iterdir()) == {source}


def test_reject_ambiguous_listing_name(tmp_path):
    source = make_zip(tmp_path / "ambiguous.zip", {"name\nPath = ../escape.txt": b"x"})
    with pytest.raises(UserError):
        archives.extract(source)
    assert set(tmp_path.iterdir()) == {source}


def test_engine_sanitizes_control_characters_to_safe_windows_names(tmp_path):
    source = make_zip(tmp_path / "control.zip", {"name\nwith-newline.txt": b"x"})
    members = archives.entries(source)
    assert any(member.name == "name_with-newline.txt" for member in members)
    folder = archives.extract(source)[0]
    assert (folder / "name_with-newline.txt").read_bytes() == b"x"
