"""
备案价查询 — Flask 后端
公众号菜单入口 → 微信 OAuth → 查询页面 → API 数据
"""
import sqlite3
import os
import re
from flask import Flask, request, jsonify, g
from datetime import datetime
from pypinyin import pinyin, Style

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "beian-dev-secret-change-in-production")

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "property.db"))

# ── 微信小程序配置（部署时改） ──
WECHAT_APPID = os.environ.get("WECHAT_APPID", "")
WECHAT_SECRET = os.environ.get("WECHAT_SECRET", "")


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def ensure_indexes():
    """启动时确保索引存在（不阻塞请求）"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_units_project_bldg "
        "ON housing_units(project_name, building_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_units_project_bldg_status "
        "ON housing_units(project_name, building_name, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_units_date_signed "
        "ON housing_units(date_signed)"
    )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════
# 页面
# ═══════════════════════════════════════════

@app.route("/beian/")
def index():
    """API 服务状态"""
    return jsonify({"status": "ok", "service": "备案价查询 API"})


@app.route("/api/wx-login", methods=["POST"])
def wx_login():
    """小程序 wx.login 换取 openid"""
    code = request.json.get("code", "")
    if not code:
        return jsonify({"error": "missing code"}), 400

    import requests
    url = (
        f"https://api.weixin.qq.com/sns/jscode2session"
        f"?appid={WECHAT_APPID}&secret={WECHAT_SECRET}&js_code={code}"
        f"&grant_type=authorization_code"
    )
    try:
        resp = requests.get(url, timeout=10).json()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    openid = resp.get("openid")
    if openid:
        return jsonify({"openid": openid})
    return jsonify({"error": resp.get("errmsg", "unknown")}), 400


# ═══════════════════════════════════════════
# API
# ═══════════════════════════════════════════

@app.route("/api/zones")
def api_zones():
    """区域列表"""
    db = get_db()
    rows = db.execute(
        "SELECT DISTINCT zone FROM housing_units WHERE zone != '' AND house_usage='住宅' AND status='未售' ORDER BY zone"
    ).fetchall()
    zones = [r["zone"] for r in rows]
    zones.sort(key=pinyin_sort_key)
    return jsonify({"zones": zones})


@app.route("/api/projects")
def api_projects():
    """小区列表，可按区域筛选（仅2019年后开盘 + 有可售房源）"""
    zone = request.args.get("zone", "")
    db = get_db()
    if zone:
        rows = db.execute(
            "SELECT DISTINCT project_name FROM housing_units "
            "WHERE zone=? AND project_name IS NOT NULL AND project_name != '' "
            "AND project_name IN (SELECT DISTINCT project_name FROM housing_units WHERE status='未售' AND house_usage='住宅') "
            "AND project_name IN (SELECT DISTINCT project_name FROM housing_units WHERE date_listed >= '2020-01-01') "
            "ORDER BY project_name",
            [zone],
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT DISTINCT project_name FROM housing_units "
            "WHERE project_name IS NOT NULL AND project_name != '' "
            "AND project_name IN (SELECT DISTINCT project_name FROM housing_units WHERE status='未售' AND house_usage='住宅') "
            "AND project_name IN (SELECT DISTINCT project_name FROM housing_units WHERE date_listed >= '2020-01-01') "
            "ORDER BY project_name"
        ).fetchall()
    projects = [r["project_name"].strip() for r in rows if r["project_name"].strip()]
    projects.sort(key=pinyin_sort_key)
    return jsonify({"projects": projects})


def pinyin_sort_key(s):
    """拼音排序：宝安→B, 福田→F"""
    py = pinyin(s, style=Style.TONE3)
    return ''.join([p[0] for p in py]).lower()


def natural_sort_key(s):
    """自然排序：提取数字部分做数值排序，如 1栋<2栋<10栋"""
    import re as _re
    parts = _re.split(r'(\d+)', s)
    return [int(p) if p.isdigit() else p.lower() for p in parts]

@app.route("/api/buildings")
def api_buildings():
    """某小区的楼栋列表（仅返回有住宅可售房源的楼栋）"""
    project = request.args.get("project", "")
    if not project:
        return jsonify({"buildings": []})
    db = get_db()
    rows = db.execute(
        "SELECT DISTINCT building_name FROM housing_units "
        "WHERE project_name=? AND status='未售' AND house_usage='住宅' "
        "ORDER BY building_name",
        [project],
    ).fetchall()
    buildings = [r["building_name"] for r in rows]
    buildings.sort(key=natural_sort_key)
    return jsonify({"buildings": buildings})


@app.route("/api/units")
def api_units():
    """房源列表（仅住宅在售，楼栋可选）"""
    project = request.args.get("project", "")
    building = request.args.get("building", "")
    price_min = request.args.get("price_min", 0, type=float)
    price_max = request.args.get("price_max", 999999999, type=float)
    area_min = request.args.get("area_min", 0, type=float)
    area_max = request.args.get("area_max", 999999, type=float)
    search = request.args.get("search", "")

    db = get_db()
    conditions = ["project_name=?", "house_usage='住宅'", "status='未售'",
                  "total_price BETWEEN ? AND ?", "built_area BETWEEN ? AND ?"]
    params = [project, price_min, price_max, area_min, area_max]

    if building:
        conditions.append("building_name=?")
        params.append(building)

    if search:
        conditions.append("unit_no LIKE ?")
        params.append(f"%{search}%")

    where = " AND ".join(conditions)
    sql = (
        f"SELECT unit_no, built_area, unit_price, total_price, "
        f"house_usage, status, check_date, sale_type, building_name "
        f"FROM housing_units WHERE {where}"
    )
    rows = db.execute(sql, params).fetchall()

    units = []
    for r in rows:
        total_w = r["total_price"]
        if total_w and total_w > 0:
            total_wan = round(total_w / 10000, 1)
        else:
            total_wan = 0

        # 从房号提取楼层
        # 格式: "3001" → 30, "二单元906" → 9, "B座1205" → 12
        unit_no = r["unit_no"]
        floor = 0
        try:
            # 匹配末尾的数字部分（至少2位）
            m = re.search(r'(\d{2,4})$', unit_no)
            if m:
                digits = m.group(1)
                # 如果3-4位数字，前1-2位是楼层
                if len(digits) >= 3:
                    floor = int(digits[:-2])
                elif len(digits) == 2:
                    floor = int(digits)
        except (ValueError, IndexError, TypeError):
            floor = 0

        units.append({
            "unit_no": unit_no,
            "built_area": r["built_area"],
            "unit_price": r["unit_price"],
            "total_price": total_wan,
            "house_usage": r["house_usage"],
            "status": r["status"],
            "check_date": r["check_date"] or "",
            "sale_type": r["sale_type"],
            "floor": floor,
            "building_name": r["building_name"],
        })

    # 排序：楼栋自然序低→高，同楼栋内楼层高→低，房号小→大
    def unit_sort_key(u):
        bldg = natural_sort_key(u.get('building_name', '') or '')
        floor = -(u.get('floor', 0) or 0)  # 负值实现 DESC
        unit = natural_sort_key(u.get('unit_no', '') or '')
        return (bldg, floor, unit)
    units.sort(key=unit_sort_key)

    return jsonify({"units": units})


@app.route("/api/stats")
def api_stats():
    """统计（区域/小区/楼栋 + 价格面积筛选，住宅在售）"""
    zone = request.args.get("zone", "")
    project = request.args.get("project", "")
    building = request.args.get("building", "")
    price_min = request.args.get("price_min", 0, type=float)
    price_max = request.args.get("price_max", 999999999, type=float)
    area_min = request.args.get("area_min", 0, type=float)
    area_max = request.args.get("area_max", 9999, type=float)
    
    db = get_db()
    
    # 构建条件：仅当用户主动设置筛选时才加价格/面积条件
    conds = ["house_usage='住宅'"]
    params = []
    has_filter = (price_min > 0 or price_max < 999999999 or area_min > 0 or area_max < 9999)
    if has_filter:
        conds.append("total_price BETWEEN ? AND ?")
        conds.append("built_area BETWEEN ? AND ?")
        params.extend([price_min, price_max, area_min, area_max])
    where_extra = " AND " + " AND ".join(conds) if conds else ""
    
    if zone and not project:
        total = db.execute(
            f"SELECT COUNT(*) as cnt FROM housing_units WHERE zone=?{where_extra}",
            [zone] + params,
        ).fetchone()["cnt"]
        sold = db.execute(
            f"SELECT COUNT(*) as cnt FROM housing_units WHERE zone=? AND status!='未售'{where_extra}",
            [zone] + params,
        ).fetchone()["cnt"]
        unsold = total - sold
        return jsonify({
            "total": total, "sold": sold, "unsold": unsold,
            "project_total": total,
            "sold_pct": round(sold / total * 100, 1) if total else 0,
            "unsold_pct": round(unsold / total * 100, 1) if total else 0,
        })
    
    if not project:
        return jsonify({"total": 0, "sold": 0, "unsold": 0})

    if building:
        total = db.execute(
            f"SELECT COUNT(*) as cnt FROM housing_units WHERE project_name=? AND building_name=?{where_extra}",
            [project, building] + params,
        ).fetchone()["cnt"]
        sold = db.execute(
            f"SELECT COUNT(*) as cnt FROM housing_units WHERE project_name=? AND building_name=? AND status!='未售'{where_extra}",
            [project, building] + params,
        ).fetchone()["cnt"]
    else:
        total = db.execute(
            f"SELECT COUNT(*) as cnt FROM housing_units WHERE project_name=?{where_extra}",
            [project] + params,
        ).fetchone()["cnt"]
        sold = db.execute(
            f"SELECT COUNT(*) as cnt FROM housing_units WHERE project_name=? AND status!='未售'{where_extra}",
            [project] + params,
        ).fetchone()["cnt"]

    unsold = total - sold
    project_total = total if not building else db.execute(
        "SELECT COUNT(*) as cnt FROM housing_units WHERE project_name=?",
        [project],
    ).fetchone()["cnt"]

    return jsonify({
        "total": total,
        "sold": sold,
        "unsold": unsold,
        "project_total": project_total,
        "sold_pct": round(sold / total * 100, 1) if total else 0,
        "unsold_pct": round(unsold / total * 100, 1) if total else 0,
    })


# ═══════════════════════════════════════════
# 首页 API
# ═══════════════════════════════════════════

@app.route("/api/overview")
def api_overview():
    """首页总览：市场概况数据（单次聚合查询）"""
    db = get_db()

    row = db.execute(
        "SELECT "
        "COUNT(*) as total, "
        "SUM(CASE WHEN status='未售' THEN 1 ELSE 0 END) as unsold, "
        "SUM(CASE WHEN status='已网签' THEN 1 ELSE 0 END) as signed, "
        "SUM(CASE WHEN status='已备案' THEN 1 ELSE 0 END) as filed, "
        "SUM(CASE WHEN status='已转移登记' THEN 1 ELSE 0 END) as transferred, "
        "ROUND(AVG(CASE WHEN status='未售' AND total_price>0 THEN total_price END)/10000, 1) as avg_total, "
        "ROUND(AVG(CASE WHEN status='未售' AND total_price>0 THEN unit_price END), 0) as avg_unit, "
        "SUM(CASE WHEN house_usage='住宅' AND check_date >= date('now', 'localtime', '-7 days') THEN 1 ELSE 0 END) as recent "
        "FROM housing_units WHERE house_usage='住宅'"
    ).fetchone()

    # 各区未售住宅统计（全部区域）
    zone_rows = db.execute(
        "SELECT zone, COUNT(*) as cnt, "
        "ROUND(AVG(total_price)/10000, 1) as avg_t, "
        "ROUND(AVG(unit_price), 0) as avg_u "
        "FROM housing_units WHERE house_usage='住宅' AND status='未售' AND zone != '' "
        "GROUP BY zone ORDER BY cnt DESC"
    ).fetchall()
    zones = [{
        "name": r["zone"], "count": r["cnt"],
        "avg_total": r["avg_t"], "avg_unit": r["avg_u"]
    } for r in zone_rows]

    return jsonify({
        "total": row["total"],
        "unsold": row["unsold"],
        "signed": row["signed"],
        "filed": row["filed"],
        "transferred": row["transferred"],
        "avg_total": row["avg_total"] or 0,
        "avg_unit": row["avg_unit"] or 0,
        "recent": row["recent"],
        "zones": zones,
    })


@app.route("/api/rankings")
def api_rankings():
    """榜单：低价 / 低单价 / 小面积"""
    db = get_db()
    base = "WHERE house_usage='住宅' AND status='未售' AND total_price > 0 AND built_area > 0"

    # 总价最低 TOP 10
    cheap = db.execute(
        f"SELECT project_name, building_name, unit_no, built_area, unit_price, "
        f"total_price, zone FROM housing_units {base} "
        f"ORDER BY total_price ASC LIMIT 10"
    ).fetchall()
    # 单价最低 TOP 10
    cheap_unit = db.execute(
        f"SELECT project_name, building_name, unit_no, built_area, unit_price, "
        f"total_price, zone FROM housing_units {base} "
        f"ORDER BY unit_price ASC LIMIT 10"
    ).fetchall()
    # 面积最小 TOP 10
    small = db.execute(
        f"SELECT project_name, building_name, unit_no, built_area, unit_price, "
        f"total_price, zone FROM housing_units {base} "
        f"ORDER BY built_area ASC LIMIT 10"
    ).fetchall()

    def fmt(row):
        return {
            "project_name": row["project_name"] or "",
            "building_name": row["building_name"] or "",
            "unit_no": row["unit_no"] or "",
            "built_area": row["built_area"] or 0,
            "unit_price": row["unit_price"] or 0,
            "total_price": round(row["total_price"] / 10000, 1),
            "zone": row["zone"] or "",
        }

    return jsonify({
        "cheap_total": [fmt(r) for r in cheap],
        "cheap_unit": [fmt(r) for r in cheap_unit],
        "small_area": [fmt(r) for r in small],
    })


@app.route("/api/latest-permits")
def api_latest_permits():
    """最新预售证（最近30条）"""
    db = get_db()
    rows = db.execute(
        "SELECT p.permit_no, p.project_name, p.developer, p.zone, p.pass_date, "
        "COUNT(h.id) as unsold "
        "FROM presale_permits p "
        "LEFT JOIN housing_units h ON h.project_name = p.project_name "
        "AND h.house_usage='住宅' AND h.status='未售' "
        "GROUP BY p.permit_no "
        "ORDER BY p.pass_date DESC LIMIT 30"
    ).fetchall()

    permits = [{
        "permit_no": r["permit_no"] or "",
        "project_name": r["project_name"] or "",
        "developer": r["developer"] or "",
        "zone": r["zone"] or "",
        "pass_date": r["pass_date"] or "",
        "unsold": r["unsold"],
    } for r in rows]

    return jsonify({"permits": permits})


# ═══════════════════════════════════════════
# 成交分析 API
# ═══════════════════════════════════════════

@app.route("/api/transactions/summary")
def api_transactions_summary():
    """本月成交概览：成交量、均价、环比变化"""
    db = get_db()
    base = "WHERE status IN ('已网签','已备案','已转移登记') AND total_price > 0"
    sql = (
        "SELECT strftime('%Y-%m', date_signed) as month, COUNT(*) as cnt, "
        "ROUND(AVG(total_price)/10000, 1) as avg_t, "
        "ROUND(AVG(unit_price), 0) as avg_u "
        f"FROM housing_units {base} AND date_signed IS NOT NULL "
        "GROUP BY month ORDER BY month DESC LIMIT 2"
    )
    rows = db.execute(sql).fetchall()
    this_month = rows[0] if len(rows) > 0 else None
    last_month = rows[1] if len(rows) > 1 else None

    def fmt(r):
        return {"month": r["month"], "count": r["cnt"], "avg_total": r["avg_t"], "avg_unit": r["avg_u"]} if r else None

    tm = fmt(this_month)
    lm = fmt(last_month)
    delta_count = round((tm["count"] - lm["count"]) / lm["count"] * 100, 1) if tm and lm and lm["count"] else 0
    delta_price = round((tm["avg_total"] - lm["avg_total"]) / lm["avg_total"] * 100, 1) if tm and lm and lm["avg_total"] else 0

    return jsonify({
        "this_month": tm, "last_month": lm,
        "delta_count_pct": delta_count, "delta_price_pct": delta_price
    })


@app.route("/api/transactions/trends")
def api_transactions_trends():
    """近 N 个月成交量价走势"""
    months = request.args.get("months", 12, type=int)
    db = get_db()
    base = "WHERE status != '未售' AND total_price > 0 AND date_signed IS NOT NULL"
    rows = db.execute(
        f"SELECT strftime('%Y-%m', date_signed) as month, COUNT(*) as cnt, "
        f"ROUND(AVG(total_price)/10000, 1) as avg_total "
        f"FROM housing_units {base} "
        f"GROUP BY month ORDER BY month DESC LIMIT ?", [months]
    ).fetchall()
    trends = [{"month": r["month"], "count": r["cnt"], "avg_total": r["avg_total"]} for r in rows]
    trends.reverse()
    return jsonify({"trends": trends})


@app.route("/api/transactions/recent")
def api_transactions_recent():
    """近期成交列表（cursor 分页）"""
    cursor = request.args.get("cursor", "")
    page_size = request.args.get("page_size", 20, type=int)
    zone = request.args.get("zone", "")
    db = get_db()

    conds = ["status != '未售'", "total_price > 0", "date_signed IS NOT NULL"]
    params = []
    if cursor:
        conds.append("date_signed < ?")
        params.append(cursor)
    if zone:
        conds.append("zone = ?")
        params.append(zone)
    where = " AND ".join(conds)

    rows = db.execute(
        f"SELECT project_name, building_name, unit_no, built_area, "
        f"unit_price, total_price, date_signed, zone "
        f"FROM housing_units WHERE {where} "
        f"ORDER BY date_signed DESC LIMIT ?",
        params + [page_size + 1]
    ).fetchall()

    has_more = len(rows) > page_size
    items = [{
        "project_name": r["project_name"] or "",
        "building_name": r["building_name"] or "",
        "unit_no": r["unit_no"],
        "built_area": r["built_area"],
        "unit_price": r["unit_price"],
        "total_price": round(r["total_price"] / 10000, 1),
        "date_signed": r["date_signed"],
        "zone": r["zone"] or "",
    } for r in rows[:page_size]]

    next_cursor = items[-1]["date_signed"] if has_more and items else ""
    return jsonify({"items": items, "has_more": has_more, "next_cursor": next_cursor})


# ═══════════════════════════════════════════
# 运营后台 API
# ═══════════════════════════════════════════

@app.route("/api/admin/status")
def api_admin_status():
    """运营面板：同步状态、数据统计、系统健康"""
    db = get_db()
    import os, time as _time

    # 总量
    total = db.execute("SELECT COUNT(*) as cnt FROM housing_units").fetchone()["cnt"]

    # 状态分布
    status_rows = db.execute(
        "SELECT status, COUNT(*) as cnt FROM housing_units "
        "GROUP BY status ORDER BY cnt DESC"
    ).fetchall()
    statuses = {r["status"]: r["cnt"] for r in status_rows}

    # 用途分布（TOP 5）
    usage_rows = db.execute(
        "SELECT house_usage, COUNT(*) as cnt FROM housing_units "
        "GROUP BY house_usage ORDER BY cnt DESC LIMIT 5"
    ).fetchall()

    # 最近同步（用 created_at 替代 sync_batch，生产表无此列）
    last_sync = db.execute(
        "SELECT MAX(created_at) as batch FROM housing_units"
    ).fetchone()["batch"] or "-"

    # 最近更新日期
    last_check = db.execute(
        "SELECT MAX(check_date) as dt FROM housing_units"
    ).fetchone()["dt"] or "-"

    # 预售证数量
    permits = db.execute("SELECT COUNT(*) as cnt FROM presale_permits").fetchone()["cnt"]

    # DB 文件大小
    _db_path = DB_PATH if os.path.exists(DB_PATH) else ""
    db_size_mb = round(os.path.getsize(_db_path) / 1048576, 1) if _db_path else 0

    # 各区可售住宅
    zone_rows = db.execute(
        "SELECT zone, COUNT(*) as cnt FROM housing_units "
        "WHERE house_usage='住宅' AND status='未售' AND zone != '' "
        "GROUP BY zone ORDER BY cnt DESC"
    ).fetchall()

    return jsonify({
        "total": total,
        "statuses": statuses,
        "top_usage": [{"usage": r["house_usage"], "count": r["cnt"]} for r in usage_rows],
        "last_sync": last_sync,
        "last_check": last_check,
        "permits": permits,
        "db_size_mb": db_size_mb,
        "zones": [{"name": r["zone"], "unsold": r["cnt"]} for r in zone_rows],
    })


if __name__ == "__main__":
    print("🏠 备案价查询 API 服务启动")
    print(f"   本地访问: http://localhost:5001/")
    ensure_indexes()
    app.run(host="0.0.0.0", port=5001, debug=True)
