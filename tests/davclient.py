"""Minimal WebDAV client for the tests: http.client, no redirects, no retries."""

import base64
import http.client

HOST = "127.0.0.1"
PORT = 18080
SMALL_PORT = 18081
USERS = {
    "zotero": "test-zotero-password-0123456789",
    "service": "test-service-password-0123456789",
}


def basic(user, pw):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def request(method, path, *, user="zotero", password=None, body=None, headers=None, port=PORT):
    """Send one request. user=None sends it unauthenticated.

    Returns (status, lowercased response headers, body bytes).
    """
    hdrs = dict(headers or {})
    if user is not None:
        if password is None:
            password = USERS[user]
        hdrs["Authorization"] = basic(user, password)
    conn = http.client.HTTPConnection(HOST, port, timeout=60)
    try:
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    finally:
        conn.close()
