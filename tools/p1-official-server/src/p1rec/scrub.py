"""De-identify recorded data, export fixtures and scan for leaked identifiers.

The originals are every secret read from the environment, the API key
prefix the server embeds in clientname, and the identifiers the configured
JSON pointers find in recorded accounts and ca_status responses. build_map
gives each original a placeholder, <CATEGORY>_<n>, numbered per category in
order of first appearance and kept stable across runs by the map file under
results_root, which is the only place originals are written.

Recorded files hold a value not only verbatim but also JSON-escaped, once
as a string in a JSONL line or twice as a server's escaped body stored in
one, percent-encoded in a URL, and inside base64 fields. scrub_text and
scan match all of those forms, case-insensitively, so an upper-case
\\uXXXX escape counts too.
"""

from __future__ import annotations

import base64
import binascii
import functools
import json
import logging
import os
import re
import shutil
import tempfile
import tomllib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from p1rec.config import ConfigError, P1Config, read_secrets
from p1rec.redact import redaction_values
from p1rec.sse import parse_frame

logger = logging.getLogger(__name__)

# Logical operations whose recorded responses the scrub pointers apply to
_ACCOUNTS_OP = "accounts"
_CA_STATUS_OP = "ca_status"

# Secret roles in the order they claim placeholders, identities before
# credentials, so a credential equal to an identity value, such as a CA
# password set to the national ID, is labelled as the identity and its
# placeholder does not reveal that it is also a credential.
_ROLE_ORDER = ("person_id", "egress_ip", "api_key", "sec_key", "ca_passwd")

# Category of a value redaction_values derives from a secret instead of
# returning as is, which is the API key's clientname prefix.
_DERIVED_CATEGORY = "API_KEY_PREFIX"

_CATEGORY_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_PLACEHOLDER_RE = re.compile(r"([A-Z][A-Z0-9_]*)_([1-9][0-9]*)")

# A body or frame that is not valid UTF-8 is kept in a *_b64 field, and
# legacy Taiwanese systems encode Chinese text as Big5.
_LEGACY_ENCODINGS = ("cp950",)

# A base64 field as a JSON member, allowing the optional \/ escape of "/"
_B64_FIELD_RE = re.compile(r'"[A-Za-z0-9_]*_b64"\s*:\s*"((?:[A-Za-z0-9+=]|\\?/)*)"')

_ARRAY_INDEX_RE = re.compile(r"0|[1-9][0-9]*")

# An order object's CA signature is base64 DER that may embed the
# certificate holder's name and national ID where no string match can see
# them, so every non-empty one is replaced by this fixed placeholder, which
# is also the category a leaked one is reported under.
_CA_KEY = "ca"
_CA_CATEGORY = "CA_SIGNATURE"

# Unicode's Supplementary Private Use Area-A, U+F0000 to U+FFFFD, which
# recorded text has no reason to contain, lends one marker character to
# each original while replacement runs.
_MARKER_FIRST = 0xF0000
_MARKER_LAST = 0xFFFFD
_MARKER_RE = re.compile(f"[{chr(_MARKER_FIRST)}-{chr(_MARKER_LAST)}]")

# CSI escape sequences, such as the colour codes a log written for a terminal carries
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# A leading ISO-8601 date and time with optional fraction and UTC offset,
# as the official server's log (2026-09-29T10:04:55.724337Z) and the
# recorder's own log format (2026-09-29 19:41:05,203, local time) begin.
_LOG_TIMESTAMP_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?(Z|[+-]\d{2}:?\d{2})?"
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_NS_PER_S = 1_000_000_000
_NS_PER_US = 1_000
_NS_FRACTION_DIGITS = 9

_REST_SOURCE = "rest.jsonl"
_SSE_SOURCE_RE = re.compile(r"sse_.+\.jsonl")
_MANIFEST_NAME = "MANIFEST.toml"
_ENTRY_KEYS = ("name", "run_id", "source", "lines", "checklist", "instrument")
_OPTIONAL_ENTRY_KEYS = ("recorded_at",)


class ScrubError(RuntimeError):
    """Raised when scrubbing cannot proceed safely; never holds an original value."""


@dataclass(frozen=True)
class Leak:
    path: Path
    line: int  # 1-based
    kind: str  # Category of the original found, never the value itself


class LeakError(ScrubError):
    """Raised by export_fixtures when its scrubbed output still holds an original value."""

    def __init__(self, leaks: Sequence[Leak], staging_dir: Path) -> None:
        super().__init__(f"{len(leaks)} original values remain in the scrubbed output staged at {staging_dir}")
        self.leaks = tuple(leaks)
        self.staging_dir = staging_dir


@dataclass(frozen=True)
class FixtureEntry:
    """One [[fixture]] table of fixtures.toml."""

    index: int  # Position in fixtures.toml, which names the entry in errors raised before its name is checked
    name: str
    run_id: str
    source: str
    lines: tuple[int, ...]
    checklist: tuple[str, ...]
    instrument: str
    recorded_at_ns: int | None  # Given explicitly, for a source whose first selected line has no timestamp


