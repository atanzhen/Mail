#!/usr/bin/env bash
# ============================================================
#  TempMail 源码版 一键部署 / 管理脚本  (install.sh)
#
#  ★ 直接在本机运行 app.py：自动检测 python3、创建独立虚拟环境、
#    安装依赖（flask / aiosmtpd）、写 systemd 服务并启动。
#    只需要 app.py、requirements.txt 与本脚本在同一目录。
#
#  用法:
#    sudo bash install.sh                 # 首次: 一键部署；已安装: 进入管理菜单
#    sudo bash install.sh install         # 安装并启动守护 (systemd)
#    sudo bash install.sh reinstall       # 重装 (重新安装依赖, 保留数据)
#    sudo bash install.sh full-reinstall  # 完整清理重装 (清空数据)
#    sudo bash install.sh reload          # 重启服务
#    sudo bash install.sh start           # 启动服务
#    sudo bash install.sh stop            # 停止服务
#    sudo bash install.sh status          # 查看状态与访问地址
#    sudo bash install.sh clear-config    # 清除主域名配置 (config.json)
#    sudo bash install.sh uninstall       # 卸载服务 (保留数据)
#    sudo bash install.sh uninstall --purge   # 卸载并删除程序与全部数据
#    sudo bash install.sh menu            # 打开管理菜单
#
#  可选参数 / 环境变量:
#    --web-port 6060              起始 Web 端口 (被占用时自动向后顺延并写回 .env)
#    --smtp-port 25               SMTP 收信端口
#    --dir  /opt/temp-mail        安装目录 (默认 /opt/temp-mail)
#    --mirror https://pypi.tuna.tsinghua.edu.cn/simple   pip 镜像源
#    WEB_PORT_START / SMTP_PORT / INSTALL_DIR  同义环境变量
#
#  说明:
#    - 需要 Linux + systemd + python3 (>= 3.8)
#    - 依赖装在独立 venv 里, 不污染系统 Python
#    - 端口自适应: Web 端口被占用时, 自家旧实例先停止复用, 其它程序占用则自动顺延
#    - systemd 托管: 开机自启 + 崩溃自动重启 + 25 端口授权
# ============================================================
set -u

SCRIPT_VERSION="2.0"
APP_NAME="TempMail"
APP_VERSION="2.2.1"
SERVICE_NAME="temp-mail"
UNIT_FILE="${UNIT_FILE:-/etc/systemd/system/${SERVICE_NAME}.service}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INSTALL_DIR="${INSTALL_DIR:-/opt/temp-mail}"
DATA_DIR="${INSTALL_DIR}/data"
ENV_FILE="${INSTALL_DIR}/.env"
VENV_DIR="${INSTALL_DIR}/venv"
APP_FILE="${INSTALL_DIR}/app.py"
PY_BIN=""
PIP_MIRROR="${PIP_INDEX_URL:-}"
WEB_PORT_START="${WEB_PORT_START:-6060}"
PORT_TRY_RANGE="${PORT_TRY_RANGE:-200}"
SMTP_PORT="${SMTP_PORT:-25}"

if [ -f "${SCRIPT_DIR}/VERSION" ]; then
    v="$(awk '{print $2}' "${SCRIPT_DIR}/VERSION" 2>/dev/null | head -n1)"
    [ -n "${v:-}" ] && APP_VERSION="$v"
fi

C_G="\033[32m"; C_Y="\033[33m"; C_R="\033[31m"; C_B="\033[36m"; C_0="\033[0m"
info() { echo -e "${C_B}[INFO]${C_0} $*"; }
ok()   { echo -e "${C_G}[ OK ]${C_0} $*"; }
warn() { echo -e "${C_Y}[WARN]${C_0} $*"; }
err()  { echo -e "${C_R}[FAIL]${C_0} $*"; }

# ---------------- 参数解析 ----------------

