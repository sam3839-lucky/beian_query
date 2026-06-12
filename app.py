"""
备案价查询 — Flask 后端 (PostgreSQL)
公众号菜单入口 → 微信 OAuth → 查询页面 → API 数据
"""
import calendar
import io
import os
import re
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, g, send_file
from datetime import datetime
from pypinyin import pinyin, Style
import poster

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "beian-dev-secret-change-in-production")

DB_CONFIG = {
    "dbname": os.environ.get("DB_NAME", "property_clawer"),
    "user": os.environ.get("DB_USER", "property_clawer"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "host": os.environ.get("DB_HOST", "localhost"),
}

def zone_name(name):
    """标准化区域名（深汕合作→深汕）"""
    return "深汕" if name == "深汕合作" else name

# 未售房源新鲜度阈值：check_date 超过 5 年的未售记录视为僵尸数据自动排除
UNSOLD_STALE_DAYS = 1825  # 5 年
UNSOLD_RECENCY = f"check_date >= (CURRENT_DATE - INTERVAL '{UNSOLD_STALE_DAYS} days')::text"
UNSOLD_STATUSES = "('未售','期房待售','在建抵押','首次登记')"

# ── 微信小程序配置（部署时改） ──
WECHAT_APPID = os.environ.get("WECHAT_APPID", "")
WECHAT_SECRET = os.environ.get("WECHAT_SECRET", "")


class PGCursor:
    """psycopg2 adapter — mimics sqlite3 connection interface for minimal code change"""
    def __init__(self, conn):
        self.conn = conn
        self.cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    def execute(self, sql, params=None):
        self.cur.execute(sql, params or ())
        return self
    def fetchall(self): return self.cur.fetchall()
    def fetchone(self): return self.cur.fetchone()
    def commit(self): self.conn.commit()
    def close(self):
        self.cur.close()
        self.conn.close()


def get_db():
    if 'db' not in g:
        conn = psycopg2.connect(**DB_CONFIG)
        g.db = PGCursor(conn)
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def ensure_indexes():
    """PostgreSQL 索引已在建表时创建，跳过"""


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

@app.route("/api/quick-search")
def api_quick_search():
    """瞬搜：输入关键词，返回匹配项目摘要（可售套数、均价、价格区间）
    支持：中文关键词、拼音首字母（如 wlpm → 未来平方云山府）"""
    q = request.args.get("q", "").strip()
    if not q or len(q) < 1:
        return jsonify({"results": []})

    db = get_db()
    like_q = f"%{q}%"
    match_names = None  # 拼音匹配的项目名列表

    # 判断是否为拼音首字母输入（纯 ASCII 字母，>=2 位）
    if q.isascii() and q.isalpha() and len(q) >= 2:
        q_lower = q.lower()
        # 取所有住宅项目名，Python 层做拼音首字母匹配
        all_names = db.execute(
            f"SELECT DISTINCT project_name FROM housing_units "
            f"WHERE house_usage='住宅' AND status IN ('期房待售','在建抵押','未售','首次登记') AND project_name != '' AND {UNSOLD_RECENCY}"
        ).fetchall()
        matched = []
        for row in all_names:
            name = row["project_name"]
            py = "".join([p[0][0] for p in pinyin(name, style=Style.FIRST_LETTER) if p[0]]).lower()
            if q_lower in py:
                matched.append(name)
        if matched:
            match_names = matched

    # 构建 SQL：优先用拼音匹配的项目名（精准），否则走 ILIKE
    if match_names:
        placeholders = ", ".join(["%s"] * len(match_names))
        sql = (
            "SELECT h.project_name, h.zone, "
            "(SELECT p.developer FROM presale_permits p WHERE p.project_name = h.project_name LIMIT 1) as developer, "
            "COUNT(*) as unsold_count, "
            "ROUND((AVG(h.total_price)/10000)::numeric, 1) as avg_total, "
            "ROUND(AVG(h.unit_price)::numeric, 0) as avg_unit, "
            "ROUND((MIN(h.total_price)/10000)::numeric, 1) as price_min, "
            "ROUND((MAX(h.total_price)/10000)::numeric, 1) as price_max "
            "FROM housing_units h "
            "WHERE h.house_usage='住宅' AND h.status IN ('期房待售','在建抵押','未售','首次登记') "
            f"AND {UNSOLD_RECENCY} "
            f"AND h.project_name IN ({placeholders}) "
            "GROUP BY h.project_name, h.zone "
            "ORDER BY unsold_count DESC LIMIT 10"
        )
        params = match_names
        is_pinyin = True
    else:
        sql = (
            "SELECT h.project_name, h.zone, "
            "(SELECT p.developer FROM presale_permits p WHERE p.project_name = h.project_name LIMIT 1) as developer, "
            "COUNT(*) as unsold_count, "
            "ROUND((AVG(h.total_price)/10000)::numeric, 1) as avg_total, "
            "ROUND(AVG(h.unit_price)::numeric, 0) as avg_unit, "
            "ROUND((MIN(h.total_price)/10000)::numeric, 1) as price_min, "
            "ROUND((MAX(h.total_price)/10000)::numeric, 1) as price_max "
            "FROM housing_units h "
            "WHERE h.house_usage='住宅' AND h.status IN ('期房待售','在建抵押','未售','首次登记') "
            f"AND {UNSOLD_RECENCY} "
            "AND (h.project_name ILIKE %s OR h.zone ILIKE %s) "
            "GROUP BY h.project_name, h.zone "
            "ORDER BY unsold_count DESC LIMIT 10"
        )
        params = [like_q, like_q]
        is_pinyin = False

    rows = db.execute(sql, params).fetchall()

    results = []
    for r in rows:
        name = r["project_name"] or ""
        q_lower = q.lower()
        if is_pinyin:
            match_type = "pinyin"
        elif name == q or name.lower() == q_lower:
            match_type = "exact"
        elif q_lower in name.lower():
            match_type = "contains"
        else:
            match_type = "zone"

        results.append({
            "project_name": name,
            "zone": zone_name(r["zone"] or ""),
            "unsold_count": r["unsold_count"],
            "avg_unit": float(r["avg_unit"] or 0),
            "avg_total": float(r["avg_total"] or 0),
            "price_min": float(r["price_min"] or 0),
            "price_max": float(r["price_max"] or 0),
            "developer": r["developer"] or "",
            "match_type": match_type,
        })

    return jsonify({"results": results})


@app.route("/api/resolve-scene")
def api_resolve_scene():
    """解码小程序码 scene 参数（base64url → 项目名）"""
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"error": "missing code"}), 400
    try:
        import base64 as _b64
        # base64url → 标准 base64
        b64 = code.replace("-", "+").replace("_", "/")
        b64 += "=" * (4 - len(b64) % 4) if len(b64) % 4 else ""
        name = _b64.urlsafe_b64decode(b64.encode()).decode("utf-8")
        return jsonify({"project_name": name})
    except Exception:
        return jsonify({"error": "invalid code"}), 400


# ═══════════════════════════════════════════
# 关注订阅 API
# ═══════════════════════════════════════════

