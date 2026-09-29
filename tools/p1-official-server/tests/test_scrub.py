"""Tests for p1rec.scrub: placeholders, replacement, fixture export and leak scanning.

Every identifier here is fake; recordings are written with the recorder's
own append_jsonl so the escaping matches what the real files hold.
"""

from __future__ import annotations

import base64
import json
import stat
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pytest
from helpers import MINIMAL_CONFIG_LINES, WriteConfig

from p1rec import scrub
from p1rec.capture import append_jsonl
from p1rec.cli import main
from p1rec.config import ConfigError, P1Config, load_config
from p1rec.redact import API_KEY_PREFIX_LEN
from p1rec.scrub import (
    Leak,
    ScrubError,
    build_map,
    categories,
    discover,
    export_fixtures,
    load_map,
    scan,
    scrub_text,
)

API_KEY = "FakeApiKey0123456789abcdefXYZ"
API_KEY_PREFIX = API_KEY[:API_KEY_PREFIX_LEN]
SEC_KEY = "FakeSecKey9876543210zyxwv"
CA_PASSWD = "fake-ca-passwd-q7"
PERSON_ID = "Z900000001"
EGRESS_IP = "203.0.113.77"
ACCOUNT_S = "0977001"
ACCOUNT_F = "0977002"
USERNAME = "測試甲"
SUBACCOUNT_REST = "S0977009"
SUBACCOUNT_SSE = "S0977010"

# A CA signature whose DER embeds the holder's name and national ID, which base64 hides from string matching
FAKE_CA = base64.b64encode(b"0\x82FAKE-CERT CN=" + USERNAME.encode() + b" SERIALNUMBER=" + PERSON_ID.encode()).decode()

SECRET_ENV = {
    "SJ_API_KEY": API_KEY,
    "SJ_SEC_KEY": SEC_KEY,
    "SJ_CA_PASSWD": CA_PASSWD,
    "P1_PERSON_ID": PERSON_ID,
    "P1_EGRESS_IP": EGRESS_IP,
}
SECRETS = {
    "api_key": API_KEY,
    "sec_key": SEC_KEY,
    "ca_passwd": CA_PASSWD,
    "person_id": PERSON_ID,
    "egress_ip": EGRESS_IP,
}
ORIGINALS = (
    API_KEY,
    API_KEY_PREFIX,
    SEC_KEY,
    CA_PASSWD,
    PERSON_ID,
    EGRESS_IP,
    ACCOUNT_S,
    ACCOUNT_F,
    USERNAME,
    SUBACCOUNT_REST,
    SUBACCOUNT_SSE,
    FAKE_CA,
)

PREFLIGHT_RUN = "20261001T000000Z-preflight"
STREAM_RUN = "20261001T000100Z-stream"
RECONCILE_RUN = "20261001T000200Z-reconcile"
TS = 1_790_000_000_123_456_789
TS_ISO = "2026-09-21T14:13:20.123456789Z"

ACCOUNT_OBJECT_S = {
    "account_type": "S",
    "broker_id": "FAKE",
    "account_id": ACCOUNT_S,
    "person_id": PERSON_ID,
    "username": USERNAME,
    "signed": True,
}
ACCOUNT_OBJECT_F = {**ACCOUNT_OBJECT_S, "account_type": "F", "account_id": ACCOUNT_F, "signed": False}

# Official server log lines, coloured for a terminal, and a recorder.log line in local time
SERVER_LOG_LINES = [
    "\x1b[2m2026-10-01T00:00:00.100000Z\x1b[0m \x1b[32m INFO\x1b[0m server listening\n",
    "\x1b[2m2026-10-01T00:00:01.724337Z\x1b[0m \x1b[32m INFO\x1b[0m "
    f"Subscribed trade events for account {ACCOUNT_S}\n",
    "    at the end of a multi-line message\n",
]
RECORDER_LOG_LINE = "2026-10-01 08:00:02,500 INFO p1rec.cli: preflight finished\n"

# Shaped like a trades list item and an order_event frame, with only the keys that matter here
TRADE_ITEM = {
    "contract": {"code": "TMFJ6"},
    "order": {
        "id": "o1",
        "seqno": "000001",
        "ordno": "A0001",
        "custom_field": "ab12cd",
        "account": ACCOUNT_OBJECT_F,
        "ca": FAKE_CA,
        "subaccount": SUBACCOUNT_REST,
    },
    "status": {"status": "Submitted"},
}
FUTURES_ORDER_FRAME = (
    "event:order_event\ndata:"
    + json.dumps(
        {
            "state": "FuturesOrder",
            "data": {
                "FuturesOrder": {
                    "event_id": "e1",
                    "order": {"id": "o1", "account": ACCOUNT_OBJECT_F, "ca": FAKE_CA, "subaccount": SUBACCOUNT_SSE},
                    "status": {},
                }
            },
        }
    )
    + "\n\n"
)

