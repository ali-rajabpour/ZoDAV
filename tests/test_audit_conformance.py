import pytest
from fakedav import PASSWORD, USER, fakedav  # noqa: F401  (fakedav is a fixture)
from zodav_audit import PROVIDER_HINTS, conformance, provider_hint

ALL_STEPS = {
    "options",
    "propfind",
    "nonexistent-file",
    "upload",
    "download",
    "delete",
    "auth-challenge",
    "propfind-depth1",
    "roundtrip",
    "missing-zip-404",
    "no-redirect",
}


def run(fakedav, **kw):
    url, srv = fakedav
    return conformance(url, USER, kw.pop("password", PASSWORD), **kw), srv


def codes(res):
    return {f.code for f in res.findings if f.severity != "info"}


def test_clean_server_passes(fakedav):
    res, srv = run(fakedav)
    assert res.ok and codes(res) == set(), res.findings
    assert {f.subject for f in res.findings if f.code == "STEP_OK"} == ALL_STEPS
    assert srv.files == {}


def test_verify_sequence_order_and_codes(fakedav):
    res, srv = run(fakedav)
    seq = [(m, p) for m, p, _ in srv.requests]
    expect = [
        ("OPTIONS", "/zotero/"),
        ("PROPFIND", "/zotero/"),
        ("GET", "/zotero/nonexistent.prop"),
        ("PUT", "/zotero/zotero-test-file.prop"),
        ("GET", "/zotero/zotero-test-file.prop"),
        ("DELETE", "/zotero/zotero-test-file.prop"),
    ]
    assert seq[:6] == expect


@pytest.mark.parametrize(
    "switch,expected",
    [
        ({"no_dav_header": True}, {"NOT_DAV"}),
        ({"soft_404": True}, {"NONEXISTENT_FILE_NOT_MISSING", "SOFT_404"}),
        ({"no_lastmodified": True}, {"NO_LASTMODIFIED"}),
        ({"redirect_zotero": True}, {"REDIRECT"}),
        ({"require_auth": False}, {"NO_AUTH_CHALLENGE"}),
        ({"has_zotero_dir": False}, {"ZOTERO_DIR_NOT_FOUND"}),
        ({"has_zotero_dir": False, "has_parent": False}, {"PARENT_DIR_NOT_FOUND"}),
        ({"drop_uploads": True}, {"FILE_MISSING_AFTER_UPLOAD"}),
        ({"mangle_get": True}, {"ROUNDTRIP_MISMATCH"}),
        ({"force_status": {"PUT": 507}}, {"UPLOAD_FAILED"}),
        ({"force_status": {"DELETE": 500}}, {"DELETE_FAILED"}),
        ({"force_status": {"OPTIONS": 500}}, {"SERVER_ERROR"}),
        ({"force_status": {"PROPFIND": 403}}, {"FORBIDDEN"}),
    ],
)
def test_misbehaviour_codes(fakedav, switch, expected):
    url, srv = fakedav
    for k, v in switch.items():
        setattr(srv, k, v)
    res = conformance(url, USER, PASSWORD)
    assert expected <= codes(res), res.findings
    for f in res.findings:
        if f.severity in ("error", "warning"):
            assert f.effect and f.fix, f.code


def test_wrong_password_is_auth_failed(fakedav):
    res, _ = run(fakedav, password="not-the-password")
    assert "AUTH_FAILED" in codes(res) and not res.ok


def test_unreachable():
    res = conformance("http://127.0.0.1:1/", USER, "x")
    assert [f.code for f in res.findings] == ["UNREACHABLE"]


def test_read_only_sends_no_writes(fakedav):
    res, srv = run(fakedav, read_only=True)
    assert res.ok, res.findings
    assert not {"PUT", "DELETE", "MKCOL"} & set(srv.methods())


def test_test_files_removed_when_a_step_fails_midway(fakedav):
    url, srv = fakedav
    # the round-trip download is rejected after both files were uploaded
    srv.force_status = {("GET", "ZODAV0TS.zip"): 401}
    res = conformance(url, USER, PASSWORD)
    assert "AUTH_FAILED" in codes(res)
    assert srv.files == {}


def test_test_files_removed_when_upload_partly_fails(fakedav):
    url, srv = fakedav
    srv.force_status = {("PUT", "ZODAV0TS.prop"): 500}
    res = conformance(url, USER, PASSWORD)
    assert "UPLOAD_FAILED" in codes(res)
    assert srv.files == {}


def test_reserved_key_is_outside_zotero_alphabet():
    assert "0" not in "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"


@pytest.mark.parametrize(
    "url",
    [
        "https://cloud.example.org/remote.php/dav/files/me/",
        "https://nextcloud.example.org/",
        "https://nas.synology.me:5006/",
        "https://app.koofr.net/dav/Koofr",
        "https://webdav.pcloud.com",
        "https://dav.jianguoyun.com/dav/",
        "https://webdav.4shared.com",
        "https://dav.box.com/dav",
    ],
)
def test_provider_hints_match(url):
    assert provider_hint(url)


def test_no_hint_for_unknown_host():
    assert provider_hint("https://files.example.net/") is None
    assert len(PROVIDER_HINTS) >= 7


def test_hint_is_added_to_failed_run(fakedav, monkeypatch):
    import zodav_audit

    monkeypatch.setattr(zodav_audit, "provider_hint", lambda u: "a hint")
    url, srv = fakedav
    srv.no_dav_header = True
    res = conformance(url, USER, PASSWORD)
    assert any(f.code == "PROVIDER_HINT" for f in res.findings)


# --- needs the ZoDAV test stack -------------------------------------------


@pytest.mark.docker
@pytest.mark.usefixtures("stack")
def test_stack_conforms():
    res = conformance("http://127.0.0.1:18080/", "zotero", "test-zotero-password-0123456789")
    assert res.ok and codes(res) == set(), res.findings
    assert {f.subject for f in res.findings if f.code == "STEP_OK"} == ALL_STEPS


@pytest.mark.docker
@pytest.mark.usefixtures("stack")
def test_stack_read_only_and_wrong_password():
    ro = conformance(
        "http://127.0.0.1:18080/", "zotero", "test-zotero-password-0123456789", read_only=True
    )
    assert ro.ok, ro.findings
    bad = conformance("http://127.0.0.1:18080/", "zotero", "wrong-password-xxxxxxxxxxxxxxxx")
    assert "AUTH_FAILED" in codes(bad)