parse_args() {
    CMD_ACTION=""
    while [ $# -gt 0 ]; do
        case "$1" in
            install|reinstall|full-reinstall|reset|reload|restart|start|stop|halt|\
            status|clear-config|reset-config|uninstall|purge|menu|help|-h|--help)
                CMD_ACTION="$1" ;;
            --web-port)  WEB_PORT_START="${2:-}"; shift || true ;;
            --smtp-port) SMTP_PORT="${2:-}"; shift || true ;;
            --dir)       INSTALL_DIR="${2:-}"; shift || true ;;
            --mirror)    PIP_MIRROR="${2:-}"; shift || true ;;
            --web-port=*)  WEB_PORT_START="${1#*=}" ;;
            --smtp-port=*) SMTP_PORT="${1#*=}" ;;
            --dir=*)       INSTALL_DIR="${1#*=}" ;;
            --mirror=*)    PIP_MIRROR="${1#*=}" ;;
            --purge)     CMD_PURGE=1 ;;
            *) ;;
        esac
        shift
    done
}
CMD_ACTION=""
CMD_PURGE=0

# ---------------- 基础检查 ----------------

check_root() {
    if [ "$(id -u 2>/dev/null || echo 1)" != "0" ]; then
        err "请使用 root 用户运行本脚本 (sudo bash install.sh)"
        exit 1
    fi
}

check_systemd() {
    if ! command -v systemctl >/dev/null 2>&1; then
        err "未检测到 systemd，本脚本需要 systemd 托管服务"
        echo "  可改用 Docker 方式: bash docker.sh up"
        exit 1
    fi
}

check_sources() {
    if [ ! -f "${SCRIPT_DIR}/app.py" ]; then
        err "缺少文件: ${SCRIPT_DIR}/app.py"
        echo "  请把 install.sh 与 app.py / requirements.txt 放在同一目录"
        exit 1
    fi
    if [ ! -f "${SCRIPT_DIR}/requirements.txt" ]; then
        warn "未找到 requirements.txt，将只安装 flask / aiosmtpd"
    fi
}

# ---------------- Python 环境 ----------------

ensure_python() {
    local c ver
    for c in python3 python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python; do
        command -v "$c" >/dev/null 2>&1 || continue
        ver="$("$c" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null)"
        if [ -n "$ver" ] && [ "$ver" -ge 308 ] 2>/dev/null; then
            PY_BIN="$(command -v "$c")"
            break
        fi
    done
    if [ -z "$PY_BIN" ]; then
        err "未检测到 python3 (>= 3.8)"
        echo "  安装: apt install -y python3 python3-venv   /   yum install -y python3"
        echo "  或者改用 Docker 方式: bash docker.sh up"
        exit 1
    fi
    ok "Python: ${PY_BIN} ($("$PY_BIN" -V 2>&1))"
}

# 创建虚拟环境（失败时自动尝试补装 python3-venv）
make_venv() {
    rm -rf "${VENV_DIR}.new"
    if "$PY_BIN" -m venv "${VENV_DIR}.new" >/dev/null 2>&1 && [ -x "${VENV_DIR}.new/bin/python" ]; then
        rm -rf "$VENV_DIR"
        mv "${VENV_DIR}.new" "$VENV_DIR"
        ok "虚拟环境: ${VENV_DIR}"
        return 0
    fi
    rm -rf "${VENV_DIR}.new"

    if command -v apt-get >/dev/null 2>&1; then
        warn "缺少 venv 模块，尝试安装 python3-venv ..."
        apt-get update -qq >/dev/null 2>&1 || true
        apt-get install -y -qq python3-venv >/dev/null 2>&1 || true
        if "$PY_BIN" -m venv "${VENV_DIR}.new" >/dev/null 2>&1 && [ -x "${VENV_DIR}.new/bin/python" ]; then
            rm -rf "$VENV_DIR"
            mv "${VENV_DIR}.new" "$VENV_DIR"
            ok "虚拟环境: ${VENV_DIR}"
            return 0
        fi
        rm -rf "${VENV_DIR}.new"
    fi
    return 1
}

# pip 安装（输出只保留末尾几行，返回 pip 自己的退出码）
pip_install() {
    local py="$1"; shift
    local out rc
    out="$("$py" -m pip install --no-cache-dir "$@" 2>&1)"
    rc=$?
    printf '%s\n' "$out" | tail -n 6
    return $rc
}

