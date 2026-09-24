#!/usr/bin/env python3
# Description: 周报工作台后端服务（Python 标准库，零依赖）
# 数据存储: SQLite 文件 weekly.db（持久化到磁盘，非浏览器 localStorage，重启服务不丢）
# 职责: 1) 托管周报工作台 HTML 页面  2) 提供 /api/data 读写接口，多浏览器/多设备共享同一份数据
# Author: workspace-builder
# Date: 2026-09-01  (2026-09-04 重构: kv 单表 JSON -> 规范化多表结构)
#
# 设计要点
# -------
# - 表结构按业务实体拆分（工作项 / 下周计划 / 周报 / OKR 目标 / KR / 全局设置），
#   不再把整份状态塞进单个 JSON 字符串，便于按行查询、更新与统计。
# - ⚠️ 对外 API 契约保持不变：GET /api/data 返回与旧版完全相同的 JSON 形状，
#   PUT /api/data 也继续接收整份状态快照（前端零改动）。
# - 保存采用「事务内全量重写」：客户端每次提交的是完整状态快照，
#   服务端在单个事务里 DELETE + INSERT，保证各表与快照严格一致。
# - 旧版 kv 表里的 'state' JSON 会在首次启动时自动迁移到新表，
#   原 kv 行改名为 'state_legacy_backup_v1' 保留，作为回滚保险。

import json
import sqlite3
import os
import uuid
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ---------------- 路径与端口 ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "weekly.db"))  # SQLite 持久化文件（容器部署可经环境变量 DB_PATH 指向挂载卷）
HTML_PATH = os.path.join(BASE_DIR, "周报工作台.html")      # 被托管的页面（周报工作台，路由 /week）
OKR_PATH = os.path.join(BASE_DIR, "月度OKR.html")          # 被托管的页面（月度 OKR，作为首页 /）
ASSET_DIR = os.path.join(BASE_DIR, "assets")                 # 前端静态资源目录（插画等）
# 路由 -> 页面文件映射：月度 OKR 为首页，周报工作台挂到 /week
PAGES = {"/": OKR_PATH, "/index.html": OKR_PATH, "/okr": OKR_PATH, "/week": HTML_PATH}
PORT = 7878                                             # 服务端口（可用环境变量 PORT 覆盖）
DEFAULT_TITLE = "个人工作台"                              # 工作台名称缺省值（库里没存时回落到此）

# ---------------- 工具函数 ----------------
def _uid():
    """生成唯一 id（避免不同周/月记录碰撞）。"""
    return uuid.uuid4().hex[:10]

def _now():
    """返回当前时间的 ISO 字符串，用于 updated_at / created_at 审计字段。"""
    return datetime.datetime.now().isoformat()

def _monday_of(d):
    """返回 d 所在周的周一（"YYYY-MM-DD"）。

    前端以周一为一周起点（weekRange() 同口径），种子数据必须跟着这个口径走，
    否则首次打开会落在上一周、看板一片空白。

    :param d: datetime.date
    :return: str
    """
    return (d - datetime.timedelta(days=d.weekday())).strftime("%Y-%m-%d")