def _json_escapes(value: str) -> set[str]:
    """Return value as it appears inside a JSON string, with and without the optional escapes."""
    forms: set[str] = set()
    for ensure_ascii in (False, True):
        body = json.dumps(value, ensure_ascii=ensure_ascii)[1:-1]
        forms.add(body)
        forms.add(body.replace("/", "\\/"))
    return forms


@functools.cache
def _text_forms(value: str) -> frozenset[str]:
    """Return every textual form value can take in recorded files and fixtures.

    That is the value itself or percent-encoded, each also JSON-escaped
    once or twice.
    """
    plain = {value, quote(value, safe=""), quote(value)}
    once = {form for item in plain for form in _json_escapes(item)}
    twice = {form for item in once for form in _json_escapes(item)}
    return frozenset(plain | once | twice)


@functools.cache
def _byte_forms(value: str) -> frozenset[bytes]:
    """Return value in each legacy encoding whose bytes differ from UTF-8."""
    utf8 = value.encode("utf-8")
    forms: set[bytes] = set()
    for encoding in _LEGACY_ENCODINGS:
        try:
            encoded = value.encode(encoding)
        except UnicodeEncodeError:
            continue
        if encoded != utf8:
            forms.add(encoded)
    return frozenset(forms)


def _longest_first(forms: Iterable[str]) -> list[str]:
    return sorted(forms, key=lambda form: (-len(form), form))


@functools.cache
def _original_pattern(original: str) -> re.Pattern[str]:
    """Return a pattern matching any textual form of original, ignoring case."""
    return re.compile("|".join(map(re.escape, _longest_first(_text_forms(original)))), re.IGNORECASE)


