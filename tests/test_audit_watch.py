import json
import os
import shutil
import threading
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fakedav import build_clean_store, build_defect_store
from zodav_audit import main, send_alert, watch_once

NOW = 2_000_000_000.0
Usage = namedtuple("Usage", "total used free")
GIB = 1024**3


@pytest.fixture
def hook():
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            got.append(
                {"path": self.path, "headers": dict(self.headers), "body": self.rfile.read(n)}
            )
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/hook", got
    srv.shutdown()
    srv.server_close()


@pytest.fixture(autouse=True)
def plenty_of_disk(monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(100 * GIB, 10 * GIB, 90 * GIB))


def fresh_backup(tmp_path, now=NOW):
    d = tmp_path / "bk"
    d.mkdir(exist_ok=True)
    f = d / "last-success"
    f.write_text("ok")
    os.utime(f, (now - 3600, now - 3600))
    return d


def codes(res):
    return {f.code: f.severity for f in res.findings}


def store(tmp_path, count=5, mtime=NOW - 86400):
    d = tmp_path / "zotero"
    build_clean_store(d, count=count)
    for p in d.iterdir():
        os.utime(p, (mtime, mtime))
    return d


def run(tmp_path, d, env=None, backup=True, now=NOW):
    bk = fresh_backup(tmp_path, now) if backup else None
    return watch_once(d, tmp_path / "rep", bk, env or {}, now=now)


def set_state(tmp_path, n, ok=True):
    (tmp_path / "rep").mkdir(exist_ok=True)
    (tmp_path / "rep" / "state.json").write_text(json.dumps({"attachments": n, "last_ok": ok}))


def test_clean_run_writes_reports_and_state(tmp_path, capsys):
    res = run(tmp_path, store(tmp_path))
    assert res.findings == []
    rep = tmp_path / "rep"
    assert json.loads((rep / "latest.json").read_text())["ok"] is True
    assert (rep / "latest.html").read_text().startswith("<!doctype html>")
    assert json.loads((rep / "state.json").read_text()) == {
        "attachments": 5,
        "last_ok": True,
        "baseline": 5,
    }
    assert not [p for p in rep.iterdir() if p.name.startswith(".tmp")]
    assert "2033-05-18T03:33:20Z" in capsys.readouterr().out


def test_mass_deletion_error(tmp_path):
    set_state(tmp_path, 100)
    res = run(tmp_path, store(tmp_path, count=5))
    assert codes(res)["MASS_DELETION"] == "error"


def test_small_drop_is_not_mass_deletion(tmp_path):
    set_state(tmp_path, 8)
    assert "MASS_DELETION" not in codes(run(tmp_path, store(tmp_path, count=5)))  # 3 < 5
    set_state(tmp_path, 100)
    d = tmp_path / "big"
    build_clean_store(d, count=5)
    assert "MASS_DELETION" in codes(
        watch_once(d, tmp_path / "rep", fresh_backup(tmp_path), {}, now=NOW)
    )


def test_drop_below_percent_threshold(tmp_path):
    set_state(tmp_path, 100)
    # 100 -> 97 is under both limits; also check the percent env raises/lowers the bar
    d = tmp_path / "z"
    build_clean_store(d, count=5)
    set_state(tmp_path, 10)
    env = {"ZODAV_DELETE_ALERT_PERCENT": "60"}
    assert "MASS_DELETION" not in codes(run(tmp_path, d, env))  # 50 % drop, limit 60


def test_no_previous_state_no_comparison(tmp_path):
    assert "MASS_DELETION" not in codes(run(tmp_path, store(tmp_path)))


def test_idle_store_warns(tmp_path):
    d = store(tmp_path, mtime=NOW - 40 * 86400)
    assert codes(run(tmp_path, d))["STORE_IDLE"] == "warning"
    d2 = store(tmp_path / "x", mtime=NOW - 40 * 86400)
    assert "STORE_IDLE" not in codes(run(tmp_path / "x", d2, {"ZODAV_IDLE_DAYS": "60"}))


def test_empty_store_not_idle(tmp_path):
    d = tmp_path / "zotero"
    d.mkdir()
    assert "STORE_IDLE" not in codes(run(tmp_path, d))


def test_low_disk_absolute_and_percent(tmp_path, monkeypatch):
    d = store(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(100 * GIB, 99 * GIB, 1 * GIB))
    assert codes(run(tmp_path, d))["LOW_DISK"] == "error"
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(1000 * GIB, 940 * GIB, 60 * GIB))
    assert "LOW_DISK" not in codes(run(tmp_path, d))
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(1000 * GIB, 970 * GIB, 30 * GIB))
    assert codes(run(tmp_path, d))["LOW_DISK"] == "error"  # 3 % free
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(100 * GIB, 90 * GIB, 10 * GIB))
    assert codes(run(tmp_path, d, {"ZODAV_MIN_FREE_GB": "20"}))["LOW_DISK"] == "error"


def test_backup_stale_never_and_fresh(tmp_path):
    d = store(tmp_path)
    assert codes(run(tmp_path, d, backup=False))["BACKUP_NEVER"] == "warning"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert codes(watch_once(d, tmp_path / "rep", empty, {}, now=NOW))["BACKUP_NEVER"] == "warning"
    assert not set(codes(run(tmp_path, d))) & {"BACKUP_NEVER", "BACKUP_STALE"}
    marker = tmp_path / "bk" / "last-success"
    os.utime(marker, (NOW - 27 * 3600, NOW - 27 * 3600))
    assert (
        codes(watch_once(d, tmp_path / "rep", tmp_path / "bk", {}, now=NOW))["BACKUP_STALE"]
        == "error"
    )


