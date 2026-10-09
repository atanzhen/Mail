# -*- coding: utf-8 -*-
import os, re, json, base64, secrets, sqlite3, threading, time, urllib.request
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr

from flask import Flask, request, jsonify, render_template_string, make_response
from aiosmtpd.controller import Controller
from werkzeug.middleware.proxy_fix import ProxyFix

# ================= 基础配置 =================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.getenv("DB_FILE", os.path.join(BASE_DIR, "temp_mail.db"))
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "8080"))
SMTP_HOST = os.getenv("SMTP_HOST", "0.0.0.0")
SMTP_PORT = int(os.getenv("SMTP_PORT", "25"))
MAX_EMAIL_SIZE = int(os.getenv("MAX_EMAIL_SIZE", str(10 * 1024 * 1024)))
GITHUB_URL = os.getenv("GITHUB_URL", "https://github.com/atanzhen/Mail")

FRONT_RETENTION_MINUTES = 10      # 前端展示保留时间(分钟)
BACKEND_RETENTION_DAYS = 7        # 数据库物理清理时间(天)

db_lock = threading.RLock()
smtp_controller = None

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# ================= 数据库操作 =================
def db_connect():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    db = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
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
            db.execute("CREATE INDEX IF NOT EXISTS idx_em_mb ON emails(mailbox_email)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_em_received ON emails(received_at)")

def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def generate_prefix():
    return re.sub(r"[^a-zA-Z0-9]", "", secrets.token_urlsafe(12))[:10].lower()

def get_client_token():
    """获取或生成客户端唯一指纹Token"""
    token = request.cookies.get("client_token")
    if not token or len(token) < 32:
        token = secrets.token_urlsafe(32)
    return token

def set_client_cookie(response, token):
    response.set_cookie("client_token", token, max_age=60*60*24*365, httponly=True, samesite="Lax", path="/")
    return response

def get_client_mailbox(token, domain):
    with db_connect() as db:
        return db.execute(
            "SELECT * FROM mailboxes WHERE client_token = ? AND email LIKE ? AND active = 1 ORDER BY id DESC LIMIT 1", 
            (token, f"%@{domain}")
        ).fetchone()

def create_mailbox(token, domain):
    with db_lock:
        with db_connect() as db:

            db.execute("UPDATE mailboxes SET active = 0 WHERE client_token = ?", (token,))
            email = ""
            for _ in range(30):
                prefix = generate_prefix()
                email = f"{prefix}@{domain}"
                if not db.execute("SELECT id FROM mailboxes WHERE email = ?", (email,)).fetchone(): 
                    break
            if not email: 
                raise RuntimeError("生成邮箱失败，请重试")
            db.execute(
                "INSERT INTO mailboxes(email, client_token, active, created_at) VALUES (?, ?, 1, ?)", 
                (email, token, now_text())
            )
    return email

def get_public_ip():
    for url in ["https://api.ipify.org", "https://ifconfig.me/ip", "https://ipv4.icanhazip.com"]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
            ip = urllib.request.urlopen(req, timeout=3).read().decode("utf-8").strip()
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip): return ip
        except: pass
    return "获取失败 (请自行查看服务器公网 IP)"

# ================= 邮件解析 =================
def extract_body(message):
    text_parts, html_parts, cid_map, attachments = [], [], {}, []
    for part in message.walk():
        if part.is_multipart(): continue
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
        except: 
            content = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", errors="replace")
        
        if ct == "text/plain": text_parts.append(str(content))
        elif ct == "text/html": html_parts.append(str(content))

    text_body, html_body = "\n\n".join(text_parts), "\n\n".join(html_parts)
    for c, uri in cid_map.items(): 
        html_body = html_body.replace(f"cid:{c}", uri)
    return text_body, html_body, json.dumps(attachments, ensure_ascii=False)

# ================= 动态域名检测 =================
def get_current_domain():
    host = request.host.split(":")[0].strip().lower()
    is_ip = bool(re.match(r'^\d{1,3}(\.\d{1,3}){3}$', host)) or host in ("localhost", "127.0.0.1")
    return host, is_ip