# 安装依赖：优先 venv；venv 不可用时退回系统 pip
install_deps() {
    local py="$1" rc=0
    local req="${INSTALL_DIR}/requirements.txt"
    info "安装依赖（首次需下载，请耐心等待）..."
    if [ -n "$PIP_MIRROR" ]; then
        info "使用 pip 镜像源: ${PIP_MIRROR}"
        pip_install "$py" -i "$PIP_MIRROR" -r "$req" || rc=1
    else
        pip_install "$py" -r "$req" || rc=1
    fi
    if [ "$rc" != "0" ]; then
        warn "依赖安装失败，升级 pip 后重试..."
        pip_install "$py" --upgrade pip >/dev/null 2>&1 || true
        rc=0
        if [ -n "$PIP_MIRROR" ]; then
            pip_install "$py" -i "$PIP_MIRROR" -r "$req" || rc=1
        else
            pip_install "$py" -r "$req" || rc=1
        fi
    fi
    [ "$rc" = "0" ] && ok "依赖安装完成"
    return "$rc"
}

setup_runtime() {
    mkdir -p "$INSTALL_DIR" "$DATA_DIR"
    cp -f "${SCRIPT_DIR}/app.py" "$APP_FILE" || { err "复制 app.py 失败"; exit 1; }
    ok "程序文件: ${APP_FILE}"
    if [ -f "${SCRIPT_DIR}/requirements.txt" ]; then
        cp -f "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"
    else
        printf 'flask==3.1.1\naiosmtpd==1.4.6\n' > "${INSTALL_DIR}/requirements.txt"
    fi

    RUN_PY=""
    if make_venv; then
        RUN_PY="${VENV_DIR}/bin/python"
        if ! install_deps "$RUN_PY"; then
            err "依赖安装失败"
            echo "  国内服务器可指定镜像重试:"
            echo "    sudo bash install.sh install --mirror https://pypi.tuna.tsinghua.edu.cn/simple"
            exit 1
        fi
    else
        warn "无法创建虚拟环境，改用系统 Python 安装依赖"
        if ! "$PY_BIN" -m pip --version >/dev/null 2>&1; then
            err "系统缺少 pip，请安装 python3-pip 后重试，或改用: bash docker.sh up"
            exit 1
        fi
        if ! install_deps "$PY_BIN"; then
            warn "尝试使用 --break-system-packages 安装..."
            if ! pip_install "$PY_BIN" --break-system-packages -r "${INSTALL_DIR}/requirements.txt"; then
                err "依赖安装失败"
                echo "  国内服务器可指定镜像重试: --mirror https://pypi.tuna.tsinghua.edu.cn/simple"
                echo "  或者改用 Docker 方式: bash docker.sh up"
                exit 1
            fi
        fi
        RUN_PY="$PY_BIN"
    fi
    ok "运行解释器: ${RUN_PY}"

    # 脚本一并装到安装目录，之后可在安装目录继续管理 / 卸载
    if [ -f "${SCRIPT_DIR}/install.sh" ]; then
        cp -f "${SCRIPT_DIR}/install.sh" "${INSTALL_DIR}/install.sh"
        chmod +x "${INSTALL_DIR}/install.sh"
    fi
    if [ -f "${SCRIPT_DIR}/docker.sh" ]; then
        cp -f "${SCRIPT_DIR}/docker.sh" "${INSTALL_DIR}/docker.sh" 2>/dev/null || true
    fi
}

# ---------------- 端口工具 ----------------

port_in_use() {
    local p="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}$" && return 0
        return 1
    elif command -v netstat >/dev/null 2>&1; then
        netstat -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}$" && return 0
        return 1
    else
        (exec 3<>"/dev/tcp/127.0.0.1/${p}") >/dev/null 2>&1 && return 0
        return 1
    fi
}

find_free_port() {
    local start="$1" p i
    for i in $(seq 0 "$PORT_TRY_RANGE"); do
        p=$((start + i))
        if ! port_in_use "$p"; then
            echo "$p"
            return 0
        fi
    done
    echo ""
    return 1
}

# ---------- Web 端口占用处理 ----------
# 规则:
#   1) 端口空闲            -> 直接用
#   2) 被 TempMail 自身占用 -> 先停止旧实例, 释放后继续用原端口
#   3) 被其它程序占用       -> 自动向后顺延一个空闲端口, 并写回 .env
stop_self_on_port() {
    local p="$1" pid=""
    pid="$(get_listening_pid "$p")"
    if [ -z "$pid" ]; then
        pid="$(systemctl show -p MainPID --value "${SERVICE_NAME}" 2>/dev/null | tr -d '[:space:]')"
        [ "$pid" = "0" ] && pid=""
    fi

    # 端口被本项目的 Docker 容器占着：先停容器
    if { [ -z "$pid" ] || ! is_our_process "$pid"; } && command -v docker >/dev/null 2>&1; then
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "temp-mail" \
           && docker port "temp-mail" 2>/dev/null | grep -qE "[:.]${p}$"; then
            warn "端口 ${p} 由 TempMail Docker 容器占用，先停止容器再复用"
            docker stop "temp-mail" >/dev/null 2>&1 || true
            sleep 2
            return 0
        fi
    fi

    [ -n "$pid" ] || return 1
    is_our_process "$pid" || return 1

    warn "端口 ${p} 由 TempMail 自身进程占用 (pid ${pid})，先停止旧实例再复用"
    systemctl stop "${SERVICE_NAME}" >/dev/null 2>&1 || true
    kill "$pid" >/dev/null 2>&1 || true
    sleep 2
    if port_in_use "$p"; then
        kill -9 "$pid" >/dev/null 2>&1 || true
        sleep 1
    fi
    return 0
}

