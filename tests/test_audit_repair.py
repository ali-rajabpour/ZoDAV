import hashlib
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fakedav import (
    ENTRY_TIME,
    GOOD,
    PASSWORD,
    USER,
    build_defect_store,
    fakedav,
    snapshot,  # noqa: F401
)
from zodav_audit import DavStore, DirStore, integrity, repair

ENTRY_MS = int(time.mktime(ENTRY_TIME + (0, 0, -1)) * 1000)
GOOD_MD5 = hashlib.md5(GOOD).hexdigest()


def read_prop(path):
    root = ET.fromstring(Path(path).read_bytes())
    return root.findtext("mtime"), root.findtext("hash")


def test_dry_run_changes_nothing(tmp_path, monkeypatch):
    store_dir = build_defect_store(tmp_path / "zotero")
    monkeypatch.chdir(tmp_path)
    before = snapshot(store_dir)
    res = repair(DirStore(store_dir))
    assert snapshot(store_dir) == before
    assert not list(tmp_path.glob("zodav-quarantine-*"))
    got = {(f.code, f.subject) for f in res.findings}
    assert ("WOULD_QUARANTINE", "CCCC2222.prop") in got
    assert {
        ("WOULD_WRITE_PROP", n)
        for n in ("BBBB2222.prop", "DDDD2222.prop", "GGGG2222.prop", "KKKK2222.prop")
    } <= got


def test_apply_fixes_and_second_run_shows_only_unfixable(tmp_path):
    store_dir = build_defect_store(tmp_path / "zotero")
    q = tmp_path / "q"
    orphan = (store_dir / "CCCC2222.prop").read_bytes()
    corrupt = {
        n: (store_dir / n).read_bytes()
        for n in ("EEEE2222.zip", "FFFF2222.zip", "FFFF2222.prop", "HHHH2222.zip", "LLLL2222.zip")
    }

    res = repair(DirStore(store_dir), apply=True, quarantine=q)
    assert not [f for f in res.findings if f.severity == "error"], res.findings

    # missing prop: hash of the single file, mtime from the zip entry, in ms
    mtime, h = read_prop(store_dir / "BBBB2222.prop")
    assert h == GOOD_MD5 and mtime == str(ENTRY_MS) and len(mtime) == 13
    # hash=undefined: real hash, the valid mtime is kept
    assert read_prop(store_dir / "DDDD2222.prop") == ("1700000001234", GOOD_MD5)
    # invalid mtime / unparseable: taken from the entry
    assert read_prop(store_dir / "GGGG2222.prop") == (str(ENTRY_MS), GOOD_MD5)
    assert read_prop(store_dir / "KKKK2222.prop") == (str(ENTRY_MS), GOOD_MD5)
    # orphan quarantined byte-exact, original gone
    assert (q / "CCCC2222.prop").read_bytes() == orphan
    assert not (store_dir / "CCCC2222.prop").exists()
    # unfixable things untouched
    for n, data in corrupt.items():
        assert (store_dir / n).read_bytes() == data

    again = integrity(DirStore(store_dir))
    assert {(f.code, f.subject) for f in again.findings if f.severity == "error"} == {
        ("CORRUPT_ZIP", "EEEE2222.zip"),
        ("HASH_MISMATCH", "FFFF2222.zip"),
        ("EMPTY_FILE", "HHHH2222.zip"),
        ("MISSING_PROP", "LLLL2222.zip"),
    }
    assert not list(store_dir.glob(".zodav-tmp-*"))


def test_unfixable_reported_as_warnings(tmp_path):
    store_dir = build_defect_store(tmp_path / "zotero")
    res = repair(DirStore(store_dir))
    nr = {f.subject for f in res.findings if f.code == "NOT_REPAIRABLE"}
    assert nr == {"EEEE2222.zip", "FFFF2222.zip", "HHHH2222.zip", "LLLL2222.zip"}
    assert all(
        f.severity == "warning" and f.effect and f.fix
        for f in res.findings
        if f.code == "NOT_REPAIRABLE"
    )