# ---------------- 首次运行示例数据 ----------------
def seed_payload():
    """首条数据：示例工作项 + 下周计划 + 月度 OKR，方便首次打开就能看到效果。
    :return: 与前端 /api/data 契约一致的整份状态 dict
    """
    today = datetime.date.today()
    mon = today - datetime.timedelta(days=today.weekday())   # 本周周一
    next_mon = mon + datetime.timedelta(days=7)              # 下周周一
    prev_mon = mon - datetime.timedelta(days=7)              # 上周周一
    prev2_mon = mon - datetime.timedelta(days=14)            # 上上周周一
    month = mon.strftime("%Y-%m")                            # 本周所属的月份
    d1 = mon + datetime.timedelta(days=1)
    # 跨月示例的锚点 = today 所在月的「上月末」。以它 ±N 天构造可保证两点：
    #   ① 起止必然分属两个月 → 跨月条（橙）一定成立；
    #   ② done 项的 doneAt 不晚于今天 → 不会出现「未来已完成」的怪数据。
    last_month_end = today.replace(day=1) - datetime.timedelta(days=1)

    def _ds(v):
        """date → 'YYYY-MM-DD'；None/空值统一转空串。"""
        return v.strftime("%Y-%m-%d") if isinstance(v, datetime.date) else (v or "")

    # ⚠️ 示例工作项必须自带 doing_at / done_at：首页「执行月历」画连续条的判定是
    #    `s = doingAt; if(!s || status==='todo') return;`——缺 doingAt 的条目会被整条跳过，
    #    表现为「首次打开月历一片空白，进一趟周报页再回来才有任务」（周报页 applyState 会补齐并回写）。
    #    补齐口径与周报工作台 applyState() 完全一致：done→doneAt=起点=deadline；doing→doingAt=deadline；todo→都空。
    def _item(content, priority, status, day, start=None, end=None):
        """构造一条示例工作项。

        :param day:   截止日期 deadline（看板按此把它归入某一周）
        :param start: 月历连续条起点（开始时间），缺省 = deadline（done/doing）或空（todo）
        :param end:   月历连续条终点（结束时间），缺省 = deadline（done）或空（doing 由前端自动取「今天」、todo 为空）
        """
        d = _ds(day)
        if start is None:
            start = d if status in ("done", "doing") else ""
        if end is None:
            end = d if status == "done" else ""
        return {
            "id": _uid(), "content": content, "project": "示例", "priority": priority, "status": status, "deadline": d,
            "doingAt": _ds(start), "doneAt": _ds(end)
        }

    return {
        "items": [
            # —— 本周：单日条 + 周内连续条 ——
            _item("完成项目首版功能评审", "P0", "done", mon),     # 单日完成条（浅绿）
            _item("整理本周待办并排好优先级", "P1", "done", d1),   # 单日完成条
            _item("修复看板拖拽后状态不同步", "P0", "doing", d1),  # 周内连续条（浅蓝，起点 → 今天）
            _item("补写单元测试，覆盖率提到 80%", "P2", "doing", today),
            # —— 跨周：起止同月、但分属两个自然周（品牌荧光绿）——
            _item("重构数据层并补充回归用例", "P1", "done", prev2_mon,
                  start=prev2_mon, end=prev2_mon + datetime.timedelta(days=9)),
            # —— 跨月：起止分属两个月（橙）——
            _item("迁移旧版配置到新格式", "P0", "done", last_month_end,
                  start=last_month_end - datetime.timedelta(days=2),
                  end=min(last_month_end + datetime.timedelta(days=2), today)),
            # —— 进行中的长条：上周开工至今（跨周还是跨月取决于启动日，两者都能体现长条效果）——
            _item("推进端到端用例覆盖", "P1", "doing", prev_mon),
            # —— 未完成：按规则不上月历，只出现在看板「未完成」列 ——
            _item("输出本月自评草稿", "P1", "todo", today),
            _item("调研同类工具的可借鉴点", "P2", "todo", today),
            _item("整理季度技术复盘素材", "P2", "todo", today)
        ],
        "next": {
            # 键为「下周」的周一；这些计划会自动同步到下一周看板的「未完成」列
            _monday_of(next_mon): [
                {"id": _uid(), "content": "把周报模板固化下来", "project": "示例"},
                {"id": _uid(), "content": "给看板加上批量操作", "project": "示例"}
            ]
        },
        "reports": {},
        "okr": {
            month: [
                {"id": _uid(), "title": "把周报流程跑顺", "krs": [
                    {"id": _uid(), "text": "连续 4 周按时提交周报", "progress": 60},
                    {"id": _uid(), "text": "看板与周报内容一致率 100%", "progress": 30}
                ]},
                {"id": _uid(), "title": "建立可复用的自动化测试体系", "krs": [
                    {"id": _uid(), "text": "核心链路用例覆盖率达到 80%", "progress": 45},
                    {"id": _uid(), "text": "CI 接入冒烟并连续两周绿灯", "progress": 20},
                    {"id": _uid(), "text": "端到端用例覆盖主流程", "progress": 10}
                ]},
                {"id": _uid(), "title": "沉淀个人知识库", "krs": [
                    {"id": _uid(), "text": "完成 12 篇技术笔记", "progress": 75},
                    {"id": _uid(), "text": "整理常用命令速查表", "progress": 40}
                ]}
            ]
        },
        "viewWeek": _monday_of(mon),   # 落在首次启动的当周，打开就是本周
        "viewMonth": month,            # 与 viewWeek 同月，避免月度 OKR 与周报视图错位
        "title": DEFAULT_TITLE         # 工作台名称（存 app_settings.title，前端启动时从库读取显示）
    }

