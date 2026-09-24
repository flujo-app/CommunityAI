"""Local test-unit accounting foundation for CommunityAI's shadow marketplace.

This module has no payment, withdrawal, access-control, or live-money interface.
Receipts stay pending until a separate verifier explicitly approves or rejects
them.  A caller-supplied approval is not proof of useful work; production
integration must supply an independent, authenticated verifier.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

SCHEMA_VERSION = 1
LEDGER_SCHEMA_VERSION = 2
QUOTE_VERSION = 1
MAX_CREDITS = 2**62 - 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,109}\Z")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class ShadowCreditError(ValueError):
    """An invalid or conflicting shadow-accounting operation."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ShadowCreditError(message)


def _id(value: object) -> str:
    _require(type(value) is str and _ID.fullmatch(value) is not None, "invalid identifier")
    return value


def _amount(value: object, *, positive: bool = False) -> int:
    _require(type(value) is int and 0 <= value <= MAX_CREDITS and (not positive or value > 0), "invalid amount")
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


@dataclass(frozen=True)
class ShadowQuote:
    """Immutable noncash service terms; neither funding nor authorization."""

    request_id: str
    buyer_id: str
    model_id: str
    profile_id: str
    artifact_sha256: str
    service_policy_sha256: str
    price_schedule_sha256: str
    service_class: str
    settlement_domain: str
    input_unit_price: int
    output_unit_price: int
    max_input_units: int
    max_output_units: int
    fee_bps: int
    spend_cap: int
    expires_at_unix: int
    version: int = QUOTE_VERSION

    def __post_init__(self) -> None:
        for value in (self.request_id, self.buyer_id, self.service_class, self.settlement_domain):
            _id(value)
        for value in (self.model_id, self.profile_id):
            _require(type(value) is str and _MODEL_ID.fullmatch(value) is not None, "invalid model/profile")
        for value in (self.artifact_sha256, self.service_policy_sha256, self.price_schedule_sha256):
            _require(type(value) is str and _HASH.fullmatch(value) is not None, "invalid quote digest field")
        for value in (self.input_unit_price, self.output_unit_price, self.max_input_units, self.max_output_units):
            _amount(value)
        _amount(self.spend_cap, positive=True)
        _require(self.max_input_units + self.max_output_units > 0, "quote has no work allowance")
        _require(
            0
            < self.input_unit_price * self.max_input_units + self.output_unit_price * self.max_output_units
            <= self.spend_cap,
            "quote cap does not cover maximum priced work",
        )
        _require(type(self.fee_bps) is int and 0 <= self.fee_bps <= 10_000, "invalid quoted fee")
        _require(type(self.expires_at_unix) is int and 0 < self.expires_at_unix < 2**53, "invalid quote expiry")
        _require(type(self.version) is int and self.version == QUOTE_VERSION, "unsupported quote version")

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True)
class WorkReceipt:
    """Content-free, untrusted work claim. Units and charge are test units."""

    receipt_id: str
    request_id: str
    provider_id: str
    stage_id: str
    attempt_id: str
    input_units: int
    output_units: int
    proposed_charge: int
    evidence_sha256: str
    version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for value in (self.receipt_id, self.request_id, self.provider_id, self.stage_id, self.attempt_id):
            _id(value)
        for value in (self.input_units, self.output_units, self.proposed_charge):
            _amount(value)
        _require(self.input_units + self.output_units <= MAX_CREDITS, "unit total overflow")
        _require(self.proposed_charge == 0 or self.input_units + self.output_units > 0, "charge without work")
        _require(
            type(self.evidence_sha256) is str and _HASH.fullmatch(self.evidence_sha256) is not None,
            "invalid evidence hash",
        )
        _require(type(self.version) is int and self.version == SCHEMA_VERSION, "unsupported receipt version")

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True)
class ValidationDecision:
    """Input from a future independent work validator, not self-attestation."""

    decision_id: str
    receipt_id: str
    receipt_digest: str
    verifier_id: str
    approved_charge: int
    fee_units: int

    def __post_init__(self) -> None:
        for value in (self.decision_id, self.receipt_id, self.verifier_id):
            _id(value)
        _require(
            type(self.receipt_digest) is str and _HASH.fullmatch(self.receipt_digest) is not None,
            "invalid receipt digest",
        )
        _amount(self.approved_charge)
        _amount(self.fee_units)
        _require(self.fee_units <= self.approved_charge, "fee exceeds charge")

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


