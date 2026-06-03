# QA Report — 深圳新房备案查询

**Date:** 2026-05-25
**Target:** `https://ruiheqi.cn` (Flask API + WeChat Mini Program)
**Tier:** Standard (Critical + High + Medium)
**Method:** API endpoint testing (curl) + frontend static code review (WXML/JS/WXSS)
**Note:** WeChat Mini Program cannot be tested in a browser. Frontend verified via code review of data bindings and type compatibility against API responses.

---

## Health Score

| Category | Weight | Score | Notes |
|----------|--------|-------|-------|
| Console | 15% | N/A | Mini program — no browser console |
| Links | 10% | N/A | No HTML links |
| Visual | 10% | N/A | Cannot render mini program |
| Functional | 20% | 92 | 2 medium bugs found + fixed |
| UX | 15% | 92 | 1 UX observation (deferred) |
| Performance | 10% | 100 | API responses <200ms |
| Content | 5% | 100 | Data accurate and consistent |
| Accessibility | 15% | N/A | Mini program native |

**Adjusted Score:** 94/100 (weighted average on testable categories)

---

## Issues Found

### ISSUE-001 — avg_total/avg_unit 类型为字符串而非数字 (Medium) ✅ FIXED

- **Severity:** Medium
- **Category:** Functional
- **Fix Status:** Verified — commit `70f92d7`
- **Root Cause:** PG `ROUND()::numeric` 返回 Python `Decimal` 类型，Flask jsonify 将其序列化为字符串
- **API:** `/api/overview`
- **Impact:** WXML 显示正常（字符串插值），但未来 JS 运算会出错 (`"514.4" + 100 = "514.4100"`)
- **Fix:** 在返回前用 `float()` 显式转换
- **Evidence:**
  - Before: `"avg_total": "514.4"` (str)
  - After: `"avg_total": 514.4` (float)

### ISSUE-002 — 数据完整度硬编码为 100% (Medium) ✅ FIXED

- **Severity:** Medium
- **Category:** Functional / Content
- **Fix Status:** Verified — commit `70f92d7`
- **Root Cause:** `completeness` 字段硬编码为 `100`，未按当月天数计算
- **API:** `/api/transactions/summary`
- **Impact:** 5月25日显示完整度 81%（25/31天），而非误导性的 100%
- **Fix:** 当月按 `day_of_month / days_in_month * 100` 计算；过往月份保持 100%

### ISSUE-003 — 月度走势柱状图基线为最旧月份 (Low) ⏸️ DEFERRED

- **Severity:** Low
- **Category:** UX
- **Location:** `trends.wxml:96-97`
- **Finding:** `trends[trends.length-1].total` 以最旧月份为 100% 基线，可能导致新月份柱子超过 100%
- **Impact:** 视觉上不太直观，但功能正常
- **Recommendation:** 考虑改为以最大值 (`Math.max(...)`) 为基线

---

## API Endpoint Verification (12/12 passing)

| Endpoint | Status | Data Issues |
|----------|--------|-------------|
| `/api/overview` | 200 | ISSUE-001 fixed |
| `/api/rankings` | 200 | Clean |
| `/api/latest-permits` | 200 | Clean |
| `/api/transactions/summary` | 200 | ISSUE-002 fixed |
| `/api/transactions/trends` | 200 | Clean |
| `/api/transactions/districts` | 200 | Clean |
| `/api/transactions/recent` | 200 | Clean (date format fixed) |
| `/api/admin/status` | 200 | Clean |
| `/api/zones` | 200 | Clean |
| `/api/projects` | 200 | Clean |
| `/api/units` | 200 | Clean |
| `/api/stats` | 200 | Clean |

---

## Data Integrity Checks

- `overview.total` = `signed + filed + transferred + unsold` → 197,331 = 197,331 ✅
- Zone unsold sum + empty-zone = total unsold → 21,735 + 2,200 = 23,935 ✅
- Admin total records: 384,564, residential: 202,636 ✅
- Transaction May 2026: new 3,991 + used 8,420 = 12,411 total ✅
- Prices: units API returns `total_price` in 万, `unit_price` in 元/㎡ as expected by frontend ✅

---

## Frontend Code Review Findings

### Verified Compatible
- `index.js` zones handling: API returns `string[]`, frontend spreads correctly ✅
- `index.js` projects handling: API returns `string[]`, mapping to `{name, value}` objects works ✅
- `home.js` rankings math: `(item.unit_price / 10000).toFixed(1)` — `unit_price` is float in API ✅
- `detail.js` URL params: all 7 fields parsed with `parseFloat`/`decodeURIComponent` ✅
- `detail.js` mortgage: LPR rate sync with `app.globalData.mortgageRate` ✅
- `trends.js` donut: guards against zero total (`t <= 0` early return) ✅
- Error handling: all three pages have retry states for failed API calls ✅

### Observations (no fix needed)
- `home.js` calculator hardcodes 30% down, detail page allows 15% — UX inconsistency between calculators
- `completeness` label doesn't explain the metric (users may not know what 81% means)

---

## Edge Cases Tested

- `GET /api/projects` without params → 200, empty projects list ✅
- `GET /api/projects?zone=NOTEXIST` → 200, empty list ✅
- `GET /api/transactions/trends?months=999` → 200, returns all available months (65) ✅
- `GET /api/transactions/recent?days=abc` → 200, graceful (type coercion defaults to 30) ✅
- `POST /api/overview` → 405 (correct — only GET is defined) ✅

---

## Summary

- **Issues found:** 3 (2 fixed, 1 deferred)
- **Health score:** 94/100
- **Commits:** `70f92d7` — fix(qa): ISSUE-001 + ISSUE-002
- **Top 3 things to fix (deferred):**
  1. 柱状图基线改为最大值（低优先级 UX）
  2. 首页计算器首付比例 30% 硬编码，与详情页 15% 不一致
  3. 完整度指标可加说明文案
