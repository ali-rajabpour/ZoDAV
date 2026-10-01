import pytest
from fakedav import (
    DEFECT_EXPECTED,
    PASSWORD,
    USER,
    build_clean_store,
    build_defect_store,
    fakedav,
    finding_set,
)  # noqa: F401  (fakedav is a fixture)
from zodav_audit import DavStore, DirStore, integrity, zotero_url

STACK_URL = "http://127.0.0.1:18080/"
STACK_USER, STACK_PW = "zotero", "test-zotero-password-0123456789"


def test_dirstore_finds_every_defect(tmp_path):
    build_defect_store(tmp_path)
    res = integrity(DirStore(tmp_path))
    assert finding_set(res) == DEFECT_EXPECTED
    assert not res.ok
    assert res.stats["attachments"] == 11


def test_error_findings_explain_effect_and_fix(tmp_path):
    build_defect_store(tmp_path)
    for f in integrity(DirStore(tmp_path)).findings:
        if f.severity in ("error", "warning"):
            assert f.effect and f.fix, f.code


def test_clean_store_is_ok(tmp_path):
    build_clean_store(tmp_path)
    res = integrity(DirStore(tmp_path))
    assert res.ok and res.findings == []


def test_davstore_matches_dirstore(tmp_path, fakedav):
    url, srv = fakedav
    build_defect_store(tmp_path)
    srv.load_dir(tmp_path)
    via_dir = finding_set(integrity(DirStore(tmp_path)))
    via_dav = finding_set(integrity(DavStore(url, USER, PASSWORD)))
    assert via_dav == via_dir == DEFECT_EXPECTED


def test_sample_checks_about_half(tmp_path):
    build_clean_store(tmp_path, count=20)
    res = integrity(DirStore(tmp_path), sample_percent=50)
    assert res.stats["attachments"] == 20
    assert res.stats["checked"] == 10


def test_sample_keeps_zip_and_prop_together(tmp_path):
    build_defect_store(tmp_path)
    res = integrity(DirStore(tmp_path), sample_percent=30)
    assert res.stats["checked"] < res.stats["attachments"]
    for f in res.findings:
        assert not (f.code == "MISSING_ZIP" and f.subject != "CCCC2222.prop")


def test_zotero_url_normalisation():
    assert zotero_url("example.org") == "https://example.org/zotero/"
    assert zotero_url("http://h:8080/dav") == "http://h:8080/dav/zotero/"
    assert zotero_url("https://user:secret@h/x/") == "https://h/x/zotero/"


def test_davstore_errors_on_bad_login(fakedav):
    from zodav_audit import AuditError

    url, _ = fakedav
    with pytest.raises(AuditError, match="rejected"):
        DavStore(url, USER, "wrong").list()


# --- needs the ZoDAV test stack -------------------------------------------


@pytest.mark.docker
@pytest.mark.usefixtures("stack")
def test_stack_url_matches_directory(tmp_path):
    build_defect_store(tmp_path)
    store = DavStore(STACK_URL, STACK_USER, STACK_PW)
    names = sorted(p.name for p in tmp_path.iterdir())
    try:
        for n in names:
            store.write(n, (tmp_path / n).read_bytes())
        got = {x for x in finding_set(integrity(store)) if x[1] in names}
    finally:
        for n in names:
            store.delete(n)
    assert got == DEFECT_EXPECTED
