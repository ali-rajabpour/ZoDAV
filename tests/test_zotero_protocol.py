import concurrent.futures
import socket
import time

import pytest
from davclient import HOST, PORT, SMALL_PORT, USERS, basic, request

pytestmark = pytest.mark.usefixtures("stack")

PROP = (
    b'<properties version="1"><mtime>1700000000000</mtime>'
    b"<hash>d41d8cd98f00b204e9800998ecf8427e</hash></properties>"
)


def test_verify_server_sequence():
    status, headers, _ = request("OPTIONS", "/zotero/")
    assert status in (200, 204, 404) and "dav" in headers
    assert request("PROPFIND", "/zotero/", headers={"Depth": "0"})[0] == 207
    assert request("GET", "/zotero/nonexistent.prop")[0] == 404
    assert request("PUT", "/zotero/zotero-test-file.prop", body=b" ")[0] in (200, 201, 204)
    status, _, body = request("GET", "/zotero/zotero-test-file.prop")
    assert status == 200 and body == b" "
    assert request("DELETE", "/zotero/zotero-test-file.prop")[0] in (200, 204)
    assert request("GET", "/zotero/zotero-test-file.prop")[0] == 404


def test_zip_and_prop_round_trip_across_users():
    blob = bytes(range(256)) * 4096
    put = request(
        "PUT",
        "/zotero/RT2TEST9.zip",
        user="service",
        body=blob,
        headers={"Content-Type": "application/zip"},
    )
    assert put[0] in (201, 204)
    assert request("PUT", "/zotero/RT2TEST9.prop", user="service", body=PROP)[0] in (201, 204)
    status, _, body = request("GET", "/zotero/RT2TEST9.zip", user="zotero")
    assert status == 200 and body == blob
    status, _, body = request("GET", "/zotero/RT2TEST9.prop", user="zotero")
    assert status == 200 and body == PROP
    for name in ("RT2TEST9.zip", "RT2TEST9.prop"):
        assert request("DELETE", f"/zotero/{name}")[0] in (200, 204)


def test_overwrite_replaces():
    request("PUT", "/zotero/OVERWR1T.prop", body=b"one")
    assert request("PUT", "/zotero/OVERWR1T.prop", body=b"two")[0] in (200, 201, 204)
    assert request("GET", "/zotero/OVERWR1T.prop")[2] == b"two"
    request("DELETE", "/zotero/OVERWR1T.prop")


def test_depth_one_lists_getlastmodified():
    request("PUT", "/zotero/L1STTEST.prop", body=PROP)
    status, _, body = request("PROPFIND", "/zotero/", headers={"Depth": "1"})
    request("DELETE", "/zotero/L1STTEST.prop")
    assert status == 207
    text = body.decode()
    assert "L1STTEST.prop" in text
    assert "getlastmodified" in text


@pytest.mark.parametrize("name", ["lastsync", "lastsync.txt"])
def test_lastsync_round_trip(name):
    assert request("PUT", f"/zotero/{name}", body=b"1700000000")[0] in (201, 204)
    assert request("GET", f"/zotero/{name}")[2] == b"1700000000"
    assert request("DELETE", f"/zotero/{name}")[0] in (200, 204)


def test_mkcol_on_existing_zotero_is_405():
    assert request("MKCOL", "/zotero/")[0] == 405


def _raw(port, head, send=b"", read=True):
    s = socket.create_connection((HOST, port), timeout=30)
    s.sendall(head.encode() + send)
    if not read:
        return s
    try:
        return s.recv(4096).decode(errors="replace")
    finally:
        s.close()


def _put_head(path, length, port=PORT, user="zotero"):
    pw = USERS[user]
    return (
        f"PUT {path} HTTP/1.1\r\nHost: {HOST}:{port}\r\n"
        f"Authorization: {basic(user, pw)}\r\n"
        f"Content-Length: {length}\r\nConnection: close\r\n\r\n"
    )


def test_body_over_limit_is_413_before_upload():
    reply = _raw(PORT, _put_head("/zotero/TOOBIG01.zip", 1024**3 + 1))
    assert reply.startswith("HTTP/1.1 413")


def test_interrupted_put_keeps_previous_version():
    request("PUT", "/zotero/1NTERRUP.zip", body=b"previous version")
    s = _raw(
        PORT, _put_head("/zotero/1NTERRUP.zip", 4 * 1024 * 1024), send=b"x" * 100_000, read=False
    )
    s.close()
    time.sleep(1)
    status, _, body = request("GET", "/zotero/1NTERRUP.zip")
    request("DELETE", "/zotero/1NTERRUP.zip")
    assert status == 200 and body == b"previous version"


def test_disk_full_is_507_and_keeps_previous_version():
    path = "/zotero/FULLDISK.zip"
    assert request("PUT", path, port=SMALL_PORT, body=b"previous version")[0] in (201, 204)
    status, _, _ = request("PUT", path, port=SMALL_PORT, body=b"x" * (6 * 1024 * 1024))
    assert status == 507
    status, _, body = request("GET", path, port=SMALL_PORT)
    assert status == 200 and body == b"previous version"
    request("DELETE", path, port=SMALL_PORT)


def test_concurrent_puts_from_both_users():
    def put(i):
        user = "zotero" if i % 2 else "service"
        return request("PUT", f"/zotero/C0NCUR{i:02d}.prop", user=user, body=PROP)[0]

    with concurrent.futures.ThreadPoolExecutor(20) as pool:
        codes = list(pool.map(put, range(20)))
    for i in range(20):
        request("DELETE", f"/zotero/C0NCUR{i:02d}.prop")
    assert all(c in (201, 204) for c in codes), codes


def test_authenticated_requests_stay_fast():
    start = time.time()
    for _ in range(100):
        assert request("PROPFIND", "/zotero/", headers={"Depth": "0"})[0] == 207
    assert time.time() - start < 10