@app.route("/api/subscribe", methods=["POST"])
def api_subscribe():
    """关注项目"""
    data = request.get_json() or {}
    openid = data.get("openid", "").strip()
    project = data.get("project", "").strip()
    if not openid or not project:
        return jsonify({"error": "missing openid or project"}), 400

    db = get_db()
    try:
        db.execute(
            "INSERT INTO subscriptions (openid, project_name) VALUES (%s, %s) "
            "ON CONFLICT (openid, project_name) DO NOTHING",
            [openid, project]
        )
        db.commit()
        # 返回当前关注数
        count = db.execute(
            "SELECT COUNT(*) as cnt FROM subscriptions WHERE openid = %s",
            [openid]
        ).fetchone()["cnt"]
        return jsonify({"subscribed": True, "count": count, "max": 5})
    except Exception:
        return jsonify({"error": "subscribe failed"}), 500


@app.route("/api/unsubscribe", methods=["POST"])
def api_unsubscribe():
    """取消关注"""
    data = request.get_json() or {}
    openid = data.get("openid", "").strip()
    project = data.get("project", "").strip()
    if not openid or not project:
        return jsonify({"error": "missing openid or project"}), 400

    db = get_db()
    db.execute(
        "DELETE FROM subscriptions WHERE openid = %s AND project_name = %s",
        [openid, project]
    )
    db.commit()
    count = db.execute(
        "SELECT COUNT(*) as cnt FROM subscriptions WHERE openid = %s",
        [openid]
    ).fetchone()["cnt"]
    return jsonify({"subscribed": False, "count": count})


@app.route("/api/my-subscriptions")
def api_my_subscriptions():
    """获取我的关注列表（含项目摘要）"""
    openid = request.args.get("openid", "").strip()
    if not openid:
        return jsonify({"subscriptions": []})

    db = get_db()
    rows = db.execute(
        "SELECT s.project_name, s.subscribed_at, "
        "COUNT(h.id) as unsold_count, "
        "ROUND((AVG(h.total_price)/10000)::numeric, 1) as avg_total, "
        "MAX(h.zone) as zone "
        "FROM subscriptions s "
        "LEFT JOIN housing_units h ON h.project_name = s.project_name "
        f"AND h.house_usage='住宅' AND h.status IN ('期房待售','在建抵押','未售','首次登记') AND {UNSOLD_RECENCY} "
        "WHERE s.openid = %s "
        "GROUP BY s.project_name, s.subscribed_at "
        "ORDER BY s.subscribed_at DESC",
        [openid]
    ).fetchall()

    subs = [{
        "project_name": r["project_name"],
        "zone": zone_name(r["zone"] or ""),
        "unsold_count": r["unsold_count"],
        "avg_total": float(r["avg_total"] or 0),
        "subscribed_at": str(r["subscribed_at"]) if r["subscribed_at"] else "",
    } for r in rows]

    return jsonify({"subscriptions": subs})


# ═══════════════════════════════════════════
# 会员体系 API
# ═══════════════════════════════════════════

TIER_LIMITS = {
    "free": {"searches": 20, "posters": 3, "follows": 3, "trends_months": 3},
    "pro":  {"searches": 9999, "posters": 9999, "follows": 10, "trends_months": 12},
    "team": {"searches": 9999, "posters": 9999, "follows": 30, "trends_months": 999},
}


def _ensure_user(openid):
    """确保 users 表存在该用户记录，返回当前记录"""
    db = get_db()
    db.execute(
        "INSERT INTO users (openid) VALUES (%s) ON CONFLICT (openid) DO NOTHING",
        [openid]
    )
    db.commit()


def _reset_daily_counters(openid):
    """跨天自动重置每日计数器"""
    db = get_db()
    today = datetime.now().date()
    row = db.execute(
        "SELECT search_date, poster_date FROM users WHERE openid = %s", [openid]
    ).fetchone()
    if not row:
        return
    updates = []
    params = []
    if row["search_date"] and row["search_date"] < today:
        updates.append("searches_today = 0, search_date = %s")
        params.append(today)
    if row["poster_date"] and row["poster_date"] < today:
        updates.append("posters_today = 0, poster_date = %s")
        params.append(today)
    if updates:
        db.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE openid = %s",
            params + [openid]
        )
        db.commit()


@app.route("/api/user-tier")
def api_user_tier():
    """获取用户等级和当日用量"""
    openid = request.args.get("openid", "").strip()
    if not openid:
        return jsonify({"tier": "free", "limits": TIER_LIMITS["free"]})

    _ensure_user(openid)
    _reset_daily_counters(openid)
    db = get_db()
    row = db.execute(
        "SELECT tier, searches_today, posters_today FROM users WHERE openid = %s",
        [openid]
    ).fetchone()
    if not row:
        return jsonify({"tier": "free", "limits": TIER_LIMITS["free"],
                        "searches_used": 0, "posters_used": 0})

    # 检查订阅是否过期
    tier = row["tier"]
    if tier != "free":
        expire = db.execute(
            "SELECT expires_at FROM users WHERE openid = %s", [openid]
        ).fetchone()
        if expire and expire["expires_at"] and expire["expires_at"] < datetime.now():
            tier = "free"

    limits = TIER_LIMITS.get(tier, TIER_LIMITS["free"])
    return jsonify({
        "tier": tier,
        "limits": limits,
        "searches_used": row["searches_today"],
        "posters_used": row["posters_today"],
    })


@app.route("/api/increment-usage", methods=["POST"])
def api_increment_usage():
    """原子递增用量计数，返回是否超限"""
    data = request.get_json() or {}
    openid = data.get("openid", "").strip()
    counter = data.get("counter", "searches")  # searches | posters
    if not openid:
        return jsonify({"allowed": True, "used": 0, "max": 9999})

    _ensure_user(openid)
    _reset_daily_counters(openid)
    db = get_db()

    # 原子递增
    col = "searches_today" if counter == "searches" else "posters_today"
    row = db.execute(
        f"UPDATE users SET {col} = {col} + 1 WHERE openid = %s "
        "RETURNING tier, searches_today, posters_today",
        [openid]
    ).fetchone()
    db.commit()

    if not row:
        return jsonify({"allowed": True, "used": 0, "max": 9999})

    used = row[col]
    tier = row["tier"]
    limits = TIER_LIMITS.get(tier, TIER_LIMITS["free"])
    max_val = limits["searches"] if counter == "searches" else limits["posters"]

    return jsonify({
        "allowed": used <= max_val,
        "used": used,
        "max": max_val,
        "tier": tier,
    })


@app.route("/api/activate", methods=["POST"])
def api_activate():
    """手动激活会员（管理端用，需 ADMIN_KEY 验证）"""
    import os as _os
    admin_key = _os.environ.get("ADMIN_KEY", "")
    if not admin_key:
        # 回退到文件读取
        key_file = os.path.join(os.path.dirname(__file__), ".admin_key")
        if os.path.exists(key_file):
            with open(key_file) as f:
                admin_key = f.read().strip()
    if not admin_key:
        return jsonify({"error": "admin key not configured"}), 500

    data = request.get_json() or {}
    if data.get("admin_key") != admin_key:
        return jsonify({"error": "unauthorized"}), 403

    openid = data.get("openid", "").strip()
    tier = data.get("tier", "pro").strip()
    days = int(data.get("days", 30))
    if not openid:
        return jsonify({"error": "missing openid"}), 400
    if tier not in ("pro", "team"):
        return jsonify({"error": "invalid tier"}), 400

    _ensure_user(openid)
    db = get_db()
    db.execute(
        "UPDATE users SET tier = %s, activated_at = NOW(), "
        "expires_at = NOW() + INTERVAL '%s days' WHERE openid = %s",
        [tier, days, openid]
    )
    db.commit()
    return jsonify({"ok": True, "openid": openid, "tier": tier, "expires_days": days})