resolve_web_port() {
    local prefer="$1" p
    WEB_PORT_RESOLVED="$prefer"
    PORT_CHANGED=0

    if ! port_in_use "$prefer"; then
        ok "Web 端口 ${prefer} 可用"
        return 0
    fi

    stop_self_on_port "$prefer" || true
    if ! port_in_use "$prefer"; then
        ok "Web 端口 ${prefer} 已释放，继续使用"
        return 0
    fi

    p="$(find_free_port $((prefer + 1)))"
    if [ -z "$p" ]; then
        err "端口 ${prefer} 被占用，且向后 ${PORT_TRY_RANGE} 个端口都不可用"
        err "请释放该端口，或用 --web-port 指定其它起始端口"
        exit 1
    fi
    warn "Web 端口 ${prefer} 被其它程序占用，已自动改用 ${p}"
    WEB_PORT_RESOLVED="$p"
    PORT_CHANGED=1
    return 0
}

# 程序启动后写入的真实端口（.runtime_port），用于校正 .env
reconcile_runtime_port() {
    local configured="$1" f="${DATA_DIR}/.runtime_port" i real=""
    RUNTIME_PORT="$configured"
    for i in $(seq 1 20); do
        if [ -s "$f" ]; then
            real="$(tr -cd '0-9' < "$f" 2>/dev/null | head -c 5)"
            break
        fi
        sleep 1
    done
    [ -n "$real" ] || return 0
    if [ "$real" != "$configured" ]; then
        warn "程序实际监听端口 ${real}（配置为 ${configured}），已自动同步 .env 并重启"
        sync_env_port "$real"
        systemctl restart "${SERVICE_NAME}" >/dev/null 2>&1 || true
        sleep 2
    fi
    RUNTIME_PORT="$real"
    return 0
}

get_listening_pid() {
    local p="$1" pid=""
    if command -v ss >/dev/null 2>&1; then
        pid=$(ss -tlnp 2>/dev/null | grep -E "[:.]${p}[[:space:]]" | grep -oP 'pid=\K[0-9]+' | head -n1)
    fi
    if [ -z "$pid" ] && command -v lsof >/dev/null 2>&1; then
        pid=$(lsof -ti ":${p}" 2>/dev/null | head -n1)
    fi
    echo "$pid"
}

is_our_process() {
    local pid="$1"
    [ -n "$pid" ] || return 1
    if [ -r "/proc/${pid}/cmdline" ]; then
        tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null | grep -qE "temp-mail|tempmail|app\.py" && return 0
    fi
    return 1
}

handle_smtp_port() {
    if ! port_in_use "$SMTP_PORT"; then
        ok "SMTP 端口 ${SMTP_PORT} 空闲"
        return 0
    fi

    local pid
    pid=$(get_listening_pid "$SMTP_PORT")
    if is_our_process "$pid"; then
        warn "端口 ${SMTP_PORT} 由 TempMail 自身进程占用 (pid: ${pid})，跳过清理"
        return 0
    fi
    if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "temp-mail"; then
        warn "端口 ${SMTP_PORT} 由 TempMail Docker 容器占用，跳过清理"
        return 0
    fi

    warn "端口 ${SMTP_PORT} 被其他程序占用，开始强制清理..."
    for svc in postfix exim4 sendmail ssmtp msmtp opensmtpd; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            echo "停止: $svc"
            systemctl stop "$svc" 2>/dev/null || true
            systemctl disable "$svc" 2>/dev/null || true
        fi
    done
    command -v fuser >/dev/null 2>&1 && fuser -k "${SMTP_PORT}/tcp" 2>/dev/null || true
    command -v lsof >/dev/null 2>&1 && lsof -ti ":${SMTP_PORT}" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
    sleep 1
    if port_in_use "$SMTP_PORT"; then
        warn "端口 ${SMTP_PORT} 仍被占用，SMTP 收信可能不可用 (不影响 Web 功能)"
    else
        ok "端口 ${SMTP_PORT} 已释放"
    fi
}