class _Replacer:
    """Replace every form of every original with its placeholder.

    Originals are replaced one at a time from longest to shortest, each
    with all its forms, so where two originals overlap the longer one is
    replaced whole and only the shorter one can lose text to it. Each is
    first replaced by a private-use marker character that no original
    contains, so a later, shorter original can never match inside or
    across an inserted placeholder; the markers become placeholders last.
    """

    def __init__(self, mapping: Mapping[str, str]) -> None:
        originals = sorted((original for original in mapping if original), key=lambda o: (-len(o), o))
        if len(originals) > _MARKER_LAST - _MARKER_FIRST + 1:
            raise ScrubError("more originals than private-use marker characters")
        if any(_MARKER_RE.search(original) for original in originals):
            raise ScrubError("an original contains a private-use character reserved as a marker")
        markers = [chr(_MARKER_FIRST + index) for index in range(len(originals))]
        self._text_steps = [(_original_pattern(o), marker) for o, marker in zip(originals, markers, strict=True)]
        self._placeholders = {marker: mapping[o] for o, marker in zip(originals, markers, strict=True)}

        # Backslashes doubled, since re.sub reads its replacement as a template
        self._byte_steps = [
            (
                re.compile(b"|".join(map(re.escape, sorted(forms, key=lambda f: (-len(f), f))))),
                mapping[o].encode("utf-8").replace(b"\\", b"\\\\"),
            )
            for o in originals
            if (forms := _byte_forms(o))
        ]

    def scrub_str(self, text: str) -> str:
        if not self._text_steps:
            return text
        if _MARKER_RE.search(text):
            raise ScrubError("text to scrub contains a private-use character reserved as a marker")
        for pattern, marker in self._text_steps:
            text = pattern.sub(marker, text)
        placeholders = self._placeholders
        return _MARKER_RE.sub(lambda match: placeholders[match.group(0)], text)

    def scrub_bytes(self, data: bytes) -> bytes:
        # surrogateescape keeps bytes that are not UTF-8 exactly as they were
        scrubbed = self.scrub_str(data.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")
        for pattern, placeholder in self._byte_steps:
            scrubbed = pattern.sub(placeholder, scrubbed)
        return scrubbed


@functools.cache
def _replacer_for(items: tuple[tuple[str, str], ...]) -> _Replacer:
    return _Replacer(dict(items))


def _replacer(mapping: Mapping[str, str]) -> _Replacer:
    return _replacer_for(tuple(sorted(mapping.items())))


def scrub_text(text: str, mapping: Mapping[str, str]) -> str:
    """Replace every form of every original in text with its placeholder.

    mapping is original -> placeholder, as returned by build_map. Longer
    originals are replaced first, so a full API key becomes one placeholder
    rather than its prefix's placeholder plus a visible remainder, and a
    value embedded in a longer string, such as the key prefix inside a
    clientname, is replaced too.
    """
    return _replacer(mapping).scrub_str(text)


class _Detector:
    """Report which originals occur in a line, in any form, including inside base64 fields.

    It also reports, under its key's category, the non-empty value of every
    JSON member named in keys that is not a placeholder, at any escape
    depth, since such a value may be an identifier no known original names.
    """

    def __init__(self, originals: Mapping[str, str], keys: Mapping[str, str]) -> None:
        self._checks: list[tuple[str, re.Pattern[str], frozenset[bytes]]] = []
        for original, label in originals.items():
            if not original:
                continue
            self._checks.append((category_of(label), _original_pattern(original), _byte_forms(original)))
        self._key_categories = dict(keys)
        self._member_re = _member_pattern(keys)

    def kinds_in(self, data: bytes) -> set[str]:
        text = data.decode("utf-8", "surrogateescape")
        found = {
            kind
            for kind, pattern, byte_forms in self._checks
            if pattern.search(text) or any(form in data for form in byte_forms)
        }
        for match in self._member_re.finditer(text):
            value = match.group(3)
            if value and not _is_placeholder(value):
                found.add(self._key_categories[match.group(2)])

        # Decoded content is shorter than its base64 text, so the recursion ends
        for match in _B64_FIELD_RE.finditer(text):
            try:
                decoded = base64.b64decode(match.group(1).replace("\\/", "/"), validate=True)
            except (binascii.Error, ValueError):
                continue
            found |= self.kinds_in(decoded)
        return found


def _member_pattern(keys: Iterable[str]) -> re.Pattern[str]:
    """Match a JSON string member named in keys at any escape depth, capturing the backslashes, key and value."""
    names = "|".join(map(re.escape, sorted(keys)))
    return re.compile(rf'(\\*)"({names})\1"\s*:\s*\1"(.*?)\1"')


def _is_placeholder(value: str) -> bool:
    return value == _CA_CATEGORY or _PLACEHOLDER_RE.fullmatch(value) is not None


def _iter_files(paths: Iterable[Path]) -> Iterator[Path]:
    """Yield each file named in paths, or found under a directory in it, once."""
    seen: set[Path] = set()
    for path in paths:
        if path.is_dir():
            candidates = sorted(candidate for candidate in path.rglob("*") if candidate.is_file())
        elif path.is_file():
            candidates = [path]
        else:
            raise FileNotFoundError(f"scan path does not exist: {path}")
        for candidate in candidates:
            key = candidate.resolve()
            if key not in seen:
                seen.add(key)
                yield candidate


def scan(
    paths: Iterable[Path], originals: Mapping[str, str], keys: Mapping[str, str] | None = None
) -> list[Leak]:
    """Return one Leak per line and category where an original occurs in paths.

    originals maps each original to its category or to a <CATEGORY>_<n>
    placeholder, so build_map's result can be passed as is; a Leak names
    the file, line and category, never the value. A non-empty "ca" member
    that is not a placeholder is reported as CA_SIGNATURE, and so is, under
    its own category, a member named in keys (key -> category). Directories
    are scanned recursively, and a path that does not exist raises
    FileNotFoundError rather than passing unscanned.
    """
    detector = _Detector(originals, {_CA_KEY: _CA_CATEGORY, **(keys or {})})
    leaks: list[Leak] = []
    for file in _iter_files(paths):
        with file.open("rb") as fh:
            for number, line in enumerate(fh, start=1):
                for kind in sorted(detector.kinds_in(line)):
                    leaks.append(Leak(path=file, line=number, kind=kind))
    return leaks


def category_of(placeholder: str) -> str:
    """Return the category part of a <CATEGORY>_<n> placeholder, or the placeholder itself."""
    match = _PLACEHOLDER_RE.fullmatch(placeholder)
    return match.group(1) if match else placeholder


def categories(mapping: Mapping[str, str]) -> dict[str, str]:
    """Turn an original -> placeholder map into the original -> category map scan takes."""
    return {original: category_of(placeholder) for original, placeholder in mapping.items() if original}


def pointer_category(pointer: str) -> str:
    """Return the placeholder category of the values a scrub pointer finds, its last token upper-cased."""
    return pointer.rsplit("/", 1)[1].upper()


def build_map(
    secrets: Mapping[str, str], discovered: Mapping[str, Sequence[str]], existing: Mapping[str, str]
) -> dict[str, str]:
    """Return the original -> placeholder map, extending existing without renumbering it.

    secrets is role -> value, as read_secrets returns, and adds the API
    key's clientname prefix under API_KEY_PREFIX. discovered is category ->
    values in order of first appearance. A value already mapped, whether in
    existing or from an earlier source, keeps its placeholder, and each new
    value takes the next free number in its category. Raises ScrubError if
    a placeholder is shared or would itself contain an original.
    """
    mapping = {original: placeholder for original, placeholder in existing.items() if original}
    next_number: dict[str, int] = {}
    for placeholder in mapping.values():
        match = _PLACEHOLDER_RE.fullmatch(placeholder)
        if match:
            category, number = match.group(1), int(match.group(2))
            next_number[category] = max(next_number.get(category, 1), number + 1)

    def claim(value: str, category: str) -> None:
        if not _CATEGORY_RE.fullmatch(category):
            raise ScrubError(f"invalid placeholder category {category!r}")
        if not value or value in mapping:
            return
        number = next_number.get(category, 1)
        mapping[value] = f"{category}_{number}"
        next_number[category] = number + 1

    for category, values in discovered.items():
        for value in values:
            claim(value, category)

    roles = [role for role in _ROLE_ORDER if role in secrets]
    roles += sorted(role for role in secrets if role not in _ROLE_ORDER)
    for role in roles:
        claim(secrets[role], role.upper())
    held = set(secrets.values())
    for value in redaction_values(secrets):
        if value not in held:
            claim(value, _DERIVED_CATEGORY)

    _check_placeholders(mapping)
    return mapping


def _check_placeholders(mapping: Mapping[str, str]) -> None:
    """Raise ScrubError unless every placeholder is distinct and free of every original."""
    placeholders = list(mapping.values())
    if "" in placeholders or len(set(placeholders)) != len(placeholders):
        raise ScrubError("the scrub map must give every original its own non-empty placeholder")
    _check_contained(mapping)


def _check_contained(mapping: Mapping[str, str]) -> None:
    """Raise ScrubError if any placeholder contains an original, in any of its forms."""
    placeholders = set(mapping.values())
    for original, own_placeholder in mapping.items():
        if not original:
            continue
        pattern = _original_pattern(original)
        if any(pattern.search(placeholder) for placeholder in placeholders):
            # Any placeholder named here could itself contain the value, so
            # only its category is named, and only when that does not.
            kind = category_of(own_placeholder)
            label = f"a {kind} value" if not pattern.search(kind) else "an original value"
            raise ScrubError(f"{label} occurs inside a placeholder, so it is too short or generic to scrub safely")


def load_map(path: Path) -> dict[str, str]:
    """Load the original -> placeholder map, or return {} if it does not exist yet."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScrubError(f"cannot read scrub map {path}: {type(exc).__name__}") from None
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ScrubError(f"scrub map {path} must be a JSON object of strings")
    return data


def _placeholder_sort_key(placeholder: str) -> tuple[str, int]:
    match = _PLACEHOLDER_RE.fullmatch(placeholder)
    return (match.group(1), int(match.group(2))) if match else (placeholder, 0)


def save_map(path: Path, mapping: Mapping[str, str]) -> None:
    """Write the map readable by its owner only, replacing any previous file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = dict(sorted(mapping.items(), key=lambda item: _placeholder_sort_key(item[1])))
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        # A tmp file left over from an interrupted run keeps its old mode otherwise
        os.fchmod(fh.fileno(), 0o600)
        fh.write(json.dumps(ordered, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


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


def _response_json(record: Mapping[str, object]) -> object | None:
    """Parse a rest.jsonl record's response body as JSON, or return None."""
    body = record.get("resp_body")
    encoded = record.get("resp_body_b64")
    try:
        if isinstance(body, str):
            return json.loads(body)
        if isinstance(encoded, str):
            return json.loads(base64.b64decode(encoded, validate=True))
    except ValueError:
        return None
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


def _key_categories(cfg: P1Config) -> dict[str, str]:
    """Return each of scrub.account_id_keys with its category, the key upper-cased."""
    return {key: key.upper() for key in cfg.scrub.account_id_keys}


def _record_documents(record: Mapping[str, object]) -> Iterator[object]:
    """Yield each JSON document a recorded line carries: its request and response bodies, or its frame's data."""
    texts = [record.get("req_body"), record.get("resp_body")]
    raw = record.get("raw")
    if isinstance(raw, str):
        texts.append(parse_frame(raw).data)
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
    """Return category -> identifiers found in every recorded REST exchange.

    The scrub pointers apply to accounts and ca_status responses, each to
    every element of a response that is a JSON array, and the
    scrub.account_id_keys to every request and response body at any depth.
    Runs are read in name order, which is chronological. Logs a warning for
    an operation with no recorded response, or a pointer that found
    nothing, but never logs a value found.
    """
    pointers_by_op = {_ACCOUNTS_OP: cfg.scrub.account_id_pointers, _CA_STATUS_OP: cfg.scrub.ca_pointers}
    keys = _key_categories(cfg)
    responses = dict.fromkeys(pointers_by_op, 0)
    hits = {(op, pointer): 0 for op, pointers in pointers_by_op.items() for pointer in pointers}
    found: dict[str, list[str]] = {}

    # A key inside a body shows in the JSONL line as \"key\", since the body is itself a JSON string
    markers = [op.encode("utf-8") for op in pointers_by_op]
    markers += [b'\\"' + key.encode("utf-8") + b'\\"' for key in keys]
    for rest_log in sorted(cfg.results_root.glob("*/rest.jsonl")):
        for record in _iter_jsonl_records(rest_log, markers):
            for body in _record_documents(record):
                _collect_key_values(body, keys, found)
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


def _plain_name(value: str) -> bool:
    """Return True if value is usable as a single file or directory name."""
    return (
        bool(value)
        and not value.startswith(".")
        and value.isprintable()
        and not any(separator in value for separator in ("/", "\\"))
    )


def _explicit_recorded_at(label: str, value: object) -> int:
    """Return an entry's explicit recorded_at, a TOML or ISO-8601 date-time with a UTC offset, as ns."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            value = None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ConfigError(f"{label}: recorded_at must be a date-time with a UTC offset")
    return _datetime_ns(value)


def _parse_entry(selection: Path, index: int, raw: object) -> FixtureEntry:
    # Named by index alone: a value checked only later, such as the name, may hold an original
    label = f"{selection}: fixture {index}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{label} must be a table")

    unknown = sorted(set(raw) - set(_ENTRY_KEYS) - set(_OPTIONAL_ENTRY_KEYS))
    if unknown:
        raise ConfigError(f"{label}: unknown keys {', '.join(unknown)}")
    missing = [key for key in _ENTRY_KEYS if key not in raw]
    if missing:
        raise ConfigError(f"{label}: missing keys {', '.join(missing)}")

    for key in ("name", "run_id", "source"):
        value = raw[key]
        if not isinstance(value, str) or not _plain_name(value):
            raise ConfigError(f"{label}: {key} must be a single file name, without path separators")
    lines = raw["lines"]
    if (
        not isinstance(lines, list)
        or not lines
        or not all(isinstance(n, int) and not isinstance(n, bool) and n >= 1 for n in lines)
        or len(set(lines)) != len(lines)
    ):
        raise ConfigError(f"{label}: lines must be a non-empty array of distinct 1-based line numbers")
    checklist = raw["checklist"]
    if not isinstance(checklist, list) or not checklist or not all(isinstance(i, str) and i for i in checklist):
        raise ConfigError(f"{label}: checklist must be a non-empty array of checklist item names")
    if not isinstance(raw["instrument"], str):
        raise ConfigError(f"{label}: instrument must be a string")

    return FixtureEntry(
        index=index,
        name=raw["name"],
        run_id=raw["run_id"],
        source=raw["source"],
        lines=tuple(lines),
        checklist=tuple(checklist),
        instrument=raw["instrument"],
        recorded_at_ns=_explicit_recorded_at(label, raw["recorded_at"]) if "recorded_at" in raw else None,
    )


def load_selection(path: Path) -> list[FixtureEntry]:
    """Load fixtures.toml, whose [[fixture]] tables list what to export; none means an empty list."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read fixture selection {path} ({exc})") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    unknown = sorted(set(data) - {"fixture"})
    if unknown:
        raise ConfigError(f"{path}: unknown keys {', '.join(unknown)}; list fixtures as [[fixture]] tables")
    raw_entries = data.get("fixture", [])
    if not isinstance(raw_entries, list):
        raise ConfigError(f"{path}: fixture must be an array of tables")

    entries: list[FixtureEntry] = []
    first_index: dict[str, int] = {}
    for index, raw in enumerate(raw_entries):
        entry = _parse_entry(path, index, raw)
        if entry.name in first_index:
            raise ConfigError(f"{path}: fixture {index} repeats the name of fixture {first_index[entry.name]}")
        first_index[entry.name] = index
        entries.append(entry)
    return entries


def _check_entry_fields(selection: Path, entry: FixtureEntry, mapping: Mapping[str, str]) -> None:
    """Raise ConfigError, naming the entry by index only, if a field that goes public holds an original."""
    fields = {"name": entry.name, "run_id": entry.run_id, "source": entry.source, "instrument": entry.instrument}
    fields.update({f"checklist[{i}]": item for i, item in enumerate(entry.checklist)})
    for original in mapping:
        if not original:
            continue
        pattern = _original_pattern(original)
        for key, value in fields.items():
            if pattern.search(value):
                raise ConfigError(f"{selection}: fixture {entry.index}: {key} contains an original value")


def _select_lines(source_path: Path, entry: FixtureEntry) -> list[bytes]:
    """Return the entry's lines of source_path, in the entry's order, reading no further than needed."""
    if not source_path.is_file():
        raise ConfigError(f"fixture {entry.name!r}: source {source_path} does not exist")
    wanted = set(entry.lines)
    last = max(wanted)
    found: dict[int, bytes] = {}
    with source_path.open("rb") as fh:
        for number, line in enumerate(fh, start=1):
            if number in wanted:
                found[number] = line
            if number >= last:
                break
    missing = [number for number in entry.lines if number not in found]
    if missing:
        raise ConfigError(f"fixture {entry.name!r}: {entry.run_id}/{entry.source} has no line {missing[0]}")
    return [found[number] for number in entry.lines]


def _parse_record(entry: FixtureEntry, number: int, line: bytes) -> dict[str, Any]:
    try:
        record = json.loads(line)
    except ValueError:
        record = None
    if not isinstance(record, dict):
        raise ConfigError(f"fixture {entry.name!r}: line {number} of {entry.source} is not a JSON record")
    return record


def _scrub_b64(key: str, value: str, replacer: _Replacer) -> str:
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ScrubError(f"field {key} is not valid base64, so it cannot be scrubbed") from None
    return base64.b64encode(replacer.scrub_bytes(data)).decode("ascii")


def _scrub_record(record: Mapping[str, object], replacer: _Replacer) -> dict[str, object]:
    """Scrub every key, string and base64 field in a parsed record, leaving numbers alone.

    The numbers in a record are the recorder's own timestamps and status
    codes, and replacing digits inside them would corrupt the JSON.
    """
    return {
        replacer.scrub_str(key): (
            _scrub_b64(key, value, replacer)
            if key.endswith("_b64") and isinstance(value, str)
            else _scrub_json(value, replacer)
        )
        for key, value in record.items()
    }


def _scrub_json(value: object, replacer: _Replacer) -> object:
    if isinstance(value, str):
        return replacer.scrub_str(value)
    if isinstance(value, list):
        return [_scrub_json(item, replacer) for item in value]
    if isinstance(value, dict):
        return _scrub_record(value, replacer)
    return value


def _check_json_kept(before: object, after: object, where: str) -> None:
    """Raise ScrubError if scrubbing turned valid JSON text into invalid JSON."""
    if not isinstance(before, str) or not isinstance(after, str) or before == after:
        return
    try:
        json.loads(before)
    except ValueError:
        return
    try:
        json.loads(after)
    except ValueError:
        raise ScrubError(
            f"scrubbing broke the JSON in {where}: an original also occurs outside a JSON string, "
            "such as inside a number"
        ) from None


@dataclass(frozen=True)
class _Selection:
    """One entry's selected lines, and their parsed records for a REST or SSE source."""

    entry: FixtureEntry
    lines: list[bytes]
    records: list[dict[str, Any]]  # Empty for a log source


def _select(cfg: P1Config, entry: FixtureEntry) -> _Selection:
    """Read and parse one entry's selected lines, requiring frame records from an SSE source."""
    lines = _select_lines(cfg.results_root / entry.run_id / entry.source, entry)
    if entry.source != _REST_SOURCE and not _SSE_SOURCE_RE.fullmatch(entry.source):
        return _Selection(entry=entry, lines=lines, records=[])
    records = [_parse_record(entry, number, line) for number, line in zip(entry.lines, lines, strict=True)]
    if _SSE_SOURCE_RE.fullmatch(entry.source):
        for number, record in zip(entry.lines, records, strict=True):
            if record.get("kind") != "frame" or not isinstance(record.get("raw"), str):
                raise ConfigError(f"fixture {entry.name!r}: line {number} of {entry.source} is not a frame record")
    return _Selection(entry=entry, lines=lines, records=records)


def _export_rest(selection: _Selection, replacer: _Replacer) -> tuple[bytes, int | None]:
    """Return the selected REST records as a scrubbed, pretty-printed JSON array."""
    entry = selection.entry
    scrubbed_records = []
    for number, record in zip(entry.lines, selection.records, strict=True):
        scrubbed = _scrub_record(record, replacer)
        for key in ("req_body", "resp_body"):
            _check_json_kept(record.get(key), scrubbed.get(key), f"{key} of line {number} of {entry.source}")
        scrubbed_records.append(scrubbed)
    text = json.dumps(scrubbed_records, ensure_ascii=False, indent=2) + "\n"
    return text.encode("utf-8"), _record_ns(selection.records[0])


def _export_sse(selection: _Selection, replacer: _Replacer) -> tuple[bytes, int | None]:
    """Return the selected frames' exact bytes, scrubbed and concatenated into stream text."""
    entry = selection.entry
    frames: list[bytes] = []
    for number, record in zip(entry.lines, selection.records, strict=True):
        raw_b64 = record.get("raw_b64")
        data = _decode_b64_field(entry, number, raw_b64) if isinstance(raw_b64, str) else record["raw"].encode()
        scrubbed = replacer.scrub_bytes(data)
        _check_json_kept(
            parse_frame(data.decode("utf-8", "replace")).data,
            parse_frame(scrubbed.decode("utf-8", "replace")).data,
            f"the data of line {number} of {entry.source}",
        )
        frames.append(scrubbed)
    return b"".join(frames), _record_ns(selection.records[0])


def _decode_b64_field(entry: FixtureEntry, number: int, value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ScrubError(f"fixture {entry.name!r}: line {number} of {entry.source} has invalid base64") from None


def _export_selection(selection: _Selection, replacer: _Replacer) -> tuple[str, bytes, int | None]:
    """Return the output file name, scrubbed content and the first selected line's time, if it has one."""
    entry = selection.entry
    try:
        if entry.source == _REST_SOURCE:
            data, recorded_ns = _export_rest(selection, replacer)
            return f"{entry.name}.json", data, recorded_ns
        if _SSE_SOURCE_RE.fullmatch(entry.source):
            data, recorded_ns = _export_sse(selection, replacer)
            return f"{entry.name}.sse", data, recorded_ns
    except ScrubError as exc:
        raise ScrubError(f"fixture {entry.name!r}: {exc}") from None
    lines = selection.lines
    return f"{entry.name}.log", b"".join(replacer.scrub_bytes(line) for line in lines), _log_line_ns(lines[0])


def _datetime_ns(moment: datetime) -> int:
    """Return an aware datetime as nanoseconds since the Unix epoch, exactly."""
    return (moment - _EPOCH) // timedelta(microseconds=1) * _NS_PER_US


def _log_line_ns(line: bytes) -> int | None:
    """Return the time a log line starts with, after any colour codes, as ns, or None.

    A time without a UTC offset, as the recorder's own log writes, is local
    time, read in this host's time zone, since scrub runs on the host that
    recorded the log.
    """
    text = _ANSI_CSI_RE.sub("", line.decode("utf-8", "replace")).lstrip()
    match = _LOG_TIMESTAMP_RE.match(text)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(group) for group in match.groups()[:6])
    fraction, offset = match.group(7) or "", match.group(8)
    try:
        moment = datetime(year, month, day, hour, minute, second, tzinfo=_utc_offset(offset))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone(timezone.utc)
    return _datetime_ns(moment) + int(fraction.ljust(_NS_FRACTION_DIGITS, "0"))


def _utc_offset(text: str | None) -> timezone | None:
    """Parse Z, +hh:mm or +hhmm into a timezone, or None for no offset."""
    if text is None:
        return None
    if text == "Z":
        return timezone.utc
    sign = -1 if text[0] == "-" else 1
    digits = text[1:].replace(":", "")
    return timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))


