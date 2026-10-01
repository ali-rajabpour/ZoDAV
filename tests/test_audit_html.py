from html.parser import HTMLParser

from zodav_audit import Finding, Result, render_html

AUTHOR = "https://rajabpour.com"


class Strict(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)

    def error(self, message):  # pragma: no cover
        self.errors.append(message)


def make():
    return Result(
        command="conformance",
        target="dav.example/zotero/",
        started="2026-10-01T10:00:00Z",
        findings=[
            Finding(
                "error",
                "MISSING_PROP",
                "abcd1234.prop",
                "Prop file <script>alert(1)</script> missing",
                "Zotero re-uploads.",
                "Run repair.",
            ),
            Finding("warning", "OLD_ITEM", "x.zip", "Item is old & unused"),
            Finding("info", "STEP_OK", "", "PUT works"),
            Finding("info", "PROVIDER_HINT", "", "Looks like Apache"),
        ],
        stats={"files": 12, "size": "3 MB <u>"},
    )


def test_html_valid_escaped_and_self_contained():
    r = make()
    page = render_html(r)
    p = Strict()
    p.feed(page)
    p.close()
    assert not p.errors and "h1" in p.tags and "script" not in p.tags
    assert page.startswith("<!doctype html>")
    assert "<script" not in page and "&lt;script&gt;" in page and "<u>" not in page
    for f in r.findings:
        assert html_escape(f.message) in page
    assert page.replace(AUTHOR, "").count("http") == 0
    assert "Problems found" in page


def test_html_passed_status():
    page = render_html(
        Result("integrity", "/data", "now", [Finding("info", "STEP_OK", "", "ok step")])
    )
    assert ">Passed<" in page


def html_escape(s):
    import html

    return html.escape(s)
