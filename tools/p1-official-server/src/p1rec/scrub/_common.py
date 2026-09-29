"""What every part of the scrubber shares: its error, placeholders, the CA signature key and base64."""

from __future__ import annotations

import base64
import binascii
import logging
import re

# One logger for the whole package, so its records read p1rec.scrub whichever module writes them
logger = logging.getLogger(__name__.rpartition(".")[0])

PLACEHOLDER_RE = re.compile(r"([A-Z][A-Z0-9_]*)_([1-9][0-9]*)")

_ESCAPED_SLASH_RE = re.compile(r"\\+/")

# An order object's CA signature is base64 DER that may embed the
# certificate holder's name and national ID where no string match can see
# them, so every non-empty one is an original of this category.
CA_KEY = "ca"
CA_CATEGORY = "CA_SIGNATURE"

REST_SOURCE = "rest.jsonl"


class ScrubError(RuntimeError):
    """Raised when scrubbing cannot proceed safely; never holds an original value."""


def decode_b64(value: str) -> bytes | None:
    """Decode strict base64, allowing a JSON-escaped "/" at any depth, or return None if it is not base64."""
    try:
        return base64.b64decode(_ESCAPED_SLASH_RE.sub("/", value), validate=True)
    except (binascii.Error, ValueError):
        return None


def category_of(placeholder: str) -> str:
    """Return the category part of a <CATEGORY>_<n> placeholder, or the placeholder itself."""
    match = PLACEHOLDER_RE.fullmatch(placeholder)
    return match.group(1) if match else placeholder


def is_placeholder(value: str) -> bool:
    """Return True if value is a <CATEGORY>_<n> placeholder."""
    return PLACEHOLDER_RE.fullmatch(value) is not None