# ================= 前端模板 =================
STYLE = r"""
<style>
:root {
    --bg: #ffffff; --card: #ffffff; --border: #e2e8f0;
    --text: #0f172a; --text-muted: #64748b; --accent: #2563eb; --accent-hover: #1d4ed8;
    --success: #10b981; --warn: #f59e0b; --danger: #ef4444;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; min-height: 100vh; padding-bottom: 60px; }
.container { max-width: 960px; margin: 0 auto; padding: 24px 16px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }

.header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.header-left { display: flex; align-items: center; gap: 12px; }
.header-left h1 { font-size: 22px; font-weight: 700; color: var(--text); white-space: nowrap; }
.header-left p { color: var(--text-muted); font-size: 14px; }
.header-right { display: flex; align-items: center; gap: 16px; font-size: 13px; color: var(--text-muted); }
.header-right .sync-status { color: var(--accent); font-weight: 500; font-size: 12px; }

.mailbox-row { display: flex; gap: 12px; align-items: stretch; }
.mailbox-box { flex: 1; background: #f8fafc; border: 1px dashed var(--accent); padding: 14px; border-radius: 8px; font-size: 18px; font-weight: 600; color: var(--text); word-break: break-all; display: flex; align-items: center; justify-content: center; min-height: 50px; }
.btn { padding: 0 20px; border: none; border-radius: 8px; font-size: 14px; font-weight: 500; cursor: pointer; transition: all 0.2s; display: flex; align-items: center; justify-content: center; gap: 6px; white-space: nowrap; }
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover { background: var(--accent-hover); }
.btn-secondary { background: #f1f5f9; color: #334155; border: 1px solid var(--border); }
.btn-secondary:hover { background: #e2e8f0; }
.btn-sm { padding: 6px 14px; font-size: 12px; }

.inbox-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.inbox-header h2 { font-size: 18px; font-weight: 600; color: var(--text); }

.mail-item { border: 1px solid var(--border); border-radius: 8px; margin-bottom: 10px; overflow: hidden; transition: all 0.2s; }
.mail-item:hover { border-color: #cbd5e1; box-shadow: 0 2px 4px rgba(0,0,0,0.02); }
.mail-head { padding: 14px 16px; cursor: pointer; background: #fafafa; }
.mail-line-pc { display: flex; align-items: center; gap: 16px; }
.mail-subject { font-weight: 600; color: var(--text); flex: 2; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mail-sender { color: var(--text-muted); font-size: 13px; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mail-right { display: flex; align-items: center; gap: 12px; flex-shrink: 0; font-size: 12px; color: var(--text-muted); }
.badge { padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.badge-unread { background: #fef3c7; color: #92400e; }
.badge-read { background: #dcfce7; color: #166534; }
.cd-timer { color: var(--accent); font-weight: 600; font-variant-numeric: tabular-nums; }

.mail-body { display: none; padding: 20px; border-top: 1px solid var(--border); background: #fff; }
.meta-table { width: 100%; border-collapse: collapse; margin-bottom: 16px; font-size: 13px; }
.meta-table th { text-align: left; padding: 8px 12px; color: var(--text-muted); width: 70px; background: #f8fafc; border-radius: 4px; }
.meta-table td { padding: 8px 12px; color: var(--text); word-break: break-all; }
.attachments { margin: 12px 0; padding: 12px; background: #f8fafc; border-radius: 8px; border: 1px solid var(--border); }
.attachments a { color: var(--accent); text-decoration: none; margin-right: 16px; font-size: 13px; }
.attachments a:hover { text-decoration: underline; }
.empty { text-align: center; padding: 50px 20px; color: var(--text-muted); font-size: 14px; }
iframe { width: 100%; border: 1px solid #eee; min-height: 300px; background: #fff; border-radius: 6px; }
pre { white-space: pre-wrap; word-wrap: break-word; font-family: inherit; margin: 0; color: var(--text); line-height: 1.6; }

.img-viewer { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.85); z-index: 9999; justify-content: center; align-items: center; cursor: zoom-out; }
.img-viewer.show { display: flex; }
.img-viewer img { max-width: 95%; max-height: 95%; object-fit: contain; border-radius: 8px; }

.toast { position: fixed; top: 24px; left: 50%; transform: translateX(-50%) translateY(-20px); background: #1e293b; color: #fff; padding: 12px 24px; border-radius: 8px; z-index: 10000; display: none; box-shadow: 0 8px 24px rgba(0,0,0,0.15); font-size: 14px; transition: all 0.3s; opacity: 0; }
.toast.show { display: block; opacity: 1; transform: translateX(-50%) translateY(0); }

.footer { position: fixed; bottom: 0; left: 0; right: 0; text-align: center; padding: 16px; color: var(--text-muted); font-size: 12px; background: rgba(255,255,255,0.9); backdrop-filter: blur(8px); border-top: 1px solid var(--border); z-index: 100; }
.footer a { color: var(--accent); text-decoration: none; margin-left: 8px; }
.footer a:hover { text-decoration: underline; }

.warn-box { border: 1px solid #fde68a; background: #fffbeb; }
.warn-box h2 { color: #92400e; border: none; margin-bottom: 16px; font-size: 20px; }
.warn-box p { color: #78350f; margin-bottom: 12px; line-height: 1.6; }
.warn-box code { background: #fef3c7; padding: 2px 6px; border-radius: 4px; font-size: 13px; color: #92400e; }
.warn-box .config-block { background: #1e293b; border-radius: 8px; padding: 16px; margin: 16px 0; text-align: left; font-family: monospace; font-size: 13px; color: #e2e8f0; line-height: 1.8; overflow-x: auto; }
.warn-box .config-block .comment { color: #64748b; }
.warn-box ol { text-align: left; max-width: 600px; margin: 20px auto; line-height: 2; color: #78350f; padding-left: 20px; }

@media (max-width: 768px) {
    .container { padding: 16px 12px; }
    .card { padding: 16px; }
    .header-left p { display: none; }
    .hide-mobile { display: none !important; }
    .mailbox-row { flex-direction: column; }
    .mailbox-box { font-size: 16px; padding: 12px; min-height: 44px; }
    .btn-row-mobile { display: flex; gap: 10px; }
    .btn-row-mobile .btn { flex: 1; padding: 12px; }
    .mail-line-pc { display: none; }
    .mail-line-mobile { display: flex; flex-direction: column; gap: 6px; }
    .mail-mobile-row1 { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
    .mail-mobile-row1 .mail-subject { font-size: 14px; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .mail-mobile-row1 .mail-time { font-size: 11px; color: var(--text-muted); flex-shrink: 0; }
    .mail-mobile-row2 { display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: var(--text-muted); }
    .mail-mobile-row2 .mail-sender { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .mail-mobile-row2 .mail-right { gap: 8px; }
}
@media (min-width: 769px) {
    .mail-line-mobile { display: none; }
    .btn-row-mobile { display: none; }
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
    <div class="container">
        {% if is_ip %}
        <div class="card warn-box">
            <h2>⚠️ 检测到 IP 直接访问</h2>
            <p>临时邮箱需要通过<strong>域名</strong>来生成有效的邮箱地址。当前您通过 IP (<code>{{ host }}</code>) 访问，无法生成可用邮箱。</p>
            <p>系统检测到您的服务器公网 IP 为：<code>{{ public_ip }}</code></p>
            <ol>
                <li>请将您的域名 A 记录解析到公网 IP：<code>{{ public_ip }}</code></li>
                <li>使用 Nginx / Caddy 等反向代理到本机端口 <code>{{ port }}</code></li>
                <li>确保反代配置中传递了 <code>Host</code> 头</li>
                <li>配置完成后，<strong>请访问配置的域名进行访问</strong>不要使用IP和端口直接访问</li>
            </ol>
            <div class="config-block">
                <span class="comment"># Nginx 反代配置示例 (放在nginx.conf配置或者宝塔伪静态均可)</span><br>
           
                &nbsp;&nbsp;&nbsp;&nbsp;location / {<br>
                &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;proxy_pass http://127.0.0.1:{{ port }};<br>
                &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;proxy_set_header Host $host;<br>
                &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;proxy_set_header X-Real-IP $remote_addr;<br>
                &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;<br>
                &nbsp;&nbsp;&nbsp;&nbsp;}<br>
               <br>
                <span class="comment"># DNS 解析配置示例，需要配置一个A记录和一个MX记录，MX的记录值就是A记录的完整二级域名</span><br>
                类型: A &nbsp;&nbsp; 主机记录: mail &nbsp;&nbsp; 记录值: {{ public_ip }}<br>
                类型: MX &nbsp;&nbsp; 主机记录: @ &nbsp;&nbsp; 记录值: mail.xxx.cn
            </div>
        </div>
        {% else %}
        <div class="card">
            <div class="header-row">
                <div class="header-left">
                    <h1>📬 TempMail</h1>
                    <p>安全、匿名、10分钟邮箱</p>
                </div>
                <div class="header-right">
                    <div class="sync-status" id="sync-status">⏳ 初始化...</div>
                    <div class="hide-mobile">🕒 邮件保留 <strong>{{ retention }}</strong> 分钟</div>
                </div>
            </div>
            <div class="mailbox-row">
                <div class="mailbox-box" id="mailbox-display">正在生成专属邮箱...</div>
                <button class="btn btn-secondary hide-mobile" onclick="generateMailbox()">🔄 换个邮箱</button>
                <button class="btn btn-primary hide-mobile" onclick="copyEmail()">📋 复制邮箱</button>
            </div>
            <div class="btn-row-mobile" style="margin-top: 12px;">
                <button class="btn btn-secondary" onclick="generateMailbox()">🔄 换个邮箱</button>
                <button class="btn btn-primary" onclick="copyEmail()">📋 复制邮箱</button>
            </div>
        </div>

        <div class="card">
            <div class="inbox-header">
                <h2>📥 收件箱</h2>
                <button class="btn btn-secondary btn-sm" onclick="loadEmails(true)">刷新</button>
            </div>
            <div id="email-list"><div class="empty">⏳ 正在加载邮件...</div></div>
        </div>
        {% endif %}
    </div>

    <div class="footer">
        © <span id="year"></span> TempMail. All rights reserved. 
        <a href="{{ github_url }}" target="_blank">GitHub</a>
    </div>
    
    <div id="toast" class="toast"></div>
    <div id="img-viewer" class="img-viewer" onclick="this.classList.remove('show')"><img id="img-viewer-src" src=""></div>

    {% if not is_ip %}
    <script>
        var currentEmail = null, openedMailId = null, lastSig = "", knownIds = {};
        var mailboxCreatedAt = 0; // 邮箱创建时间戳(ms)，用于判断是否为新邮箱
        document.getElementById("year").innerHTML = new Date().getFullYear();

        function showToast(m) {
            var t = document.getElementById("toast"); 
            t.innerText = m; 
            t.classList.add("show");
            clearTimeout(window.toastTimer);
            window.toastTimer = setTimeout(() => { t.classList.remove("show"); }, 2500);
        }

        function copyEmail() {
            if (!currentEmail) return;
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(currentEmail).then(() => showToast("✅ 已复制: " + currentEmail)).catch(fallbackCopy);
            } else { fallbackCopy(); }
        }
        function fallbackCopy() {
            var input = document.createElement("textarea");
            input.value = currentEmail;
            document.body.appendChild(input);
            input.select();
            try { document.execCommand("copy"); showToast("✅ 已复制: " + currentEmail); } 
            catch (e) { showToast("❌ 复制失败，请手动复制"); }
            document.body.removeChild(input);
        }

        function loadMailbox() {
            fetch("/api/mailbox").then(r => r.json()).then(d => {
                if (d.ok && d.email) {
                    currentEmail = d.email;
                    mailboxCreatedAt = d.created_ts || 0;
                    document.getElementById("mailbox-display").innerText = currentEmail;
                    // 如果是刚生成的邮箱(5秒内)，直接显示空状态，跳过数据库查询
                    if (mailboxCreatedAt && (Date.now() - mailboxCreatedAt < 5000)) {
                        document.getElementById("email-list").innerHTML = '<div class="empty">📭 暂无邮件，等待收件中...</div>';
                        document.getElementById("sync-status").innerText = "数据同步中" + new Date().toLocaleTimeString();
                        knownIds = {}; lastSig = "";
                    } else {
                        // 非新邮箱：先用轻量接口检查是否有有效邮件
                        checkAndLoadEmails();
                    }
                } else { 
                    generateMailbox(); 
                }
            });
        }

        function checkAndLoadEmails() {
            // 轻量级检查：只查COUNT，毫秒级返回
            fetch("/api/emails/check").then(r => r.json()).then(d => {
                if (d.ok && d.count > 0) {
                    loadEmails(false);
                } else {
                    document.getElementById("email-list").innerHTML = '<div class="empty">📭 有效期内暂无邮件，等待收件中...</div>';
                    document.getElementById("sync-status").innerText = "数据同步中" + new Date().toLocaleTimeString();
                    knownIds = {}; lastSig = "";
                }
            }).catch(() => {
                // check接口异常时降级到完整加载
                loadEmails(false);
            });
        }

        function generateMailbox() {
            fetch("/api/mailbox/generate", { method: "POST" }).then(r => r.json()).then(d => {
                if (d.ok) {
                    currentEmail = d.email;
                    mailboxCreatedAt = d.created_ts || Date.now();
                    document.getElementById("mailbox-display").innerText = currentEmail;
                    // 新生成的邮箱必定无邮件，秒开无需查库
                    document.getElementById("email-list").innerHTML = '<div class="empty">📭 暂无邮件，等待收件中...</div>';
                    showToast("🎉 已生成新邮箱: " + currentEmail);
                    knownIds = {}; lastSig = ""; openedMailId = null;
                    document.getElementById("sync-status").innerText = "数据同步中" + new Date().toLocaleTimeString();
                }
            });
        }

        function loadEmails(manual) {
            if (!currentEmail) return;
            fetch("/api/emails").then(r => r.json()).then(d => {
                if (!d.ok) return;
                var mails = d.emails || [];
                var sig = mails.map(m => m.id + "_" + m.is_read).join(",");
                
                if (!manual && sig === lastSig) {
                    document.getElementById("sync-status").innerText = "数据同步中" + new Date().toLocaleTimeString();
                    updateCountdowns(); 
                    return;
                }
                
                // 检测新邮件并提示
                if (!manual && lastSig !== "") {
                    for (var i = 0; i < mails.length; i++) {
                        if (!knownIds[mails[i].id]) { 
                            showToast("📩 收到新邮件！请查阅"); 
                            break; 
                        }
                    }
                }
                
                lastSig = sig; 
                var newKnownIds = {};
                mails.forEach(m => newKnownIds[m.id] = true);
                
                var listEl = document.getElementById("email-list");
                if (mails.length === 0) {
                    listEl.innerHTML = '<div class="empty">📭 有效期内暂无邮件，等待收件中...</div>';
                } else {
                    if (listEl.querySelector('.empty')) listEl.innerHTML = '';
                    
                    var existingIds = {};
                    listEl.querySelectorAll('.mail-item').forEach(el => {
                        existingIds[el.getAttribute('data-id')] = el;
                    });
                    
                    mails.forEach(m => {
                        var idStr = String(m.id);
                        if (existingIds[idStr]) {
                            var el = existingIds[idStr];
                            updateMailItem(el, m);
                            delete existingIds[idStr];
                        } else {
                            var newEl = createMailItem(m);
                            listEl.appendChild(newEl);
                        }
                    });
                    
                    for (var id in existingIds) {
                        existingIds[id].remove();
                        if (openedMailId == id) openedMailId = null;
                    }
                    
                    // 按API返回顺序(id DESC)重排DOM，确保最新邮件在最上面
                    mails.forEach(m => {
                        var el = listEl.querySelector('.mail-item[data-id="' + m.id + '"]');
                        if (el) listEl.appendChild(el);
                    });
                }
                
                knownIds = newKnownIds;
                document.getElementById("sync-status").innerText = "数据同步中" + new Date().toLocaleTimeString();
                updateCountdowns();
            });
        }

        function createMailItem(m) {
            var div = document.createElement('div');
            div.className = 'mail-item';
            div.setAttribute('data-id', m.id);
            var isOpen = (openedMailId == m.id);
            div.innerHTML = `
                <div class="mail-head" onclick="toggleMail(${m.id})">
                    <div class="mail-line-pc">
                        <div class="mail-subject">标题：${esc(m.subject || "(无标题)")}</div>
                        <div class="mail-sender">发件人：${esc(m.sender)}</div>
                        <div class="mail-right">
                            <span class="cd-timer" data-remain="${m.remaining_seconds}">--</span>
                            <span>${m.received_at.substring(11, 16)}</span>
                            <span class="badge ${m.is_read ? 'badge-read' : 'badge-unread'}">${m.is_read ? '已读' : '未读'}</span>
                        </div>
                    </div>
                    <div class="mail-line-mobile">
                        <div class="mail-mobile-row1">
                            <div class="mail-subject">${esc(m.subject || "(无标题)")}</div>
                            <div class="mail-time">${m.received_at.substring(11, 16)}</div>
                        </div>
                        <div class="mail-mobile-row2">
                            <div class="mail-sender">${esc(m.sender)}</div>
                            <div class="mail-right">
                                <span class="cd-timer" data-remain="${m.remaining_seconds}">--</span>
                                <span class="badge ${m.is_read ? 'badge-read' : 'badge-unread'}">${m.is_read ? '已读' : '未读'}</span>
                            </div>
                        </div>
                    </div>
                </div>
                <div id="body-${m.id}" class="mail-body" style="display:${isOpen ? 'block' : 'none'}">
                    ${renderBody(m)}
                </div>
            `;
            return div;
        }

        function updateMailItem(el, m) {
            var badgeClass = m.is_read ? 'badge-read' : 'badge-unread';
            var badgeText = m.is_read ? '已读' : '未读';
            el.querySelectorAll('.badge').forEach(b => {
                b.className = 'badge ' + badgeClass;
                b.innerText = badgeText;
            });
            el.querySelectorAll('.cd-timer').forEach(t => {
                t.setAttribute('data-remain', m.remaining_seconds);
            });
        }

        function renderBody(m) {
            var meta = `<table class="meta-table">
                <tr><th>发件人</th><td>${esc(m.sender)}</td></tr>
                <tr><th>时间</th><td>${m.received_at}</td></tr>
            </table>`;
            var att = "";
            if (m.attachments && m.attachments.length > 0) {
                att = '<div class="attachments"><b>📎 附件：</b>';
                m.attachments.forEach(a => {
                    if (a.data_uri) att += `<a href="${a.data_uri}" download="${esc(a.filename)}">${esc(a.filename)}</a>`;
                });
                att += '</div>';
            }
            var content = m.html_body ? `<iframe srcdoc="${esc(m.html_body)}" onload="injectIframeStyle(this); autoResize(this)"></iframe>` : `<pre>${esc(m.text_body || "(无正文)")}</pre>`;
            return meta + att + content;
        }

        function injectIframeStyle(iframe) {
            try {
                var doc = iframe.contentDocument || iframe.contentWindow.document;
                var style = doc.createElement('style');
                style.innerHTML = 'img { max-width: 100% !important; height: auto !important; cursor: zoom-in; border-radius: 6px; margin: 8px 0; } body { font-family: inherit; margin: 0; padding: 12px; color: #1e293b; } a { color: #2563eb; }';
                doc.head.appendChild(style);
                var imgs = doc.getElementsByTagName('img');
                for (var i = 0; i < imgs.length; i++) {
                    imgs[i].onclick = (function(src) {
                        return function(e) { e.stopPropagation(); showImage(src); };
                    })(imgs[i].src);
                }
            } catch(e) {}
        }

        function showImage(src) {
            document.getElementById("img-viewer-src").src = src;
            document.getElementById("img-viewer").classList.add("show");
        }

        function toggleMail(id) {
            if (openedMailId == id) {
                document.getElementById("body-" + id).style.display = "none";
                openedMailId = null; return;
            }
            if (openedMailId !== null) {
                var oldBody = document.getElementById("body-" + openedMailId);
                if (oldBody) oldBody.style.display = "none";
            }
            document.getElementById("body-" + id).style.display = "block";
            openedMailId = id;
            fetch(`/api/emails/${id}/read`, { method: "POST" });
        }

        function autoResize(f) {
            try { f.style.height = (f.contentWindow.document.body.scrollHeight + 40) + 'px'; } catch(e) {}
        }

        function updateCountdowns() {
            var nodes = document.getElementsByClassName("cd-timer");
            for (var i = 0; i < nodes.length; i++) {
                var rem = parseInt(nodes[i].getAttribute("data-remain") || 0);
                if (rem <= 0) {
                    nodes[i].innerText = "已过期";
                    continue;
                }
                var m = Math.floor(rem / 60), s = rem % 60;
                nodes[i].innerText = m + "分" + (s < 10 ? "0" : "") + s + "秒";
            }
        }

        function esc(t) { return String(t || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }

        loadMailbox();
        setInterval(() => loadEmails(false), 5000);
        
        setInterval(() => {
            var nodes = document.getElementsByClassName("cd-timer");
            for (var i = 0; i < nodes.length; i++) {
                var rem = parseInt(nodes[i].getAttribute("data-remain") || 0);
                if (rem > 0) {
                    nodes[i].setAttribute("data-remain", rem - 1);
                }
            }
            updateCountdowns();
        }, 1000);
    </script>
    {% endif %}
</body>
</html>
"""

