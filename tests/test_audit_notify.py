import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from zodav_audit import main, notify

TOKEN = "123456:SECRET-TOKEN"
PASSWORD = "smtp-pass-xyz"


@pytest.fixture
def telegram(monkeypatch):
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            got.append({"path": self.path, "body": json.loads(self.rfile.read(n))})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ZODAV_TELEGRAM_API", f"http://127.0.0.1:{srv.server_port}")
    yield got
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def smtp_sink():
    mails = []

    class H(socketserver.StreamRequestHandler):
        def reply(self, line):
            self.wfile.write(line.encode() + b"\r\n")

        def handle(self):
            self.reply("220 sink")
            mail = {"to": []}
            while True:
                line = self.rfile.readline().decode().rstrip("\r\n")
                cmd = line.upper()
                if not line or cmd == "QUIT":
                    self.reply("221 bye")
                    return
                if cmd.startswith("EHLO") or cmd.startswith("HELO"):
                    self.reply("250 sink")
                elif cmd.startswith("MAIL FROM"):
                    mail["from"] = line
                    self.reply("250 ok")
                elif cmd.startswith("RCPT TO"):
                    mail["to"].append(line)
                    self.reply("250 ok")
                elif cmd == "DATA":
                    self.reply("354 go")
                    lines = []
                    while (d := self.rfile.readline().decode().rstrip("\r\n")) != ".":
                        lines.append(d)
                    mail["data"] = "\n".join(lines)
                    mails.append(mail)
                    self.reply("250 queued")
                else:
                    self.reply("250 ok")

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], mails
    srv.shutdown()
    srv.server_close()


def tg_env():
    return {"ZODAV_TELEGRAM_BOT_TOKEN": TOKEN, "ZODAV_TELEGRAM_CHAT_ID": "42"}


def smtp_env(port, **extra):
    return {
        "ZODAV_SMTP_HOST": "127.0.0.1",
        "ZODAV_SMTP_PORT": str(port),
        "ZODAV_SMTP_SECURITY": "none",
        "ZODAV_SMTP_FROM": "zodav@example.org",
        "ZODAV_SMTP_TO": "a@example.org, b@example.org",
        **extra,
    }


def test_telegram_request_shape(telegram, capsys):
    notify(tg_env(), "Title", "x" * 5000)
    assert len(telegram) == 1
    r = telegram[0]
    assert r["path"] == f"/bot{TOKEN}/sendMessage"
    assert r["body"]["chat_id"] == "42"
    assert r["body"]["disable_web_page_preview"] is True
    assert "parse_mode" not in r["body"]
    assert r["body"]["text"].startswith("Title\n") and len(r["body"]["text"]) == 4096


def test_email_received(smtp_sink, capsys):
    port, mails = smtp_sink
    notify(smtp_env(port), "ZoDAV needs attention", "something broke")
    assert len(mails) == 1
    m = mails[0]
    assert len(m["to"]) == 2 and "a@example.org" in m["to"][0]
    assert "Subject: ZoDAV: ZoDAV needs attention" in m["data"]
    assert "something broke" in m["data"]
    assert "unencrypted" in capsys.readouterr().err


def test_all_three_channels(telegram, smtp_sink, hook_server):
    port, mails = smtp_sink
    url, hooks = hook_server
    notify({**tg_env(), **smtp_env(port), "ZODAV_ALERT_URL": url}, "T", "B")
    assert len(telegram) == 1 and len(mails) == 1 and len(hooks) == 1


@pytest.fixture
def hook_server():
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/h", got
    srv.shutdown()
    srv.server_close()


def test_failing_channel_does_not_stop_others(telegram, smtp_sink, capsys):
    port, mails = smtp_sink
    env = {
        **tg_env(),
        **smtp_env(port, ZODAV_SMTP_USERNAME="u", ZODAV_SMTP_PASSWORD=PASSWORD),
        "ZODAV_ALERT_URL": "http://127.0.0.1:9/hook?token=URLSECRET",
    }
    notify(env, "T", "B")  # webhook refused, smtp login unsupported by the sink
    err = capsys.readouterr().err
    assert "could not send alert" in err
    assert len(telegram) == 1
    for secret in (TOKEN, PASSWORD, "URLSECRET"):
        assert secret not in err


def test_telegram_down_email_still_sent(smtp_sink, monkeypatch, capsys):
    port, mails = smtp_sink
    monkeypatch.setenv("ZODAV_TELEGRAM_API", "http://127.0.0.1:9")
    notify({**tg_env(), **smtp_env(port)}, "T", "B")
    err = capsys.readouterr().err
    assert len(mails) == 1 and "Telegram" in err and TOKEN not in err


@pytest.mark.parametrize(
    "env,needle",
    [
        ({"ZODAV_TELEGRAM_BOT_TOKEN": TOKEN}, "ZODAV_TELEGRAM_CHAT_ID"),
        ({"ZODAV_TELEGRAM_CHAT_ID": "42"}, "ZODAV_TELEGRAM_BOT_TOKEN"),
        ({"ZODAV_SMTP_HOST": "h", "ZODAV_SMTP_FROM": "a@b.c"}, "ZODAV_SMTP_TO"),
        ({"ZODAV_SMTP_HOST": "h", "ZODAV_SMTP_TO": "a@b.c"}, "ZODAV_SMTP_FROM"),
        (
            {
                "ZODAV_SMTP_HOST": "h",
                "ZODAV_SMTP_FROM": "a@b.c",
                "ZODAV_SMTP_TO": "a@b.c",
                "ZODAV_SMTP_USERNAME": "u",
            },
            "set together",
        ),
        (
            {
                "ZODAV_SMTP_HOST": "h",
                "ZODAV_SMTP_FROM": "a@b.c",
                "ZODAV_SMTP_TO": "a@b.c",
                "ZODAV_SMTP_SECURITY": "plain",
            },
            "ZODAV_SMTP_SECURITY",
        ),
    ],
)
def test_config_errors_exit_2(tmp_path, monkeypatch, capsys, env, needle):
    from fakedav import build_clean_store

    d = tmp_path / "z"
    build_clean_store(d)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert main(["watch", str(d), "--once", "--reports", str(tmp_path / "r")]) == 2
    err = capsys.readouterr().err
    assert needle in err and TOKEN not in err
