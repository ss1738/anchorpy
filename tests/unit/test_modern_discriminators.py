"""Exercise explicit IDL discriminators through runtime and generated clients."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from anchorpy import AccountsCoder, EventCoder, Idl, InstructionCoder, Program
from anchorpy.clientgen.accounts import gen_accounts
from anchorpy.clientgen.instructions import gen_instructions
from anchorpy.error import AccountInvalidDiscriminator
from based58 import b58decode
from construct import ConstructError
from solders.pubkey import Pubkey

IX_DISC = bytes([9, 8, 7, 6, 5, 4, 3, 2])
ACC_DISC = bytes([19, 18, 17, 16, 15, 14, 13, 12])
EVENT_DISC = bytes([29, 28, 27, 26, 25, 24, 23, 22])
VALUE = 1234
PAYLOAD = VALUE.to_bytes(8, "little")


def _idl_json(size: int) -> dict:
    return {
        "address": str(Pubkey.default()),
        "metadata": {
            "name": "explicit_discriminators",
            "version": "0.1.0",
            "spec": "0.1.0",
        },
        "instructions": [
            {
                "name": "set_value",
                "discriminator": list((IX_DISC + b"abcd")[:size]),
                "accounts": [],
                "args": [{"name": "value", "type": "u64"}],
            }
        ],
        "accounts": [
            {"name": "State", "discriminator": list((ACC_DISC + b"abcd")[:size])}
        ],
        "events": [
            {"name": "Changed", "discriminator": list((EVENT_DISC + b"abcd")[:size])}
        ],
        "types": [
            {
                "name": name,
                "type": {
                    "kind": "struct",
                    "fields": [{"name": "value", "type": "u64"}],
                },
            }
            for name in ["State", "Changed"]
        ],
    }


@pytest.fixture(params=[1, 2, 8, 12])
def modern_idl(request) -> Idl:
    """Use explicit, non-default discriminators of several supported lengths."""
    return Idl.from_json(json.dumps(_idl_json(request.param)))


def test_instruction_wire_bytes(modern_idl: Idl) -> None:
    coder = InstructionCoder(modern_idl)
    assert (
        coder.encode("set_value", {"value": VALUE})
        == bytes(modern_idl.instructions[0].discriminator) + PAYLOAD
    )


def test_instruction_decode(modern_idl: Idl) -> None:
    decoded = InstructionCoder(modern_idl).parse(
        bytes(modern_idl.instructions[0].discriminator) + PAYLOAD
    )
    assert decoded.name == "set_value"
    assert decoded.data.value == VALUE


def test_account_wire_bytes(modern_idl: Idl) -> None:
    coder = AccountsCoder(modern_idl)
    data = bytes(modern_idl.accounts[0].discriminator) + PAYLOAD
    decoded = coder.parse(data)
    assert decoded.name == "State"
    assert decoded.data.value == VALUE
    assert coder.build(decoded) == data


def test_event_decode(modern_idl: Idl) -> None:
    decoded = EventCoder(modern_idl).parse(
        bytes(modern_idl.events[0].discriminator) + PAYLOAD
    )
    assert decoded is not None
    assert decoded.name == "Changed"
    assert decoded.data.value == VALUE


@pytest.mark.asyncio
async def test_account_fetch_uses_idl_discriminator(modern_idl: Idl) -> None:
    data = bytes(modern_idl.accounts[0].discriminator) + PAYLOAD
    connection = SimpleNamespace(
        get_account_info=AsyncMock(
            return_value=SimpleNamespace(value=SimpleNamespace(data=data))
        )
    )
    provider = SimpleNamespace(connection=connection)
    program = Program(modern_idl, Pubkey.default(), provider)
    result = await program.account["State"].fetch(Pubkey.default())
    assert result.value == VALUE
    connection.get_account_info.return_value.value.data = b"incorrect" + PAYLOAD
    with pytest.raises(AccountInvalidDiscriminator):
        await program.account["State"].fetch(Pubkey.default())


def test_generated_clients_use_idl_discriminators(
    modern_idl: Idl, tmp_path: Path, monkeypatch
) -> None:
    package = (
        f"explicit_discriminator_client_{len(modern_idl.instructions[0].discriminator)}"
    )
    root = tmp_path / package
    root.mkdir()
    (root / "__init__.py").write_text("")
    (root / "program_id.py").write_text(
        "from solders.pubkey import Pubkey\nPROGRAM_ID = Pubkey.default()\n"
    )
    gen_instructions(modern_idl, root, gen_pdas=False)
    gen_accounts(modern_idl, root)
    monkeypatch.syspath_prepend(str(tmp_path))
    instruction = importlib.import_module(f"{package}.instructions.set_value")
    account = importlib.import_module(f"{package}.accounts.state")
    assert (
        bytes(instruction.set_value({"value": VALUE}).data)
        == bytes(modern_idl.instructions[0].discriminator) + PAYLOAD
    )
    assert (
        account.State.decode(
            bytes(modern_idl.accounts[0].discriminator) + PAYLOAD
        ).value
        == VALUE
    )
    with pytest.raises(AccountInvalidDiscriminator):
        account.State.decode(b"incorrect" + PAYLOAD)


def test_unknown_and_truncated_payloads(modern_idl: Idl) -> None:
    """Unknown events are ignored; recognized malformed payloads still fail."""
    assert EventCoder(modern_idl).parse(b"unknown event") is None
    with pytest.raises(ConstructError):
        EventCoder(modern_idl).parse(
            bytes(modern_idl.events[0].discriminator) + b"\x01"
        )
    with pytest.raises(ConstructError):
        InstructionCoder(modern_idl).parse(b"unknown instruction")
    with pytest.raises(ConstructError):
        AccountsCoder(modern_idl).parse(b"unknown account")


@pytest.mark.asyncio
async def test_account_filters_and_batch_fetch(modern_idl: Idl, monkeypatch) -> None:
    discriminator = bytes(modern_idl.accounts[0].discriminator)
    data = discriminator + PAYLOAD
    row = SimpleNamespace(pubkey=Pubkey.default(), account=SimpleNamespace(data=data))
    connection = SimpleNamespace(
        _commitment="confirmed",
        get_program_accounts=AsyncMock(return_value=SimpleNamespace(value=[row])),
    )
    program = Program(
        modern_idl, Pubkey.default(), SimpleNamespace(connection=connection)
    )
    client = program.account["State"]
    assert client.size == len(discriminator) + len(PAYLOAD)
    result = await client.all(buffer=b"\xd2")
    assert result[0].account.value == VALUE
    filters = connection.get_program_accounts.call_args.kwargs["filters"]
    assert b58decode(filters[0].bytes.encode()) == discriminator + b"\xd2"
    batch = AsyncMock(
        return_value=[
            row,
            None,
            SimpleNamespace(account=SimpleNamespace(data=b"wrong" + PAYLOAD)),
        ]
    )
    monkeypatch.setattr(
        "anchorpy.program.namespace.account.get_multiple_accounts", batch
    )
    results = await client.fetch_multiple([Pubkey.default()] * 3)
    assert results[0].value == VALUE
    assert results[1:] == [None, None]


@pytest.mark.parametrize(
    "coder_type, section",
    [
        (InstructionCoder, "instructions"),
        (AccountsCoder, "accounts"),
        (EventCoder, "events"),
    ],
)
@pytest.mark.parametrize(
    "discriminators", [[[]], [[1], [1]], [[1], [1, 2]], [[1, 2], [1]]]
)
def test_reject_ambiguous_or_empty_discriminators(
    coder_type, section, discriminators
) -> None:
    raw = _idl_json(8)
    template = raw[section][0]
    raw[section] = [
        dict(template, name=f"entry_{index}", discriminator=disc)
        for index, disc in enumerate(discriminators)
    ]
    if section != "instructions":
        raw["types"].extend(
            dict(raw["types"][0], name=item["name"]) for item in raw[section]
        )
    with pytest.raises(ValueError, match="IDL discriminators"):
        coder_type(Idl.from_json(json.dumps(raw)))


@pytest.mark.parametrize(
    "coder_type, section",
    [
        (InstructionCoder, "instructions"),
        (AccountsCoder, "accounts"),
        (EventCoder, "events"),
    ],
)
def test_mixed_discriminator_lengths(coder_type, section) -> None:
    raw = _idl_json(8)
    original = raw[section][0]
    raw[section].append(dict(original, name="Other", discriminator=[99]))
    if section != "instructions":
        raw["types"].append(dict(raw["types"][0], name="Other"))
    coder = coder_type(Idl.from_json(json.dumps(raw)))
    assert coder.parse(b"\x63" + PAYLOAD).data.value == VALUE
    assert coder.parse(bytes(original["discriminator"]) + PAYLOAD).data.value == VALUE