ORDER_FRAME = "event:order_event\ndata:" + json.dumps({"account": ACCOUNT_OBJECT_S, "status": "New"}) + "\n\n"
HEARTBEAT_FRAME = 'event:heartbeat\ndata:{"connection_id":"c1"}\n\n'


def _rest(op: str, method: str, path: str, req_body: str | None, resp_body: str, ts: int) -> dict[str, object]:
    return {
        "client": "main",
        "op": op,
        "method": method,
        "url": f"http://127.0.0.1:8080{path}",
        "req_body": req_body,
        "sent_ts_ns": ts - 1000,
        "recv_ts_ns": ts,
        "status": 200,
        "resp_headers": [["content-type", "application/json"]],
        "resp_body": resp_body,
    }


def _sse(kind: str, ts: int, **fields: object) -> dict[str, object]:
    return {"client": "a", "stream": "order_event", "kind": kind, "recv_ts_ns": ts, **fields}


def _write_recordings(results_root: Path) -> None:
    """Write one preflight run and one stream run holding every fake identifier."""
    run = results_root / PREFLIGHT_RUN
    run.mkdir(parents=True)
    rest = run / "rest.jsonl"
    accounts_body = json.dumps([ACCOUNT_OBJECT_S, ACCOUNT_OBJECT_F], ensure_ascii=False)
    append_jsonl(rest, _rest("accounts", "GET", "/api/v1/auth/accounts", None, accounts_body, TS))
    ca_body = json.dumps({"person_id": PERSON_ID, "expire_time": "2027-01-01T00:00:00"})
    append_jsonl(rest, _rest("ca_status", "GET", "/api/v1/auth/ca_expiretime", None, ca_body, TS + 1))

    # The request as sent, the response as a server that escapes non-ASCII would send it
    place_request = json.dumps({"account": ACCOUNT_OBJECT_S}, ensure_ascii=False)
    place_response = json.dumps({"account": ACCOUNT_OBJECT_S, "clientname": f"PYAPI/{API_KEY_PREFIX}/0929/x"})
    place = _rest("place_stock", "POST", "/api/v1/order/place_order", place_request, place_response, TS + 2)
    append_jsonl(rest, place)

    (run / "server-start.log").write_text("".join(SERVER_LOG_LINES), encoding="utf-8")
    (run / "recorder.log").write_text(RECORDER_LOG_LINE, encoding="utf-8")

    stream = results_root / STREAM_RUN
    stream.mkdir(parents=True)
    sse = stream / "sse_a_order_event.jsonl"
    append_jsonl(sse, _sse("connected", TS, status=200, url="http://127.0.0.1:8080/api/v1/stream/data/order_event"))
    append_jsonl(sse, _sse("frame", TS + 10, raw=ORDER_FRAME))
    append_jsonl(sse, _sse("frame", TS + 20, raw=HEARTBEAT_FRAME))
    append_jsonl(stream / "sse_b_order_event.jsonl", _sse("frame", TS + 40, raw=FUTURES_ORDER_FRAME))

    reconcile = results_root / RECONCILE_RUN
    reconcile.mkdir(parents=True)
    trades_body = json.dumps([TRADE_ITEM], ensure_ascii=False)
    append_jsonl(reconcile / "rest.jsonl", _rest("trades", "POST", "/api/v1/order/trades", None, trades_body, TS + 50))


def _fixture_table(
    name: str,
    run_id: str,
    source: str,
    lines: list[int],
    checklist: list[str],
    instrument: str,
    *extra: str,
) -> str:
    return "\n".join(
        [
            "[[fixture]]",
            f'name = "{name}"',
            f'run_id = "{run_id}"',
            f'source = "{source}"',
            f"lines = {json.dumps(lines)}",
            f"checklist = {json.dumps(checklist)}",
            f'instrument = "{instrument}"',
            *extra,
            "",
        ]
    )


