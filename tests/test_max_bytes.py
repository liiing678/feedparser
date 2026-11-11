# Tests for the max_bytes body budget of feedparser.parse().

import gzip
import io
import os
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import feedparser
from feedparser.exceptions import MaxBytesExceeded

FEED = (
    b"<?xml version='1.0' encoding='utf-8'?>\n"
    b'<feed xmlns="http://www.w3.org/2005/Atom">\n'
    b"<title>Example</title>\n"
    b'<entry><title>one</title><id>urn:example:1</id></entry>\n'
    b"</feed>\n"
)

# A feed whose title contains multibyte UTF-8 characters.
MULTIBYTE_FEED = (
    "<?xml version='1.0' encoding='utf-8'?>\n"
    '<feed xmlns="http://www.w3.org/2005/Atom">\n'
    "<title>café 😀</title>\n"
    '<entry><title>one</title><id>urn:example:1</id></entry>\n'
    "</feed>\n"
).encode("utf-8")


def assert_over_budget(result):
    assert result.bozo
    assert isinstance(result.bozo_exception, MaxBytesExceeded)
    assert result.entries == []
    assert result.feed == {}


class ShortReadStream:
    """A binary stream that returns at most ``chunk`` bytes per read."""

    def __init__(self, data, chunk=3):
        self.buffer = io.BytesIO(data)
        self.chunk = chunk

    def read(self, size=-1):
        if size is None or size < 0 or size > self.chunk:
            size = self.chunk
        return self.buffer.read(size)


class RecordingStream(io.BytesIO):
    """A stream that records every read, to prove no I/O happens."""

    def __init__(self, data):
        super().__init__(data)
        self.reads = 0

    def read(self, size=-1):
        self.reads += 1
        return super().read(size)


# Invalid budgets are rejected before any I/O.


@pytest.mark.parametrize("budget", [0, -1, -100, 1.5, "100", b"100", True])
def test_invalid_max_bytes_rejected_before_io(budget):
    stream = RecordingStream(FEED)
    with pytest.raises(ValueError):
        feedparser.parse(stream, max_bytes=budget)
    assert stream.reads == 0


def test_invalid_max_bytes_rejected_before_network_io():
    # Nothing may raise URLError/connection errors; ValueError comes first.
    with pytest.raises(ValueError):
        feedparser.parse("http://localhost:1/unreachable", max_bytes=0)


def test_max_bytes_none_is_valid():
    result = feedparser.parse(FEED, max_bytes=None)
    assert not result.bozo
    assert len(result.entries) == 1


# Exactly at / over the limit, for every input kind.


def test_bytes_exactly_at_limit():
    result = feedparser.parse(FEED, max_bytes=len(FEED))
    assert not result.bozo
    assert len(result.entries) == 1


def test_bytes_over_limit_by_one():
    result = feedparser.parse(FEED, max_bytes=len(FEED) - 1)
    assert_over_budget(result)


def test_str_exactly_at_limit():
    text = FEED.decode("utf-8")
    result = feedparser.parse(text, max_bytes=len(FEED))
    assert not result.bozo
    assert len(result.entries) == 1


def test_str_over_limit():
    text = FEED.decode("utf-8")
    result = feedparser.parse(text, max_bytes=len(FEED) - 1)
    assert_over_budget(result)


def test_binary_stream_exactly_at_limit():
    result = feedparser.parse(io.BytesIO(FEED), max_bytes=len(FEED))
    assert not result.bozo
    assert len(result.entries) == 1


def test_binary_stream_over_limit():
    result = feedparser.parse(io.BytesIO(FEED), max_bytes=len(FEED) - 1)
    assert_over_budget(result)


def test_text_stream_exactly_at_limit():
    result = feedparser.parse(io.StringIO(FEED.decode("utf-8")), max_bytes=len(FEED))
    assert not result.bozo
    assert len(result.entries) == 1


def test_text_stream_over_limit():
    result = feedparser.parse(
        io.StringIO(FEED.decode("utf-8")), max_bytes=len(FEED) - 1
    )
    assert_over_budget(result)


def test_local_file_exactly_at_limit(tmp_path):
    path = tmp_path / "feed.atom"
    path.write_bytes(FEED)
    result = feedparser.parse(str(path), max_bytes=len(FEED))
    assert not result.bozo
    assert len(result.entries) == 1