# ---------------- 公网 IP ----------------

get_public_ip() {
    local ip=""
    if command -v ip >/dev/null 2>&1; then
        for nic in $(ip -o -4 addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1); do
            case "$nic" in
                10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*|127.*|169.254.*|100.6[4-9].*|100.[7-9][0-9].*|100.1[0-2][0-9].*) ;;
                *) echo "$nic"; return 0 ;;
            esac
        done
    fi
    command -v curl >/dev/null 2>&1 || { echo ""; return 0; }
    for url in http://ip.3322.net https://myip.ipip.net https://ipv4.icanhazip.com \
               https://api.ipify.org https://ifconfig.me/ip https://checkip.amazonaws.com \
               https://ipinfo.io/ip https://api.ip.sb/ip; do
        ip=$(curl -4 -fsS --noproxy '*' --max-time 3 "$url" 2>/dev/null \
             | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' | head -n1)
        if [ -n "$ip" ]; then
            echo "$ip"
            return 0
        fi
    done
    echo ""
}

service_public_ip() {
    local port="$1" body ip i
    command -v curl >/dev/null 2>&1 || return 0
    for i in 1 2 3; do
        body="$(curl -s --noproxy '*' --max-time 6 \
                "http://127.0.0.1:${port}/api/setup/status?t=$i" 2>/dev/null)"
        ip="$(printf '%s' "$body" \
              | sed -n 's/.*"public_ip"[[:space:]]*:[[:space:]]*"\([0-9.]\{7,\}\)".*/\1/p' | head -n1)"
        if [ -n "$ip" ]; then
            printf '%s' "$ip"
            return 0
        fi
        sleep 2
    done
    return 0
}

# ---------------- 配置 / 服务 ----------------

write_env() {
    local web_port="$1"
    if [ -f "$ENV_FILE" ]; then
        if grep -qE '^WEB_PORT=' "$ENV_FILE"; then
            sed -i -E "s/^WEB_PORT=.*/WEB_PORT=${web_port}/" "$ENV_FILE"
        else
            printf '\nWEB_PORT=%s\n' "$web_port" >> "$ENV_FILE"
        fi
        if grep -qE '^APP_PORT=' "$ENV_FILE"; then
            sed -i -E "s/^APP_PORT=.*/APP_PORT=${web_port}/" "$ENV_FILE"
        fi
        ok "已同步 .env：Web 端口 ${web_port}"
        return 0
    fi
    cat > "$ENV_FILE" <<EOF
WEB_PORT=${web_port}
APP_HOST=0.0.0.0
APP_PORT=${web_port}
SMTP_HOST=0.0.0.0
SMTP_PORT=${SMTP_PORT}
DB_FILE=${DATA_DIR}/temp_mail.db
TZ=Asia/Shanghai
FRONT_RETENTION_MINUTES=10
BACKEND_RETENTION_DAYS=7
MAILBOX_VALIDITY_HOURS=24
# 主域名（可选）：留空则在网页配置页填写并自动写入 config.json
# PRIMARY_DOMAIN=mail.example.com
# 部署上报地址（默认已内置，如需关闭可置空）
# DEPLOY_REPORT_URL=http://ac.atusu.cn/mail.php
EOF
    ok "已生成 .env (WEB_PORT=${web_port})"
}

read_env_port() {
    local p=""
    if [ -f "$ENV_FILE" ]; then
        p=$(grep -E '^WEB_PORT=' "$ENV_FILE" | head -n1 | cut -d= -f2 | tr -d '[:space:]')
    fi
    [ -n "$p" ] && echo "$p" || echo "$WEB_PORT_START"
}

sync_env_port() {
    local p="$1"
    [ -f "$ENV_FILE" ] || return 1
    sed -i -E "s/^WEB_PORT=.*/WEB_PORT=${p}/; s/^APP_PORT=.*/APP_PORT=${p}/" "$ENV_FILE"
}

