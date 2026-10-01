import json
import tracemalloc
import zipfile

import pytest
import zodav_audit as za
from fakedav import (
    GOOD,
    PASSWORD,
    USER,
    build_clean_store,
    build_defect_store,
    fakedav,
    make_zip,
    md5,  # noqa: F401
    prop,
)
from test_audit_watch import (
    GIB,
    NOW,
    Usage,
    fresh_backup,
    hook,
    plenty_of_disk,
    run,
    set_state,
    store,
)
from zodav_audit import AuditError, DavStore, DirStore, Finding, Result, integrity, main, repair


def codes_for(res):
    return {(f.code, f.subject) for f in res.findings}


# --- 1. bounded memory -----------------------------------------------------


def test_integrity_never_reads_a_zip_whole(tmp_path, monkeypatch):
    build_clean_store(tmp_path, count=2)
    real = DirStore.read

    def guarded(self, name, limit=None):
        assert name.endswith(".prop"), f"zip {name} loaded into memory"
        return real(self, name, limit)

    monkeypatch.setattr(DirStore, "read", guarded)
    assert integrity(DirStore(tmp_path)).ok


def test_large_member_is_hashed_in_bounded_memory(tmp_path):
    big = 48 * 1024 * 1024
    (tmp_path / "AAAA2222.zip").write_bytes(b"")
    with zipfile.ZipFile(tmp_path / "AAAA2222.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("z.bin", (2020, 1, 1, 0, 0, 0)), bytes(big))
    h = za.hashlib.md5(bytes(big)).hexdigest()
    (tmp_path / "AAAA2222.prop").write_bytes(prop(md5=h))
    tracemalloc.start()
    try:
        res = integrity(DirStore(tmp_path))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert res.ok, res.findings
    assert peak < 8 * 1024 * 1024


def test_absurd_declared_size_is_reported_not_unpacked(tmp_path, monkeypatch):
    build_clean_store(tmp_path, count=1)
    monkeypatch.setattr(za, "MAX_UNCOMPRESSED", 3)

    def no_unpack(*a, **k):
        raise AssertionError("decompressed a zip over the size limit")

    monkeypatch.setattr(zipfile.ZipFile, "testzip", no_unpack)
    res = integrity(DirStore(tmp_path))
    f = [f for f in res.findings if f.code == "CORRUPT_ZIP"]
    assert len(f) == 1 and "declares" in f[0].message and f[0].effect


def test_davstore_open_streams_to_spool_dir(tmp_path, fakedav):
    url, srv = fakedav
    srv.files["AAAA2222.zip"] = make_zip({"a.txt": GOOD})
    spool = tmp_path / "spool"
    spool.mkdir()
    st = DavStore(url, USER, PASSWORD, spool_dir=str(spool))
    with st.open("AAAA2222.zip") as f:
        assert f.read() == srv.files["AAAA2222.zip"]
    with pytest.raises(AuditError, match="larger"):
        st.read("AAAA2222.zip", limit=10)


# --- 2. mass-deletion baseline ----------------------------------------------


def state(tmp_path):
    return json.loads((tmp_path / "rep" / "state.json").read_text())


def test_baseline_survives_a_mass_deletion_and_needs_acceptance(tmp_path, hook):
    url, got = hook
    env = {"ZODAV_ALERT_URL": url}
    set_state(tmp_path, 100)
    d = store(tmp_path, count=5)
    assert "MASS_DELETION" in {f.code for f in run(tmp_path, d, env).findings}
    assert state(tmp_path)["baseline"] == 100
    # the reduced count is not the new normal, and no "recovered" is sent
    assert "MASS_DELETION" in {f.code for f in run(tmp_path, d, env).findings}
    assert len(got) == 2 and not any(
        b"recovered" in g["headers"]["Title"].lower().encode() for g in got
    )
    fix = [f for f in run(tmp_path, d, env).findings if f.code == "MASS_DELETION"][0].fix
    assert "./zodav accept-deletions" in fix
    assert (
        main(["watch", str(d), "--accept-current-count", "--reports", str(tmp_path / "rep")]) == 0
    )
    assert state(tmp_path)["baseline"] == 5
    assert "MASS_DELETION" not in {f.code for f in run(tmp_path, d, env).findings}


def test_baseline_grows_with_the_count(tmp_path):
    set_state(tmp_path, 3)
    run(tmp_path, store(tmp_path, count=8))
    assert state(tmp_path)["baseline"] == 8


# --- 3, 4. repair keeps evidence, checks for races --------------------------


def test_hash_mismatch_with_bad_mtime_is_not_rewritten(tmp_path):
    d = tmp_path / "z"
    d.mkdir()
    (d / "FFFF2222.zip").write_bytes(make_zip({"f.txt": GOOD}))
    bad = prop(mtime="abc", md5=md5(b"other"))
    (d / "FFFF2222.prop").write_bytes(bad)
    res = repair(DirStore(d), apply=True, quarantine=tmp_path / "q")
    assert (d / "FFFF2222.prop").read_bytes() == bad
    assert {f.subject for f in res.findings if f.code == "NOT_REPAIRABLE"} == {
        "FFFF2222.prop",
        "FFFF2222.zip",
    }
    assert not any(f.code in ("WROTE_PROP", "WOULD_WRITE_PROP") for f in res.findings)


def test_overwritten_prop_is_quarantined_first(tmp_path):
    d = build_defect_store(tmp_path / "z")
    old = (d / "DDDD2222.prop").read_bytes()
    repair(DirStore(d), apply=True, quarantine=tmp_path / "q")
    assert (tmp_path / "q" / "DDDD2222.prop").read_bytes() == old
    assert (d / "DDDD2222.prop").read_bytes() != old


def test_wrote_prop_says_when_mtime_is_approximate(tmp_path):
    d = build_defect_store(tmp_path / "z")
    res = repair(DirStore(d), apply=True, quarantine=tmp_path / "q")
    msg = {f.subject: f.message for f in res.findings if f.code == "WROTE_PROP"}
    assert "approximate" in msg["BBBB2222.prop"] and "approximate" not in msg["DDDD2222.prop"]


class Racy(DirStore):
    """Runs `action` right after read #`after` of `name`, like a client writing mid-repair."""

    def __init__(self, path, name, after, action):
        super().__init__(path)
        self.name, self.after, self.action, self.n = name, after, action, 0

    def read(self, name, limit=None):
        data = super().read(name, limit)
        if name == self.name:
            self.n += 1
            if self.n == self.after:
                self.action()
        return data


def test_prop_changed_during_repair_is_not_overwritten(tmp_path):
    d = build_defect_store(tmp_path / "z")
    newer = prop(mtime="1800000000000", md5="undefined")
    st = Racy(d, "DDDD2222.prop", 2, lambda: (d / "DDDD2222.prop").write_bytes(newer))
    res = repair(st, apply=True, quarantine=tmp_path / "q")
    assert ("REPAIR_FAILED", "DDDD2222.prop") in codes_for(res)
    assert (d / "DDDD2222.prop").read_bytes() == newer


def test_zip_changed_during_repair_blocks_the_rewrite(tmp_path):
    d = build_defect_store(tmp_path / "z")
    before = (d / "DDDD2222.prop").read_bytes()
    st = Racy(
        d,
        "DDDD2222.prop",
        2,
        lambda: (d / "DDDD2222.zip").write_bytes(make_zip({"d.txt": b"replaced"})),
    )
    res = repair(st, apply=True, quarantine=tmp_path / "q")
    assert ("REPAIR_FAILED", "DDDD2222.prop") in codes_for(res)
    assert (d / "DDDD2222.prop").read_bytes() == before


def test_orphan_changed_during_repair_is_not_deleted(tmp_path):
    d = build_defect_store(tmp_path / "z")
    newer = prop(mtime="1800000000000")
    st = Racy(d, "CCCC2222.prop", 2, lambda: (d / "CCCC2222.prop").write_bytes(newer))
    res = repair(st, apply=True, quarantine=tmp_path / "q")
    assert ("REPAIR_FAILED", "CCCC2222.prop") in codes_for(res)
    assert (d / "CCCC2222.prop").read_bytes() == newer


def test_zip_appearing_during_orphan_repair_keeps_the_prop(tmp_path):
    d = build_defect_store(tmp_path / "z")
    st = Racy(
        d, "CCCC2222.prop", 2, lambda: (d / "CCCC2222.zip").write_bytes(make_zip({"c.txt": GOOD}))
    )
    res = repair(st, apply=True, quarantine=tmp_path / "q")
    assert ("REPAIR_FAILED", "CCCC2222.prop") in codes_for(res)
    assert (d / "CCCC2222.prop").exists()


# --- 13. fullmatch -----------------------------------------------------------


def test_trailing_newline_is_not_valid(tmp_path):
    (tmp_path / "AAAA2222.zip").write_bytes(make_zip({"a.txt": GOOD}))
    (tmp_path / "AAAA2222.prop").write_bytes(
        f'<properties version="1"><mtime>1700000000000\n</mtime><hash>{md5(GOOD)}\n</hash></properties>'.encode()
    )
    got = codes_for(integrity(DirStore(tmp_path)))
    assert {("PROP_BAD_MTIME", "AAAA2222.prop"), ("PROP_BAD_HASH", "AAAA2222.prop")} <= got


# --- 14. terminal escapes ----------------------------------------------------


def test_render_text_escapes_control_characters():
    r = Result(
        "integrity",
        "dir\x1b[2J",
        "t",
        [Finding("error", "X", "evil\x1b]0;pwned\x07.zip", "a\x1b[31mb\nc")],
    )
    out = za.render_text(r)
    assert "\x1b" not in out and "\x07" not in out
    assert "\\x1b]0;pwned\\x07" in out


# --- 15. untrusted XML -------------------------------------------------------


def test_dtd_in_propfind_is_rejected():
    body = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><multistatus xmlns="DAV:"/>'
    with pytest.raises(AuditError, match="DTD"):
        za._parse_multistatus(body, "/zotero/")


def test_dtd_and_oversized_prop_are_unparseable(tmp_path):
    for n, body in (
        ("AAAA2222", b'<!DOCTYPE p [<!ENTITY a "b">]><properties version="1"/>'),
        ("BBBB2222", b'<properties version="1">' + b" " * (4 * 1024 * 1024) + b"</properties>"),
    ):
        (tmp_path / f"{n}.zip").write_bytes(make_zip({"a.txt": GOOD}))
        (tmp_path / f"{n}.prop").write_bytes(body)
    got = codes_for(integrity(DirStore(tmp_path)))
    assert {("PROP_UNPARSEABLE", "AAAA2222.prop"), ("PROP_UNPARSEABLE", "BBBB2222.prop")} <= got


def test_oversized_propfind_is_refused(fakedav, monkeypatch):
    url, srv = fakedav
    monkeypatch.setattr(za, "PROPFIND_MAX", 50)
    srv.files["AAAA2222.prop"] = b"x"
    with pytest.raises(AuditError, match="larger"):
        DavStore(url, USER, PASSWORD).list()


# --- 17. internal errors -----------------------------------------------------


def test_unexpected_error_is_one_line_exit_2(tmp_path, monkeypatch, capsys):
    def boom(args):
        raise ValueError("kaboom")

    monkeypatch.setattr(za, "_run", boom)
    assert main(["integrity", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert err == "zodav-audit: unexpected error: ValueError: kaboom\n"


# --- 18. failed watch run ----------------------------------------------------


def test_failed_run_replaces_the_report_and_alerts(tmp_path, monkeypatch, hook):
    url, got = hook
    d = store(tmp_path)
    run(tmp_path, d)
    before = (tmp_path / "rep" / "state.json").read_text()
    calls = []

    def boom(*a, **k):
        calls.append(1)
        if len(calls) > 1:
            raise KeyboardInterrupt
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(za, "watch_once", boom)
    monkeypatch.setattr(za.time, "sleep", lambda s: None)
    monkeypatch.setenv("ZODAV_ALERT_URL", url)
    with pytest.raises(KeyboardInterrupt):
        main(["watch", str(d), "--reports", str(tmp_path / "rep")])
    doc = json.loads((tmp_path / "rep" / "latest.json").read_text())
    assert [f["code"] for f in doc["findings"]] == ["WATCH_RUN_FAILED"] and doc["ok"] is False
    assert "WATCH_RUN_FAILED" in (tmp_path / "rep" / "latest.html").read_text()
    assert (tmp_path / "rep" / "state.json").read_text() == before
    assert len(got) == 1 and b"disk exploded" in got[0]["body"]
