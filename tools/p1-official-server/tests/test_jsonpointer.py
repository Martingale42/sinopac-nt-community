"""Tests for p1rec.jsonpointer: RFC 6901 pointer resolution and syntax checks."""

from __future__ import annotations

import pytest

from p1rec.jsonpointer import JsonPointerError, is_valid_pointer_syntax, resolve_pointer


def test_resolves_root_pointer() -> None:
    doc = {"a": 1}
    assert resolve_pointer(doc, "") is doc


def test_resolves_nested_object_and_array() -> None:
    doc = {"a": {"b": [10, 20, 30]}}
    assert resolve_pointer(doc, "/a/b/1") == 20


def test_missing_key_raises() -> None:
    with pytest.raises(JsonPointerError):
        resolve_pointer({}, "/simulation")


def test_leading_zero_array_index_rejected() -> None:
    with pytest.raises(JsonPointerError):
        resolve_pointer([1, 2], "/01")


def test_non_ascii_digit_array_index_rejected() -> None:
    # str.isdigit() accepts "²" but int() then rejects it; the pointer
    # walk must never let that raw ValueError escape as something other
    # than JsonPointerError.
    with pytest.raises(JsonPointerError):
        resolve_pointer([1, 2], "/²")


def test_dash_array_index_rejected() -> None:
    with pytest.raises(JsonPointerError):
        resolve_pointer([1, 2], "/-")


def test_out_of_range_array_index_rejected() -> None:
    with pytest.raises(JsonPointerError):
        resolve_pointer([1, 2], "/5")


def test_descending_into_scalar_raises() -> None:
    with pytest.raises(JsonPointerError):
        resolve_pointer({"a": 5}, "/a/b")


def test_tilde_escape_decoding_order() -> None:
    # "~01" must decode to "~1" (a literal tilde then "1"), not "/" (a
    # path separator). That only holds if "~1" is unescaped before "~0",
    # per RFC 6901's specified order: the wrong order would turn "~01"
    # into "/" and this pointer would resolve into a completely
    # different, nonexistent path instead of the "a~1" key.
    doc = {"a~1": "literal-tilde-one"}
    assert resolve_pointer(doc, "/a~01") == "literal-tilde-one"


def test_is_valid_pointer_syntax() -> None:
    assert is_valid_pointer_syntax("")
    assert is_valid_pointer_syntax("/simulation")
    assert is_valid_pointer_syntax("/a~0b/c~1d")
    assert not is_valid_pointer_syntax("simulation")


def test_is_valid_pointer_syntax_rejects_bad_escapes() -> None:
    assert not is_valid_pointer_syntax("/a~2b")
    assert not is_valid_pointer_syntax("/a~")
