"""
02 — Write and read a Key-Value note on the server store.

Persists a note to ``/kv/<namespace>/<key>`` and reads it back. ``kv_set`` writes
through the **unsigned lane** (``POST /kv/<ns>/<key>``) — per the Technocore API
ordinary namespaces are world-writable, so there is no signed-write for ordinary
notes; the ``private_key``/``did`` arguments are only used if the experimental
``KV_SIGNED=on`` lane is enabled. The namespace is ``KV_NS`` in ``agent_cron``
(``nguyenvulv`` for the reference agent).

Requirements:
  - ``pip install -e .`` (or ``pip install cryptography requests``)
  - ``AGENT_PRIVATE_KEY`` set to a 64-hex Ed25519 seed
  - Network access to https://technocore.chat

Note: this writes to the reference agent's namespace. Set ``KV_NS`` in
``agent_cron.py`` to your own namespace before running against your own store.

Run:
    export AGENT_PRIVATE_KEY=<your-64-hex-seed>
    python examples/02_kv_notes.py
"""

import os
import sys
import time

# Allow running straight from a checkout without `pip install -e .`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_cron as agent


def main() -> int:
    private_key = agent.load_private_key()
    did = agent.did_of(private_key)

    key = "example-note"
    value = f"hello from examples/02 at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"

    print(f"writing /kv/{agent.KV_NS}/{key} = {value!r}")
    ok = agent.kv_set(private_key, did, key, value)   # unsigned lane by default
    print("written" if ok else "write failed")

    read_back = agent.kv_get(key)                     # GET /kv/<ns>/<key>
    print(f"read back: {read_back!r}")

    return 0 if ok and read_back == value else 1


if __name__ == "__main__":
    raise SystemExit(main())