write_deploy_marker() {
    mkdir -p "$DATA_DIR"
    echo "$(date +%s)-$(hostname 2>/dev/null || echo host)-$RANDOM$RANDOM" > "${DATA_DIR}/.deploy_marker"
}

install_unit() {
    local run_py="$1"
    cat > "$UNIT_FILE" <<EOF
[Unit]
Description=TempMail Temporary Mailbox Service
Documentation=https://atusu.cn/mail
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${run_py} ${APP_FILE}
Restart=always
RestartSec=3
LimitNOFILE=65535
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || true
}

http_ok() {
    local url="$1"
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --noproxy '*' --max-time 3 -o /dev/null "$url" 2>/dev/null && return 0
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T 3 -O /dev/null "$url" 2>/dev/null && return 0
    fi
    return 1
}

wait_service() {
    local web_port="$1" i
    info "等待服务启动..."
    for i in $(seq 1 25); do
        if http_ok "http://127.0.0.1:${web_port}/"; then
            ok "服务已启动"
            return 0
        fi
        sleep 2
    done
    warn "服务健康检查超时，请执行: journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
    return 1
}

print_summary() {
    local web_port="$1" public_ip
    public_ip=$(service_public_ip "$web_port")
    [ -n "$public_ip" ] || public_ip=$(get_public_ip)
    echo ""
    echo -e "${C_G}============================================================${C_0}"
    echo -e "${C_G}  📬 TempMail 部署完成 (源码版 v${SCRIPT_VERSION})${C_0}"
    echo -e "${C_G}============================================================${C_0}"
    echo -e "  程序版本 : ${C_B}v${APP_VERSION}${C_0}"
    echo -e "  安装目录 : ${C_B}${INSTALL_DIR}${C_0}"
    echo -e "  程序文件 : ${C_B}${APP_FILE}${C_0}"
    echo -e "  运行环境 : ${C_B}${RUN_PY}${C_0}"
    echo -e "  数据目录 : ${C_B}${DATA_DIR}${C_0}"
    echo -e "  配置文件 : ${C_B}${ENV_FILE}${C_0}"
    echo -e "  守护服务 : ${C_B}${SERVICE_NAME} (systemd, 已开机自启)${C_0}"
    if [ -n "$public_ip" ]; then
        echo -e "  服务器IP : ${C_B}${public_ip}${C_0}"
        echo -e "  访问链接 : ${C_B}http://${public_ip}:${web_port}${C_0}"
    else
        echo -e "  服务器IP : ${C_Y}未获取到 (可在 ${ENV_FILE} 中设置 PUBLIC_IP=x.x.x.x)${C_0}"
        echo -e "  访问链接 : ${C_B}http://<服务器公网IP>:${web_port}${C_0}"
    fi
    echo -e "  本机链接 : ${C_B}http://127.0.0.1:${web_port}${C_0}"
    echo -e "  SMTP端口 : ${C_B}${SMTP_PORT}${C_0} (MX 记录需指向服务器域名)"
    echo ""
    echo -e "  提示: 首次访问 ${C_B}http://${public_ip:-<服务器公网IP>}:${web_port}${C_0} 打开配置页"
    echo -e "        请先放行防火墙端口 ${web_port} (Web) 与 ${SMTP_PORT} (SMTP)"
    echo -e "        填写主域名并保存 → A 记录解析生效后即可用域名访问"
    echo -e "  常用命令:"
    echo -e "    systemctl status ${SERVICE_NAME}      # 查看状态"
    echo -e "    systemctl restart ${SERVICE_NAME}     # 重启"
    echo -e "    journalctl -u ${SERVICE_NAME} -f      # 实时日志"
    echo -e "  后续管理: ${C_Y}${INSTALL_DIR}/install.sh${C_0} 或重新运行本脚本"
    echo -e "  版权/授权: ${C_B}开源版 · 禁止商用 · 开发者 atusu.cn/mail${C_0}"
    echo -e "${C_G}============================================================${C_0}"
    echo ""
}

# ---------------- 动作 ----------------

