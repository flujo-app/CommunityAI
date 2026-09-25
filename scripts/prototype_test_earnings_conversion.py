"""Standalone SQLite experiment: released test earnings cannot be spent twice."""

import sqlite3
import tempfile
import threading
from contextlib import closing
from pathlib import Path


def convert(path: Path, conversion_id: str, amount: int) -> bool:
    db = sqlite3.connect(path, timeout=2, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT amount FROM conversions WHERE conversion_id=?", (conversion_id,)).fetchone()
        if old is not None:
            if old[0] != amount:
                raise ValueError("changed replay")
            db.execute("COMMIT")
            return False
        eligible = db.execute("SELECT balance FROM accounts WHERE name='eligible'").fetchone()[0]
        if amount <= 0 or amount > eligible:
            raise ValueError("insufficient released units")
        db.execute("UPDATE accounts SET balance=balance-? WHERE name='eligible'", (amount,))
        db.execute("UPDATE accounts SET balance=balance+? WHERE name='buyer'", (amount,))
        db.execute("INSERT INTO conversions VALUES (?, ?)", (conversion_id, amount))
        db.execute("COMMIT")
        return True
    except BaseException:
        db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "wallet.sqlite"
        with closing(sqlite3.connect(path)) as db:
            db.executescript(
                "CREATE TABLE accounts(name TEXT PRIMARY KEY,balance INTEGER);"
                "INSERT INTO accounts VALUES('pending',45),('eligible',0),('buyer',0);"
                "CREATE TABLE releases(receipt_id TEXT PRIMARY KEY, net INTEGER);"
                "CREATE TABLE conversions(conversion_id TEXT PRIMARY KEY, amount INTEGER);"
            )
            net = 45
            db.execute("UPDATE accounts SET balance=balance-? WHERE name='pending'", (net,))
            db.execute("UPDATE accounts SET balance=balance+? WHERE name='eligible'", (net,))
            db.execute("INSERT INTO releases VALUES('receipt-1',?)", (net,))
            db.commit()
        assert convert(path, "convert-1", 20)
        assert not convert(path, "convert-1", 20)
        try:
            convert(path, "convert-1", 21)
        except ValueError:
            pass
        else:
            raise AssertionError("changed replay accepted")
        results = []

        def contender(identifier):
            try:
                results.append(convert(path, identifier, 20))
            except ValueError:
                results.append(False)

        threads = [threading.Thread(target=contender, args=(f"race-{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results) == [False, True]
        with closing(sqlite3.connect(path)) as db:
            assert dict(db.execute("SELECT name,balance FROM accounts")) == {"pending": 0, "eligible": 5, "buyer": 40}
    print("standalone test earnings conversion PASS")


if __name__ == "__main__":
    main()
