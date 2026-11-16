"""Build and decode modern Anchor data locally, without a wallet or RPC."""

import json

from anchorpy import AccountsCoder, Idl, InstructionCoder

VALUE = 1234

idl = Idl.from_json(
    json.dumps(
        {
            "address": "11111111111111111111111111111111",
            "metadata": {"name": "local_example", "version": "0.1.0", "spec": "0.1.0"},
            "instructions": [
                {
                    "name": "set_value",
                    "discriminator": [1, 2],
                    "accounts": [],
                    "args": [{"name": "value", "type": "u64"}],
                }
            ],
            "accounts": [{"name": "State", "discriminator": [3]}],
            "types": [
                {
                    "name": "State",
                    "type": {
                        "kind": "struct",
                        "fields": [{"name": "value", "type": "u64"}],
                    },
                }
            ],
        }
    )
)

instruction = InstructionCoder(idl).encode("set_value", {"value": VALUE})
assert instruction == bytes.fromhex("0102d204000000000000")
state = AccountsCoder(idl).decode(bytes.fromhex("03d204000000000000"))
assert state.value == VALUE
print(
    json.dumps(
        {"instruction_hex": instruction.hex(), "decoded_state_value": state.value}
    )
)
