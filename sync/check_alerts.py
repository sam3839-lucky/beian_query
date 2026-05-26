#!/usr/bin/env python3
"""检查新增预售证，向关注用户推送订阅消息。Cron: 0 9 * * *"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import psycopg2, psycopg2.extras, requests, time

DB = {
    "dbname": os.environ.get("DB_NAME", "property_clawer"),
    "user": os.environ.get("DB_USER", "property_clawer"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "host": os.environ.get("DB_HOST", "localhost"),
}
WECHAT_APPID = os.environ.get("WECHAT_APPID", "")
WECHAT_SECRET = os.environ.get("WECHAT_SECRET", "")
# 订阅消息模板 ID（在微信公众平台申请后替换）
TEMPLATE_ID = os.environ.get("WX_TEMPLATE_ID", "")

def get_token():
    resp = requests.get("https://api.weixin.qq.com/cgi-bin/token",
        params={"grant_type": "client_credential", "appid": WECHAT_APPID, "secret": WECHAT_SECRET}, timeout=10)
    return resp.json().get("access_token")

def main():
    if not WECHAT_APPID or not TEMPLATE_ID:
        print("WECHAT_APPID or WX_TEMPLATE_ID not set, skip")
        return

    conn = psycopg2.connect(**DB)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # 昨天新增的预售证
    cur.execute("""
        SELECT project_name FROM presale_permits
        WHERE pass_date = CURRENT_DATE - INTERVAL '1 day'
    """)
    new_projects = [r["project_name"] for r in cur.fetchall()]
    if not new_projects:
        print("no new permits today")
        cur.close(); conn.close(); return

    print(f"new permits: {new_projects}")

    # 匹配关注用户（24h 内未推送过的）
    placeholders = ",".join(["%s"] * len(new_projects))
    cur.execute(f"""
        SELECT s.openid, s.project_name FROM subscriptions s
        WHERE s.project_name IN ({placeholders})
        AND (s.last_notified_at IS NULL OR s.last_notified_at < CURRENT_DATE - INTERVAL '1 day')
    """, new_projects)
    alerts = cur.fetchall()
    if not alerts:
        print("no subscribers to notify")
        cur.close(); conn.close(); return

    token = get_token()
    if not token:
        print("failed to get access_token")
        cur.close(); conn.close(); return

    sent = 0
    for a in alerts:
        body = {
            "touser": a["openid"],
            "template_id": TEMPLATE_ID,
            "page": f"pages/index/index",
            "data": {
                "thing1": {"value": "新预售证"},
                "thing2": {"value": a["project_name"][:20]},
                "thing3": {"value": "你关注的项目有新预售证发布，点击查看详情"},
                "date4": {"value": time.strftime("%Y-%m-%d")},
                "thing5": {"value": "点击查看"},
            }
        }
        resp = requests.post(
            f"https://api.weixin.qq.com/cgi-bin/message/subscribe/send?access_token={token}",
            json=body, timeout=10
        )
        result = resp.json()
        if result.get("errcode") == 0:
            sent += 1
            cur.execute(
                "UPDATE subscriptions SET last_notified_at = NOW(), notification_count = notification_count + 1 WHERE openid = %s AND project_name = %s",
                [a["openid"], a["project_name"]]
            )
        else:
            print(f"  push failed for {a['openid']}: {result}")

    conn.commit()
    cur.close(); conn.close()
    print(f"sent {sent}/{len(alerts)} pushes")

if __name__ == "__main__":
    main()