@app.route("/api/generate-poster")
def api_generate_poster():
    """生成项目分享海报，返回 PNG 图片"""
    project = request.args.get("project", "").strip()
    if not project:
        return jsonify({"error": "missing project"}), 400

    db = get_db()
    # 查询项目摘要数据
    row = db.execute(
        "SELECT h.project_name, h.zone, "
        "COUNT(*) as unsold, "
        "ROUND(AVG(h.unit_price)::numeric, 0) as avg_unit, "
        "ROUND((AVG(h.total_price)/10000)::numeric, 1) as avg_total, "
        "ROUND((MIN(h.total_price)/10000)::numeric, 0) as price_min, "
        "ROUND((MAX(h.total_price)/10000)::numeric, 0) as price_max, "
        "MAX(p.developer) as developer, "
        "MAX(p.pass_date) as pass_date "
        "FROM housing_units h "
        "LEFT JOIN presale_permits p ON p.project_name = h.project_name "
        "WHERE h.project_name = %s AND h.house_usage='住宅' AND h.status IN ('期房待售','在建抵押','未售','首次登记') "
        f"AND {UNSOLD_RECENCY} "
        "GROUP BY h.project_name, h.zone",
        [project]
    ).fetchone()

    if not row:
        return jsonify({"error": "project not found"}), 404

    avg_unit_w = round(float(row["avg_unit"] or 0) / 10000, 1)

    try:
        png_bytes, _ = poster.get_or_generate(
            project_name=row["project_name"],
            zone=row["zone"] or "",
            unsold=row["unsold"],
            avg_unit=avg_unit_w,
            avg_total=row["avg_total"] or 0,
            price_min=int(row["price_min"] or 0),
            price_max=int(row["price_max"] or 0),
            developer=row["developer"] or "",
            pass_date=row["pass_date"] or "",
        )
    except Exception as e:
        return jsonify({"error": "poster generation failed"}), 500

    return send_file(
        io.BytesIO(png_bytes),
        mimetype="image/png",
        as_attachment=False,
    )


@app.route("/api/app-poster")
def api_app_poster():
    """生成小程序推广海报（引导关注）"""
    try:
        png_bytes = poster.generate_app_poster()
    except Exception as e:
        return jsonify({"error": f"poster generation failed"}), 500
    return send_file(
        io.BytesIO(png_bytes),
        mimetype="image/png",
        as_attachment=False,
    )


@app.route("/api/zones")
def api_zones():
    """区域列表"""
    db = get_db()
    rows = db.execute(
        f"SELECT DISTINCT zone FROM housing_units WHERE zone != '' AND house_usage='住宅' AND status IN ('期房待售','在建抵押','未售','首次登记') AND {UNSOLD_RECENCY} ORDER BY zone"
    ).fetchall()
    zones = list(dict.fromkeys(zone_name(r["zone"]) for r in rows))
    zones.sort(key=pinyin_sort_key)
    return jsonify({"zones": zones})


