import sqlite3
from sqlalchemy import create_engine, text

SRC = "/tmp/claims.db"
URL = "postgresql+psycopg://claims:claims_pwd_change_me@db:5432/claims"

tables = [
    "users", "claim_events", "audit_logs", "claim_replies", "duty_changes",
    "evidence", "refund_records", "refund_items", "stockin_items",
]

src = sqlite3.connect(SRC)
src.row_factory = sqlite3.Row
engine = create_engine(URL)

with engine.begin() as conn:
    for table in tables:
        cols = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")').fetchall()]
        if not cols:
            continue
        rows = src.execute(f'SELECT * FROM "{table}"').fetchall()
        if not rows:
            continue
        existing = conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()
        if existing:
            print(f"skip {table}: already has {existing} rows")
            continue
        names = ", ".join(f'"{c}"' for c in cols)
        params = ", ".join(f":p{i}" for i in range(len(cols)))
        stmt = text(f'INSERT INTO "{table}" ({names}) VALUES ({params})')
        types = {
            r[0]: r[1] for r in conn.execute(text("""
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_schema='public' AND table_name=:table
            """), {"table": table}).fetchall()
        }
        values = []
        for row in rows:
            vals = []
            for col, value in zip(cols, tuple(row)):
                if types.get(col) == "boolean" and value is not None:
                    value = bool(value)
                vals.append(value)
            values.append(dict(zip([f"p{i}" for i in range(len(cols))], vals)))
        conn.execute(stmt, values)
        print(f"migrated {table}: {len(rows)}")

    seqs = conn.execute(text("""
        SELECT table_name, column_name, pg_get_serial_sequence(quote_ident(table_schema)||'.'||quote_ident(table_name), column_name)
        FROM information_schema.columns
        WHERE table_schema='public' AND column_default LIKE 'nextval%'
    """)).fetchall()
    for table, col, seq in seqs:
        if seq:
            conn.execute(text(f"SELECT setval(:seq, COALESCE((SELECT MAX(\"{col}\") FROM \"{table}\"), 1), true)"), {"seq": seq})

print("migration complete")
