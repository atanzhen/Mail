# -*- coding: utf-8 -*-
# ============================================================================
#  TempMail 临时邮箱服务  v2.3
#  Copyright (C) 2026 atusu.cn. 保留所有权利 / All rights reserved.
#  开发者 (Developer): https://atusu.cn
#  许可 (License)   : 开源版 · 禁止商用 (Non-commercial use only)
#                     严禁商业售卖 / 付费分发 / 二次销售，详见 LICENSE 文件
# ============================================================================
import os, re, sys, json, socket, base64, secrets, sqlite3, threading, time, platform, urllib.request
import hashlib, uuid
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr

from flask import Flask, request, jsonify, render_template_string, make_response
from aiosmtpd.controller import Controller

BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
# 二进制/容器环境下保证中文与 emoji 输出不因终端编码报错
try:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DB_FILE = os.getenv("DB_FILE", os.path.join(BASE_DIR, "temp_mail.db"))
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "6060"))
SMTP_HOST = os.getenv("SMTP_HOST", "0.0.0.0")
SMTP_PORT = int(os.getenv("SMTP_PORT", "25"))
MAX_EMAIL_SIZE = int(os.getenv("MAX_EMAIL_SIZE", str(10 * 1024 * 1024)))
GITHUB_URL = os.getenv("GITHUB_URL", "https://github.com/atanzhen/Mail")
# 页脚 / 按钮统一指向项目主页
SITE_URL = os.getenv("SITE_URL", "https://atusu.cn/mail")
FRONT_RETENTION_MINUTES = int(os.getenv("FRONT_RETENTION_MINUTES", "10"))
BACKEND_RETENTION_DAYS = int(os.getenv("BACKEND_RETENTION_DAYS", "7"))
MAILBOX_VALIDITY_HOURS = int(os.getenv("MAILBOX_VALIDITY_HOURS", "24"))
CLEAN_ON_START = os.getenv("CLEAN_ON_START", "false").lower() in ("true", "1", "yes")
APP_VERSION = "2.3.0"
APP_NAME = "TempMail"

# 版权与授权声明（开源版 · 禁止商用）
DEVELOPER_NAME = os.getenv("DEVELOPER_NAME", "atusu.cn")
DEVELOPER_URL = os.getenv("DEVELOPER_URL", "https://atusu.cn")
DEVELOPER_LINK = os.getenv("DEVELOPER_LINK", "atusu.cn/mail")
LICENSE_TITLE = "开源版 · 禁止商用"
COPYRIGHT = "Copyright (C) 2026 atusu.cn"

# 部署上报（每次新部署上报一次，全程后台静默，不在日志/前端出现）
REPORT_URL = os.getenv("DEPLOY_REPORT_URL", "http://ac.atusu.cn/mail.php")
# 上报失败后的重试间隔（秒）：默认 10 分钟
REPORT_RETRY_INTERVAL = int(os.getenv("DEPLOY_REPORT_RETRY", "600"))

db_lock = threading.RLock()
_report_lock = threading.Lock()
smtp_controller = None

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))


class SimpleProxyFix:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        if 'HTTP_X_FORWARDED_FOR' in environ:
            environ['REMOTE_ADDR'] = environ['HTTP_X_FORWARDED_FOR'].split(',')[0].strip()
        if 'HTTP_X_FORWARDED_PROTO' in environ:
            environ['wsgi.url_scheme'] = environ['HTTP_X_FORWARDED_PROTO'].strip()
        if 'HTTP_X_FORWARDED_HOST' in environ:
            environ['HTTP_HOST'] = environ['HTTP_X_FORWARDED_HOST'].strip()
        return self.wsgi_app(environ, start_response)


app.wsgi_app = SimpleProxyFix(app.wsgi_app)


# ---------------- 端口检测与自动递增 ----------------

def ensure_free_port(host, port, max_try=100):
    """检测端口是否被占用，被占用则自动递增换一个可用端口。

    返回 (实际端口, 是否发生了切换)。部署脚本在启动后读取运行时端口文件，
    即可把 .env 与反代配置同步到真正监听的那个端口。
    """
    start = port
    for _ in range(max_try):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                # 不设置 SO_REUSEADDR, 确保能真实检测到端口被占用
                s.bind((host, port))
                return port, port != start
        except OSError:
            print(f"[Port] ⚠️ 端口 {port} 已被占用，尝试 {port + 1} ...")
            port += 1
    return port, port != start


def runtime_port_path():
    """运行时端口文件：部署脚本据此拿到程序真正监听的端口"""
    return os.path.join(os.path.dirname(DB_FILE) or BASE_DIR, ".runtime_port")


def write_runtime_port(port):
    path = runtime_port_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(port))
    except Exception:
        pass


# ---------------- 数据库 ----------------

def db_connect():
    db_dir = os.path.dirname(DB_FILE)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    db = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode=WAL;")
        db.execute("PRAGMA synchronous=NORMAL;")
        db.execute("PRAGMA busy_timeout=30000;")
    except Exception:
        pass
    return db