def test_findings_have_effect_and_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(100 * GIB, 99 * GIB, GIB))
    set_state(tmp_path, 100)
    res = run(tmp_path, store(tmp_path, mtime=NOW - 99 * 86400), backup=False)
    mine = [
        f
        for f in res.findings
        if f.code in {"MASS_DELETION", "STORE_IDLE", "LOW_DISK", "BACKUP_NEVER"}
    ]
    assert len(mine) == 4 and all(f.effect and f.fix for f in mine)


def test_clean_run_sends_nothing(tmp_path, hook):
    url, got = hook
    run(tmp_path, store(tmp_path), {"ZODAV_ALERT_URL": url})
    run(tmp_path, store(tmp_path), {"ZODAV_ALERT_URL": url})
    assert got == []


def test_failing_run_posts_then_single_recovered(tmp_path, hook):
    url, got = hook
    env = {"ZODAV_ALERT_URL": url}
    d = tmp_path / "zotero"
    build_defect_store(d)
    run(tmp_path, d, env)
    assert len(got) == 1
    body = got[0]["body"].decode()
    assert got[0]["headers"]["Title"] and "error(s)" in body.splitlines()[0]
    assert len(body.splitlines()) <= 11
    run(tmp_path, d, env)
    assert len(got) == 2  # problems are reported every run
    shutil.rmtree(d)
    store(tmp_path)
    set_state(tmp_path, 5, ok=False)
    run(tmp_path, d, env)
    assert len(got) == 3 and b"recovered" in got[2]["headers"]["Title"].lower().encode()
    run(tmp_path, d, env)
    assert len(got) == 3


def test_alert_never_contains_the_url(tmp_path, hook):
    url, got = hook
    build_defect_store(tmp_path / "zotero")
    run(tmp_path, tmp_path / "zotero", {"ZODAV_ALERT_URL": url})
    assert url.encode() not in got[0]["body"]


def test_payload_shapes(hook, monkeypatch):
    url, got = hook
    send_alert(url, "T", "B")
    assert got[0]["headers"]["Title"] == "T"
    assert got[0]["headers"]["Content-Type"].startswith("text/plain")
    assert got[0]["body"] == b"B"
    # route Slack/Discord hostnames to the local receiver, keep the real URL's host for dispatch
    import urllib.request

    real = urllib.request.urlopen
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: (
            real(url, data=req.data, timeout=timeout)
            if False
            else _redirect(real, req, url, timeout)
        ),
    )
    send_alert("https://hooks.slack.com/services/X", "T", "B")
    send_alert("https://discord.com/api/webhooks/1/x", "T", "B")
    send_alert("https://discordapp.com/api/webhooks/1/x", "T", "B")
    assert json.loads(got[1]["body"]) == {"text": "T\nB"}
    assert json.loads(got[2]["body"]) == {"content": "T\nB"}
    assert json.loads(got[3]["body"]) == {"content": "T\nB"}
    assert got[1]["headers"]["Content-Type"] == "application/json"


def _redirect(real, req, url, timeout):
    import urllib.request

    new = urllib.request.Request(
        url, data=req.data, method="POST", headers=dict(req.header_items())
    )
    return real(new, timeout=timeout)


def test_webhook_down_run_still_completes(tmp_path, capsys):
    build_defect_store(tmp_path / "zotero")
    res = run(tmp_path, tmp_path / "zotero", {"ZODAV_ALERT_URL": "http://127.0.0.1:9/hook"})
    assert not res.ok
    assert "could not send alert" in capsys.readouterr().err
    assert (tmp_path / "rep" / "latest.json").exists()


def test_cli_once_exit_codes(tmp_path, monkeypatch, capsys):
    d = store(tmp_path)
    args = [
        "watch",
        str(d),
        "--once",
        "--reports",
        str(tmp_path / "r"),
        "--backup-state",
        str(fresh_backup(tmp_path, __import__("time").time())),
    ]
    monkeypatch.delenv("ZODAV_ALERT_URL", raising=False)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(100 * GIB, 10 * GIB, 90 * GIB))
    # newest file is a day old relative to the real clock only if mtimes are real; refresh them
    for p in d.iterdir():
        os.utime(p)
    assert main(args) == 0
    build_defect_store(tmp_path / "bad")
    assert main(["watch", str(tmp_path / "bad"), "--once", "--reports", str(tmp_path / "r2")]) == 1
    assert "zodav-audit watch:" in capsys.readouterr().out


def test_invalid_env_exits_2(tmp_path, monkeypatch, capsys):
    d = store(tmp_path)
    monkeypatch.setenv("ZODAV_IDLE_DAYS", "soon")
    assert main(["watch", str(d), "--once", "--reports", str(tmp_path / "r")]) == 2
    assert "ZODAV_IDLE_DAYS" in capsys.readouterr().err


def test_missing_directory_exits_2(tmp_path):
    assert main(["watch", str(tmp_path / "nope"), "--once"]) == 2


def test_loop_survives_a_failing_run(tmp_path, monkeypatch, capsys):
    import zodav_audit as za

    calls = []

    def boom(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk exploded")
        raise KeyboardInterrupt

    monkeypatch.setattr(za, "watch_once", boom)
    monkeypatch.setattr(za.time, "sleep", lambda s: None)
    monkeypatch.delenv("ZODAV_ALERT_URL", raising=False)
    with pytest.raises(KeyboardInterrupt):
        main(["watch", str(store(tmp_path)), "--reports", str(tmp_path / "r")])
    assert len(calls) == 2 and "disk exploded" in capsys.readouterr().err
