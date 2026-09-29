"""Resolve RFC 6901 JSON pointers against an already-parsed JSON document.

Several p1rec subcommands read one value out of a JSON response by pointer
(the simulation-mode interlock today; contract, funds and event field
pointers reuse this module later), so the walk lives in one place instead
of being reimplemented per caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


class JsonPointerError(LookupError):
    """Raised when a JSON pointer does not resolve against a document."""


def resolve_pointer(doc: object, pointer: str) -> object:
    """Resolve pointer against doc and return the value it names.

    pointer must be "" (the whole document) or start with "/"; each
    segment between slashes is unescaped by replacing "~1" with "/" and
    then "~0" with "~", in that order, before use, per RFC 6901. Raises
    JsonPointerError if any segment names a missing object key, or a list
    index that is not a non-negative integer or is out of range.
    """
    if pointer == "":
        return doc
    if not pointer.startswith("/"):
        raise JsonPointerError(f"invalid JSON pointer: {pointer!r}")

    node = doc
    for raw_token in pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping):
            if token not in node:
                raise JsonPointerError(f"JSON pointer {pointer!r} not found: no key {token!r}")
            node = node[token]
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            if not token.isdigit():
                raise JsonPointerError(f"JSON pointer {pointer!r} not found: invalid array index {token!r}")
            index = int(token)
            if index >= len(node):
                raise JsonPointerError(f"JSON pointer {pointer!r} not found: array index {index} out of range")
            node = node[index]
        else:
            raise JsonPointerError(
                f"JSON pointer {pointer!r} not found: cannot descend into {type(node).__name__}"
            )
    return node