def test_local_file_over_limit(tmp_path):
    path = tmp_path / "feed.atom"
    path.write_bytes(FEED)
    result = feedparser.parse(str(path), max_bytes=len(FEED) - 1)
    assert_over_budget(result)


# Short reads must not confuse the budget accounting.


def test_short_reads_within_budget():
    result = feedparser.parse(ShortReadStream(FEED, chunk=1), max_bytes=len(FEED))
    assert not result.bozo
    assert len(result.entries) == 1


def test_short_reads_over_budget():
    result = feedparser.parse(
        ShortReadStream(FEED, chunk=1), max_bytes=len(FEED) - 1
    )
    assert_over_budget(result)


def test_short_reads_never_read_past_budget():
    stream = ShortReadStream(FEED * 100, chunk=7)
    result = feedparser.parse(stream, max_bytes=10)
    assert_over_budget(result)
    # At most max_bytes + 1 bytes may have been consumed.
    assert stream.buffer.tell() <= 11


# The budget counts bytes, not characters.


def test_multibyte_budget_counts_bytes_not_characters():
    text = MULTIBYTE_FEED.decode("utf-8")
    assert len(text) < len(MULTIBYTE_FEED)  # fewer characters than bytes
    # A budget of the *character* count must reject the *byte*-sized body.
    result = feedparser.parse(text, max_bytes=len(text))
    assert_over_budget(result)
    # The exact byte count must accept it.
    result = feedparser.parse(text, max_bytes=len(MULTIBYTE_FEED))
    assert not result.bozo
    assert len(result.entries) == 1


def test_multibyte_binary_feed_over_limit():
    result = feedparser.parse(MULTIBYTE_FEED, max_bytes=len(MULTIBYTE_FEED) - 1)
    assert_over_budget(result)


def test_multibyte_text_stream_over_limit():
    text = MULTIBYTE_FEED.decode("utf-8")
    result = feedparser.parse(io.StringIO(text), max_bytes=len(MULTIBYTE_FEED) - 1)
    assert_over_budget(result)


# Resource ownership.


def test_caller_stream_not_closed_on_budget_exceeded():
    stream = io.BytesIO(FEED)
    result = feedparser.parse(stream, max_bytes=1)
    assert_over_budget(result)
    assert not stream.closed


def test_caller_text_stream_not_closed_on_budget_exceeded():
    stream = io.StringIO(FEED.decode("utf-8"))
    result = feedparser.parse(stream, max_bytes=1)
    assert_over_budget(result)
    assert not stream.closed


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="requires procfs")
def test_library_owned_file_closed_on_budget_exceeded(tmp_path):
    path = tmp_path / "feed.atom"
    path.write_bytes(FEED)
    fds_before = len(os.listdir("/proc/self/fd"))
    result = feedparser.parse(str(path), max_bytes=1)
    assert_over_budget(result)
    assert len(os.listdir("/proc/self/fd")) == fds_before


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="requires procfs")
def test_library_owned_file_closed_on_success(tmp_path):
    path = tmp_path / "feed.atom"
    path.write_bytes(FEED)
    fds_before = len(os.listdir("/proc/self/fd"))
    result = feedparser.parse(str(path), max_bytes=len(FEED))
    assert not result.bozo
    assert len(os.listdir("/proc/self/fd")) == fds_before


# HTTP tests: redirects, 304, and compression cannot bypass the budget.


def gzip_multi_member(data, members=3):
    """Compress data as concatenated gzip members."""
    chunk, rest = divmod(len(data), members)
    parts = [data[i * chunk : (i + 1) * chunk] for i in range(members - 1)]
    parts.append(data[(members - 1) * chunk :])
    assert rest == 0 or len(parts[-1]) == chunk + rest
    return b"".join(gzip.compress(part) for part in parts)


def zlib_compress(data):
    return zlib.compress(data)


def raw_deflate(data):
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()


BIG_FEED = FEED + b"<!-- " + b"x" * 10000 + b" -->\n"


class BudgetHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", headers=()):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/feed":
            self._send(
                200,
                FEED,
                [("Content-Type", "application/atom+xml"), ("ETag", '"abc"')],
            )
        elif self.path == "/redirect":
            self._send(302, headers=[("Location", "/feed")])
        elif self.path == "/redirect-chain":
            self._send(301, headers=[("Location", "/redirect")])
        elif self.path == "/not-modified":
            self._send(304, headers=[("ETag", '"abc"')])
        elif self.path == "/gzip":
            self._send(
                200,
                gzip.compress(BIG_FEED),
                [
                    ("Content-Type", "application/atom+xml"),
                    ("Content-Encoding", "gzip"),
                ],
            )
        elif self.path == "/gzip-multi":
            self._send(
                200,
                gzip_multi_member(BIG_FEED),
                [
                    ("Content-Type", "application/atom+xml"),
                    ("Content-Encoding", "gzip"),
                ],
            )
        elif self.path == "/deflate-zlib":
            self._send(
                200,
                zlib_compress(BIG_FEED),
                [
                    ("Content-Type", "application/atom+xml"),
                    ("Content-Encoding", "deflate"),
                ],
            )
        elif self.path == "/deflate-raw":
            self._send(
                200,
                raw_deflate(BIG_FEED),
                [
                    ("Content-Type", "application/atom+xml"),
                    ("Content-Encoding", "deflate"),
                ],
            )
        else:
            self._send(404)


@pytest.fixture(scope="module")
def server():
    import responses

    # The session-wide responses mock blocks all HTTP by default;
    # allow real requests to the local test server.
    responses.add_passthru("http://127.0.0.1")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), BudgetHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1]
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


def test_http_exactly_at_limit(server):
    result = feedparser.parse(server + "/feed", max_bytes=len(FEED))
    assert not result.bozo
    assert len(result.entries) == 1
    assert result.status == 200


def test_http_over_limit_keeps_metadata(server):
    result = feedparser.parse(server + "/feed", max_bytes=len(FEED) - 1)
    assert_over_budget(result)
    # HTTP metadata gathered before the body was read is still available.
    assert result.status == 200
    assert result.href == server + "/feed"
    assert result.etag == '"abc"'
    assert "content-type" in result.headers


def test_http_redirect_within_budget(server):
    result = feedparser.parse(server + "/redirect", max_bytes=len(FEED))
    assert not result.bozo
    assert len(result.entries) == 1
    assert result.href == server + "/feed"


def test_http_redirect_chain_over_budget(server):
    result = feedparser.parse(server + "/redirect-chain", max_bytes=10)
    assert_over_budget(result)
    # The final URL (after both redirects) is still reported.
    assert result.href == server + "/feed"
    assert result.status == 200


def test_http_304_with_budget(server):
    result = feedparser.parse(server + "/not-modified", max_bytes=10)
    assert not result.bozo
    assert result.status == 304
    assert result.entries == []
    assert result.etag == '"abc"'


@pytest.mark.parametrize(
    "path",
    ["/gzip", "/gzip-multi", "/deflate-zlib", "/deflate-raw"],
    ids=["gzip", "gzip-multi-member", "deflate-zlib", "deflate-raw"],
)
def test_http_compressed_body_cannot_bypass_budget(server, path):
    # The compressed body is small; the decompressed body is not.
    result = feedparser.parse(server + path, max_bytes=len(FEED))
    assert_over_budget(result)
    assert result.status == 200


@pytest.mark.parametrize(
    "path",
    ["/gzip", "/gzip-multi", "/deflate-zlib", "/deflate-raw"],
    ids=["gzip", "gzip-multi-member", "deflate-zlib", "deflate-raw"],
)
def test_http_compressed_body_exactly_at_limit(server, path):
    result = feedparser.parse(server + path, max_bytes=len(BIG_FEED))
    assert not result.bozo
    assert len(result.entries) == 1


@pytest.mark.parametrize(
    "path",
    ["/gzip", "/gzip-multi", "/deflate-zlib", "/deflate-raw"],
    ids=["gzip", "gzip-multi-member", "deflate-zlib", "deflate-raw"],
)
def test_http_compressed_body_without_budget_still_works(server, path):
    # No budget: the existing decompression behavior is unchanged.
    result = feedparser.parse(server + path)
    assert not result.bozo
    assert len(result.entries) == 1


def test_http_without_budget_unchanged(server):
    result = feedparser.parse(server + "/feed")
    assert not result.bozo
    assert len(result.entries) == 1
    assert result.status == 200