def test_nothing_deleted_when_quarantine_cannot_be_written(tmp_path):
    store_dir = build_defect_store(tmp_path / "zotero")
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    res = repair(DirStore(store_dir), apply=True, quarantine=blocker)
    assert any(f.code == "REPAIR_FAILED" and f.subject == "CCCC2222.prop" for f in res.findings)
    assert (store_dir / "CCCC2222.prop").exists()
    assert not res.ok


def test_nothing_deleted_when_copy_does_not_reread_exact(tmp_path, monkeypatch):
    store_dir = build_defect_store(tmp_path / "zotero")
    real = Path.read_bytes
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda self: (
            b"tampered" if "quarantine" in str(self) or self.parent.name == "q" else real(self)
        ),
    )
    res = repair(DirStore(store_dir), apply=True, quarantine=tmp_path / "q")
    assert any(f.code == "REPAIR_FAILED" for f in res.findings)
    assert (store_dir / "CCCC2222.prop").exists()


def test_apply_over_webdav(tmp_path, fakedav):
    url, srv = fakedav
    store_dir = build_defect_store(tmp_path / "zotero")
    srv.load_dir(store_dir)
    res = repair(DavStore(url, USER, PASSWORD), apply=True, quarantine=tmp_path / "q")
    assert not [f for f in res.findings if f.severity == "error"], res.findings
    assert "CCCC2222.prop" not in srv.files and (tmp_path / "q" / "CCCC2222.prop").exists()
    assert b"<hash>" + GOOD_MD5.encode() in srv.files["BBBB2222.prop"]


def test_dirstore_write_is_atomic(tmp_path, monkeypatch):
    s = DirStore(tmp_path)
    s.write("a.prop", b"one")
    os.chmod(tmp_path / "a.prop", 0o640)
    s.write("a.prop", b"two")
    assert s.read("a.prop") == b"two"
    assert (tmp_path / "a.prop").stat().st_mode & 0o777 == 0o640

    def boom(*a):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        s.write("a.prop", b"three")
    assert s.read("a.prop") == b"two"
    assert [p.name for p in tmp_path.iterdir()] == ["a.prop"]


def test_default_quarantine_dir_is_timestamped_in_cwd(tmp_path, monkeypatch):
    store_dir = build_defect_store(tmp_path / "zotero")
    monkeypatch.chdir(tmp_path)
    repair(DirStore(store_dir), apply=True)
    dirs = list(tmp_path.glob("zodav-quarantine-*"))
    assert len(dirs) == 1 and (dirs[0] / "CCCC2222.prop").exists()


def test_clean_store_needs_nothing(tmp_path):
    from fakedav import build_clean_store

    res = repair(DirStore(build_clean_store(tmp_path / "z")), apply=True)
    assert res.findings == [] and res.ok


# --- needs the ZoDAV test stack -------------------------------------------


@pytest.mark.docker
@pytest.mark.usefixtures("stack")
def test_stack_repair_apply(tmp_path):
    src = build_defect_store(tmp_path / "src")
    store = DavStore("http://127.0.0.1:18080/", "zotero", "test-zotero-password-0123456789")
    names = sorted(p.name for p in src.iterdir())
    try:
        for n in names:
            store.write(n, (src / n).read_bytes())
        res = repair(store, apply=True, quarantine=tmp_path / "q")
        assert not [f for f in res.findings if f.severity == "error"], res.findings
        assert (tmp_path / "q" / "CCCC2222.prop").exists()
        assert b"<hash>" + GOOD_MD5.encode() in store.read("BBBB2222.prop")
        assert "CCCC2222.prop" not in store.list()
    finally:
        # repair creates files of its own (e.g. BBBB2222.prop), so clean up by key
        keys = {n.split(".")[0] for n in names}
        for n in store.list():
            if n.split(".")[0] in keys:
                store.delete(n)
