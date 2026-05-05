import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "history.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            keyword TEXT NOT NULL,
            report TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def save_history(type_: str, keyword: str, report: dict):
    import json
    conn = get_db()
    conn.execute(
        "INSERT INTO history (type, keyword, report) VALUES (?, ?, ?)",
        (type_, keyword, json.dumps(report, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def list_history(type_: str = "", limit: int = 50):
    import json
    conn = get_db()
    if type_:
        rows = conn.execute(
            "SELECT id, type, keyword, created_at FROM history WHERE type=? ORDER BY created_at DESC LIMIT ?",
            (type_, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, type, keyword, created_at FROM history ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_history(id_: int):
    import json
    conn = get_db()
    row = conn.execute("SELECT * FROM history WHERE id=?", (id_,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["report"] = json.loads(d["report"])
    return d


def delete_history(id_: int):
    conn = get_db()
    conn.execute("DELETE FROM history WHERE id=?", (id_,))
    conn.commit()
    conn.close()