def init_db():
    with db_lock:
        with db_connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS mailboxes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, client_token TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, last_received_at TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT, mailbox_email TEXT NOT NULL, sender TEXT, recipients TEXT,
                subject TEXT, text_body TEXT, html_body TEXT, attachments TEXT, raw_size INTEGER DEFAULT 0, received_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS email_reads (
                client_token TEXT NOT NULL, email_id INTEGER NOT NULL, read_at TEXT NOT NULL, PRIMARY KEY(client_token, email_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_mb_token ON mailboxes(client_token)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_mb_email ON mailboxes(email)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_mb_active ON mailboxes(active)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_em_mb ON emails(mailbox_email)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_em_received ON emails(received_at)")


def clean_all_data():
    with db_lock:
        with db_connect() as db:
            db.execute("DELETE FROM email_reads")
            db.execute("DELETE FROM emails")
            db.execute("DELETE FROM mailboxes")
    print("[Clean] All old data cleared")


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def generate_prefix():
    return re.sub(r"[^a-zA-Z0-9]", "", secrets.token_urlsafe(12))[:10].lower()


def get_client_token():
    token = request.cookies.get("client_token")
    if not token or len(token) < 32:
        token = secrets.token_urlsafe(32)
    return token


def set_client_cookie(response, token):
    response.set_cookie("client_token", token, max_age=60 * 60 * 24 * 365, httponly=True, samesite="Lax", path="/")
    return response


def get_client_mailbox(token, domain):
    cutoff = (datetime.now() - timedelta(hours=MAILBOX_VALIDITY_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    with db_connect() as db:
        return db.execute(
            "SELECT * FROM mailboxes WHERE client_token = ? AND email LIKE ? AND active = 1 AND created_at >= ? ORDER BY id DESC LIMIT 1",
            (token, f"%@{domain}", cutoff)
        ).fetchone()


def deactivate_expired_mailboxes():
    cutoff = (datetime.now() - timedelta(hours=MAILBOX_VALIDITY_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        with db_connect() as db:
            db.execute("UPDATE mailboxes SET active = 0 WHERE active = 1 AND created_at < ?", (cutoff,))


def check_email_available(email):
    with db_connect() as db:
        row = db.execute(
            "SELECT client_token, created_at FROM mailboxes WHERE email = ? AND active = 1",
            (email,)
        ).fetchone()
    if not row:
        return True, None
    try:
        created = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() - created > timedelta(hours=MAILBOX_VALIDITY_HOURS):
            return True, None
    except Exception:
        pass
    return False, {"owner_token": row["client_token"]}


def create_mailbox(token, domain, custom_prefix=None):
    with db_lock:
        with db_connect() as db:
            db.execute("UPDATE mailboxes SET active = 0 WHERE client_token = ?", (token,))
            if custom_prefix:
                email = f"{custom_prefix}@{domain}"
                existing = db.execute(
                    "SELECT client_token, created_at FROM mailboxes WHERE email = ? AND active = 1",
                    (email,)
                ).fetchone()
                if existing:
                    try:
                        created = datetime.strptime(existing["created_at"], "%Y-%m-%d %H:%M:%S")
                        still_valid = (datetime.now() - created) <= timedelta(hours=MAILBOX_VALIDITY_HOURS)
                    except Exception:
                        still_valid = True
                    if still_valid and existing["client_token"] != token:
                        raise ValueError("CONFLICT")
                    if still_valid and existing["client_token"] == token:
                        db.execute("UPDATE mailboxes SET active = 1, created_at = ? WHERE email = ?", (now_text(), email))
                        return email
                    db.execute("UPDATE mailboxes SET active = 0 WHERE email = ?", (email,))
                inactive = db.execute("SELECT id FROM mailboxes WHERE email = ?", (email,)).fetchone()
                if inactive:
                    db.execute(
                        "UPDATE mailboxes SET client_token = ?, active = 1, created_at = ?, last_received_at = NULL WHERE email = ?",
                        (token, now_text(), email)
                    )
                else:
                    db.execute(
                        "INSERT INTO mailboxes(email, client_token, active, created_at) VALUES (?, ?, 1, ?)",
                        (email, token, now_text())
                    )
                return email
            else:
                email = ""
                for _ in range(50):
                    prefix = generate_prefix()
                    candidate = f"{prefix}@{domain}"
                    if not db.execute("SELECT id FROM mailboxes WHERE email = ? AND active = 1", (candidate,)).fetchone():
                        email = candidate
                        break
                if not email:
                    raise RuntimeError("Failed to generate mailbox")
                db.execute(
                    "INSERT INTO mailboxes(email, client_token, active, created_at) VALUES (?, ?, 1, ?)",
                    (email, token, now_text())
                )
                return email


# ---------------- 公网 IP 获取（本机网卡优先 + 多接口冗余 + 缓存） ----------------

# 支持用环境变量强制指定（多网卡 / NAT 环境最稳妥）
PUBLIC_IP_OVERRIDE = os.getenv("PUBLIC_IP", "").strip()

# 顺序按「国内可直连 / 响应快」优先，探测时并发执行，谁先返回用谁
PUBLIC_IP_ENDPOINTS = [
    "http://ip.3322.net",
    "https://myip.ipip.net",
    "http://members.3322.org/dyndns/getip",
    "https://ddns.oray.com/checkip",
    "https://ipv4.icanhazip.com",
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://checkip.amazonaws.com",
    "https://ipinfo.io/ip",
    "https://api.ip.sb/ip",
    "https://api-ipv4.ip.sb/ip",
    "https://ipv4.ident.me",
    "https://4.ident.me",
    "https://www.trackip.net/ip",
]

_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _is_ipv4(text):
    if not text:
        return False
    text = str(text).strip()
    if not _IP_RE.match(text):
        return False
    return all(0 <= int(p) <= 255 for p in text.split("."))


def is_public_ip(ip):
    """判断是否为公网 IPv4（排除内网/保留/回环/运营商 NAT 地址）"""
    if not _is_ipv4(ip):
        return False
    a, b = [int(x) for x in ip.split(".")[:2]]
    if a in (0, 10, 127):
        return False
    if a == 172 and 16 <= b <= 31:
        return False
    if a == 192 and b == 168:
        return False
    if a == 169 and b == 254:
        return False
    if a == 100 and 64 <= b <= 127:     # 100.64/10 运营商级 NAT
        return False
    if a == 198 and b in (18, 19):
        return False
    if a >= 224:                        # 组播 / 保留
        return False
    return True


def get_local_ips():
    """读取本机网卡 IP：UDP 出口探测 + 主机名解析（不产生真实流量）"""
    ips = []
    for probe in ("8.8.8.8", "114.114.114.114", "1.1.1.1", "223.5.5.5"):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.8)
            s.connect((probe, 80))
            ips.append(s.getsockname()[0])
        except Exception:
            pass
        finally:
            try:
                if s:
                    s.close()
            except Exception:
                pass
    try:
        hostname = socket.gethostname()
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ips.append(item[4][0])
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.append(ip)
    except Exception:
        pass
    seen, result = set(), []
    for ip in ips:
        if _is_ipv4(ip) and ip not in seen:
            seen.add(ip)
            result.append(ip)
    return result


def get_local_public_ip():
    """本机网卡上如果直接就是公网 IP，优先返回它"""
    for ip in get_local_ips():
        if is_public_ip(ip):
            return ip
    return ""


_ip_cache = {"ip": "", "ts": 0.0}
_ip_lock = threading.Lock()
IP_CACHE_SECONDS = 600
_region_cache = {}


# 系统代理（HTTP_PROXY / 注册表）经常把免费 IP 接口和回传接口拦成 502，
# 这里统一「直连优先，失败再走系统代理」，保证公网 IP 探测与上报成功率。
_OPENER_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_OPENER_DEFAULT = urllib.request.build_opener()


def _open_url(req, timeout=4):
    """直连优先；直连异常时回退到系统代理配置"""
    try:
        return _OPENER_DIRECT.open(req, timeout=timeout)
    except Exception:
        return _OPENER_DEFAULT.open(req, timeout=timeout)


def _http_text(url, timeout=4):
    req = urllib.request.Request(
        url, headers={"User-Agent": "curl/7.68.0", "Accept": "*/*"}
    )
    resp = _open_url(req, timeout=timeout)
    try:
        return resp.read().decode("utf-8", "ignore")
    finally:
        try:
            resp.close()
        except Exception:
            pass


def _extract_public_ipv4(text):
    """从各种返回格式里提取公网 IPv4（支持 JSON / 纯文本 / '当前 IP：x.x.x.x'）"""
    for token in re.findall(r"\d{1,3}(?:\.\d{1,3}){3}", text or ""):
        if is_public_ip(token):
            return token
    return ""


def _probe_ip_endpoints(budget=6):
    """并发请求全部免费接口，谁先返回有效公网 IPv4 就用谁。

    单个接口慢 / 被墙不会拖垮整体（这是之前串行探测最容易踩的坑）。
    """
    urls = list(PUBLIC_IP_ENDPOINTS)
    if not urls:
        return ""

    found = {"ip": ""}
    lock = threading.Lock()
    done = threading.Event()

    def worker(url):
        if done.is_set():
            return
        try:
            ip = _extract_public_ipv4(_http_text(url, timeout=3))
        except Exception:
            return
        if ip:
            with lock:
                if not found["ip"]:
                    found["ip"] = ip
            done.set()

    for url in urls:
        try:
            threading.Thread(target=worker, args=(url,), daemon=True).start()
        except Exception:
            continue

    deadline = time.monotonic() + max(1.0, float(budget))
    while time.monotonic() < deadline and not found["ip"]:
        time.sleep(0.05)
    return found["ip"]


def get_public_ip(force=False, budget=8):
    """获取服务器公网 IP：手动指定 > 本机网卡 > 多免费接口（多轮）> 历史记录。带缓存。

    budget 为本次探测的总时间预算（秒），避免页面请求被长时间阻塞。
    """
    if is_public_ip(PUBLIC_IP_OVERRIDE):
        return PUBLIC_IP_OVERRIDE

    now = time.time()
    with _ip_lock:
        if not force and _ip_cache["ip"] and (now - _ip_cache["ts"]) < IP_CACHE_SECONDS:
            return _ip_cache["ip"]

    ip = get_local_public_ip()

    if not ip:
        ip = _probe_ip_endpoints(budget=budget)
    if not ip:
        # 再给一轮更长的预算（网络抖动 / DNS 慢时兜底）
        ip = _probe_ip_endpoints(budget=max(4, int(budget) + 4))

    if not ip:
        ip = _load_report_state().get("ip", "")
        if not is_public_ip(ip):
            ip = ""

    if ip:
        with _ip_lock:
            _ip_cache["ip"] = ip
            _ip_cache["ts"] = now
    return ip


def get_system_info():
    try:
        return "{0} {1} ({2}) / Python {3}".format(
            platform.system(), platform.release(), platform.machine(), platform.python_version()
        )
    except Exception:
        return "unknown"


def get_region(ip):
    """根据公网 IP 获取地区信息（带缓存，失败返回空串，多接口冗余）"""
    if not is_public_ip(ip):
        return ""
    if ip in _region_cache:
        return _region_cache[ip]
    for url in (
        "http://ip-api.com/json/{0}?lang=zh-CN&fields=status,country,regionName,city,isp".format(ip),
        "https://ipapi.co/{0}/json/".format(ip),
        "https://ipinfo.io/{0}/json".format(ip),
    ):
        try:
            data = json.loads(_http_text(url, timeout=5))
            if isinstance(data, dict):
                parts = [str(data.get(k)) for k in ("country", "regionName", "region", "city", "isp", "org") if data.get(k)]
                if parts:
                    region = " ".join(parts[:4])
                    _region_cache[ip] = region
                    return region
        except Exception:
            continue
    _region_cache[ip] = ""
    return ""


# ============================================================================
#  主域名配置（反代白名单）
#  - 只允许「已配置且解析指向本机」的域名访问程序页面，禁止他人随意反代
#  - 配置持久化在 config.json，写入后立即生效
# ============================================================================

CONFIG_FILE = os.getenv("CONFIG_FILE", os.path.join(os.path.dirname(DB_FILE) or BASE_DIR, "config.json"))
PRIMARY_DOMAIN_ENV = os.getenv("PRIMARY_DOMAIN", "").strip().lower()

_cfg_lock = threading.RLock()
_cfg = {"loaded": False, "domain": "", "active": False, "configured_at": "",
        "verified_at": "", "ips": [], "source": "file"}

# 配置变更 / 重试节拍共用的事件（域名配置成功时立即唤醒上报线程）
_domain_ready = threading.Event()


def _normalize_domain(value):
    """把用户输入整理成裸域名：去掉协议、端口、路径、末尾点、通配前缀"""
    v = (value or "").strip().lower()
    v = re.sub(r"^[a-z]+://", "", v)
    v = v.split("/")[0].split("?")[0].split("#")[0]
    v = v.split("@")[-1]
    v = v.split(":")[0]
    v = v.strip(".")
    if v.startswith("*."):
        v = v[2:]
    return v


def _valid_domain(v):
    if not v or len(v) > 253 or "." not in v:
        return False
    return bool(re.match(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?)+$", v))


def _config_mtime():
    try:
        return os.path.getmtime(CONFIG_FILE)
    except Exception:
        return 0.0


_cfg_mtime = 0.0


def load_config():
    """读取配置；文件被手工修改时自动重新加载（无需重启）"""
    global _cfg_mtime
    with _cfg_lock:
        mtime = _config_mtime()
        if not _cfg["loaded"] or mtime != _cfg_mtime:
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _cfg["domain"] = _normalize_domain(data.get("domain") or "")
                    _cfg["active"] = bool(data.get("active"))
                    _cfg["configured_at"] = data.get("configured_at") or ""
                    _cfg["verified_at"] = data.get("verified_at") or ""
                    _cfg["ips"] = data.get("ips") or []
            except Exception:
                if not _cfg["loaded"]:
                    _cfg["domain"] = ""
                    _cfg["active"] = False
            _cfg["loaded"] = True
            _cfg_mtime = mtime
        snap = dict(_cfg)
        snap.pop("loaded", None)
        return snap


def save_config(**kwargs):
    global _cfg_mtime
    with _cfg_lock:
        load_config()
        _cfg.update(kwargs)
        _cfg["loaded"] = True
        try:
            d = os.path.dirname(CONFIG_FILE)
            if d:
                os.makedirs(d, exist_ok=True)
            payload = {k: v for k, v in _cfg.items() if k != "loaded"}
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_FILE)
            try:
                os.chmod(CONFIG_FILE, 0o644)
            except Exception:
                pass
            _cfg_mtime = _config_mtime()
        except Exception:
            pass
        snap = dict(_cfg)
        snap.pop("loaded", None)
        return snap


def get_primary_domain():
    """主域名：环境变量 PRIMARY_DOMAIN 优先，其次配置文件"""
    if PRIMARY_DOMAIN_ENV:
        return PRIMARY_DOMAIN_ENV
    return (load_config().get("domain") or "").strip().lower()


def is_domain_active():
    if PRIMARY_DOMAIN_ENV:
        return True
    cfg = load_config()
    return bool(cfg.get("domain")) and bool(cfg.get("active"))


def _dns_ipv4(host):
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        return sorted({item[4][0] for item in infos})
    except Exception:
        return []


def check_domain_resolution(domain):
    """校验域名是否解析到本机，返回 (ok, ips, reason)

    判定顺序：
      1. 解析结果包含本机公网 IP        -> 通过
      2. 解析结果包含本机内网网卡 IP     -> 通过（内网/自建 DNS 场景）
      3. 本机公网 IP 未知               -> 只要解析到 A 记录即放行（无法比对）
    """
    domain = _normalize_domain(domain)
    if not domain:
        return False, [], "域名为空"
    ips = _dns_ipv4(domain)
    if not ips:
        return False, [], "未解析到 A 记录（DNS 可能还未生效）"
    public_ip = get_public_ip(budget=4)
    local_ips = set(get_local_ips())
    if public_ip and public_ip in ips:
        return True, ips, ""
    if any(ip in local_ips for ip in ips):
        return True, ips, "解析到本机内网地址"
    if not public_ip:
        return True, ips, "本机公网 IP 未获取到，已按解析结果放行"
    return False, ips, "解析结果与本机公网 IP 不一致（本机 %s）" % public_ip


_res_cache = {}
_res_lock = threading.Lock()


def resolution_cached(domain, ttl=60):
    """带缓存的解析校验（避免每次请求都打 DNS）"""
    domain = _normalize_domain(domain)
    now = time.time()
    with _res_lock:
        hit = _res_cache.get(domain)
        if hit and now - hit[0] < ttl:
            return hit[1], hit[2], hit[3]
    ok, ips, reason = check_domain_resolution(domain)
    with _res_lock:
        _res_cache[domain] = (now, ok, ips, reason)
    return ok, ips, reason


def setup_target_url(domain):
    """配置成功后的跳转地址：域名已由反向代理接入，一律不带端口"""
    domain = _normalize_domain(domain)
    if not domain:
        return ""
    return "http://%s/" % domain


def dns_plan(domain):
    """按主域名推导 DNS 记录（A 记录主机记录 + MX 记录值），用于检测页自动匹配"""
    d = _normalize_domain(domain)
    if not d:
        return {"a_host": "mail", "mx_value": "mail.你的域名",
                "hint": "配置主域名后，这里会自动匹配对应的 A / MX 记录值"}
    labels = d.split(".")
    if len(labels) >= 3:
        return {"a_host": labels[0], "mx_value": d, "hint": ""}
    return {"a_host": "@", "mx_value": d,
            "hint": "当前使用的是裸域名，A 记录用 @ 指向本机 IP；建议改用二级域名（如 mail.%s）更规范" % d}


def resolve_mode():
    """当前请求的页面模式

    setup   : 通过 IP 访问，或尚未配置主域名 -> 配置引导页
    preview : IP 访问 + ?preview=1 -> 程序界面预览（只读）
    normal  : 通过已配置的主域名访问 -> 正常邮箱页面
    blocked : 其它域名反代过来 -> 未授权页
    """
    host, is_ip = get_current_domain()
    domain = get_primary_domain()
    preview = str(request.args.get("preview", "")).lower() in ("1", "true", "yes")
    if is_ip:
        return ("preview" if preview else "setup"), host, is_ip, domain
    if not domain:
        # 未配置主域名时：只有解析到本机的域名才允许打开配置页，
        # 其它解析到别处的域名一律按未授权处理（禁止随意反代）
        ok, _ips, _reason = resolution_cached(host)
        return ("setup" if ok else "blocked"), host, is_ip, domain
    if host == domain:
        return "normal", host, is_ip, domain
    return "blocked", host, is_ip, domain


# ============================================================================
#  部署信息上报（每次部署自动回传，后台静默，失败每 10 分钟重试直到成功）
# ============================================================================

def _report_state_path():
    return os.path.join(os.path.dirname(DB_FILE) or BASE_DIR, ".deploy_report.json")


def _deploy_marker_path():
    return os.path.join(os.path.dirname(DB_FILE) or BASE_DIR, ".deploy_marker")


_fp_cache = {"v": ""}


def _fingerprint_path():
    return os.path.join(os.path.dirname(DB_FILE) or BASE_DIR, ".server_fingerprint")


def get_fingerprint(force=False):
    """服务器指纹：主机名 / 网卡 / 系统 / 机器 ID 的哈希，落盘保存，配置域名时确保已生成"""
    if _fp_cache["v"] and not force:
        return _fp_cache["v"]
    path = _fingerprint_path()
    if not force:
        try:
            with open(path, "r", encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                _fp_cache["v"] = value
                return value
        except Exception:
            pass
    raw = "|".join([
        socket.gethostname() or "",
        str(uuid.getnode()),
        platform.system(), platform.machine(), platform.release(),
        BASE_DIR,
    ])
    for mid_path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(mid_path, "r", encoding="utf-8") as f:
                mid = f.read().strip()
            if mid:
                raw += "|" + mid
                break
        except Exception:
            pass
    fp = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]
    _fp_cache["v"] = fp
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(fp)
        try:
            os.chmod(path, 0o644)
        except Exception:
            pass
    except Exception:
        pass
    return fp


def _read_deploy_marker():
    try:
        with open(_deploy_marker_path(), "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _marker_epoch(marker):
    """部署标记格式: <epoch>-<hostname>-<random>"""
    try:
        return int(str(marker).split("-")[0])
    except Exception:
        return 0


def _load_report_state():
    try:
        with open(_report_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_report_state(state):
    try:
        d = os.path.dirname(_report_state_path())
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = _report_state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _report_state_path())
    except Exception:
        pass


def _post_report(payload):
    """发送上报数据，HTTP 2xx 且 ok!=false 视为成功；失败静默返回 False"""
    try:
        req = urllib.request.Request(
            REPORT_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "TempMail/{0}".format(APP_VERSION),
                "X-TempMail-Report": str(payload.get("type", "deploy")),
            },
            method="POST",
        )
        with _open_url(req, timeout=15) as resp:
            body = resp.read(2048).decode("utf-8", "replace")
            code = int(getattr(resp, "status", 0) or resp.getcode())
            if not (200 <= code < 300):
                return False
            try:
                data = json.loads(body)
                if isinstance(data, dict) and data.get("ok") is False:
                    return False
            except Exception:
                pass
            return True
    except Exception:
        return False


def _new_report_state(marker_id, deploy_epoch):
    return {
        "report_id": marker_id,
        "deploy_epoch": deploy_epoch,
        "deploy_time": datetime.fromtimestamp(deploy_epoch).strftime("%Y-%m-%d %H:%M:%S")
        if deploy_epoch else now_text(),
        "sent": False,
        "domain_sent": "",
        "attempts": 0,
        "last_attempt": "",
        "ok_at": "",
    }


def _build_report_payload(kind, state, domain):
    """上报体：服务器 IP、部署时间、域名为必带字段"""
    ip = state.get("ip") or ""
    if not ip:
        ip = get_public_ip(force=True, budget=12) or ""
        if ip:
            state["ip"] = ip
    return {
        "type": kind,                       # deploy | domain（配置域名后追加）
        "report_id": state.get("report_id", ""),
        "app": APP_NAME,
        "version": APP_VERSION,
        "ip": ip or "unknown",              # 必报：服务器公网 IP
        "domain": domain or "",             # 必报：访问域名（未配置时由 domain 事件补齐）
        "fingerprint": get_fingerprint(),   # 服务器指纹（首次配置域名时生成并保存）
        "deploy_time": state.get("deploy_time") or now_text(),   # 必报：部署时间
        "deploy_epoch": state.get("deploy_epoch", 0),
        "region": get_region(ip) if ip else "",
        "os": get_system_info(),
        "arch": platform.machine(),
        "hostname": socket.gethostname(),
        "install_dir": BASE_DIR,
        "server_time": now_text(),
    }


def _deliver(kind, state, domain):
    """单次投递；成功返回 True 并记录状态"""
    payload = _build_report_payload(kind, state, domain)
    state["attempts"] = int(state.get("attempts", 0)) + 1
    state["last_attempt"] = now_text()
    # IP 是必报字段：前几次探测不到就等下一轮重试，连续多次仍失败才带 unknown 上报
    if payload.get("ip", "unknown") == "unknown" and state["attempts"] < 6:
        return False
    if _post_report(payload):
        state["ok_at"] = now_text()
        if kind == "deploy":
            state["sent"] = True
            state["domain_sent"] = domain or ""
        else:
            state["domain_sent"] = domain or ""
        return True
    return False


def report_worker():
    """后台上报线程（无任何输出）

    - 每次部署（部署脚本写入新的 .deploy_marker）都会上报一次
    - 首次失败后每隔 REPORT_RETRY_INTERVAL（默认 600 秒）重试，直到成功
    - 主域名后配置完成时，会补报一条 domain 事件（IP + 部署时间 + 域名）
    """
    if not REPORT_URL:
        return  # 显式留空即关闭部署回传

    state = _load_report_state()

    while True:
        try:
            marker = _read_deploy_marker()
            if not marker:
                # 非脚本启动（双击 exe / 直接跑二进制 / docker 手工起）也要上报：
                # 首次运行动态生成一个部署标记，等价于「本次部署时刻」
                marker = "{0}-{1}-{2}".format(
                    int(time.time()), socket.gethostname() or "host", secrets.randbelow(10 ** 8)
                )
                try:
                    d = os.path.dirname(_deploy_marker_path())
                    if d:
                        os.makedirs(d, exist_ok=True)
                    with open(_deploy_marker_path(), "w", encoding="utf-8") as f:
                        f.write(marker)
                except Exception:
                    pass
            marker_id = marker or ""
            epoch = _marker_epoch(marker)

            if marker_id and state.get("report_id") != marker_id:
                # 新部署：重置状态，重新上报
                state = _new_report_state(marker_id, epoch)

            domain = get_primary_domain()

            if not state.get("sent"):
                if _deliver("deploy", state, domain):
                    _save_report_state(state)
                else:
                    _save_report_state(state)
                    _domain_ready.wait(REPORT_RETRY_INTERVAL)
                    _domain_ready.clear()
                    continue
            elif domain and state.get("domain_sent") != domain:
                if _deliver("domain", state, domain):
                    _save_report_state(state)
                else:
                    _save_report_state(state)
                    _domain_ready.wait(REPORT_RETRY_INTERVAL)
                    _domain_ready.clear()
                    continue

            _save_report_state(state)
            _domain_ready.wait(REPORT_RETRY_INTERVAL)
            _domain_ready.clear()
        except Exception:
            # 任何异常都不影响主服务，也不输出
            time.sleep(REPORT_RETRY_INTERVAL)


def maybe_report_deployment():
    """启动入口：探测到新部署后唤醒上报线程"""
    marker = _read_deploy_marker()
    state = _load_report_state()
    if marker and state.get("report_id") == marker and state.get("sent"):
        # 已上报过，但仍需在后台守护域名补报
        pass
    _domain_ready.set()


def maybe_report_domain(domain):
    """域名访问 / 配置完成时触发一次补报（每个域名只报一次，失败自动重试）"""
    if not domain:
        return
    _domain_ready.set()


# ---------------- 邮件解析 ----------------

def extract_body(message):
    text_parts, html_parts, cid_map, attachments = [], [], {}, []
    for part in message.walk():
        if part.is_multipart():
            continue
        ct = part.get_content_type()
        disp = str(part.get("Content-Disposition", "")).lower()
        cid = str(part.get("Content-ID", "")).strip("<> ")
        fname = part.get_filename()
        is_img = ct.startswith("image/")
        is_inline = "inline" in disp
        if is_img and (cid or is_inline) and "attachment" not in disp:
            payload = part.get_payload(decode=True) or b""
            if payload:
                cid_map[cid or fname or str(len(cid_map))] = f"data:{ct};base64,{base64.b64encode(payload).decode('ascii')}"
            continue
        if "attachment" in disp or (fname and not (is_img and is_inline)):
            payload = part.get_payload(decode=True) or b""
            if payload and len(payload) <= 5 * 1024 * 1024:
                attachments.append({"filename": fname or "attachment", "data_uri": f"data:{ct};base64,{base64.b64encode(payload).decode('ascii')}"})
            elif payload:
                attachments.append({"filename": fname or "attachment", "data_uri": "", "error": "文件过大"})
            continue
        try:
            content = part.get_content()
        except Exception:
            content = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", errors="replace")
        if ct == "text/plain":
            text_parts.append(str(content))
        elif ct == "text/html":
            html_parts.append(str(content))
    text_body, html_body = "\n\n".join(text_parts), "\n\n".join(html_parts)
    for c, uri in cid_map.items():
        html_body = html_body.replace(f"cid:{c}", uri)
    return text_body, html_body, json.dumps(attachments, ensure_ascii=False)


def get_current_domain():
    host = request.host.split(":")[0].strip().lower()
    is_ip = bool(re.match(r'^\d{1,3}(\.\d{1,3}){3}$', host)) or host in ("localhost", "127.0.0.1")
    return host, is_ip


STYLE = r"""
<style>
:root {
    --bg: #ffffff;
    --card: #ffffff;
    --line: #e8ecf4;
    --line2: #dde3ef;
    --ink: #0f172a; --ink2: #475569; --ink3: #8a94a6;
    --accent: #7c5cf0; --accent2: #0891b2;
    --brand: #6366f1;
    --ok: #059669; --warn: #b45309; --danger: #dc2626;
    --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html { background: #fff; }
body {
    background: var(--bg);
    background-image:
        radial-gradient(720px 420px at 10% -6%, rgba(124,92,240,.10), transparent 62%),
        radial-gradient(680px 400px at 92% -4%, rgba(34,211,238,.10), transparent 62%),
        radial-gradient(900px 520px at 50% 108%, rgba(99,102,241,.07), transparent 62%);
    background-attachment: fixed;
    color: var(--ink);
    font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", Roboto, sans-serif;
    -webkit-font-smoothing: antialiased;
    min-height: 100vh; padding-bottom: 70px;
}
a { color: var(--accent2); }

/* ---------- 背景科技线条（错落渐变 + 交叉断开动效） ---------- */
.bg-deco { position: fixed; inset: 0; overflow: hidden; pointer-events: none; z-index: 0; }
.bg-deco i {
    position: absolute; display: block; height: 1.5px; border-radius: 4px;
    background: linear-gradient(90deg, rgba(124,92,240,0), rgba(124,92,240,.55) 35%, rgba(34,211,238,.45) 70%, rgba(34,211,238,0));
    will-change: transform, opacity; animation: drift 14s ease-in-out infinite;
}
.bg-deco i.dl-1 { top: 6%;  left: -8%;  width: 640px; transform: rotate(-17deg); opacity: .55; animation-delay: -2s; }
.bg-deco i.dl-2 { top: 16%; right: -10%; width: 560px; transform: rotate(14deg);  opacity: .38; animation-delay: -6s; }
.bg-deco i.dl-3 { top: 34%; left: -12%; width: 480px; transform: rotate(-9deg);  opacity: .30; animation-delay: -9s; }
.bg-deco i.dl-4 { top: 48%; right: -14%; width: 720px; transform: rotate(-22deg); opacity: .42; animation-delay: -4s; }
.bg-deco i.dl-5 { top: 63%; left: -10%; width: 600px; transform: rotate(12deg);  opacity: .34; animation-delay: -11s; }
.bg-deco i.dl-6 { top: 78%; right: -8%;  width: 520px; transform: rotate(-15deg); opacity: .40; animation-delay: -7s; }
.bg-deco i.dl-7 { top: 90%; left: 6%;   width: 460px; transform: rotate(20deg);  opacity: .26; animation-delay: -13s; }
.bg-deco i.dl-8 { top: 27%; right: 12%; width: 300px; transform: rotate(36deg);  opacity: .20; animation-delay: -5s; }

/* 新增：更细更短的穿插线条（普通漂浮） */
.bg-deco i.dl-15 { top: 11%; left: 22%;  width: 240px; transform: rotate(-32deg); opacity: .16; animation-delay: -10s; }
.bg-deco i.dl-16 { top: 68%; right: 20%; width: 280px; transform: rotate(28deg);  opacity: .18; animation-delay: -16s; }

/* 交叉线条：对向靠拢 -> 交叉 -> 自动断开 -> 复位 */
.bg-deco i.cx {
    -webkit-mask-image: linear-gradient(90deg, #000 0 40%, transparent 40% 46%, #000 46% 72%, transparent 72% 78%, #000 78%);
    mask-image: linear-gradient(90deg, #000 0 40%, transparent 40% 46%, #000 46% 72%, transparent 72% 78%, #000 78%);
    -webkit-mask-size: 200% 100%; mask-size: 200% 100%;
    -webkit-mask-repeat: no-repeat; mask-repeat: no-repeat;
    animation: cxSwing var(--sw, 20s) ease-in-out infinite, cxGap var(--gp, 8s) linear infinite;
}
.bg-deco i.dl-9  { top: 40%; left: 4%;   width: 540px; opacity: .42;
                   --r: -16deg; --r2: 5deg;  --o1: .44; --o2: .20; --dx: 30px; --dy: -14px;
                   animation-delay: -3s, -1s; }
.bg-deco i.dl-10 { top: 41.5%; left: 4%; width: 540px; opacity: .36;
                   --r: 12deg;  --r2: -9deg; --o1: .36; --o2: .14; --dx: -26px; --dy: 12px;
                   animation-delay: -12s, -5s; }
.bg-deco i.dl-11 { top: 55%; right: 6%;  width: 500px; opacity: .34;
                   --r: 18deg;  --r2: -6deg; --o1: .36; --o2: .16; --dx: -34px; --dy: -12px; --sw: 24s;
                   animation-delay: -7s, -2s; }
.bg-deco i.dl-12 { top: 56.5%; right: 6%; width: 500px; opacity: .30;
                   --r: -11deg; --r2: 10deg; --o1: .32; --o2: .13; --dx: 28px; --dy: 10px; --sw: 24s;
                   animation-delay: -18s, -6s; }
.bg-deco i.dl-13 { top: 24%; left: 16%;  width: 420px; opacity: .26;
                   --r: -24deg; --r2: 8deg;  --o1: .28; --o2: .12; --dx: 22px; --dy: 14px; --sw: 28s; --gp: 10s;
                   animation-delay: -5s, -3s; }
.bg-deco i.dl-14 { top: 25.5%; left: 16%; width: 420px; opacity: .24;
                   --r: 20deg;  --r2: -10deg; --o1: .26; --o2: .10; --dx: -20px; --dy: -12px; --sw: 28s; --gp: 10s;
                   animation-delay: -16s, -8s; }

@keyframes cxSwing {
    0%, 100% { transform: translate3d(0, 0, 0) rotate(var(--r, 0deg)); opacity: var(--o1, .4); }
    50% { transform: translate3d(var(--dx, 24px), var(--dy, -10px), 0) rotate(var(--r2, var(--r, 0deg))); opacity: var(--o2, .18); }
}
@keyframes cxGap {
    0%   { -webkit-mask-position: 122% 0; mask-position: 122% 0; }
    100% { -webkit-mask-position: -22% 0; mask-position: -22% 0; }
}

.bg-deco b {
    position: absolute; display: block; border-radius: 50%; filter: blur(46px); opacity: .5;
}
.bg-deco b.g-1 { width: 300px; height: 300px; top: -70px;  left: 4%;  background: rgba(124,92,240,.13); }
.bg-deco b.g-2 { width: 260px; height: 260px; top: 12%;  right: 2%; background: rgba(34,211,238,.12); }
.bg-deco b.g-3 { width: 340px; height: 340px; bottom: -90px; left: 46%; background: rgba(99,102,241,.10); }
@keyframes drift {
    0%, 100% { transform: translateY(0) rotate(var(--r, 0deg)); }
    50% { transform: translateY(-9px) rotate(var(--r, 0deg)); }
}

/* ---------- 卡片 ---------- */
.container { position: relative; z-index: 1; max-width: 920px; margin: 0 auto; padding: 30px 16px; }
.card {
    position: relative;
    background: var(--card);
    border: 1px solid var(--line); border-radius: 18px;
    padding: 26px; margin-bottom: 20px;
    box-shadow: 0 10px 34px rgba(15,23,42,.06), 0 1px 2px rgba(15,23,42,.04);
    overflow: hidden;
}
.card::before {
    content: ""; position: absolute; left: 0; right: 0; top: 0; height: 1.5px;
    background: linear-gradient(90deg, transparent, rgba(124,92,240,.5), rgba(34,211,238,.42), transparent);
}

/* ---------- 顶部 ---------- */
.header-row { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 20px; }
.header-left { display: flex; align-items: center; gap: 12px; min-width: 0; }
.logo-badge {
    width: 44px; height: 44px; border-radius: 13px; flex-shrink: 0;
    background: linear-gradient(135deg, #8b5cf6, #6366f1 55%, #22d3ee);
    display: grid; place-items: center; font-size: 21px;
    box-shadow: 0 6px 16px rgba(124,92,240,.28), inset 0 1px 0 rgba(255,255,255,.45);
}
.header-left h1 {
    font-size: 23px; font-weight: 800; letter-spacing: -.3px; white-space: nowrap;
    background: linear-gradient(90deg, #3b1d8f, #6d5efc 55%, #0891b2);
    -webkit-background-clip: text; background-clip: text;
    -webkit-text-fill-color: transparent; color: #4c1d95;
}
.header-left p { color: var(--ink3); font-size: 12.5px; margin-top: 2px; }
.header-right { display: flex; align-items: center; gap: 14px; font-size: 12.5px; color: var(--ink3); white-space: nowrap; }
.sync-status { color: var(--accent2); font-weight: 600; font-size: 12px; }
.sync-status::before { content: "●"; margin-right: 5px; font-size: 9px; vertical-align: 1px; }

/* ---------- 邮箱区（高度与按钮一致 46px） ---------- */
.mailbox-row { display: flex; align-items: stretch; gap: 10px; }
.mailbox-box {
    flex: 1; min-width: 0; display: flex; align-items: center; justify-content: center;
    height: 46px; min-height: 46px; padding: 0 15px;
    background: #f7f5ff; border: 1px dashed rgba(124,92,240,.45);
    border-radius: 11px;
    font: 700 16px/1.2 var(--mono); letter-spacing: .2px; color: #4c1d95;
    word-break: break-all; text-align: center; overflow: hidden;
}
.mailbox-box.muted {
    font-family: inherit; font-size: 13px; font-weight: 600; letter-spacing: 0;
    color: #b45309; background: #fffaf0; border-color: rgba(180,83,9,.35);
}

/* ---------- 按钮 ---------- */
.btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    height: 46px; padding: 0 20px; border-radius: 11px; border: 1px solid transparent;
    font-size: 14px; font-weight: 600; font-family: inherit; letter-spacing: .2px;
    cursor: pointer; white-space: nowrap; text-decoration: none;
    transition: filter .18s, background .18s, box-shadow .18s, transform .06s;
}
.btn:active { transform: translateY(.5px); }
.btn-primary {
    background: linear-gradient(135deg, #7c5cf0, #6366f1); color: #fff;
    box-shadow: 0 4px 14px rgba(99,102,241,.28), inset 0 1px 0 rgba(255,255,255,.3);
}
.btn-primary:hover { filter: brightness(1.06); box-shadow: 0 6px 18px rgba(99,102,241,.34), inset 0 1px 0 rgba(255,255,255,.3); }
.btn-accent {
    background: linear-gradient(135deg, #22d3ee, #38bdf8); color: #053b4d;
    box-shadow: 0 4px 14px rgba(14,165,233,.24), inset 0 1px 0 rgba(255,255,255,.55);
}
.btn-accent:hover { filter: brightness(1.04); box-shadow: 0 6px 18px rgba(14,165,233,.3), inset 0 1px 0 rgba(255,255,255,.55); }
.btn-secondary {
    background: #f4f6fa; border-color: var(--line2); color: #3f4a5f;
}
.btn-secondary:hover { background: #eef1f7; border-color: #cdd6e4; }
.btn-sm { height: 34px; padding: 0 14px; font-size: 12.5px; border-radius: 9px; }
.btn-block { width: 100%; }

/* ---------- 收件箱 ---------- */
.inbox-header { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 14px; }
.inbox-title { display: flex; align-items: center; gap: 10px; }
.inbox-title h2 { font-size: 16.5px; font-weight: 700; letter-spacing: -.2px; }
.chip {
    background: #f1edff; color: #5b21b6; border: 1px solid #e2d9ff;
    border-radius: 99px; padding: 1px 9px; font-size: 11.5px; font-weight: 700;
}
.mail-item {
    border: 1px solid var(--line); border-radius: 12px; margin-bottom: 9px; overflow: hidden;
    background: #fff; transition: border-color .18s, box-shadow .18s, background .18s;
}
.mail-item:hover { border-color: rgba(124,92,240,.35); box-shadow: 0 4px 14px rgba(15,23,42,.05); }
.mail-item.open { border-color: rgba(124,92,240,.45); background: #faf9ff; }
.mail-head { padding: 14px 16px; cursor: pointer; }
.mail-line-pc { display: flex; align-items: center; gap: 16px; }
.mail-line-mobile { display: none; }
.mail-subject { flex: 2; min-width: 0; font-weight: 600; font-size: 13.5px; color: var(--ink); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mail-sender { flex: 1; min-width: 0; color: var(--ink3); font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mail-right { display: flex; align-items: center; gap: 12px; flex-shrink: 0; font-size: 12px; color: var(--ink3); }
.badge { padding: 2px 9px; border-radius: 7px; font-size: 11px; font-weight: 700; letter-spacing: .2px; }
.badge-unread { background: #fef3c7; color: #b45309; }
.badge-read { background: #ecfdf5; color: #047857; }
.cd-timer { color: var(--accent2); font-weight: 600; font-variant-numeric: tabular-nums; }
.mail-body { display: none; padding: 18px 16px 20px; border-top: 1px solid var(--line); background: #fbfcfe; }
.meta-table { width: 100%; border-collapse: collapse; margin-bottom: 14px; font-size: 12.5px; }
.meta-table tr { display: flex; flex-wrap: wrap; align-items: baseline; }
.meta-table th { text-align: left; padding: 7px 11px; color: var(--ink3); font-weight: 600; background: #f3f5fa; border-radius: 6px; white-space: nowrap; }
.meta-table td { padding: 7px 11px; color: var(--ink2); word-break: break-all; min-width: 0; }
.meta-table td + th { margin-left: 16px; }
.attachments { margin: 10px 0; padding: 10px 12px; background: #fff; border: 1px solid var(--line); border-radius: 9px; font-size: 12.5px; }
.attachments b { color: var(--ink2); font-weight: 600; }
.attachments a { color: #0284c7; text-decoration: none; margin-right: 14px; font-weight: 500; }
.attachments a:hover { text-decoration: underline; }
.empty { text-align: center; padding: 54px 20px; color: var(--ink3); font-size: 13px; line-height: 1.9; }
.empty b { display: block; color: #b45309; font-size: 13.5px; font-weight: 600; margin-bottom: 4px; }
iframe { width: 100%; display: block; border: 1px solid var(--line); height: 80px; min-height: 0; background: #fff; border-radius: 9px; }
pre { white-space: pre-wrap; word-break: break-word; font-family: inherit; line-height: 1.7; color: var(--ink2); }

/* ---------- 浮层 / 页脚 ---------- */
.img-viewer { display: none; position: fixed; inset: 0; background: rgba(15,23,42,.72); z-index: 9999; justify-content: center; align-items: center; cursor: zoom-out; }
.img-viewer.show { display: flex; }
.img-viewer img { max-width: 95%; max-height: 95%; object-fit: contain; border-radius: 8px; }
.toast {
    position: fixed; left: 50%; top: 50%; transform: translate(-50%, -46%) scale(.96);
    background: #0f172a; color: #fff; border: 1px solid rgba(255,255,255,.1);
    padding: 14px 22px; border-radius: 12px; font-size: 13.5px; z-index: 10000;
    display: none; opacity: 0; box-shadow: 0 18px 46px rgba(15,23,42,.3);
    transition: opacity .2s, transform .2s; max-width: min(90vw, 460px); text-align: center;
}
.toast.show { display: block; opacity: 1; transform: translate(-50%, -50%) scale(1); }
.toast-title { font-size: 12.5px; color: #cbd5e1; margin-bottom: 5px; letter-spacing: .2px; }
.toast-email {
    font: 700 14.5px/1.4 var(--mono); color: #a5f3fc; word-break: break-all;
    letter-spacing: .2px;
}
.footer {
    position: fixed; left: 0; right: 0; bottom: 0; padding: 13px 16px; text-align: center;
    font-size: 12px; line-height: 1.9; color: var(--ink3); background: rgba(255,255,255,.9);
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    border-top: 1px solid var(--line); z-index: 99;
}
.footer a, .footer a:link, .footer a:visited, .footer a:active {
    color: var(--accent); text-decoration: none !important; margin-left: 4px; font-weight: 600;
    border-bottom: 0 !important; box-shadow: none !important;
}
/* 鼠标悬停也不显示下划线 */
.footer a:hover, .footer a:focus, .footer a:focus-visible {
    text-decoration: none !important; opacity: .82; border-bottom: 0 !important;
}
.footer .footer-brand { color: var(--brand); }
.footer .footer-sep { color: #cbd5e1; margin: 0 3px; }
.footer .footer-dev a { margin-left: 2px; }

/* ---------- 开源声明 / 版权说明 ---------- */
.license-strip {
    margin-top: 16px; padding: 13px 15px; border-radius: 12px;
    background: linear-gradient(135deg, #f8f6ff, #f1fbff);
    border: 1px dashed #d8d0fa;
    display: flex; gap: 11px; align-items: flex-start; text-align: left;
}
.license-strip .lic-badge {
    flex: none; font-size: 11px; font-weight: 700; letter-spacing: .4px; color: #fff;
    background: linear-gradient(135deg, #7c5cf0, #22d3ee);
    padding: 4px 9px; border-radius: 6px; white-space: nowrap; margin-top: 1px;
}
.license-strip .lic-text { font-size: 12.5px; line-height: 1.8; color: var(--ink2); }
.license-strip .lic-text b { color: #b45309; }
.license-strip .lic-text a {
    color: var(--accent); font-weight: 700;
    text-decoration: none !important;
}
.license-strip .lic-text a:hover { text-decoration: none !important; opacity: .82; }
.license-strip .lic-meta { display: block; margin-top: 3px; font-size: 11.5px; color: var(--ink3); }
/* 禁止商用细则独立成行，避免长句把卡片撑破 */
.license-strip .lic-ban { display: block; margin: 4px 0 6px; line-height: 1.75; }

/* ---------- 主域名配置表单 ---------- */
.setup-box {
    margin: 14px 0 16px; padding: 15px 16px; border-radius: 12px;
    background: linear-gradient(135deg, #f7f9ff, #f4fbfd);
    border: 1px solid #e2e8f6;
}
.setup-title { font-size: 13px; font-weight: 700; color: var(--ink); margin-bottom: 10px; }
.setup-form { display: flex; gap: 9px; flex-wrap: wrap; align-items: stretch; }
/* 左侧输入框与右侧保存按钮保持同一高度 (46px) */
.setup-input {
    flex: 1 1 260px; min-width: 0; height: 46px; padding: 0 14px;
    border: 1px solid var(--line2); border-radius: 11px; background: #fff;
    font: 13.5px/1.4 var(--mono); color: var(--ink); outline: none;
    transition: border-color .18s, box-shadow .18s;
}
.setup-form .btn { flex: 0 0 auto; height: 46px; min-width: 148px; }
.setup-input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(124,92,240,.13); }
.setup-status {
    margin-top: 10px; font-size: 12.5px; line-height: 1.7; color: var(--ink3);
    padding: 8px 11px; border-radius: 9px; background: rgba(255,255,255,.72);
    border: 1px dashed var(--line2);
}
.setup-status b { color: var(--ink); }
.setup-status.ok { color: #047857; border-color: #a7f3d0; background: #f0fdfa; }
.setup-status.pending { color: #b45309; border-color: #fde68a; background: #fffbeb; }
.setup-status.err { color: #b91c1c; border-color: #fecaca; background: #fef2f2; }

/* ---------- 自定义前缀弹窗 ---------- */
.modal-overlay { display: none; position: fixed; inset: 0; background: rgba(15,23,42,.34); z-index: 9998; align-items: center; justify-content: center; padding: 16px; backdrop-filter: blur(3px); }
.modal-overlay.show { display: flex; }
.modal-box {
    background: #fff; border: 1px solid var(--line); border-radius: 16px;
    padding: 28px; width: 100%; max-width: 440px; box-shadow: 0 24px 64px rgba(15,23,42,.18);
    animation: modalIn .2s ease;
}
@keyframes modalIn { from { opacity: 0; transform: scale(.96) translateY(8px); } to { opacity: 1; transform: none; } }
.modal-box h3 { font-size: 17px; font-weight: 700; margin-bottom: 6px; }
.modal-box p { color: var(--ink3); font-size: 12.5px; line-height: 1.7; margin-bottom: 18px; }
.modal-input-row { display: flex; margin-bottom: 12px; }
.modal-input {
    flex: 1; min-width: 0; height: 46px; padding: 0 14px;
    background: #fbfcff; border: 1px solid var(--line2); border-right: none;
    border-radius: 9px 0 0 9px; color: var(--ink); font: 600 14px var(--mono); outline: none;
}
.modal-input:focus { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); background: #fff; }
.modal-input.error { border-color: var(--danger); box-shadow: inset 0 0 0 1px var(--danger); }
.modal-suffix {
    display: flex; align-items: center; padding: 0 14px; background: #f4f6fa;
    border: 1px solid var(--line2); border-radius: 0 9px 9px 0; color: var(--ink3);
    font-size: 12.5px; white-space: nowrap; font-weight: 500;
}
.modal-hint { font-size: 11.5px; color: var(--ink3); margin-bottom: 6px; }
.modal-error { font-size: 12px; color: var(--danger); min-height: 17px; margin-bottom: 10px; line-height: 1.5; }
.modal-actions { display: flex; gap: 10px; margin-top: 6px; }
.modal-actions .btn { flex: 1; }

/* ---------- IP 访问引导（白底 + 科技线条） ---------- */
.warn-card { border-color: #f3e2c8; }
.warn-card::before { background: linear-gradient(90deg, transparent, rgba(180,83,9,.4), rgba(124,92,240,.35), transparent); }
.warn-head { display: flex; gap: 14px; align-items: flex-start; margin-bottom: 6px; }
.warn-icon {
    width: 44px; height: 44px; border-radius: 12px; flex-shrink: 0; display: grid; place-items: center;
    background: #fff7ed; border: 1px solid #fed7aa;
    color: #c2410c; font-size: 20px; font-weight: 800;
}
.warn-card h2 { font-size: 19px; font-weight: 700; letter-spacing: -.3px; color: #b45309; }
.warn-sub { color: #92400e; font-size: 12.5px; margin-top: 5px; line-height: 1.7; opacity: .9; }
.note { background: #fbfcfe; border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; margin: 16px 0; font-size: 12.5px; color: var(--ink2); line-height: 1.8; }
.note code, .steps code {
    background: #fff7ed; color: #b45309; padding: 1px 6px; border-radius: 5px;
    font: 600 12px var(--mono); border: 1px solid #fde8ce;
}
.steps { list-style: none; counter-reset: s; margin: 18px 0; display: flex; flex-direction: column; gap: 11px; }
.steps li { counter-increment: s; position: relative; padding-left: 32px; font-size: 13px; color: #7c4a03; line-height: 1.75; }
.steps li::before {
    content: counter(s); position: absolute; left: 0; top: 1px; width: 21px; height: 21px; border-radius: 50%;
    background: #fff7ed; border: 1px solid #fed7aa; color: #c2410c;
    font-size: 11.5px; font-weight: 700; display: grid; place-items: center;
}
.config-block {
    background: #0f1729; border-radius: 10px; padding: 15px;
    font: 12.5px/1.9 var(--mono); color: #a5f3fc; overflow-x: auto; margin: 14px 0;
    box-shadow: inset 0 1px 0 rgba(255,255,255,.08);
}
.config-block .comment { color: #7c8aa5; }

/* ---------- 独立代码块 + 复制按钮 ---------- */
.code-card {
    margin: 14px 0 0; border-radius: 11px; overflow: hidden; background: #0f1729;
    box-shadow: inset 0 1px 0 rgba(255,255,255,.08);
}
.code-card:last-of-type { margin-bottom: 16px; }
.code-head {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: 9px 11px 9px 15px; background: rgba(255,255,255,.055);
    border-bottom: 1px solid rgba(255,255,255,.08);
}
.code-title { font: 600 12px/1.4 var(--mono); color: #cbd5e1; letter-spacing: .2px; }
.code-copy {
    flex: none; height: 27px; padding: 0 11px; border-radius: 7px; cursor: pointer;
    background: rgba(34,211,238,.16); border: 1px solid rgba(34,211,238,.42);
    color: #a5f3fc; font: 600 12px/1 var(--mono); letter-spacing: 0;
    white-space: nowrap;
    transition: background .16s, color .16s, border-color .16s;
}
.code-copy:hover { background: rgba(34,211,238,.28); color: #ecfeff; }
.code-copy.ok { background: #22d3ee; border-color: #22d3ee; color: #053b4d; }
.code-body {
    margin: 0; padding: 13px 15px; overflow-x: auto; white-space: pre;
    font: 12.5px/1.9 var(--mono); color: #a5f3fc; -webkit-overflow-scrolling: touch;
}

/* ---------- 小复制按钮 + 普通信息行 ---------- */
.note-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.mini-copy {
    flex: none; display: inline-flex; align-items: center; justify-content: center;
    height: 23px; padding: 0 9px; border-radius: 6px; cursor: pointer; white-space: nowrap;
    background: #fff7ed; border: 1px solid #fed7aa; color: #b45309;
    font: 600 11.5px/1 var(--mono); letter-spacing: 0;
    transition: background .16s, color .16s, border-color .16s;
}
.mini-copy:hover { background: #ffedd5; border-color: #fdba74; color: #9a3412; }
.mini-copy:active { transform: translateY(.5px); }
.mini-copy.ok { background: #16a34a; border-color: #16a34a; color: #fff; }

/* ---------- DNS 解析（普通排版，非代码块） ---------- */
.dns-block {
    margin: 14px 0 16px; padding: 13px 15px 12px;
    border: 1px solid var(--line); border-radius: 10px; background: #fbfcfe;
}
.dns-head {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    margin-bottom: 9px;
}
.dns-title { font-size: 13px; font-weight: 700; color: var(--ink); letter-spacing: -.1px; }
.dns-row {
    display: flex; flex-wrap: wrap; align-items: center; gap: 3px 22px;
    font-size: 12.5px; color: var(--ink2); line-height: 1.85;
}
.dns-row + .dns-row { margin-top: 3px; padding-top: 7px; border-top: 1px dashed var(--line); }
.dns-type { font-weight: 700; color: #b45309; }
.dns-val { display: inline-flex; align-items: center; gap: 8px; color: var(--ink); }
.dns-val code {
    background: #fff7ed; color: #b45309; padding: 1px 6px; border-radius: 5px;
    font: 600 12px var(--mono); border: 1px solid #fde8ce;
}
.dns-hint { margin-top: 9px; font-size: 11.5px; line-height: 1.7; color: var(--ink3); }
.jump-tip {
    margin-top: 12px; padding: 9px 12px; border-radius: 10px;
    background: #f8fafc; border: 1px dashed var(--line2);
    font-size: 12.5px; line-height: 1.75; color: var(--ink2);
}
.jump-tip b { color: #b45309; }
.jump-tip a { color: var(--accent); font-weight: 600; text-decoration: none; }
.warn-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; padding-top: 18px; border-top: 1px solid var(--line); }

@media (max-width: 768px) {
    .container { padding: 16px 12px; }
    .card { padding: 18px; }
    body { padding-bottom: 86px; }
    .footer { font-size: 11.5px; line-height: 1.85; padding: 10px 10px; }
    .footer .footer-sep { margin: 0 2px; }
    .license-strip { padding: 11px 12px; gap: 9px; }
    .license-strip .lic-text { font-size: 12px; }
    .header-left p { display: none; }
    .hide-mobile { display: none !important; }
    .header-right { gap: 8px; font-size: 11.5px; }
    .mailbox-row { flex-direction: column; }
    .mailbox-row .mailbox-box { height: 46px; min-height: 46px; font-size: 15px; }
    .btn-row-mobile { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
    .btn-row-mobile .btn { flex: 1; min-width: 0; padding: 0 10px; font-size: 13px; height: 46px; }
    .mail-line-pc { display: none; }
    .mail-line-mobile { display: flex; flex-direction: column; gap: 6px; }
    .mail-mobile-row1 { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .mail-mobile-row1 .mail-subject { font-size: 13px; }
    .mail-mobile-row2 { display: flex; align-items: center; justify-content: space-between; gap: 10px; font-size: 12px; color: var(--ink3); }
    .meta-table tr { flex-direction: column; }
    .meta-table td + th { margin-left: 0; margin-top: 6px; }
    .meta-table th, .meta-table td { width: 100%; }
    .modal-box { padding: 22px; }
    .warn-card { padding: 18px; }
    .warn-actions .btn { flex: 1; }
    .bg-deco i.dl-3, .bg-deco i.dl-5, .bg-deco i.dl-7, .bg-deco i.dl-8,
    .bg-deco i.dl-11, .bg-deco i.dl-12, .bg-deco i.dl-13, .bg-deco i.dl-14,
    .bg-deco i.dl-16 { display: none; }
    .bg-deco i { opacity: .26 !important; }
}
@media (min-width: 769px) {
    .btn-row-mobile { display: none; }
}
@media (prefers-reduced-motion: reduce) {
    .bg-deco i { animation: none !important; }
    .bg-deco i.cx { -webkit-mask-image: none; mask-image: none; }
}
</style>
"""

INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TempMail - 临时邮箱</title>
    {{ style|safe }}
</head>
<body>
<div class="bg-deco" aria-hidden="true">
    <i class="dl-1" style="--r:-17deg"></i>
    <i class="dl-2" style="--r:14deg"></i>
    <i class="dl-3" style="--r:-9deg"></i>
    <i class="dl-4" style="--r:-22deg"></i>
    <i class="dl-5" style="--r:12deg"></i>
    <i class="dl-6" style="--r:-15deg"></i>
    <i class="dl-7" style="--r:20deg"></i>
    <i class="dl-8" style="--r:36deg"></i>
    <i class="dl-9 cx"></i>
    <i class="dl-10 cx"></i>
    <i class="dl-11 cx"></i>
    <i class="dl-12 cx"></i>
    <i class="dl-13 cx"></i>
    <i class="dl-14 cx"></i>
    <i class="dl-15"></i>
    <i class="dl-16"></i>
    <b class="g-1"></b>
    <b class="g-2"></b>
    <b class="g-3"></b>
</div>
{% if mode == 'setup' %}
    <div class="container" style="max-width: 800px;">
        <div class="card warn-card">
            <div class="warn-head">
                <div class="warn-icon">!</div>
                <div>
                    <h2>未配置主域名</h2>
                    <div class="warn-sub">当前访问：<code>{{ host }}</code>{% if host != public_ip and public_ip %}（服务器公网 IP <code>{{ public_ip }}</code>）{% endif %}。临时邮箱必须绑定一个已解析到本机的域名。</div>
                </div>
            </div>

            <div class="setup-box">
                <div class="setup-title">① 配置主域名（绑定后仅该域名可访问，禁止他人随意反代）</div>
                <form class="setup-form" onsubmit="saveDomain(event)">
                    <input id="domain-input" class="setup-input" type="text" spellcheck="false" autocomplete="off"
                           placeholder="例如 mail.example.com" value="{{ configured_domain }}">
                    <button class="btn btn-accent" type="submit" id="setup-save">保存并检测解析</button>
                </form>
                <div class="setup-status" id="setup-status">
                    {% if configured_domain %}
                        已保存 <b>{{ configured_domain }}</b>，正在等待 DNS 解析生效 · 每 10 秒自动检测
                    {% else %}
                        尚未配置 · 每 10 秒自动检测解析状态
                    {% endif %}
                </div>
            </div>

            <div class="note note-row">服务器公网 IP：<code id="public-ip">{{ public_ip }}</code>
                <button class="mini-copy" type="button" data-copy-text="{{ public_ip }}" onclick="copyPlain(this)">复制</button>
            </div>
            <ol class="steps">
                <li>将主域名 A 记录解析到公网 IP：<code>{{ public_ip }}</code></li>
                <li>用 Nginx / Caddy 反向代理到本机端口 <code>{{ port }}</code>，并传递 <code>Host</code> 头</li>
                <li>配置 MX 记录：主机记录 <code>@</code>，记录值 <code id="step-mx">{{ dns_mx_value }}</code></li>
                <li>在上方填写主域名并保存，解析正常后下滑到页面底部点「前往邮箱」按钮手动进入（不再自动跳转）</li>
            </ol>
            <div class="code-card">
                <div class="code-head">
                    <span class="code-title">Nginx 反向代理示例</span>
                    <button class="code-copy" type="button" data-copy-target="code-nginx" data-copy-key="nginx" onclick="copyCode(this)">复制反代配置</button>
                </div>
                <pre class="code-body" id="code-nginx">location / {
    proxy_pass http://127.0.0.1:{{ port }};
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}</pre>
            </div>
            <div class="dns-block">
                <div class="dns-head">
                    <span class="dns-title">DNS 解析（A 记录 + MX 记录）</span>
                    <button class="mini-copy" type="button" onclick="copyDns(this)">复制解析配置</button>
                </div>
                <div class="dns-row">
                    <span class="dns-type">类型: A</span>
                    <span>主机记录: <b id="dns-a-host">{{ dns_a_host }}</b></span>
                    <span class="dns-val">记录值: <code id="dns-a-value">{{ public_ip }}</code>
                        <button class="mini-copy" type="button" data-copy-text="{{ public_ip }}" onclick="copyPlain(this)">复制</button>
                    </span>
                </div>
                <div class="dns-row">
                    <span class="dns-type">类型: MX</span>
                    <span>主机记录: <b>@</b></span>
                    <span class="dns-val">记录值: <code id="dns-mx-value">{{ dns_mx_value }}</code>
                        <button class="mini-copy" type="button" id="dns-mx-copy" data-copy-text="{{ dns_mx_value }}" onclick="copyPlain(this)">复制</button>
                    </span>
                </div>
                <div class="dns-hint" id="dns-hint"{% if not dns_hint %} style="display:none"{% endif %}>{{ dns_hint }}</div>
            </div>
            <div class="license-strip">
                <span class="lic-badge">开源版</span>
                <div class="lic-text">
                    <b>禁止商用</b>：本程序为开源版本，仅限个人学习与自用。
                    <span class="lic-ban"><b>严禁任何形式的商业售卖、付费分发、二次销售或作为付费服务的一部分。</b></span>
                    开发者：<a href="{{ developer_url }}" target="_blank" rel="noopener">{{ developer_name }}</a>
                    <span class="lic-meta">{{ copyright_text }} · {{ app_name }} v{{ app_version }} · {{ license_title }}</span>
                </div>
            </div>
            <div class="warn-actions">
                <a class="btn btn-accent" id="goto-btn" href="{{ setup_target or '#' }}" style="display:none">前往邮箱</a>
                <button class="btn btn-secondary" type="button" id="stay-btn" style="display:none" onclick="stopCountdown(true)">查看检测页面</button>
                <a class="btn btn-secondary" href="/?preview=1" target="_blank" rel="noopener">🖥 查看程序页面</a>
                <a class="btn btn-secondary" href="{{ site_url }}" target="_blank" rel="noopener">项目主页</a>
            </div>
            <div class="jump-tip" id="jump-tip">配置完成后请下滑到页面底部，点「前往邮箱」按钮进入程序（跳转地址不带端口）。复制「反代配置」与「解析配置」后，再次用 IP 打开本页会 5 秒倒计时自动跳转。</div>
        </div>
    </div>
{% else %}
    <div class="container">
        <div class="card">
            <div class="header-row">
                <div class="header-left">
                    <div class="logo-badge">📬</div>
                    <div>
                        <h1>TempMail</h1>
                        <p>匿名临时邮箱 · 收信即焚 · 无需注册</p>
                    </div>
                </div>
                <div class="header-right">
                    {% if mode == 'preview' %}
                    <span class="sync-status" style="color:#b45309">未配置域名</span>
                    {% else %}
                    <span class="sync-status" id="sync-status">连接中...</span>
                    {% endif %}
                    <span class="hide-mobile">邮件保留 <b>{{ retention }}</b> 分钟</span>
                </div>
            </div>
            <div class="mailbox-row">
                <div class="mailbox-box{% if mode == 'preview' %} muted{% endif %}" id="mailbox-display">{% if mode == 'preview' %}未检测到域名配置，无法生成临时邮箱{% else %}正在生成专属邮箱...{% endif %}</div>
                {% if mode != 'preview' %}
                <button class="btn btn-accent hide-mobile" onclick="openCustomModal()">✎ 自定义前缀</button>
                <button class="btn btn-secondary hide-mobile" onclick="generateMailbox()">⟳ 换一个</button>
                <button class="btn btn-primary hide-mobile" onclick="copyEmail()">⧉ 复制邮箱</button>
                {% endif %}
            </div>
            {% if mode == 'preview' %}
            <div class="btn-row-mobile">
                <button class="btn btn-accent" onclick="previewBlocked()">✎ 自定义前缀</button>
                <button class="btn btn-secondary" onclick="previewBlocked()">⟳ 换一个</button>
                <button class="btn btn-primary" onclick="previewBlocked()">⧉ 复制邮箱</button>
            </div>
            {% else %}
            <div class="btn-row-mobile">
                <button class="btn btn-accent" onclick="openCustomModal()">✎ 自定义前缀</button>
                <button class="btn btn-secondary" onclick="generateMailbox()">⟳ 换一个</button>
                <button class="btn btn-primary" onclick="copyEmail()">⧉ 复制邮箱</button>
            </div>
            {% endif %}
        </div>

        <div class="card">
            <div class="inbox-header">
                <div class="inbox-title">
                    <h2>📥 收件箱</h2>
                    <span class="chip" id="mail-count">0</span>
                </div>
                {% if mode == 'preview' %}
                <button class="btn btn-secondary btn-sm" onclick="previewBlocked()">刷新</button>
                {% else %}
                <button class="btn btn-secondary btn-sm" onclick="loadEmails(true)">刷新</button>
                {% endif %}
            </div>
            <div id="email-list">
                {% if mode == 'preview' %}
                <div class="empty"><b>未检测到域名配置，无法生成临时邮箱</b>页面已正常渲染，说明服务运行正常<br>请解析域名并配置反向代理后，使用域名访问</div>
                {% else %}
                <div class="empty">正在加载邮件...</div>
                {% endif %}
            </div>
        </div>
    </div>
{% endif %}

<div class="footer">
    © <span id="year"></span> <a class="footer-brand" href="{{ site_url }}" target="_blank" rel="noopener">TempMail</a>
    <span class="footer-sep">·</span><a class="footer-lic" href="{{ site_url }}" target="_blank" rel="noopener">开源版</a>
    <a href="{{ github_url }}" target="_blank" rel="noopener">GitHub</a>
</div>

<div id="toast" class="toast"></div>
<div id="img-viewer" class="img-viewer" onclick="this.classList.remove('show')"><img id="img-viewer-src" src="" alt=""></div>

{% if mode == 'setup' %}
<script>
(function () {
    var IS_IP = {{ 'true' if is_ip else 'false' }};
    var DOMAIN = {{ configured_domain|tojson }};
    var PUBLIC_IP = {{ public_ip|tojson }};
    var pollTimer = null, tickTimer = null, justSaved = false, firstStatus = true, curActive = false;

    function $(id) { return document.getElementById(id); }
    function lsGet(k) { try { return localStorage.getItem(k) === "1"; } catch (e) { return false; } }
    function lsSet(k) { try { localStorage.setItem(k, "1"); } catch (e) {} }

    /* 已配置反向代理：跳转地址一律不带端口 */
    function targetUrl(domain) {
        var proto = location.protocol === "https:" ? "https:" : "http:";
        return proto + "//" + domain + "/";
    }

    function showGoto(domain) {
        var btn = $("goto-btn");
        if (!btn) return;
        if (!domain) { btn.style.display = "none"; return; }
        btn.href = targetUrl(domain);
        btn.textContent = "前往邮箱：" + domain;
        btn.style.display = "";
    }

    /* 按主域名自动匹配记录：三级及以上域名 -> 主机记录取首段；裸域名 -> @ */
    function dnsPlan(domain) {
        var d = (domain || "").trim().toLowerCase();
        if (!d) return {aHost: "mail", mx: "mail.你的域名", hint: "配置主域名后，这里会自动匹配对应的 A / MX 记录值"};
        var parts = d.split(".");
        if (parts.length >= 3) return {aHost: parts[0], mx: d, hint: ""};
        return {aHost: "@", mx: d,
                hint: "当前使用裸域名：A 记录用 @ 指向本机 IP，建议改用二级域名（如 mail." + d + "）更规范"};
    }

    function renderDns(domain) {
        var p = dnsPlan(domain);
        $("dns-a-host").textContent = p.aHost;
        $("dns-mx-value").textContent = p.mx;
        $("dns-mx-copy").setAttribute("data-copy-text", p.mx);
        var step = $("step-mx");
        if (step) step.textContent = p.mx;
        var hint = $("dns-hint");
        if (p.hint) { hint.textContent = p.hint; hint.style.display = ""; }
        else { hint.style.display = "none"; }
    }

    function dnsText() {
        return "A 记录：主机记录 " + $("dns-a-host").textContent + "，记录值 " + $("dns-a-value").textContent +
               "\nMX 记录：主机记录 @，记录值 " + $("dns-mx-value").textContent;
    }

    function setStatus(cls, html) {
        var box = $("setup-status");
        if (!box) return;
        box.className = "setup-status" + (cls ? " " + cls : "");
        box.innerHTML = html;
    }

    function setTip(html) {
        var t = $("jump-tip");
        if (t) { t.style.display = ""; t.innerHTML = html; }
    }

    function startCountdown(domain) {
        if (tickTimer || !domain) return;
        var left = 5, tip = $("jump-tip"), stay = $("stay-btn");
        stay.style.display = "";
        tip.style.display = "";
        function paint() {
            tip.innerHTML = "解析正常，<b>" + left + "</b> 秒后自动前往 <b>" + domain +
                            "</b>；不想跳转请点「查看检测页面」";
        }
        paint();
        tickTimer = setInterval(function () {
            left -= 1;
            if (left <= 0) {
                clearInterval(tickTimer); tickTimer = null;
                location.href = targetUrl(domain);
                return;
            }
            paint();
        }, 1000);
    }

    window.stopCountdown = function (manual) {
        if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
        var stay = $("stay-btn");
        if (stay) stay.style.display = "none";
        if (manual) {
            setTip("已停止自动跳转 · 确认解析与反代都配置好后，下滑到底部点「前往邮箱」按钮手动进入" +
                   (DOMAIN ? "（<a href=\"" + targetUrl(DOMAIN) + "\">" + DOMAIN + "</a>）" : ""));
        }
    };

    function maybeCountdown(domain) {
        if (!IS_IP || !domain) return;
        if (!(lsGet("tm_copy_nginx") && lsGet("tm_copy_dns"))) {
            setTip("解析已生效 · 首次请先复制「反代配置」和「解析配置」，再下滑到底部点「前往邮箱」按钮手动进入；复制后再次用 IP 打开本页会 5 秒倒计时跳转。");
            return;
        }
        startCountdown(domain);
    }

    function afterCopy() {
        if (!curActive) return;
        if (lsGet("tm_copy_nginx") && lsGet("tm_copy_dns") && !justSaved) {
            setTip("两项配置已复制 · 解析正常时刷新本页会 5 秒后自动跳转，也可直接下滑到底部点「前往邮箱」按钮。");
        }
    }

    function applyStatus(d) {
        if (!d || !d.ok) return;
        var wasFirst = firstStatus;
        firstStatus = false;
        if (d.domain) { DOMAIN = d.domain; showGoto(d.domain); renderDns(d.domain); }
        if (d.active && d.domain) {
            curActive = true;
            setStatus("ok", "✅ 解析正常，主域名 <b>" + d.domain + "</b> 已生效 · 请下滑到页面底部点「前往邮箱」按钮进入");
            if (wasFirst && !justSaved) maybeCountdown(d.domain);
        } else if (d.configured && !d.active) {
            curActive = false;
            setStatus("pending", "⏳ 已保存 <b>" + d.domain + "</b>，" + (d.reason || "等待 DNS 解析生效") +
                                " · 每 10 秒自动复检");
        } else {
            curActive = false;
            setStatus("", "尚未配置 · 每 10 秒自动检测解析状态");
        }
    }

    async function poll() {
        try {
            var r = await fetch("/api/setup/status?t=" + Date.now(), {cache: "no-store"});
            var d = await r.json();
            if (d && d.ok) applyStatus(d);
        } catch (e) {}
    }

    /* 复制（HTTPS 下用 clipboard API，HTTP / IP 访问回退 execCommand） */
    function doCopy(btn, text, key) {
        function done() {
            if (!btn.getAttribute("data-label")) btn.setAttribute("data-label", btn.textContent.trim());
            btn.textContent = "已复制";
            btn.classList.add("ok");
            setTimeout(function () {
                btn.textContent = btn.getAttribute("data-label") || "复制";
                btn.classList.remove("ok");
            }, 1600);
            if (key) { lsSet("tm_copy_" + key); afterCopy(); }
        }
        function fallback(t) {
            var ta = document.createElement("textarea");
            ta.value = t;
            ta.setAttribute("readonly", "");
            ta.style.position = "fixed";
            ta.style.top = "-1000px";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.select();
            ta.setSelectionRange(0, t.length);
            var okFlag = false;
            try { okFlag = document.execCommand("copy"); } catch (e) { okFlag = false; }
            document.body.removeChild(ta);
            if (okFlag) done();
        }

        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text).then(done, function () { fallback(text); });
        } else {
            fallback(text);
        }
    }

    /* 代码块复制（反代配置） */
    window.copyCode = function (btn) {
        var el = $(btn.getAttribute("data-copy-target"));
        if (!el) return;
        doCopy(btn, (el.innerText || el.textContent || "").replace(/\u00a0/g, " "), btn.getAttribute("data-copy-key") || "");
    };

    /* 整块 DNS 解析配置复制 */
    window.copyDns = function (btn) {
        doCopy(btn, dnsText(), "dns");
    };

    /* 单值复制（IP / 记录值） */
    window.copyPlain = function (btn) {
        var text = btn.getAttribute("data-copy-text") || "";
        if (!text) return;
        doCopy(btn, text, "");
    };

    window.saveDomain = async function (ev) {
        ev.preventDefault();
        var input = $("domain-input");
        var btn = $("setup-save");
        var val = (input.value || "").trim();
        if (!val) { setStatus("err", "请填写主域名"); return; }
        btn.disabled = true; btn.textContent = "检测中…";
        try {
            var r = await fetch("/api/setup/domain", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({domain: val})
            });
            var d = await r.json();
            if (d && d.ok) {
                justSaved = true;                 // 本次会话首次配置：不自动跳转
                window.stopCountdown(false);
                if (d.domain) { DOMAIN = d.domain; showGoto(d.domain); renderDns(d.domain); }
                if (d.active) {
                    curActive = true;
                    setStatus("ok", "✅ 解析正常，主域名 <b>" + d.domain + "</b> 已生效 · 请下滑到页面底部点「前往邮箱」按钮进入");
                    setTip("配置已完成 · 把上面的「反代配置」加进 Nginx、把「解析配置」加进 DNS 后，下滑到页面底部点「前往邮箱」按钮进入（地址不带端口）。");
                } else {
                    curActive = false;
                    setStatus("pending", "⏳ 已保存 <b>" + (d.domain || val) + "</b>，" +
                                        (d.message || "等待 DNS 解析生效") + " · 每 10 秒自动复检");
                }
            } else {
                justSaved = true;
                setStatus("err", (d && (d.message || d.error)) || "保存失败");
            }
        } catch (e) {
            setStatus("err", "网络异常，请重试");
        }
        btn.disabled = false; btn.textContent = "保存并检测解析";
    };

    document.getElementById("year").textContent = new Date().getFullYear();
    if (DOMAIN) { showGoto(DOMAIN); renderDns(DOMAIN); }
    poll();
    pollTimer = setInterval(poll, 10000);
})();
</script>
{% elif mode == 'preview' %}
<script>
    document.getElementById("year").textContent = new Date().getFullYear();
    function showToast(m) {
        var t = document.getElementById("toast");
        t.innerText = m;
        t.classList.add("show");
        clearTimeout(window.toastTimer);
        window.toastTimer = setTimeout(function() { t.classList.remove("show"); }, 2200);
    }
    function previewBlocked() { showToast("未检测到域名配置，无法生成临时邮箱"); }
</script>
{% else %}
<div id="custom-modal" class="modal-overlay" onclick="closeModalOnOverlay(event)">
    <div class="modal-box">
        <h3>自定义邮箱前缀</h3>
        <p>输入喜欢的前缀（仅小写字母和数字，1~20 位），生成个性化临时邮箱地址。</p>
        <div class="modal-input-row">
            <input type="text" id="custom-prefix-input" class="modal-input" placeholder="your-name" maxlength="20" autocomplete="off" spellcheck="false">
            <span class="modal-suffix" id="modal-domain-suffix">@...</span>
        </div>
        <div class="modal-hint">有效期内该前缀为你独占</div>
        <div class="modal-error" id="modal-error"></div>
        <div class="modal-actions">
            <button class="btn btn-secondary" onclick="closeCustomModal()">取消</button>
            <button class="btn btn-accent" id="modal-submit-btn" onclick="submitCustomPrefix()">确认创建</button>
        </div>
    </div>
</div>
<script>
    var currentEmail = null, openedMailId = null, lastSig = "", knownIds = {};
    var mailboxCreatedAt = 0, currentDomain = "", isFirstLoad = true;
    document.getElementById("year").textContent = new Date().getFullYear();

    function showToast(m, email) {
        var t = document.getElementById("toast");
        if (email) {
            t.innerHTML = '<div class="toast-title">' + esc(m) + '</div><div class="toast-email">' + esc(email) + '</div>';
        } else {
            t.innerText = m;
        }
        t.classList.add("show");
        clearTimeout(window.toastTimer);
        window.toastTimer = setTimeout(function() { t.classList.remove("show"); }, email ? 3200 : 2000);
    }

    function setSyncStatus() {
        var el = document.getElementById("sync-status");
        if (el) el.innerText = "实时同步中 " + new Date().toLocaleTimeString();
    }

    function setCount(n) {
        var el = document.getElementById("mail-count");
        if (el) el.innerText = n;
    }

    function copyEmail() {
        if (!currentEmail) return;
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(currentEmail).then(function() { showToast("已复制: " + currentEmail); }).catch(fallbackCopy);
        } else { fallbackCopy(); }
    }

    function fallbackCopy() {
        var input = document.createElement("textarea");
        input.value = currentEmail;
        document.body.appendChild(input);
        input.select();
        try { document.execCommand("copy"); showToast("已复制: " + currentEmail); }
        catch (e) { showToast("复制失败，请手动选择复制"); }
        document.body.removeChild(input);
    }

    function openCustomModal() {
        if (currentEmail) currentDomain = "@" + currentEmail.split("@").slice(1).join("@");
        document.getElementById("modal-domain-suffix").innerText = currentDomain || "@...";
        document.getElementById("custom-prefix-input").value = "";
        document.getElementById("modal-error").innerText = "";
        document.getElementById("custom-prefix-input").classList.remove("error");
        document.getElementById("custom-modal").classList.add("show");
        setTimeout(function() { document.getElementById("custom-prefix-input").focus(); }, 100);
    }

    function closeCustomModal() { document.getElementById("custom-modal").classList.remove("show"); }
    function closeModalOnOverlay(e) { if (e.target === document.getElementById("custom-modal")) closeCustomModal(); }

    document.getElementById("custom-prefix-input").addEventListener("input", function() {
        this.value = this.value.toLowerCase().replace(/[^a-z0-9]/g, "");
        document.getElementById("modal-error").innerText = "";
        this.classList.remove("error");
    });
    document.getElementById("custom-prefix-input").addEventListener("keydown", function(e) {
        if (e.key === "Enter") submitCustomPrefix();
    });

    function submitCustomPrefix() {
        var input = document.getElementById("custom-prefix-input");
        var prefix = input.value.trim().toLowerCase();
        var errorEl = document.getElementById("modal-error");
        if (!prefix) { errorEl.innerText = "请输入邮箱前缀"; input.classList.add("error"); return; }
        if (prefix.length < 1 || prefix.length > 20) { errorEl.innerText = "前缀长度需在 1~20 个字符之间"; input.classList.add("error"); return; }
        if (!/^[a-z0-9]+$/.test(prefix)) { errorEl.innerText = "前缀仅支持小写字母和数字"; input.classList.add("error"); return; }
        var btn = document.getElementById("modal-submit-btn");
        btn.disabled = true; btn.innerText = "创建中...";
        fetch("/api/mailbox/custom", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({prefix: prefix})
        }).then(function(r) { return r.json(); }).then(function(d) {
            btn.disabled = false; btn.innerText = "确认创建";
            if (d.ok) {
                currentEmail = d.email;
                mailboxCreatedAt = d.created_ts || Date.now();
                document.getElementById("mailbox-display").innerText = currentEmail;
                document.getElementById("email-list").innerHTML = '<div class="empty">暂无邮件，等待收件中...</div>';
                showToast("自定义邮箱已创建", currentEmail);
                knownIds = {}; lastSig = ""; openedMailId = null; isFirstLoad = true;
                setCount(0); setSyncStatus(); closeCustomModal();
            } else {
                errorEl.innerText = (d.error === "CONFLICT")
                    ? "该前缀已被其他用户使用中，请稍后再试或换一个前缀"
                    : (d.error || "创建失败，请重试");
                input.classList.add("error");
            }
        }).catch(function() {
            btn.disabled = false; btn.innerText = "确认创建";
            errorEl.innerText = "网络错误，请重试";
        });
    }

    function loadMailbox() {
        fetch("/api/mailbox").then(function(r) { return r.json(); }).then(function(d) {
            if (d.ok && d.email) {
                currentEmail = d.email;
                mailboxCreatedAt = d.created_ts || 0;
                document.getElementById("mailbox-display").innerText = currentEmail;
                if (mailboxCreatedAt && (Date.now() - mailboxCreatedAt < 5000)) {
                    document.getElementById("email-list").innerHTML = '<div class="empty">暂无邮件，等待收件中...</div>';
                    setCount(0); setSyncStatus();
                    knownIds = {}; lastSig = ""; isFirstLoad = true;
                } else {
                    checkAndLoadEmails();
                }
            } else {
                generateMailbox();
            }
        });
    }

    function checkAndLoadEmails() {
        fetch("/api/emails/check").then(function(r) { return r.json(); }).then(function(d) {
            if (d.ok && d.count > 0) {
                loadEmails(false);
            } else {
                document.getElementById("email-list").innerHTML = '<div class="empty">暂无邮件，等待收件中...</div>';
                setCount(0); setSyncStatus();
                knownIds = {}; lastSig = ""; isFirstLoad = true;
            }
        }).catch(function() { loadEmails(false); });
    }

    function generateMailbox() {
        fetch("/api/mailbox/generate", { method: "POST" }).then(function(r) { return r.json(); }).then(function(d) {
            if (d.ok) {
                currentEmail = d.email;
                mailboxCreatedAt = d.created_ts || Date.now();
                document.getElementById("mailbox-display").innerText = currentEmail;
                document.getElementById("email-list").innerHTML = '<div class="empty">暂无邮件，等待收件中...</div>';
                showToast("已生成新邮箱", currentEmail);
                knownIds = {}; lastSig = ""; openedMailId = null; isFirstLoad = true;
                setCount(0); setSyncStatus();
            }
        });
    }

    function markAsReadInstant(id) {
        var el = document.querySelector('.mail-item[data-id="' + id + '"]');
        if (el) {
            el.querySelectorAll('.badge').forEach(function(b) {
                b.className = 'badge badge-read';
                b.innerText = '已读';
            });
        }
    }

    function loadEmails(manual) {
        if (!currentEmail) return;
        fetch("/api/emails").then(function(r) { return r.json(); }).then(function(d) {
            if (!d.ok) return;
            var mails = d.emails || [];
            var sig = mails.map(function(m) { return m.id + "_" + m.is_read; }).join(",");
            if (!manual && sig === lastSig) { setSyncStatus(); updateCountdowns(); return; }
            if (!manual && !isFirstLoad) {
                var newCount = 0;
                for (var i = 0; i < mails.length; i++) { if (!knownIds[mails[i].id]) newCount++; }
                if (newCount > 0) showToast("收到 " + newCount + " 封新邮件");
            }
            if (!manual) isFirstLoad = false;
            lastSig = sig;
            var newKnownIds = {};
            mails.forEach(function(m) { newKnownIds[m.id] = true; });
            var listEl = document.getElementById("email-list");
            if (mails.length === 0) {
                listEl.innerHTML = '<div class="empty">暂无邮件，等待收件中...</div>';
            } else {
                if (listEl.querySelector('.empty')) listEl.innerHTML = '';
                var existingIds = {};
                listEl.querySelectorAll('.mail-item').forEach(function(el) { existingIds[el.getAttribute('data-id')] = el; });
                mails.forEach(function(m) {
                    var idStr = String(m.id);
                    if (existingIds[idStr]) {
                        updateMailItem(existingIds[idStr], m);
                        delete existingIds[idStr];
                    } else {
                        listEl.appendChild(createMailItem(m));
                    }
                });
                for (var id in existingIds) {
                    existingIds[id].remove();
                    if (openedMailId == id) openedMailId = null;
                }
                mails.forEach(function(m) {
                    var el = listEl.querySelector('.mail-item[data-id="' + m.id + '"]');
                    if (el) listEl.appendChild(el);
                });
            }
            knownIds = newKnownIds;
            setCount(mails.length);
            setSyncStatus();
            updateCountdowns();
        });
    }

    function createMailItem(m) {
        var div = document.createElement('div');
        div.className = 'mail-item';
        div.setAttribute('data-id', m.id);
        var isOpen = (openedMailId == m.id);
        if (isOpen) div.classList.add('open');
        div.innerHTML = '<div class="mail-head" onclick="toggleMail(' + m.id + ')">' +
            '<div class="mail-line-pc">' +
            '<div class="mail-subject">' + esc(m.subject || "(无标题)") + '</div>' +
            '<div class="mail-sender">' + esc(m.sender) + '</div>' +
            '<div class="mail-right">' +
            '<span class="cd-timer" data-remain="' + m.remaining_seconds + '">--</span>' +
            '<span>' + m.received_at.substring(11, 16) + '</span>' +
            '<span class="badge ' + (m.is_read ? 'badge-read' : 'badge-unread') + '">' + (m.is_read ? '已读' : '未读') + '</span>' +
            '</div></div>' +
            '<div class="mail-line-mobile">' +
            '<div class="mail-mobile-row1">' +
            '<div class="mail-subject">' + esc(m.subject || "(无标题)") + '</div>' +
            '<div class="mail-time">' + m.received_at.substring(11, 16) + '</div>' +
            '</div>' +
            '<div class="mail-mobile-row2">' +
            '<div class="mail-sender">' + esc(m.sender) + '</div>' +
            '<div class="mail-right">' +
            '<span class="cd-timer" data-remain="' + m.remaining_seconds + '">--</span>' +
            '<span class="badge ' + (m.is_read ? 'badge-read' : 'badge-unread') + '">' + (m.is_read ? '已读' : '未读') + '</span>' +
            '</div></div></div></div>' +
            '<div id="body-' + m.id + '" class="mail-body" style="display:' + (isOpen ? 'block' : 'none') + '">' +
            renderBody(m) + '</div>';
        return div;
    }

    function updateMailItem(el, m) {
        var badgeClass = m.is_read ? 'badge-read' : 'badge-unread';
        var badgeText = m.is_read ? '已读' : '未读';
        el.querySelectorAll('.badge').forEach(function(b) { b.className = 'badge ' + badgeClass; b.innerText = badgeText; });
        el.querySelectorAll('.cd-timer').forEach(function(t) { t.setAttribute('data-remain', m.remaining_seconds); });
    }

    function renderBody(m) {
        var meta = '<table class="meta-table"><tr>' +
            '<th>发件人</th><td>' + esc(m.sender) + '</td>' +
            '<th>时间</th><td>' + m.received_at + '</td>' +
            '</tr></table>';
        var att = "";
        if (m.attachments && m.attachments.length > 0) {
            att = '<div class="attachments"><b>附件</b>&nbsp;&nbsp;';
            m.attachments.forEach(function(a) {
                if (a.data_uri) att += '<a href="' + a.data_uri + '" download="' + esc(a.filename) + '">' + esc(a.filename) + '</a>';
            });
            att += '</div>';
        }
        var content = m.html_body
            ? '<iframe srcdoc="' + esc(m.html_body) + '" onload="injectIframeStyle(this); autoResize(this)"></iframe>'
            : '<pre>' + esc(m.text_body || "(无正文)") + '</pre>';
        return meta + att + content;
    }

    function injectIframeStyle(iframe) {
        try {
            var doc = iframe.contentDocument || iframe.contentWindow.document;
            var style = doc.createElement('style');
            style.innerHTML = '' +
                'img { max-width: 100% !important; height: auto !important; cursor: zoom-in; border-radius: 6px; margin: 8px 0; } ' +
                'body { font-family: inherit; margin: 0; padding: 12px; color: #1f2937; } ' +
                'a { color: #2563eb; text-decoration: none !important; cursor: pointer; } ' +
                '.link-open-tip { display: none; position: fixed; z-index: 99999; background: #111827; color: #fff; ' +
                'font-size: 12px; padding: 6px 12px; border-radius: 7px; white-space: nowrap; box-shadow: 0 6px 18px rgba(0,0,0,.18); ' +
                'line-height: 1.6; align-items: center; gap: 10px; pointer-events: auto; cursor: default; } ' +
                '.link-open-tip.visible { display: inline-flex; } ' +
                '.link-open-tip .tip-text { color: #9ca3af; font-size: 11px; } ' +
                '.link-open-tip .tip-btn { background: #6366f1; color: #fff; border: none; padding: 3px 11px; ' +
                'border-radius: 5px; font-size: 12px; cursor: pointer; font-weight: 500; white-space: nowrap; } ' +
                '.link-open-tip .tip-btn:hover { background: #4f46e5; }';
            doc.head.appendChild(style);

            var tipEl = doc.createElement('div');
            tipEl.className = 'link-open-tip';
            tipEl.innerHTML = '<span class="tip-text">\u70B9\u51FB\u53EF\u76F4\u63A5\u8BBF\u95EE</span><button class="tip-btn">\u2197\uFE0E \u65B0\u7A97\u53E3\u6253\u5F00</button>';
            doc.body.appendChild(tipEl);

            var hideTimer = null, currentHref = '';

            function showTip(link) {
                if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
                currentHref = link.href;
                var rect = link.getBoundingClientRect();
                tipEl.style.left = (rect.right + 8) + 'px';
                tipEl.style.top = (rect.top + rect.height / 2 - 16) + 'px';
                tipEl.classList.add('visible');
            }
            function hideTip() {
                hideTimer = setTimeout(function() { tipEl.classList.remove('visible'); hideTimer = null; }, 2000);
            }
            tipEl.addEventListener('mouseenter', function() { if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; } });
            tipEl.addEventListener('mouseleave', hideTip);
            tipEl.querySelector('.tip-btn').addEventListener('click', function(e) {
                e.preventDefault(); e.stopPropagation();
                if (currentHref) window.open(currentHref, '_blank', 'noopener,noreferrer');
                tipEl.classList.remove('visible');
            });

            var imgs = doc.getElementsByTagName('img');
            for (var i = 0; i < imgs.length; i++) {
                imgs[i].onclick = (function(src) { return function(e) { e.stopPropagation(); showImage(src); }; })(imgs[i].src);
            }

            var links = doc.getElementsByTagName('a');
            for (var j = 0; j < links.length; j++) {
                (function(link) {
                    link.setAttribute('target', '_blank');
                    link.setAttribute('rel', 'noopener noreferrer');
                    link.addEventListener('mouseenter', function() { showTip(link); });
                    link.addEventListener('mouseleave', hideTip);
                    link.addEventListener('click', function(e) {
                        e.preventDefault(); e.stopPropagation();
                        if (link.href) window.open(link.href, '_blank', 'noopener,noreferrer');
                    });
                })(links[j]);
            }

            // 图片加载 / 内容尺寸变化时自动重算高度, 保证不留空白
            for (var k = 0; k < imgs.length; k++) {
                if (!imgs[k].complete) imgs[k].addEventListener('load', function() { autoResize(iframe); });
            }
            if (window.ResizeObserver) {
                try {
                    if (iframe.__ro) { iframe.__ro.disconnect(); }
                    var ro = new ResizeObserver(function() { autoResize(iframe); });
                    ro.observe(doc.body);
                    iframe.__ro = ro;
                } catch (e) {}
            }
            try { iframe.contentWindow.addEventListener('load', function() { autoResize(iframe); }); } catch (e) {}
        } catch(e) {}
    }

    function showImage(src) {
        document.getElementById("img-viewer-src").src = src;
        document.getElementById("img-viewer").classList.add("show");
    }

    function toggleMail(id) {
        var item = document.querySelector('.mail-item[data-id="' + id + '"]');
        if (openedMailId == id) {
            document.getElementById("body-" + id).style.display = "none";
            if (item) item.classList.remove('open');
            openedMailId = null;
            return;
        }
        if (openedMailId !== null) {
            var oldBody = document.getElementById("body-" + openedMailId);
            if (oldBody) oldBody.style.display = "none";
            var oldItem = document.querySelector('.mail-item[data-id="' + openedMailId + '"]');
            if (oldItem) oldItem.classList.remove('open');
        }
        document.getElementById("body-" + id).style.display = "block";
        if (item) item.classList.add('open');
        openedMailId = id;
        // 展开后重新按内容高度调整 iframe (折叠时测不到真实高度)
        var openedBody = document.getElementById("body-" + id);
        requestAnimationFrame(function() { resizeFrames(openedBody); });
        setTimeout(function() { resizeFrames(openedBody); }, 60);
        setTimeout(function() { resizeFrames(openedBody); }, 300);
        markAsReadInstant(id);
        fetch("/api/emails/" + id + "/read", { method: "POST" });
    }

    // 自动识别邮件正文实际高度, 避免正文下方出现大块空白
    function autoResize(f) {
        if (!f || !f.contentWindow) return;
        try {
            var doc = f.contentWindow.document;
            if (!doc || !doc.body) return;
            // 折叠状态量不到真实高度, 展开后再测量
            if (!f.offsetParent) return;
            var bodyH = doc.body.scrollHeight || 0;
            var docH = doc.documentElement ? (doc.documentElement.scrollHeight || 0) : 0;
            var h = Math.max(bodyH, docH) + 2;
            if (h < 48) h = 48;
            if (h > 900) h = 900;               // 超长正文限制最大高度, 框内滚动
            var last = parseFloat(f.getAttribute("data-h") || "0");
            if (Math.abs(h - last) < 2) return;  // 高度未变化则不重排, 防止抖动
            f.setAttribute("data-h", h);
            f.style.height = h + "px";
        } catch (e) {}
    }

    // 展开邮件后重新测量该邮件内所有 iframe 高度
    function resizeFrames(root) {
        if (!root) return;
        var frames = root.querySelectorAll("iframe");
        for (var i = 0; i < frames.length; i++) { autoResize(frames[i]); }
    }

    function updateCountdowns() {
        var nodes = document.getElementsByClassName("cd-timer");
        for (var i = 0; i < nodes.length; i++) {
            var rem = parseInt(nodes[i].getAttribute("data-remain") || 0);
            if (rem <= 0) { nodes[i].innerText = "已过期"; continue; }
            var m = Math.floor(rem / 60), s = rem % 60;
            nodes[i].innerText = m + "分" + (s < 10 ? "0" : "") + s + "秒";
        }
    }

    function esc(t) {
        return String(t || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    loadMailbox();
    setInterval(function() { loadEmails(false); }, 5000);
    setInterval(function() {
        var nodes = document.getElementsByClassName("cd-timer");
        for (var i = 0; i < nodes.length; i++) {
            var rem = parseInt(nodes[i].getAttribute("data-remain") || 0);
            if (rem > 0) nodes[i].setAttribute("data-remain", rem - 1);
        }
        updateCountdowns();
    }, 1000);
</script>
{% endif %}
</body>
</html>
"""


# ============================================================================
#  访问守卫：只放行「已配置且解析到本机」的主域名，禁止他人随意反代
# ============================================================================

BLOCKED_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>未授权域名 · TempMail</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#fff;font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:#0f172a}
.box{max-width:560px;margin:24px;padding:30px 32px;border:1px solid #e8ecf4;border-radius:16px;
box-shadow:0 12px 40px rgba(15,23,42,.06)}
h1{margin:0 0 10px;font-size:19px;color:#b45309}
p{margin:6px 0;color:#475569;font-size:13.5px}
code{background:#f4f6fb;border:1px solid #e8ecf4;border-radius:6px;padding:2px 7px;font-size:12.5px;color:#334155}
.foot{margin-top:18px;padding-top:14px;border-top:1px solid #eef1f7;font-size:12px;color:#8a94a6}
.foot a{color:#0891b2;text-decoration:none}
.foot a:hover{text-decoration:none;opacity:.85}
</style>
</head>
<body>
<div class="box">
  <h1>该域名未授权使用本服务</h1>
  <p>当前访问域名 <code>{{ host }}</code> 不是本实例绑定的主域名。</p>
  <p>本程序仅允许部署者已配置的主域名（{{ domain or '尚未配置' }}）反向代理访问，
     禁止其它站点随意反代使用。</p>
  <p>如果你是该服务器的管理员，请通过 <code>IP:{{ port }}</code> 打开配置页面完成主域名绑定。</p>
  <div class="foot">
    Copyright (C) 2026 atusu.cn ·
    <a href="{{ site_url }}" target="_blank" rel="noopener">atusu.cn/mail</a>
  </div>
</div>
</body>
</html>
"""


@app.before_request
def guard_request():
    """拦截未授权域名：非主域名的 Host 一律拒绝（API 返回 403 JSON）"""
    path = request.path or "/"
    mode, host, is_ip, domain = resolve_mode()
    request.environ["tempmail.mode"] = mode

    if mode == "blocked":
        if path.startswith("/api/"):
            return jsonify({"ok": False, "error": "该域名未授权使用本服务", "code": "domain_not_allowed"}), 403
        return render_template_string(
            BLOCKED_HTML, host=host, domain=domain, port=APP_PORT, site_url=SITE_URL
        ), 403

    if path.startswith("/api/") and not path.startswith("/api/setup"):
        if mode == "setup":
            return jsonify({"ok": False, "error": "请先配置主域名", "code": "setup_required"}), 403
        if mode == "preview":
            return jsonify({"ok": False, "error": "预览模式不可用", "code": "preview_only"}), 403
    return None


@app.route("/api/setup/status")
def api_setup_status():
    """配置页轮询接口（前端每 10 秒调用一次）"""
    mode, host, is_ip, domain = resolve_mode()
    if mode == "blocked":
        return jsonify({"ok": False, "error": "该域名未授权使用本服务"}), 403

    cfg = load_config()
    active = bool(domain) and (bool(cfg.get("active")) or bool(PRIMARY_DOMAIN_ENV))
    resolved, ips, reason = False, [], ""
    if domain and not active:
        resolved, ips, reason = check_domain_resolution(domain)
        if resolved:
            save_config(active=True, verified_at=now_text(), ips=ips)
            active = True
            _domain_ready.set()

    return jsonify({
        "ok": True,
        "configured": bool(domain),
        "active": active,
        "domain": domain,
        "resolved": resolved if domain and not active else active,
        "ips": ips,
        "reason": reason,
        "host": host,
        "is_ip": is_ip,
        "port": APP_PORT,
        "public_ip": get_public_ip(budget=6) or "",
        "target_url": setup_target_url(domain) if domain else "",
    })


@app.route("/api/setup/domain", methods=["POST"])
def api_setup_domain():
    """保存主域名：DNS 解析指向本机才激活"""
    mode, host, is_ip, domain = resolve_mode()
    if mode == "blocked":
        return jsonify({"ok": False, "error": "该域名未授权使用本服务"}), 403

    data = request.get_json(silent=True) or {}
    value = _normalize_domain(data.get("domain") or request.form.get("domain", ""))
    if not _valid_domain(value):
        return jsonify({"ok": False, "active": False, "domain": value,
                        "message": "域名格式不正确，请填写如 mail.example.com"}), 400

    resolved, ips, reason = check_domain_resolution(value)
    # 回车 / 点击保存即生成并落盘服务器指纹，随本次域名记录一起上报
    get_fingerprint()
    save_config(domain=value, active=bool(resolved), configured_at=now_text(),
                verified_at=now_text() if resolved else "", ips=ips)
    if resolved:
        _domain_ready.set()
    # 配置了域名就追加一条 domain 记录（带 IP + 域名 + 指纹），失败由后台线程重试
    maybe_report_domain(value)

    return jsonify({
        "ok": True,
        "configured": True,
        "active": bool(resolved),
        "domain": value,
        "resolved": bool(resolved),
        "ips": ips,
        "message": "" if resolved else (reason or "解析未生效，页面会每 10 秒自动复检"),
        "target_url": setup_target_url(value),
    })


@app.route("/")
def index():
    mode, host, is_ip, domain = resolve_mode()
    public_ip = ""
    if mode in ("setup", "preview"):
        public_ip = get_public_ip(budget=5) or "未获取到公网 IP（可在 .env 设置 PUBLIC_IP=x.x.x.x 指定）"
    if mode == "normal":
        # 已授权域名访问 -> 唤醒上报线程补报域名记录（静默）
        _domain_ready.set()
    plan = dns_plan(domain)
    return render_template_string(
        INDEX_HTML, style=STYLE, mode=mode, host=host, port=APP_PORT,
        retention=FRONT_RETENTION_MINUTES, public_ip=public_ip, github_url=GITHUB_URL,
        site_url=SITE_URL, mailbox_hours=MAILBOX_VALIDITY_HOURS, smtp_port=SMTP_PORT,
        developer_name=DEVELOPER_NAME, developer_url=DEVELOPER_URL,
        developer_link=DEVELOPER_LINK,
        license_title=LICENSE_TITLE, copyright_text=COPYRIGHT,
        app_name=APP_NAME, app_version=APP_VERSION,
        configured_domain=domain, is_ip=is_ip,
        domain_active=is_domain_active() if mode == "setup" else False,
        dns_a_host=plan["a_host"], dns_mx_value=plan["mx_value"], dns_hint=plan["hint"],
        setup_target=(setup_target_url(domain) if domain else "")
    )


@app.route("/api/mailbox")
def api_mailbox():
    domain, is_ip = get_current_domain()
    if is_ip:
        return jsonify({"ok": False, "error": "请通过域名访问"}), 400
    token = get_client_token()
    deactivate_expired_mailboxes()
    mailbox = get_client_mailbox(token, domain)
    resp_data = {"ok": True, "email": None, "created_ts": 0}
    if mailbox:
        resp_data["email"] = mailbox["email"]
        try:
            ct = datetime.strptime(mailbox["created_at"], "%Y-%m-%d %H:%M:%S")
            resp_data["created_ts"] = int(ct.timestamp() * 1000)
        except Exception:
            pass
    resp = make_response(jsonify(resp_data))
    return set_client_cookie(resp, token)


@app.route("/api/mailbox/generate", methods=["POST"])
def api_generate():
    domain, is_ip = get_current_domain()
    if is_ip:
        return jsonify({"ok": False, "error": "请通过域名访问"}), 400
    token = get_client_token()
    deactivate_expired_mailboxes()
    try:
        email = create_mailbox(token, domain)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    resp = make_response(jsonify({"ok": True, "email": email, "created_ts": int(time.time() * 1000)}))
    return set_client_cookie(resp, token)


@app.route("/api/mailbox/custom", methods=["POST"])
def api_custom():
    domain, is_ip = get_current_domain()
    if is_ip:
        return jsonify({"ok": False, "error": "请通过域名访问"}), 400
    data = request.get_json(silent=True) or {}
    prefix = (data.get("prefix") or "").strip().lower()
    if not prefix:
        return jsonify({"ok": False, "error": "请输入邮箱前缀"})
    if len(prefix) < 1 or len(prefix) > 20:
        return jsonify({"ok": False, "error": "前缀长度需在 1~20 个字符之间"})
    if not re.match(r'^[a-z0-9]+$', prefix):
        return jsonify({"ok": False, "error": "前缀仅支持小写字母和数字"})
    token = get_client_token()
    deactivate_expired_mailboxes()
    try:
        email = create_mailbox(token, domain, custom_prefix=prefix)
    except ValueError as e:
        if str(e) == "CONFLICT":
            return jsonify({"ok": False, "error": "CONFLICT"})
        return jsonify({"ok": False, "error": str(e)})
    except Exception as e:
        return jsonify({"ok": False, "error": "创建失败: " + str(e)})
    resp = make_response(jsonify({"ok": True, "email": email, "created_ts": int(time.time() * 1000)}))
    return set_client_cookie(resp, token)


@app.route("/api/emails/check")
def api_emails_check():
    domain, is_ip = get_current_domain()
    if is_ip:
        return jsonify({"ok": False}), 400
    token = get_client_token()
    mailbox = get_client_mailbox(token, domain)
    if not mailbox:
        return jsonify({"ok": True, "count": 0})
    cutoff = (datetime.now() - timedelta(minutes=FRONT_RETENTION_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    with db_connect() as db:
        row = db.execute(
            "SELECT COUNT(*) as cnt FROM emails WHERE mailbox_email = ? AND received_at >= ?",
            (mailbox["email"], cutoff)
        ).fetchone()
    return jsonify({"ok": True, "count": row["cnt"] if row else 0})


@app.route("/api/emails")
def api_emails():
    domain, is_ip = get_current_domain()
    if is_ip:
        return jsonify({"ok": False, "error": "请通过域名访问"}), 400
    token = get_client_token()
    mailbox = get_client_mailbox(token, domain)
    if not mailbox:
        return jsonify({"ok": False, "error": "无邮箱"}), 400
    cutoff = (datetime.now() - timedelta(minutes=FRONT_RETENTION_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    now = datetime.now()
    with db_connect() as db:
        rows = db.execute(
            "SELECT e.id, e.sender, e.subject, e.text_body, e.html_body, e.attachments, e.received_at, "
            "CASE WHEN r.email_id IS NULL THEN 0 ELSE 1 END AS is_read "
            "FROM emails e LEFT JOIN email_reads r ON r.email_id = e.id AND r.client_token = ? "
            "WHERE e.mailbox_email = ? AND e.received_at >= ? ORDER BY e.id DESC",
            (token, mailbox["email"], cutoff)
        ).fetchall()
    emails = []
    for r in rows:
        d = dict(r)
        try:
            d["attachments"] = json.loads(d["attachments"] or "[]")
        except Exception:
            d["attachments"] = []
        try:
            recv_time = datetime.strptime(d["received_at"], "%Y-%m-%d %H:%M:%S")
            expire_time = recv_time + timedelta(minutes=FRONT_RETENTION_MINUTES)
            d["remaining_seconds"] = max(0, int((expire_time - now).total_seconds()))
        except Exception:
            d["remaining_seconds"] = 0
        emails.append(d)
    return jsonify({"ok": True, "emails": emails})


@app.route("/api/emails/<int:email_id>/read", methods=["POST"])
def api_read(email_id):
    token = get_client_token()
    domain, _ = get_current_domain()
    mailbox = get_client_mailbox(token, domain)
    if not mailbox:
        return jsonify({"ok": False}), 400
    with db_lock:
        with db_connect() as db:
            db.execute("INSERT OR REPLACE INTO email_reads(client_token, email_id, read_at) VALUES (?, ?, ?)", (token, email_id, now_text()))
    return jsonify({"ok": True})


class SMTPHandler:
    async def handle_DATA(self, server, session, envelope):
        try:
            raw_message = envelope.content
            if isinstance(raw_message, str):
                raw_message = raw_message.encode('utf-8', errors='replace')
            if len(raw_message) > MAX_EMAIL_SIZE:
                return "552 Message too large"
            message = BytesParser(policy=policy.default).parsebytes(raw_message)
            recipients = list(envelope.rcpt_tos or [])
            valid_emails = []
            for rcpt in recipients:
                addr = parseaddr(str(rcpt))[1].lower()
                if "@" in addr:
                    with db_connect() as db:
                        if db.execute("SELECT id FROM mailboxes WHERE email = ? AND active = 1", (addr,)).fetchone():
                            valid_emails.append(addr)
            if not valid_emails:
                return "250 OK"
            sender = parseaddr(message.get("From", ""))[1]
            subject = str(message.get("Subject", ""))
            text_body, html_body, attachments_json = extract_body(message)
            received_at = now_text()
            with db_lock:
                with db_connect() as db:
                    for email in valid_emails:
                        db.execute(
                            "INSERT INTO emails(mailbox_email, sender, recipients, subject, text_body, html_body, attachments, raw_size, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (email, sender, ", ".join(valid_emails), subject, text_body, html_body, attachments_json, len(raw_message), received_at)
                        )
                        db.execute("UPDATE mailboxes SET last_received_at = ? WHERE email = ?", (received_at, email))
            print(f"[SMTP] Received mail for {valid_emails} from {sender}")
            return "250 OK"
        except Exception as e:
            print(f"[SMTP Error] {e}")
            import traceback
            traceback.print_exc()
            return "451 Temporary error"


def smtp_bind_host():
    """aiosmtpd 在 Windows 上绑定 "0.0.0.0" 会抛 WinError 10049,
    而传入 "" 表示监听全部网卡 (aiosmtpd 内部 getaddrinfo AI_PASSIVE),
    Linux / macOS / Windows 均可用。"""
    host = (SMTP_HOST or "").strip()
    if host in ("0.0.0.0", "*", "::", "[::]", "all", "ALL"):
        return ""
    return host


def start_smtp_server():
    controller = Controller(
        SMTPHandler(),
        hostname=smtp_bind_host(),
        port=SMTP_PORT,
        decode_data=False
    )
    controller.start()
    return controller


def cleanup_worker():
    while True:
        try:
            cutoff_mailbox = (datetime.now() - timedelta(hours=MAILBOX_VALIDITY_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
            cutoff_front = (datetime.now() - timedelta(minutes=FRONT_RETENTION_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
            cutoff_back = (datetime.now() - timedelta(days=BACKEND_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
            with db_lock:
                with db_connect() as db:
                    db.execute("UPDATE mailboxes SET active = 0 WHERE active = 1 AND created_at < ?", (cutoff_mailbox,))
                    db.execute("DELETE FROM email_reads WHERE read_at < ?", (cutoff_front,))
                    db.execute("DELETE FROM emails WHERE received_at < ?", (cutoff_back,))
                    db.execute("DELETE FROM mailboxes WHERE active = 0 AND created_at < ?", (cutoff_back,))
        except Exception as e:
            print(f"[Cleanup Error] {e}")
        time.sleep(60)


if __name__ == "__main__":
    # 软件信息 / 版权信息 (--version / --license)
    if "--version" in sys.argv or "-v" in sys.argv:
        print("{0} v{1}".format(APP_NAME, APP_VERSION))
        print("Developer: {0}".format(DEVELOPER_LINK))
        print("License  : {0}".format(LICENSE_TITLE))
        print(COPYRIGHT)
        sys.exit(0)
    if "--license" in sys.argv:
        print("{0} v{1}  |  {2}".format(APP_NAME, APP_VERSION, LICENSE_TITLE))
        print(COPYRIGHT)
        print("开发者: {0}".format(DEVELOPER_LINK))
        print("")
        print("本程序为开源版本，仅限个人学习与自用；")
        print("严禁任何形式的商业售卖、付费分发、二次销售或作为付费服务的一部分。")
        print("完整条款见项目目录 LICENSE 文件。")
        sys.exit(0)

    init_db()
    if CLEAN_ON_START:
        clean_all_data()

    # 自动识别 Web 端口是否被占用，被占用则自动递增换端口
    new_port, port_changed = ensure_free_port(APP_HOST, APP_PORT)
    if port_changed:
        print(f"[Port] ✅ 原端口 {APP_PORT} 被占用，已自动切换到 {new_port}")
        APP_PORT = new_port
    write_runtime_port(APP_PORT)

    print("=" * 54)
    print("📬 TempMail Service Started (v{})".format(APP_VERSION))
    print(f"🌐 Web: http://127.0.0.1:{APP_PORT}")
    print(f"📥 SMTP: {SMTP_HOST}:{SMTP_PORT}")
    print(f"⏱️  Frontend Retention: {FRONT_RETENTION_MINUTES} min")
    print(f"🗑️  Backend Cleanup: {BACKEND_RETENTION_DAYS} days")
    print(f"📦 Mailbox Validity: {MAILBOX_VALIDITY_HOURS} hours")
    print(f"🧹 Clean On Start: {'Enabled' if CLEAN_ON_START else 'Disabled'}")
    print("-" * 54)
    print(f"© 2026 {APP_NAME}  |  {LICENSE_TITLE}")
    print(f"Developer: {DEVELOPER_LINK}")
    print("=" * 54)

    threading.Thread(target=cleanup_worker, daemon=True).start()
    # 后台预热公网 IP 缓存（静默，不输出任何日志）
    threading.Thread(target=get_public_ip, kwargs={"force": True, "budget": 60}, daemon=True).start()
    # 新部署上报（后台静默，不阻塞启动）
    threading.Thread(target=report_worker, daemon=True, name="deploy-report").start()

    try:
        smtp_controller = start_smtp_server()
        print(f"[SMTP] ✅ SMTP started on {SMTP_HOST}:{SMTP_PORT}")
    except OSError as e:
        err_str = str(e).lower()
        if "address already in use" in err_str or "errno 98" in err_str or "errno 48" in err_str:
            print(f"[SMTP] ❌ Port {SMTP_PORT} is already in use!")
            print(f"[SMTP] 💡 Check: sudo lsof -i:{SMTP_PORT} or sudo ss -tlnp | grep :{SMTP_PORT}")
            print(f"[SMTP] ⚠️  Web service is still available but cannot receive emails.")
        else:
            print(f"[SMTP] ❌ Failed to start: {e}")
    except Exception as e:
        print(f"[SMTP] ❌ Exception: {e}")
    try:
        app.run(host=APP_HOST, port=APP_PORT, debug=False, use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        print("\nStopping service...")
    finally:
        if smtp_controller:
            smtp_controller.stop()
