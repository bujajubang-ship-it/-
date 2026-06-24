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
    conn.execute("UPDATE pipeline SET content_type='숏폼' WHERE content_type='쇼츠'")
    # 구 스테이지 키 마이그레이션
    conn.execute("UPDATE pipeline SET stage='filming' WHERE stage='filming'")  # 촬영 완료 → 촬영 (key 동일)
    conn.execute("UPDATE pipeline SET stage='uploaded' WHERE stage='scheduled'")  # 업로드 예정 → 업로드
    conn.execute("UPDATE pipeline SET stage='sns' WHERE stage='blog'")            # 블로그 완료 → 기타 SNS 배포
    conn.commit()
    conn.close()


def list_pipeline():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM pipeline ORDER BY sort_order, id"
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


# ── 기존 영상 최적화 체크리스트 ──────────────────────────────────────

def _ensure_optimize_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS optimize_videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            thumbnail INTEGER DEFAULT 0,
            title_edit INTEGER DEFAULT 0,
            cut INTEGER DEFAULT 0,
            description INTEGER DEFAULT 0,
            notes TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def list_optimize():
    conn = get_db()
    _ensure_optimize_table(conn)
    rows = conn.execute(
        "SELECT * FROM optimize_videos ORDER BY sort_order, id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_optimize(title: str, notes: str = "") -> int:
    conn = get_db()
    _ensure_optimize_table(conn)
    cur = conn.execute(
        "INSERT INTO optimize_videos (title, notes) VALUES (?,?)",
        (title, notes),
    )
    conn.commit()
    id_ = cur.lastrowid
    conn.close()
    return id_


def update_optimize(id_: int, data: dict):
    allowed = {"title", "thumbnail", "title_edit", "cut", "description", "notes", "sort_order"}
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [id_]
    conn = get_db()
    _ensure_optimize_table(conn)
    conn.execute(f"UPDATE optimize_videos SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?", vals)
    conn.commit()
    conn.close()


def delete_optimize(id_: int):
    conn = get_db()
    _ensure_optimize_table(conn)
    conn.execute("DELETE FROM optimize_videos WHERE id=?", (id_,))
    conn.commit()
    conn.close()


# ── 기획 워크시트 (스프레드시트형 작업공간) ──────────────────────────
def _ensure_worksheet(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS worksheet_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sort_order INTEGER DEFAULT 0,
            data TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

def list_worksheet():
    conn = get_db(); _ensure_worksheet(conn)
    rows = conn.execute("SELECT * FROM worksheet_rows ORDER BY sort_order, id").fetchall()
    conn.close(); return [dict(r) for r in rows]

def create_worksheet_row(data: str = "{}") -> int:
    conn = get_db(); _ensure_worksheet(conn)
    cur = conn.execute("INSERT INTO worksheet_rows (data) VALUES (?)", (data,))
    conn.commit(); id_ = cur.lastrowid; conn.close(); return id_

def update_worksheet_row(id_: int, data: str):
    conn = get_db(); _ensure_worksheet(conn)
    conn.execute("UPDATE worksheet_rows SET data=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (data, id_))
    conn.commit(); conn.close()

def delete_worksheet_row(id_: int):
    conn = get_db(); _ensure_worksheet(conn)
    conn.execute("DELETE FROM worksheet_rows WHERE id=?", (id_,))
    conn.commit(); conn.close()
