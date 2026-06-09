from __future__ import annotations

from app.db import create_schema, session_scope
from app.ledger.service import MICRO_UNITS, add_ledger_entry, ensure_genesis
from app.ledger.snapshots import (
    SNAPSHOT_VERSION,
    build_account_snapshot,
    merkle_proof,
    snapshot_leaves,
    verify_merkle_proof,
)


def test_account_snapshot_builds_deterministic_root_and_verifiable_account_proof(
    sqlite_url: str,
) -> None:
    create_schema(sqlite_url)

    with session_scope(sqlite_url) as session:
        ensure_genesis(session)
        add_ledger_entry(
            session,
            entry_type="bounty_reserve",
            from_account="treasury:mrwk",
            to_account="reserve:bounty:1027",
            amount_microunits=450 * MICRO_UNITS,
            reference="https://github.com/ramimbo/mergework/issues/1027",
        )
        add_ledger_entry(
            session,
            entry_type="bounty_payment",
            from_account="reserve:bounty:1027",
            to_account="github:alice",
            amount_microunits=125 * MICRO_UNITS,
            reference="https://github.com/ramimbo/mergework/pull/1027",
        )

        snapshot = build_account_snapshot(session)
        rebuilt = build_account_snapshot(session)

    assert snapshot["version"] == SNAPSHOT_VERSION
    assert snapshot["source"] == "ledger_entries"
    assert snapshot["through_sequence"] == 3
    assert snapshot["through_entry_hash"]
    assert snapshot["root"] == rebuilt["root"]
    assert snapshot["account_count"] == len(snapshot["leaves"])
    assert [leaf["account"] for leaf in snapshot["leaves"]] == sorted(
        leaf["account"] for leaf in snapshot["leaves"]
    )
    assert all(leaf["balance_microunits"] > 0 for leaf in snapshot["leaves"])

    leaves = snapshot_leaves(
        {leaf["account"]: leaf["balance_microunits"] for leaf in snapshot["leaves"]}
    )
    proof = merkle_proof(leaves, "github:alice")
    assert proof["version"] == SNAPSHOT_VERSION
    assert proof["root"] == snapshot["root"]
    assert verify_merkle_proof(proof, snapshot["root"]) is True

    tampered = dict(proof)
    tampered["leaf"] = dict(proof["leaf"], balance_microunits=126 * MICRO_UNITS)
    assert verify_merkle_proof(tampered, snapshot["root"]) is False


def test_snapshot_root_changes_when_latest_ledger_entry_changes(sqlite_url: str) -> None:
    create_schema(sqlite_url)

    with session_scope(sqlite_url) as session:
        ensure_genesis(session)
        before = build_account_snapshot(session)
        add_ledger_entry(
            session,
            entry_type="grant",
            from_account="treasury:mrwk",
            to_account="github:bob",
            amount_microunits=1 * MICRO_UNITS,
            reference="manual:test",
        )
        after = build_account_snapshot(session)

    assert before["root"] != after["root"]
    assert before["through_entry_hash"] != after["through_entry_hash"]
