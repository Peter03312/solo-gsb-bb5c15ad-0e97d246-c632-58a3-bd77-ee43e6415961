"""FastAPI application: exhibition-label multilingual review workflow."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from starlette.middleware.sessions import SessionMiddleware

from .db import init_db, tx
from .security import (
    current_user,
    get_db,
    require_login,
    require_roles,
    verify_password,
)

BASE_DIR = os.path.dirname(__file__)
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-insecure-secret-change-me")

app = FastAPI(title="展馆多语展签审校系统")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")


# ---------------------------------------------------------------------------
# Helpers / schemas
# ---------------------------------------------------------------------------

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conflict(code: str, message: str, **extra: Any) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


class LoginForm(BaseModel):
    username: str
    password: str


class LanguageIn(BaseModel):
    code: str
    name_zh: str
    name_en: str

    @field_validator("code", "name_zh", "name_en")
    @classmethod
    def strip_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空")
        return v


class LabelIn(BaseModel):
    code: str
    source_text: str
    note: str = ""

    @field_validator("code", "source_text")
    @classmethod
    def strip_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空")
        return v


class LabelRevisionIn(BaseModel):
    source_text: str
    base_rev: int
    note: str = ""

    @field_validator("source_text")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("原文不能为空")
        return v


class TranslationIn(BaseModel):
    label_id: int
    text: str
    base_rev: int          # translation current_rev read by client (0 if missing)
    base_source_rev: int   # source current_rev read by client

    @field_validator("text")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("译文不能为空")
        return v


class ConfirmIn(BaseModel):
    base_rev: int          # translation current_rev read by client


def get_language(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM languages WHERE code = ?", (code.strip(),)
    ).fetchone()


def get_language_or_404(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = get_language(conn, code)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found",
                                                     "message": "语言不存在。"})
    return row


def compute_item(row: sqlite3.Row) -> dict[str, Any]:
    """Compute per-label review state for one language from a joined row."""
    if row["t_source_rev"] is None:
        state = "missing"
    elif row["t_source_rev"] != row["l_current_rev"]:
        state = "stale"
    elif not row["t_confirmed"]:
        state = "pending"
    else:
        state = "confirmed"

    reasons: list[str] = []
    if state == "missing":
        reasons.append("尚无译文")
    elif state == "stale":
        reasons.append(
            f"原文已更新至第 {row['l_current_rev']} 版，"
            f"译文基于第 {row['t_source_rev']} 版原文，需重新提交译文"
        )
    elif state == "pending":
        reasons.append("译文已提交，等待另一名校对者确认")

    return {
        "label_id": row["l_id"],
        "label_code": row["l_code"],
        "source_current_rev": row["l_current_rev"],
        "source_text": row["lr_text"],
        "source_updated_at": row["l_updated_at"],
        "translation": None
        if row["t_id"] is None
        else {
            "id": row["t_id"],
            "current_rev": row["t_current_rev"],
            "source_rev": row["t_source_rev"],
            "text": row["tr_text"],
            "confirmed": bool(row["t_confirmed"]),
            "translator_id": row["t_translator_id"],
            "translator_name": row["translator_name"],
            "confirmed_by": row["t_confirmed_by"],
            "reviewer_name": row["reviewer_name"],
            "confirmed_at": row["t_confirmed_at"],
            "updated_at": row["t_updated_at"],
        },
        "state": state,
        "block_reason": reasons[0] if reasons else None,
    }


WORKSPACE_SQL = """
SELECT  l.id              AS l_id,
        l.code            AS l_code,
        l.current_rev     AS l_current_rev,
        l.updated_at      AS l_updated_at,
        lr.source_text    AS lr_text,
        t.id              AS t_id,
        t.current_rev     AS t_current_rev,
        t.source_rev      AS t_source_rev,
        t.confirmed       AS t_confirmed,
        t.translator_id   AS t_translator_id,
        t.confirmed_by    AS t_confirmed_by,
        t.confirmed_at    AS t_confirmed_at,
        t.updated_at      AS t_updated_at,
        tr.text           AS tr_text,
        tu.display_name   AS translator_name,
        ru.display_name   AS reviewer_name