do_install() {
    check_root
    check_systemd
    check_sources
    ensure_python
    setup_runtime

    handle_smtp_port

    local web_port
    web_port=$(read_env_port)
    # 端口占用处理：自家实例先停后复用；其它程序占用则自动顺延
    resolve_web_port "$web_port"
    web_port="$WEB_PORT_RESOLVED"
    write_env "$web_port"

    write_deploy_marker

    info "安装 systemd 服务..."
    install_unit "$RUN_PY"
    rm -f "${DATA_DIR}/.runtime_port"
    systemctl restart "${SERVICE_NAME}"
    ok "服务已启动"

    # 程序内部若仍发生端口顺延，按真实端口回写配置，保证反代端口一致
    reconcile_runtime_port "$web_port"
    web_port="$RUNTIME_PORT"

    wait_service "$web_port" || true
    print_summary "$web_port"
}

do_reinstall() {
    check_root
    check_systemd
    if [ ! -f "$APP_FILE" ]; then
        warn "未检测到已有安装 (${INSTALL_DIR})，将执行全新安装"
        do_install
        return
    fi
    info "重装: 停止服务 -> 替换程序 -> 重装依赖 -> 启动 (数据保留)"
    systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    do_install
}

do_reload() {
    check_root
    check_systemd
    if [ ! -f "$UNIT_FILE" ]; then
        err "未检测到已安装服务，请先执行: bash install.sh install"
        exit 1
    fi
    systemctl daemon-reload
    systemctl restart "${SERVICE_NAME}"
    ok "服务已重启"
    sleep 2
    run_status
}

do_start() {
    check_root
    check_systemd
    systemctl start "${SERVICE_NAME}" 2>/dev/null || { err "启动失败，服务可能未安装"; exit 1; }
    ok "服务已启动"
}

do_stop() {
    check_root
    check_systemd
    systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    ok "服务已停止"
}

do_clear_config() {
    local cfg="${DATA_DIR}/config.json"
    if [ -f "$cfg" ]; then
        rm -f "$cfg"
        ok "已清除主域名配置 (${cfg})"
        info "重启服务后生效: systemctl restart ${SERVICE_NAME}"
    else
        info "未找到 ${cfg}，无需清理"
    fi
}

do_uninstall() {
    check_root
    check_systemd
    warn "即将卸载 TempMail 服务 (程序与数据保留在 ${INSTALL_DIR})"
    systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    rm -f "$UNIT_FILE"
    systemctl daemon-reload 2>/dev/null || true
    ok "服务已卸载 (数据保留: ${DATA_DIR})"
}

do_full_uninstall() {
    check_root
    check_systemd
    systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    rm -f "$UNIT_FILE"
    systemctl daemon-reload 2>/dev/null || true
    [ -d "$INSTALL_DIR" ] && rm -rf "$INSTALL_DIR"
    ok "已完整清理完成 (服务 + 程序 + 安装目录 + 数据)"
}

do_full_reinstall() {
    local assume_yes="${1:-}"
    echo ""
    warn "完整清理重装将先删除: 服务 / 程序 / 安装目录 / 全部数据 (不可恢复)"
    echo -e "  安装目录: ${INSTALL_DIR}"
    if [ "$assume_yes" != "--yes" ]; then
        printf "确认继续? 输入 yes 回车继续: "
        if ! read -r ans; then
            echo ""
            info "已取消"
            return 0
        fi
        if [ "${ans:-}" != "yes" ]; then
            info "已取消"
            return 0
        fi
    fi
    do_full_uninstall
    ok "清理完成，开始重新部署..."
    do_install
}

