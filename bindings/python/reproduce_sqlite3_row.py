import sqlite3
import turso

conn = turso.connect(":memory:")
conn.row_factory = sqlite3.Row

conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
conn.execute("INSERT INTO t VALUES (1, 'alice')")

row = conn.execute("SELECT * FROM t").fetchone()
print(row["id"], row["name"])
