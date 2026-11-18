#!/usr/bin/env python3
"""隐藏 / 显示题目。

用法：
  python3 toggle_hidden.py list                # 查看当前隐藏了哪些
  python3 toggle_hidden.py add s08-06-002 q2-1136
  python3 toggle_hidden.py del s08-06-002
  python3 toggle_hidden.py clear               # 全部取消隐藏
  python3 toggle_hidden.py fromfile ids.txt    # 每行一个题号

改完自动重建库并重启 API。
"""
import json, subprocess, sys, os

HID = '/root/workspace/projects/wenchang-data/hidden_questions.json'
PY = '/root/workspace/projects/notebook/venv/bin/python'
IMPORT = '/root/workspace/projects/notebook/import_wenchang.py'


def load():
    if os.path.exists(HID):
        return set(json.load(open(HID, encoding='utf-8')))
    return set()


def save(s):
    json.dump(sorted(s), open(HID, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


def rebuild():
    subprocess.run([PY, IMPORT], check=True, capture_output=True)
    subprocess.run(['systemctl', 'restart', 'notebook-api'], check=True)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    cur = load()
    if cmd == 'list':
        print(f'当前隐藏 {len(cur)} 题:')
        for i in sorted(cur):
            print(' ', i)
        return
    if cmd == 'clear':
        save(set())
    elif cmd == 'add':
        cur |= set(sys.argv[2:])
        save(cur)
    elif cmd == 'del':
        cur -= set(sys.argv[2:])
        save(cur)
    elif cmd == 'fromfile':
        ids = [l.strip() for l in open(sys.argv[2], encoding='utf-8') if l.strip()]
        cur |= set(ids)
        save(cur)
    else:
        print(__doc__)
        return
    rebuild()
    print(f'完成，当前隐藏 {len(cur)} 题')


if __name__ == '__main__':
    main()