run_status() {
    echo ""
    echo -e "${C_B}----------------- TempMail 运行状态 -----------------${C_0}"
    local web_port smtp_port state enabled dsize
    web_port="$(read_env_port)"
    smtp_port="$(grep -E '^SMTP_PORT=' "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2 | tr -d '[:space:]')"
    [ -n "$smtp_port" ] || smtp_port="25"

    if [ -f "$UNIT_FILE" ]; then
        state="$(systemctl is-active "${SERVICE_NAME}" 2>/dev/null)"
        enabled="$(systemctl is-enabled "${SERVICE_NAME}" 2>/dev/null)"
        dsize="$(du -sh "$DATA_DIR" 2>/dev/null | awk '{print $1}')"
        [ -n "$dsize" ] || dsize="-"

        if [ "$state" = "active" ]; then
            echo -e "  运行状态 : ${C_G}active (running)${C_0}"
        else
            echo -e "  运行状态 : ${C_R}${state:-unknown}${C_0}"
        fi
        echo -e "  开机自启 : ${enabled:-unknown}"
        echo -e "  Web 端口 : ${web_port}"
        echo -e "  SMTP端口 : ${smtp_port}"
        local pip
        pip="$(service_public_ip "$web_port")"
        [ -n "$pip" ] || pip="$(get_public_ip)"
        echo -e "  公网地址 : http://${pip:-<未获取到>}:${web_port}"
        echo -e "  本机链接 : http://127.0.0.1:${web_port}"
        echo -e "  安装目录 : ${INSTALL_DIR}"
        echo -e "  运行环境 : ${RUN_PY:-${VENV_DIR}/bin/python}"
        echo -e "  数据大小 : ${dsize}"
        echo -e "${C_B}------------------- 最近日志 (8 行) -----------------${C_0}"
        journalctl -u "${SERVICE_NAME}" -n 8 --no-pager 2>/dev/null | sed 's/^/  /' || true
    else
        echo -e "  运行状态 : ${C_Y}未安装 / 服务不存在${C_0}"
        echo -e "  安装目录 : ${INSTALL_DIR} ($([ -d "$INSTALL_DIR" ] && echo '存在' || echo '不存在'))"
        echo -e "  提示     : 执行 'bash install.sh install' 进行部署"
    fi
    echo -e "${C_B}-----------------------------------------------------${C_0}"
    echo ""
    return 0
}

finish_and_exit() {
    echo ""
    echo -e "${C_G}--------------------------------------------------${C_0}"
    ok "操作完成，脚本已自动退出"
    echo -e "${C_G}--------------------------------------------------${C_0}"
    echo ""
    exit 0
}

is_installed() {
    [ -f "$UNIT_FILE" ] || return 1
    systemctl is-enabled "${SERVICE_NAME}" >/dev/null 2>&1 || [ -f "$APP_FILE" ]
}

show_usage() {
    sed -n '2,/^set -u$/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
    exit 0
}

manage_menu() {
    local choice
    echo ""
    echo -e "${C_G}==============================================${C_0}"
    echo -e "${C_G}   📬 TempMail 管理菜单 (源码版 v${SCRIPT_VERSION})${C_0}"
    echo -e "${C_G}==============================================${C_0}"
    echo "  1) 重装            重装依赖并重启 (保留数据)"
    echo "  2) 完整清理重装    清理服务/目录/数据 后全新部署"
    echo "  3) 重载            重启服务"
    echo "  4) 卸载            停止并移除服务 (保留数据)"
    echo "  5) 完整清理卸载    删除服务/程序/目录/全部数据"
    echo "  6) 查看状态"
    echo "  7) 停止服务"
    echo "  8) 启动服务"
    echo "  9) 清除域名配置    删除 config.json (可重新绑定主域名)"
    echo "  0) 退出"
    echo ""
    printf "请选择操作 [0-9]: "

    if ! read -r choice; then
        echo ""
        info "未选择操作，已退出"
        exit 0
    fi
    choice="${choice//[[:space:]]/}"

    case "$choice" in
        1) do_reinstall ;;
        2) do_full_reinstall ;;
        3) do_reload ;;
        4) do_uninstall ;;
        5) do_full_uninstall ;;
        6) run_status ;;
        7) do_stop ;;
        8) do_start ;;
        9) do_clear_config ;;
        0|q|Q|exit|quit) info "已退出"; exit 0 ;;
        *) err "无效选择: ${choice}"; exit 1 ;;
    esac
    finish_and_exit
}

main() {
    parse_args "$@"
    local cmd="$CMD_ACTION"
    case "$cmd" in
        help|-h|--help) show_usage ;;
        install|auto|install-auto) do_install ;;
        reinstall) do_reinstall ;;
        full-reinstall|reset) do_full_reinstall ;;
        reload|restart) do_reload ;;
        start) do_start ;;
        stop|halt) do_stop ;;
        clear-config|reset-config) do_clear_config ;;
        uninstall)
            if [ "$CMD_PURGE" = "1" ] || [ "${2:-}" = "--purge" ]; then
                do_full_uninstall
            else
                do_uninstall
            fi
            ;;
        purge) do_full_uninstall ;;
        status) run_status ;;
        menu) manage_menu ;;
        "")
            if is_installed; then
                manage_menu
            else
                do_install
            fi
            ;;
        *)
            echo "用法: bash install.sh {install|reinstall|full-reinstall|reload|start|stop|clear-config|uninstall [--purge]|status|menu}"
            exit 1
            ;;
    esac
    exit 0
}

main "$@"
