#!/usr/bin/env python3
"""zodav-audit: conformance, integrity and repair checks for Zotero WebDAV storage.

Copyright (C) 2026 Ali Rajabpour Sanati <ali@rajabpour.com>
https://rajabpour.com · https://github.com/ali-rajabpour/ZoDAV
License: AGPL-3.0-only

Standard library only, Python 3.10+. See docs/SPEC.md for behaviour.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import html
import http.client
import io
import json
import os
import random
import re
import shutil
import smtplib
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

VERSION = "1.0.1"
PROJECT_URL = "https://rajabpour.com"
SEVERITIES = ("error", "warning", "info")

KEY_ALPHABET = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
# Always used with fullmatch: "$" would also accept a trailing newline, which Zotero's JS regexes do not.
_NAME_RE = re.compile(rf"([{KEY_ALPHABET}]{{8}})\.(zip|prop)")
_MTIME_RE = re.compile(r"[0-9]{1,13}")  # Zotero accepts 1-10 (s) or 1-13 (ms) digits
_HASH_RE = re.compile(r"[0-9a-fA-F]{32}")
IGNORED_NAMES = {"lastsync", "lastsync.txt"}
TMP_PREFIX = ".zodav-tmp-"

# The audit container has 256 MB of RAM: nothing here may hold a whole attachment in memory.
CHUNK = 1024 * 1024
MAX_UNCOMPRESSED = 8 * 1024**3  # a zip declaring more than this is reported, not decompressed
XML_MAX = 4 * 1024 * 1024  # .prop files and other small XML bodies
PROPFIND_MAX = 64 * 1024 * 1024  # a directory listing can be long

# "0" is not in Zotero's key alphabet, so this can never collide with a real item.
TEST_KEY = "ZODAV0TS"
TEST_ZIP = f"{TEST_KEY}.zip"
TEST_PROP = f"{TEST_KEY}.prop"
ZOTERO_TEST_FILE = "zotero-test-file.prop"


class AuditError(Exception):
    """The audit could not run (exit code 2)."""


class Unreachable(AuditError):
    pass


@dataclass
class Finding:
    severity: str
    code: str
    subject: str
    message: str
    effect: str = ""
    fix: str = ""


@dataclass
class Result:
    command: str
    target: str
    started: str
    findings: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    def counts(self) -> dict:
        return {s: sum(1 for f in self.findings if f.severity == s) for s in SEVERITIES}

    def to_dict(self) -> dict:
        c = self.counts()
        return {
            "command": self.command,
            "target": self.target,
            "started": self.started,
            "ok": self.ok,
            "summary": {"errors": c["error"], "warnings": c["warning"], "info": c["info"]},
            "findings": [asdict(f) for f in self.findings],
            "stats": self.stats,
        }


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- finding texts --------------------------------------------------------
# effect / fix are written for a Zotero user, not a server admin.

_RESET = (
    "On the computer that still has the file, open Zotero > Settings > Sync > Reset "
    "and choose 'Reset File Sync History', then sync again. Zotero uploads the file again."
)
_REPAIR = "Run 'zodav-audit repair' (a dry run shows what it would do); add --apply to do it."

_TEXT = {
    # integrity
    "PROP_UNPARSEABLE": (
        "Zotero cannot read the timestamp and checksum stored for this attachment, so file sync "
        "for it will not work properly.",
        _REPAIR + " If the .zip is intact it writes a fresh .prop. Otherwise: " + _RESET,
    ),
    "PROP_BAD_MTIME": (
        "Zotero deletes a .prop whose modification time is not a plain number, so this attachment's "
        "sync record will disappear and the file will be uploaded again or look missing.",
        _REPAIR + " It rewrites the .prop from the .zip.",
    ),
    "PROP_BAD_HASH": (
        "Zotero cannot check the file against its checksum (a hash of 'undefined' is a known Zotero "
        "bug, zotero/zotero#3573), so this attachment may be downloaded again on every sync or rejected.",
        _REPAIR + " It rewrites the .prop with the real checksum.",
    ),
    "MISSING_ZIP": (
        "Other computers see a sync record but no file. Zotero gets a 404 when it downloads it and "
        "deletes the .prop itself, so the attachment shows as missing until a computer that has the "
        "file uploads it again.",
        "If a computer still has the file, Zotero re-uploads it once the .prop is gone. "
        "'zodav-audit repair --apply' keeps a copy of the .prop and removes it from the store.",
    ),
    "MISSING_PROP": (
        "Zotero does not know this file exists on the server, so other computers will not download "
        "it, and Zotero's cleanup of leftover files can delete the .zip once it is more than 7 days old.",
        _REPAIR + " If the .zip holds a single file it writes the missing .prop.",
    ),
    "EMPTY_FILE": (
        "Zotero would download a file with nothing in it and fail to open the attachment.",
        _RESET,
    ),
    "CORRUPT_ZIP": (
        "Other computers download this file but cannot unpack it, so the attachment will not open there.",
        _RESET,
    ),
    "HASH_MISMATCH": (
        "The file inside the .zip is not the file the .prop describes. Zotero compares the two to decide "
        "whether to download, so it may keep re-downloading or report a conflict.",
        _RESET,
    ),
    # conformance
    "UNREACHABLE": (
        "Zotero cannot connect either; Verify Server fails with a connection error.",
        "Check the address and port, that the server is running and (for https) that its certificate "
        "is valid for that name. Zotero checks the certificate too.",
    ),
    "AUTH_FAILED": (
        "Zotero's Verify Server reports an invalid username or password and nothing syncs.",
        "Re-enter the username and password in Zotero > Settings > Sync. Some providers "
        "(Nextcloud, Koofr, Jianguoyun) need an app password instead of the account password.",
    ),
    "FORBIDDEN": (
        "Zotero shows 'permission denied' and cannot read or write files.",
        "Give this account read and write access to the folder, and allow the WebDAV methods "
        "Zotero uses (GET, PUT, DELETE, PROPFIND, MKCOL).",
    ),
    "NOT_DAV": (
        "Zotero's Verify Server says the address is not a WebDAV server.",
        "Check that the address points at the WebDAV endpoint (not a web page), that the WebDAV "
        "module is switched on, and that any proxy in front passes the DAV header and the PROPFIND method.",
    ),
    "ZOTERO_DIR_NOT_FOUND": (
        "Zotero offers to create the 'zotero' folder; until it exists nothing can be stored.",
        "Accept Zotero's offer to create the folder, or create a folder named 'zotero' at that address.",
    ),
    "PARENT_DIR_NOT_FOUND": (
        "Zotero reports that the folder above 'zotero' does not exist, so Verify Server fails.",
        "The address is probably wrong or has a typo in the folder path. Fix the URL in Zotero's WebDAV settings.",
    ),
    "NONEXISTENT_FILE_NOT_MISSING": (
        "Zotero refuses this server: it answers 'found' for files that do not exist, so Zotero could "
        "never tell which files are missing.",
        "Make the server return a real 404 for missing files (turn off any catch-all or custom error "
        "page that answers 200).",
    ),
    "FILE_MISSING_AFTER_UPLOAD": (
        "Zotero refuses this server: a file it just uploaded cannot be read back.",
        "The server accepts uploads but does not keep or serve them. Check write permission on the "
        "folder, caching, and that reads and writes use the same location.",
    ),
    "UPLOAD_FAILED": (
        "Zotero cannot save attachment files on this server. A 507 is shown as 'insufficient space'; "
        "other failures as a generic file sync error.",
        "Check free space and write permission on the server, and that the 'zotero' folder exists.",
    ),
    "DELETE_FAILED": (
        "Zotero cannot delete files here, so Verify Server fails or stale files stay behind.",
        "Allow DELETE for this account on the folder. If a test file was left behind, remove "
        "'zotero-test-file.prop' or 'ZODAV0TS.zip'/'ZODAV0TS.prop' by hand.",
    ),
    "SERVER_ERROR": (
        "Zotero shows a generic server error and file sync stops.",
        "Look at the server's own log for the error behind this response.",
    ),
    "NO_AUTH_CHALLENGE": (
        "Zotero may not send the login at all, or anyone who can reach this address can read and write "
        "your files.",
        "Require a login on the folder and answer unauthenticated requests with 401 and "
        "'WWW-Authenticate: Basic'.",
    ),
    "NO_LASTMODIFIED": (
        "Zotero cannot tell how old files are, so its cleanup of leftover files may not work.",
        "Make the server include 'getlastmodified' in directory listings (PROPFIND Depth: 1).",
    ),
    "ROUNDTRIP_MISMATCH": (
        "Files come back different from what was uploaded, so attachments would be stored or "
        "delivered corrupted.",
        "Look for a proxy or filter that rewrites or compresses request or response bodies and turn it off for this folder.",
    ),
    "SOFT_404": (
        "When a file is missing the server answers 200 with a page instead of 404. Zotero saves that "
        "page as the attachment file, and the item looks synced but cannot open.",
        "Make the server return a real 404 for missing files (turn off any catch-all or custom error page that answers 200).",
    ),
    "REDIRECT": (
        "Zotero does not re-send WebDAV requests across a redirect, so uploads fail.",
        "Enter the final address, exactly as the server redirects to it, in Zotero's WebDAV settings.",
    ),
    "WATCH_RUN_FAILED": (
        "The scheduled check could not run, so this report says nothing about the state of the files.",
        "Look at the audit container's log for the error and fix it; the next run starts on schedule.",
    ),
    "REPAIR_FAILED": (
        "Nothing was deleted; the store is unchanged for this item.",
        "Check write permission on the store and on the quarantine folder, then run repair again.",
    ),
}


def _finding(severity: str, code: str, subject: str, message: str) -> Finding:
    effect, fix = _TEXT.get(code, ("", "")) if severity != "info" else ("", "")
    return Finding(severity, code, subject, message, effect, fix)


# --- URLs -----------------------------------------------------------------


def zotero_url(url: str) -> str:
    """What Zotero requests: the typed URL, https if no scheme, plus 'zotero/'.
    Credentials embedded in the URL are dropped."""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    u = urlsplit(url)
    if u.scheme not in ("http", "https"):
        raise AuditError(f"unsupported URL scheme '{u.scheme}'; use http or https")
    try:
        port = u.port
    except ValueError:
        raise AuditError("the URL has an invalid port") from None
    host = u.hostname or ""
    if not host:
        raise AuditError("the URL has no host name")
    if ":" in host:
        host = f"[{host}]"
    path = u.path or "/"
    if not path.endswith("/"):
        path += "/"
    return f"{u.scheme}://{host}{':' + str(port) if port else ''}{path}zotero/"


def _clean_location(loc: str) -> str:
    u = urlsplit(loc)
    netloc = u.netloc.rsplit("@", 1)[-1]
    return (f"{u.scheme}://" if u.scheme else "") + netloc + u.path


# --- provider hints -------------------------------------------------------

PROVIDER_HINTS = [
    (
        r"nextcloud|owncloud|remote\.php",
        "Nextcloud/ownCloud: use https://HOST/remote.php/dav/files/USERNAME/ as the URL and an app "
        "password (Settings > Security) instead of your login password.",
    ),
    (
        r"synology|quickconnect|diskstation|:5006",
        "Synology: install and enable the WebDAV Server package; HTTPS WebDAV listens on port 5006 "
        "(https://HOST:5006/), plain HTTP on 5005.",
    ),
    (
        r"koofr",
        "Koofr: use https://app.koofr.net/dav/Koofr and an app password created in Koofr's preferences.",
    ),
    (
        r"pcloud",
        "pCloud: use https://webdav.pcloud.com (US) or https://ewebdav.pcloud.com (EU) matching your account region.",
    ),
    (
        r"jianguoyun",
        "Jianguoyun: use https://dav.jianguoyun.com/dav/ and an application password from the account's security settings.",
    ),
    (r"4shared", "4shared: use https://webdav.4shared.com."),
    (
        r"(^|\.)box\.com",
        "Box has discontinued WebDAV, so Zotero cannot use it for file storage; pick another provider.",
    ),
]


def provider_hint(url: str) -> str | None:
    u = urlsplit(url if "://" in url else "https://" + url)
    probe = f"{(u.hostname or '').lower()}:{u.port or ''}{u.path}".lower()
    for pattern, hint in PROVIDER_HINTS:
        if re.search(pattern, probe):
            return hint
    return None


# --- HTTP -----------------------------------------------------------------


@dataclass
class _Resp:
    status: int
    reason: str
    headers: dict
    body: bytes


class _Http:
    """One connection per request, no redirects, TLS verified, Basic auth sent up front."""

    def __init__(self, url: str, user: str, password: str, timeout: float):
        u = urlsplit(url)
        self.scheme, self.host, self.port = u.scheme, u.hostname, u.port
        self.netloc = u.netloc.rsplit("@", 1)[-1]
        self.base_path = u.path
        self.timeout = timeout
        token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        self._auth = "Basic " + token

    def request(
        self, method, path, body=None, headers=None, auth=True, limit=None, sink=None
    ) -> _Resp:
        """`limit` caps the response body (AuditError beyond it). With `sink`, a 200 body is
        streamed into that file object instead of being returned."""
        hdrs = {"User-Agent": f"zodav-audit/{VERSION}", "Connection": "close"}
        if auth:
            hdrs["Authorization"] = self._auth
        hdrs.update(headers or {})
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        conn = cls(self.host, self.port, timeout=self.timeout)
        try:
            conn.request(method, path, body=body, headers=hdrs)
            r = conn.getresponse()
            if sink is not None and r.status == 200:
                while chunk := r.read(CHUNK):
                    sink.write(chunk)
                data = b""
            elif limit is None:
                data = r.read()
            else:
                data = r.read(limit + 1)
                if len(data) > limit:
                    raise AuditError(
                        f"the response from {self.netloc} is larger than {limit} bytes"
                    )
            return _Resp(r.status, r.reason, {k.lower(): v for k, v in r.getheaders()}, data)
        except (OSError, http.client.HTTPException) as e:
            raise Unreachable(
                f"cannot talk to {self.netloc}: {e.__class__.__name__}: {e}"
            ) from None
        finally:
            conn.close()


_PF0 = b'<?xml version="1.0"?><propfind xmlns="DAV:"><prop><getcontentlength/></prop></propfind>'
_PF1 = (
    b'<?xml version="1.0"?><propfind xmlns="DAV:"><prop><getcontentlength/>'
    b"<getlastmodified/></prop></propfind>"
)
_DAV = "{DAV:}"


@dataclass
class _Entry:
    name: str
    is_dir: bool
    size: int | None
    lastmod: str | None


def _has_dtd(body: bytes) -> bool:
    # NULs dropped so a UTF-16 body cannot hide a declaration
    flat = body.replace(b"\x00", b"")
    return b"<!DOCTYPE" in flat or b"<!ENTITY" in flat


def _parse_multistatus(body: bytes, base_path: str) -> list:
    if len(body) > PROPFIND_MAX:
        raise AuditError("the server's directory listing is too large")
    if _has_dtd(body):
        raise AuditError("the server's directory listing contains a DTD, which is not valid WebDAV")
    try:
        root = ET.fromstring(body)  # noqa: S314  DTDs are rejected above
    except ET.ParseError:
        raise AuditError("the server's directory listing is not valid WebDAV XML") from None
    base = unquote(base_path)
    out = []
    for resp in root.findall(f"{_DAV}response"):
        href = resp.findtext(f"{_DAV}href") or ""
        path = unquote(urlsplit(href.strip()).path)
        if path == base or path == base.rstrip("/"):
            continue
        name = path[len(base) :] if path.startswith(base) else path.rsplit("/", 1)[-1]
        is_dir = name.endswith("/")
        name = name.strip("/")
        if not name or "/" in name:
            continue
        size = lastmod = None
        for ps in resp.findall(f"{_DAV}propstat"):
            status = ps.findtext(f"{_DAV}status") or "200"
            prop = ps.find(f"{_DAV}prop")
            if "200" not in status or prop is None:
                continue
            if prop.find(f"{_DAV}resourcetype/{_DAV}collection") is not None:
                is_dir = True
            length = (prop.findtext(f"{_DAV}getcontentlength") or "").strip()
            if length.isdigit():
                size = int(length)
            lastmod = (prop.findtext(f"{_DAV}getlastmodified") or "").strip() or lastmod
        out.append(_Entry(name, is_dir, size, lastmod))
    return out


# --- stores ---------------------------------------------------------------


class DirStore:
    """The zotero/ directory on local disk."""

    def __init__(self, path):
        self.path = Path(path)

    def list(self) -> dict:
        out = {}
        with os.scandir(self.path) as it:
            for e in it:
                if e.name.startswith(TMP_PREFIX) or not e.is_file():
                    continue
                st = e.stat()
                out[e.name] = (st.st_size, st.st_mtime)
        return out

    def read(self, name: str, limit: int | None = None) -> bytes:
        """Whole file; with `limit`, AuditError if it is longer (meant for small files)."""
        with (self.path / name).open("rb") as f:
            data = f.read() if limit is None else f.read(limit + 1)
        if limit is not None and len(data) > limit:
            raise AuditError(f"{name} is larger than {limit} bytes")
        return data

    def open(self, name: str):
        """Binary file object for streaming; the caller closes it."""
        return (self.path / name).open("rb")

    def write(self, name: str, data: bytes) -> None:
        dest = self.path / name
        fd, tmp = tempfile.mkstemp(dir=self.path, prefix=TMP_PREFIX)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            # mkstemp makes 0600; the web server's user still has to read the result
            mode = dest.stat().st_mode & 0o777 if dest.exists() else 0o644
            os.chmod(tmp, mode)
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def delete(self, name: str) -> None:
        (self.path / name).unlink()


class DavStore:
    """The same operations over WebDAV. `url` is what the user types into Zotero;
    'zotero/' is appended here."""

    def __init__(
        self, url: str, user: str, password: str, timeout: float = 60, spool_dir: str | None = None
    ):
        self.url = zotero_url(url)
        self._h = _Http(self.url, user, password, timeout)
        # downloads for open() go to a temp file; point this at real disk when /tmp is RAM-backed
        self.spool_dir = spool_dir or os.environ.get("ZODAV_SPOOL_DIR") or None

    def _path(self, name: str) -> str:
        return self._h.base_path + quote(name)

    def _check(self, what: str, r: _Resp, good) -> None:
        if r.status in good:
            return
        if 300 <= r.status < 400:
            raise AuditError(
                f"{what}: the server redirects to "
                f"{_clean_location(r.headers.get('location', ''))}; use the final address"
            )
        if r.status == 401:
            raise AuditError(f"{what}: the server rejected the username or password (401)")
        if r.status == 403:
            raise AuditError(f"{what}: permission denied (403)")
        if r.status == 404:
            raise AuditError(
                f"{what}: not found (404); does the 'zotero' folder exist at {self.url}?"
            )
        raise AuditError(f"{what}: unexpected answer {r.status} {r.reason}")

    def list(self) -> dict:
        r = self._h.request(
            "PROPFIND",
            self._h.base_path,
            body=_PF1,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            limit=PROPFIND_MAX,
        )
        self._check("listing the store", r, (207,))
        out = {}
        for e in _parse_multistatus(r.body, self._h.base_path):
            if e.is_dir:
                continue
            mtime = 0.0
            if e.lastmod:
                try:
                    mtime = parsedate_to_datetime(e.lastmod).timestamp()
                except (TypeError, ValueError):
                    pass
            out[e.name] = (e.size if e.size is not None else -1, mtime)
        return out

    def read(self, name: str, limit: int | None = None) -> bytes:
        try:
            r = self._h.request("GET", self._path(name), limit=limit)
        except Unreachable:
            raise
        except AuditError as e:
            raise AuditError(f"reading {name}: {e}") from None
        self._check(f"reading {name}", r, (200,))
        return r.body

    def open(self, name: str):
        """Download to a temp file and return it rewound; closing it deletes it."""
        f = tempfile.TemporaryFile(dir=self.spool_dir)
        try:
            r = self._h.request("GET", self._path(name), sink=f)
            self._check(f"reading {name}", r, (200,))
            f.seek(0)
            return f
        except BaseException:
            f.close()
            raise

    def write(self, name: str, data: bytes) -> None:
        ct = "application/zip" if name.endswith(".zip") else "application/octet-stream"
        r = self._h.request("PUT", self._path(name), body=data, headers={"Content-Type": ct})
        self._check(f"writing {name}", r, (200, 201, 204))

    def delete(self, name: str) -> None:
        r = self._h.request("DELETE", self._path(name))
        self._check(f"deleting {name}", r, (200, 204, 404))


# --- conformance ----------------------------------------------------------


class _Stop(Exception):
    pass


def _test_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            zipfile.ZipInfo("test.txt", (2020, 1, 1, 0, 0, 0)), "zodav-audit test file\n" * 20
        )
    return buf.getvalue()


def conformance(url, user, password, read_only=False) -> Result:
    base = zotero_url(url)
    h = _Http(base, user, password, 60)
    P = h.base_path
    res = Result("conformance", base, _now(), [], {"read_only": read_only})
    out = res.findings
    touched: list = []

    def add(sev, code, subject, msg):
        out.append(_finding(sev, code, subject, msg))

    def ok(step, msg):
        out.append(Finding("info", "STEP_OK", step, msg))

    def call(step, method, path, **kw) -> _Resp:
        r = h.request(method, path, **kw)
        if 300 <= r.status < 400:
            add(
                "error",
                "REDIRECT",
                step,
                f"{method} {path} -> {r.status}; the server redirects to "
                f"{_clean_location(r.headers.get('location', ''))}",
            )
            raise _Stop
        return r

    def gate(step, method, path, r, five=True):
        line = f"{method} {path} -> {r.status} {r.reason}"
        if r.status == 401:
            add(
                "error",
                "AUTH_FAILED",
                step,
                f"{line}: the server rejected the username or password",
            )
            raise _Stop
        if r.status == 403:
            add("error", "FORBIDDEN", step, f"{line}: permission denied")
            raise _Stop
        if five and r.status >= 500:
            add("error", "SERVER_ERROR", step, f"{line}: the server reported an internal error")
            raise _Stop

    def line(method, path, r):
        return f"{method} {path} -> {r.status} {r.reason}"

    def listing(step):
        try:
            r = call(
                step,
                "PROPFIND",
                P,
                body=_PF1,
                limit=PROPFIND_MAX,
                headers={"Depth": "1", "Content-Type": "application/xml"},
            )
        except Unreachable:
            raise
        except AuditError as e:
            add("error", "NOT_DAV", step, str(e))
            return
        gate(step, "PROPFIND", P, r)
        if r.status != 207:
            add("error", "NOT_DAV", step, f"{line('PROPFIND (Depth: 1)', P, r)}; expected 207")
            return
        try:
            files = [e for e in _parse_multistatus(r.body, P) if not e.is_dir]
        except AuditError as e:
            add("error", "NOT_DAV", step, str(e))
            return
        missing = [e.name for e in files if not e.lastmod]
        if missing:
            add(
                "warning",
                "NO_LASTMODIFIED",
                step,
                f"The directory listing has no getlastmodified for {len(missing)} of {len(files)} "
                f"file(s), e.g. {', '.join(missing[:3])}",
            )
        else:
            ok(step, f"directory listing carries getlastmodified for {len(files)} file(s)")

    try:
        try:
            # 1. OPTIONS: needs a DAV header
            r = call("options", "OPTIONS", P)
            gate("options", "OPTIONS", P, r)
            if r.status in (200, 204, 404) and "dav" in r.headers:
                ok("options", f"{line('OPTIONS', P, r)} with a DAV header")
            elif r.status in (200, 204, 404):
                add("error", "NOT_DAV", "options", f"{line('OPTIONS', P, r)} has no DAV header")
            else:
                add(
                    "error",
                    "NOT_DAV",
                    "options",
                    f"{line('OPTIONS', P, r)}; expected 200, 204 or 404 with a DAV header",
                )

            # 2. PROPFIND on zotero/, else look at the parent
            hd0 = {"Depth": "0", "Content-Type": "application/xml"}
            r = call("propfind", "PROPFIND", P, body=_PF0, headers=hd0)
            gate("propfind", "PROPFIND", P, r)
            if r.status == 207:
                ok("propfind", f"{line('PROPFIND', P, r)}: the zotero folder exists")
            elif r.status == 404:
                parent = P.rstrip("/").rsplit("/", 1)[0] + "/"
                pr = call("propfind", "PROPFIND", parent, body=_PF0, headers=hd0)
                gate("propfind", "PROPFIND", parent, pr)
                if pr.status == 207:
                    add(
                        "error",
                        "ZOTERO_DIR_NOT_FOUND",
                        "propfind",
                        f"{line('PROPFIND', P, r)}, but the parent folder exists ({pr.status})",
                    )
                else:
                    add(
                        "error",
                        "PARENT_DIR_NOT_FOUND",
                        "propfind",
                        f"{line('PROPFIND', P, r)} and the parent folder answers {pr.status} {pr.reason} too",
                    )
                raise _Stop
            else:
                add(
                    "error", "NOT_DAV", "propfind", f"{line('PROPFIND', P, r)}; expected 207 or 404"
                )

            # 3. a file that cannot exist must be 404
            path = P + "nonexistent.prop"
            r = call("nonexistent-file", "GET", path)
            gate("nonexistent-file", "GET", path, r)
            if r.status == 404:
                ok("nonexistent-file", f"{line('GET', path, r)}")
            elif 200 <= r.status < 300:
                add(
                    "error",
                    "NONEXISTENT_FILE_NOT_MISSING",
                    "nonexistent-file",
                    f"{line('GET', path, r)}; expected 404",
                )
            else:
                add(
                    "error",
                    "SERVER_ERROR",
                    "nonexistent-file",
                    f"{line('GET', path, r)}; expected 404",
                )

            if read_only:
                out.append(
                    Finding(
                        "info",
                        "STEP_SKIPPED",
                        "write-steps",
                        "Read-only: skipped upload, download, delete and the .zip/.prop round trip",
                    )
                )
            else:
                # 4-6. Zotero's own test file
                path = P + ZOTERO_TEST_FILE
                touched.append(ZOTERO_TEST_FILE)
                r = call("upload", "PUT", path, body=b" ")
                gate("upload", "PUT", path, r, five=False)
                uploaded = r.status in (200, 201, 204)
                if uploaded:
                    ok("upload", line("PUT", path, r))
                else:
                    add(
                        "error",
                        "UPLOAD_FAILED",
                        "upload",
                        f"{line('PUT', path, r)}; expected 200, 201 or 204",
                    )
                if uploaded:
                    r = call("download", "GET", path)
                    gate("download", "GET", path, r)
                    if r.status == 200:
                        ok("download", line("GET", path, r))
                    elif r.status == 404:
                        add(
                            "error",
                            "FILE_MISSING_AFTER_UPLOAD",
                            "download",
                            f"{line('GET', path, r)} right after a successful upload",
                        )
                    else:
                        add(
                            "error",
                            "SERVER_ERROR",
                            "download",
                            f"{line('GET', path, r)}; expected 200",
                        )
                    r = call("delete", "DELETE", path)
                    gate("delete", "DELETE", path, r, five=False)
                    if r.status in (200, 204):
                        touched.remove(ZOTERO_TEST_FILE)
                        ok("delete", line("DELETE", path, r))
                    else:
                        add(
                            "error",
                            "DELETE_FAILED",
                            ZOTERO_TEST_FILE,
                            f"{line('DELETE', path, r)}; expected 200 or 204",
                        )

            # sync-path checks Verify does not cover
            r = call("auth-challenge", "PROPFIND", P, body=_PF0, headers=hd0, auth=False)
            challenge = r.headers.get("www-authenticate", "")
            if r.status == 401 and "basic" in challenge.lower():
                ok("auth-challenge", "unauthenticated request gets 401 with a Basic challenge")
            elif r.status == 401:
                add(
                    "error",
                    "NO_AUTH_CHALLENGE",
                    "auth-challenge",
                    f"{line('PROPFIND (no login)', P, r)} but without 'WWW-Authenticate: Basic'",
                )
            else:
                add(
                    "warning",
                    "NO_AUTH_CHALLENGE",
                    "auth-challenge",
                    f"{line('PROPFIND (no login)', P, r)}; expected 401 with a Basic challenge",
                )

            delete_failed = False
            if read_only:
                listing("propfind-depth1")
            else:
                zdata, pdata = (
                    _test_zip(),
                    b'<properties version="1"><mtime>1700000000000</mtime><hash>'
                    + hashlib.md5(b"x").hexdigest().encode()  # noqa: S324 # Zotero stores an MD5 of the file, so MD5 is the format, not a security choice
                    + b"</hash></properties>",
                )
                items = [(TEST_ZIP, zdata, "application/zip"), (TEST_PROP, pdata, None)]
                all_up = True
                for name, data, ct in items:
                    touched.append(name)
                    r = call(
                        "roundtrip",
                        "PUT",
                        P + name,
                        body=data,
                        headers={"Content-Type": ct} if ct else None,
                    )
                    gate("roundtrip", "PUT", P + name, r, five=False)
                    if r.status not in (200, 201, 204):
                        add(
                            "error",
                            "UPLOAD_FAILED",
                            name,
                            f"{line('PUT', P + name, r)}; expected 200, 201 or 204",
                        )
                        all_up = False
                if all_up:
                    listing("propfind-depth1")
                    clean = True
                    for name, data, _ in items:
                        r = call("roundtrip", "GET", P + name)
                        gate("roundtrip", "GET", P + name, r)
                        if r.status == 404:
                            add(
                                "error",
                                "FILE_MISSING_AFTER_UPLOAD",
                                name,
                                f"{line('GET', P + name, r)} right after a successful upload",
                            )
                            clean = False
                        elif r.status != 200:
                            add(
                                "error",
                                "SERVER_ERROR",
                                name,
                                f"{line('GET', P + name, r)}; expected 200",
                            )
                            clean = False
                        elif r.body != data:
                            add(
                                "error",
                                "ROUNDTRIP_MISMATCH",
                                name,
                                f"GET {P + name} returned {len(r.body)} bytes that differ from the {len(data)} uploaded",
                            )
                            clean = False
                    if clean:
                        ok("roundtrip", "uploaded and downloaded a .zip and a .prop byte-for-byte")
                for name, _, _ in items:
                    r = call("roundtrip", "DELETE", P + name)
                    gate("roundtrip", "DELETE", P + name, r, five=False)
                    if r.status in (200, 204):
                        touched.remove(name)
                    else:
                        delete_failed = True
                        add(
                            "error",
                            "DELETE_FAILED",
                            name,
                            f"{line('DELETE', P + name, r)}; expected 200 or 204",
                        )

            if not delete_failed:
                path = P + TEST_ZIP
                r = call("missing-zip-404", "GET", path)
                gate("missing-zip-404", "GET", path, r)
                if r.status == 404:
                    ok("missing-zip-404", f"{line('GET', path, r)} for a .zip that does not exist")
                elif 200 <= r.status < 300:
                    add(
                        "error",
                        "SOFT_404",
                        "missing-zip-404",
                        f"{line('GET', path, r)} for a .zip that does not exist; "
                        f"content type {r.headers.get('content-type', 'unknown')}",
                    )
                else:
                    add(
                        "error",
                        "SERVER_ERROR",
                        "missing-zip-404",
                        f"{line('GET', path, r)}; expected 404",
                    )
            ok("no-redirect", "no redirects on the zotero folder")
        except _Stop:
            pass
        except Unreachable as e:
            add("error", "UNREACHABLE", "", str(e))
    finally:
        for name in list(touched):
            try:
                r = h.request("DELETE", P + name)
                if r.status in (200, 204, 404):
                    continue
            except Exception:  # noqa: S110 # best-effort cleanup; the failure is reported below
                pass
            if not any(f.code == "DELETE_FAILED" and f.subject == name for f in out):
                add(
                    "warning",
                    "DELETE_FAILED",
                    name,
                    f"The test file {name} could not be removed and is still on the server",
                )

    res.stats["steps_ok"] = sum(1 for f in out if f.code == "STEP_OK")
    if any(f.severity in ("error", "warning") for f in out):
        hint = provider_hint(base)
        if hint:
            out.append(Finding("info", "PROVIDER_HINT", urlsplit(base).hostname or "", hint))
    return res


# --- integrity ------------------------------------------------------------


def _target(store) -> str:
    return store.url if isinstance(store, DavStore) else str(store.path)


def _parse_prop(data: bytes):
    """-> (mtime, hash) as found (None when absent) or None when it is not a v1 .prop."""
    if len(data) > XML_MAX or _has_dtd(data):
        return None
    try:
        root = ET.fromstring(data)  # noqa: S314 # the audited server is the only XML source
    except ET.ParseError:
        return None
    if root.tag != "properties" or root.get("version") != "1":
        return None
    return root.findtext("mtime"), root.findtext("hash")


def _zip_state(f):
    """f: seekable binary file. -> ('ok', [ZipInfo of files]) or ('empty'|'corrupt'|'huge', infos).
    Reads the file member by member (testzip), never the whole archive."""
    f.seek(0, 2)
    if f.tell() == 0:
        return "empty", []
    f.seek(0)
    try:
        with zipfile.ZipFile(f) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            # checked before testzip, which decompresses every member
            if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
                return "huge", infos
            if z.testzip() is not None:
                return "corrupt", []
    except Exception:
        return "corrupt", []
    return ("ok", infos) if infos else ("empty", [])


def _md5_member(f, info) -> str:
    h = hashlib.md5()  # noqa: S324 # Zotero stores an MD5 of the file, so MD5 is the format, not a security choice
    with zipfile.ZipFile(f) as z, z.open(info) as m:
        while chunk := m.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _single_file(f):
    """(md5 hex, entry mtime in ms) when the zip holds exactly one intact file, else None."""
    state, infos = _zip_state(f)
    if state != "ok" or len(infos) != 1:
        return None
    try:
        md5 = _md5_member(f, infos[0])
    except Exception:
        return None
    # date_time is local naive time; mktime gives the epoch Zotero would see
    ms = int(time.mktime(tuple(infos[0].date_time) + (0, 0, -1)) * 1000)
    return md5, ms


def _digest(f) -> str:
    h = hashlib.sha256()
    f.seek(0)
    while chunk := f.read(CHUNK):
        h.update(chunk)
    f.seek(0)
    return h.hexdigest()


def _shown(v) -> str:
    return "missing" if v is None else repr(v if len(v) <= 40 else v[:40] + "...")


def integrity(store, sample_percent: float | None = None) -> Result:
    res = Result("integrity", _target(store), _now(), [], {})
    out = res.findings

    def add(sev, code, subject, msg):
        out.append(_finding(sev, code, subject, msg))

    files = store.list()
    keys: dict = {}
    for name in sorted(files):
        m = _NAME_RE.fullmatch(name)
        if m:
            keys.setdefault(m.group(1), set()).add(m.group(2))
        elif name not in IGNORED_NAMES:
            add(
                "info",
                "UNEXPECTED_FILE",
                name,
                f"{name} is not a Zotero storage file; Zotero ignores it",
            )

    all_keys = sorted(keys)
    chosen = all_keys
    if sample_percent is not None and all_keys and sample_percent < 100:
        n = min(len(all_keys), max(1, round(len(all_keys) * sample_percent / 100)))
        chosen = sorted(random.sample(all_keys, n))
    nbytes = 0

    for k in chosen:
        zname, pname = f"{k}.zip", f"{k}.prop"
        has_zip, has_prop = "zip" in keys[k], "prop" in keys[k]
        prop_hash = None
        if has_prop:
            try:
                data = store.read(pname, limit=XML_MAX)
                parsed = _parse_prop(data)
                nbytes += len(data)
            except AuditError as e:
                if isinstance(e, Unreachable):
                    raise
                parsed = None
            if parsed is None:
                add(
                    "error",
                    "PROP_UNPARSEABLE",
                    pname,
                    f'{pname} is not a valid <properties version="1"> file',
                )
            else:
                mtime, h = parsed
                if mtime is None or not _MTIME_RE.fullmatch(mtime):
                    add(
                        "error",
                        "PROP_BAD_MTIME",
                        pname,
                        f"{pname} has an invalid mtime ({_shown(mtime)}); expected 1 to 13 digits",
                    )
                if h is None or not _HASH_RE.fullmatch(h):
                    add(
                        "error",
                        "PROP_BAD_HASH",
                        pname,
                        f"{pname} has an invalid hash ({_shown(h)}); expected 32 hex characters",
                    )
                else:
                    prop_hash = h.lower()
            if not has_zip:
                add("error", "MISSING_ZIP", pname, f"{pname} has no matching {zname}")
        if has_zip:
            if not has_prop:
                add("error", "MISSING_PROP", zname, f"{zname} has no matching {pname}")
            with store.open(zname) as zf:
                size = zf.seek(0, 2)
                nbytes += size
                state, infos = _zip_state(zf)
                if state == "empty":
                    add(
                        "error",
                        "EMPTY_FILE",
                        zname,
                        f"{zname} is empty" if not size else f"{zname} contains no files",
                    )
                elif state == "corrupt":
                    add(
                        "error",
                        "CORRUPT_ZIP",
                        zname,
                        f"{zname} is not a readable zip archive (failed its integrity test)",
                    )
                elif state == "huge":
                    total = sum(i.file_size for i in infos)
                    add(
                        "error",
                        "CORRUPT_ZIP",
                        zname,
                        f"{zname} declares {total / GIB:.1f} GB of content when unpacked, more than the "
                        f"{MAX_UNCOMPRESSED // GIB} GB this check will unpack; it is probably a zip bomb or damaged",
                    )
                elif len(infos) > 1:
                    add(
                        "info",
                        "MULTI_FILE_ZIP",
                        zname,
                        f"{zname} holds {len(infos)} files, so the checksum in {pname} cannot be compared",
                    )
                elif prop_hash:
                    actual = _md5_member(zf, infos[0])
                    if actual != prop_hash:
                        add(
                            "error",
                            "HASH_MISMATCH",
                            zname,
                            f"The file inside {zname} has MD5 {actual}, but {pname} says {prop_hash}",
                        )

    res.stats = {"attachments": len(all_keys), "checked": len(chosen), "bytes": nbytes}
    return res


# --- repair ---------------------------------------------------------------

_PROP_CODES = {"PROP_UNPARSEABLE", "PROP_BAD_MTIME", "PROP_BAD_HASH"}


def _prop_bytes(mtime: str, md5: str) -> bytes:
    return f'<properties version="1"><mtime>{mtime}</mtime><hash>{md5}</hash></properties>'.encode()


def repair(store, apply: bool = False, quarantine: Path | None = None) -> Result:
    inner = integrity(store)
    res = Result(
        "repair",
        inner.target,
        _now(),
        [],
        {"applied": apply, "planned": 0, "done": 0, "not_repairable": 0},
    )
    out = res.findings
    errors = [f for f in inner.findings if f.severity == "error"]
    by_subject: dict = {}
    for f in errors:
        by_subject.setdefault(f.subject, []).append(f)
    qdir = {"path": quarantine}

    def unfixable(f, why):
        res.stats["not_repairable"] += 1
        out.append(
            Finding(
                "warning",
                "NOT_REPAIRABLE",
                f.subject,
                f"{f.message}. Automatic repair is not possible: {why}",
                _TEXT[f.code][0],
                _RESET,
            )
        )

    def failed(subject, msg):
        out.append(_finding("error", "REPAIR_FAILED", subject, msg))

    def copy_out(name, data):
        """Verified byte-exact copy of `data` in the quarantine folder; returns its path."""
        if qdir["path"] is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            qdir["path"] = Path(f"zodav-quarantine-{stamp}")
        q = Path(qdir["path"])
        q.mkdir(parents=True, exist_ok=True)
        dest, n = q / name, 0
        while dest.exists():
            n += 1
            dest = q / f"{name}.{n}"
        with open(dest, "xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if dest.read_bytes() != data:
            raise OSError(f"the copy at {dest} does not match the original")
        res.stats["quarantine"] = str(q)
        return dest

    def unchanged(name, expected):
        if store.read(name, limit=XML_MAX) != expected:
            raise AuditError(f"{name} changed while the repair was running")

    def zip_unchanged(zname, digest):
        with store.open(zname) as f:
            if _digest(f) != digest:
                raise AuditError(f"{zname} changed while the repair was running")

    def write_prop(pname, zname, data, old, digest, approx):
        res.stats["planned"] += 1
        if not apply:
            out.append(
                Finding(
                    "info",
                    "WOULD_WRITE_PROP",
                    pname,
                    f"Would write {pname}"
                    + (", keeping a copy of the current one" if old is not None else ""),
                )
            )
            return
        try:
            if old is not None:
                dest = copy_out(pname, old)
                out.append(
                    Finding(
                        "info",
                        "QUARANTINED",
                        pname,
                        f"Copied the old {pname} to {dest} before replacing it",
                    )
                )
                unchanged(pname, old)
            elif pname in store.list():
                raise AuditError(f"{pname} appeared while the repair was running")
            zip_unchanged(zname, digest)
            store.write(pname, data)
            if store.read(pname, limit=XML_MAX) != data:
                raise AuditError("the file read back differs from what was written")
        except (OSError, AuditError) as e:
            failed(pname, f"Could not write {pname}: {e}")
            return
        res.stats["done"] += 1
        note = (
            " The modification time is approximate (taken from the file's date inside the .zip); "
            "Zotero compares the checksum and then updates it."
            if approx
            else ""
        )
        out.append(Finding("info", "WROTE_PROP", pname, f"Wrote {pname}.{note}"))

    def quarantine_file(name, zname):
        res.stats["planned"] += 1
        if not apply:
            out.append(
                Finding(
                    "info",
                    "WOULD_QUARANTINE",
                    name,
                    f"Would copy {name} to the quarantine folder, then delete it from the store",
                )
            )
            return
        try:
            data = store.read(name, limit=XML_MAX)
            dest = copy_out(name, data)
            unchanged(name, data)
            if zname in store.list():
                raise AuditError(f"{zname} appeared while the repair was running")
            store.delete(name)
        except (OSError, AuditError) as e:
            failed(
                name, f"{name} was left in place because it could not be quarantined safely: {e}"
            )
            return
        res.stats["done"] += 1
        out.append(
            Finding(
                "info",
                "QUARANTINED",
                name,
                f"Copied {name} to {dest} and removed it from the store",
            )
        )

    keys = sorted({m.group(1) for s in by_subject if (m := _NAME_RE.fullmatch(s))})
    for k in keys:
        zname, pname = f"{k}.zip", f"{k}.prop"
        zf, pf = by_subject.get(zname, []), by_subject.get(pname, [])
        zcodes, pcodes = {f.code for f in zf}, {f.code for f in pf}
        if "MISSING_ZIP" in pcodes:
            quarantine_file(pname, zname)
            continue
        zip_broken = bool(zcodes & {"CORRUPT_ZIP", "EMPTY_FILE"})
        wants = [f for f in zf + pf if f.code == "MISSING_PROP" or f.code in _PROP_CODES]
        if wants and "HASH_MISMATCH" in zcodes:
            # rewriting the hash would make the evidence of the mismatch disappear
            for f in wants:
                unfixable(
                    f,
                    "the checksum in the .prop disagrees with the file in the .zip, "
                    "and rewriting it would hide that",
                )
        elif wants and not zip_broken:
            try:
                with store.open(zname) as zfile:
                    digest = _digest(zfile)
                    info = _single_file(zfile)
                old = None if "MISSING_PROP" in zcodes else store.read(pname, limit=XML_MAX)
            except (OSError, AuditError) as e:
                failed(zname, f"Could not read {zname} to repair it: {e}")
                continue
            if info is None:
                for f in wants:
                    unfixable(
                        f, "the .zip holds more than one file, so its checksum cannot be derived"
                    )
            else:
                md5, entry_ms = info
                mtime, approx = str(entry_ms), True
                if old is not None and not pcodes & {"PROP_BAD_MTIME", "PROP_UNPARSEABLE"}:
                    parsed = _parse_prop(old)
                    if parsed and parsed[0] and _MTIME_RE.fullmatch(parsed[0]):
                        mtime, approx = parsed[0], False
                write_prop(pname, zname, _prop_bytes(mtime, md5), old, digest, approx)
        for f in zf:
            if f.code in ("CORRUPT_ZIP", "EMPTY_FILE", "HASH_MISMATCH"):
                unfixable(f, "only the computer that has the original file can supply it again")
    return res


# --- rendering ------------------------------------------------------------


def _safe(text) -> str:
    """Server-supplied names and messages can carry terminal escapes; show them as \\xNN."""
    return "".join(
        c if c.isprintable() else (f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}")
        for c in str(text)
    )


def render_text(result: Result) -> str:
    lines = [f"zodav-audit {result.command} {_safe(result.target)}", ""]
    for sev in SEVERITIES:
        group = [f for f in result.findings if f.severity == sev]
        if not group:
            continue
        lines.append(f"{sev.upper()}S ({len(group)})" if sev != "info" else f"INFO ({len(group)})")
        for f in group:
            subject = f" {_safe(f.subject)}:" if f.subject else ""
            lines.append(f"  [{_safe(f.code)}]{subject} {_safe(f.message)}")
            if f.effect:
                lines.append(f"      What Zotero will do: {_safe(f.effect)}")
            if f.fix:
                lines.append(f"      What to do: {_safe(f.fix)}")
        lines.append("")
    c = result.counts()
    status = "OK" if result.ok else "PROBLEMS FOUND"
    lines.append(f"{c['error']} error(s), {c['warning']} warning(s), {c['info']} info - {status}")
    return "\n".join(lines)


def render_json(result: Result) -> str:
    return json.dumps(result.to_dict(), indent=2, sort_keys=False)


_HTML_CSS = """
:root{color-scheme:light dark;--bg:#f4f5f3;--panel:#fff;--ink:#1c2421;--muted:#55605b;--rule:#d3d9d5;
--err:#a11d1d;--err-bg:#fbeceb;--warn:#7a4f00;--warn-bg:#fbf1dc;--ok:#1a6a43;--ok-bg:#e6f3ec;--info:#34556f;--info-bg:#e8eff5}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#131715;--panel:#1a1f1c;--ink:#e6ebe7;--muted:#a3ada7;--rule:#323a35;
--err:#ff9d93;--err-bg:#3a1c1b;--warn:#ecb85f;--warn-bg:#352a12;--ok:#82d4a4;--ok-bg:#16301f;--info:#94b8d9;--info-bg:#1b2a38}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#131715;--panel:#1a1f1c;--ink:#e6ebe7;--muted:#a3ada7;--rule:#323a35;
--err:#ff9d93;--err-bg:#3a1c1b;--warn:#ecb85f;--warn-bg:#352a12;--ok:#82d4a4;--ok-bg:#16301f;--info:#94b8d9;--info-bg:#1b2a38}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;overflow-wrap:anywhere}
main{max-width:46rem;margin:0 auto;padding:1.5rem 1rem 2rem}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{border-bottom:2px solid var(--ink);padding-bottom:1rem;margin-bottom:1.25rem}
.mark{font:700 1.1rem ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;letter-spacing:.04em;margin:0 0 .75rem}
.mark span{color:var(--muted);font-weight:400}
.status{margin:0 0 .25rem;font-size:1.9rem;line-height:1.2;font-weight:700}
.status.ok{color:var(--ok)}.status.warn{color:var(--warn)}.status.bad{color:var(--err)}
.lede{margin:0 0 1rem;color:var(--muted)}
.meta{display:grid;grid-template-columns:max-content 1fr;gap:.15rem 1rem;margin:0;font-size:.9rem}
.meta dt{color:var(--muted)}.meta dd{margin:0}
h2{font-size:1.05rem;margin:2rem 0 .6rem;padding-bottom:.3rem;border-bottom:1px solid var(--rule)}
.counts{display:grid;grid-template-columns:repeat(auto-fit,minmax(7rem,1fr));gap:.5rem;list-style:none;margin:0;padding:0}
.counts li{background:var(--panel);border:1px solid var(--rule);padding:.5rem .75rem}
.counts b{display:block;font-size:1.6rem;line-height:1.1}
.counts span{font-size:.85rem;color:var(--muted)}
.counts .n-error b{color:var(--err)}.counts .n-warning b{color:var(--warn)}.counts .n-pass b{color:var(--ok)}.counts .n-info b{color:var(--info)}
.finding{list-style:none;margin:0 0 .75rem;padding:.75rem .9rem;background:var(--panel);border:1px solid var(--rule)}
.findings{margin:0;padding:0;list-style:none}
.finding h3{margin:0 0 .35rem;font-size:1rem;display:flex;flex-wrap:wrap;gap:.25rem .6rem;align-items:baseline}
.tag{font-size:.72rem;font-weight:700;letter-spacing:.05em;text-transform:uppercase;padding:.05rem .4rem}
.tag.error{background:var(--err-bg);color:var(--err)}.tag.warning{background:var(--warn-bg);color:var(--warn)}.tag.info{background:var(--info-bg);color:var(--info)}
.code{font-size:.75rem;color:var(--muted)}
.finding p{margin:.2rem 0}
.finding .label{font-weight:700;font-size:.85rem;display:block;color:var(--muted)}
.passed{list-style:none;margin:0;padding:0;columns:2 16rem;column-gap:1.5rem;font-size:.92rem}
.passed li{break-inside:avoid;padding:.12rem 0 .12rem 1.3rem;position:relative}
.passed li::before{content:"";position:absolute;left:.15rem;top:.55rem;width:.35rem;height:.7rem;border:solid var(--ok);border-width:0 2px 2px 0;transform:translateY(-.25rem) rotate(40deg)}
.stats{display:grid;grid-template-columns:max-content 1fr;gap:.2rem 1.25rem;margin:0;font-size:.92rem}
.stats dt{color:var(--muted)}.stats dd{margin:0}
footer{margin-top:2.5rem;padding-top:.75rem;border-top:1px solid var(--rule);font-size:.85rem;color:var(--muted)}
a{color:var(--info)}
@media print{:root{--bg:#fff;--panel:#fff}body{font-size:11pt}.finding{break-inside:avoid}main{max-width:none;padding:0}}
"""


def render_html(result: Result) -> str:
    e = html.escape
    c = result.counts()
    passed = [f for f in result.findings if f.code == "STEP_OK"]
    notes = [f for f in result.findings if f.severity == "info" and f.code != "STEP_OK"]
    if not result.ok:
        cls, head = "bad", "Problems found"
        lede = "At least one problem will stop Zotero syncing files correctly. Details below."
    elif c["warning"]:
        cls, head = "warn", "Passed, with warnings"
        lede = "Nothing blocks syncing, but some things deserve attention."
    else:
        cls, head = "ok", "Passed"
        lede = "No problems found."

    def finding(f):
        subj = f"<span>{e(f.subject)}</span>" if f.subject else ""
        out = (
            f'<li class="finding"><h3><span class="tag {e(f.severity)}">{e(f.severity)}</span>{subj}'
            f'<code class="code">{e(f.code)}</code></h3><p>{e(f.message)}</p>'
        )
        if f.effect:
            out += f'<p><span class="label">What Zotero does</span>{e(f.effect)}</p>'
        if f.fix:
            out += f'<p><span class="label">What to do</span>{e(f.fix)}</p>'
        return out + "</li>"

    sections = []
    for sev, title in (("error", "Errors"), ("warning", "Warnings"), ("info", "Notes")):
        group = [f for f in (notes if sev == "info" else result.findings) if f.severity == sev]
        if group:
            sections.append(
                f'<section><h2>{title} ({len(group)})</h2><ul class="findings">'
                + "".join(finding(f) for f in group)
                + "</ul></section>"
            )
    if passed:
        sections.append(
            f'<section><h2>Passed checks ({len(passed)})</h2><ul class="passed">'
            + "".join(f"<li>{e(f.message)}</li>" for f in passed)
            + "</ul></section>"
        )
    if result.stats:
        sections.append(
            '<section><h2>Numbers</h2><dl class="stats">'
            + "".join(f"<dt>{e(str(k))}</dt><dd>{e(str(v))}</dd>" for k, v in result.stats.items())
            + "</dl></section>"
        )
    if not result.findings:
        sections.append("<section><p>Nothing to report.</p></section>")

    counts = "".join(
        f'<li class="n-{k}"><b>{n}</b><span>{label}</span></li>'
        for k, n, label in (
            ("error", c["error"], "Errors"),
            ("warning", c["warning"], "Warnings"),
            ("info", len(notes), "Notes"),
            ("pass", len(passed), "Checks passed"),
        )
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>ZoDAV audit - {e(result.command)} - {e(head)}</title><style>{_HTML_CSS}</style></head><body><main>"
        '<header><p class="mark">ZoDAV <span>audit</span></p>'
        f'<h1 class="status {cls}">{e(head)}</h1><p class="lede">{e(lede)}</p>'
        f'<dl class="meta"><dt>Check</dt><dd>{e(result.command)}</dd><dt>Server</dt><dd>{e(result.target)}</dd>'
        f"<dt>Started</dt><dd>{e(result.started)}</dd></dl></header>"
        f'<section aria-label="Summary"><ul class="counts">{counts}</ul></section>'
        + "".join(sections)
        + f'<footer>ZoDAV {e(VERSION)} &middot; by <a href="https://rajabpour.com">Ali Rajabpour Sanati</a></footer>'
        "</main></body></html>"
    )


# --- watch ----------------------------------------------------------------

BACKUP_STALE_SECONDS = 26 * 3600
GIB = 1024**3


def _env_number(env, name: str, default: float, minimum: float = 0) -> float:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        value = None
    if value is None or value != value or value < minimum:
        raise AuditError(f"{name} must be a number of at least {minimum:g}, got '{raw}'")
    return value


def _thresholds(env) -> dict:
    return {
        "delete_pct": _env_number(env, "ZODAV_DELETE_ALERT_PERCENT", 10),
        "idle_days": _env_number(env, "ZODAV_IDLE_DAYS", 30),
        "min_free_gb": _env_number(env, "ZODAV_MIN_FREE_GB", 2),
        "interval_h": _env_number(env, "ZODAV_AUDIT_INTERVAL_HOURS", 24, 0.001),
    }


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_state(reports: Path):
    try:
        data = json.loads((reports / "state.json").read_text(encoding="utf-8"))
        n = int(data["attachments"])
        # baseline = the count mass-deletion is judged against; older state files have none
        return n, bool(data["last_ok"]), int(data.get("baseline", n))
    except (OSError, ValueError, KeyError, TypeError):
        return None, None, None


def _watch_findings(
    files: dict,
    attachments: int,
    prev: int | None,
    data_dir: Path,
    backup_state,
    th: dict,
    now: float,
) -> list:
    out = []
    if prev is not None:
        drop = prev - attachments
        if drop >= 5 and drop * 100 / prev > th["delete_pct"]:
            out.append(
                Finding(
                    "error",
                    "MASS_DELETION",
                    "",
                    f"The library holds {attachments} attachments, down from {prev} at the previous check.",
                    "Many files disappeared at once. If that was not you, Zotero will treat the missing "
                    "attachments as deleted and the other devices will lose them too.",
                    "Do not sync from any device yet. Restore the files from your latest backup, "
                    "then check which device removed them. If you removed them on purpose, run "
                    "'./zodav accept-deletions' to accept the new count; until then this alert repeats.",
                )
            )
    props = [m for n, (_, m) in files.items() if n.endswith(".prop")]
    if props:
        age_days = (now - max(props)) / 86400
        if age_days > th["idle_days"]:
            out.append(
                Finding(
                    "warning",
                    "STORE_IDLE",
                    "",
                    f"No attachment has been uploaded for {int(age_days)} days.",
                    "Nothing is wrong with the files, but no device has synced new attachments, "
                    "so a sync problem could be going unnoticed.",
                    "Open Zotero on a device, add or change an attachment, and sync. "
                    "If sync reports an error, run the connection check.",
                )
            )
    usage = shutil.disk_usage(data_dir)
    free_gb = usage.free / GIB
    if free_gb < th["min_free_gb"] or (usage.total and usage.free / usage.total < 0.05):
        out.append(
            Finding(
                "error",
                "LOW_DISK",
                "",
                f"Only {free_gb:.1f} GB are free on the storage volume.",
                "When the disk is full, uploads fail halfway and Zotero reports sync errors "
                "or leaves attachments incomplete.",
                "Free up space on the server or move the storage to a larger disk.",
            )
        )
    marker = Path(backup_state) / "last-success" if backup_state else None
    if marker is None or not marker.is_file():
        out.append(
            Finding(
                "warning",
                "BACKUP_NEVER",
                "",
                "No successful backup has been recorded.",
                "If the server disk fails or files are deleted, nothing can bring your attachments back.",
                "Turn on backups in the settings and make sure the first one finishes.",
            )
        )
    elif now - marker.stat().st_mtime > BACKUP_STALE_SECONDS:
        hours = int((now - marker.stat().st_mtime) / 3600)
        out.append(
            Finding(
                "error",
                "BACKUP_STALE",
                "",
                f"The last successful backup was {hours} hours ago.",
                "Recent attachments are not protected; a disk failure now would lose them.",
                "Look at the backup log for the reason it is failing and fix it.",
            )
        )
    return out


def _alert_body(result: Result) -> str:
    c = result.counts()
    lines = [f"ZoDAV audit: {c['error']} error(s), {c['warning']} warning(s)"]
    shown = [f for f in result.findings if f.severity in ("error", "warning")]
    for f in shown[:10]:
        lines.append(f"[{f.code}] {f.subject + ': ' if f.subject else ''}{f.message}")
    if len(shown) > 10:
        lines.append(f"... and {len(shown) - 10} more (see the report)")
    return "\n".join(lines)


def send_alert(url: str, title: str, body: str, timeout: float = 15) -> None:
    host = (urlsplit(url).hostname or "").lower()
    if host == "hooks.slack.com":
        data, ctype = json.dumps({"text": f"{title}\n{body}"}).encode(), "application/json"
        headers = {}
    elif host in ("discord.com", "discordapp.com") or host.endswith(
        (".discord.com", ".discordapp.com")
    ):
        data, ctype = json.dumps({"content": f"{title}\n{body}"}).encode(), "application/json"
        headers = {}
    else:
        data, ctype = body.encode(), "text/plain; charset=utf-8"
        headers = {"Title": title}
    req = urllib.request.Request(  # noqa: S310 # alert URL is operator-set config; only http(s) is expected
        url, data=data, method="POST", headers={"Content-Type": ctype, **headers}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 # alert URL is operator-set config
            r.read()
    except (urllib.error.URLError, OSError, ValueError) as e:
        # the URL can carry a token, so report the reason only
        reason = getattr(e, "reason", e)
        print(f"zodav-audit: could not send alert: {reason}", file=sys.stderr)


TELEGRAM_LIMIT = 4096


def _telegram_send(token: str, chat_id: str, text: str, timeout: float = 15) -> None:
    base = os.environ.get("ZODAV_TELEGRAM_API", "https://api.telegram.org").rstrip("/")
    data = json.dumps(
        {"chat_id": chat_id, "text": text[:TELEGRAM_LIMIT], "disable_web_page_preview": True}
    ).encode()
    req = urllib.request.Request(  # noqa: S310 # operator-set config
        f"{base}/bot{token}/sendMessage",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 # operator-set config
        r.read()


def _smtp_send(cfg: dict, title: str, body: str, timeout: float = 30) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"ZoDAV: {title}"
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(cfg["to"])
    msg.set_content(body)
    sec = cfg["security"]
    ctx = ssl.create_default_context()
    if sec == "ssl":
        client = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=timeout, context=ctx)
    else:
        client = smtplib.SMTP(cfg["host"], cfg["port"], timeout=timeout)
    with client:
        client.ehlo()
        if sec == "starttls":
            client.starttls(context=ctx)
            client.ehlo()
        if cfg["user"]:
            client.login(cfg["user"], cfg["password"])
        client.send_message(msg)


def _alert_config(env) -> dict:
    """Validate the alert settings. Raises AuditError so a bad setup stops the run (exit 2)."""

    def get(name):
        return (env.get(name) or "").strip()

    cfg: dict = {"url": get("ZODAV_ALERT_URL"), "telegram": None, "smtp": None}
    token, chat = get("ZODAV_TELEGRAM_BOT_TOKEN"), get("ZODAV_TELEGRAM_CHAT_ID")
    if bool(token) != bool(chat):
        raise AuditError(
            "Telegram alerts need both ZODAV_TELEGRAM_BOT_TOKEN and "
            "ZODAV_TELEGRAM_CHAT_ID; only one is set"
        )
    if token:
        cfg["telegram"] = (token, chat)
    host = get("ZODAV_SMTP_HOST")
    if host:
        sec = (get("ZODAV_SMTP_SECURITY") or "starttls").lower()
        if sec not in ("starttls", "ssl", "none"):
            raise AuditError(f"ZODAV_SMTP_SECURITY must be starttls, ssl or none, got '{sec}'")
        port = _env_number(env, "ZODAV_SMTP_PORT", 587, 1)
        user, pw = get("ZODAV_SMTP_USERNAME"), env.get("ZODAV_SMTP_PASSWORD") or ""
        if bool(user) != bool(pw):
            raise AuditError("ZODAV_SMTP_USERNAME and ZODAV_SMTP_PASSWORD must be set together")
        sender = get("ZODAV_SMTP_FROM")
        to = [a.strip() for a in get("ZODAV_SMTP_TO").split(",") if a.strip()]
        if not sender or not to:
            raise AuditError(
                "email alerts need ZODAV_SMTP_FROM and ZODAV_SMTP_TO (ZODAV_SMTP_HOST is set)"
            )
        cfg["smtp"] = {
            "host": host,
            "port": int(port),
            "security": sec,
            "user": user,
            "password": pw,
            "from": sender,
            "to": to,
        }
    return cfg


def notify(env, title: str, body: str) -> None:
    """Send one alert to every configured channel; a failing channel never stops the others."""
    cfg = _alert_config(env)
    # Reasons only: URLs and tokens must not reach the log.
    if cfg["url"]:
        send_alert(cfg["url"], title, body)
    if cfg["telegram"]:
        try:
            _telegram_send(*cfg["telegram"], f"{title}\n{body}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(
                f"zodav-audit: could not send Telegram alert: {getattr(e, 'reason', e)}",
                file=sys.stderr,
            )
    if cfg["smtp"]:
        if cfg["smtp"]["security"] == "none":
            print(
                "zodav-audit: warning: SMTP security is 'none', the email is sent unencrypted",
                file=sys.stderr,
            )
        try:
            _smtp_send(cfg["smtp"], title, body)
        except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
            print(
                f"zodav-audit: could not send email alert: {e.__class__.__name__}: {e}",
                file=sys.stderr,
            )


def watch_once(
    data_dir: Path, reports: Path, backup_state: Path | None, env, now: float | None = None
) -> Result:
    now = time.time() if now is None else now
    th = _thresholds(env)
    data_dir, reports = Path(data_dir), Path(reports)
    store = DirStore(data_dir)
    res = integrity(store)
    res.command = "watch"
    _, prev_ok, prev = _read_state(reports)
    attachments = res.stats.get("attachments", 0)
    res.findings += _watch_findings(
        store.list(), attachments, prev, data_dir, backup_state, th, now
    )
    reports.mkdir(parents=True, exist_ok=True)
    clean = not any(f.severity in ("error", "warning") for f in res.findings)
    _write_atomic(reports / "latest.json", render_json(res))
    _write_atomic(reports / "latest.html", render_html(res))
    # After a mass deletion keep the old baseline, so the reduced count does not become the new
    # normal; only a restore or `watch --accept-current-count` moves it down.
    baseline = prev if any(f.code == "MASS_DELETION" for f in res.findings) else attachments
    _write_atomic(
        reports / "state.json",
        json.dumps({"attachments": attachments, "last_ok": clean, "baseline": baseline}),
    )
    c = res.counts()
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(
        f"{stamp} zodav-audit watch: {attachments} attachments, {c['error']} error(s), "
        f"{c['warning']} warning(s) - {'OK' if clean else 'PROBLEMS FOUND'}",
        flush=True,
    )
    if not clean:
        notify(env, "ZoDAV needs attention", _alert_body(res))
    elif prev_ok is False:
        notify(env, "ZoDAV recovered", "ZoDAV audit: all checks pass again.")
    return res


def _count_attachments(store) -> int:
    return len({m.group(1) for n in store.list() if (m := _NAME_RE.fullmatch(n))})


def accept_current_count(data_dir: Path, reports: Path) -> int:
    """Make today's attachment count the baseline, e.g. after deleting files on purpose."""
    n = _count_attachments(DirStore(data_dir))
    _, ok, _ = _read_state(reports)
    reports.mkdir(parents=True, exist_ok=True)
    _write_atomic(
        reports / "state.json",
        json.dumps({"attachments": n, "last_ok": True if ok is None else ok, "baseline": n}),
    )
    return n


def _report_failure(reports: Path, data_dir: Path, exc: Exception, env) -> None:
    """A run that throws must not leave yesterday's green report in place. state.json stays as it was."""
    print(
        f"{_now()} zodav-audit watch: run failed: {exc.__class__.__name__}: {exc}",
        file=sys.stderr,
        flush=True,
    )
    res = Result(
        "watch",
        str(data_dir),
        _now(),
        [
            _finding(
                "error",
                "WATCH_RUN_FAILED",
                "",
                f"The check could not run: {exc.__class__.__name__}: {exc}",
            )
        ],
    )
    try:
        reports.mkdir(parents=True, exist_ok=True)
        _write_atomic(reports / "latest.json", render_json(res))
        _write_atomic(reports / "latest.html", render_html(res))
    except OSError as e:
        print(f"zodav-audit: could not write the failure report: {e}", file=sys.stderr, flush=True)
    try:
        notify(env, "ZoDAV audit could not run", _alert_body(res))
    except AuditError:
        pass  # a bad alert setting is already reported at start-up


def _watch(args) -> int:
    env = os.environ
    th = _thresholds(env)
    _alert_config(env)
    if args.every is not None and args.every <= 0:
        raise AuditError("--every must be a number of hours above 0")
    hours = args.every if args.every is not None else th["interval_h"]
    data_dir = Path(args.target)
    if not data_dir.is_dir():
        raise AuditError(f"'{args.target}' is not an existing directory")
    reports = Path(args.reports or "reports")
    if args.accept_current_count:
        n = accept_current_count(data_dir, reports)
        print(f"{_now()} zodav-audit watch: baseline set to {n} attachments")
        return 0
    if args.once:
        try:
            res = watch_once(data_dir, reports, args.backup_state, env)
        except Exception as e:
            _report_failure(reports, data_dir, e, env)
            raise
        return 0 if res.ok else 1
    while True:
        try:
            watch_once(data_dir, reports, args.backup_state, env)
        except Exception as e:  # keep the schedule alive whatever one run hit
            _report_failure(reports, data_dir, e, env)
        time.sleep(hours * 3600)


# --- CLI ------------------------------------------------------------------


def _password() -> str:
    pw = os.environ.get("ZODAV_AUDIT_PASSWORD")
    if pw:
        return pw
    if sys.stdin.isatty():
        return getpass.getpass("Password: ")
    raise AuditError(
        "no password: set the ZODAV_AUDIT_PASSWORD environment variable "
        "(there is no terminal to ask on)"
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="zodav-audit", description="Check a Zotero WebDAV server or store."
    )
    p.add_argument("--version", action="version", version=f"zodav-audit {VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, html_out=True):
        sp.add_argument("--user", default="zotero", help="WebDAV user name (default: zotero)")
        sp.add_argument("--json", action="store_true", help="print JSON instead of text")
        if html_out:
            sp.add_argument("--html", metavar="FILE", help="also write an HTML report")

    c = sub.add_parser("conformance", help="check a server the way Zotero's Verify Server does")
    c.add_argument("target", metavar="URL")
    c.add_argument("--read-only", action="store_true", help="send no PUT or DELETE")
    common(c)
    i = sub.add_parser("integrity", help="check every .zip/.prop pair; never writes")
    i.add_argument("target", metavar="URL|DIR")
    i.add_argument(
        "--sample", type=float, metavar="PCT", help="check a random PCT percent of attachments"
    )
    common(i)
    r = sub.add_parser("repair", help="fix what can be fixed safely (dry run unless --apply)")
    r.add_argument("target", metavar="URL|DIR")
    r.add_argument("--apply", action="store_true")
    r.add_argument("--quarantine", metavar="DIR", type=Path)
    common(r, html_out=False)
    w = sub.add_parser("watch", help="run integrity on a schedule (container mode)")
    w.add_argument("target", metavar="DIR")
    w.add_argument("--every", type=float, metavar="HOURS")
    w.add_argument("--reports", type=Path)
    w.add_argument("--backup-state", type=Path)
    w.add_argument("--once", action="store_true")
    w.add_argument(
        "--accept-current-count",
        action="store_true",
        help="accept today's attachment count as the new normal (after deleting files on purpose) and exit",
    )
    return p


def _run(args) -> int:
    if args.command == "watch":
        return _watch(args)
    target = args.target
    is_url = "://" in target
    if args.command == "conformance":
        if os.path.isdir(target):
            raise AuditError("conformance needs a server URL, not a directory")
        result = conformance(target, args.user, _password(), read_only=args.read_only)
    else:
        if args.command == "integrity" and args.sample is not None and not 0 < args.sample <= 100:
            raise AuditError("--sample must be a percentage above 0 and up to 100")
        if is_url:
            store = DavStore(target, args.user, _password())
        elif os.path.isdir(target):
            store = DirStore(target)
        else:
            raise AuditError(
                f"'{target}' is not an existing directory (a server address needs http:// or https://)"
            )
        if args.command == "integrity":
            result = integrity(store, args.sample)
        else:
            result = repair(store, apply=args.apply, quarantine=args.quarantine)
    print(render_json(result) if args.json else render_text(result))
    if getattr(args, "html", None):
        Path(args.html).write_text(render_html(result), encoding="utf-8")
    if any(f.code == "UNREACHABLE" for f in result.findings):
        return 2
    return 0 if result.ok else 1


def main(argv: list | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _run(args)
    except AuditError as e:
        print(f"zodav-audit: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"zodav-audit: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # an internal error is not "problems found" (1) and never a traceback
        print(f"zodav-audit: unexpected error: {e.__class__.__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