def _record_ns(record: Mapping[str, object]) -> int | None:
    """Return when the recorder received a record's reply, or recorded its failure, as ns."""
    for key in ("recv_ts_ns", "error_ts_ns"):
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _recorded_at(entry: FixtureEntry, derived_ns: int | None) -> int:
    """Return the entry's recording time: from its first selected line, or else given explicitly."""
    if derived_ns is not None and entry.recorded_at_ns is not None:
        raise ConfigError(
            f"fixture {entry.name!r}: recorded_at is only for a source whose first selected line has no timestamp"
        )
    if derived_ns is not None:
        return derived_ns
    if entry.recorded_at_ns is not None:
        return entry.recorded_at_ns
    raise ConfigError(f"fixture {entry.name!r}: the first selected line has no timestamp, so give recorded_at")


def _iso_utc(ns: int) -> str:
    """Format nanoseconds since the Unix epoch as ISO-8601 UTC, keeping every digit."""
    seconds, fraction = divmod(ns, _NS_PER_S)
    return f"{datetime.fromtimestamp(seconds, tz=timezone.utc):%Y-%m-%dT%H:%M:%S}.{fraction:0{_NS_FRACTION_DIGITS}d}Z"


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        # JSON string escapes are all valid TOML ones, except that TOML also forbids a raw DEL
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to MANIFEST.toml")


