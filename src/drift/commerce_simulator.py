"""Durable, provider-neutral noncash marketplace lifecycle simulator.

Amounts are synthetic integer units. Calls labelled ``verified`` assume a
future adapter has already authenticated and reconciled the external event;
this module contains no payment credentials, network calls, cash balance or
live endpoint. Its purpose is to test accounting and failure transitions.
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
MAX_UNITS = 2**62 - 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_JOURNAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}\Z")


class CommerceSimulationError(ValueError):
    pass


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise CommerceSimulationError(message)


def _id(value: object) -> str:
    _require(type(value) is str and _ID.fullmatch(value) is not None, "invalid simulator identifier")
    return value


def _amount(value: object, *, positive: bool = False) -> int:
    _require(type(value) is int and 0 <= value <= MAX_UNITS and (not positive or value > 0), "invalid amount")
    return value


def _digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def _buyer_account(buyer_id: str, source: str) -> str:
    _require(type(source) is str and source in {"purchased", "earned"}, "invalid buyer funding provenance")
    return "buyer_" + source + ":" + buyer_id


@dataclass(frozen=True)
class SimulatedServiceQuote:
    """Immutable noncash service terms; an order or quote alone funds nothing."""

    request_id: str
    buyer_id: str
    provider_id: str
    funding_source: str
    model_id: str
    profile_id: str
    service_class: str
    settlement_domain: str
    artifact_sha256: str
    service_policy_sha256: str
    price_schedule_sha256: str
    input_unit_price: int
    output_unit_price: int
    max_input_units: int
    max_output_units: int
    fee_bps: int
    spend_cap: int
    expires_at_unix: int
    version: int = 1

    def __post_init__(self) -> None:
        _id(self.request_id); _id(self.buyer_id); _id(self.provider_id)
        _require(self.buyer_id != self.provider_id, "self-provided service is ineligible")
        _buyer_account(self.buyer_id, self.funding_source)
        _id(self.service_class)
        _require(self.settlement_domain == "local_simulation", "invalid settlement domain")
        for value in (self.model_id, self.profile_id):
            _require(type(value) is str and _MODEL_ID.fullmatch(value) is not None, "invalid quote profile")
        for value in (self.artifact_sha256, self.service_policy_sha256, self.price_schedule_sha256):
            _require(type(value) is str and _HASH.fullmatch(value) is not None, "invalid quote digest")
        for value in (self.input_unit_price, self.output_unit_price,
                      self.max_input_units, self.max_output_units):
            _amount(value)
        _amount(self.spend_cap, positive=True)
        _require(
            0 < self.input_unit_price * self.max_input_units
            + self.output_unit_price * self.max_output_units <= self.spend_cap,
            "quote cap does not cover maximum priced work",
        )
        _require(type(self.fee_bps) is int and 0 <= self.fee_bps <= 10_000, "invalid quoted fee")
        _require(type(self.expires_at_unix) is int and 0 < self.expires_at_unix < 2**53,
                 "invalid quote expiry")
        _require(type(self.version) is int and self.version == 1, "unsupported quote version")

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


class CommerceSimulator:
    """One-writer SQLite journal for simulated buy/use/earn/withdraw transitions."""

    def __init__(self, path: str | Path) -> None:
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
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL, processor_ref TEXT NOT NULL UNIQUE,
                amount INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','captured','reversed')),
                capture_event_id TEXT, reversal_event_id TEXT
            );
            CREATE TABLE IF NOT EXISTS inbox (
                event_id TEXT PRIMARY KEY, processor_ref TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('capture','reversal')),
                amount INTEGER NOT NULL, payload_digest TEXT NOT NULL,
                processed INTEGER NOT NULL CHECK(processed IN (0,1))
            );
            CREATE INDEX IF NOT EXISTS inbox_by_ref ON inbox(processor_ref);
            CREATE TABLE IF NOT EXISTS reservations (
                request_id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source IN ('purchased','earned')),
                cap INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('held','settled','refunded')),
                provider_id TEXT, charge INTEGER, fee INTEGER, decision_id TEXT UNIQUE,
                receipt_digest TEXT, refund_reason TEXT, input_units INTEGER, output_units INTEGER
            );
            CREATE TABLE IF NOT EXISTS quotes (
                request_id TEXT PRIMARY KEY REFERENCES reservations(request_id),
                terms_json TEXT NOT NULL, terms_digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS releases (
                release_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, amount INTEGER NOT NULL,
                risk_decision_id TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS payouts (
                payout_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, amount INTEGER NOT NULL,
                external_ref TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK(status IN ('reserved','unknown','paid','failed')),
                outcome_event_id TEXT UNIQUE
            );
            """
        )
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                version = self._db.execute("PRAGMA user_version").fetchone()[0]
                _require(version in (0, SCHEMA_VERSION), "unsupported commerce simulator schema")
                if version == 0:
                    _require(self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0,
                             "unversioned commerce journal")
                    self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self._account("external_funding", "external")
                self._account("operator_loss", "loss")
                self._account("fees", "fees")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> CommerceSimulator:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _account(self, account_id: str, kind: str) -> None:
        row = self._db.execute("SELECT kind FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            self._db.execute("INSERT INTO accounts VALUES (?,?,0)", (account_id, kind))
        else:
            _require(row[0] == kind, "account kind conflict")

    def _balance(self, account_id: str) -> int:
        row = self._db.execute("SELECT balance FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        return 0 if row is None else row[0]

    def _load_quote(self, request_id: str) -> SimulatedServiceQuote:
        row = self._db.execute(
            "SELECT terms_json,terms_digest FROM quotes WHERE request_id=?", (request_id,)
        ).fetchone()
        _require(row is not None, "reservation has no bound quote")
        try:
            terms = json.loads(row[0])
            _require(type(terms) is dict, "invalid stored quote terms")
            _require(json.dumps(terms, sort_keys=True, separators=(",", ":"), ensure_ascii=True) == row[0],
                     "noncanonical quote")
            quote = SimulatedServiceQuote(**terms)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CommerceSimulationError("invalid stored quote") from exc
        _require(quote.request_id == request_id and quote.digest == row[1], "quote binding mismatch")
        reservation = self._db.execute(
            "SELECT buyer_id,source,cap FROM reservations WHERE request_id=?", (request_id,)
        ).fetchone()
        _require(reservation == (quote.buyer_id, quote.funding_source, quote.spend_cap),
                 "quote reservation mismatch")
        return quote

    def _post(self, event_id: str, kind: str, postings: list[tuple[str, int]], payload: object) -> bool:
        _require(_JOURNAL_ID.fullmatch(event_id) is not None, "invalid journal event identifier")
        _require(sum(delta for _, delta in postings) == 0, "unbalanced event")
        digest = _digest(payload)
        old = self._db.execute("SELECT kind,payload_digest FROM events WHERE event_id=?", (event_id,)).fetchone()
        if old is not None:
            _require(old == (kind, digest), "conflicting journal replay")
            return False
        next_balances: dict[str, int] = {}
        for account_id, delta in postings:
            row = self._db.execute("SELECT kind,balance FROM accounts WHERE account_id=?", (account_id,)).fetchone()
            _require(row is not None and type(delta) is int, "unknown account or invalid posting")
            after = next_balances.get(account_id, row[1]) + delta
            _require(-MAX_UNITS <= after <= MAX_UNITS, "balance overflow")
            _require(row[0] in {"external", "loss"} or after >= 0, "insufficient balance")
            next_balances[account_id] = after
        self._db.execute("INSERT INTO events VALUES (?,?,?)", (event_id, kind, digest))
        for ordinal, (account_id, delta) in enumerate(postings):
            self._db.execute("INSERT INTO postings VALUES (?,?,?,?)", (event_id, ordinal, account_id, delta))
        for account_id, balance in next_balances.items():
            self._db.execute("UPDATE accounts SET balance=? WHERE account_id=?", (balance, account_id))
        return True

    def _apply_order(self, processor_ref: str) -> None:
        order = self._db.execute(
            "SELECT order_id,buyer_id,amount,status FROM orders WHERE processor_ref=?", (processor_ref,)
        ).fetchone()
        if order is None:
            return
        order_id, buyer_id, amount, status = order
        rows = self._db.execute(
            "SELECT event_id,kind,amount FROM inbox WHERE processor_ref=? ORDER BY rowid", (processor_ref,)
        ).fetchall()
        _require(all(row[2] == amount for row in rows), "processor event amount conflicts with order")
        captures = [row[0] for row in rows if row[1] == "capture"]
        reversals = [row[0] for row in rows if row[1] == "reversal"]
        buyer = _buyer_account(buyer_id, "purchased")
        if status == "pending" and captures:
            self._account(buyer, "buyer_purchased")
            self._post("capture:" + order_id, "capture", [("external_funding", -amount), (buyer, amount)],
                       [order_id, processor_ref, amount])
            self._db.execute(
                "UPDATE orders SET status='captured',capture_event_id=? WHERE order_id=?", (captures[0], order_id)
            )
            status = "captured"
        if status == "captured" and reversals:
            recovered = min(self._balance(buyer), amount)
            self._post(
                "reversal:" + order_id, "reversal",
                [(buyer, -recovered), ("operator_loss", -(amount - recovered)), ("external_funding", amount)],
                [order_id, processor_ref, amount, recovered],
            )
            self._db.execute(
                "UPDATE orders SET status='reversed',reversal_event_id=? WHERE order_id=?", (reversals[0], order_id)
            )
            status = "reversed"
        if status in {"captured", "reversed"}:
            self._db.execute(
                "UPDATE inbox SET processed=1 WHERE processor_ref=? AND kind='capture'", (processor_ref,)
            )
        if status == "reversed":
            self._db.execute(
                "UPDATE inbox SET processed=1 WHERE processor_ref=? AND kind='reversal'", (processor_ref,)
            )

    def create_order(self, order_id: str, buyer_id: str, processor_ref: str, amount: int) -> bool:
        """Create a simulated purchase intent; no access is credited yet."""
        _id(order_id); _id(buyer_id); _id(processor_ref); _amount(amount, positive=True)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                old = self._db.execute(
                    "SELECT buyer_id,processor_ref,amount FROM orders WHERE order_id=?", (order_id,)
                ).fetchone()
                if old is not None:
                    _require(old == (buyer_id, processor_ref, amount), "conflicting order replay")
                    inserted = False
                else:
                    collision = self._db.execute(
                        "SELECT order_id FROM orders WHERE processor_ref=?", (processor_ref,)
                    ).fetchone()
                    _require(collision is None, "processor reference already bound")
                    self._db.execute(
                        "INSERT INTO orders VALUES (?,?,?,?,'pending',NULL,NULL)",
                        (order_id, buyer_id, processor_ref, amount),
                    )
                    inserted = True
                self._apply_order(processor_ref)
                self._db.execute("COMMIT")
                return inserted
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def record_verified_processor_event(self, event_id: str, processor_ref: str, kind: str, amount: int) -> bool:
        """Ingest an event already authenticated by a future processor adapter.

        Reordered reversal-before-capture is held in the durable inbox. A new
        event ID for the same economic capture/reversal never posts twice.
        """
        _id(event_id); _id(processor_ref); _amount(amount, positive=True)
        _require(type(kind) is str and kind in {"capture", "reversal"}, "invalid processor event kind")
        digest = _digest([processor_ref, kind, amount])
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                old = self._db.execute(
                    "SELECT processor_ref,kind,amount,payload_digest FROM inbox WHERE event_id=?", (event_id,)
                ).fetchone()
                if old is not None:
                    _require(old == (processor_ref, kind, amount, digest), "conflicting processor event replay")
                    inserted = False
                else:
                    self._db.execute(
                        "INSERT INTO inbox VALUES (?,?,?,?,?,0)", (event_id, processor_ref, kind, amount, digest)
                    )
                    inserted = True
                self._apply_order(processor_ref)
                self._db.execute("COMMIT")
                return inserted
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def reserve_service(self, quote: SimulatedServiceQuote) -> bool:
        """Reserve a bound synthetic quote from one funding provenance."""
        _require(type(quote) is SimulatedServiceQuote, "bound service quote required")
        request_id, buyer_id, source, cap = (
            quote.request_id, quote.buyer_id, quote.funding_source, quote.spend_cap
        )
        buyer = _buyer_account(buyer_id, source)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                old = self._db.execute(
                    "SELECT buyer_id,source,cap FROM reservations WHERE request_id=?", (request_id,)
                ).fetchone()
                if old is not None:
                    stored = self._load_quote(request_id)
                    _require(old == (buyer_id, source, cap) and stored.digest == quote.digest,
                             "conflicting service reservation replay")
                    self._db.execute("COMMIT")
                    return False
                _require(self._balance("operator_loss") == 0, "unfunded reversal loss blocks service admission")
                _require(quote.expires_at_unix > time.time(), "service quote expired")
                hold = "service_hold:" + request_id
                self._account(hold, "service_hold")
                self._post("reserve:" + request_id, "reserve", [(buyer, -cap), (hold, cap)],
                           [request_id, buyer_id, source, cap, quote.digest])
                self._db.execute(
                    "INSERT INTO reservations(request_id,buyer_id,source,cap,status) VALUES (?,?,?,?,'held')",
                    (request_id, buyer_id, source, cap),
                )
                self._db.execute(
                    "INSERT INTO quotes VALUES (?,?,?)",
                    (request_id,
                     json.dumps(asdict(quote), sort_keys=True, separators=(",", ":"), ensure_ascii=True),
                     quote.digest),
                )
                self._db.execute("COMMIT")
                return True
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def settle_service(
        self, request_id: str, provider_id: str, charge: int, fee: int,
        decision_id: str, receipt_digest: str, input_units: int, output_units: int,
    ) -> bool:
        """Apply a simulated independent work decision, not a self-reported claim."""
        _id(request_id); _id(provider_id); _id(decision_id)
        _amount(charge, positive=True); _amount(fee); _amount(input_units); _amount(output_units)
        _require(fee <= charge and type(receipt_digest) is str and _HASH.fullmatch(receipt_digest) is not None,
                 "invalid settlement decision")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT buyer_id,source,cap,status,provider_id,charge,fee,decision_id,receipt_digest,"
                    "input_units,output_units "
                    "FROM reservations WHERE request_id=?", (request_id,)
                ).fetchone()
                _require(row is not None, "unknown service reservation")
                (buyer_id, source, cap, status, old_provider, old_charge, old_fee,
                 old_decision, old_receipt, old_input, old_output) = row
                if status == "settled":
                    _require((old_provider, old_charge, old_fee, old_decision, old_receipt, old_input, old_output)
                             == (provider_id, charge, fee, decision_id, receipt_digest,
                                 input_units, output_units),
                             "conflicting settlement replay")
                    self._db.execute("COMMIT")
                    return False
                _require(status == "held" and charge <= cap, "service is not held within cap")
                quote = self._load_quote(request_id)
                _require(provider_id == quote.provider_id, "provider differs from quoted route")
                _require(
                    input_units <= quote.max_input_units
                    and output_units <= quote.max_output_units
                    and charge <= input_units * quote.input_unit_price
                    + output_units * quote.output_unit_price,
                    "settlement exceeds quoted units or rates",
                )
                _require(fee <= charge * quote.fee_bps // 10_000, "fee exceeds quoted limit")
                _require(self._db.execute(
                    "SELECT 1 FROM reservations WHERE decision_id=?", (decision_id,)
                ).fetchone() is None, "decision ID reused")
                pending = "provider_pending:" + provider_id
                self._account(pending, "provider_pending")
                self._post(
                    "settle:" + request_id, "settle",
                    [("service_hold:" + request_id, -cap),
                     (_buyer_account(buyer_id, source), cap - charge),
                     (pending, charge - fee), ("fees", fee)],
                    [request_id, provider_id, charge, fee, decision_id, receipt_digest,
                     input_units, output_units, quote.digest],
                )
                self._db.execute(
                    "UPDATE reservations SET status='settled',provider_id=?,charge=?,fee=?,"
                    "decision_id=?,receipt_digest=?,input_units=?,output_units=? WHERE request_id=?",
                    (provider_id, charge, fee, decision_id, receipt_digest,
                     input_units, output_units, request_id),
                )
                self._db.execute("COMMIT")
                return True
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def refund_service(self, request_id: str, reason: str) -> bool:
        """Release an unserved request's entire synthetic hold."""
        _id(request_id); _id(reason)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT buyer_id,source,cap,status,refund_reason FROM reservations WHERE request_id=?", (request_id,)
                ).fetchone()
                _require(row is not None, "unknown service reservation")
                buyer_id, source, cap, status, old_reason = row
                if status == "refunded":
                    _require(old_reason == reason, "conflicting refund replay")
                    self._db.execute("COMMIT")
                    return False
                _require(status == "held", "settled service cannot be refunded as unserved")
                self._post(
                    "refund_service:" + request_id, "service_refund",
                    [("service_hold:" + request_id, -cap), (_buyer_account(buyer_id, source), cap)],
                    [request_id, reason],
                )
                self._db.execute(
                    "UPDATE reservations SET status='refunded',refund_reason=? WHERE request_id=?", (reason, request_id)
                )
                self._db.execute("COMMIT")
                return True
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def release_earnings(self, release_id: str, provider_id: str, amount: int, risk_decision_id: str) -> bool:
        """Move a simulated settled payable into eligible earnings."""
        _id(release_id); _id(provider_id); _id(risk_decision_id); _amount(amount, positive=True)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                old = self._db.execute(
                    "SELECT provider_id,amount,risk_decision_id FROM releases WHERE release_id=?", (release_id,)
                ).fetchone()
                if old is not None:
                    _require(old == (provider_id, amount, risk_decision_id), "conflicting release replay")
                    self._db.execute("COMMIT")
                    return False
                _require(self._balance("operator_loss") == 0, "unfunded reversal loss blocks earning release")
                eligible = "provider_eligible:" + provider_id
                self._account(eligible, "provider_eligible")
                self._post(
                    "release:" + release_id, "earning_release",
                    [("provider_pending:" + provider_id, -amount), (eligible, amount)],
                    [release_id, provider_id, amount, risk_decision_id],
                )
                self._db.execute("INSERT INTO releases VALUES (?,?,?,?)",
                                 (release_id, provider_id, amount, risk_decision_id))
                self._db.execute("COMMIT")
                return True
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def convert_earnings(self, conversion_id: str, provider_id: str, amount: int) -> bool:
        """Use eligible earnings as a separate source of future access."""
        _id(conversion_id); _id(provider_id); _amount(amount, positive=True)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                prior = self._db.execute(
                    "SELECT 1 FROM events WHERE event_id=?", ("convert:" + conversion_id,)
                ).fetchone()
                if prior is None:
                    _require(self._balance("operator_loss") == 0, "unfunded reversal loss blocks conversion")
                earned = _buyer_account(provider_id, "earned")
                self._account(earned, "buyer_earned")
                result = self._post(
                    "convert:" + conversion_id, "earning_conversion",
                    [("provider_eligible:" + provider_id, -amount), (earned, amount)],
                    [conversion_id, provider_id, amount],
                )
                self._db.execute("COMMIT")
                return result
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def request_payout(self, payout_id: str, provider_id: str, amount: int, external_ref: str) -> bool:
        """Reserve eligible earnings; an unknown outcome must retain this hold."""
        _id(payout_id); _id(provider_id); _id(external_ref); _amount(amount, positive=True)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                old = self._db.execute(
                    "SELECT provider_id,amount,external_ref FROM payouts WHERE payout_id=?", (payout_id,)
                ).fetchone()
                if old is not None:
                    _require(old == (provider_id, amount, external_ref), "conflicting payout replay")
                    self._db.execute("COMMIT")
                    return False
                _require(self._balance("operator_loss") == 0, "unfunded reversal loss blocks payouts")
                hold = "payout_hold:" + payout_id
                self._account(hold, "payout_hold")
                self._post(
                    "payout_hold:" + payout_id, "payout_reserve",
                    [("provider_eligible:" + provider_id, -amount), (hold, amount)],
                    [payout_id, provider_id, amount, external_ref],
                )
                self._db.execute("INSERT INTO payouts VALUES (?,?,?,?,'reserved',NULL)",
                                 (payout_id, provider_id, amount, external_ref))
                self._db.execute("COMMIT")
                return True
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def mark_payout_unknown(self, payout_id: str) -> bool:
        _id(payout_id)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT status FROM payouts WHERE payout_id=?", (payout_id,)).fetchone()
                _require(row is not None, "unknown payout")
                if row[0] == "unknown":
                    self._db.execute("COMMIT")
                    return False
                _require(row[0] == "reserved", "terminal payout cannot become unknown")
                self._post("payout_unknown:" + payout_id, "payout_unknown", [], [payout_id])
                self._db.execute("UPDATE payouts SET status='unknown' WHERE payout_id=?", (payout_id,))
                self._db.execute("COMMIT")
                return True
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def resolve_payout(self, payout_id: str, outcome_event_id: str, *, paid: bool) -> bool:
        """Apply an externally verified final outcome, never a submission ACK."""
        _id(payout_id); _id(outcome_event_id)
        _require(type(paid) is bool, "invalid payout outcome")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT provider_id,amount,status,outcome_event_id FROM payouts WHERE payout_id=?", (payout_id,)
                ).fetchone()
                _require(row is not None, "unknown payout")
                provider_id, amount, status, prior_event = row
                desired = "paid" if paid else "failed"
                if status in {"paid", "failed"}:
                    _require((status, prior_event) == (desired, outcome_event_id), "conflicting payout outcome")
                    self._db.execute("COMMIT")
                    return False
                _require(status in {"reserved", "unknown"}, "invalid payout state")
                destination = "external_funding" if paid else "provider_eligible:" + provider_id
                self._post(
                    "payout_result:" + payout_id, "payout_" + desired,
                    [("payout_hold:" + payout_id, -amount), (destination, amount)],
                    [payout_id, outcome_event_id, desired],
                )
                self._db.execute(
                    "UPDATE payouts SET status=?,outcome_event_id=? WHERE payout_id=?",
                    (desired, outcome_event_id, payout_id),
                )
                self._db.execute("COMMIT")
                return True
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def buyer_wallet(self, buyer_id: str) -> dict[str, int | str]:
        _id(buyer_id)
        with self._lock:
            held = self._db.execute(
                "SELECT COALESCE(SUM(cap),0) FROM reservations WHERE buyer_id=? AND status='held'", (buyer_id,)
            ).fetchone()[0]
            return {
                "unit": "simulated_minor_unit", "buyer_id": buyer_id,
                "purchased_available": self._balance(_buyer_account(buyer_id, "purchased")),
                "earned_access_available": self._balance(_buyer_account(buyer_id, "earned")),
                "service_held": held,
            }

    def provider_wallet(self, provider_id: str) -> dict[str, int | str]:
        _id(provider_id)
        with self._lock:
            held = self._db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM payouts WHERE provider_id=? "
                "AND status IN ('reserved','unknown')", (provider_id,)
            ).fetchone()[0]
            return {
                "unit": "simulated_minor_unit", "provider_id": provider_id,
                "pending": self._balance("provider_pending:" + provider_id),
                "eligible": self._balance("provider_eligible:" + provider_id),
                "payout_held": held,
            }

    def unresolved_payouts(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple(self._db.execute(
                "SELECT payout_id,status FROM payouts WHERE status IN ('reserved','unknown') ORDER BY payout_id"
            ).fetchall())

    def audit(self) -> dict[str, int]:
        """Check journal conservation and materialized holds; no external reconciliation."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                accounts = self._db.execute("SELECT account_id,kind,balance FROM accounts").fetchall()
                _require(sum(row[2] for row in accounts) == 0, "journal is unbalanced")
                for account_id, kind, balance in accounts:
                    posted = self._db.execute(
                        "SELECT COALESCE(SUM(delta),0) FROM postings WHERE account_id=?", (account_id,)
                    ).fetchone()[0]
                    _require(balance == posted and (kind in {"external", "loss"} or balance >= 0),
                             "account mismatch")
                for (event_id,) in self._db.execute("SELECT event_id FROM events"):
                    total = self._db.execute(
                        "SELECT COALESCE(SUM(delta),0) FROM postings WHERE event_id=?", (event_id,)
                    ).fetchone()[0]
                    _require(total == 0, "event imbalance")
                for event_id, processor_ref, kind, amount, digest, processed in self._db.execute(
                    "SELECT event_id,processor_ref,kind,amount,payload_digest,processed FROM inbox"
                ):
                    _require(digest == _digest([processor_ref, kind, amount]), "processor inbox digest mismatch")
                    order = self._db.execute(
                        "SELECT status FROM orders WHERE processor_ref=?", (processor_ref,)
                    ).fetchone()
                    if processed:
                        _require(order is not None and (
                            order[0] == "reversed" or order[0] == "captured" and kind == "capture"
                        ), "processed processor event has no matching order state")
                for order_id, buyer_id, processor_ref, amount, status, capture_id, reversal_id in self._db.execute(
                    "SELECT order_id,buyer_id,processor_ref,amount,status,capture_event_id,reversal_event_id FROM orders"
                ):
                    rows = self._db.execute(
                        "SELECT event_id,kind,amount,processed FROM inbox WHERE processor_ref=?", (processor_ref,)
                    ).fetchall()
                    _require(all(row[2] == amount for row in rows), "order processor amount mismatch")
                    captures = {row[0] for row in rows if row[1] == "capture"}
                    reversals = {row[0] for row in rows if row[1] == "reversal"}
                    if status == "pending":
                        _require(not captures and capture_id is None and reversal_id is None
                                 and all(row[3] == 0 for row in rows), "pending order mismatch")
                    elif status == "captured":
                        _require(capture_id in captures and not reversals and reversal_id is None
                                 and all(row[3] == 1 for row in rows), "captured order mismatch")
                    else:
                        _require(capture_id in captures and reversal_id in reversals
                                 and all(row[3] == 1 for row in rows), "reversed order mismatch")
                    if status != "pending":
                        event = self._db.execute(
                            "SELECT kind,payload_digest FROM events WHERE event_id=?",
                            ("capture:" + order_id,)
                        ).fetchone()
                        postings = self._db.execute(
                            "SELECT account_id,delta FROM postings WHERE event_id=? ORDER BY ordinal",
                            ("capture:" + order_id,)
                        ).fetchall()
                        _require(
                            event == ("capture", _digest([order_id, processor_ref, amount]))
                            and postings == [
                                ("external_funding", -amount),
                                (_buyer_account(buyer_id, "purchased"), amount),
                            ],
                            "capture journal mismatch",
                        )
                    if status == "reversed":
                        event = self._db.execute(
                            "SELECT kind,payload_digest FROM events WHERE event_id=?",
                            ("reversal:" + order_id,)
                        ).fetchone()
                        postings = self._db.execute(
                            "SELECT account_id,delta FROM postings WHERE event_id=? ORDER BY ordinal",
                            ("reversal:" + order_id,)
                        ).fetchall()
                        recovered = -postings[0][1] if len(postings) == 3 else -1
                        _require(
                            0 <= recovered <= amount
                            and event == ("reversal", _digest([order_id, processor_ref, amount, recovered]))
                            and postings == [
                                (_buyer_account(buyer_id, "purchased"), -recovered),
                                ("operator_loss", -(amount - recovered)),
                                ("external_funding", amount),
                            ],
                            "reversal journal mismatch",
                        )
                for request_id, cap, status in self._db.execute(
                    "SELECT request_id,cap,status FROM reservations"
                ):
                    quote = self._load_quote(request_id)
                    _require(quote.spend_cap == cap, "service quote cap mismatch")
                    _require(self._balance("service_hold:" + request_id) == (cap if status == "held" else 0),
                             "service hold mismatch")
                    row = self._db.execute(
                        "SELECT provider_id,charge,fee,input_units,output_units,decision_id,receipt_digest,refund_reason "
                        "FROM reservations WHERE request_id=?", (request_id,)
                    ).fetchone()
                    provider_id, charge, fee, input_units, output_units, decision_id, receipt_digest, reason = row
                    if status == "settled":
                        _require(
                            provider_id == quote.provider_id
                            and all(value is not None for value in (charge, fee, input_units, output_units,
                                                                 decision_id, receipt_digest))
                            and input_units <= quote.max_input_units
                            and output_units <= quote.max_output_units
                            and charge <= quote.spend_cap
                            and charge <= input_units * quote.input_unit_price
                            + output_units * quote.output_unit_price
                            and fee <= charge * quote.fee_bps // 10_000
                            and reason is None,
                            "settled quote mismatch",
                        )
                    else:
                        _require(provider_id is None and all(value is None for value in
                                     (charge, fee, input_units, output_units, decision_id, receipt_digest))
                                 and (reason is None if status == "held" else reason is not None),
                                 "unsettled service mismatch")
                for payout_id, provider_id, amount, external_ref, status, outcome_event_id in self._db.execute(
                    "SELECT payout_id,provider_id,amount,external_ref,status,outcome_event_id FROM payouts"
                ):
                    _require(self._balance("payout_hold:" + payout_id)
                             == (amount if status in {"reserved", "unknown"} else 0),
                             "payout hold mismatch")
                    reserve = self._db.execute(
                        "SELECT kind,payload_digest FROM events WHERE event_id=?",
                        ("payout_hold:" + payout_id,)
                    ).fetchone()
                    reserve_postings = self._db.execute(
                        "SELECT account_id,delta FROM postings WHERE event_id=? ORDER BY ordinal",
                        ("payout_hold:" + payout_id,)
                    ).fetchall()
                    _require(
                        reserve == ("payout_reserve", _digest([payout_id, provider_id, amount, external_ref]))
                        and reserve_postings == [
                            ("provider_eligible:" + provider_id, -amount),
                            ("payout_hold:" + payout_id, amount),
                        ],
                        "payout reserve journal mismatch",
                    )
                    unknown_event = self._db.execute(
                        "SELECT kind,payload_digest FROM events WHERE event_id=?",
                        ("payout_unknown:" + payout_id,)
                    ).fetchone()
                    result = self._db.execute(
                        "SELECT kind,payload_digest FROM events WHERE event_id=?", ("payout_result:" + payout_id,)
                    ).fetchone()
                    if status == "reserved":
                        _require(unknown_event is None and result is None and outcome_event_id is None,
                                 "reserved payout mismatch")
                    elif status == "unknown":
                        _require(unknown_event == ("payout_unknown", _digest([payout_id]))
                                 and result is None and outcome_event_id is None,
                                 "unknown payout mismatch")
                    else:
                        _require(unknown_event is None or unknown_event ==
                                 ("payout_unknown", _digest([payout_id])), "payout uncertainty journal mismatch")
                        result_postings = self._db.execute(
                            "SELECT account_id,delta FROM postings WHERE event_id=? ORDER BY ordinal",
                            ("payout_result:" + payout_id,)
                        ).fetchall()
                        destination = "external_funding" if status == "paid" else "provider_eligible:" + provider_id
                        _require(result == ("payout_" + status,
                                            _digest([payout_id, outcome_event_id, status]))
                                 and result_postings == [
                                     ("payout_hold:" + payout_id, -amount), (destination, amount)
                                 ],
                                 "terminal payout mismatch")
                _require(self._balance("operator_loss") <= 0, "invalid loss balance")
                result = {
                    "events": self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                    "orders": self._db.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
                    "reservations": self._db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0],
                    "payouts": self._db.execute("SELECT COUNT(*) FROM payouts").fetchone()[0],
                    "unfunded_reversal_loss": -self._balance("operator_loss"),
                }
                self._db.execute("COMMIT")
                return result
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
