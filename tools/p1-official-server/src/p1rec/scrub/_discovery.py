"""Find the identifiers to scrub in every recording under results_root."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from p1rec.config import P1Config
from p1rec.scrub._common import CA_CATEGORY, CA_KEY, REST_SOURCE, decode_b64, logger
from p1rec.sse import parse_frame

# Logical operations whose recorded responses the scrub pointers apply to
_ACCOUNTS_OP = "accounts"
_CA_STATUS_OP = "ca_status"

_ARRAY_INDEX_RE = re.compile(r"0|[1-9][0-9]*")

# Recordings whose lines can hold identifiers by key, and the marker of a
# line keeping a body or frame that is not UTF-8 in a *_b64 field.
_SSE_RECORDING_GLOB = "sse_*.jsonl"
_B64_FIELD_MARKER = b'_b64"'


def pointer_category(pointer: str) -> str:
    """Return the placeholder category of the values a scrub pointer finds, its last token upper-cased."""
    return pointer.rsplit("/", 1)[1].upper()


def _resolve_pointer(document: object, pointer: str) -> object | None:
    """Resolve an RFC 6901 JSON pointer, returning None if any token is missing."""
    node = document
    for raw_token in pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if token not in node:
                return None
            node = node[token]
        elif isinstance(node, list):
            if not _ARRAY_INDEX_RE.fullmatch(token) or int(token) >= len(node):
                return None
            node = node[int(token)]
        else:
            return None
    return node


def _identifier_text(value: object) -> str | None:
    """Return value as it appears in text if it can be an identifier, else None."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _field_text(record: Mapping[str, object], key: str) -> str | None:
    """Return a recorded text field, decoding its *_b64 twin instead when the text was not UTF-8."""
    encoded = record.get(f"{key}_b64")
    if isinstance(encoded, str):
        decoded = decode_b64(encoded)
        if decoded is not None:
            return decoded.decode("utf-8", "replace")
    value = record.get(key)
    return value if isinstance(value, str) else None


def _response_json(record: Mapping[str, object]) -> object | None:
    """Parse a rest.jsonl record's response body as JSON, or return None."""
    body = _field_text(record, "resp_body")
    if body is None:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def _iter_jsonl_records(path: Path, markers: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    """Yield, in file order, the records of a JSONL file whose line contains any of markers."""
    wanted = list(markers)
    with path.open("rb") as fh:
        for number, line in enumerate(fh, start=1):
            if not any(marker in line for marker in wanted):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                logger.warning("skipping unparsable line %d of %s", number, path)
                continue
            if isinstance(record, dict):
                yield record


def key_categories(cfg: P1Config) -> dict[str, str]:
    """Return each of scrub.account_id_keys with its category, the key upper-cased."""
    return {key: key.upper() for key in cfg.scrub.account_id_keys}


def _record_keys(cfg: P1Config) -> dict[str, str]:
    """Return every key whose values are originals wherever recorded JSON holds it: "ca" and account_id_keys."""
    return {CA_KEY: CA_CATEGORY, **key_categories(cfg)}


def _record_documents(record: Mapping[str, object]) -> Iterator[object]:
    """Yield each JSON document a recorded line carries: its request and response bodies, or its frame's data."""
    texts = [_field_text(record, "req_body"), _field_text(record, "resp_body")]
    for key in ("raw", "partial"):
        frame = _field_text(record, key)
        if frame is not None:
            texts.append(parse_frame(frame).data)
    for text in texts:
        if isinstance(text, str):
            try:
                yield json.loads(text)
            except ValueError:
                continue


def _collect_key_values(document: object, keys: Mapping[str, str], found: dict[str, list[str]]) -> None:
    """Add to found, by category and in order of first appearance, each key in keys' value at any depth."""
    if isinstance(document, dict):
        for key, value in document.items():
            text = _identifier_text(value) if key in keys else None
            if text is None:
                _collect_key_values(value, keys, found)
                continue
            bucket = found.setdefault(keys[key], [])
            if text not in bucket:
                bucket.append(text)
    elif isinstance(document, list):
        for item in document:
            _collect_key_values(item, keys, found)


def discover(cfg: P1Config) -> dict[str, list[str]]:
    """Return category -> identifiers found in every recording under results_root.

    The scrub pointers apply to recorded accounts and ca_status responses,
    each to every element of a response that is a JSON array. The "ca"
    signatures and scrub.account_id_keys values are collected at any depth
    of every request, response and frame in every rest.jsonl and
    sse_*.jsonl, including bodies and frames kept in *_b64 fields, so a
    value is known even where a fixture shows it in another form, such as
    a log line. Runs and their files are read in name order, which is
    chronological for runs. Logs a warning for an operation with no
    recorded response, or a pointer that found nothing, but never logs a
    value found.
    """
    pointers_by_op = {_ACCOUNTS_OP: cfg.scrub.account_id_pointers, _CA_STATUS_OP: cfg.scrub.ca_pointers}
    keys = _record_keys(cfg)
    responses = dict.fromkeys(pointers_by_op, 0)
    hits = {(op, pointer): 0 for op, pointers in pointers_by_op.items() for pointer in pointers}
    found: dict[str, list[str]] = {}

    # A key inside a body or frame shows in the JSONL line as \"key\", since that text is itself a JSON string
    key_markers = [b'\\"' + key.encode("utf-8") + b'\\"' for key in keys] + [_B64_FIELD_MARKER]
    op_markers = [op.encode("utf-8") for op in pointers_by_op]
    root = cfg.results_root
    recordings = sorted([*root.glob(f"*/{REST_SOURCE}"), *root.glob(f"*/{_SSE_RECORDING_GLOB}")])
    for recording in recordings:
        is_rest = recording.name == REST_SOURCE
        for record in _iter_jsonl_records(recording, key_markers + op_markers if is_rest else key_markers):
            for document in _record_documents(record):
                _collect_key_values(document, keys, found)
            if not is_rest:
                continue
            op = record.get("op")
            if not isinstance(op, str) or op not in pointers_by_op:
                continue
            document = _response_json(record)
            if document is None:
                continue
            responses[op] += 1
            elements = document if isinstance(document, list) else [document]
            for element in elements:
                for pointer in pointers_by_op[op]:
                    value = _identifier_text(_resolve_pointer(element, pointer))
                    if value is None:
                        continue
                    hits[(op, pointer)] += 1
                    bucket = found.setdefault(pointer_category(pointer), [])
                    if value not in bucket:
                        bucket.append(value)

    for op, count in responses.items():
        if count == 0:
            logger.warning("no recorded %s response with a JSON body under %s", op, cfg.results_root)
    for (op, pointer), count in hits.items():
        if responses[op] and count == 0:
            logger.warning("scrub pointer %s found nothing in %d recorded %s responses", pointer, responses[op], op)
    logger.info(
        "discovered identifiers: %s",
        ", ".join(f"{category}={len(values)}" for category, values in found.items()) or "none",
    )
    return found
