"""Logging filters that keep user locations out of server logs."""

from __future__ import annotations

import logging
import re

_QUERY = re.compile(r"\?[^\s\"']*")


def strip_query(text: str) -> str:
    """Remove query strings from any URLs or request lines in `text`."""
    return _QUERY.sub("?[redacted]", text)


class RedactQueryStringFilter(logging.Filter):
    """Redact query strings from request-log lines.

    Older API callers may still put coordinates, zones or searched addresses in
    the URL (e.g. GET /api/scene?lat=..&lon=..). Django's request logger
    ("django.server" for runserver, "django.request" for errors) would write
    those to the log; this filter replaces the query string with "[redacted]".
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "?" in msg:
            record.msg = strip_query(msg)
            record.args = ()
        return True