# ================= 路由与 API =================
@app.route("/")
def index():
    host, is_ip = get_current_domain()
    public_ip = get_public_ip() if is_ip else ""
    return render_template_string(
        INDEX_HTML, style=STYLE, host=host, port=APP_PORT, is_ip=is_ip, 
        retention=FRONT_RETENTION_MINUTES, public_ip=public_ip, github_url=GITHUB_URL
    )

@app.route("/api/mailbox")
def api_mailbox():
    domain, is_ip = get_current_domain()
    if is_ip: return jsonify({"ok": False, "error": "请通过域名访问"}), 400
    token = get_client_token()
    mailbox = get_client_mailbox(token, domain)
    resp_data = {"ok": True, "email": None, "created_ts": 0}
    if mailbox:
        resp_data["email"] = mailbox["email"]
        try:
            ct = datetime.strptime(mailbox["created_at"], "%Y-%m-%d %H:%M:%S")
            resp_data["created_ts"] = int(ct.timestamp() * 1000)
        except: pass
    resp = make_response(jsonify(resp_data))
    return set_client_cookie(resp, token)

@app.route("/api/mailbox/generate", methods=["POST"])
def api_generate():
    domain, is_ip = get_current_domain()
    if is_ip: return jsonify({"ok": False, "error": "请通过域名访问"}), 400
    token = get_client_token()
    try: 
        email = create_mailbox(token, domain)
    except Exception as e: 
        return jsonify({"ok": False, "error": str(e)}), 400
    resp = make_response(jsonify({"ok": True, "email": email, "created_ts": int(time.time() * 1000)}))
    return set_client_cookie(resp, token)

