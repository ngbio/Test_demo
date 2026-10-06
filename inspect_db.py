"""Read-only evidence: query the actual SQLite file after test_demo.py."""
import os
import sqlite3
from pathlib import Path

path = Path(os.getenv("DATABASE_PATH", str(Path(__file__).parent / "data" / "demo.db"))).resolve()
with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
    print("Database:", path)
    print("\nPRODUCTS (id, name, stock)")
    for row in conn.execute("SELECT id, name, stock FROM products"):
        print(row)
    print("\nORDERS (id, user_id, product_id, event_id, status, request_id)")
    for row in conn.execute("SELECT * FROM orders ORDER BY id"):
        print(row)
    count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    stock = conn.execute("SELECT stock FROM products WHERE id=1").fetchone()[0]
    print(f"\nOrders created: {count}\nDatabase stock: {stock}")
    assert count == 10 and stock == 0, "Run the full test_demo.py before capturing evidence"
    print("DATABASE CHECK: PASS")
