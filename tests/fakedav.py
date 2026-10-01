"""In-process WebDAV fake for zodav-audit tests, plus store-building helpers.

Switches on the server object (all default to well-behaved):
  no_dav_header, soft_404, no_lastmodified, redirect_zotero, require_auth,
  has_zotero_dir, has_parent, drop_uploads, mangle_get,
  force_status  {"PUT": 507} or {("GET", "NAME"): 500}
"""

import base64
import hashlib
import io
import threading
import zipfile
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote
from xml.sax.saxutils import escape

import pytest

USER = "zotero"
PASSWORD = "fake-Sentinel-pw-48151623"
KEY_CHARS = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
ENTRY_TIME = (2024, 5, 17, 12, 30, 0)


class FakeDav(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.files = {}
        self.requests = []  # (method, path, authenticated)
        self.no_dav_header = False
        self.soft_404 = False
        self.no_lastmodified = False
        self.redirect_zotero = False
        self.require_auth = True
        self.has_zotero_dir = True
        self.has_parent = True
        self.drop_uploads = False
        self.mangle_get = False
        self.force_status = {}

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/"

    def load_dir(self, path):
        for p in Path(path).iterdir():
            if p.is_file():
                self.files[p.name] = p.read_bytes()

    def methods(self):
        return [m for m, _, _ in self.requests]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, body=b"", headers=None):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authed(self):
        want = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        return self.headers.get("Authorization") == want

    def _handle(self):
        srv = self.server
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        path = unquote(self.path.split("?")[0])
        srv.requests.append((self.command, path, "Authorization" in self.headers))

        if srv.redirect_zotero and path.startswith("/zotero"):
            return self._send(301, headers={"Location": "/zotero-moved/"})
        if srv.require_auth and not self._authed():
            return self._send(401, headers={"WWW-Authenticate": 'Basic realm="fake"'})
        name = path[len("/zotero/") :] if path.startswith("/zotero/") else None
        forced = srv.force_status.get((self.command, name)) or srv.force_status.get(self.command)
        if forced:
            return self._send(forced)

        m = self.command
        if m == "OPTIONS":
            h = {"Allow": "OPTIONS, GET, PUT, DELETE, PROPFIND"}
            if not srv.no_dav_header:
                h["DAV"] = "1"
            return self._send(200, headers=h)
        if m == "PROPFIND":
            return self._propfind(path, name)
        if name is None or name == "":
            return self._send(404)
        if m == "GET":
            if name in srv.files:
                data = srv.files[name]
                if srv.mangle_get:
                    data = data[:-1] + bytes([data[-1] ^ 0xFF])
                return self._send(200, data)
            if srv.soft_404:
                return self._send(
                    200, b"<html><body>Not found</body></html>", {"Content-Type": "text/html"}
                )
            return self._send(404)
        if m == "PUT":
            new = name not in srv.files
            if not srv.drop_uploads:
                srv.files[name] = body
            return self._send(201 if new else 204)
        if m == "DELETE":
            return self._send(204 if srv.files.pop(name, None) is not None else 404)
        return self._send(405)

    do_OPTIONS = do_GET = do_PUT = do_DELETE = do_PROPFIND = do_HEAD = do_MKCOL = _handle

    def _item(self, href, size=None, collection=False):
        srv = self.server
        props = ""
        if size is not None:
            props += f"<D:getcontentlength>{size}</D:getcontentlength>"
        if not srv.no_lastmodified:
            props += f"<D:getlastmodified>{formatdate(1700000000, usegmt=True)}</D:getlastmodified>"
        props += (
            "<D:resourcetype><D:collection/></D:resourcetype>"
            if collection
            else "<D:resourcetype/>"
        )
        return (
            f"<D:response><D:href>{escape(quote(href))}</D:href><D:propstat><D:prop>{props}</D:prop>"
            "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
        )

    def _propfind(self, path, name):
        srv = self.server
        depth = self.headers.get("Depth", "infinity")
        items = []
        if path == "/zotero/" or path == "/zotero":
            if not srv.has_zotero_dir:
                return self._send(404)
            items.append(self._item("/zotero/", collection=True))
            if depth == "1":
                items += [self._item(f"/zotero/{n}", len(d)) for n, d in sorted(srv.files.items())]
        elif path == "/":
            if not srv.has_parent:
                return self._send(404)
            items.append(self._item("/", collection=True))
        elif name and name in srv.files:
            items.append(self._item(path, len(srv.files[name])))
        else:
            return self._send(404)
        xml = (
            '<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">'
            + "".join(items)
            + "</D:multistatus>"
        )
        self._send(207, xml.encode(), {"Content-Type": "application/xml"})


