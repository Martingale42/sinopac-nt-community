"""Resolve RFC 6901 JSON pointers against an already-parsed JSON document.

Several p1rec subcommands read one value out of a JSON response by
pointer: the simulation-mode interlock, and contract, funds and event
field pointers, so the walk lives in one place instead of being
reimplemented per caller.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

# RFC 6901 array index: "0", or a nonzero digit followed by more digits.
# An explicit ASCII class, not \d, so a Unicode digit look-alike such as
# "²" (which str.isdigit() accepts but int() then rejects) never
# reaches int() at all.
_ARRAY_INDEX_RE = re.compile(r"^(0|[1-9][0-9]*)$")

# RFC 6901 pointer shape: zero or more "/"-prefixed segments, each made of
# any character except "~" and "/", or a "~" escape ("~0" or "~1"). This
# also rejects a malformed escape such as "~2" or a trailing "~", which a
# bare startswith("/") check would let through.
_POINTER_SYNTAX_RE = re.compile(r"^(/([^~/]|~[01])*)*$")


class JsonPointerError(LookupError):
    """Raised when a JSON pointer does not resolve against a document."""


def is_valid_pointer_syntax(pointer: str) -> bool:
    """Return whether pointer has the shape of a JSON pointer per RFC 6901.

    This checks shape only, not whether pointer resolves against any
    particular document; callers that need a stronger guarantee, such as
    rejecting the empty (whole-document) pointer, apply that themselves.
    """
    return _POINTER_SYNTAX_RE.fullmatch(pointer) is not None


def resolve_pointer(doc: object, pointer: str) -> object:
    """Resolve pointer against doc and return the value it names.

    pointer must satisfy is_valid_pointer_syntax; each segment between
    slashes is unescaped by replacing "~1" with "/" and then "~0" with
    "~", in that order, before use, per RFC 6901. Raises JsonPointerError,
    never ValueError, if any segment names a missing object key, or an
    array index that does not match ^(0|[1-9][0-9]*)$ or is out of range.
    """
    if pointer == "":
        return doc
    if not is_valid_pointer_syntax(pointer):
        raise JsonPointerError(f"invalid JSON pointer: {pointer!r}")

    node = doc
    for raw_token in pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping):
            if token not in node:
                raise JsonPointerError(f"JSON pointer {pointer!r} not found: no key {token!r}")
            node = node[token]
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            if not _ARRAY_INDEX_RE.fullmatch(token):
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