REST_ENTRY = _fixture_table("F1_accounts", PREFLIGHT_RUN, "rest.jsonl", [1, 3], ["A1", "C3"], "")
SSE_ENTRY = _fixture_table("C1_order_event", STREAM_RUN, "sse_a_order_event.jsonl", [2, 3], ["C1"], "2890")
LOG_ENTRY = _fixture_table("E1_server_log", PREFLIGHT_RUN, "server-start.log", [2], ["E1"], "")


# fixtures_root nested as in the repo, so its parent holds fixtures only and not results_root
ENV_SETTINGS = {"fixtures_root": '"test_data/official/1.7.6"'}


def _config_lines(**overrides: str) -> list[str]:
    """Return MINIMAL_CONFIG_LINES with the named top-level or [scrub] keys set to TOML values."""
    settings = {**ENV_SETTINGS, **overrides}
    lines = []
    for line in MINIMAL_CONFIG_LINES:
        key = line.split(" = ", 1)[0]
        lines.append(f"{key} = {settings[key]}" if " = " in line and key in settings else line)
    return lines


@dataclass(frozen=True)
class _Env:
    config_path: Path
    cfg: P1Config


@pytest.fixture
def env(tmp_path: Path, write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch) -> _Env:
    """A config under tmp_path, fake secrets in the environment and recordings holding them."""
    for var, value in SECRET_ENV.items():
        monkeypatch.setenv(var, value)
    config_path = write_config(_config_lines())
    (tmp_path / "fixtures.toml").write_text("", encoding="utf-8")
    (tmp_path / "contract.md").write_text("clientname is PYAPI/API_KEY_PREFIX_1/...\n", encoding="utf-8")
    cfg = load_config(config_path)
    cfg.fixtures_root.parent.mkdir(parents=True)
    _write_recordings(cfg.results_root)
    return _Env(config_path=config_path, cfg=cfg)


def _rewrite_config(env: _Env, **overrides: str) -> None:
    env.config_path.write_text("\n".join(_config_lines(**overrides)) + "\n", encoding="utf-8")


def _select(env: _Env, *entries: str) -> None:
    env.cfg.scrub.selection.write_text("\n".join(entries), encoding="utf-8")


def _full_map(env: _Env) -> dict[str, str]:
    return build_map(SECRETS, discover(env.cfg), {})


def _assert_no_original(data: bytes) -> None:
    text = data.decode("utf-8", "replace")
    for original in ORIGINALS:
        for form in (original, json.dumps(original)[1:-1], json.dumps(json.dumps(original)[1:-1])[1:-1]):
            assert form.lower() not in text.lower()


def test_deterministic_placeholders() -> None:
    discovered = {"ACCOUNT_ID": [ACCOUNT_S, ACCOUNT_F], "USERNAME": [USERNAME], "PERSON_ID": [PERSON_ID]}

    first = build_map(SECRETS, discovered, {})
    second = build_map(SECRETS, discovered, {})

    assert first == second
    assert first[ACCOUNT_S] == "ACCOUNT_ID_1"
    assert first[ACCOUNT_F] == "ACCOUNT_ID_2"
    assert first[USERNAME] == "USERNAME_1"
    assert first[API_KEY] == "API_KEY_1"
    assert first[API_KEY_PREFIX] == "API_KEY_PREFIX_1"
    assert first[EGRESS_IP] == "EGRESS_IP_1"

    # The national ID from the environment and from responses is one value, one placeholder
    assert first[PERSON_ID] == "PERSON_ID_1"
    assert list(first.values()).count("PERSON_ID_1") == 1

    # A later run that sees a new account first keeps every earlier placeholder
    later = build_map(SECRETS, {"ACCOUNT_ID": ["0977003", ACCOUNT_F]}, first)

    assert {original: later[original] for original in first} == first
    assert later["0977003"] == "ACCOUNT_ID_3"


def test_credential_equal_to_identity_takes_identity_placeholder() -> None:
    mapping = build_map({**SECRETS, "ca_passwd": PERSON_ID}, {}, {})

    assert mapping[PERSON_ID] == "PERSON_ID_1"
    assert not any(placeholder.startswith("CA_PASSWD") for placeholder in mapping.values())


def test_placeholder_containing_original_rejected() -> None:
    with pytest.raises(ScrubError) as exc_info:
        build_map(SECRETS, {"ACCOUNT_ID": [ACCOUNT_S], "USERNAME": ["ID_1"]}, {})

    assert "ID_1" not in str(exc_info.value)


