import subprocess

import pytest
from davclient import USERS, basic, request

pytestmark = pytest.mark.usefixtures("stack")

COMPOSE = ["docker", "compose", "-f", "docker-compose.test.yml"]
IMAGE = "zodav-webdav:test"
GOOD = "test-zotero-password-0123456789"


def test_healthz_is_anonymous():
    status, _, body = request("GET", "/healthz", user=None)
    assert status == 200
    assert body.strip() == b"ok"


def test_options_challenges_then_reports_dav():
    status, headers, _ = request("OPTIONS", "/zotero/", user=None)
    assert status == 401
    assert headers["www-authenticate"].startswith("Basic")
    status, headers, _ = request("OPTIONS", "/zotero/")
    assert status == 200
    assert "dav" in headers


@pytest.mark.parametrize("user", ["zotero", "service"])
def test_both_users_can_propfind(user):
    status, _, _ = request("PROPFIND", "/zotero/", user=user, headers={"Depth": "0"})
    assert status == 207


def test_wrong_password_and_unknown_user():
    assert request("PROPFIND", "/zotero/", password="wrong-" * 5, headers={"Depth": "0"})[0] == 401
    assert (
        request("PROPFIND", "/zotero/", user="nobody", password=GOOD, headers={"Depth": "0"})[0]
        == 401
    )


@pytest.mark.parametrize(
    "path",
    ["/", "/etc/passwd", "/zotero/../etc/passwd", "/data/zotero/", "/zotero/%2e%2e/etc/passwd"],
)
def test_everything_else_is_denied(path):
    status, _, _ = request("GET", path)
    assert status in (400, 403, 404)


@pytest.mark.parametrize(
    "method", ["LOCK", "UNLOCK", "PROPPATCH", "MOVE", "COPY", "TRACE", "PATCH"]
)
def test_other_methods_refused(method):
    status, _, _ = request(
        method, "/zotero/some.prop", headers={"Destination": "http://127.0.0.1:18080/zotero/x.prop"}
    )
    assert status in (403, 405, 501)


def test_collection_cannot_be_deleted_or_overwritten():
    for method in ("DELETE", "PUT"):
        assert request(method, "/zotero/", body=b"x")[0] == 403
    assert request("MKCOL", "/zotero/")[0] == 405
    status, _, _ = request("PROPFIND", "/zotero/", headers={"Depth": "0"})
    assert status == 207


def test_depth_infinity_refused():
    status, _, _ = request("PROPFIND", "/zotero/", headers={"Depth": "infinity"})
    assert status == 403


def test_server_header_is_bare():
    _, headers, _ = request("GET", "/healthz", user=None)
    assert headers["server"] == "Apache"


def test_no_directory_listing():
    status, _, body = request("GET", "/zotero/")
    assert status in (403, 404, 405) or b"Index of" not in body


@pytest.mark.docker
def test_runs_as_non_root():
    out = subprocess.run(
        COMPOSE + ["exec", "-T", "webdav", "id", "-u"], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() != "0"


def _run(env):
    args = ["docker", "run", "--rm"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    return subprocess.run(args + [IMAGE], capture_output=True, text=True, timeout=60)


@pytest.mark.docker
@pytest.mark.parametrize(
    "env, var",
    [
        ({"ZODAV_PASSWORD": "short"}, "ZODAV_PASSWORD"),
        ({}, "ZODAV_PASSWORD"),
        ({"ZODAV_PASSWORD": "has-a-dollar-sign-$-in-it-0123456789"}, "ZODAV_PASSWORD"),
        ({"ZODAV_PASSWORD": "CHANGE_ME_32_RANDOM_LETTERS_AND_DIGITS"}, "ZODAV_PASSWORD"),
        ({"ZODAV_PASSWORD": GOOD, "ZODAV_USERNAME": "Bad User"}, "ZODAV_USERNAME"),
        ({"ZODAV_PASSWORD": GOOD, "ZODAV_SERVICE_USERNAME": "service"}, "ZODAV_SERVICE_PASSWORD"),
        (
            {"ZODAV_PASSWORD": GOOD, "ZODAV_SERVICE_PASSWORD": USERS["service"]},
            "ZODAV_SERVICE_USERNAME",
        ),
        (
            {
                "ZODAV_PASSWORD": GOOD,
                "ZODAV_SERVICE_USERNAME": "zotero",
                "ZODAV_SERVICE_PASSWORD": USERS["service"],
            },
            "ZODAV_SERVICE_USERNAME",
        ),
        (
            {
                "ZODAV_PASSWORD": GOOD,
                "ZODAV_SERVICE_USERNAME": "service",
                "ZODAV_SERVICE_PASSWORD": "tiny",
            },
            "ZODAV_SERVICE_PASSWORD",
        ),
    ],
)
def test_bad_settings_exit_naming_the_variable(env, var):
    res = _run(env)
    assert res.returncode != 0
    assert var in res.stderr
    assert basic("x", "y") not in res.stderr
