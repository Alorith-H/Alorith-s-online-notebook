# Alorith's Online Notebook

个人语文/英语学习站点的源码与内容数据。

## 结构

```
site/     前端页面（静态，nginx 直接托管）
server/   后端 API（Python 标准库 + SQLite，systemd 常驻）
data/     内容数据（JSON 为源，wenchang.db 为运行时库）
```

## 内容数据说明

- `source.json` —— 资料卡（章节 / 板块 / 条目，含红字考点）
- `questions.json` —— 语文题库（含月考、周测）
- `extra_2000.json` —— 2000题·选择/填空（隐藏章节）
- `eng.json` / `eng_questions.json` —— 英语资料与题库
- `hidden_questions.json` —— 待处理题目清单（审计/纠错隐藏）
- `wenchang.db` —— 由上面几个 JSON 导入生成，仅含公开题目内容

## 本地跑起来

```bash
python3 server/import_wenchang.py      # 重建 data/wenchang.db
python3 server/server.py --port 8789   # 启后端
# site/ 用任意静态服务器托管，API 走 /api/notebook/*
```

## 不含什么

用户账号、密码哈希、登录会话、学习进度同步库（`users.db`）、纠错提交记录（`reports.db`）都不在本仓库，
这些只存在服务器本地。