def test_longest_first_replacement() -> None:
    mapping = build_map(SECRETS, {}, {})

    result = scrub_text(f"key={API_KEY} prefix={API_KEY_PREFIX}", mapping)

    assert result == "key=API_KEY_1 prefix=API_KEY_PREFIX_1"


def test_embedded_value_replaced() -> None:
    mapping = build_map(SECRETS, {}, {})

    result = scrub_text(f'{{"clientname":"PYAPI/{API_KEY_PREFIX}/0929/abc"}}', mapping)

    assert result == '{"clientname":"PYAPI/API_KEY_PREFIX_1/0929/abc"}'


def test_overlapping_originals_replace_longer_whole() -> None:
    mapping = build_map({}, {"ACCOUNT_ID": ["QQQXYZ"], "USERNAME": ["XYZWWWWWWW"]}, {})
    escaped_text = json.dumps("QQQXYZWWWWWWW", ensure_ascii=True)

    assert scrub_text("QQQXYZWWWWWWW", mapping) == "QQQUSERNAME_1"
    assert scrub_text(escaped_text, mapping) == '"QQQUSERNAME_1"'


def test_placeholder_never_rewritten_by_shorter_original(tmp_path: Path) -> None:
    mapping = build_map({}, {"ACCOUNT_ID": ["LONGVALUE0"], "USERNAME": ["_1Q"]}, {})

    result = scrub_text("LONGVALUE0Q", mapping)

    # The shorter original re-forms across the placeholder's end, and the
    # scan reports it instead of a rewritten placeholder hiding it.
    assert result == "ACCOUNT_ID_1Q"
    output = tmp_path / "output.txt"
    output.write_text(result, encoding="utf-8")
    assert scan([output], mapping) == [Leak(path=output, line=1, kind="USERNAME")]


def test_escaped_forms_replaced_and_detected(tmp_path: Path) -> None:
    mapping = build_map(SECRETS, {"USERNAME": [USERNAME]}, {})
    escaped = json.dumps(USERNAME)[1:-1]  # 測試甲
    upper_hex = escaped.replace("\\u6e2c", "\\u6E2C")

    # A server body that escapes non-ASCII, stored in a JSONL line, escapes it twice
    jsonl_line = json.dumps({"resp_body": json.dumps({"username": USERNAME})}, ensure_ascii=False)
    forms = [USERNAME, escaped, upper_hex, jsonl_line, quote(USERNAME)]
    assert "\\\\u6e2c" in jsonl_line

    for text in forms:
        result = scrub_text(text, mapping)
        assert "USERNAME_1" in result
        _assert_no_original(result.encode("utf-8"))
    assert json.loads(json.loads(scrub_text(jsonl_line, mapping))["resp_body"]) == {"username": "USERNAME_1"}

    leaky = tmp_path / "forms.txt"
    leaky.write_text("\n".join(forms) + "\n", encoding="utf-8")
    leaks = scan([leaky], categories(mapping))
    assert leaks == [Leak(path=leaky, line=n, kind="USERNAME") for n in range(1, len(forms) + 1)]


def test_scan_finds_originals_in_base64_and_big5(tmp_path: Path) -> None:
    mapping = build_map(SECRETS, {"ACCOUNT_ID": [ACCOUNT_S], "USERNAME": [USERNAME]}, {})
    encoded = base64.b64encode(f'{{"account_id":"{ACCOUNT_S}"}}'.encode()).decode("ascii")
    leaky = tmp_path / "fixture.json"
    leaky.write_bytes(
        f'{{\n  "resp_body_b64": "{encoded}"\n}}\n'.encode() + b"name=" + USERNAME.encode("cp950") + b"\n"
    )

    leaks = scan([tmp_path], categories(mapping))

    assert leaks == [Leak(path=leaky, line=2, kind="ACCOUNT_ID"), Leak(path=leaky, line=4, kind="USERNAME")]