@app.route("/api/emails/check")
def api_emails_check():
    """轻量级检查接口：仅返回有效期内邮件数量，毫秒级响应"""
    domain, is_ip = get_current_domain()
    if is_ip: return jsonify({"ok": False}), 400
    token = get_client_token()
    mailbox = get_client_mailbox(token, domain)
    if not mailbox: return jsonify({"ok": True, "count": 0})
    
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
    if is_ip: return jsonify({"ok": False, "error": "请通过域名访问"}), 400
    token = get_client_token()
    mailbox = get_client_mailbox(token, domain)
    if not mailbox: return jsonify({"ok": False, "error": "无邮箱"}), 400
    
    cutoff = (datetime.now() - timedelta(minutes=FRONT_RETENTION_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    with db_connect() as db:
        rows = db.execute(
            "SELECT e.id, e.sender, e.subject, e.text_body, e.html_body, e.attachments, e.received_at, "
            "CAST((julianday(datetime(e.received_at, '+' || ? || ' minutes')) - julianday('now', 'localtime')) * 86400 AS INTEGER) AS remaining_seconds, "
            "CASE WHEN r.email_id IS NULL THEN 0 ELSE 1 END AS is_read "
            "FROM emails e LEFT JOIN email_reads r ON r.email_id = e.id AND r.client_token = ? "
            "WHERE e.mailbox_email = ? AND e.received_at >= ? ORDER BY e.id DESC",
            (FRONT_RETENTION_MINUTES, token, mailbox["email"], cutoff)
        ).fetchall()
        
    emails = []
    for r in rows:
        d = dict(r)
        try: d["attachments"] = json.loads(d["attachments"] or "[]")
        except: d["attachments"] = []
        d["remaining_seconds"] = max(0, d.get("remaining_seconds", 0))
        emails.append(d)
    return jsonify({"ok": True, "emails": emails})

@app.route("/api/emails/<int:email_id>/read", methods=["POST"])
def api_read(email_id):
    token = get_client_token()
    domain, _ = get_current_domain()
    mailbox = get_client_mailbox(token, domain)
    if not mailbox: return jsonify({"ok": False}), 400
    with db_lock:
        with db_connect() as db:
            db.execute("INSERT OR REPLACE INTO email_reads(client_token, email_id, read_at) VALUES (?, ?, ?)", (token, email_id, now_text()))
    return jsonify({"ok": True})

# ================= SMTP 接收服务 =================
class SMTPHandler:
    async def handle_DATA(self, server, session, envelope):
        try:
            raw_message = envelope.content
            if len(raw_message) > MAX_EMAIL_SIZE: return "552 Message too large"
            message = BytesParser(policy=policy.default).parsebytes(raw_message)
            recipients = list(envelope.rcpt_tos or [])
            valid_emails = []
            for rcpt in recipients:
                addr = parseaddr(str(rcpt))[1].lower()
                if "@" in addr:
                    with db_connect() as db:
                        if db.execute("SELECT id FROM mailboxes WHERE email = ? AND active = 1", (addr,)).fetchone():
                            valid_emails.append(addr)
            if not valid_emails: return "250 OK"
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
            return "451 Temporary error"

def start_smtp_server():
    controller = Controller(SMTPHandler(), hostname=SMTP_HOST, port=SMTP_PORT)
    controller.start()
    return controller

def cleanup_worker():
    while True:
        try:
            cutoff_front = (datetime.now() - timedelta(minutes=FRONT_RETENTION_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
            cutoff_back = (datetime.now() - timedelta(days=BACKEND_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
            with db_lock:
                with db_connect() as db:
                    db.execute("DELETE FROM email_reads WHERE read_at < ?", (cutoff_front,))
                    db.execute("DELETE FROM emails WHERE received_at < ?", (cutoff_back,))
                    db.execute("DELETE FROM mailboxes WHERE active = 0 AND created_at < ?", (cutoff_back,))
        except Exception as e:
            print(f"[Cleanup Error] {e}")
        time.sleep(60)

if __name__ == "__main__":
    init_db()
    print("=" * 50)
    print("📬 临时邮箱服务已启动 (极简版)")
    print(f"🌐 前端地址: http://127.0.0.1:{APP_PORT}")
    print(f"📥 SMTP 监听: {SMTP_HOST}:{SMTP_PORT}")
    print(f"⏱️  前端保留: {FRONT_RETENTION_MINUTES} 分钟")
    print(f"🗑️  后端清理: {BACKEND_RETENTION_DAYS} 天")
    print("=" * 50)
    
    threading.Thread(target=cleanup_worker, daemon=True).start()
    smtp_controller = start_smtp_server()
    
    try:
        app.run(host=APP_HOST, port=APP_PORT, debug=False, use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        print("\n正在停止服务...")
    finally:
        if smtp_controller: smtp_controller.stop()
