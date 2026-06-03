# beian_query — 备案价查询 Flask 后端

## 项目概述
微信公众号"备案价查询"的 Flask 后端。用户通过公众号菜单进入 OAuth 登录 → 查询深圳新房备案价 → API 返回数据。
部署在 ruiheqi.cn，与 property_clawer 共享 PG 数据库。

## 关键路径
- 主入口: `app.py` — Flask 应用
- 数据库: PG property_clawer (DB_NAME/DB_USER/DB_PASSWORD 从环境变量读取)
- 模板: `templates/` — Jinja2 HTML 模板
- 同步: `sync/` — 数据同步脚本
- 海报: `poster.py` — 房源信息海报生成

## 常用命令
- 本地开发: `python3 app.py` (Flask debug)
- 部署: scp app.py 到 ruiheqi.cn + 重启 gunicorn
- PG 查询: `psql -h localhost -U sam -d property_clawer`

## API 端点
- `/api/zones` — 区域列表
- `/api/projects` — 按区域查项目
- `/api/buildings` — 按项目查楼栋
- `/api/units` — 按条件查房源
- `/api/stats` — 统计

## 关键约定
- 价格单位: 接口内部用"元"，前端用"万"
- 区域名标准化: "深汕合作" → "深汕"
- 未售房源过期自动排除（check_date 阈值）