# ---------------- 数据库层（规范化表结构） ----------------
# 业务实体拆分为 6 张表：
#   work_items     工作项（含状态流转日期 doing_at / done_at）
#   next_plans     下周计划（week_monday = 计划所属周的周一）
#   weekly_reports 周报正文（week_monday 主键，存富文本 HTML）
#   objectives     月度 OKR 目标（month = "YYYY-MM"）
#   key_results    KR，外键挂在 objective 上，级联删除
#   app_settings   全局设置（viewWeek / viewMonth 等键值对）
# ⚠️ 历史包袱：items 里可能存在契约外的旧字段（如 srcNext 之外的自定义键），
#    统一落进 extra JSON 列，读出时合并回去，保证不丢数据。
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS work_items (
    id         TEXT PRIMARY KEY,
    content    TEXT NOT NULL,
    project    TEXT DEFAULT '',
    priority   TEXT DEFAULT '',
    status     TEXT DEFAULT 'todo',
    deadline   TEXT DEFAULT '',   -- 截止日期（2026-09-24 由 date 列重命名）
    doing_at   TEXT DEFAULT '',   -- 开始时间（可在编辑弹窗手填；状态流转亦会写入）
    done_at    TEXT DEFAULT '',   -- 结束时间（可在编辑弹窗手填；状态流转亦会写入）
    src_next   TEXT DEFAULT '',
    extra      TEXT DEFAULT '{}',
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS next_plans (
    id          TEXT PRIMARY KEY,
    week_monday TEXT NOT NULL,
    content     TEXT NOT NULL,
    project     TEXT DEFAULT '',
    extra       TEXT DEFAULT '{}',
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_next_plans_week ON next_plans(week_monday);
CREATE TABLE IF NOT EXISTS weekly_reports (
    week_monday TEXT PRIMARY KEY,
    content     TEXT NOT NULL,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS objectives (
    id         TEXT PRIMARY KEY,
    month      TEXT NOT NULL,
    title      TEXT NOT NULL,
    position   INTEGER DEFAULT 0,
    extra      TEXT DEFAULT '{}',
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_objectives_month ON objectives(month);
CREATE TABLE IF NOT EXISTS key_results (
    id           TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL REFERENCES objectives(id) ON DELETE CASCADE,
    text         TEXT NOT NULL,
    progress     INTEGER DEFAULT 0,
    position     INTEGER DEFAULT 0,
    extra        TEXT DEFAULT '{}',
    updated_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_key_results_obj ON key_results(objective_id);
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_conn():
    """获取 SQLite 连接，并确保所有表存在（含外键开启）。

    ⚠️ 会在首次连接前自动创建 DB 所在目录：全新 clone 的仓库里 data/ 是空目录
    （只有 .gitkeep），若 DB_PATH 指向不存在的子目录，sqlite3.connect 会直接抛
    "unable to open database file"，页面整片 500。

    :return: sqlite3.Connection（调用方负责 close）
    """
    d = os.path.dirname(os.path.abspath(DB_PATH))
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")   # ⚠️ SQLite 默认关闭外键，必须逐连接开启
    conn.executescript(SCHEMA_SQL)
    _migrate_date_to_deadline(conn)
    return conn


def _migrate_date_to_deadline(conn):
    """把老库 work_items.date 列改名为 deadline（一次性、幂等）。

    ⚠️ CREATE TABLE IF NOT EXISTS 对已存在的表不生效，老库的 date 列不会自动跟着
    SCHEMA_SQL 改名，必须显式 ALTER TABLE ... RENAME COLUMN，否则写入时会报
    "table work_items has no column named deadline"。判定口径：
      - 同时存在 date 与 deadline → 异常态（理论上不会出现），保留 deadline 并丢弃 date 不可能，
        这里选择不动，交由人工处理；
      - 只有 date → 改名为 deadline；
      - 只有 deadline → 已迁移过，跳过。

    :param conn: sqlite3.Connection
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(work_items)")]
    if "deadline" in cols:
        return                       # 新结构（或已迁移），无需处理
    if "date" in cols:
        # ⚠️ SQLite 3.25+ 才支持 RENAME COLUMN；Python 3.9+ 自带版本均满足
        conn.execute("ALTER TABLE work_items RENAME COLUMN date TO deadline")
        conn.commit()


def _known_item_keys():
    """工作项的已知字段集合；契约外字段进入 extra 列。

    :return: set[str]
    """
    # ⚠️ "date" 已于 2026-09-24 改名 "deadline"（语义=截止日期）；旧数据兼容见 _normalize_item_dates
    return {"id", "content", "project", "priority", "status", "deadline", "doingAt", "doneAt", "srcNext"}


def _normalize_item_dates(it, known, extra):
    """把旧契约字段 date 归一化成 deadline，保证导入老 JSON 备份不丢截止日期。

    ⚠️ 2026-09-24 之前工作项的日期字段叫 date。老备份（data/weekly.db.bak、导出的
    JSON）里仍是 date，若不做兜底，它会因不在 _known_item_keys 里而落进 extra 列、
    deadline 列为空，前端表现为「卡片日期空白 + 月历不画条」。
    优先级：deadline 有值则用它，否则回落到 date。

    :param it: 原始 item dict
    :param known: _split_known 拆出的已知字段子集（原地修改）
    :param extra: _split_known 拆出的契约外字段（原地移除 date）
    """
    if not known.get("deadline"):
        known["deadline"] = it.get("date", "") or ""
    extra.pop("date", None)


def _split_known(d, known):
    """把 dict 拆成 (已知字段子集, 剩余字段) 两个 dict。

    :param d: 原始 dict
    :param known: 已知字段名集合
    :return: (known_part, extra_part)
    """
    known_part = {k: d[k] for k in known if k in d}
    extra_part = {k: d[k] for k in d if k not in known}
    return known_part, extra_part


# ---------------- 旧版 kv 数据迁移 ----------------
def migrate_legacy_if_needed(conn):
    """检测旧版 kv 表的 'state' JSON，迁移到规范化表结构。

    - 仅当新表全空且 kv 里存在 'state' 行时执行（一次性）；
    - 迁移成功后原 kv 行改名为 'state_legacy_backup_v1' 保留，作为回滚保险；
    - 旧版 next 可能是全局数组（无周键），统一挂到「下一周的周一」。

    :param conn: sqlite3.Connection
    """
    try:
        row = conn.execute("SELECT v FROM kv WHERE k='state'").fetchone()
    except sqlite3.OperationalError:
        return  # ⚠️ kv 表已删除（旧结构清理完毕）时直接跳过迁移
    # 新表已有数据则视为已迁移过，跳过
    if conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] > 0:
        return
    try:
        state = json.loads(row[0])
    except Exception:
        return  # ⚠️ 数据损坏时不动原行，避免二次破坏
    _write_snapshot(conn, state)
    conn.execute("UPDATE kv SET k='state_legacy_backup_v1' WHERE k='state'")
    conn.commit()


# ---------------- 写入：整份快照 -> 多表（事务内全量重写） ----------------
def _write_snapshot(conn, state):
    """把一份完整状态快照写入各业务表。

    ⚠️ 调用方需自行 commit；本函数只做写入不做提交，方便与迁移逻辑共用事务。

    :param conn: sqlite3.Connection
    :param state: 与前端契约一致的完整状态 dict
    """
    ts = _now()
    now_dt = datetime.datetime.now()

    # ---- 清空旧数据（快照式同步，保证各表与提交内容严格一致） ----
    # ⚠️ 先删子表 key_results 再删父表 objectives，避免外键约束
    conn.execute("DELETE FROM key_results")
    conn.execute("DELETE FROM objectives")
    conn.execute("DELETE FROM next_plans")
    conn.execute("DELETE FROM weekly_reports")
    conn.execute("DELETE FROM work_items")

    # ---- 工作项 ----
    for it in state.get("items", []):
        known, extra = _split_known(it, _known_item_keys())
        _normalize_item_dates(it, known, extra)   # 旧契约 date -> deadline，须在 INSERT 之前
        conn.execute(
            "INSERT INTO work_items(id, content, project, priority, status, deadline, doing_at, done_at, src_next, extra, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                known.get("id") or _uid(),
                known.get("content", ""),
                known.get("project", ""),
                known.get("priority", ""),
                known.get("status", "todo"),
                known.get("deadline", ""),
                known.get("doingAt", ""),
                known.get("doneAt", ""),
                known.get("srcNext", ""),
                json.dumps(extra, ensure_ascii=False),
                ts,
            ),
        )

    # ---- 下周计划（next: {周一路径: [条目]}；兼容旧版全局数组） ----
    nxt = state.get("next", {})
    if isinstance(nxt, list):
        # 旧版形态：无周键 -> 统一挂到「下一周的周一」（与前端迁移规则一致）
        next_monday = (now_dt + datetime.timedelta(days=(7 - now_dt.weekday()) % 7 or 7)).strftime("%Y-%m-%d")
        nxt = {next_monday: nxt}
    for week, entries in nxt.items():
        for entry in entries:
            known, extra = _split_known(entry, {"id", "content", "project"})
            conn.execute(
                "INSERT INTO next_plans(id, week_monday, content, project, extra, updated_at) VALUES(?,?,?,?,?,?)",
                (
                    known.get("id") or _uid(),
                    week,
                    known.get("content", ""),
                    known.get("project", ""),
                    json.dumps(extra, ensure_ascii=False),
                    ts,
                ),
            )

    # ---- 周报（week_monday -> 富文本 HTML） ----
    for week, content in state.get("reports", {}).items():
        conn.execute(
            "INSERT INTO weekly_reports(week_monday, content, updated_at) VALUES(?,?,?)",
            (week, content, ts),
        )

    # ---- 月度 OKR（objectives + key_results 两级） ----
    for month, objs in state.get("okr", {}).items():
        for oi, obj in enumerate(objs):
            known, extra = _split_known(obj, {"id", "title", "krs"})
            oid = known.get("id") or _uid()
            conn.execute(
                "INSERT INTO objectives(id, month, title, position, extra, updated_at) VALUES(?,?,?,?,?,?)",
                (oid, month, known.get("title", ""), oi, json.dumps(extra, ensure_ascii=False), ts),
            )
            for ki, kr in enumerate(obj.get("krs", [])):
                known_kr, extra_kr = _split_known(kr, {"id", "text", "progress"})
                conn.execute(
                    "INSERT INTO key_results(id, objective_id, text, progress, position, extra, updated_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        known_kr.get("id") or _uid(),
                        oid,
                        known_kr.get("text", ""),
                        int(known_kr.get("progress", 0) or 0),
                        ki,
                        json.dumps(extra_kr, ensure_ascii=False),
                        ts,
                    ),
                )

    # ---- 全局设置（viewWeek / viewMonth / title 工作台名称） ----
    for key in ("viewWeek", "viewMonth", "title"):
        if key in state:
            conn.execute(
                "INSERT INTO app_settings(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(state[key])),
            )

    # ---- 版本号自增（乐观锁）：每次写入都使其他页面的旧快照失效 ----
    conn.execute(
        "INSERT INTO app_settings(key, value) VALUES('__version','1') "
        "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)"
    )


def get_version():
    """读取当前数据版本号（乐观锁基准值）。

    :return: int，库为空时从 0 开始
    """
    conn = get_conn()
    try:
        return _get_version(conn)
    finally:
        conn.close()


def _get_version(conn):
    """在既有连接上读取版本号。"""
    row = conn.execute("SELECT value FROM app_settings WHERE key='__version'").fetchone()
    return int(row[0]) if row else 0


def save_state(state):
    """把整份状态写入 SQLite（事务内全量重写各表）。"""
    conn = get_conn()
    try:
        _write_snapshot(conn, state)
        conn.commit()
    finally:
        conn.close()


# ---------------- 读取：多表 -> 整份快照 ----------------
def _row_to_item(row):
    """work_items 行 -> 前端契约的 item dict（extra 合并回去）。

    :param row: SELECT 结果元组
    :return: dict
    """
    (rid, content, project, priority, status, deadline, doing_at, done_at, src_next, extra) = row
    d = {
        "id": rid, "content": content, "project": project, "priority": priority,
        "status": status, "deadline": deadline, "doingAt": doing_at, "doneAt": done_at, "srcNext": src_next,
    }
    try:
        ex = json.loads(extra or "{}")
        ex.pop("date", None)   # ⚠️ 历史 extra 里可能残留旧字段名 date，读出即丢弃，避免前端 state 长期带脏键
        d.update(ex)
    except Exception:
        pass  # ⚠️ extra 损坏时丢弃扩展字段，不阻断整体读取
    return d


def load_state():
    """从各业务表读取并组装整份状态；库为空则写入示例数据。

    :return: 与前端 /api/data 契约一致的完整状态 dict
    """
    conn = get_conn()
    try:
        migrate_legacy_if_needed(conn)
        empty = (
            conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0
            and conn.execute("SELECT COUNT(*) FROM next_plans").fetchone()[0] == 0
            and conn.execute("SELECT COUNT(*) FROM weekly_reports").fetchone()[0] == 0
            and conn.execute("SELECT COUNT(*) FROM objectives").fetchone()[0] == 0
        )
        if empty:
            data = seed_payload()
            _write_snapshot(conn, data)
            conn.commit()
            return data

        # ---- 工作项 ----
        items = [_row_to_item(r) for r in conn.execute(
            "SELECT id, content, project, priority, status, deadline, doing_at, done_at, src_next, extra "
            "FROM work_items ORDER BY deadline, rowid"
        )]

        # ---- 下周计划：按 week_monday 分组 ----
        nxt = {}
        for week, rid, content, project, extra in conn.execute(
            "SELECT week_monday, id, content, project, extra FROM next_plans ORDER BY week_monday, rowid"
        ):
            d = {"id": rid, "content": content, "project": project}
            try:
                d.update(json.loads(extra or "{}"))
            except Exception:
                pass
            nxt.setdefault(week, []).append(d)

        # ---- 周报 ----
        reports = {week: content for week, content in conn.execute(
            "SELECT week_monday, content FROM weekly_reports"
        )}

        # ---- 月度 OKR：objectives + key_results 两级组装 ----
        okr = {}
        krs_by_obj = {}
        # ⚠️ id 列必须返回给前端：否则 KR 无 id，前端按 id 删除时会误删整个 O 的所有 KR
        for krid, oid, text, progress, extra in conn.execute(
            "SELECT id, objective_id, text, progress, extra FROM key_results ORDER BY position, rowid"
        ):
            d = {"id": krid, "text": text, "progress": progress}
            try:
                kr_extra = json.loads(extra or "{}")
                for k, v in kr_extra.items():
                    d.setdefault(k, v)  # ⚠️ id/text/progress 以列值为准，extra 只补契约外字段
            except Exception:
                pass
            krs_by_obj.setdefault(oid, []).append(d)
        for oid, month, title, extra in conn.execute(
            "SELECT id, month, title, extra FROM objectives ORDER BY month, position, rowid"
        ):
            d = {"id": oid, "title": title, "krs": krs_by_obj.get(oid, [])}
            try:
                obj_extra = json.loads(extra or "{}")
                for k, v in obj_extra.items():
                    d.setdefault(k, v)  # ⚠️ id/title/krs 以列值为准，extra 只补契约外字段
            except Exception:
                pass
            okr.setdefault(month, []).append(d)

        # ---- 全局设置 ----
        settings = {k: v for k, v in conn.execute("SELECT key, value FROM app_settings")}

        return {
            "items": items,
            "next": nxt,
            "reports": reports,
            "okr": okr,
            "title": settings.get("title", DEFAULT_TITLE),   # 工作台名称（缺省回落到当前默认名）
            # ⚠️ 没存过设置时按「今天」推算，别硬编码日期——否则在别的年份打开会落在远古的某一周
            "viewWeek": settings.get("viewWeek", _monday_of(datetime.date.today())),
            "viewMonth": settings.get("viewMonth", datetime.date.today().strftime("%Y-%m")),
            "__v": _get_version(conn),   # 乐观锁版本号：前端提交时必须原样带回
        }
    finally:
        conn.close()


# ---------------- HTTP 处理 ----------------
class Handler(BaseHTTPRequestHandler):
    """多线程请求处理：页面托管 + /api/data 读写。"""

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        """发送响应体（自动编码 UTF-8）。"""
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """GET 路由：页面 / 静态资源 / API 读取。"""
        path = urlparse(self.path).path
        if path in PAGES:
            try:
                with open(PAGES[path], "r", encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "页面文件未找到", "text/plain; charset=utf-8")
        elif path.startswith("/assets/"):
            # 仅托管项目 assets 目录，避免静态资源引用返回 404。
            relative = path[len("/assets/"):]
            asset_path = os.path.abspath(os.path.join(ASSET_DIR, relative))
            if not asset_path.startswith(os.path.abspath(ASSET_DIR) + os.sep):
                self._send(403, "禁止访问", "text/plain; charset=utf-8")
                return
            try:
                with open(asset_path, "rb") as f:
                    body = f.read()
                content_types = {
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".webp": "image/webp",
                    ".svg": "image/svg+xml; charset=utf-8",
                    ".html": "text/html; charset=utf-8",
                }
                suffix = os.path.splitext(asset_path)[1].lower()
                self._send(200, body, content_types.get(suffix, "application/octet-stream"))
            except FileNotFoundError:
                self._send(404, "资源文件未找到", "text/plain; charset=utf-8")
        elif path == "/api/data":
            # 读取持久化状态返回给前端
            try:
                self._send(200, json.dumps(load_state(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False))

    def do_PUT(self):
        """PUT 路由：接收整份状态快照并持久化。"""
        path = urlparse(self.path).path
        if path == "/api/data":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw)
                # ⚠️ 乐观锁：提交快照必须携带 GET 时返回的 __v。
                # 版本不一致 = 该页面加载后数据已被其他页面更新过，拒绝写入，
                # 防止持有脏/过期状态的页面把服务端数据整份覆盖（本日两次数据被清的根因）。
                incoming = data.pop("__v", None)
                # ⚠️ 原子校验（2026-09-04 事故根因修复）：版本读取与快照写入必须在同一
                #    BEGIN IMMEDIATE 事务内。原先 get_version()/save_state() 各开连接，
                #    「读版本→（并发直写 DB 插队提交）→写快照」的窗口会让过期快照通过校验，
                #    把整份旧状态盖回去并接管版本号。
                conn = get_conn()
                try:
                    conn.isolation_level = None   # 关闭隐式事务，改用手动 BEGIN IMMEDIATE
                    conn.execute("BEGIN IMMEDIATE")
                    current = _get_version(conn)
                    if incoming is None or int(incoming) != current:
                        conn.rollback()
                        self._send(409, json.dumps(
                            {"error": "数据已被其他页面更新，请刷新页面后再操作", "current": current},
                            ensure_ascii=False))
                        return
                    _write_snapshot(conn, data)
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    raise
                finally:
                    conn.close()
                self._send(200, json.dumps({"ok": True, "__v": current + 1}, ensure_ascii=False))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False))

    def log_message(self, *args):
        # 静默日志，避免刷屏
        pass


def main():
    """服务入口：启动多线程 HTTP 服务并常驻。"""
    port = int(os.environ.get("PORT", PORT))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"周报工作台服务已启动: http://localhost:{port}  (数据持久化于 {DB_PATH})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
