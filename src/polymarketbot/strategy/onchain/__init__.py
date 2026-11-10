"""On-chain settlement primitives: RPC access, CTF calldata and the tx ledger.

No decision logic lives here; every module takes its collaborators by
injection so the paper and live paths can share it without touching the
network on import.
"""