def test_scan_reports_without_value(env: _Env, capsys: pytest.CaptureFixture[str]) -> None:
    fixtures_root = env.cfg.fixtures_root
    fixtures_root.mkdir(parents=True)
    leaky = fixtures_root / "C9_leaky.json"
    leaky.write_text(f'[\n  {{"status": "New"}},\n  {{"account_id": "{ACCOUNT_S}"}}\n]\n', encoding="utf-8")

    expected = [Leak(path=leaky, line=3, kind="ACCOUNT_ID")]
    assert scan([fixtures_root], categories(_full_map(env))) == expected

    # build_map's placeholders are reduced to their categories too
    assert scan([fixtures_root], _full_map(env)) == expected

    exit_code = main(["scrub", "--scan-only", "--config", str(env.config_path)])

    assert exit_code == 2
    stderr = capsys.readouterr().err
    assert f"{leaky}:3:ACCOUNT_ID" in stderr
    [run_dir] = env.cfg.results_root.glob("*-scrub")
    log_text = (run_dir / "recorder.log").read_text(encoding="utf-8")
    assert f"{leaky}:3:ACCOUNT_ID" in log_text
    for output in (stderr, log_text):
        _assert_no_original(output.encode("utf-8"))


def test_scan_clean_returns_zero(env: _Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fixtures_root = env.cfg.fixtures_root
    fixtures_root.mkdir(parents=True)
    (fixtures_root / "C1_order_event.sse").write_text(
        'event:order_event\ndata:{"account_id":"ACCOUNT_ID_1"}\n\n', encoding="utf-8"
    )
    (tmp_path / "notes.md").write_text("account ACCOUNT_ID_2\n", encoding="utf-8")
    _rewrite_config(env, extra_scan_paths='["notes.md"]')

    exit_code = main(["scrub", "--scan-only", "--config", str(env.config_path)])

    assert exit_code == 0
    assert "leak" not in capsys.readouterr().err

    # The map now holds the originals, readable by its owner only
    map_path = env.cfg.scrub.map_path
    assert stat.S_IMODE(map_path.stat().st_mode) == 0o600
    assert load_map(map_path)[ACCOUNT_S] == "ACCOUNT_ID_1"


def test_scan_only_fails_when_account_identifiers_unknown(
    env: _Env, capsys: pytest.CaptureFixture[str]
) -> None:
    for rest_log in env.cfg.results_root.glob("*/rest.jsonl"):
        rest_log.unlink()

    exit_code = main(["scrub", "--scan-only", "--config", str(env.config_path)])

    assert exit_code == 1
    assert "ACCOUNT_ID, USERNAME" in capsys.readouterr().err


def test_spec_targets_scanned_without_extra_paths(
    env: _Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Outside fixtures_root but inside its parent, as an older version's fixtures would be
    older = env.cfg.fixtures_root.parent / "1.7.5" / "C1_old.sse"
    older.parent.mkdir()
    older.write_text(f'data:{ACCOUNT_S}\ndata:{{"subaccount": "S1"}}\n', encoding="utf-8")
    contract = tmp_path / "contract.md"
    contract.write_text(f"# Contract\nlogin from {EGRESS_IP}\n", encoding="utf-8")
    assert env.cfg.scrub.extra_scan_paths == ()

    exit_code = main(["scrub", "--scan-only", "--config", str(env.config_path)])

    assert exit_code == 2
    stderr = capsys.readouterr().err
    assert f"{older}:1:ACCOUNT_ID" in stderr
    assert f"{older}:2:SUBACCOUNT" in stderr
    assert f"{contract.resolve()}:2:EGRESS_IP" in stderr


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"extra_scan_paths": '["missing.md"]'}, "scrub.extra_scan_paths names a path that does not exist"),
        ({"contract_doc": '"missing.md"'}, "scrub.contract_doc names a path that does not exist"),
        ({"fixtures_root": '"elsewhere/1.7.6"'}, "fixtures_root's parent names a path that does not exist"),
        ({"fixtures_root": '"fixtures"'}, "overlaps results_root"),
    ],
)
def test_scan_only_refuses_bad_targets(
    env: _Env, capsys: pytest.CaptureFixture[str], overrides: dict[str, str], message: str
) -> None:
    _rewrite_config(env, **overrides)

    assert main(["scrub", "--scan-only", "--config", str(env.config_path)]) == 1
    assert message in capsys.readouterr().err


