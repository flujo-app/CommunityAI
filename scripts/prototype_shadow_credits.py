"""Fast, standalone SQLite experiment for shadow-credit accounting.

Run with ``python scripts/prototype_shadow_credits.py``.  No project imports,
network, payment credentials, or model downloads are needed.
"""

import sqlite3


def transfer(db, tx_id, source, destination, amount):
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise ValueError("positive integer amount required")
    with db:
        previous = db.execute("SELECT source, destination, amount FROM transfers WHERE tx_id=?", (tx_id,)).fetchone()
        if previous:
            if previous != (source, destination, amount):
                raise ValueError("conflicting replay")
            return False
        balance = db.execute("SELECT balance FROM accounts WHERE account_id=?", (source,)).fetchone()
        if balance is None or balance[0] < amount:
            raise ValueError("insufficient balance")
        if db.execute("SELECT 1 FROM accounts WHERE account_id=?", (destination,)).fetchone() is None:
            raise ValueError("unknown destination")
        db.execute("UPDATE accounts SET balance=balance-? WHERE account_id=?", (amount, source))
        db.execute("UPDATE accounts SET balance=balance+? WHERE account_id=?", (amount, destination))
        db.execute("INSERT INTO transfers VALUES (?, ?, ?, ?)", (tx_id, source, destination, amount))
        return True


def main():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.executescript(
        "CREATE TABLE accounts(account_id TEXT PRIMARY KEY, balance INTEGER NOT NULL CHECK(balance>=0));"
        "CREATE TABLE transfers(tx_id TEXT PRIMARY KEY, source TEXT NOT NULL, "
        "destination TEXT NOT NULL, amount INTEGER NOT NULL);"
        "INSERT INTO accounts VALUES ('buyer', 100), ('held', 0), ('provider', 0), ('fee', 0);"
    )
    assert transfer(db, "hold-1", "buyer", "held", 70)
    assert transfer(db, "hold-1", "buyer", "held", 70) is False
    try:
        transfer(db, "hold-1", "buyer", "held", 71)
        raise AssertionError("conflicting replay accepted")
    except ValueError:
        pass
    try:
        transfer(db, "hold-2", "buyer", "held", 31)
        raise AssertionError("overspend accepted")
    except ValueError:
        pass
    assert transfer(db, "settle-provider", "held", "provider", 40)
    assert transfer(db, "settle-fee", "held", "fee", 5)
    assert transfer(db, "release", "held", "buyer", 25)
    assert dict(db.execute("SELECT account_id, balance FROM accounts")) == {
        "buyer": 55,
        "held": 0,
        "provider": 40,
        "fee": 5,
    }
    print("shadow-credit prototype: atomic hold, replay, overspend and balanced settlement PASS")


if __name__ == "__main__":
    main()