def _render_manifest(entries: Sequence[Mapping[str, object]]) -> str:
    out = ["# Written by `p1rec scrub` from fixtures.toml, one [[fixture]] per exported file.", ""]
    for entry in entries:
        out.append("[[fixture]]")
        out.extend(f"{key} = {_toml_value(value)}" for key, value in entry.items())
        out.append("")
    return "\n".join(out)


def _extend_for_selections(
    cfg: P1Config, mapping: Mapping[str, str], selections: Sequence[_Selection]
) -> dict[str, str]:
    """Return mapping extended with what the selected records alone reveal.

    A scrub.account_id_keys value found only there, such as a subaccount
    seen on the order stream alone, gets the next placeholder of its
    category and is saved to the scrub map, so a later scan knows it. Each
    non-empty "ca" signature maps to CA_SIGNATURE for this export only.
    """
    keys = {**_key_categories(cfg), _CA_KEY: _CA_CATEGORY}
    found: dict[str, list[str]] = {}
    for selection in selections:
        for record in selection.records:
            for document in _record_documents(record):
                _collect_key_values(document, keys, found)
    signatures = found.pop(_CA_CATEGORY, [])

    extended = build_map({}, found, mapping)
    if extended != dict(mapping):
        save_map(cfg.scrub.map_path, extended)
    effective = {**dict.fromkeys(signatures, _CA_CATEGORY), **extended}
    _check_contained(effective)
    return effective