def test_manifest_entry_per_fixture(env: _Env) -> None:
    _select(env, REST_ENTRY, SSE_ENTRY)
    mapping = _full_map(env)

    written = export_fixtures(env.cfg, mapping)

    root = env.cfg.fixtures_root
    assert written == [root / "F1_accounts.json", root / "C1_order_event.sse", root / "MANIFEST.toml"]
    manifest = tomllib.loads((root / "MANIFEST.toml").read_text(encoding="utf-8"))
    assert manifest["fixture"] == [
        {
            "file": "F1_accounts.json",
            "run_id": PREFLIGHT_RUN,
            "source": "rest.jsonl",
            "lines": [1, 3],
            "recorded_at": TS_ISO,
            "instrument": "",
            "checklist": ["A1", "C3"],
        },
        {
            "file": "C1_order_event.sse",
            "run_id": STREAM_RUN,
            "source": "sse_a_order_event.jsonl",
            "lines": [2, 3],
            "recorded_at": "2026-09-21T14:13:20.123456799Z",
            "instrument": "2890",
            "checklist": ["C1"],
        },
    ]
    assert scan(written, categories(mapping)) == []
    for path in written:
        _assert_no_original(path.read_bytes())

    # Nothing is left staged once the export succeeds
    assert not list(env.cfg.results_root.glob("scrub-staging-*"))


def test_manifest_recorded_at_for_every_kind(env: _Env) -> None:
    _select(
        env,
        REST_ENTRY,
        SSE_ENTRY,
        LOG_ENTRY,
        _fixture_table("E1_recorder_log", PREFLIGHT_RUN, "recorder.log", [1], ["E1"], ""),
    )

    export_fixtures(env.cfg, _full_map(env))

    manifest = tomllib.loads((env.cfg.fixtures_root / "MANIFEST.toml").read_text(encoding="utf-8"))
    recorded_at = {entry["file"]: entry["recorded_at"] for entry in manifest["fixture"]}

    # recorder.log writes local time, so the expected value follows this host's time zone
    local = datetime(2026, 10, 1, 8, 0, 2).astimezone(timezone.utc)
    assert recorded_at == {
        "F1_accounts.json": TS_ISO,
        "C1_order_event.sse": "2026-09-21T14:13:20.123456799Z",
        "E1_server_log.log": "2026-10-01T00:00:01.724337000Z",
        "E1_recorder_log.log": f"{local:%Y-%m-%dT%H:%M:%S}.500000000Z",
    }


def test_log_without_timestamp_needs_explicit_recorded_at(env: _Env) -> None:
    untimed = ("E1_continuation", PREFLIGHT_RUN, "server-start.log", [3], ["E1"], "")

    _select(env, _fixture_table(*untimed))
    with pytest.raises(ConfigError, match=r"E1_continuation.*give recorded_at"):
        export_fixtures(env.cfg, _full_map(env))

    _select(env, _fixture_table(*untimed, "recorded_at = 2026-10-01T08:00:03+08:00"))
    export_fixtures(env.cfg, _full_map(env))
    manifest = tomllib.loads((env.cfg.fixtures_root / "MANIFEST.toml").read_text(encoding="utf-8"))
    assert manifest["fixture"][0]["recorded_at"] == "2026-10-01T00:00:03.000000000Z"

    # A source with its own timestamp takes no explicit one, so the two can never disagree
    _select(env, REST_ENTRY + "recorded_at = 2026-10-01T08:00:03+08:00\n")
    with pytest.raises(ConfigError, match="recorded_at is only for"):
        export_fixtures(env.cfg, _full_map(env))


def test_cli_export_scrubs_every_source_kind(env: _Env) -> None:
    _select(env, REST_ENTRY, SSE_ENTRY, LOG_ENTRY)

    assert main(["scrub", "--config", str(env.config_path)]) == 0

    root = env.cfg.fixtures_root
    records = json.loads((root / "F1_accounts.json").read_text(encoding="utf-8"))
    accounts = json.loads(records[0]["resp_body"])
    assert [account["account_id"] for account in accounts] == ["ACCOUNT_ID_1", "ACCOUNT_ID_2"]
    assert accounts[0]["username"] == "USERNAME_1"
    assert accounts[0]["person_id"] == "PERSON_ID_1"
    assert accounts[0]["broker_id"] == "FAKE"
    assert records[0]["recv_ts_ns"] == TS
    placed = json.loads(records[1]["resp_body"])
    assert placed["clientname"] == "PYAPI/API_KEY_PREFIX_1/0929/x"
    assert placed["account"]["username"] == "USERNAME_1"
    assert json.loads(records[1]["req_body"])["account"]["account_id"] == "ACCOUNT_ID_1"

    scrubbed_frame = scrub_text(ORDER_FRAME, load_map(env.cfg.scrub.map_path))
    assert (root / "C1_order_event.sse").read_text(encoding="utf-8") == scrubbed_frame + HEARTBEAT_FRAME
    assert '"account_id": "ACCOUNT_ID_1"' in scrubbed_frame

    assert (root / "E1_server_log.log").read_text(encoding="utf-8") == SERVER_LOG_LINES[1].replace(
        ACCOUNT_S, "ACCOUNT_ID_1"
    )
    for path in root.iterdir():
        _assert_no_original(path.read_bytes())
    for run_log in env.cfg.results_root.glob("*-scrub/recorder.log"):
        _assert_no_original(run_log.read_bytes())

    # Tagged so its run directory cannot collide with the export's within the same second
    assert main(["scrub", "--scan-only", "--config", str(env.config_path), "--tag", "scan"]) == 0