@app.route("/api/projects")
def api_projects():
    """小区列表，可按区域筛选（仅2019年后开盘 + 有可售房源）"""
    zone = request.args.get("zone", "")
    db = get_db()
    base_cond = f"h.house_usage='住宅' AND h.status IN ('期房待售','在建抵押','未售','首次登记') AND {UNSOLD_RECENCY} AND h.project_name IS NOT NULL AND h.project_name != ''"
    base_cond += f" AND (p.pass_date IS NULL OR p.pass_date >= (CURRENT_DATE - INTERVAL '5 years')::text)"
    if zone:
        rows = db.execute(
            f"SELECT DISTINCT h.project_name FROM housing_units h "
            f"LEFT JOIN presale_permits p ON p.project_name = h.project_name "
            f"WHERE h.zone=%s AND {base_cond} ORDER BY h.project_name",
            [zone],
        ).fetchall()
    else:
        rows = db.execute(
            f"SELECT DISTINCT h.project_name FROM housing_units h "
            f"LEFT JOIN presale_permits p ON p.project_name = h.project_name "
            f"WHERE {base_cond} ORDER BY h.project_name"
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
        f"WHERE project_name=%s AND status IN ('期房待售','在建抵押','未售','首次登记') AND house_usage='住宅' AND {UNSOLD_RECENCY} "
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
    conditions = ["project_name=%s", "house_usage='住宅'", f"status IN {UNSOLD_STATUSES}", UNSOLD_RECENCY]
    params = [project]
    has_filter = (price_min > 0 or price_max < 999999999 or area_min > 0 or area_max < 999999)
    if has_filter:
        conditions.append("total_price BETWEEN %s AND %s")
        conditions.append("built_area BETWEEN %s AND %s")
        params.extend([price_min, price_max, area_min, area_max])

    if building:
        conditions.append("building_name=%s")
        params.append(building)

    if search:
        conditions.append("unit_no LIKE %s")
        params.append(f"%{search}%")

    where = " AND ".join(conditions)
    sql = (
        f"SELECT unit_no, built_area, unit_price, total_price, "
        f"house_usage, status, check_date, building_name, "
        f"CASE WHEN EXISTS (SELECT 1 FROM housing_units u WHERE u.project_name = housing_units.project_name AND u.status IN ('首次登记','已转移登记')) "
        f"THEN '现售' ELSE '预售' END as sale_type, "
        f"(SELECT pass_date FROM presale_permits p WHERE p.project_name = housing_units.project_name LIMIT 1) as permit_date "
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
            "permit_date": r["permit_date"] or "",
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
    # 5 年窗口：只统计近 5 年有房源活动的楼盘
    conds = ["house_usage='住宅'", UNSOLD_RECENCY]
    params = []
    has_filter = (price_min > 0 or price_max < 999999999 or area_min > 0 or area_max < 9999)
    if has_filter:
        conds.append("total_price BETWEEN %s AND %s")
        conds.append("built_area BETWEEN %s AND %s")
        params.extend([price_min, price_max, area_min, area_max])
    where_extra = " AND " + " AND ".join(conds) if conds else ""
    
    # 构建 COUNT SQL 的辅助函数
    def _count_stats(extra_cond="", extra_params=None):
        p = params + (extra_params or [])
        base_where = where_extra[5:] if where_extra.startswith(" AND ") else "TRUE"
        row = db.execute(
            "SELECT COUNT(*) as total, "
            "COUNT(*) FILTER (WHERE h.status IN ('期房待售','在建抵押','未售','首次登记')) as unsold "
            f"FROM housing_units h "
            f"WHERE {base_where} {extra_cond}", p
        ).fetchone()
        return dict(row)

    if zone and not project:
        d = _count_stats("AND h.zone=%s", [zone])
        return jsonify({"total": d["total"], "sold": d["total"] - d["unsold"], "unsold": d["unsold"], "sold_out": d["unsold"] <= 0})

    if not project:
        # 全深圳：返回全市统计
        d = _count_stats()
        return jsonify({"total": d["total"], "sold": d["total"] - d["unsold"], "unsold": d["unsold"], "sold_out": d["unsold"] <= 0})

    cond = "AND h.project_name=%s AND h.building_name=%s" if building else "AND h.project_name=%s"
    eparams = [project, building] if building else [project]
    d = _count_stats(cond, eparams)
    if d["unsold"] <= 0:
        return jsonify({"total": 0, "sold": 0, "unsold": 0, "sold_out": True})
    return jsonify({"total": d["total"], "sold": d["total"] - d["unsold"], "unsold": d["unsold"]})


# ═══════════════════════════════════════════
# 首页 API
# ═══════════════════════════════════════════

@app.route("/api/overview")
def api_overview():
    """首页总览：市场概况数据（6 小时内存缓存）"""
    import time
    if hasattr(api_overview, "_cache") and (time.time() - api_overview._cache.get("ts", 0)) < 21600:
        return jsonify(api_overview._cache["data"])
    db = get_db()

    row = db.execute(
        "SELECT "
        "COUNT(*) as total, "
        f"SUM(CASE WHEN h.status IN ('期房待售','在建抵押','未售','首次登记') AND {UNSOLD_RECENCY} THEN 1 ELSE 0 END) as unsold, "
        f"SUM(CASE WHEN h.status IN ('期房待售','在建抵押','未售','首次登记') AND {UNSOLD_RECENCY} AND pp.project_name IS NULL THEN 1 ELSE 0 END) as presale, "
        f"SUM(CASE WHEN h.status IN ('期房待售','在建抵押','未售','首次登记') AND {UNSOLD_RECENCY} AND pp.project_name IS NOT NULL THEN 1 ELSE 0 END) as spot_sale, "
        "SUM(CASE WHEN h.status IN ('已签认购书','已签合同','已录入合同') THEN 1 ELSE 0 END) as signed, "
        "SUM(CASE WHEN h.status='已备案' THEN 1 ELSE 0 END) as filed, "
        "SUM(CASE WHEN h.status='首次登记' THEN 1 ELSE 0 END) as transferred, "
        f"ROUND((SUM(CASE WHEN h.status IN ('期房待售','在建抵押','未售','首次登记') AND h.total_price>0 AND {UNSOLD_RECENCY} THEN h.total_price ELSE 0 END) / NULLIF(SUM(CASE WHEN h.status IN ('期房待售','在建抵押','未售','首次登记') AND h.total_price>0 AND {UNSOLD_RECENCY} THEN h.built_area ELSE 0 END), 0))::numeric, 0) as avg_unit_price, "
        f"ROUND((AVG(CASE WHEN h.status IN ('期房待售','在建抵押','未售','首次登记') AND h.total_price>0 AND {UNSOLD_RECENCY} THEN h.total_price END)/10000)::numeric, 1) as avg_total, "
        f"ROUND(AVG(CASE WHEN h.status IN ('期房待售','在建抵押','未售','首次登记') AND h.total_price>0 AND {UNSOLD_RECENCY} THEN h.unit_price END)::numeric, 0) as avg_unit, "
        f"SUM(CASE WHEN h.status IN ('期房待售','在建抵押','未售','首次登记') AND {UNSOLD_RECENCY} AND ppr.project_name IS NOT NULL THEN 1 ELSE 0 END) as recent, "
        f"SUM(CASE WHEN h.status IN ('期房待售','在建抵押','未售','首次登记') AND {UNSOLD_RECENCY} THEN 1 ELSE 0 END) + "
        "SUM(CASE WHEN h.status IN ('已签认购书','已签合同','已录入合同') THEN 1 ELSE 0 END) + "
        "SUM(CASE WHEN h.status='已备案' THEN 1 ELSE 0 END) + "
        "SUM(CASE WHEN h.status='首次登记' THEN 1 ELSE 0 END) as total "
        "FROM housing_units h "
        "LEFT JOIN (SELECT DISTINCT project_name FROM housing_units WHERE status='首次登记') pp ON pp.project_name = h.project_name "
        f"LEFT JOIN (SELECT DISTINCT project_name FROM presale_permits WHERE pass_date >= (CURRENT_DATE - INTERVAL '1 month')::text) ppr ON ppr.project_name = h.project_name "
        "WHERE h.house_usage='住宅'"
    ).fetchone()

    # 各区未售住宅统计（仅预售，排除现售）
    zone_rows = db.execute(
        "SELECT h.zone, COUNT(*) as cnt, "
        "SUM(CASE WHEN pp.project_name IS NULL THEN 1 ELSE 0 END) as presale_cnt, "
        "SUM(CASE WHEN pp.project_name IS NOT NULL THEN 1 ELSE 0 END) as spot_cnt, "
        "ROUND((AVG(h.total_price)/10000)::numeric, 1) as avg_t, "
        "ROUND(AVG(h.unit_price)::numeric, 0) as avg_u "
        f"FROM housing_units h "
        "LEFT JOIN (SELECT DISTINCT project_name FROM housing_units WHERE status='首次登记') pp ON pp.project_name = h.project_name "
        f"WHERE h.house_usage='住宅' AND h.status IN ('期房待售','在建抵押','未售','首次登记') AND h.zone != '' AND {UNSOLD_RECENCY} "
        "GROUP BY h.zone ORDER BY cnt DESC"
    ).fetchall()

    # 近90天各区一手住宅日均成交量
    sales_rows = db.execute(
        "SELECT d.name as zone, "
        "SUM(CASE WHEN t.property_type_id = 1 THEN t.deal_count ELSE 0 END) / 90.0 as daily_avg "
        "FROM transaction_data t "
        "JOIN districts d ON d.id = t.district_id "
        "WHERE t.report_date >= CURRENT_DATE - INTERVAL '90 days' "
        "AND t.city_id = 1 AND t.building_type = '住宅' AND t.district_id != 5999 "
        "GROUP BY d.name"
    ).fetchall()
    # 映射 housing_units.zone → districts.name（名称不一致）
    zone_alias = {"深汕合作": "深汕"}
    sales_map = {}
    for r in sales_rows:
        z = r["zone"]
        sales_map[z] = max(r["daily_avg"] or 0, 0.01)
        # 别名也指向同一数据
        for k, v in zone_alias.items():
            if v == z:
                sales_map[k] = max(r["daily_avg"] or 0, 0.01)

    # 合并同名区域（如深汕合作→深汕）
    merged = {}
    for r in zone_rows:
        name = zone_name(r["zone"])
        if name not in merged:
            merged[name] = {"count": 0, "presale": 0, "spot": 0, "total_t": 0.0, "total_u": 0.0, "n": 0}
        m = merged[name]
        m["count"] += r["cnt"]
        m["presale"] += r["presale_cnt"]
        m["spot"] += r["spot_cnt"]
        m["total_t"] += float(r["avg_t"] or 0) * r["cnt"]
        m["total_u"] += float(r["avg_u"] or 0) * r["cnt"]
        m["n"] += r["cnt"]
    zones = [{
        "name": name, "count": m["count"],
        "presale": m["presale"], "spot_sale": m["spot"],
        "avg_total": round(m["total_t"] / m["n"], 1) if m["n"] else 0,
        "avg_unit": round(m["total_u"] / m["n"], 0) if m["n"] else 0,
        "inventory_months": round(m["count"] / sales_map.get(name, 0.01) / 30, 1),
    } for name, m in merged.items()]
    zones.sort(key=lambda z: z["count"], reverse=True)

    data = {
        "total": row["total"],
        "unsold": row["unsold"],
        "presale": row["presale"],
        "spot_sale": row["spot_sale"],
        "signed": row["signed"],
        "filed": row["filed"],
        "transferred": row["transferred"],
        "avg_total": float(row["avg_total"] or 0),
        "avg_unit": float(row["avg_unit"] or 0),
        "avg_unit_price": float(row["avg_unit_price"] or 0),
        "recent": row["recent"],
        "zones": zones,
    }
    api_overview._cache = {"data": data, "ts": time.time()}
    return jsonify(data)


@app.route("/api/rankings")
def api_rankings():
    """榜单：总价最低/最高 + 单价最低/最高"""
    db = get_db()
    base = f"WHERE house_usage='住宅' AND status IN ('期房待售','在建抵押','未售','首次登记') AND project_name IS NOT NULL AND project_name != '' AND total_price >= 10000 AND unit_price > 0 AND built_area > 0 AND {UNSOLD_RECENCY}"

    def run(order):
        return db.execute(
            f"SELECT unit_api_id, project_name, building_name, unit_no, built_area, unit_price, "
            f"total_price, zone FROM housing_units {base} {order} LIMIT 10"
        ).fetchall()

    cheap_total  = run("ORDER BY total_price ASC")
    dear_total   = run("ORDER BY total_price DESC")
    cheap_unit   = run("ORDER BY unit_price ASC")
    dear_unit    = run("ORDER BY unit_price DESC")

    def fmt(row):
        return {
            "unit_api_id": row["unit_api_id"],
            "project_name": row["project_name"] or "",
            "building_name": row["building_name"] or "",
            "unit_no": row["unit_no"] or "",
            "built_area": row["built_area"] or 0,
            "unit_price": row["unit_price"] or 0,
            "total_price": round(row["total_price"] / 10000, 1),
            "zone": row["zone"] or "",
        }

    return jsonify({
        "cheap_total": [fmt(r) for r in cheap_total],
        "dear_total":  [fmt(r) for r in dear_total],
        "cheap_unit":  [fmt(r) for r in cheap_unit],
        "dear_unit":   [fmt(r) for r in dear_unit],
    })


@app.route("/api/latest-permits")
def api_latest_permits():
    """最新预售证 — 按项目去重，取最新预售证日期，优先有可售房源（共20条）"""
    db = get_db()

    # 先算每个项目的未售房源数（独立子查询避免笛卡尔积）
    unsold_map = {}
    unsold_rows = db.execute(
        f"SELECT project_name, COUNT(*) as cnt FROM housing_units "
        f"WHERE house_usage='住宅' AND status IN ('期房待售','在建抵押','未售','首次登记') AND {UNSOLD_RECENCY} "
        f"GROUP BY project_name"
    ).fetchall()
    for r in unsold_rows:
        unsold_map[r["project_name"]] = r["cnt"]

    # 预售证按项目去重，取最新日期
    permits_rows = db.execute(
        "SELECT project_name, MAX(developer) as developer, MAX(zone) as zone, "
        "MAX(pass_date) as pass_date "
        "FROM presale_permits GROUP BY project_name "
        "ORDER BY MAX(pass_date) DESC"
    ).fetchall()

    # 组装结果：有可售的排前面
    result = []
    for r in permits_rows:
        u = unsold_map.get(r["project_name"], 0)
        result.append({
            "project_name": r["project_name"] or "",
            "developer": r["developer"] or "",
            "zone": zone_name(r["zone"] or ""),
            "pass_date": r["pass_date"] or "",
            "unsold": u,
        })

    # 只展示有可售房源的，按日期倒序
    permits = sorted(
        [x for x in result if x["unsold"] > 0],
        key=lambda x: x["pass_date"], reverse=True
    )[:20]

    return jsonify({"permits": permits})


# ═══════════════════════════════════════════
# 成交分析 API（数据源: transaction_data + monthly_aggregation）
# ═══════════════════════════════════════════

@app.route("/api/transactions/summary")
def api_transactions_summary():
    """本月成交概览：总量、新房、二手房、环比、同比"""
    db = get_db()

    # 最新交易日期
    latest = db.execute(
        "SELECT MAX(report_date) as dt FROM transaction_data "
        "WHERE city_id=1 AND district_id != 5999"
    ).fetchone()
    latest_date = str(latest["dt"]) if latest and latest["dt"] else ""

    base = "WHERE city_id=1 AND district_id != 5999 "
    rows = db.execute(
        "SELECT TO_CHAR(report_date, 'YYYY-MM') as ym, "
        "SUM(CASE WHEN property_type_id=1 THEN deal_count ELSE 0 END) as new_cnt, "
        "SUM(CASE WHEN property_type_id=2 THEN deal_count ELSE 0 END) as used_cnt, "
        "SUM(deal_area) as area "
        f"FROM transaction_data {base} "
        "GROUP BY ym ORDER BY ym DESC LIMIT 13"
    ).fetchall()
    this_row = rows[0] if len(rows) > 0 else None
    last_row = rows[1] if len(rows) > 1 else None
    yoy_row = None
    if this_row:
        this_ym = this_row["ym"]
        for r in rows[1:]:
            if r["ym"][5:] == this_ym[5:]:
                yoy_row = r
                break

    def fmt(r):
        if not r: return None
        parts = r["ym"].split("-")
        year, month = int(parts[0]), int(parts[1])
        # 当月：按已过天数估算完整度；过往月份：100%
        now = datetime.now()
        if year == now.year and month == now.month:
            dom = calendar.monthrange(year, month)[1]
            completeness = min(100, round(now.day / dom * 100))
        else:
            completeness = 100
        return {
            "year": year, "month": month,
            "total": (r["new_cnt"] or 0) + (r["used_cnt"] or 0),
            "new": r["new_cnt"] or 0,
            "used": r["used_cnt"] or 0,
            "area": round(r["area"] or 0, 1),
            "completeness": completeness,
        }

    tm = fmt(this_row)
    lm = fmt(last_row)
    ym = fmt(yoy_row)

    def pct(a, b):
        return round((a - b) / b * 100, 1) if a and b and b else 0

    return jsonify({
        "this_month": tm, "last_month": lm, "yoy_month": ym,
        "latest_date": latest_date,
        "total_mom_pct": pct(tm["total"], lm["total"]) if tm and lm else 0,
        "total_yoy_pct": pct(tm["total"], ym["total"]) if tm and ym else 0,
        "new_mom_pct":   pct(tm["new"], lm["new"]) if tm and lm else 0,
        "new_yoy_pct":   pct(tm["new"], ym["new"]) if tm and ym else 0,
        "used_mom_pct":  pct(tm["used"], lm["used"]) if tm and lm else 0,
        "used_yoy_pct":  pct(tm["used"], ym["used"]) if tm and ym else 0,
    })


@app.route("/api/transactions/districts")
def api_transactions_districts():
    """本月各区一手/二手成交分布"""
    db = get_db()
    # 从 transaction_data 直接获取最新月份
    cur = db.execute(
        "SELECT TO_CHAR(report_date, 'YYYY-MM') as ym FROM transaction_data "
        "WHERE city_id=1 "
        "ORDER BY report_date DESC LIMIT 1"
    ).fetchone()
    if not cur:
        return jsonify({"new": [], "used": []})
    ym = cur["ym"]

    def query(ptype_id):
        rows = db.execute(
            "SELECT d.name as zone, SUM(t.deal_count) as cnt "
            "FROM transaction_data t JOIN districts d ON d.id = t.district_id "
            "WHERE t.property_type_id = %s "
            "AND t.city_id = 1 "
            "AND TO_CHAR(t.report_date, 'YYYY-MM') = %s "
            "AND d.name != '全市' "
            "GROUP BY d.name ORDER BY cnt DESC", [ptype_id, ym]
        ).fetchall()
        total = sum(r["cnt"] for r in rows)
        top8 = [{
            "zone": r["zone"], "count": r["cnt"],
            "pct": round(r["cnt"] / total * 100, 1) if total else 0,
        } for r in rows[:8]]
        other_cnt = total - sum(i["count"] for i in top8)
        if other_cnt > 0:
            top8.append({"zone": "其他", "count": other_cnt,
                         "pct": round(other_cnt / total * 100, 1) if total else 0})
        return {"items": top8, "total": total}

    return jsonify({"new": query(1), "used": query(2)})


@app.route("/api/transactions/trends")
def api_transactions_trends():
    """近 N 个月成交量走势（新房+二手）"""
    months = request.args.get("months", 12, type=int)
    db = get_db()
    rows = db.execute(
        "SELECT TO_CHAR(report_date, 'YYYY-MM') as month, "
        "SUM(CASE WHEN property_type_id=1 THEN deal_count ELSE 0 END) as new_count, "
        "SUM(CASE WHEN property_type_id=2 THEN deal_count ELSE 0 END) as used_count, "
        "SUM(deal_count) as total, "
        "SUM(deal_area) as area "
        f"FROM transaction_data "
        f"WHERE city_id=1 AND district_id != 5999 "
        "AND TO_CHAR(report_date, 'YYYY-MM') != TO_CHAR(CURRENT_DATE, 'YYYY-MM') "
        "GROUP BY month ORDER BY month DESC LIMIT %s",
        [months]
    ).fetchall()
    trends = [{
        "month": r["month"],
        "total": r["total"] or 0,
        "new": r["new_count"] or 0,
        "used": r["used_count"] or 0,
        "area": round(r["area"] or 0, 1),
    } for r in rows]
    return jsonify({"trends": trends})


@app.route("/api/project-sales-rank")
def api_project_sales_rank():
    """近N天楼盘销量排行（全市/分区）"""
    zone = request.args.get("zone", "").strip()
    days = request.args.get("days", 30, type=int)
    db = get_db()

    conds = [
        "new_status IN ('已网签','已备案','已转移登记')",
        "old_status IN ('期房待售','在建抵押','未售','首次登记')",
        f"changed_at >= NOW() - INTERVAL '{days} days'",
        "project_name IS NOT NULL",
        "zone IS NOT NULL",
    ]
    params = []
    if zone:
        conds.append("zone = %s")
        params.append(zone)

    where = " AND ".join(conds)
    rows = db.execute(
        f"SELECT project_name, zone, COUNT(*) as sold_count "
        f"FROM unit_change_log WHERE {where} "
        f"GROUP BY project_name, zone ORDER BY sold_count DESC LIMIT 10",
        params
    ).fetchall()

    ranks = [{
        "project_name": r["project_name"],
        "zone": r["zone"],
        "sold_count": r["sold_count"],
    } for r in rows]

    # 有数据的区域列表（供 tab 使用）
    zone_rows = db.execute(
        "SELECT zone, COUNT(*) as cnt FROM unit_change_log "
        "WHERE new_status IN ('已网签','已备案','已转移登记') AND old_status IN ('期房待售','在建抵押','未售','首次登记') "
        f"AND changed_at >= NOW() - INTERVAL '{days} days' "
        "AND zone IS NOT NULL "
        "GROUP BY zone ORDER BY cnt DESC LIMIT 6"
    ).fetchall()
    zones = [r["zone"] for r in zone_rows]

    return jsonify({"ranks": ranks, "zones": zones, "days": days})


@app.route("/api/transactions/recent")
def api_transactions_recent():
    """近期日成交明细（按日期聚合）"""
    days = request.args.get("days", 30, type=int)
    zone_id = request.args.get("zone_id", type=int)
    db = get_db()

    conds = ["report_date IS NOT NULL", "city_id = 1", "district_id != 5999"]
    params = []
    if zone_id:
        conds.append("district_id = %s")
        params.append(zone_id)
    where = " AND ".join(conds)

    rows = db.execute(
        f"SELECT TO_CHAR(report_date, 'YYYY-MM-DD') as report_date, "
        f"SUM(CASE WHEN property_type_id=1 THEN deal_count ELSE 0 END) as new_cnt, "
        f"SUM(CASE WHEN property_type_id=2 THEN deal_count ELSE 0 END) as used_cnt "
        f"FROM transaction_data WHERE {where} "
        f"GROUP BY report_date ORDER BY report_date DESC LIMIT %s",
        params + [days]
    ).fetchall()

    items = [{
        "date": r["report_date"],
        "new": r["new_cnt"] or 0,
        "used": r["used_cnt"] or 0,
        "total": (r["new_cnt"] or 0) + (r["used_cnt"] or 0),
    } for r in rows]

    return jsonify({"items": items})


@app.route("/api/dashboard")
def api_dashboard():
    """成交仪表盘 — 一次请求替代 5 个独立调用（10 分钟内存缓存）"""
    import time
    months = request.args.get("months", 12, type=int)
    cache_key = f"dashboard_{months}"
    now = time.time()
    if hasattr(api_dashboard, "_cache") and api_dashboard._cache.get("key") == cache_key and (now - api_dashboard._cache.get("ts", 0)) < 600:
        return jsonify(api_dashboard._cache["data"])
    db = get_db()
    result = {}

    # summary
    latest = db.execute(
        "SELECT MAX(report_date) as dt FROM transaction_data WHERE city_id=1 AND district_id!=5999"
    ).fetchone()
    latest_date = str(latest["dt"]) if latest and latest["dt"] else ""
    rows = db.execute(
        "SELECT TO_CHAR(report_date,'YYYY-MM') as ym, "
        "SUM(CASE WHEN property_type_id=1 THEN deal_count ELSE 0 END) as nc, "
        "SUM(CASE WHEN property_type_id=2 THEN deal_count ELSE 0 END) as uc "
        "FROM transaction_data WHERE city_id=1 AND district_id!=5999 "
        "GROUP BY ym ORDER BY ym DESC LIMIT 2"
    ).fetchall()
    r0 = rows[0] if rows else None
    r1 = rows[1] if len(rows) > 1 else None
    result["summary"] = {
        "latest_date": latest_date,
        "this_month": {"month": r0["ym"] if r0 else "", "new": r0["nc"] or 0 if r0 else 0, "used": r0["uc"] or 0 if r0 else 0, "total": (r0["nc"] or 0)+(r0["uc"] or 0) if r0 else 0} if r0 else None,
        "last_month": {"total": (r1["nc"] or 0)+(r1["uc"] or 0) if r1 else 0, "new": r1["nc"] or 0 if r1 else 0, "used": r1["uc"] or 0 if r1 else 0} if r1 else None,
    }

    # districts
    cur = db.execute("SELECT TO_CHAR(report_date,'YYYY-MM') as ym FROM transaction_data WHERE city_id=1 ORDER BY report_date DESC LIMIT 1").fetchone()
    if cur:
        ym = cur["ym"]
        def _pt(pt):
            rs = db.execute(
                "SELECT d.name as z, SUM(t.deal_count) as c FROM transaction_data t "
                "JOIN districts d ON d.id=t.district_id WHERE t.property_type_id=%s "
                "AND t.city_id=1 AND TO_CHAR(t.report_date,'YYYY-MM')=%s AND d.name!='全市' "
                "GROUP BY d.name ORDER BY c DESC", [pt, ym]
            ).fetchall()
            t = sum(r["c"] for r in rs)
            return {"items": [{"zone": r["z"], "count": r["c"], "pct": round(r["c"]/t*100,1) if t else 0} for r in rs[:8]], "total": t}
        result["districts"] = {"new": _pt(1), "used": _pt(2)}
    else:
        result["districts"] = {"new": {"items": []}, "used": {"items": []}}

    # trends
    trows = db.execute(
        "SELECT TO_CHAR(report_date,'YYYY-MM') as m, "
        "SUM(CASE WHEN property_type_id=1 THEN deal_count ELSE 0 END) as nc, "
        "SUM(CASE WHEN property_type_id=2 THEN deal_count ELSE 0 END) as uc "
        "FROM transaction_data WHERE city_id=1 AND district_id!=5999 "
        "AND TO_CHAR(report_date,'YYYY-MM') != TO_CHAR(CURRENT_DATE,'YYYY-MM') "
        "GROUP BY m ORDER BY m DESC LIMIT %s", [months]
    ).fetchall()
    result["trends"] = [{"month": r["m"], "new": r["nc"] or 0, "used": r["uc"] or 0, "total": (r["nc"] or 0)+(r["uc"] or 0)} for r in trows]

    # recent
    days = request.args.get("days", 14, type=int)
    rrows = db.execute(
        "SELECT TO_CHAR(report_date,'YYYY-MM-DD') as dt, "
        "SUM(CASE WHEN property_type_id=1 THEN deal_count ELSE 0 END) as nc, "
        "SUM(CASE WHEN property_type_id=2 THEN deal_count ELSE 0 END) as uc "
        "FROM transaction_data WHERE city_id=1 AND district_id!=5999 "
        "GROUP BY dt ORDER BY dt DESC LIMIT %s", [days]
    ).fetchall()
    result["dailyItems"] = [{"date": r["dt"], "new": r["nc"] or 0, "used": r["uc"] or 0, "total": (r["nc"] or 0)+(r["uc"] or 0)} for r in rrows]

    # sales_rank
    sdays = request.args.get("sales_days", 30, type=int)
    srows = db.execute(
        "SELECT project_name, zone, COUNT(*) as sc FROM unit_change_log "
        "WHERE new_status IN ('已网签','已备案','已转移登记') AND old_status IN ('期房待售','在建抵押','未售','首次登记') "
        f"AND changed_at >= NOW() - INTERVAL '{sdays} days' "
        "AND project_name IS NOT NULL AND zone IS NOT NULL "
        "GROUP BY project_name, zone ORDER BY sc DESC LIMIT 10"
    ).fetchall()
    zrows = db.execute(
        "SELECT zone, COUNT(*) as cnt FROM unit_change_log "
        "WHERE new_status IN ('已网签','已备案','已转移登记') AND old_status IN ('期房待售','在建抵押','未售','首次登记') "
        f"AND changed_at >= NOW() - INTERVAL '{sdays} days' "
        "AND zone IS NOT NULL "
        "GROUP BY zone ORDER BY cnt DESC LIMIT 6"
    ).fetchall()
    result["salesRanks"] = [{"project_name": r["project_name"], "zone": r["zone"], "sold_count": r["sc"]} for r in srows]
    result["salesZones"] = [r["zone"] for r in zrows]

    api_dashboard._cache = {"key": cache_key, "data": result, "ts": now}
    return jsonify(result)


@app.route("/api/daily-stats")
def api_daily_stats():
    """日成交查询（按日期范围），用于同比/环比计算"""
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    if not start or not end:
        return jsonify({"items": []})

    db = get_db()
    rows = db.execute(
        "SELECT TO_CHAR(report_date,'YYYY-MM-DD') as dt, "
        "SUM(CASE WHEN property_type_id=1 THEN deal_count ELSE 0 END) as nc, "
        "SUM(CASE WHEN property_type_id=2 THEN deal_count ELSE 0 END) as uc "
        "FROM transaction_data WHERE city_id=1 AND district_id!=5999 "
        "AND report_date >= %s AND report_date <= %s "
        "GROUP BY dt ORDER BY dt",
        [start, end]
    ).fetchall()
    items = [{"date": r["dt"], "new": r["nc"] or 0, "used": r["uc"] or 0} for r in rows]
    return jsonify({"items": items})


# ═══════════════════════════════════════════
# 运营后台 API
# ═══════════════════════════════════════════

@app.route("/api/admin/status")
def api_admin_status():
    """运营面板：同步状态、数据统计、系统健康"""
    db = get_db()

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

    # 最近同步用 check_date 替代
    last_sync = db.execute(
        "SELECT MAX(check_date) as batch FROM housing_units"
    ).fetchone()["batch"] or "-"

    # 最近更新日期
    last_check = db.execute(
        "SELECT MAX(check_date) as dt FROM housing_units"
    ).fetchone()["dt"] or "-"

    # 预售证数量
    permits = db.execute("SELECT COUNT(*) as cnt FROM presale_permits").fetchone()["cnt"]

    # DB 大小（PG）
    db_size = db.execute(
        "SELECT pg_database_size(current_database()) as sz"
    ).fetchone()
    db_size_mb = round(db_size["sz"] / 1048576, 1) if db_size and db_size["sz"] else 0

    # 各区可售住宅
    zone_rows = db.execute(
        "SELECT zone, COUNT(*) as cnt FROM housing_units "
        f"WHERE house_usage='住宅' AND status IN ('期房待售','在建抵押','未售','首次登记') AND zone != '' AND {UNSOLD_RECENCY} "
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
        "zones": [{"name": zone_name(r["zone"]), "unsold": r["cnt"]} for r in zone_rows],
    })


# ── 小区历史成交价查询 ──


@app.route("/api/project-history-search-meta")
def api_project_history_search_meta():
    """搜索页初始化数据：热门小区 + 区域统计"""
    db = get_db()
    hot = db.execute("""
        SELECT project_name, zone, COUNT(*) as cnt,
               ROUND(AVG(unit_price) FILTER (WHERE unit_price > 0)) as avg_price
        FROM housing_units
        WHERE project_name IS NOT NULL AND check_date >= '2020-01-01'
        GROUP BY project_name, zone ORDER BY cnt DESC LIMIT 20
    """).fetchall()
    zones = db.execute("""
        SELECT zone, COUNT(DISTINCT project_name) as project_count,
               COUNT(*) as record_count
        FROM housing_units
        WHERE project_name IS NOT NULL AND check_date >= '2010-01-01'
          AND zone IS NOT NULL
        GROUP BY zone ORDER BY record_count DESC
    """).fetchall()
    return jsonify({
        "hot": [{"project_name": r["project_name"], "zone": zone_name(r["zone"] or ""),
                 "count": r["cnt"], "avg_price": float(r["avg_price"] or 0)} for r in hot],
        "zones": [{"zone": zone_name(r["zone"]), "project_count": r["project_count"],
                   "record_count": r["record_count"]} for r in zones],
    })


@app.route("/api/project-history-search")
def api_project_history_search():
    """小区搜索：模糊匹配 + 区域筛选"""
    q = request.args.get("q", "").strip()
    zone = request.args.get("zone", "").strip()

    conditions = ["project_name IS NOT NULL", "check_date >= '2010-01-01'"]
    params = []

    if q:
        conditions.append("project_name ILIKE %s")
        params.append(f"%{q}%")
    if zone:
        conditions.append("zone = %s")
        params.append(zone)

    where = " AND ".join(conditions)
    sql = f"""
        SELECT project_name, zone, COUNT(*) as record_count,
               MIN(check_date) as earliest, MAX(check_date) as latest,
               ROUND(AVG(unit_price) FILTER (WHERE unit_price > 0)) as avg_price
        FROM housing_units
        WHERE {where}
        GROUP BY project_name, zone
        ORDER BY record_count DESC
        LIMIT 50
    """
    rows = get_db().execute(sql, params).fetchall()
    results = [{
        "project_name": r["project_name"] or "",
        "zone": zone_name(r["zone"] or ""),
        "record_count": r["record_count"],
        "earliest": r["earliest"] or "",
        "latest": r["latest"] or "",
        "avg_price": float(r["avg_price"] or 0),
    } for r in rows]
    return jsonify({"projects": results})


@app.route("/api/project-history")
def api_project_history():
    """小区历史成交：趋势 + 摘要 + 筛选选项 + 分页列表"""
    project = request.args.get("project", "").strip()
    if not project:
        return jsonify({"error": "project required"}), 400

    years = request.args.get("years", "").strip()
    building = request.args.get("building", "").strip()
    sort = request.args.get("sort", "date_desc")
    offset = request.args.get("offset", 0, type=int)
    limit = min(request.args.get("limit", 50, type=int), 200)

    conditions = ["project_name = %s"]
    params = [project]

    if years:
        year_list = [y.strip() for y in years.split(",") if y.strip().isdigit()]
        if year_list:
            placeholders = ",".join(["%s"] * len(year_list))
            conditions.append(f"SUBSTRING(check_date,1,4) IN ({placeholders})")
            params.extend(year_list)
    if building:
        conditions.append("building_name = %s")
        params.append(building)

    where = " AND ".join(conditions)
    db = get_db()

    # 1. 年度均价趋势
    trend_rows = db.execute(f"""
        SELECT SUBSTRING(check_date,1,4) as year,
               ROUND(AVG(unit_price) FILTER (WHERE unit_price > 0)) as avg_price,
               COUNT(*) as cnt
        FROM housing_units WHERE {where}
        GROUP BY year ORDER BY year
    """, params).fetchall()
    trend = [{"year": r["year"], "avg_price": float(r["avg_price"] or 0), "count": r["cnt"]} for r in trend_rows]

    # 2. 摘要统计
    summary_row = db.execute(f"""
        SELECT COUNT(*) as total_records,
               ROUND(AVG(unit_price) FILTER (WHERE unit_price > 0)) as avg_price,
               MAX(unit_price) FILTER (WHERE unit_price > 0) as max_price,
               MIN(unit_price) FILTER (WHERE unit_price > 0) as min_price,
               ROUND(AVG(CASE WHEN total_price > 0 THEN total_price END)::numeric/10000, 1) as avg_total_wan,
               ROUND(AVG(CASE WHEN built_area > 0 THEN built_area END)::numeric, 1) as avg_area
        FROM housing_units WHERE {where}
    """, params).fetchone()

    # 3. 可选楼栋
    buildings = [{"name": r["building_name"], "count": r["cnt"]} for r in db.execute(f"""
        SELECT building_name, COUNT(*) as cnt
        FROM housing_units WHERE {where} AND building_name IS NOT NULL
        GROUP BY building_name ORDER BY cnt DESC
    """, params).fetchall()]

    # 4. 可选年份
    years_list = [r["year"] for r in db.execute(f"""
        SELECT DISTINCT SUBSTRING(check_date,1,4) as year
        FROM housing_units WHERE {where} ORDER BY year DESC
    """, params).fetchall()]

    # 5. 成交记录（分页）
    order_map = {
        "date_desc": "check_date DESC",
        "date_asc": "check_date ASC",
        "price_desc": "unit_price DESC NULLS LAST",
        "price_asc": "unit_price ASC NULLS LAST",
    }
    order_clause = order_map.get(sort, "check_date DESC")
    list_rows = db.execute(f"""
        SELECT id, check_date, building_name, unit_no, built_area,
               unit_price, total_price, house_usage, house_attr, status, zone, unit_type
        FROM housing_units WHERE {where}
        ORDER BY {order_clause}
        LIMIT %s OFFSET %s
    """, params + [limit, offset]).fetchall()
    records = [{
        "id": r["id"],
        "date": r["check_date"] or "",
        "building": r["building_name"] or "",
        "unit_no": r["unit_no"] or "",
        "area": float(r["built_area"] or 0),
        "unit_price": float(r["unit_price"] or 0),
        "total_price_wan": round(float(r["total_price"] or 0) / 10000, 1),
        "usage": r["house_usage"] or "",
        "attr": r["house_attr"] or "",
        "status": r["status"] or "",
        "layout": r["unit_type"] or "",
    } for r in list_rows]

    return jsonify({
        "project": project,
        "zone": zone_name(list_rows[0]["zone"] or "") if list_rows else "",
        "summary": {
            "total_records": summary_row["total_records"],
            "avg_price": float(summary_row["avg_price"] or 0),
            "max_price": float(summary_row["max_price"] or 0),
            "min_price": float(summary_row["min_price"] or 0),
            "avg_total_wan": float(summary_row["avg_total_wan"] or 0),
            "avg_area": float(summary_row["avg_area"] or 0),
        },
        "trend": trend,
        "buildings": buildings,
        "years": years_list,
        "records": records,
    })


@app.route("/api/project-history-detail")
def api_project_history_detail():
    """成交详情：单条记录 + 同小区近期成交"""
    uid = request.args.get("id", 0, type=int)
    if not uid:
        return jsonify({"error": "id required"}), 400

    db = get_db()
    row = db.execute("""
        SELECT id, project_name, zone, check_date, building_name, unit_no,
               built_area, unit_price, total_price, house_usage, house_attr,
               status, permit_no, parcel_no
        FROM housing_units WHERE id = %s
    """, (uid,)).fetchone()

    if not row:
        return jsonify({"error": "not found"}), 404

    detail = {
        "id": row["id"],
        "project_name": row["project_name"] or "",
        "zone": zone_name(row["zone"] or ""),
        "date": row["check_date"] or "",
        "building": row["building_name"] or "",
        "unit_no": row["unit_no"] or "",
        "area": float(row["built_area"] or 0),
        "unit_price": float(row["unit_price"] or 0),
        "total_price_wan": round(float(row["total_price"] or 0) / 10000, 1),
        "usage": row["house_usage"] or "",
        "attr": row["house_attr"] or "",
        "status": row["status"] or "",
        "permit_no": row["permit_no"] or "",
        "parcel_no": row["parcel_no"] or "",
    }

    # 同小区近期成交（最多5条）
    recent_rows = db.execute("""
        SELECT id, check_date, building_name, unit_no, built_area,
               unit_price, total_price
        FROM housing_units
        WHERE project_name = %s AND id != %s
        ORDER BY check_date DESC LIMIT 5
    """, (row["project_name"], uid)).fetchall()
    recent = [{
        "id": r["id"],
        "date": r["check_date"] or "",
        "building": r["building_name"] or "",
        "unit_no": r["unit_no"] or "",
        "area": float(r["built_area"] or 0),
        "unit_price": float(r["unit_price"] or 0),
        "total_price_wan": round(float(r["total_price"] or 0) / 10000, 1),
    } for r in recent_rows]

    return jsonify({"detail": detail, "recent": recent})


if __name__ == "__main__":
    print("🏠 备案价查询 API 服务启动")
    print(f"   本地访问: http://localhost:5001/")
    ensure_indexes()
    app.run(host="0.0.0.0", port=5001, debug=True)