@pytest.fixture
def fakedav():
    srv = FakeDav()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.base_url, srv
    finally:
        srv.shutdown()
        srv.server_close()


# --- store builders --------------------------------------------------------


def make_zip(files, corrupt=False):
    """files: {entry name: bytes}. corrupt=True flips a payload byte (stored, so CRC fails)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for n, d in files.items():
            z.writestr(zipfile.ZipInfo(n, ENTRY_TIME), d)
    data = bytearray(buf.getvalue())
    if corrupt:
        data[data.index(next(iter(files.values())))] ^= 0xFF
    return bytes(data)


def prop(mtime="1700000000000", md5="d41d8cd98f00b204e9800998ecf8427e"):
    return f'<properties version="1"><mtime>{mtime}</mtime><hash>{md5}</hash></properties>'.encode()


def md5(b):
    return hashlib.md5(b).hexdigest()


GOOD = b"good attachment content"
OTHER = b"another file"


def build_clean_store(path, count=5):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        k = f"CLEA{KEY_CHARS[i]}222"
        content = f"clean {i}".encode()
        (path / f"{k}.zip").write_bytes(make_zip({"f.txt": content}))
        (path / f"{k}.prop").write_bytes(prop(md5=md5(content)))
    return path


# One of each defect. name -> expected (code, subject) set contribution.
DEFECT_EXPECTED = {
    ("MISSING_PROP", "BBBB2222.zip"),
    ("MISSING_ZIP", "CCCC2222.prop"),
    ("PROP_BAD_HASH", "DDDD2222.prop"),
    ("CORRUPT_ZIP", "EEEE2222.zip"),
    ("HASH_MISMATCH", "FFFF2222.zip"),
    ("PROP_BAD_MTIME", "GGGG2222.prop"),
    ("EMPTY_FILE", "HHHH2222.zip"),
    ("MULTI_FILE_ZIP", "JJJJ2222.zip"),
    ("PROP_UNPARSEABLE", "KKKK2222.prop"),
    ("MISSING_PROP", "LLLL2222.zip"),
    ("MULTI_FILE_ZIP", "LLLL2222.zip"),
    ("UNEXPECTED_FILE", "readme.txt"),
}


def build_defect_store(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)

    def put(name, data):
        (path / name).write_bytes(data)

    put("AAAA2222.zip", make_zip({"a.txt": GOOD}))
    put("AAAA2222.prop", prop(md5=md5(GOOD)))
    put("BBBB2222.zip", make_zip({"b.txt": GOOD}))  # no prop
    put("CCCC2222.prop", prop(md5=md5(GOOD)))  # no zip
    put("DDDD2222.zip", make_zip({"d.txt": GOOD}))
    put("DDDD2222.prop", prop(mtime="1700000001234", md5="undefined"))
    put("EEEE2222.zip", make_zip({"e.txt": GOOD}, corrupt=True))
    put("EEEE2222.prop", prop(md5=md5(GOOD)))
    put("FFFF2222.zip", make_zip({"f.txt": GOOD}))
    put("FFFF2222.prop", prop(md5=md5(OTHER)))
    put("GGGG2222.zip", make_zip({"g.txt": GOOD}))
    put("GGGG2222.prop", prop(mtime="abc", md5=md5(GOOD)))
    put("HHHH2222.zip", b"")
    put("HHHH2222.prop", prop())
    put("JJJJ2222.zip", make_zip({"1.txt": GOOD, "2.txt": OTHER}))
    put("JJJJ2222.prop", prop(md5=md5(GOOD)))
    put("KKKK2222.zip", make_zip({"k.txt": GOOD}))
    put("KKKK2222.prop", b"this is not xml")
    put("LLLL2222.zip", make_zip({"1.txt": GOOD, "2.txt": OTHER}))  # multi-file, no prop
    put("lastsync.txt", b"1700000000")
    put("readme.txt", b"hello")
    return path


def snapshot(path):
    return {p.name: p.read_bytes() for p in Path(path).iterdir() if p.is_file()}


def finding_set(result):
    return {
        (f.code, f.subject)
        for f in result.findings
        if f.severity != "info" or f.code in ("MULTI_FILE_ZIP", "UNEXPECTED_FILE")
    }