def test_export_scrubs_non_utf8_frame_bytes(env: _Env) -> None:
    raw = b"event:order_event\ndata:" + USERNAME.encode("cp950") + f" {ACCOUNT_S}\xff\n\n".encode("latin-1")
    sse = env.cfg.results_root / STREAM_RUN / "sse_a_order_event.jsonl"
    append_jsonl(
        sse,
        _sse(
            "frame",
            TS + 30,
            raw=raw.decode("utf-8", "replace"),
            raw_b64=base64.b64encode(raw).decode("ascii"),
        ),
    )
    _select(env, _fixture_table("C1_big5", STREAM_RUN, "sse_a_order_event.jsonl", [4], ["C1"], ""))

    export_fixtures(env.cfg, _full_map(env))

    assert (env.cfg.fixtures_root / "C1_big5.sse").read_bytes() == (
        b"event:order_event\ndata:USERNAME_1 ACCOUNT_ID_1\xff\n\n"
    )


def test_export_leak_leaves_fixtures_root_untouched(
    env: _Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _select(env, SSE_ENTRY)

    # A replacer that replaces nothing stands in for any scrubbing gap
    monkeypatch.setattr(scrub, "_replacer", lambda _mapping: scrub._Replacer({}))

    exit_code = main(["scrub", "--config", str(env.config_path)])

    assert exit_code == 2
    assert not env.cfg.fixtures_root.exists()
    [staging] = env.cfg.results_root.glob("scrub-staging-*")
    stderr = capsys.readouterr().err
    assert f"{staging / 'C1_order_event.sse'}:2:ACCOUNT_ID" in stderr
    _assert_no_original(stderr.encode("utf-8"))


def test_export_refuses_json_broken_by_scrubbing(env: _Env) -> None:
    rest_log = env.cfg.results_root / PREFLIGHT_RUN / "rest.jsonl"
    append_jsonl(rest_log, _rest("trades", "POST", "/api/v1/order/trades", None, f'{{"n": 9{ACCOUNT_S}}}', TS + 3))
    _select(env, _fixture_table("C7_trades", PREFLIGHT_RUN, "rest.jsonl", [4], ["C7"], ""))

    with pytest.raises(ScrubError, match="C7_trades"):
        export_fixtures(env.cfg, _full_map(env))


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (_fixture_table("F1_accounts", PREFLIGHT_RUN, "rest.jsonl", [1, 99], ["A1"], ""), "no line 99"),
        (_fixture_table("../escape", PREFLIGHT_RUN, "rest.jsonl", [1], ["A1"], ""), "path separators"),
        (_fixture_table("C1_bad", STREAM_RUN, "sse_a_order_event.jsonl", [1], ["C1"], ""), "not a frame record"),
        (_fixture_table("C1_bad", STREAM_RUN, "missing.jsonl", [1], ["C1"], ""), "does not exist"),
        (REST_ENTRY + REST_ENTRY, "fixture 1 repeats the name of fixture 0"),
        (REST_ENTRY.replace("lines = ", "line = "), "unknown keys line"),
    ],
)
def test_export_refuses_bad_entries(env: _Env, entry: str, message: str) -> None:
    _select(env, entry)

    with pytest.raises(ConfigError, match=message):
        export_fixtures(env.cfg, _full_map(env))
    assert not env.cfg.fixtures_root.exists()


