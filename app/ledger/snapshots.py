from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ledger.service import MICRO_UNITS, canonical_json
from app.models import LedgerEntry

SNAPSHOT_VERSION = "mrwk_account_snapshot_v1"
LEAF_PREFIX = b"mergework:mrwk:snapshot:leaf:v1\0"
NODE_PREFIX = b"mergework:mrwk:snapshot:node:v1\0"
EMPTY_PREFIX = b"mergework:mrwk:snapshot:empty:v1\0"


@dataclass(frozen=True)
class SnapshotLeaf:
    account: str
    balance_microunits: int

    def payload(self) -> dict[str, Any]:
        return {
            "version": SNAPSHOT_VERSION,
            "account": self.account,
            "balance_microunits": self.balance_microunits,
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def leaf_hash(leaf: SnapshotLeaf | dict[str, Any]) -> str:
    if isinstance(leaf, dict):
        leaf = SnapshotLeaf(
            account=str(leaf["account"]),
            balance_microunits=int(leaf["balance_microunits"]),
        )
    return _sha256(LEAF_PREFIX + canonical_json(leaf.payload()).encode("utf-8"))


def node_hash(left_hash: str, right_hash: str) -> str:
    if len(left_hash) != 64 or len(right_hash) != 64:
        raise ValueError("Merkle child hashes must be 64-char hex strings")
    return _sha256(NODE_PREFIX + f"{left_hash}:{right_hash}".encode("ascii"))


def empty_root() -> str:
    return _sha256(EMPTY_PREFIX + SNAPSHOT_VERSION.encode("ascii"))


def account_balances_from_ledger(
    session: Session, *, through_sequence: int | None = None
) -> dict[str, int]:
    stmt = select(LedgerEntry).order_by(LedgerEntry.sequence.asc())
    if through_sequence is not None:
        stmt = stmt.where(LedgerEntry.sequence <= through_sequence)
    balances: dict[str, int] = {}
    for entry in session.scalars(stmt):
        amount = int(entry.amount_microunits)
        if entry.from_account:
            balances[entry.from_account] = balances.get(entry.from_account, 0) - amount
        if entry.to_account:
            balances[entry.to_account] = balances.get(entry.to_account, 0) + amount
    return {account: amount for account, amount in balances.items() if amount > 0}


def snapshot_leaves(balances: dict[str, int]) -> list[SnapshotLeaf]:
    return [
        SnapshotLeaf(account=account, balance_microunits=int(balances[account]))
        for account in sorted(balances)
        if int(balances[account]) > 0
    ]


def merkle_root_from_leaf_hashes(hashes: Iterable[str]) -> str:
    level = list(hashes)
    if not level:
        return empty_root()
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [node_hash(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def merkle_root(leaves: Iterable[SnapshotLeaf]) -> str:
    return merkle_root_from_leaf_hashes(leaf_hash(leaf) for leaf in leaves)


def merkle_proof(leaves: list[SnapshotLeaf], account: str) -> dict[str, Any]:
    accounts = [leaf.account for leaf in leaves]
    if account not in accounts:
        raise KeyError("account not present in snapshot")
    index = accounts.index(account)
    leaf = leaves[index]
    level = [leaf_hash(item) for item in leaves]
    path: list[dict[str, str]] = []
    pos = index
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        sibling_pos = pos - 1 if pos % 2 else pos + 1
        path.append(
            {
                "side": "left" if pos % 2 else "right",
                "hash": level[sibling_pos],
            }
        )
        pos //= 2
        level = [node_hash(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return {
        "version": SNAPSHOT_VERSION,
        "leaf": leaf.payload(),
        "leaf_hash": leaf_hash(leaf),
        "path": path,
        "root": level[0] if level else empty_root(),
    }


def verify_merkle_proof(proof: dict[str, Any], expected_root: str) -> bool:
    if proof.get("version") != SNAPSHOT_VERSION:
        return False
    try:
        current = leaf_hash(proof["leaf"])
        if current != proof.get("leaf_hash"):
            return False
        for step in proof.get("path") or []:
            sibling = str(step["hash"])
            side = step["side"]
            if side == "left":
                current = node_hash(sibling, current)
            elif side == "right":
                current = node_hash(current, sibling)
            else:
                return False
    except (KeyError, TypeError, ValueError):
        return False
    return current == expected_root


def build_account_snapshot(
    session: Session, *, through_sequence: int | None = None
) -> dict[str, Any]:
    if through_sequence is None:
        latest = session.scalar(select(LedgerEntry).order_by(LedgerEntry.sequence.desc()).limit(1))
        through_sequence = int(latest.sequence) if latest is not None else 0
    latest_entry = session.get(LedgerEntry, through_sequence) if through_sequence else None
    balances = account_balances_from_ledger(session, through_sequence=through_sequence or None)
    leaves = snapshot_leaves(balances)
    return {
        "version": SNAPSHOT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "ledger_entries",
        "through_sequence": through_sequence,
        "through_entry_hash": latest_entry.entry_hash if latest_entry is not None else None,
        "unit": "MRWK microunits",
        "microunits_per_mrwk": MICRO_UNITS,
        "account_count": len(leaves),
        "root": merkle_root(leaves),
        "leaves": [leaf.payload() | {"hash": leaf_hash(leaf)} for leaf in leaves],
    }