def export_fixtures(cfg: P1Config, mapping: Mapping[str, str]) -> list[Path]:
    """Scrub the lines fixtures.toml selects from results_root and write them to fixtures_root.

    A rest.jsonl source becomes <name>.json, the records as a JSON array;
    an sse_*.jsonl source becomes <name>.sse, its frames' raw text; any
    other file becomes <name>.log, its lines. MANIFEST.toml records each
    file's run_id, source, lines, recording time, instrument and checklist
    items; the recording time comes from the first selected line (its
    recv_ts_ns or error_ts_ns, or the time a log line starts with), or
    else from the entry's explicit recorded_at, never from file times.
    Besides the originals in mapping, every non-empty "ca" signature in
    the selected JSON becomes CA_SIGNATURE, and a scrub.account_id_keys
    value found only there is added to the saved scrub map first.
    Every entry is validated, and the output staged and scanned under
    results_root, before anything reaches fixtures_root: an invalid entry,
    or one with an original in a field that goes public, raises
    ConfigError, and output that still holds an original raises
    LeakError, leaving fixtures_root untouched and the staged copy in
    place for inspection. Returns the written paths, MANIFEST.toml last.
    """
    entries = load_selection(cfg.scrub.selection)
    for entry in entries:
        _check_entry_fields(cfg.scrub.selection, entry, mapping)
    selections = [_select(cfg, entry) for entry in entries]
    effective = _extend_for_selections(cfg, mapping, selections)
    replacer = _replacer(effective)

    outputs: dict[str, bytes] = {}
    manifest: list[dict[str, object]] = []
    for selection in selections:
        entry = selection.entry
        filename, data, derived_ns = _export_selection(selection, replacer)
        outputs[filename] = data
        manifest.append(
            {
                "file": filename,
                "run_id": entry.run_id,
                "source": entry.source,
                "lines": list(entry.lines),
                "recorded_at": _iso_utc(_recorded_at(entry, derived_ns)),
                "instrument": entry.instrument,
                "checklist": list(entry.checklist),
            }
        )
    outputs[_MANIFEST_NAME] = _render_manifest(manifest).encode("utf-8")

    cfg.results_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="scrub-staging-", dir=cfg.results_root))
    staged = []
    for filename, data in outputs.items():
        (staging / filename).write_bytes(data)
        staged.append(staging / filename)
    leaks = scan(staged, effective, _key_categories(cfg))
    if leaks:
        raise LeakError(leaks, staging)

    cfg.fixtures_root.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, data in outputs.items():
        (cfg.fixtures_root / filename).write_bytes(data)
        written.append(cfg.fixtures_root / filename)
    shutil.rmtree(staging)
    return written


