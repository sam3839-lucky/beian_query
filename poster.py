"""生成项目分享海报 (750×1334 PNG)"""
import base64
import io
import os
import hashlib
import time
import requests
from PIL import Image, ImageDraw, ImageFont

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
POSTER_DIR = os.path.join(STATIC_DIR, "posters")
FALLBACK_QR = os.path.join(STATIC_DIR, "qrcode.png")  # 通用小程序码

# 微信小程序配置
WECHAT_APPID = os.environ.get("WECHAT_APPID", "")
WECHAT_SECRET = os.environ.get("WECHAT_SECRET", "")
_token_cache = {"token": None, "expires_at": 0}


def _get_wx_token():
    """获取/刷新微信 access_token"""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 300:
        return _token_cache["token"]
    if not WECHAT_APPID or not WECHAT_SECRET:
        return None
    resp = requests.get(
        "https://api.weixin.qq.com/cgi-bin/token",
        params={"grant_type": "client_credential", "appid": WECHAT_APPID, "secret": WECHAT_SECRET},
        timeout=10
    )
    data = resp.json()
    if "access_token" in data:
        _token_cache["token"] = data["access_token"]
        _token_cache["expires_at"] = now + data.get("expires_in", 7200)
        return _token_cache["token"]
    return None


def _get_project_qr(project_name):
    """获取项目专用小程序码（扫码直达该项目房源）"""
    # 用项目名的 md5 做文件名，避免特殊字符
    key = hashlib.md5(project_name.encode()).hexdigest()[:12]
    qr_path = os.path.join(POSTER_DIR, f"qr_{key}.png")

    # 24h 缓存
    if os.path.exists(qr_path) and (time.time() - os.path.getmtime(qr_path) < 86400):
        return qr_path

    token = _get_wx_token()
    if not token:
        return FALLBACK_QR if os.path.exists(FALLBACK_QR) else None

    # scene 参数：base64url 编码项目名（扫码后在 app.js 解码）
    scene = base64.urlsafe_b64encode(project_name.encode()).decode().rstrip("=")[:32]
    body = {
        "scene": scene,
        "page": "pages/index/index",
        "width": 430,
        "auto_color": False,
        "line_color": {"r": 7, "g": 193, "b": 96},
        "is_hyaline": False,
    }
    resp = requests.post(
        f"https://api.weixin.qq.com/wxa/getwxacodeunlimit?access_token={token}",
        json=body, timeout=15
    )
    if resp.headers.get("Content-Type", "").startswith("image"):
        os.makedirs(POSTER_DIR, exist_ok=True)
        with open(qr_path, "wb") as f:
            f.write(resp.content)
        return qr_path

    return FALLBACK_QR if os.path.exists(FALLBACK_QR) else None

# 中文字体：优先系统字体（.ttc 可渲染），其次自定义字体
_SYSTEM_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
_CUSTOM_FONT = os.path.join(STATIC_DIR, "NotoSansSC-Regular.ttf")
if os.path.exists(_SYSTEM_FONT):
    FONT_PATH = _SYSTEM_FONT
elif os.path.exists(_CUSTOM_FONT):
    FONT_PATH = _CUSTOM_FONT
else:
    FONT_PATH = _CUSTOM_FONT  # 让 truetype 报错，回退到 default

W, H = 750, 1334
BG = "#FFFFFF"
GREEN = "#07C160"
DARK = "#333333"
GRAY = "#888888"
LIGHT_BG = "#F7F7F7"
BORDER = "#E5E5E5"


def _font(size, weight="regular"):
    """加载中文字体，回退到默认"""
    kwargs = {"size": size}
    if FONT_PATH.endswith(".ttc"):
        kwargs["index"] = 2  # NotoSansCJK: index 2 = 简体中文
    try:
        return ImageFont.truetype(FONT_PATH, **kwargs)
    except (OSError, IOError):
        return ImageFont.load_default()


