#!/usr/bin/env python3
"""纠错库管理。

用法：
  python3 manage_reports.py list                # 列出待处理
  python3 manage_reports.py show <rid>          # 看某条详情
  python3 manage_reports.py delete <rid>        # 删掉记录（不改题目可见性）
  python3 manage_reports.py resolve <rid> [--hide]  # 标记已处理；默认恢复题目可见，
                                                    # 加 --hide 表示改完仍要隐藏

题目可见性改的是主库 questions.hidden 和 hidden_questions.json（重建库也会保留）。
"""
import json, sqlite3, sys

MAIN = "/var/lib/notebook/wenchang.db"
REP = "/var/lib/notebook/reports.db"
HID = "/root/workspace/projects/wenchang-data/hidden_questions.json"


def unhide(qid):
    c = sqlite3.connect(MAIN)
    c.execute("UPDATE questions SET hidden = 0 WHERE id = ?", (qid,))
    c.commit()
    c.close()
    ids = set(json.load(open(HID, encoding="utf-8"))) if __import__("os").path.exists(HID) else set()
    ids.discard(qid)
    json.dump(sorted(ids), open(HID, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    r = sqlite3.connect(REP)
    r.row_factory = sqlite3.Row
    if cmd == "list":
        rows = r.execute("SELECT * FROM reports WHERE status='pending' ORDER BY rid").fetchall()
        print(f"待处理 {len(rows)} 条")
        for x in rows:
            print(f"  #{x['rid']} {x['qid']} [{x['subject']}] {x['created_at']} 备注: {x['note'] or '-'}")
            print(f"      题干: {x['q'][:60]}")
            print(f"      答案: {x['a'][:40]}")
        return
    if len(sys.argv) < 3:
        print(__doc__)
        return
    rid = int(sys.argv[2])
    row = r.execute("SELECT * FROM reports WHERE rid = ?", (rid,)).fetchone()
    if not row:
        print("没有这条记录")
        return
    if cmd == "show":
        print(json.dumps(dict(row), ensure_ascii=False, indent=1))
    elif cmd == "delete":
        r.execute("DELETE FROM reports WHERE rid = ?", (rid,))
        r.commit()
        print("已删除记录", rid)
    elif cmd == "resolve":
        r.execute("UPDATE reports SET status='resolved' WHERE rid = ?", (rid,))
        r.commit()
        if "--hide" not in sys.argv:
            unhide(row["qid"])
            print(f"已处理 #{rid}，{row['qid']} 恢复显示")
        else:
            print(f"已处理 #{rid}，{row['qid']} 保持隐藏")
    else:
        print(__doc__)
    r.close()


if __name__ == "__main__":
    main()