FROM labels l
JOIN label_revisions lr
  ON lr.label_id = l.id AND lr.rev = l.current_rev
LEFT JOIN translations t
  ON t.label_id = l.id AND t.language_id = :lang
LEFT JOIN translation_revisions tr
  ON tr.translation_id = t.id AND tr.rev = t.current_rev
LEFT JOIN users tu ON tu.id = t.translator_id
LEFT JOIN users ru ON ru.id = t.confirmed_by
ORDER BY l.code
"""


def workspace_payload(conn: sqlite3.Connection, lang: sqlite3.Row) -> dict[str, Any]:
    rows = conn.execute(WORKSPACE_SQL, {"lang": lang["id"]}).fetchall()
    items = [compute_item(r) for r in rows]

    # Batch history: label revisions + translation revisions.
    label_ids = [i["label_id"] for i in items]
    label_hist: dict[int, list[dict[str, Any]]] = {lid: [] for lid in label_ids}
    trans_hist: dict[int, list[dict[str, Any]]] = {}
    if label_ids:
        placeholders = ",".join("?" for _ in label_ids)
        for r in conn.execute(
            f"""SELECT lr.label_id, lr.rev, lr.source_text, lr.note,
                       lr.created_at, u.display_name AS author_name
                FROM label_revisions lr JOIN users u ON u.id = lr.author_id
                WHERE lr.label_id IN ({placeholders})
                ORDER BY lr.label_id, lr.rev""",
            label_ids,
        ):
            label_hist[r["label_id"]].append(dict(r))
        for r in conn.execute(
            f"""SELECT t.label_id, tr.rev, tr.text, tr.source_rev,
                       tr.created_at, u.display_name AS translator_name
                FROM translation_revisions tr
                JOIN translations t ON t.id = tr.translation_id
                JOIN users u ON u.id = tr.translator_id
                WHERE t.language_id = ? AND t.label_id IN ({placeholders})
                ORDER BY t.label_id, tr.rev""",
            [lang["id"], *label_ids],
        ):
            trans_hist.setdefault(r["label_id"], []).append(dict(r))
    for item in items:
        item["source_history"] = label_hist.get(item["label_id"], [])
        item["translation_history"] = trans_hist.get(item["label_id"], [])

    counts = {"missing": 0, "pending": 0, "stale": 0, "confirmed": 0}
    blockers: list[dict[str, Any]] = []
    for it in items:
        counts[it["state"]] += 1
        if it["state"] != "confirmed":
            blockers.append({"label_code": it["label_code"],
                             "state": it["state"],
                             "reason": it["block_reason"]})

    total = len(items)
    publishable = total > 0 and not blockers
    snap = conn.execute(
        "SELECT version, label_count, published_at FROM snapshots"
        " WHERE language_id = ? ORDER BY version DESC LIMIT 1",
        (lang["id"],),
    ).fetchone()

    return {
        "language": dict(lang),
        "total": total,
        "counts": counts,
        "publishable": publishable,
        "blockers": blockers,
        "latest_snapshot": None if snap is None else dict(snap),
        "items": items,
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@app.post("/api/auth/login")
def login(form: LoginForm, request: Request, conn=Depends(get_db)):
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", (form.username.strip(),)
    ).fetchone()
    if row is None or not verify_password(form.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail={"code": "bad_credentials",
                                                     "message": "用户名或密码错误。"})
    request.session["uid"] = row["id"]
    request.session["role"] = row["role"]
    return {"id": row["id"], "username": row["username"],
            "display_name": row["display_name"], "role": row["role"]}


@app.post("/api/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return user  # may be null for the public visitor page


# ---------------------------------------------------------------------------
# Languages
# ---------------------------------------------------------------------------


@app.get("/api/languages")
def list_languages(conn=Depends(get_db), user=Depends(require_login)):
    rows = conn.execute(
        """SELECT l.*,
                  (SELECT version FROM snapshots s
                   WHERE s.language_id = l.id ORDER BY s.version DESC LIMIT 1
                  ) AS latest_version,
                  (SELECT published_at FROM snapshots s
                   WHERE s.language_id = l.id ORDER BY s.version DESC LIMIT 1
                  ) AS latest_published_at
           FROM languages l ORDER BY l.code"""
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/languages", status_code=201)
def create_language(body: LanguageIn, conn=Depends(get_db),
                    user=Depends(require_roles("curator"))):
    if conn.execute("SELECT 1 FROM languages WHERE code = ?", (body.code,)).fetchone():
        raise conflict("duplicate_language", f"语言代码 {body.code} 已存在。")
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO languages (code, name_zh, name_en, created_at)"
            " VALUES (?, ?, ?, ?)",
            (body.code, body.name_zh, body.name_en, utcnow()),
        )
        lid = cur.lastrowid
    return dict(conn.execute("SELECT * FROM languages WHERE id = ?", (lid,)).fetchone())


# ---------------------------------------------------------------------------
# Labels (source text maintained by curators)
# ---------------------------------------------------------------------------


@app.get("/api/labels")
def list_labels(conn=Depends(get_db), user=Depends(require_login)):
    rows = conn.execute(
        """SELECT l.id, l.code, l.current_rev, l.updated_at, lr.source_text
           FROM labels l
           JOIN label_revisions lr ON lr.label_id = l.id AND lr.rev = l.current_rev
           ORDER BY l.code"""
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        stats = conn.execute(
            """SELECT lang.code AS code,
                      CASE WHEN t.id IS NULL THEN 'missing'
                           WHEN t.source_rev != l.current_rev THEN 'stale'
                           WHEN t.confirmed = 0 THEN 'pending'
                           ELSE 'confirmed' END AS state
               FROM languages lang
               LEFT JOIN translations t
                 ON t.label_id = l.id AND t.language_id = lang.id""",
        ).fetchall()
        d["languages"] = [dict(s) for s in stats]
        result.append(d)
    return result


@app.post("/api/labels", status_code=201)
def create_label(body: LabelIn, conn=Depends(get_db),
                 user=Depends(require_roles("curator"))):
    if conn.execute("SELECT 1 FROM labels WHERE code = ?", (body.code,)).fetchone():
        raise conflict("duplicate_label", f"展签编号 {body.code} 已存在。")
    now = utcnow()
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO labels (code, current_rev, created_at, updated_at)"
            " VALUES (?, 1, ?, ?)",
            (body.code, now, now),
        )
        lid = cur.lastrowid
        conn.execute(
            "INSERT INTO label_revisions (label_id, rev, source_text, note, author_id, created_at)"
            " VALUES (?, 1, ?, ?, ?, ?)",
            (lid, body.source_text, body.note, user["id"], now),
        )
    row = conn.execute(
        """SELECT l.id, l.code, l.current_rev, l.updated_at, lr.source_text
           FROM labels l JOIN label_revisions lr
             ON lr.label_id = l.id AND lr.rev = l.current_rev
           WHERE l.id = ?""",
        (lid,),
    ).fetchone()
    return dict(row)


@app.get("/api/labels/{label_id}")
def label_detail(label_id: int, conn=Depends(get_db), user=Depends(require_login)):
    label = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
    if label is None:
        raise HTTPException(status_code=404, detail={"code": "not_found",
                                                     "message": "展签不存在。"})
    revs = [dict(r) for r in conn.execute(
        """SELECT lr.rev, lr.source_text, lr.note, lr.created_at,
                  u.display_name AS author_name
           FROM label_revisions lr JOIN users u ON u.id = lr.author_id
           WHERE lr.label_id = ? ORDER BY lr.rev""",
        (label_id,),
    )]
    translations = [dict(r) for r in conn.execute(
        """SELECT lang.code, lang.name_zh, t.current_rev, t.source_rev,
                  t.confirmed, tr.text
           FROM languages lang
           LEFT JOIN translations t
             ON t.label_id = ? AND t.language_id = lang.id
           LEFT JOIN translation_revisions tr
             ON tr.translation_id = t.id AND tr.rev = t.current_rev
           ORDER BY lang.code""",
        (label_id,),
    )]
    return {"label": dict(label), "revisions": revs, "translations": translations}


@app.post("/api/labels/{label_id}/revisions")
def revise_label(label_id: int, body: LabelRevisionIn,
                 conn=Depends(get_db),
                 user=Depends(require_roles("curator"))):
    """Create a new source revision.

    Even identical text creates a new revision number; every translation
    becomes unconfirmed/stale and must be resubmitted and re-confirmed.
    """
    with tx(conn):
        label = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
        if label is None:
            raise HTTPException(status_code=404, detail={"code": "not_found",
                                                         "message": "展签不存在。"})
        if label["current_rev"] != body.base_rev:
            raise conflict(
                "source_rev_changed",
                f"原文已被他人更新：您读取的是第 {body.base_rev} 版，"
                f"当前为第 {label['current_rev']} 版，请刷新后重试。",
                current_rev=label["current_rev"],
            )
        new_rev = body.base_rev + 1
        now = utcnow()
        conn.execute(
            "INSERT INTO label_revisions (label_id, rev, source_text, note, author_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (label_id, new_rev, body.source_text, body.note, user["id"], now),
        )
        conn.execute(
            "UPDATE labels SET current_rev = ?, updated_at = ? WHERE id = ?",
            (new_rev, now, label_id),
        )
        # All translations, even previously confirmed ones, lose confirmation.
        conn.execute(
            "UPDATE translations SET confirmed = 0, confirmed_by = NULL,"
            " confirmed_at = NULL WHERE label_id = ?",
            (label_id,),
        )
    return {"ok": True, "current_rev": new_rev}


# ---------------------------------------------------------------------------
# Workspace per language: translate / confirm / publish
# ---------------------------------------------------------------------------


@app.get("/api/workspace/{code}")
def workspace(code: str, conn=Depends(get_db), user=Depends(require_login)):
    lang = get_language_or_404(conn, code)
    return workspace_payload(conn, lang)


@app.post("/api/workspace/{code}/translations")
def submit_translation(code: str, body: TranslationIn,
                       conn=Depends(get_db),
                       user=Depends(require_roles("translator"))):
    """Submit (or resubmit) a translation. New rev clears confirmation."""
    with tx(conn):
        lang = get_language_or_404(conn, code)
        label = conn.execute(
            "SELECT * FROM labels WHERE id = ?", (body.label_id,)
        ).fetchone()
        if label is None:
            raise HTTPException(status_code=404, detail={"code": "not_found",
                                                         "message": "展签不存在。"})
        if label["current_rev"] != body.base_source_rev:
            raise conflict(
                "source_rev_changed",
                f"原文已更新至第 {label['current_rev']} 版（您读取的是第 "
                f"{body.base_source_rev} 版），请刷新后基于最新原文提交。",
                current_source_rev=label["current_rev"],
            )
        t = conn.execute(
            "SELECT * FROM translations WHERE label_id = ? AND language_id = ?",
            (body.label_id, lang["id"]),
        ).fetchone()
        if t is None:
            if body.base_rev != 0:
                raise conflict("state_changed", "译文状态已变化，请刷新后重试。")
            now = utcnow()
            cur = conn.execute(
                "INSERT INTO translations (label_id, language_id, current_rev,"
                " source_rev, confirmed, translator_id, updated_at)"
                " VALUES (?, ?, 1, ?, 0, ?, ?)",
                (body.label_id, lang["id"], label["current_rev"], user["id"], now),
            )
            tid = cur.lastrowid
            new_rev = 1
        else:
            if t["current_rev"] != body.base_rev:
                raise conflict(
                    "translation_rev_changed",
                    f"译文已被更新：您读取的是第 {body.base_rev} 版，"
                    f"当前为第 {t['current_rev']} 版，请刷新后重试。",
                    current_rev=t["current_rev"],
                )
            new_rev = t["current_rev"] + 1
            now = utcnow()
            conn.execute(
                "UPDATE translations SET current_rev = ?, source_rev = ?,"
                " confirmed = 0, translator_id = ?, confirmed_by = NULL,"
                " confirmed_at = NULL, updated_at = ? WHERE id = ?",
                (new_rev, label["current_rev"], user["id"], now, t["id"]),
            )
            tid = t["id"]
        conn.execute(
            "INSERT INTO translation_revisions (translation_id, rev, text,"
            " source_rev, translator_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (tid, new_rev, body.text, label["current_rev"], user["id"], now),
        )
    return {"ok": True, "current_rev": new_rev}


@app.post("/api/workspace/{code}/translations/{label_id}/confirm")
def confirm_translation(code: str, label_id: int, body: ConfirmIn,
                        conn=Depends(get_db),
                        user=Depends(require_roles("reviewer"))):
    """A reviewer other than the translator confirms the current revision."""
    with tx(conn):
        lang = get_language_or_404(conn, code)
        t = conn.execute(
            "SELECT * FROM translations WHERE label_id = ? AND language_id = ?",
            (label_id, lang["id"]),
        ).fetchone()
        if t is None:
            raise HTTPException(status_code=404, detail={"code": "not_found",
                                                         "message": "尚无译文可确认。"})
        if t["current_rev"] != body.base_rev:
            raise conflict(
                "translation_rev_changed",
                f"译文已被更新：您读取的是第 {body.base_rev} 版，"
                f"当前为第 {t['current_rev']} 版，请刷新后重试。",
                current_rev=t["current_rev"],
            )
        label = conn.execute(
            "SELECT current_rev FROM labels WHERE id = ?", (label_id,)
        ).fetchone()
        if t["source_rev"] != label["current_rev"]:
            raise conflict(
                "source_rev_changed",
                f"原文已更新至第 {label['current_rev']} 版，该译文已过期，"
                "需由译者重新提交后才能确认。",
                current_source_rev=label["current_rev"],
            )
        if t["translator_id"] == user["id"]:
            raise HTTPException(
                status_code=403,
                detail={"code": "self_review",
                        "message": "不能确认自己提交的译文，请由另一名校对者确认。"},
            )
        if t["confirmed"]:
            raise conflict("already_confirmed", "该译文修订已确认，无需重复操作。")
        now = utcnow()
        conn.execute(
            "UPDATE translations SET confirmed = 1, confirmed_by = ?,"
            " confirmed_at = ? WHERE id = ?",
            (user["id"], now, t["id"]),
        )
    return {"ok": True}


@app.post("/api/workspace/{code}/publish")
def publish_snapshot(code: str, conn=Depends(get_db),
                     user=Depends(require_roles("curator"))):
    """Freeze every confirmed, up-to-date translation into an immutable snapshot."""
    with tx(conn):
        lang = get_language_or_404(conn, code)
        payload = workspace_payload(conn, lang)
        if payload["total"] == 0:
            raise conflict("no_labels", "当前没有任何展签，无法发布。")
        if not payload["publishable"]:
            raise conflict("not_publishable",
                          "仍有展签译文缺失、过期或未确认，无法发布。",
                          blockers=payload["blockers"])
        last = conn.execute(
            "SELECT MAX(version) AS v FROM snapshots WHERE language_id = ?",
            (lang["id"],),
        ).fetchone()["v"]
        version = (last or 0) + 1
        now = utcnow()
        cur = conn.execute(
            "INSERT INTO snapshots (language_id, version, label_count,"
            " publisher_id, published_at) VALUES (?, ?, ?, ?, ?)",
            (lang["id"], version, payload["total"], user["id"], now),
        )
        sid = cur.lastrowid
        for it in payload["items"]:
            tr = it["translation"]
            conn.execute(
                "INSERT INTO snapshot_items (snapshot_id, label_code, source_rev,"
                " source_text, trans_rev, trans_text) VALUES (?, ?, ?, ?, ?, ?)",
                (sid, it["label_code"], it["source_current_rev"],
                 it["source_text"], tr["current_rev"], tr["text"]),
            )
    return {"ok": True, "version": version}


@app.get("/api/workspace/{code}/snapshots")
def list_snapshots(code: str, conn=Depends(get_db), user=Depends(require_login)):
    lang = get_language_or_404(conn, code)
    rows = conn.execute(
        """SELECT s.id, s.version, s.label_count, s.published_at,
                  u.display_name AS publisher_name
           FROM snapshots s JOIN users u ON u.id = s.publisher_id
           WHERE s.language_id = ? ORDER BY s.version DESC""",
        (lang["id"],),
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/snapshots/{snapshot_id}")
def snapshot_detail(snapshot_id: int, conn=Depends(get_db),
                    user=Depends(require_login)):
    snap = conn.execute(
        """SELECT s.*, l.code AS language_code, l.name_zh AS language_name,
                  u.display_name AS publisher_name
           FROM snapshots s
           JOIN languages l ON l.id = s.language_id
           JOIN users u ON u.id = s.publisher_id
           WHERE s.id = ?""",
        (snapshot_id,),
    ).fetchone()
    if snap is None:
        raise HTTPException(status_code=404, detail={"code": "not_found",
                                                     "message": "快照不存在。"})
    items = conn.execute(
        "SELECT label_code, source_rev, source_text, trans_rev, trans_text"
        " FROM snapshot_items WHERE snapshot_id = ? ORDER BY label_code",
        (snapshot_id,),
    ).fetchall()
    return {"snapshot": dict(snap), "items": [dict(r) for r in items]}


# ---------------------------------------------------------------------------
# Public visitor API (no login)
# ---------------------------------------------------------------------------


@app.get("/api/public/languages")
def public_languages(conn=Depends(get_db)):
    rows = conn.execute(
        """SELECT l.code, l.name_zh, l.name_en, s.version,
                  s.label_count, s.published_at
           FROM languages l
           LEFT JOIN snapshots s
             ON s.id = (SELECT id FROM snapshots
                        WHERE language_id = l.id ORDER BY version DESC LIMIT 1)
           ORDER BY l.code"""
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/public/languages/{code}")
def public_language(code: str, conn=Depends(get_db)):
    lang = get_language_or_404(conn, code)
    snap = conn.execute(
        """SELECT s.*, u.display_name AS publisher_name FROM snapshots s
           JOIN users u ON u.id = s.publisher_id
           WHERE s.language_id = ? ORDER BY s.version DESC LIMIT 1""",
        (lang["id"],),
    ).fetchone()
    if snap is None:
        return {"language": dict(lang), "snapshot": None, "items": []}
    items = conn.execute(
        "SELECT label_code, source_rev, source_text, trans_rev, trans_text"
        " FROM snapshot_items WHERE snapshot_id = ? ORDER BY label_code",
        (snap["id"],),
    ).fetchall()
    return {"language": dict(lang), "snapshot": dict(snap),
            "items": [dict(r) for r in items]}


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Pages (mounted after API routes)
# ---------------------------------------------------------------------------

@app.get("/")
def root_page():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(BASE_DIR, "static", "login.html"))


@app.get("/workspace")
def workspace_page():
    return FileResponse(os.path.join(BASE_DIR, "static", "workspace.html"))


@app.get("/admin")
def admin_page():
    return FileResponse(os.path.join(BASE_DIR, "static", "admin.html"))


@app.get("/visitor")
def visitor_page():
    return FileResponse(os.path.join(BASE_DIR, "static", "visitor.html"))


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")),
          name="static")


@app.on_event("startup")
def _startup() -> None:
    init_db()
