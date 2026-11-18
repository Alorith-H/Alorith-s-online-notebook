#!/usr/bin/env python3
"""把 WenChang 的 source.json / questions.json 导入 SQLite。幂等：重建全库。"""
import json
import os
import sqlite3

from pypinyin import Style, lazy_pinyin


def py_initials(s):
    """汉字转拼音首字母（lx 形式），非汉字保留原样后去掉"""
    if not s:
        return ""
    out = lazy_pinyin(s, style=Style.FIRST_LETTER, errors=lambda x: ["" ])
    return "".join(x for x in out if x and x.isascii() and x.isalnum()).lower()

SRC = "/root/workspace/projects/wenchang-data/source.json"
JSJ = "/root/workspace/projects/wenchang-data/jsj.json"
ENG = "/root/workspace/projects/wenchang-data/eng.json"
QST = "/root/workspace/projects/wenchang-data/questions.json"
ENGQ = "/root/workspace/projects/wenchang-data/eng_questions.json"
DB = "/var/lib/notebook/wenchang.db"

os.makedirs(os.path.dirname(DB), exist_ok=True)
src = json.load(open(SRC, encoding="utf-8"))
qs = json.load(open(QST, encoding="utf-8"))
if os.path.exists(ENGQ):
    qs = qs + json.load(open(ENGQ, encoding="utf-8"))

if os.path.exists(DB):
    os.remove(DB)
db = sqlite3.connect(DB)
c = db.cursor()
c.executescript("""
CREATE TABLE chapters(id TEXT PRIMARY KEY, title TEXT, ord INTEGER, subject TEXT, hidden INTEGER DEFAULT 0);
CREATE TABLE sections(id TEXT PRIMARY KEY, chapter_id TEXT REFERENCES chapters(id),
                      title TEXT, ord INTEGER);
CREATE TABLE items(id INTEGER PRIMARY KEY AUTOINCREMENT, section_id TEXT REFERENCES sections(id),
                   title TEXT, ord INTEGER, parent_id INTEGER);
CREATE TABLE bullets(id INTEGER PRIMARY KEY AUTOINCREMENT, section_id TEXT REFERENCES sections(id),
                     item_id INTEGER, text TEXT, is_key INTEGER, segs TEXT, py TEXT, ord INTEGER);
CREATE TABLE questions(id TEXT PRIMARY KEY, sec TEXT, q TEXT, a TEXT, src TEXT, hidden INTEGER DEFAULT 0);
CREATE INDEX idx_bullets_sec ON bullets(section_id);
CREATE INDEX idx_items_sec ON items(section_id);
CREATE INDEX idx_q_sec ON questions(sec);
""")

def import_book(book, subject):
    for ci, ch in enumerate(book["chapters"]):
        c.execute("INSERT INTO chapters(id,title,ord,subject,hidden) VALUES (?,?,?,?,?)",
                  (ch["id"], ch["title"], ci, subject, 1 if ch.get("hidden") else 0))
        for si, sec in enumerate(ch["sections"]):
            c.execute("INSERT INTO sections VALUES (?,?,?,?)", (sec["id"], ch["id"], sec["title"], si))
            ord_ = 0

            def put_items(items, parent_id, ord_start):
                """递归写入分组（支持选定区这类二级分组）"""
                nonlocal ord_
                for ii, item in enumerate(items or []):
                    c.execute("INSERT INTO items(section_id,title,ord,parent_id) VALUES (?,?,?,?)",
                              (sec["id"], item.get("title", ""), ord_start + ii, parent_id))
                    item_rowid = c.lastrowid
                    for b in item.get("bullets") or []:
                        c.execute("INSERT INTO bullets(section_id,item_id,text,is_key,segs,py,ord) VALUES (?,?,?,?,?,?,?)",
                                  (sec["id"], item_rowid, b.get("t", ""), 1 if b.get("key") else 0,
                                   json.dumps(b.get("segs") or [], ensure_ascii=False), py_initials(b.get("t", "")), ord_))
                        ord_ += 1
                    if item.get("items"):
                        put_items(item["items"], item_rowid, 0)

            put_items(sec.get("items"), None, 0)
            for b in sec.get("bullets") or []:
                c.execute("INSERT INTO bullets(section_id,item_id,text,is_key,segs,py,ord) VALUES (?,?,?,?,?,?,?)",
                          (sec["id"], None, b.get("t", ""), 1 if b.get("key") else 0,
                           json.dumps(b.get("segs") or [], ensure_ascii=False), py_initials(b.get("t", "")), ord_))
                ord_ += 1


import_book(src, "yuwen")
if os.path.exists(ENG):
    import_book(json.load(open(ENG, encoding="utf-8")), "eng")

HIDDEN_FILE = "/root/workspace/projects/wenchang-data/hidden_questions.json"
HIDDEN = set()
if os.path.exists(HIDDEN_FILE):
    try:
        HIDDEN = set(json.load(open(HIDDEN_FILE, encoding="utf-8")))
    except Exception:
        HIDDEN = set()
print("隐藏题目:", len(HIDDEN))

EXTRA = "/root/workspace/projects/wenchang-data/extra_2000.json"
if os.path.exists(EXTRA):
    ex = json.load(open(EXTRA, encoding="utf-8"))
    for ch in ex["chapters"]:
        c.execute("INSERT OR REPLACE INTO chapters(id,title,ord,subject,hidden) VALUES (?,?,?,?,?)",
                  (ch["id"], ch["title"], ch["ord"], ch["subject"], ch.get("hidden", 1)))
    for sec in ex["sections"]:
        c.execute("INSERT OR REPLACE INTO sections VALUES (?,?,?,?)",
                  (sec["id"], sec["chapter_id"], sec["title"], sec["ord"]))
    qs = qs + ex["questions"]

for q in qs:
    c.execute("INSERT OR REPLACE INTO questions(id,sec,q,a,src,hidden) VALUES (?,?,?,?,?,?)",
              (q["id"], q["sec"], q["q"], q.get("a", ""), q.get("src") or None,
               1 if q["id"] in HIDDEN else 0))
db.commit()

# 校验
for t in ["chapters", "sections", "items", "bullets", "questions"]:
    print(t, c.execute("SELECT COUNT(*) FROM " + t).fetchone()[0])
print("红字考点:", c.execute("SELECT COUNT(*) FROM bullets WHERE is_key=1").fetchone()[0])
db.close()
print("OK ->", DB)
