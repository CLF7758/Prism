import sqlite3

db = sqlite3.connect(':memory:')
db.execute('CREATE TABLE t (id TEXT PRIMARY KEY, payload JSON NOT NULL)')
try:
    db.execute("ALTER TABLE t ADD COLUMN name TEXT "
               "GENERATED ALWAYS AS (json_extract(payload,'$.name')) VIRTUAL")
    db.execute("INSERT INTO t (id,payload) VALUES ('a','{\"name\":\"x\"}')")
    print('ALTER ok ->', db.execute('SELECT name FROM t').fetchall())
except Exception as error:
    print('ALTER failed:', error)