def _missing_categories(cfg: P1Config, mapping: Mapping[str, str]) -> list[str]:
    """Return the pointer categories no known original belongs to."""
    wanted = {pointer_category(pointer) for pointer in (*cfg.scrub.account_id_pointers, *cfg.scrub.ca_pointers)}
    known = {category_of(placeholder) for placeholder in mapping.values()}
    return sorted(wanted - known)


def _scan_targets(cfg: P1Config) -> list[Path]:
    """Return what scrub --scan-only checks: fixtures_root's parent, contract_doc and extra_scan_paths.

    Raises ConfigError if any of them is missing, since a mistyped path
    would otherwise pass unscanned, or if one overlaps results_root, whose
    recordings hold the originals.
    """
    named = [
        ("fixtures_root's parent", cfg.fixtures_root.parent),
        ("scrub.contract_doc", cfg.scrub.contract_doc),
        *(("scrub.extra_scan_paths", extra) for extra in cfg.scrub.extra_scan_paths),
    ]
    targets: list[Path] = []
    for label, path in named:
        if not path.exists():
            raise ConfigError(f"{label} names a path that does not exist: {path}")
        targets.append(path)

    results_root = cfg.results_root.resolve()
    for target in targets:
        resolved = target.resolve()
        if results_root.is_relative_to(resolved) or resolved.is_relative_to(results_root):
            raise ConfigError(f"scan target {target} overlaps results_root {results_root}")
    return targets


