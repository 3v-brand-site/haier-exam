#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import base64
import gzip
import json
import os
import random
import secrets
import sqlite3
import time
from io import BytesIO
from pathlib import Path

import qrcode
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
STATIC = ROOT / "static"
DB_PATH = DATA / "exam.db"
QUESTIONS_PATH = ROOT / "data" / "questions.json"
PASSWORD_PATH = DATA / "admin_password.txt"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

DRAW = 50
POINTS = 2


def load_bank() -> dict:
    b64 = os.environ.get("QUESTIONS_JSON_B64", "").strip()
    if b64:
        raw = gzip.decompress(base64.b64decode(b64))
        return json.loads(raw.decode("utf-8"))
    if not QUESTIONS_PATH.exists():
        raise RuntimeError("未找到题库：请设置 QUESTIONS_JSON_B64，或提供 data/questions.json")
    return json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))


bank = load_bank()
QUESTIONS = {q["id"]: q for q in bank["questions"]}
assert len(QUESTIONS) >= DRAW


def admin_password() -> str:
    env = os.environ.get("ADMIN_PASSWORD", "").strip()
    if env:
        return env
    DATA.mkdir(parents=True, exist_ok=True)
    if PASSWORD_PATH.exists():
        return PASSWORD_PATH.read_text(encoding="utf-8").strip()
    pwd = secrets.token_urlsafe(10)
    PASSWORD_PATH.write_text(pwd + "\n", encoding="utf-8")
    PASSWORD_PATH.chmod(0o600)
    return pwd


def _use_postgres() -> bool:
    return bool(DATABASE_URL)


def _sql(query: str) -> str:
    if _use_postgres():
        return query.replace("?", "%s")
    return query


def _pg_conninfo(url: str) -> str:
    lower = url.lower()
    if "sslmode=" not in lower and "localhost" not in lower and "127.0.0.1" not in lower:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


class DB:
    def __init__(self):
        if _use_postgres():
            import psycopg
            from psycopg.rows import dict_row

            self._conn = psycopg.connect(_pg_conninfo(DATABASE_URL), row_factory=dict_row)
        else:
            DATA.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(DB_PATH)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")

    def execute(self, query, params=()):
        return self._conn.execute(_sql(query), params)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def db() -> DB:
    return DB()


def init_db() -> None:
    conn = db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                outlet TEXT NOT NULL,
                question_ids TEXT NOT NULL,
                started_at REAL NOT NULL,
                submitted_at REAL,
                score INTEGER,
                answers TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_name_outlet ON sessions(name, outlet)"
        )
        conn.commit()
    finally:
        conn.close()


init_db()

app = FastAPI(title="海尔空调维修测试")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def public_base(request: Request) -> str:
    env = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if env:
        return env
    render = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if render:
        return render
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    return f"{proto}://{host}"


def require_admin(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "需要管理员登录")
    token = authorization[7:].strip()
    if not secrets.compare_digest(token, admin_password()):
        raise HTTPException(401, "密码错误")


def public_question(q: dict) -> dict:
    return {
        "id": q["id"],
        "type": q["type"],
        "stem": q["stem"],
        "options": q["options"],
        "hint": "多选题，全部选对才得分" if q["type"] == "multiple" else None,
    }


def grade(question_ids: list[str], answers: dict) -> int:
    score = 0
    for qid in question_ids:
        q = QUESTIONS[qid]
        raw = answers.get(qid)
        if q["type"] == "multiple":
            got = set(raw or [])
            need = set(q["answer"])
            ok = got == need
        else:
            ok = raw == q["answer"]
        if ok:
            score += POINTS
    return score


