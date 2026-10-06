"""SQLite is the final authority for inventory and unique orders."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class PurchaseRejected(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Database:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    stock INTEGER NOT NULL CHECK (stock >= 0)
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    event_id INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (status = 'SUCCESS'),
                    request_id TEXT NOT NULL UNIQUE,
                    UNIQUE (user_id, event_id, product_id)
                );
            ''')

    def reset(self):
        with self.connect() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM orders")
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM products")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'orders'")
            conn.execute("INSERT INTO products VALUES (1, 'Flash Sale Demo Product', 10)")

    def purchase(self, user_id, event_id, product_id, nonce):
        with self.connect() as conn, conn:
            # Each call has its own connection. SQLite serializes writers; it does
            # not block the ASGI event loop because this runs in a thread pool.
            # Any exception rolls back BOTH stock decrement and order insert.
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT id FROM orders WHERE user_id=? AND event_id=? AND product_id=?",
                (user_id, event_id, product_id),
            ).fetchone():
                raise PurchaseRejected("ALREADY_PURCHASED")
            changed = conn.execute(
                "UPDATE products SET stock = stock - 1 WHERE id = ? AND stock > 0",
                (product_id,),
            ).rowcount
            if changed != 1:
                raise PurchaseRejected("SOLD_OUT")
            conn.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (user_id,))
            cursor = conn.execute(
                "INSERT INTO orders(user_id, product_id, event_id, status, request_id) "
                "VALUES (?, ?, ?, 'SUCCESS', ?)",
                (user_id, product_id, event_id, nonce),
            )
            order_id = cursor.lastrowid
        return order_id

    def status(self):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT stock, (SELECT COUNT(*) FROM orders WHERE event_id=1 AND product_id=1) "
                "AS order_count FROM products WHERE id=1"
            ).fetchone()
            return {"databaseStock": row["stock"] if row else None,
                    "orderCount": row["order_count"] if row else 0}
