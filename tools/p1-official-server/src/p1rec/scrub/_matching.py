"""Match originals in every form they take: replace them in text, and scan files for them."""

from __future__ import annotations

import functools
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from p1rec.scrub._common import CA_CATEGORY, CA_KEY, ScrubError, category_of, decode_b64, is_placeholder

# Shorter originals, such as a two-digit branch suffix or a stock code
# like 2330, occur by chance inside prices, timestamps and other codes, so
# they match only as whole tokens rather than anywhere; measured in UTF-8
# bytes so that a two-character Chinese name, distinctive and written
# without separators, still matches anywhere.
# calibrate: check against the lengths of the real values once recorded.
MIN_SUBSTRING_MATCH_LEN = 6

# Characters that continue a token around a short original, the ones
# placeholders are built from, so a short original never matches inside one.
_TOKEN_CHAR = "[0-9A-Za-z_]"

# A body or frame that is not valid UTF-8 is kept in a *_b64 field, and
# legacy Taiwanese systems encode Chinese text as Big5.
_LEGACY_ENCODINGS = ("cp950",)

# A JSON escape sequence, whose backslash becomes a run of them when the
# JSON holding it is itself nested in a JSON string.
_ESCAPE_SEQUENCE_RE = re.compile(r'\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])')

# A base64 field as a JSON member at any escape depth
_B64_FIELD_RE = re.compile(r'(?P<esc>\\*)"[A-Za-z0-9_]*_b64(?P=esc)"\s*:\s*(?P=esc)"(?P<value>.*?)(?P=esc)"')
UNDECODABLE_B64 = "UNDECODABLE_B64"

# Unicode's Supplementary Private Use Area-A, U+F0000 to U+FFFFD, which
# recorded text has no reason to contain, lends one marker character to
# each original while replacement runs.
_MARKER_FIRST = 0xF0000
_MARKER_LAST = 0xFFFFD
_MARKER_RE = re.compile(f"[{chr(_MARKER_FIRST)}-{chr(_MARKER_LAST)}]")


@dataclass(frozen=True)
class Leak:
    path: Path
    line: int  # 1-based, or 0 when the file or directory name itself holds the original
    kind: str  # Category of the original found, never the value itself


def is_short(value: str) -> bool:
    """Return True if value is shorter than MIN_SUBSTRING_MATCH_LEN, so it matches only as a whole token."""
    return len(value.encode("utf-8")) < MIN_SUBSTRING_MATCH_LEN


def _plain_bases(original: str) -> set[str]:
    """Return original as written, stripped of surrounding blanks, and NFKC-normalized, without empties."""
    normalized = unicodedata.normalize("NFKC", original)
    return {original, original.strip(), normalized, normalized.strip()} - {""}


def _literal_pattern(text: str) -> str:
    # A "/" may carry the optional JSON escape at any depth
    return "".join(r"\\*/" if char == "/" else re.escape(char) for char in text)


def _escaped_pattern(body: str) -> str:
    """Return a regex for a JSON-escaped string body at any depth: a run of backslashes for each one it has."""
    parts: list[str] = []
    position = 0
    for match in _ESCAPE_SEQUENCE_RE.finditer(body):
        parts.append(_literal_pattern(body[position : match.start()]))
        parts.append(r"\\+" + re.escape(match.group(0)[1:]))
        position = match.end()
    parts.append(_literal_pattern(body[position:]))
    return "".join(parts)


def _base_alternatives(base: str) -> list[str]:
    """Return regexes for base verbatim, percent-encoded and JSON-escaped, longest first."""
    alternatives: set[str] = set()
    for form in (base, quote(base, safe=""), quote(base)):
        alternatives.add(_literal_pattern(form))
        for ensure_ascii in (False, True):
            alternatives.add(_escaped_pattern(json.dumps(form, ensure_ascii=ensure_ascii)[1:-1]))
    return sorted(alternatives, key=lambda alternative: (-len(alternative), alternative))


@functools.cache
def original_pattern(original: str) -> re.Pattern[str]:
    """Return a pattern matching any textual form of original, ignoring case.

    A form of a base shorter than MIN_SUBSTRING_MATCH_LEN matches only
    where no token character adjoins it.
    """
    groups: list[str] = []
    for base in sorted(_plain_bases(original), key=lambda b: (-len(b), b)):
        body = "|".join(_base_alternatives(base))
        groups.append(f"(?<!{_TOKEN_CHAR})(?:{body})(?!{_TOKEN_CHAR})" if is_short(base) else f"(?:{body})")
    return re.compile("|".join(groups), re.IGNORECASE)


@functools.cache
def _byte_forms(value: str) -> frozenset[bytes]:
    """Return value in each legacy encoding whose bytes differ from UTF-8, unless value is short."""
    if is_short(value):
        return frozenset()
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


