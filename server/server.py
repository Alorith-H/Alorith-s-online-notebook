#!/usr/bin/env python3
"""文常卡片 API — SQLite 存储，纯标准库实现。供网页与手机 App 访问。
端点：
  GET /api/notebook/overview           章节目录
  GET /api/notebook/section/<id>       板块详情（要点+红字）
  GET /api/notebook/search?q=关键词    全文搜索要点
  GET /api/notebook/questions?sec=a,b 或 ?chapter=c1 或 ?all=1
  GET /api/notebook/stats              数据统计
  GET /api/notebook/papers             试卷列表（按来源聚合）
  POST /api/notebook/login             用户名登录（无密码），返回 token + 云端数据
  GET /api/notebook/state?token=       读取云端学习数据
  POST /api/notebook/state             保存云端学习数据
  POST /api/notebook/logout            退出（作废 token）
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = "127.0.0.1"
PORT = 8789
DB = "/var/lib/notebook/wenchang.db"
USERS_DB = "/var/lib/notebook/users.db"
SECRET_FILE = "/var/lib/notebook/users.secret"
SUBJECTS = ("yuwen", "jsj", "eng")
NAME_RE = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9_-]{2,20}$")

_local = threading.local()


REPORTS_DB = "/var/lib/notebook/reports.db"
HIDDEN_FILE = "/root/workspace/projects/wenchang-data/hidden_questions.json"


def rdb():
    if not hasattr(_local, "rconn"):
        _local.rconn = sqlite3.connect(REPORTS_DB)
        _local.rconn.row_factory = sqlite3.Row
        _local.rconn.execute(
            "CREATE TABLE IF NOT EXISTS reports("
            " rid INTEGER PRIMARY KEY AUTOINCREMENT,"
            " qid TEXT, subject TEXT, q TEXT, a TEXT, note TEXT,"
            " created_at TEXT, status TEXT DEFAULT 'pending')")
        _local.rconn.commit()
    return _local.rconn


def db():
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(DB)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn


def segs_of(row):
    try:
        return json.loads(row["segs"] or "[]")
    except json.JSONDecodeError:
        return [[row["text"], 0]]


def udb():
    """用户数据独立库，避免重新导入题库时被删。"""
    if not hasattr(_local, "uconn"):
        os.makedirs(os.path.dirname(USERS_DB), exist_ok=True)
        c = sqlite3.connect(USERS_DB)
        c.row_factory = sqlite3.Row
        c.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uname_hash TEXT UNIQUE NOT NULL,
  created_at TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS user_state(
  user_id INTEGER PRIMARY KEY,
  data TEXT NOT NULL,
  updated_at TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  created_at TEXT);
""")
        cols = [r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()]
        for col, typ in (("pw_salt", "TEXT"), ("pw_hash", "TEXT"), ("pw_iter", "INTEGER")):
            if col not in cols:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
        c.commit()
        _local.uconn = c
    return _local.uconn


def _secret():
    try:
        with open(SECRET_FILE, encoding="utf-8") as f:
            v = f.read().strip()
            if v:
                return v
    except FileNotFoundError:
        pass
    v = secrets.token_hex(32)
    os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
    with open(SECRET_FILE, "w", encoding="utf-8") as f:
        f.write(v)
    os.chmod(SECRET_FILE, 0o600)
    return v


def name_hash(name):
    """用户名加盐哈希后入库，库里不存明文。"""
    return hmac.new(_secret().encode(), name.strip().lower().encode(), hashlib.sha256).hexdigest()


PW_RE = re.compile(r"^[^\s\x00-\x1f]{6,32}$")
_FAILS = {}          # name_hash -> (count, until_ts)
_FAIL_LOCK = 5
_FAIL_WINDOW = 30.0


def pw_make(password):
    salt = secrets.token_hex(16)
    iters = 120000
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iters).hex()
    return salt, h, iters


def pw_check(password, salt, expect, iters):
    if not salt or not expect:
        return False
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iters or 120000)).hex()
    return hmac.compare_digest(h, expect)