class StartBody(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    outlet: str = Field(min_length=1, max_length=80)


class SubmitBody(BaseModel):
    token: str
    answers: dict


class AdminLogin(BaseModel):
    password: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/admin")
def admin_page():
    return FileResponse(STATIC / "admin.html")


@app.get("/api/meta")
def meta():
    return {
        "title": bank.get("title", "海尔空调维修测试"),
        "draw": DRAW,
        "points": POINTS,
        "total": DRAW * POINTS,
        "bank": len(QUESTIONS),
    }


@app.post("/api/start")
def start(body: StartBody):
    name = " ".join(body.name.split())
    outlet = " ".join(body.outlet.split())
    if not name or not outlet:
        raise HTTPException(400, "请填写姓名和网点名")
    conn = db()
    try:
        row = conn.execute(
            "SELECT token, submitted_at FROM sessions WHERE name = ? AND outlet = ? ORDER BY started_at DESC LIMIT 1",
            (name, outlet),
        ).fetchone()
        if row and row["submitted_at"]:
            raise HTTPException(409, "该姓名和网点已经交过卷，不能重复考试")
        if row and not row["submitted_at"]:
            token = row["token"]
            s = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
            ids = json.loads(s["question_ids"])
            return {
                "token": token,
                "name": name,
                "outlet": outlet,
                "resumed": True,
                "questions": [public_question(QUESTIONS[i]) for i in ids],
            }
        ids = random.sample(list(QUESTIONS.keys()), DRAW)
        token = secrets.token_urlsafe(16)
        conn.execute(
            "INSERT INTO sessions(token, name, outlet, question_ids, started_at) VALUES (?,?,?,?,?)",
            (token, name, outlet, json.dumps(ids, ensure_ascii=False), time.time()),
        )
        conn.commit()
        return {
            "token": token,
            "name": name,
            "outlet": outlet,
            "resumed": False,
            "questions": [public_question(QUESTIONS[i]) for i in ids],
        }
    finally:
        conn.close()


@app.get("/api/session/{token}")
def get_session(token: str):
    conn = db()
    try:
        s = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
        if not s:
            raise HTTPException(404, "考试不存在")
        if s["submitted_at"]:
            return {
                "submitted": True,
                "name": s["name"],
                "outlet": s["outlet"],
                "score": s["score"],
                "total": DRAW * POINTS,
            }
        ids = json.loads(s["question_ids"])
        return {
            "submitted": False,
            "token": token,
            "name": s["name"],
            "outlet": s["outlet"],
            "questions": [public_question(QUESTIONS[i]) for i in ids],
        }
    finally:
        conn.close()


@app.post("/api/submit")
def submit(body: SubmitBody):
    conn = db()
    try:
        s = conn.execute("SELECT * FROM sessions WHERE token = ?", (body.token,)).fetchone()
        if not s:
            raise HTTPException(404, "考试不存在")
        if s["submitted_at"]:
            return {"ok": True, "already": True, "score": s["score"], "total": DRAW * POINTS}
        ids = json.loads(s["question_ids"])
        score = grade(ids, body.answers or {})
        conn.execute(
            "UPDATE sessions SET submitted_at=?, score=?, answers=? WHERE token=?",
            (time.time(), score, json.dumps(body.answers, ensure_ascii=False), body.token),
        )
        conn.commit()
        return {"ok": True, "already": False, "score": score, "total": DRAW * POINTS}
    finally:
        conn.close()


@app.post("/api/admin/login")
def admin_login(body: AdminLogin):
    if not secrets.compare_digest(body.password.strip(), admin_password()):
        raise HTTPException(401, "密码错误")
    return {"ok": True, "token": admin_password()}


@app.get("/api/admin/results")
def admin_results(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    conn = db()
    try:
        rows = conn.execute(
            "SELECT token, name, outlet, started_at, submitted_at, score FROM sessions ORDER BY submitted_at IS NULL, submitted_at DESC, started_at DESC"
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["token"],
                "name": r["name"],
                "outlet": r["outlet"],
                "started_at": r["started_at"],
                "submitted_at": r["submitted_at"],
                "score": r["score"],
                "total": DRAW * POINTS,
                "status": "已交卷" if r["submitted_at"] else "考试中",
            })
        return {"results": out, "count": len(out), "submitted": sum(1 for x in out if x["submitted_at"])}
    finally:
        conn.close()


@app.delete("/api/admin/session/{sid}")
def admin_delete(sid: str, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    conn = db()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (sid,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/admin/export.csv")
def admin_export(authorization: str | None = Header(default=None), token: str | None = Query(default=None)):
    auth = authorization
    if token and not auth:
        auth = f"Bearer {token}"
    require_admin(auth)
    conn = db()
    try:
        rows = conn.execute(
            "SELECT name, outlet, started_at, submitted_at, score FROM sessions ORDER BY submitted_at IS NULL, score DESC, submitted_at DESC"
        ).fetchall()
    finally:
        conn.close()

    def fmt(ts):
        if not ts:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    lines = ["姓名,网点,开始时间,交卷时间,状态,分数,满分"]
    for r in rows:
        status = "已交卷" if r["submitted_at"] else "考试中"
        score = "" if r["score"] is None else str(r["score"])

        def esc(s):
            s = "" if s is None else str(s)
            if any(c in s for c in ',\"\n'):
                return '"' + s.replace('"', '""') + '"'
            return s

        lines.append(",".join([
            esc(r["name"]), esc(r["outlet"]), esc(fmt(r["started_at"])),
            esc(fmt(r["submitted_at"])), status, score, str(DRAW * POINTS),
        ]))
    data = ("\ufeff" + "\n".join(lines) + "\n").encode("utf-8")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=haier-exam-results.csv"},
    )


def make_qr(url: str) -> bytes:
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=12, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@app.get("/qr.png")
def qr_png(request: Request):
    url = public_base(request) + "/"
    return Response(content=make_qr(url), media_type="image/png")


@app.get("/api/exam-url")
def exam_url(request: Request):
    url = public_base(request) + "/"
    return {"url": url}