class Replacer:
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
        self._text_steps = [
            (original_pattern(o), marker, category_of(mapping[o]))
            for o, marker in zip(originals, markers, strict=True)
        ]
        self._placeholders = {marker: mapping[o] for o, marker in zip(originals, markers, strict=True)}

        # Backslashes doubled, since re.sub reads its replacement as a template
        self._byte_steps = [
            (
                re.compile(b"|".join(map(re.escape, sorted(forms, key=lambda f: (-len(f), f))))),
                mapping[o].encode("utf-8").replace(b"\\", b"\\\\"),
                category_of(mapping[o]),
            )
            for o in originals
            if (forms := _byte_forms(o))
        ]

    def scrub_str(self, text: str, counts: Counter[str] | None = None) -> str:
        """Return text scrubbed, adding the number of replacements per category to counts."""
        if not self._text_steps:
            return text
        if _MARKER_RE.search(text):
            raise ScrubError("text to scrub contains a private-use character reserved as a marker")
        for pattern, marker, category in self._text_steps:
            text, replaced = pattern.subn(marker, text)
            if counts is not None and replaced:
                counts[category] += replaced
        placeholders = self._placeholders
        return _MARKER_RE.sub(lambda match: placeholders[match.group(0)], text)

    def scrub_bytes(self, data: bytes, counts: Counter[str] | None = None) -> bytes:
        """Return data scrubbed, keeping bytes that are not UTF-8 exactly as they were."""
        scrubbed = self.scrub_str(data.decode("utf-8", "surrogateescape"), counts).encode("utf-8", "surrogateescape")
        for pattern, placeholder, category in self._byte_steps:
            scrubbed, replaced = pattern.subn(placeholder, scrubbed)
            if counts is not None and replaced:
                counts[category] += replaced
        return scrubbed


@functools.cache
def _replacer_for(items: tuple[tuple[str, str], ...]) -> Replacer:
    return Replacer(dict(items))


def cached_replacer(mapping: Mapping[str, str]) -> Replacer:
    return _replacer_for(tuple(sorted(mapping.items())))


def scrub_text(text: str, mapping: Mapping[str, str]) -> str:
    """Replace every form of every original in text with its placeholder.

    mapping is original -> placeholder, as returned by build_map. Longer
    originals are replaced first, so a full API key becomes one placeholder
    rather than its prefix's placeholder plus a visible remainder, and a
    value embedded in a longer string, such as the key prefix inside a
    clientname, is replaced too, unless the value is shorter than
    MIN_SUBSTRING_MATCH_LEN.
    """
    return cached_replacer(mapping).scrub_str(text)


def _field_patterns(keys: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    """Return patterns capturing a string field named in keys as key and value.

    The field may be a JSON member at any escape depth, a Debug-printed
    field (key: "value" or key: Some("value")), or a key=value pair,
    quoted or bare.
    """
    names = "(?P<key>" + "|".join(map(re.escape, sorted(keys))) + ")"
    start = f"(?<!{_TOKEN_CHAR})"
    quoted = r'(?P<esc>\\*)"(?P<value>.*?)(?P=esc)"'
    return (
        re.compile(r'(?P<esc>\\*)"' + names + r'(?P=esc)"\s*:\s*(?P=esc)"(?P<value>.*?)(?P=esc)"'),
        re.compile(start + names + r"\s*:\s*(?:Some\()?" + quoted),
        re.compile(start + names + "=(?:" + quoted + r"|(?P<bare>[^\s\"',;(){}\[\]]*))"),
    )


class _Detector:
    """Report which originals occur in a line, in any form, including inside base64 fields.

    It also reports, under its key's category, the non-empty value of every
    field named in keys that is not a placeholder, since such a value may
    be an identifier no known original names, and reports a *_b64 field
    that does not decode as UNDECODABLE_B64, since its content cannot be
    checked.
    """

    def __init__(self, originals: Mapping[str, str], keys: Mapping[str, str]) -> None:
        self._checks: list[tuple[str, re.Pattern[str], frozenset[bytes]]] = []
        for original, label in originals.items():
            if not original:
                continue
            self._checks.append((category_of(label), original_pattern(original), _byte_forms(original)))
        self.key_categories = dict(keys)
        self._field_patterns = _field_patterns(keys)

    def kinds_in(self, data: bytes) -> set[str]:
        text = data.decode("utf-8", "surrogateescape")
        normalized = unicodedata.normalize("NFKC", text)
        texts = (text,) if normalized == text else (text, normalized)
        found = {
            kind
            for kind, pattern, byte_forms in self._checks
            if any(pattern.search(candidate) for candidate in texts) or any(form in data for form in byte_forms)
        }
        for candidate in texts:
            for pattern in self._field_patterns:
                for match in pattern.finditer(candidate):
                    fields = match.groupdict()
                    value = fields["value"] if fields["value"] is not None else fields.get("bare")
                    if value and not is_placeholder(value):
                        found.add(self.key_categories[fields["key"]])

        # Decoded content is shorter than its base64 text, so the recursion ends
        for match in _B64_FIELD_RE.finditer(text):
            decoded = decode_b64(match.group("value"))
            if decoded is None:
                found.add(UNDECODABLE_B64)
            else:
                found |= self.kinds_in(decoded)
        return found


def _iter_entries(paths: Iterable[Path]) -> Iterator[Path]:
    """Yield each file named in paths, and each file and directory under a directory in it, once."""
    seen: set[Path] = set()
    for path in paths:
        if path.is_dir():
            candidates = sorted(path.rglob("*"))
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
    the file, line and category, never the value. The name of every file
    and directory under a scanned directory, and of a scanned file, is
    checked too and reported as line 0. A non-empty "ca" field that is not
    a placeholder is reported as CA_SIGNATURE, and so is, under its own
    category, a field named in keys (key -> category). Directories are
    scanned recursively, and a path that does not exist raises
    FileNotFoundError rather than passing unscanned.
    """
    detector = _Detector(originals, {CA_KEY: CA_CATEGORY, **(keys or {})})
    leaks: list[Leak] = []
    for entry in _iter_entries(paths):
        for kind in sorted(detector.kinds_in(entry.name.encode("utf-8", "surrogateescape"))):
            leaks.append(Leak(path=entry, line=0, kind=kind))
        if not entry.is_file():
            continue
        with entry.open("rb") as fh:
            for number, line in enumerate(fh, start=1):
                for kind in sorted(detector.kinds_in(line)):
                    leaks.append(Leak(path=entry, line=number, kind=kind))
    return leaks