class ShadowLedger:
    """Durable, serialized double-entry store for noncash test balances.

    One SQLite transaction covers each hold, decision, or settlement.  WAL plus
    FULL synchronous mode bounds ordinary process-crash recovery for a local
    file; deployment-level backup, disk-loss, and replica durability are open.
    """

    def __init__(self, path: str | Path):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), timeout=5, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY, kind TEXT NOT NULL, balance INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS postings (
                event_id TEXT NOT NULL REFERENCES events(event_id), ordinal INTEGER NOT NULL,
                account_id TEXT NOT NULL REFERENCES accounts(account_id), delta INTEGER NOT NULL,
                PRIMARY KEY(event_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS reservations (
                request_id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL, cap INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('held', 'settled'))
            );
            CREATE TABLE IF NOT EXISTS quotes (
                request_id TEXT PRIMARY KEY REFERENCES reservations(request_id),
                terms_json TEXT NOT NULL, terms_digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS legacy_unquoted (
                request_id TEXT PRIMARY KEY REFERENCES reservations(request_id)
            );
            CREATE TABLE IF NOT EXISTS receipts (
                receipt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES reservations(request_id),
                provider_id TEXT NOT NULL, stage_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                claim_digest TEXT NOT NULL, proposed_charge INTEGER NOT NULL,
                input_units INTEGER, output_units INTEGER,
                status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
                approved_charge INTEGER, fee_units INTEGER, decision_id TEXT UNIQUE,
                decision_digest TEXT, rejection_reason TEXT,
                UNIQUE(request_id, provider_id, stage_id, attempt_id)
            );
            """
        )
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                version = self._db.execute("PRAGMA user_version").fetchone()[0]
                _require(version in (0, 1, LEDGER_SCHEMA_VERSION), "unsupported ledger schema")
                receipt_columns = {row[1] for row in self._db.execute("PRAGMA table_info(receipts)")}
                for column in ("input_units", "output_units"):
                    if column not in receipt_columns:
                        self._db.execute(f"ALTER TABLE receipts ADD COLUMN {column} INTEGER")
                if version == 0:
                    count = self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                    _require(count == 0, "unversioned ledger")
                elif version == 1:
                    self._db.execute(
                        "INSERT OR IGNORE INTO legacy_unquoted "
                        "SELECT request_id FROM reservations WHERE request_id NOT IN (SELECT request_id FROM quotes)"
                    )
                if version != LEDGER_SCHEMA_VERSION:
                    self._db.execute(f"PRAGMA user_version={LEDGER_SCHEMA_VERSION}")
                self._account("test_issuance", "issuance")
                self._account("fees", "fees")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> ShadowLedger:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _account(self, account_id: str, kind: str) -> None:
        row = self._db.execute("SELECT kind FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            self._db.execute("INSERT INTO accounts VALUES (?, ?, 0)", (account_id, kind))
        else:
            _require(row[0] == kind, "account kind conflict")

    def _begin(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")

    def _load_quote(self, request_id: str) -> ShadowQuote:
        row = self._db.execute(
            "SELECT terms_json, terms_digest FROM quotes WHERE request_id=?", (request_id,)
        ).fetchone()
        _require(row is not None, "request has no bound quote")
        try:
            terms = json.loads(row[0])
            _require(type(terms) is dict and _canonical_json(terms) == row[0], "noncanonical quote terms")
            quote = ShadowQuote(**terms)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShadowCreditError("invalid stored quote") from exc
        _require(quote.request_id == request_id and quote.digest == row[1], "quote binding mismatch")
        reservation = self._db.execute(
            "SELECT buyer_id, cap FROM reservations WHERE request_id=?", (request_id,)
        ).fetchone()
        _require(
            reservation == (quote.buyer_id, quote.spend_cap),
            "quote reservation mismatch",
        )
        event = self._db.execute(
            "SELECT payload_digest FROM events WHERE event_id=?", ("reserve:" + request_id,)
        ).fetchone()
        _require(
            event is not None and event[0] == _digest([quote.buyer_id, quote.spend_cap, quote.digest]),
            "reserve event quote mismatch",
        )
        return quote

    def _post(self, event_id: str, kind: str, postings: list[tuple[str, int]], payload: object) -> bool:
        """Write a balanced event while the caller holds a write transaction."""
        _id(event_id)
        _require(sum(delta for _, delta in postings) == 0, "unbalanced event")
        digest = _digest(payload)
        old = self._db.execute("SELECT kind, payload_digest FROM events WHERE event_id=?", (event_id,)).fetchone()
        if old is not None:
            _require(old == (kind, digest), "conflicting event replay")
            return False
        current: dict[str, int] = {}
        for account_id, delta in postings:
            row = self._db.execute("SELECT kind, balance FROM accounts WHERE account_id=?", (account_id,)).fetchone()
            _require(row is not None, "unknown account")
            before = current.get(account_id, row[1])
            after = before + delta
            _require(-MAX_CREDITS <= after <= MAX_CREDITS, "balance overflow")
            _require(row[0] == "issuance" or after >= 0, "insufficient balance")
            current[account_id] = after
        self._db.execute("INSERT INTO events VALUES (?, ?, ?)", (event_id, kind, digest))
        for ordinal, (account_id, delta) in enumerate(postings):
            self._db.execute("INSERT INTO postings VALUES (?, ?, ?, ?)", (event_id, ordinal, account_id, delta))
        for account_id, balance in current.items():
            self._db.execute("UPDATE accounts SET balance=? WHERE account_id=?", (balance, account_id))
        return True

    def grant_test_credits(self, grant_id: str, buyer_id: str, amount: int) -> bool:
        """Explicitly mint noncash fixture units; returns false for exact replay."""
        _id(grant_id)
        _id(buyer_id)
        _amount(amount, positive=True)
        with self._lock:
            self._begin()
            try:
                buyer = "buyer:" + buyer_id
                self._account(buyer, "buyer")
                result = self._post(
                    "grant:" + grant_id,
                    "test_grant",
                    [("test_issuance", -amount), (buyer, amount)],
                    [buyer_id, amount],
                )
                self._db.execute("COMMIT")
                return result
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def reserve(self, quote: ShadowQuote) -> bool:
        """Atomically hold a bound request's maximum noncash test spend."""
        _require(type(quote) is ShadowQuote, "bound quote required")
        request_id, buyer_id, cap = quote.request_id, quote.buyer_id, quote.spend_cap
        with self._lock:
            self._begin()
            try:
                old = self._db.execute(
                    "SELECT v.buyer_id, v.cap, q.terms_digest FROM reservations v "
                    "LEFT JOIN quotes q USING(request_id) WHERE v.request_id=?",
                    (request_id,),
                ).fetchone()
                if old is not None:
                    self._load_quote(request_id)
                    _require(old == (buyer_id, cap, quote.digest), "conflicting reservation replay")
                    self._db.execute("COMMIT")
                    return False
                _require(quote.expires_at_unix > time.time(), "quote expired")
                hold = "hold:" + request_id
                self._account(hold, "hold")
                self._post(
                    "reserve:" + request_id,
                    "reserve",
                    [("buyer:" + buyer_id, -cap), (hold, cap)],
                    [buyer_id, cap, quote.digest],
                )
                self._db.execute("INSERT INTO reservations VALUES (?, ?, ?, 'held')", (request_id, buyer_id, cap))
                self._db.execute(
                    "INSERT INTO quotes VALUES (?, ?, ?)",
                    (request_id, _canonical_json(asdict(quote)), quote.digest),
                )
                self._db.execute("COMMIT")
                return True
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def submit_receipt(self, receipt: WorkReceipt) -> bool:
        _require(type(receipt) is WorkReceipt, "invalid receipt")
        with self._lock:
            self._begin()
            try:
                old = self._db.execute(
                    "SELECT claim_digest FROM receipts WHERE receipt_id=?", (receipt.receipt_id,)
                ).fetchone()
                if old is not None:
                    _require(old[0] == receipt.digest, "conflicting receipt replay")
                    self._db.execute("COMMIT")
                    return False
                prior_attempt = self._db.execute(
                    "SELECT receipt_id FROM receipts WHERE request_id=? AND provider_id=? "
                    "AND stage_id=? AND attempt_id=?",
                    (receipt.request_id, receipt.provider_id, receipt.stage_id, receipt.attempt_id),
                ).fetchone()
                _require(prior_attempt is None, "duplicate work attempt")
                reservation = self._db.execute(
                    "SELECT cap, status FROM reservations WHERE request_id=?", (receipt.request_id,)
                ).fetchone()
                _require(reservation is not None and reservation[1] == "held", "request not held")
                _require(receipt.proposed_charge <= reservation[0], "receipt exceeds cap")
                quote = self._load_quote(receipt.request_id)
                _require(
                    receipt.input_units <= quote.max_input_units and receipt.output_units <= quote.max_output_units,
                    "receipt exceeds quoted units",
                )
                _require(
                    receipt.proposed_charge
                    <= receipt.input_units * quote.input_unit_price + receipt.output_units * quote.output_unit_price,
                    "receipt exceeds quoted rates",
                )
                active_units = self._db.execute(
                    "SELECT input_units, output_units FROM receipts WHERE request_id=? AND status!='rejected'",
                    (receipt.request_id,),
                ).fetchall()
                _require(all(None not in pair for pair in active_units), "missing quoted receipt units")
                used_input = sum(pair[0] for pair in active_units)
                used_output = sum(pair[1] for pair in active_units)
                _require(
                    used_input + receipt.input_units <= quote.max_input_units
                    and used_output + receipt.output_units <= quote.max_output_units,
                    "receipt exceeds aggregate quoted units",
                )
                self._db.execute(
                    "INSERT INTO receipts (receipt_id, request_id, provider_id, stage_id, attempt_id, "
                    "claim_digest, proposed_charge, input_units, output_units, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (
                        receipt.receipt_id,
                        receipt.request_id,
                        receipt.provider_id,
                        receipt.stage_id,
                        receipt.attempt_id,
                        receipt.digest,
                        receipt.proposed_charge,
                        receipt.input_units,
                        receipt.output_units,
                    ),
                )
                self._db.execute("COMMIT")
                return True
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def approve_receipt(self, decision: ValidationDecision) -> bool:
        _require(type(decision) is ValidationDecision, "invalid decision")
        with self._lock:
            self._begin()
            try:
                row = self._db.execute(
                    "SELECT r.request_id, r.claim_digest, r.proposed_charge, r.status, r.decision_digest, "
                    "v.cap, v.status, r.provider_id "
                    "FROM receipts r JOIN reservations v ON v.request_id=r.request_id WHERE r.receipt_id=?",
                    (decision.receipt_id,),
                ).fetchone()
                _require(row is not None, "unknown receipt")
                request_id, claim_digest, proposed, status, old_decision, cap, reservation_status, provider_id = row
                _require(claim_digest == decision.receipt_digest, "receipt digest mismatch")
                if status == "approved":
                    _require(old_decision == decision.digest, "conflicting decision replay")
                    self._db.execute("COMMIT")
                    return False
                _require(status == "pending" and reservation_status == "held", "receipt not pending")
                quote = self._load_quote(request_id)
                _require(decision.verifier_id != provider_id, "provider cannot verify own work")
                _require(decision.approved_charge <= proposed, "approval exceeds claim")
                _require(
                    decision.fee_units <= decision.approved_charge * quote.fee_bps // 10_000,
                    "approval exceeds quoted fee",
                )
                prior_decision = self._db.execute(
                    "SELECT receipt_id FROM receipts WHERE decision_id=?", (decision.decision_id,)
                ).fetchone()
                _require(prior_decision is None, "decision ID already used")
                approved = self._db.execute(
                    "SELECT COALESCE(SUM(approved_charge), 0) FROM receipts WHERE request_id=? AND status='approved'",
                    (request_id,),
                ).fetchone()[0]
                _require(approved + decision.approved_charge <= cap, "approved work exceeds hold")
                self._account("provider_pending:" + provider_id, "provider_pending")
                self._db.execute(
                    "UPDATE receipts SET status='approved', approved_charge=?, fee_units=?, "
                    "decision_id=?, decision_digest=? "
                    "WHERE receipt_id=?",
                    (
                        decision.approved_charge,
                        decision.fee_units,
                        decision.decision_id,
                        decision.digest,
                        decision.receipt_id,
                    ),
                )
                self._db.execute("COMMIT")
                return True
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def reject_receipt(self, receipt_id: str, reason: str) -> bool:
        _id(receipt_id)
        _id(reason)
        with self._lock:
            self._begin()
            try:
                row = self._db.execute(
                    "SELECT status, rejection_reason FROM receipts WHERE receipt_id=?", (receipt_id,)
                ).fetchone()
                _require(row is not None, "unknown receipt")
                if row[0] == "rejected":
                    _require(row[1] == reason, "conflicting rejection replay")
                    self._db.execute("COMMIT")
                    return False
                _require(row[0] == "pending", "receipt not pending")
                self._db.execute(
                    "UPDATE receipts SET status='rejected', rejection_reason=? WHERE receipt_id=?",
                    (reason, receipt_id),
                )
                self._db.execute("COMMIT")
                return True
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def finalize(self, request_id: str) -> bool:
        """Settle approved work and release unused hold in one balanced event."""
        _id(request_id)
        with self._lock:
            self._begin()
            try:
                row = self._db.execute(
                    "SELECT buyer_id, cap, status FROM reservations WHERE request_id=?", (request_id,)
                ).fetchone()
                _require(row is not None, "unknown reservation")
                buyer_id, cap, status = row
                if status == "settled":
                    self._db.execute("COMMIT")
                    return False
                pending = self._db.execute(
                    "SELECT COUNT(*) FROM receipts WHERE request_id=? AND status='pending'", (request_id,)
                ).fetchone()[0]
                _require(pending == 0, "pending receipts remain")
                rows = self._db.execute(
                    "SELECT provider_id, approved_charge, fee_units FROM receipts "
                    "WHERE request_id=? AND status='approved' ORDER BY receipt_id",
                    (request_id,),
                ).fetchall()
                charge = sum(item[1] for item in rows)
                fees = sum(item[2] for item in rows)
                _require(charge <= cap, "settlement exceeds hold")
                by_provider: dict[str, int] = {}
                for provider_id, approved_charge, fee_units in rows:
                    by_provider[provider_id] = by_provider.get(provider_id, 0) + approved_charge - fee_units
                postings = [("hold:" + request_id, -cap), ("buyer:" + buyer_id, cap - charge)]
                postings.extend(
                    ("provider_pending:" + provider_id, value) for provider_id, value in sorted(by_provider.items())
                )
                postings.append(("fees", fees))
                self._post("finalize:" + request_id, "finalize", postings, [request_id, buyer_id, cap, rows])
                self._db.execute("UPDATE reservations SET status='settled' WHERE request_id=?", (request_id,))
                self._db.execute("COMMIT")
                return True
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def balance(self, account_id: str) -> int:
        _id(account_id)
        with self._lock:
            row = self._db.execute("SELECT balance FROM accounts WHERE account_id=?", (account_id,)).fetchone()
            _require(row is not None, "unknown account")
            return row[0]

    def buyer_wallet(self, buyer_id: str, *, recent_limit: int = 20) -> dict[str, object]:
        """Read one consistent, content-free snapshot of a buyer's test units.

        Recent events use SQLite's local insertion order for display only; the
        row ID is deliberately not exposed as a durable pagination cursor.
        This is not an authenticated wallet API or a payment balance.
        """
        _id(buyer_id)
        _require(type(recent_limit) is int and 1 <= recent_limit <= 100, "invalid wallet history limit")
        with self._lock:
            self._db.execute("BEGIN")
            try:
                buyer_account = "buyer:" + buyer_id
                row = self._db.execute("SELECT balance FROM accounts WHERE account_id=?", (buyer_account,)).fetchone()
                _require(row is not None, "unknown buyer")
                available = row[0]
                held = self._db.execute(
                    "SELECT COALESCE(SUM(cap), 0) FROM reservations WHERE buyer_id=? AND status='held'",
                    (buyer_id,),
                ).fetchone()[0]
                pending, approved_unsettled, settled_spend = self._db.execute(
                    "SELECT "
                    "COUNT(CASE WHEN r.status='pending' THEN 1 END), "
                    "COALESCE(SUM(CASE WHEN r.status='approved' AND v.status='held' "
                    "THEN r.approved_charge ELSE 0 END), 0), "
                    "COALESCE(SUM(CASE WHEN r.status='approved' AND v.status='settled' "
                    "THEN r.approved_charge ELSE 0 END), 0) "
                    "FROM reservations v LEFT JOIN receipts r USING(request_id) WHERE v.buyer_id=?",
                    (buyer_id,),
                ).fetchone()
                events = self._db.execute(
                    "SELECT e.event_id, e.kind, p.delta FROM events e "
                    "JOIN postings p USING(event_id) WHERE p.account_id=? "
                    "ORDER BY e.rowid DESC LIMIT ?",
                    (buyer_account, recent_limit),
                ).fetchall()
                result = {
                    "unit": "test_credit",
                    "buyer_id": buyer_id,
                    "available": available,
                    "held": held,
                    "pending_receipts": pending,
                    "approved_unsettled": approved_unsettled,
                    "settled_spend": settled_spend,
                    "recent_events": [
                        {"event_id": event_id, "kind": kind, "available_delta": delta}
                        for event_id, kind, delta in events
                    ],
                }
                self._db.execute("COMMIT")
                return result
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def provider_wallet(self, provider_id: str, *, recent_limit: int = 20) -> dict[str, object]:
        """Show one provider's noncash claims and settled pending test units.

        A settled pending unit is still ineligible for payout or reuse. This
        view has no provider authentication and must not be exposed directly.
        """
        _id(provider_id)
        _require(type(recent_limit) is int and 1 <= recent_limit <= 100, "invalid wallet history limit")
        with self._lock:
            self._db.execute("BEGIN")
            try:
                account_id = "provider_pending:" + provider_id
                row = self._db.execute("SELECT balance FROM accounts WHERE account_id=?", (account_id,)).fetchone()
                total, pending_review, approved_unsettled, rejected = self._db.execute(
                    "SELECT COUNT(*), "
                    "COUNT(CASE WHEN r.status='pending' THEN 1 END), "
                    "COALESCE(SUM(CASE WHEN r.status='approved' AND v.status='held' "
                    "THEN r.approved_charge ELSE 0 END), 0), "
                    "COUNT(CASE WHEN r.status='rejected' THEN 1 END) "
                    "FROM receipts r JOIN reservations v USING(request_id) WHERE r.provider_id=?",
                    (provider_id,),
                ).fetchone()
                _require(total > 0 or row is not None, "unknown provider")
                events = self._db.execute(
                    "SELECT e.event_id, e.kind, p.delta FROM events e "
                    "JOIN postings p USING(event_id) WHERE p.account_id=? "
                    "ORDER BY e.rowid DESC LIMIT ?",
                    (account_id, recent_limit),
                ).fetchall()
                result = {
                    "unit": "test_credit",
                    "provider_id": provider_id,
                    "pending_review_receipts": pending_review,
                    "approved_unsettled": approved_unsettled,
                    "rejected_receipts": rejected,
                    "settled_pending_balance": row[0] if row is not None else 0,
                    "recent_events": [
                        {"event_id": event_id, "kind": kind, "pending_delta": delta} for event_id, kind, delta in events
                    ],
                }
                self._db.execute("COMMIT")
                return result
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def audit(self) -> dict[str, int]:
        """Check materialized balances, conservation, and held reservations."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                accounts = self._db.execute("SELECT account_id, kind, balance FROM accounts").fetchall()
                _require(sum(row[2] for row in accounts) == 0, "ledger imbalance")
                for account_id, kind, balance in accounts:
                    posted = self._db.execute(
                        "SELECT COALESCE(SUM(delta), 0) FROM postings WHERE account_id=?", (account_id,)
                    ).fetchone()[0]
                    _require(balance == posted and (kind == "issuance" or balance >= 0), "account imbalance")
                for (event_id,) in self._db.execute("SELECT event_id FROM events"):
                    total = self._db.execute(
                        "SELECT COALESCE(SUM(delta), 0) FROM postings WHERE event_id=?", (event_id,)
                    ).fetchone()[0]
                    _require(total == 0, "event imbalance")
                for request_id, cap, status in self._db.execute("SELECT request_id, cap, status FROM reservations"):
                    held = self.balance("hold:" + request_id)
                    _require(held == (cap if status == "held" else 0), "reservation imbalance")
                    quoted = self._db.execute("SELECT 1 FROM quotes WHERE request_id=?", (request_id,)).fetchone()
                    legacy = self._db.execute(
                        "SELECT 1 FROM legacy_unquoted WHERE request_id=?", (request_id,)
                    ).fetchone()
                    _require((quoted is not None) != (legacy is not None), "reservation quote state is invalid")
                    if quoted is not None:
                        quote = self._load_quote(request_id)
                        units = self._db.execute(
                            "SELECT input_units, output_units FROM receipts "
                            "WHERE request_id=? AND status!='rejected'",
                            (request_id,),
                        ).fetchall()
                        _require(
                            all(None not in pair for pair in units)
                            and sum(pair[0] for pair in units) <= quote.max_input_units
                            and sum(pair[1] for pair in units) <= quote.max_output_units,
                            "receipt units exceed quote",
                        )
                    approved, pending = self._db.execute(
                        "SELECT COALESCE(SUM(CASE WHEN status='approved' THEN approved_charge ELSE 0 END), 0), "
                        "COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), 0) "
                        "FROM receipts WHERE request_id=?",
                        (request_id,),
                    ).fetchone()
                    _require(approved <= cap and (status == "held" or pending == 0), "receipt imbalance")
                result = {
                    "accounts": len(accounts),
                    "events": self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                    "reservations": self._db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0],
                    "receipts": self._db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],
                    "quotes": self._db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0],
                    "legacy_unquoted": self._db.execute("SELECT COUNT(*) FROM legacy_unquoted").fetchone()[0],
                }
                self._db.execute("COMMIT")
                return result
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
