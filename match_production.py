#!/usr/bin/env python3
"""
生产库项目匹配 — 将 housing_units 的 project_name 对齐 presale_permits
策略：楼栋名前缀匹配 + 开发商名匹配
"""
import sqlite3, sys, os, re
from collections import defaultdict

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "property.db")
DRY_RUN = "--dry-run" in sys.argv

conn = sqlite3.connect(DB_PATH)

# 加载预售证项目名
permits = {}
for row in conn.execute("SELECT DISTINCT project_name, developer, zone FROM presale_permits"):
    permits[row[0]] = {"developer": row[1], "zone": row[2]}
print(f"预售证项目: {len(permits)}")

# 找出未匹配的楼栋（project_name 为空 或 不在预售证列表中）
permit_names = set(permits.keys())
unmatched = conn.execute("""
    SELECT DISTINCT building_name, owner_name, parcel_no, COUNT(*) as cnt
    FROM housing_units
    WHERE project_name IS NULL OR project_name NOT IN ({})
    GROUP BY building_name
""".format(','.join('?' * len(permit_names))), list(permit_names)).fetchall()

print(f"未匹配楼栋: {len(unmatched)}")

# 匹配
def extract_candidates(bldg):
    """从楼栋名中提取可能项目名"""
    cands = [bldg]
    for pat in [
        r'^(.+?)(\d+栋.*)$', r'^(.+?)(\d+号楼.*)$',
        r'^(.+?)([A-F]\d*座.*)$', r'^(.+?)(\d+单元.*)$',
        r'^(.+?)(\d+区.*)$', r'^(.+?)(\d+期.*)$',
    ]:
        m = re.match(pat, bldg)
        if m: cands.append(m.group(1))
    return cands

def prefix_match(bldg, projects):
    best, best_len = None, 0
    for p in projects:
        if bldg.startswith(p) and len(p) > best_len:
            best, best_len = p, len(p)
    return best

dev_index = defaultdict(list)
for pname, info in permits.items():
    dev_index[info["developer"]].append(pname)

updated = 0
stats = {"prefix": 0, "developer": 0, "unmatched": 0}

for bldg, owner, parcel, cnt in unmatched:
    matched = None
    for cand in extract_candidates(bldg):
        matched = prefix_match(cand, permits)
        if matched: break

    if matched:
        stats["prefix"] += 1
    elif owner and owner in dev_index:
        devs = dev_index[owner]
        if len(devs) == 1:
            matched = devs[0]
            stats["developer"] += 1

    if matched:
        info = permits[matched]
        if not DRY_RUN:
            conn.execute(
                "UPDATE housing_units SET project_name=?, zone=? WHERE building_name=? AND (project_name IS NULL OR project_name NOT IN (SELECT project_name FROM presale_permits))",
                (matched, info["zone"], bldg)
            )
        updated += cnt
    else:
        stats["unmatched"] += 1

if not DRY_RUN:
    conn.commit()

total = len(unmatched)
matched = total - stats["unmatched"]
print(f"\n结果:")
print(f"  楼栋前缀匹配: {stats['prefix']} ({stats['prefix']/total*100:.1f}%)" if total else "  无数据")
print(f"  开发商匹配:   {stats['developer']}")
print(f"  未匹配:       {stats['unmatched']}")
print(f"  匹配率:       {matched/total*100:.1f}%" if total else "  N/A")
print(f"  受影响房源:   {updated:,}")

# 验证：翰熙典居
print(f"\n验证 翰熙典居:")
rows = conn.execute("SELECT COUNT(*) FROM housing_units WHERE project_name = '翰熙典居'").fetchone()
print(f"  housing_units: {rows[0]} 条")
for r in conn.execute("SELECT building_name, COUNT(*) FROM housing_units WHERE building_name LIKE '%翰熙%' OR building_name LIKE '%熙典%' OR building_name LIKE '%典居%' LIMIT 10").fetchall():
    print(f"  楼栋: {r[0]} ({r[1]}条)")

if DRY_RUN:
    print("\n⚠️ DRY RUN — 未写入数据库。去掉 --dry-run 执行真实更新。")
else:
    print("\n✅ 匹配完成。")

conn.close()