def generate(project_name, zone, unsold, avg_unit, avg_total, price_min, price_max, developer, pass_date):
    """生成海报，返回 PNG bytes"""
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # ── 顶部品牌栏 ──
    draw.text((40, 40), "深圳商品房备案价格查询", fill=GRAY, font=_font(28))

    # 分隔线
    y = 90
    draw.line([(40, y), (W - 40, y)], fill=BORDER, width=1)

    # ── 项目名 ──
    y = 130
    draw.text((40, y), project_name, fill=DARK, font=_font(48, "bold"))

    # 区域标签
    y = 195
    tag_w = draw.textlength(zone, font=_font(26))
    draw.rounded_rectangle([(40, y), (48 + tag_w, y + 36)], radius=6, fill="#E8F8EF")
    draw.text((44, y + 4), zone, fill=GREEN, font=_font(26))

    # ── 三列数据卡 ──
    y = 270
    card_h = 140
    card_w = (W - 96) // 3  # 三列等宽，两边各 40 间距，中间 8+8
    gap = 8

    data = [
        (str(unsold) + " 套", "可售房源"),
        (str(avg_unit) + " 万", "备案均价/㎡"),
        (str(price_min) + "-" + str(price_max), "总价区间/万"),
    ]

    for i, (num_text, label) in enumerate(data):
        x = 40 + i * (card_w + gap)
        # 卡片背景
        draw.rounded_rectangle([(x, y), (x + card_w, y + card_h)], radius=12, fill=LIGHT_BG)
        # 数字
        num_font = _font(34, "bold")
        num_w = draw.textlength(num_text, font=num_font)
        draw.text((x + (card_w - num_w) / 2, y + 28), num_text, fill=DARK, font=num_font)
        # 标签
        label_font = _font(22)
        label_w = draw.textlength(label, font=label_font)
        draw.text((x + (card_w - label_w) / 2, y + 82), label, fill=GRAY, font=label_font)

    # ── 分隔线 ──
    y = 460
    draw.line([(40, y), (W - 40, y)], fill=BORDER, width=1)

    # ── 详细信息 ──
    y = 500
    info_lines = []
    if developer:
        info_lines.append(("开发商", developer))
    info_lines.append(("备案均价", f"{avg_total} 万/套"))
    if pass_date:
        info_lines.append(("最新预售证", pass_date))
    info_lines.append(("数据来源", "深圳市住房和建设局"))

    for label, value in info_lines:
        draw.text((40, y), label, fill=GRAY, font=_font(24))
        val_w = draw.textlength(value, font=_font(24))
        draw.text((W - 40 - val_w, y), value, fill=DARK, font=_font(24))
        y += 52

    # ── 底部小程序码 ──
    qr_size = 220
    qr_x = (W - qr_size) // 2
    qr_y = H - 340

    # 灰色引导区
    draw.rounded_rectangle(
        [(qr_x - 40, qr_y - 30), (qr_x + qr_size + 40, qr_y + qr_size + 80)],
        radius=16, fill=LIGHT_BG
    )

    qr_path = _get_project_qr(project_name)
    try:
        if qr_path and os.path.exists(qr_path):
            qr_img = Image.open(qr_path).convert("RGBA")
        elif os.path.exists(FALLBACK_QR):
            qr_img = Image.open(FALLBACK_QR).convert("RGBA")
        else:
            raise FileNotFoundError
        qr_img = qr_img.resize((qr_size, qr_size), Image.LANCZOS)
        if qr_img.mode == "RGBA":
            img.paste(qr_img, (qr_x, qr_y), qr_img)
        else:
            img.paste(qr_img, (qr_x, qr_y))
    except (OSError, IOError, FileNotFoundError):
        draw.rectangle([(qr_x, qr_y), (qr_x + qr_size, qr_y + qr_size)], outline=BORDER, width=2)
        placeholder = "小程序码"
        pw = draw.textlength(placeholder, font=_font(28))
        draw.text((qr_x + (qr_size - pw) / 2, qr_y + qr_size / 2 - 14), placeholder, fill=GRAY, font=_font(28))

    # 扫码引导文案
    guide = "扫码查看全部可售房源"
    gw = draw.textlength(guide, font=_font(26))
    draw.text(((W - gw) / 2, qr_y + qr_size + 16), guide, fill=GRAY, font=_font(26))

    # ── 底部 slogan ──
    slogan = "查深圳新房备案价，用备案查询"
    sw = draw.textlength(slogan, font=_font(24))
    draw.text(((W - sw) / 2, H - 56), slogan, fill=GREEN, font=_font(24))

    # 输出
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def get_or_generate(project_name, **data):
    """获取缓存或生成新海报，返回 (png_bytes, cache_key)"""
    import hashlib
    key = hashlib.md5(project_name.encode()).hexdigest()[:12]
    cache_path = os.path.join(POSTER_DIR, f"{key}.png")

    # 检查缓存（24h 内直接返回）
    if os.path.exists(cache_path):
        import time
        if time.time() - os.path.getmtime(cache_path) < 86400:
            with open(cache_path, "rb") as f:
                return f.read(), key

    png_bytes = generate(project_name, **data)
    os.makedirs(POSTER_DIR, exist_ok=True)
    with open(cache_path, "wb") as f:
        f.write(png_bytes)
    return png_bytes, key
