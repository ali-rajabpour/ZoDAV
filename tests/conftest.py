import time
import urllib.request

import pytest
from davclient import HOST, PORT, SMALL_PORT

UP = "docker compose -f docker-compose.test.yml up -d --build --wait"


def _healthy(port):
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/healthz", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


@pytest.fixture(scope="session")
def stack():
    deadline = time.time() + 90
    while time.time() < deadline:
        if _healthy(PORT) and _healthy(SMALL_PORT):
            return
        time.sleep(1)
    pytest.fail(f"test stack is not up; run: {UP}")


def pytest_collection_modifyitems(items):
    # Anything that needs the compose test stack is a docker test, so
    # `-m "not docker"` is safe without the stack.
    for item in items:
        if "stack" in item.fixturenames:
            item.add_marker(pytest.mark.docker)
