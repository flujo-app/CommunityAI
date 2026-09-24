"""Standalone quote-bound hold experiment, independent of the product ledger."""

import hashlib
import json
import sqlite3


def digest(terms):
    raw = json.dumps(terms, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def hold(db, terms):
    db.execute("BEGIN IMMEDIATE")
    try:
        old = db.execute("SELECT terms_digest FROM holds WHERE request_id=?", (terms["request_id"],)).fetchone()
        claim = digest(terms)
        if old is not None:
            if old[0] != claim:
                raise ValueError("changed quote replay")
            db.execute("COMMIT")
            return False
        price_max = terms["input_limit"] * terms["input_price"] + terms["output_limit"] * terms["output_price"]
        if price_max > terms["cap"]:
            raise ValueError("quote cap below maximum")
        db.execute("INSERT INTO holds VALUES (?, ?)", (terms["request_id"], claim))
        db.execute("COMMIT")
        return True
    except BaseException:
        db.execute("ROLLBACK")
        raise


if __name__ == "__main__":
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute("CREATE TABLE holds (request_id TEXT PRIMARY KEY, terms_digest TEXT NOT NULL)")
    quote = {
        "request_id": "q1",
        "model_id": "test/model",
        "profile_id": "test/profile",
        "input_limit": 10,
        "output_limit": 30,
        "input_price": 2,
        "output_price": 2,
        "cap": 80,
    }
    assert hold(db, quote)
    assert not hold(db, dict(reversed(list(quote.items()))))
    for changed in (quote | {"model_id": "other/model"}, quote | {"cap": 79}):
        try:
            hold(db, changed)
        except ValueError:
            continue
        raise AssertionError("changed quote accepted")
    assert db.execute("SELECT COUNT(*) FROM holds").fetchone()[0] == 1
    print("shadow quote standalone digest, cap and replay prototype PASS")
