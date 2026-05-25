import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "history.db"))


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


# ── 콘텐츠 파이프라인 ─────────────────────────────────────────────────

def init_pipeline():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'filming',
            content_type TEXT DEFAULT '미드폼',
            editor TEXT DEFAULT '',
            planned_date TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def list_pipeline():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM pipeline ORDER BY stage, sort_order, created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_pipeline_item(title, stage, content_type, editor, planned_date, notes):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO pipeline (title, stage, content_type, editor, planned_date, notes) VALUES (?,?,?,?,?,?)",
        (title, stage, content_type, editor, planned_date, notes),
    )
    conn.commit()
    id_ = cur.lastrowid
    conn.close()
    return id_


def update_pipeline_item(id_: int, data: dict):
    fields = {k: v for k, v in data.items() if k in
              ("title", "stage", "content_type", "editor", "planned_date", "notes", "sort_order")}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [id_]
    conn = get_db()
    conn.execute(f"UPDATE pipeline SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?", vals)
    conn.commit()
    conn.close()


def delete_pipeline_item(id_: int):
    conn = get_db()
    conn.execute("DELETE FROM pipeline WHERE id=?", (id_,))
    conn.commit()
    conn.close()
