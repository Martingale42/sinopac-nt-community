"""Build the original -> placeholder map, and keep it on disk where only its owner can read it."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from p1rec.redact import redaction_values
from p1rec.scrub._common import CA_CATEGORY, PLACEHOLDER_RE, ScrubError, category_of
from p1rec.scrub._matching import MIN_SUBSTRING_MATCH_LEN, is_short, original_pattern

# Secret roles in the order they claim placeholders, identities before
# credentials, so a credential equal to an identity value, such as a CA
# password set to the national ID, is labelled as the identity and its
# placeholder does not reveal that it is also a credential.
_ROLE_ORDER = ("person_id", "egress_ip", "api_key", "sec_key", "ca_passwd")

# Category of a value redaction_values derives from a secret instead of
# returning as is, which is the API key's clientname prefix.
_DERIVED_CATEGORY = "API_KEY_PREFIX"

# A category never ends in _<digits>, so <CATEGORY>_<n> parses back unambiguously
_CATEGORY_RE = re.compile(r"(?![A-Z0-9_]*_[0-9]+$)[A-Z][A-Z0-9_]*")

# Values of these categories are never that short, so a short one means a
# pointer or key selects the wrong field, and matching it only as a token
# would hide that; a short subaccount or CA password is legitimate.
_IDENTITY_CATEGORIES = frozenset(
    {"API_KEY", "API_KEY_PREFIX", "SEC_KEY", "PERSON_ID", "EGRESS_IP", "ACCOUNT_ID", "USERNAME", CA_CATEGORY}
)


def categories(mapping: Mapping[str, str]) -> dict[str, str]:
    """Turn an original -> placeholder map into the original -> category map scan takes."""
    return {original: category_of(placeholder) for original, placeholder in mapping.items() if original}


def build_map(
    secrets: Mapping[str, Sequence[str]], discovered: Mapping[str, Sequence[str]], existing: Mapping[str, str]
) -> dict[str, str]:
    """Return the original -> placeholder map, extending existing without renumbering it.

    secrets is role -> values, as read_secrets returns, each value claimed
    under the role's category, such as every egress IP under EGRESS_IP, and
    adds the API key's clientname prefix under API_KEY_PREFIX. discovered is
    category -> values in order of first appearance. A value already mapped,
    whether in existing or from an earlier source, keeps its placeholder,
    and each new value takes the next free number in its category. Raises
    ScrubError if a placeholder is shared or would itself contain an
    original, or if an identity value, such as an account ID, is shorter
    than MIN_SUBSTRING_MATCH_LEN.
    """
    mapping = {original: placeholder for original, placeholder in existing.items() if original}
    next_number: dict[str, int] = {}
    for placeholder in mapping.values():
        match = PLACEHOLDER_RE.fullmatch(placeholder)
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
        for value in secrets[role]:
            claim(value, role.upper())
    held = {value for values in secrets.values() for value in values}
    for value in redaction_values(secrets):
        if value not in held:
            claim(value, _DERIVED_CATEGORY)

    _check_placeholders(mapping)
    for original, placeholder in mapping.items():
        category = category_of(placeholder)
        if category in _IDENTITY_CATEGORIES and is_short(original):
            raise ScrubError(
                f"a {category} value is shorter than {MIN_SUBSTRING_MATCH_LEN} UTF-8 bytes, "
                "so a pointer or key probably selects the wrong field"
            )
    return mapping


def _check_placeholders(mapping: Mapping[str, str]) -> None:
    """Raise ScrubError unless every placeholder is distinct and free of every original.

    A short original matches only as a whole token, which no placeholder
    can hold, so it is never checked here.
    """
    placeholders = list(mapping.values())
    if "" in placeholders or len(set(placeholders)) != len(placeholders):
        raise ScrubError("the scrub map must give every original its own non-empty placeholder")
    for original, own_placeholder in mapping.items():
        if not original or is_short(original):
            continue
        pattern = original_pattern(original)
        if any(pattern.search(placeholder) for placeholder in placeholders):
            # Any placeholder named here could itself contain the value, so
            # only its category is named, and only when that does not.
            kind = category_of(own_placeholder)
            label = f"a {kind} value" if not pattern.search(kind) else "an original value"
            raise ScrubError(f"{label} occurs inside a placeholder, so it is too generic to scrub safely")


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
    match = PLACEHOLDER_RE.fullmatch(placeholder)
    return (match.group(1), int(match.group(2))) if match else (placeholder, 0)


def save_map(path: Path, mapping: Mapping[str, str]) -> None:
    """Write the map readable by its owner only, replacing any previous file atomically and durably.

    mkstemp creates the temporary file exclusively and with mode 0600, so
    nothing planted at its name, such as a symlink, can redirect the write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = dict(sorted(mapping.items(), key=lambda item: _placeholder_sort_key(item[1])))
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(ordered, ensure_ascii=False, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
