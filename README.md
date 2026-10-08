# Mail
# TempMail 临时邮箱 10分钟邮箱

# 📬 TempMail 临时邮箱服务部署文档

> 轻量级、阅后即焚的临时邮箱。支持 Web 实时收件、多域名自适应、附件内联预览，内置 SMTP 25 端口收信能力。
> 本文档兼容所有主流 Linux 发行版（Ubuntu/Debian/CentOS/RHEL/Rocky/Alma/Fedora 等）。

## ⚡ 一键部署（推荐）

自动检测 Python 版本、自适应依赖降级、释放 25 端口、配置 Systemd 服务：

bash <(wget -qO- atusu.cn/mail/1.0/install.sh)

首次运行自动安装，再次运行进入交互式管理菜单（重载 / 重装 / 卸载）。


## 📋 目录

- [环境要求](#-环境要求)
- [方案 A：原生手动部署](#-方案-a原生手动部署)
- [方案 B：Docker 容器化部署](#-方案-bdocker-容器化部署)
- [域名解析与反向代理（必读）](#️-域名解析与反向代理必读)
- [环境变量](#-环境变量)
- [常见问题排查](#-常见问题排查)


## ⚙️ 环境要求

| 项目 | 要求 |
| :--- | :--- |
| 操作系统 | Ubuntu/Debian, CentOS/RHEL/Rocky/Alma, Fedora 等 |
| 架构 | x86_64 / ARM64 |
| Python | ≥ 3.8 |
| 端口 8080 | Web 面板（建议 Nginx/Caddy 反代，不直接暴露） |
| 端口 25 | SMTP 收信（**必须放行入站**，确认云安全组未封禁） |

> ⚠️ **核心机制**：程序通过请求头 `Host` 识别域名。**IP + 端口直接访问无法生成邮箱**，必须配置域名和反向代理。


## 🛠️ 方案 A：原生手动部署

### 1. 安装系统依赖

**Ubuntu / Debian**

    sudo apt-get update
    sudo apt-get install -y curl wget build-essential libsqlite3-dev psmisc lsof net-tools

**CentOS / RHEL / Rocky / Alma / Fedora**

    if command -v dnf &> /dev/null; then
        sudo dnf install -y curl wget gcc make sqlite-devel psmisc lsof net-tools
    else
        sudo yum install -y epel-release curl wget gcc make sqlite-devel psmisc lsof net-tools
    fi

### 2. 安装 Python 3.8+

**Ubuntu / Debian**

    sudo apt-get install -y python3 python3-venv python3-pip

**CentOS / RHEL / Rocky / Alma**

    if [ "$(rpm -E %{rhel})" == "7" ]; then
        sudo yum install -y centos-release-scl rh-python38
        sudo ln -sf /opt/rh/rh-python38/root/usr/bin/python3 /usr/local/bin/python3.8
    else
        sudo dnf install -y python3 python3-pip python3-devel
    fi

验证版本：

    python3 --version

### 3. 下载程序 & 配置虚拟环境

    sudo mkdir -p /opt/tempmail/data
    cd /opt/tempmail
    sudo curl -sSfL https://atusu.cn/mail/app.py -o app.py
    sudo python3 -m venv venv
    sudo ./venv/bin/pip install --upgrade pip
    echo -e "flask\naiosmtpd\nwerkzeug" | sudo tee requirements.txt > /dev/null
    sudo ./venv/bin/pip install -r requirements.txt

### 4. 释放 25 端口（关键）

Linux 自带 MTA 会占用 25 端口，必须清理：

    for svc in postfix exim4 sendmail ssmtp msmtp opensmtpd; do
        systemctl is-active --quiet "$svc" 2>/dev/null && \
            echo "停止: $svc" && sudo systemctl stop "$svc" && sudo systemctl disable "$svc"
    done
    sudo fuser -k 25/tcp 2>/dev/null
    sudo lsof -ti :25 | xargs sudo kill -9 2>/dev/null

### 5. 配置 Systemd 服务

    sudo tee /etc/systemd/system/tempmail.service > /dev/null <<EOF
    [Unit]
    Description=TempMail Service
    After=network.target

    [Service]
    Type=simple
    User=root
    WorkingDirectory=/opt/tempmail
    ExecStart=/opt/tempmail/venv/bin/python /opt/tempmail/app.py
    Restart=always
    RestartSec=5
    Environment=TZ=Asia/Shanghai

    [Install]
    WantedBy=multi-user.target
    EOF

    sudo systemctl daemon-reload
    sudo systemctl enable --now tempmail
    sudo systemctl status tempmail --no-pager

### 常用命令

    sudo systemctl restart tempmail     # 重载
    sudo journalctl -u tempmail -f      # 实时日志
    sudo systemctl stop tempmail        # 停止


## 🐳 方案 B：Docker 容器化部署

### 1. 安装 Docker

    curl -fsSL https://get.docker.com | sudo sh
    sudo systemctl enable --now docker

### 2. 创建部署目录

    mkdir -p ~/tempmail-docker/data && cd ~/tempmail-docker
    curl -sSfL https://atusu.cn/mail/app.py -o app.py

### 3. 创建 Dockerfile

    FROM python:3.11-slim
    WORKDIR /app
    RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gcc libsqlite3-dev && rm -rf /var/lib/apt/lists/*
    COPY app.py .
    RUN pip install --no-cache-dir flask aiosmtpd werkzeug
    EXPOSE 8080 25
    CMD ["python", "app.py"]

### 4. 创建 docker-compose.yml

    services:
      tempmail:
        build: .
        container_name: tempmail
        restart: always
        network_mode: host
        volumes:
          - ./data:/app/data
        environment:
          - TZ=Asia/Shanghai

### 5. 构建启动

    # 先释放宿主机 25 端口（同手动部署第 4 步）
    sudo systemctl stop postfix exim4 sendmail 2>/dev/null
    sudo fuser -k 25/tcp 2>/dev/null

    sudo docker compose up -d --build
    sudo docker compose logs -f
    

## 🌐️ 域名解析与反向代理（必读）

### DNS 解析

以 `mail.yourdomain.com` 为邮箱后缀：

| 类型 | 主机记录 | 记录值 | 说明 |
| :--- | :--- | :--- | :--- |
| A | mail | 服务器公网 IP | Web 访问 + MX 指向 |
| MX | @ | mail.yourdomain.com | 优先级 10，接收邮件 |

### Nginx 反向代理

        location / {
            proxy_pass http://127.0.0.1:8080;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }


    sudo nginx -t && sudo nginx -s reload

> 💡 建议后续用 Certbot 开启 HTTPS：`sudo certbot --nginx -d mail.yourdomain.com`

---

## 🔧 环境变量

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `APP_HOST` | `0.0.0.0` | Web 监听地址 |
| `APP_PORT` | `8080` | Web 监听端口 |
| `SMTP_HOST` | `0.0.0.0` | SMTP 监听地址 |
| `SMTP_PORT` | `25` | SMTP 监听端口 |
| `DB_FILE` | `./temp_mail.db` | 数据库路径 |
| `MAX_EMAIL_SIZE` | `10485760` | 单封邮件上限（10MB） |
| `FLASK_SECRET_KEY` | 随机生成 | Session 密钥 |
| `TZ` | `Asia/Shanghai` | 时区 |

---

## ❓ 常见问题排查

### 提示“检测到 IP 直接访问”

- 使用了 IP+端口访问，或反代未传 `Host` 头
- 必须用域名访问，确认 Nginx 含 `proxy_set_header Host $host;`

### 网页正常但收不到邮件

1. 检查 MX 记录：

       dig MX yourdomain.com

2. 外部检查 25 端口入站：

       telnet mail.yourdomain.com 25

   超时则安全组拦截了入站。

3. 服务器内检查端口占用：

       ss -tuln | grep :25

   确认是 Python/Docker 监听而非系统 MTA。

4. 查看日志：

       # 手动部署
       sudo journalctl -u tempmail -f

       # Docker 部署
       sudo docker compose logs -f

   - 有 `[SMTP] Received mail for...` = 收到邮件
   - 无此日志 = 网络层被拦截

### 云厂商封禁 25 端口

阿里云/腾讯云/AWS 通常只封 **出站**，**入站** 默认开放。TempMail 仅需收信，放行 25 入站即可。

### 数据备份

所有数据存储在 SQLite 文件中，定期备份即可完整恢复：

- 手动部署：`/opt/tempmail/temp_mail.db`
- Docker：`~/tempmail-docker/data/temp_mail.db`
