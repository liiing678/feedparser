# Copyright 2010-2025 Kurt McKee <contactme@kurtmckee.org>
# Copyright 2002-2008 Mark Pilgrim
# All rights reserved.
#
# This file is a part of feedparser.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice,
#   this list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS 'AS IS'
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from __future__ import annotations

import typing

import requests
import urllib3

from .datetimes import _parse_date
from .exceptions import MaxBytesExceeded

# HTTP "Accept" header to send to servers when downloading feeds.
ACCEPT_HEADER: str = (
    "application/atom+xml"
    ",application/rdf+xml"
    ",application/rss+xml"
    ",application/x-netcdf"
    ",application/xml"
    ";q=0.9,text/xml"
    ";q=0.2,*/*"
    ";q=0.1"
)

# How many (decompressed) bytes to read from a response body at a time
# when a max_bytes budget is enforced.
READ_CHUNK_SIZE = 2**16


def get(url: str, result: dict[str, typing.Any], max_bytes: int | None = None) -> bytes:
    try:
        response = requests.get(
            url,
            headers={"Accept": ACCEPT_HEADER},
            timeout=10,
            stream=max_bytes is not None,
        )
    except requests.RequestException as exception:
        result["bozo"] = True
        result["bozo_exception"] = exception
        return b""

    # Lowercase the HTTP header keys for comparisons per RFC 2616.
    result["headers"] = {k.lower(): v for k, v in response.headers.items()}

    # save HTTP headers
    if "etag" in result["headers"]:
        result["etag"] = result["headers"]["etag"]
    if "last-modified" in result["headers"]:
        modified = result["headers"]["last-modified"]
        if modified:
            result["modified"] = modified
            result["modified_parsed"] = _parse_date(modified)
    result["href"] = response.url
    result["status"] = response.status_code

    if max_bytes is None:
        return response.content

    try:
        return _read_bounded(response, max_bytes)
    except MaxBytesExceeded as exception:
        result["bozo"] = True
        result["bozo_exception"] = exception
        return b""
    except (requests.RequestException, urllib3.exceptions.HTTPError) as exception:
        result["bozo"] = True
        result["bozo_exception"] = exception
        return b""
    finally:
        # The response is owned by the library; always release it.
        response.close()


def _read_bounded(response: requests.Response, max_bytes: int) -> bytes:
    """Read at most ``max_bytes`` decompressed bytes from a streamed response.

    Decompression (gzip, including concatenated members, zlib-wrapped
    deflate, and raw deflate) is performed incrementally by urllib3,
    so a compressed body cannot bypass the budget and the full body is
    never read or decompressed before the budget is checked.

    :raises MaxBytesExceeded: if the decompressed body exceeds ``max_bytes``.
    """

    chunks = []
    total = 0
    while total <= max_bytes:
        chunk = response.raw.read(
            min(READ_CHUNK_SIZE, max_bytes + 1 - total),
            decode_content=True,
        )
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > max_bytes:
        raise MaxBytesExceeded(
            "the decompressed response body exceeds the max_bytes budget "
            "of %d bytes" % max_bytes
        )
    return b"".join(chunks)