def throttled(key):
    import time as _t
    rec = _FAILS.get(key)
    if not rec:
        return 0
    cnt, until = rec
    left = until - _t.time()
    return int(left) if left > 0 else 0


def note_fail(key):
    import time as _t
    cnt, until = _FAILS.get(key, (0, 0))
    cnt += 1
    if cnt >= _FAIL_LOCK:
        _FAILS[key] = (0, _t.time() + _FAIL_WINDOW)
    else:
        _FAILS[key] = (cnt, 0)


def clear_fail(key):
    _FAILS.pop(key, None)


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Handler(BaseHTTPRequestHandler):
    server_version = "NotebookAPI/1.0"

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}
        try:
            if path == "/api/notebook/login":
                self.login(body)
            elif path == "/api/notebook/register":
                self.register(body)
            elif path == "/api/notebook/state":
                self.save_state(body)
            elif path == "/api/notebook/logout":
                self.logout(body)
            elif path == "/api/notebook/report":
                self.report(body)
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            try:
                self._send(500, {"error": str(e)})
            except BrokenPipeError:
                pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path.rstrip("/")
        try:
            if path == "/api/notebook/overview":
                self.overview((q.get("subject") or ["yuwen"])[0])
            elif path.startswith("/api/notebook/section/"):
                self.section(path.rsplit("/", 1)[1])
            elif path == "/api/notebook/search":
                self.search((q.get("q") or [""])[0], (q.get("subject") or ["yuwen"])[0])
            elif path == "/api/notebook/questions":
                self.questions(q)
            elif path == "/api/notebook/papers":
                self.papers((q.get("subject") or ["yuwen"])[0])
            elif path == "/api/notebook/state":
                self.get_state(q)
            elif path == "/api/notebook/stats":
                self.stats((q.get("subject") or ["yuwen"])[0])
            elif path == "/api/notebook/ids":
                self.ids((q.get("subject") or ["yuwen"])[0])
            elif path == "/api/notebook/reports":
                self.list_reports()
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            try:
                self._send(500, {"error": str(e)})
            except BrokenPipeError:
                pass

    def overview(self, subject="yuwen"):
        con = db()
        chs = con.execute("SELECT * FROM chapters WHERE COALESCE(subject,'yuwen')=? AND COALESCE(hidden,0)=0 ORDER BY ord",
                          (subject,)).fetchall()
        ids = [c["id"] for c in chs] or [""]
        ph = ",".join("?" * len(ids))
        secs = con.execute(
            "SELECT s.id, s.title, s.chapter_id, s.ord,"
            " (SELECT COUNT(*) FROM bullets b WHERE b.section_id = s.id) AS n_b,"
            " (SELECT COUNT(*) FROM questions q WHERE q.sec = s.id AND COALESCE(q.hidden,0)=0) AS n_q"
            " FROM sections s WHERE s.chapter_id IN (" + ph + ") ORDER BY s.chapter_id, s.ord",
            tuple(ids)).fetchall()
        by_ch = {}
        for s in secs:
            by_ch.setdefault(s["chapter_id"], []).append(
                {"id": s["id"], "title": s["title"], "bullets": s["n_b"], "questions": s["n_q"]})
        total_q = con.execute(
            "SELECT COUNT(*) FROM questions WHERE COALESCE(hidden,0)=0 AND sec IN (SELECT id FROM sections WHERE chapter_id IN "
            "(SELECT id FROM chapters WHERE COALESCE(subject,'yuwen')=?))", (subject,)).fetchone()[0]
        self._send(200, {"chapters": [
            {"id": c["id"], "title": c["title"], "sections": by_ch.get(c["id"], [])} for c in chs],
            "total_questions": total_q})

    def section(self, sid):
        con = db()
        sec = con.execute("SELECT s.*, c.title AS chapter_title FROM sections s"
                          " JOIN chapters c ON c.id = s.chapter_id WHERE s.id = ?", (sid,)).fetchone()
        if not sec:
            self._send(404, {"error": "no such section"})
            return
        items = con.execute("SELECT * FROM items WHERE section_id = ? ORDER BY ord, id", (sid,)).fetchall()
        by_item = {}
        for b in con.execute("SELECT * FROM bullets WHERE section_id = ? ORDER BY ord", (sid,)).fetchall():
            by_item.setdefault(b["item_id"], []).append(
                {"t": b["text"], "key": bool(b["is_key"]), "segs": segs_of(b)})

        nodes = {}
        order = []
        for it in items:
            nodes[it["id"]] = {"title": it["title"], "bullets": by_item.get(it["id"], []), "items": []}
            order.append(it)
        out_items = []
        for it in order:
            parent = it["parent_id"] if "parent_id" in it.keys() else None
            node = nodes[it["id"]]
            if parent and parent in nodes:
                nodes[parent]["items"].append(node)
            else:
                out_items.append(node)
        for n in nodes.values():
            if not n["items"]:
                del n["items"]
        loose = by_item.get(None, [])
        self._send(200, {"id": sec["id"], "title": sec["title"], "chapter": sec["chapter_title"],
                         "items": out_items, "bullets": loose})

    def search(self, kw, subject="yuwen"):
        kw = kw.strip()
        con = db()
        base_sql = ("SELECT b.text AS t, b.segs, b.is_key, b.section_id, s.title AS sec_title,"
                    " c.title AS ch_title FROM bullets b JOIN sections s ON s.id = b.section_id"
                    " JOIN chapters c ON c.id = s.chapter_id ")
        sub = " AND COALESCE(c.subject,'yuwen')=? "      # 只搜本学科
        q_base = ("SELECT q.id, q.sec, q.q, q.a, q.src, q.explain FROM questions q"
                  " JOIN sections s2 ON s2.id = q.sec JOIN chapters c2 ON c2.id = s2.chapter_id ")
        empty = {"results": [], "questions": [], "kw": kw, "mode": "exact", "fuzzy": False}
        if not kw:
            self._send(200, empty)
            return

        def pack(r):
            return {"t": r["t"], "segs": segs_of(r), "key": bool(r["is_key"]),
                    "sec": r["section_id"], "sec_title": r["sec_title"], "ch_title": r["ch_title"]}

        qn = re.sub(r"\s+", "", kw).lower()
        is_py = bool(re.match(r"^[a-z]{1,15}$", qn))

        # 通配符模式：* 任意多个字，? 单个字（支持全角＊？）
        wq = kw.replace("＊", "*").replace("？", "?")
        if "*" in wq or "?" in wq:
            pat = []
            for ch in wq:
                if ch == "*":
                    pat.append("%")
                elif ch == "?":
                    pat.append("_")
                elif ch in ("%", "_", "\\"):
                    pat.append("\\" + ch)
                else:
                    pat.append(ch)
            pat_sql = "%" + "".join(pat) + "%"
            rows = con.execute(base_sql + "WHERE b.text LIKE ? ESCAPE '\\'" + sub + "ORDER BY s.id LIMIT 100",
                               (pat_sql, subject)).fetchall()
            out = []
            seen = set()
            for r in rows:
                key = (r["t"], r["section_id"])
                if key in seen:
                    continue
                seen.add(key)
                item = pack(r)
                item["wildcard"] = True
                out.append(item)
            # 纯字母通配（如 lx*）同时匹配拼音首字母
            if re.match(r"^[a-z*?]+$", qn):
                for r in con.execute(base_sql + "WHERE b.py LIKE ? ESCAPE '\\'" + sub + "ORDER BY s.id LIMIT 60",
                                     (pat_sql, subject)).fetchall():
                    key = (r["t"], r["section_id"])
                    if key in seen:
                        continue
                    seen.add(key)
                    item = pack(r)
                    item["wildcard"] = True
                    out.append(item)
            qs_w = con.execute(q_base + "WHERE (q.q LIKE ? ESCAPE '\\' OR q.a LIKE ? ESCAPE '\\'"
                               " OR q.src LIKE ? ESCAPE '\\') AND COALESCE(c2.subject,'yuwen')=? AND COALESCE(q.hidden,0)=0 LIMIT 60",
                               (pat_sql, pat_sql, pat_sql, subject)).fetchall()
            self._send(200, {"kw": kw, "results": out, "questions": [dict(x) for x in qs_w],
                             "mode": "wildcard", "fuzzy": False})
            return

        # 第一级：精确子串
        exact = con.execute(base_sql + "WHERE b.text LIKE ?" + sub + "ORDER BY s.id LIMIT 100",
                            ("%" + kw + "%", subject)).fetchall()
        results = [pack(r) for r in exact]
        seen = set((x["t"], x["sec"]) for x in results)
        mode = "exact"

        # 第二级：拼音首字母（lx -> 鲁迅）
        if is_py:
            mode = "pinyin"
            for r in con.execute(base_sql + "WHERE b.py LIKE ?" + sub + "ORDER BY s.id LIMIT 60",
                                 ("%" + qn + "%", subject)).fetchall():
                key = (r["t"], r["section_id"])
                if key in seen:
                    continue
                seen.add(key)
                item = pack(r)
                item["approx"] = True
                results.append(item)

        # 第三级：近似词（新月社 -> 新月派），二元组 + 字覆盖率打分
        if (not results) or (not is_py and len(results) < 3):
            if mode == "exact" and not is_py:
                mode = "fuzzy" if not results else "exact+fuzzy"

            def norm(s):
                return re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", s or "").lower()

            qqn = norm(qn)
            if len(qqn) >= 2:
                bigrams = set(qqn[i:i + 2] for i in range(len(qqn) - 1))
                chars = set(qqn)
                scored = []
                for r in con.execute(base_sql + "WHERE COALESCE(c.subject,'yuwen')=?", (subject,)).fetchall():
                    tx = norm(r["t"])
                    if not tx:
                        continue
                    bg = sum(1 for g in bigrams if g in tx) / float(len(bigrams))
                    cc = sum(1 for ch in chars if ch in tx) / float(len(chars))
                    if bg >= 0.5 or (cc >= 0.75 and bg >= 0.25):
                        scored.append((bg * 0.65 + cc * 0.35, r))
                scored.sort(key=lambda x: -x[0])
                for sc, r in scored[:30]:
                    key = (r["t"], r["section_id"])
                    if key in seen:
                        continue
                    seen.add(key)
                    item = pack(r)
                    item["approx"] = True
                    results.append(item)

        # 章节 / 板块标题也算命中（如「Day 5」「五、唐代」）
        heads = con.execute(
            "SELECT c.title AS t, MIN(s2.id) AS sid, s2.title AS sec_title"
            " FROM chapters c JOIN sections s2 ON s2.chapter_id = c.id"
            " WHERE c.title LIKE ? AND COALESCE(c.subject,'yuwen')=? AND COALESCE(c.hidden,0)=0"
            " GROUP BY c.id ORDER BY c.ord LIMIT 20", ("%" + kw + "%", subject)).fetchall()
        secs = con.execute(
            "SELECT s2.title AS t, s2.id AS sid, s2.title AS sec_title, c.title AS ch_title"
            " FROM sections s2 JOIN chapters c ON c.id = s2.chapter_id"
            " WHERE s2.title LIKE ? AND COALESCE(c.subject,'yuwen')=? AND COALESCE(c.hidden,0)=0 LIMIT 20",
            ("%" + kw + "%", subject)).fetchall()
        head_rows = []
        for r in heads:
            head_rows.append({"t": "📘 " + r["t"], "segs": [], "key": False, "sec": r["sid"],
                              "sec_title": r["sec_title"], "ch_title": "", "item": True})
        for r in secs:
            head_rows.append({"t": "📄 " + r["t"], "segs": [], "key": False, "sec": r["sid"],
                              "sec_title": r["sec_title"], "ch_title": r["ch_title"], "item": True})

        # 分组标题也算命中（快捷键、选定区这类）
        its = con.execute(
            "SELECT i.title AS t, i.section_id AS sid, s.title AS sec_title, c.title AS ch_title"
            " FROM items i JOIN sections s ON s.id = i.section_id JOIN chapters c ON c.id = s.chapter_id"
            " WHERE i.title LIKE ? AND COALESCE(c.subject,'yuwen')=? LIMIT 30",
            ("%" + kw + "%", subject)).fetchall()
        head = []
        for r in its:
            head.append({"t": r["t"], "segs": [], "key": False, "sec": r["sid"],
                         "sec_title": r["sec_title"], "ch_title": r["ch_title"], "item": True})
        if head_rows or head:
            results = head_rows + head + results

        qs = con.execute(q_base + "WHERE (q.q LIKE ? OR q.a LIKE ? OR q.src LIKE ?)"
                         " AND COALESCE(c2.subject,'yuwen')=? AND COALESCE(q.hidden,0)=0 LIMIT 60",
                         ("%" + kw + "%", "%" + kw + "%", "%" + kw + "%", subject)).fetchall()
        self._send(200, {"kw": kw, "results": results, "questions": [dict(x) for x in qs],
                         "mode": mode, "fuzzy": any(x.get("approx") for x in results)})

    # ---------- 用户 / 同步 ----------
    def _uid(self, token):
        if not token:
            return None
        con = udb()
        row = con.execute("SELECT user_id FROM sessions WHERE token=?", (str(token),)).fetchone()
        return row["user_id"] if row else None

    def _check_name_pw(self, body):
        name = str(body.get("name") or "").strip()
        pw = str(body.get("password") or "")
        if not NAME_RE.match(name):
            self._send(400, {"error": "用户名需 2-20 位，只能包含汉字、字母、数字、下划线或连字符"})
            return None, None, None
        if not PW_RE.match(pw):
            self._send(400, {"error": "密码需 6-32 位，不能含空格或控制字符"})
            return None, None, None
        return name, pw, name_hash(name)

    def _issue(self, con, uid, name, subject="yuwen"):
        now = utcnow()
        con.execute("UPDATE users SET last_seen=? WHERE id=?", (now, uid))
        token = secrets.token_hex(24)
        con.execute("INSERT INTO sessions(token, user_id, created_at) VALUES (?,?,?)", (token, uid, now))
        con.commit()
        st = con.execute("SELECT data FROM user_state WHERE user_id=? AND subject=?",
                         (uid, subject)).fetchone()
        data = None
        if st:
            try:
                data = json.loads(st["data"])
            except Exception:
                data = None
        self._send(200, {"token": token, "name": name, "data": data})

    def login(self, body):
        name, pw, h = self._check_name_pw(body)
        if name is None:
            return
        wait = throttled(h)
        if wait:
            self._send(429, {"error": "密码错误次数过多，请 %d 秒后再试" % wait})
            return
        con = udb()
        row = con.execute("SELECT id, pw_salt, pw_hash, pw_iter FROM users WHERE uname_hash=?", (h,)).fetchone()
        if not row:
            # 没注册过：交给前端弹「是否注册」
            self._send(200, {"need_register": True, "name": name})
            return
        if not row["pw_hash"]:
            # 老账号（无密码）首次登录：同样走注册流程认领
            self._send(200, {"need_register": True, "name": name, "claim": True})
            return
        if not pw_check(pw, row["pw_salt"], row["pw_hash"], row["pw_iter"]):
            note_fail(h)
            self._send(401, {"error": "密码不正确"})
            return
        clear_fail(h)
        subject = str(body.get("subject") or "yuwen")
        if subject not in SUBJECTS:
            subject = "yuwen"
        self._issue(con, row["id"], name, subject)

    def register(self, body):
        name, pw, h = self._check_name_pw(body)
        if name is None:
            return
        con = udb()
        now = utcnow()
        subject = str(body.get("subject") or "yuwen")
        if subject not in SUBJECTS:
            subject = "yuwen"
        salt, ph, iters = pw_make(pw)
        row = con.execute("SELECT id, pw_hash FROM users WHERE uname_hash=?", (h,)).fetchone()
        if row and row["pw_hash"]:
            self._send(409, {"error": "该用户名已被注册，请直接登录"})
            return
        if row:
            uid = row["id"]
            con.execute("UPDATE users SET pw_salt=?, pw_hash=?, pw_iter=?, last_seen=? WHERE id=?",
                        (salt, ph, iters, now, uid))
        else:
            uid = con.execute(
                "INSERT INTO users(uname_hash, created_at, last_seen, pw_salt, pw_hash, pw_iter)"
                " VALUES (?,?,?,?,?,?)", (h, now, now, salt, ph, iters)).lastrowid
        con.commit()
        clear_fail(h)
        self._issue(con, uid, name, subject)

    def get_state(self, q):
        uid = self._uid((q.get("token") or [""])[0])
        if not uid:
            self._send(401, {"error": "登录已失效，请重新登录"})
            return
        subject = (q.get("subject") or ["yuwen"])[0] or "yuwen"
        con = udb()
        st = con.execute("SELECT data, updated_at FROM user_state WHERE user_id=? AND subject=?",
                         (uid, subject)).fetchone()
        data = None
        if st:
            try:
                data = json.loads(st["data"])
            except Exception:
                data = None
        self._send(200, {"data": data, "updated_at": (st["updated_at"] if st else None)})

    def save_state(self, body):
        uid = self._uid(body.get("token"))
        if not uid:
            self._send(401, {"error": "登录已失效，请重新登录"})
            return
        subject = str(body.get("subject") or "yuwen")
        if subject not in SUBJECTS:
            subject = "yuwen"
        data = body.get("data")
        if not isinstance(data, dict):
            self._send(400, {"error": "数据格式不正确"})
            return
        blob = json.dumps(data, ensure_ascii=False)
        if len(blob) > 4_000_000:
            self._send(413, {"error": "数据过大"})
            return
        con = udb()
        now = utcnow()
        con.execute("INSERT INTO user_state(user_id, subject, data, updated_at) VALUES (?,?,?,?)"
                    " ON CONFLICT(user_id, subject) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                    (uid, subject, blob, now))
        con.commit()
        self._send(200, {"ok": True, "updated_at": now})

    def logout(self, body):
        con = udb()
        con.execute("DELETE FROM sessions WHERE token=?", (str(body.get("token") or ""),))
        con.commit()
        self._send(200, {"ok": True})

    def papers(self, subject="yuwen"):
        """只把真正的试卷算进「试卷」分类；英语的 Day N 属于章节，不算试卷。"""
        con = db()
        rows = con.execute(
            "SELECT q.src AS s, COUNT(*) AS n FROM questions q"
            " JOIN sections s2 ON s2.id = q.sec JOIN chapters c ON c.id = s2.chapter_id"
            " WHERE q.src IS NOT NULL AND q.src <> '' AND q.src NOT LIKE 'Day %' AND COALESCE(q.hidden,0)=0"
            " AND COALESCE(c.subject,'yuwen')=? GROUP BY q.src", (subject,)).fetchall()

        def key(r):
            m = re.match(r"(\d+)月", r["s"] or "")
            return (int(m.group(1)) if m else 99, r["s"])

        self._send(200, {"papers": [{"src": r["s"], "n": r["n"]} for r in sorted(rows, key=key)]})

    def questions(self, q):
        con = db()
        limit = min(int((q.get("limit") or ["100"])[0]), 2000)
        if (q.get("all") or [""])[0]:
            limit = 100000
        if (q.get("sec") or [""])[0]:
            ids = [x for x in (q.get("sec") or [""])[0].split(",") if x]
            ph = ",".join("?" * len(ids))
            rows = con.execute(f"SELECT * FROM questions WHERE sec IN ({ph}) AND COALESCE(hidden,0)=0 LIMIT ?", (*ids, limit)).fetchall()
        elif (q.get("chapter") or [""])[0]:
            rows = con.execute(
                "SELECT q.* FROM questions q JOIN sections s ON s.id = q.sec WHERE s.chapter_id = ? AND COALESCE(q.hidden,0)=0 LIMIT ?",
                ((q.get("chapter") or [""])[0], limit)).fetchall()
        else:
            subject = (q.get("subject") or ["yuwen"])[0]
            rows = con.execute(
                "SELECT q.* FROM questions q JOIN sections s ON s.id = q.sec "
                "JOIN chapters c ON c.id = s.chapter_id "
                "WHERE COALESCE(c.subject,'yuwen')=? AND COALESCE(q.hidden,0)=0 LIMIT ?", (subject, limit)).fetchall()
        self._send(200, {"questions": [dict(x) for x in rows]})

    def ids(self, subject="yuwen"):
        """可见题目 id 列表，供前端把本地统计对齐到真正能抽到的题。"""
        con = db()
        rows = con.execute(
            "SELECT q.id FROM questions q JOIN sections s ON s.id = q.sec "
            "JOIN chapters c ON c.id = s.chapter_id "
            "WHERE COALESCE(c.subject,'yuwen')=? AND COALESCE(q.hidden,0)=0", (subject,)).fetchall()
        self._send(200, {"ids": [r["id"] for r in rows], "count": len(rows)})

    def report(self, body):
        """提交纠错：写入独立纠错库，并把该题暂时隐藏。"""
        qid = (body.get("id") or "").strip()
        if not qid:
            self._send(400, {"error": "missing id"})
            return
        con = db()
        row = con.execute("SELECT id, q, a, src, sec FROM questions WHERE id = ?", (qid,)).fetchone()
        if not row:
            self._send(404, {"error": "题目不存在"})
            return
        subject = (body.get("subject") or "yuwen").strip()
        note = (body.get("note") or "").strip()[:500]
        r = rdb()
        r.execute("INSERT INTO reports(qid, subject, q, a, note, created_at) VALUES (?,?,?,?,?,datetime('now','localtime'))",
                  (qid, subject, row["q"], row["a"] or "", note))
        r.commit()
        con.execute("UPDATE questions SET hidden = 1 WHERE id = ?", (qid,))
        con.commit()
        try:
            ids = set()
            if os.path.exists(HIDDEN_FILE):
                ids = set(json.load(open(HIDDEN_FILE, encoding="utf-8")))
            ids.add(qid)
            json.dump(sorted(ids), open(HIDDEN_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except Exception:
            pass
        self._send(200, {"ok": True, "qid": qid, "hidden": True})

    def list_reports(self):
        r = rdb()
        rows = r.execute("SELECT * FROM reports ORDER BY rid DESC LIMIT 500").fetchall()
        self._send(200, {"reports": [dict(x) for x in rows], "count": len(rows)})

    def stats(self, subject="yuwen"):
        con = db()
        ch = "SELECT id FROM chapters WHERE COALESCE(subject,'yuwen')=? AND COALESCE(hidden,0)=0"
        out = {"chapters": con.execute("SELECT COUNT(*) FROM (" + ch + ")", (subject,)).fetchone()[0]}
        out["sections"] = con.execute(
            "SELECT COUNT(*) FROM sections WHERE chapter_id IN (" + ch + ")", (subject,)).fetchone()[0]
        out["items"] = con.execute(
            "SELECT COUNT(*) FROM items WHERE section_id IN (SELECT id FROM sections WHERE chapter_id IN (" + ch + "))",
            (subject,)).fetchone()[0]
        out["bullets"] = con.execute(
            "SELECT COUNT(*) FROM bullets WHERE section_id IN (SELECT id FROM sections WHERE chapter_id IN (" + ch + "))",
            (subject,)).fetchone()[0]
        out["key_points"] = con.execute(
            "SELECT COUNT(*) FROM bullets WHERE is_key=1 AND section_id IN (SELECT id FROM sections WHERE chapter_id IN (" + ch + "))",
            (subject,)).fetchone()[0]
        ch_all = "SELECT id FROM chapters WHERE COALESCE(subject,'yuwen')=?"
        out["questions"] = con.execute(
            "SELECT COUNT(*) FROM questions WHERE COALESCE(hidden,0)=0 AND sec IN (SELECT id FROM sections WHERE chapter_id IN (" + ch_all + "))",
            (subject,)).fetchone()[0]
        self._send(200, out)


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print("notebook api on %s:%d" % (HOST, PORT), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