def run_scrub(cfg: P1Config, *, scan_only: bool) -> int:
    """Export the selected fixtures, or with scan_only scan the committed ones, and return the exit code.

    Both first extend and save the scrub map with the current secrets and
    newly discovered identifiers. Returns 0 when no original was found, 2
    when one was (each reported as path:line:category, never the value),
    and 1 on a configuration or scrubbing error, including when a category
    the scrub pointers name has no known value, since its identifiers
    could then pass unscrubbed.
    """
    try:
        mapping = build_map(read_secrets(cfg), discover(cfg), load_map(cfg.scrub.map_path))
        save_map(cfg.scrub.map_path, mapping)
        missing = _missing_categories(cfg, mapping)
        if missing:
            logger.error(
                "no %s value is known, so they cannot be scrubbed; record an accounts and a ca_status "
                "response first",
                ", ".join(missing),
            )
            return 1

        if scan_only:
            leaks = scan(_scan_targets(cfg), mapping, _key_categories(cfg))
        else:
            try:
                written = export_fixtures(cfg, mapping)
            except LeakError as exc:
                leaks = list(exc.leaks)
                logger.error(
                    "scrubbed output still holds original values; %s is untouched and the output is in %s",
                    cfg.fixtures_root,
                    exc.staging_dir,
                )
            else:
                leaks = []
                logger.info("exported %d files to %s", len(written), cfg.fixtures_root)
    except (ConfigError, ScrubError) as exc:
        logger.error("scrub failed: %s", exc)
        return 1

    for leak in leaks:
        logger.error("%s:%d:%s", leak.path, leak.line, leak.kind)
    if leaks:
        logger.error("found %d leaks", len(leaks))
        return 2
    logger.info("no original value found")
    return 0