def test_ca_signature_and_subaccount_scrubbed_on_export(env: _Env) -> None:
    assert discover(env.cfg)["SUBACCOUNT"] == [SUBACCOUNT_REST]
    _select(
        env,
        _fixture_table("C3_trades", RECONCILE_RUN, "rest.jsonl", [1], ["C3"], "TMFJ6"),
        _fixture_table("C1_futures_order", STREAM_RUN, "sse_b_order_event.jsonl", [1], ["C1"], "TMFJ6"),
    )

    written = export_fixtures(env.cfg, _full_map(env))

    root = env.cfg.fixtures_root
    [trade] = json.loads(json.loads((root / "C3_trades.json").read_text(encoding="utf-8"))[0]["resp_body"])
    assert trade["order"]["ca"] == "CA_SIGNATURE"
    assert trade["order"]["subaccount"] == "SUBACCOUNT_1"
    assert trade["order"]["account"]["account_id"] == "ACCOUNT_ID_2"
    frame = (root / "C1_futures_order.sse").read_text(encoding="utf-8")
    order = json.loads(frame.split("data:", 1)[1])["data"]["FuturesOrder"]["order"]
    assert order["ca"] == "CA_SIGNATURE"

    # Seen on the order stream alone, it still gets a placeholder, saved so a later scan knows it
    assert order["subaccount"] == "SUBACCOUNT_2"
    assert load_map(env.cfg.scrub.map_path)[SUBACCOUNT_SSE] == "SUBACCOUNT_2"
    for path in written:
        _assert_no_original(path.read_bytes())


def test_scan_reports_ca_signature_and_account_keys(tmp_path: Path) -> None:
    member = json.dumps({"order": {"ca": FAKE_CA, "subaccount": "S1"}})
    clean = json.dumps({"order": {"ca": "CA_SIGNATURE", "subaccount": "SUBACCOUNT_1"}, "empty": {"ca": "", "x": None}})
    fixture = tmp_path / "scan" / "C1_mixed.jsonl"
    fixture.parent.mkdir()
    fixture.write_text(
        "\n".join(
            [
                f"data:{member}",
                json.dumps({"resp_body": member}),
                json.dumps({"line": json.dumps({"resp_body": member})}),
                f"data:{clean}",
                json.dumps({"resp_body": clean}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # No original is known here: the members are reported by key alone, at every escape depth
    leaks = scan([fixture], {}, {"subaccount": "SUBACCOUNT"})

    assert leaks == [
        Leak(path=fixture, line=line, kind=kind) for line in (1, 2, 3) for kind in ("CA_SIGNATURE", "SUBACCOUNT")
    ]
    assert scan([fixture], {}) == [Leak(path=fixture, line=line, kind="CA_SIGNATURE") for line in (1, 2, 3)]


@pytest.mark.parametrize("field", ["name", "run_id", "instrument"])
def test_entry_field_holding_original_rejected(env: _Env, field: str) -> None:
    values = {"name": "C1_order_event", "run_id": STREAM_RUN, "instrument": ""}
    values[field] = f"{values[field]}{ACCOUNT_S}"
    name, run_id, instrument = values["name"], values["run_id"], values["instrument"]
    _select(env, REST_ENTRY, _fixture_table(name, run_id, "sse_a_order_event.jsonl", [2], ["C1"], instrument))

    with pytest.raises(ConfigError, match=f"fixture 1: {field} contains an original value") as exc_info:
        export_fixtures(env.cfg, _full_map(env))
    assert ACCOUNT_S not in str(exc_info.value)
    assert not env.cfg.fixtures_root.exists()


def test_map_path_must_stay_under_results_root(write_config: WriteConfig) -> None:
    lines = [
        'map_path = "fixtures/scrub-map.json"' if line.startswith("map_path") else line
        for line in MINIMAL_CONFIG_LINES
    ]

    with pytest.raises(ConfigError, match=r"scrub\.map_path"):
        load_config(write_config(lines))


def test_scrub_pointer_must_end_in_field_name(write_config: WriteConfig) -> None:
    lines = ['ca_pointers = ["/0"]' if line.startswith("ca_pointers") else line for line in MINIMAL_CONFIG_LINES]

    with pytest.raises(ConfigError, match=r"scrub\.ca_pointers\[0\]"):
        load_config(write_config(lines))


def test_real_p1_toml_scrub_section() -> None:
    tool_dir = Path(__file__).resolve().parent.parent
    cfg = load_config(tool_dir / "p1.toml")

    assert cfg.scrub.account_id_pointers == ("/account_id", "/username", "/person_id")
    assert cfg.scrub.ca_pointers == ("/person_id",)
    assert cfg.scrub.map_path.is_relative_to(cfg.results_root)
    assert cfg.scrub.contract_doc.is_file()
    assert cfg.fixtures_root.parent.name == "official"
    assert all(path.exists() for path in cfg.scrub.extra_scan_paths)
    assert scrub.load_selection(cfg.scrub.selection) == []
