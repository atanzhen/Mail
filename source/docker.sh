#!/usr/bin/env bash
# ============================================================
#  TempMail Docker 部署 / 管理脚本（源码版）  docker.sh
#
#  ★ 直接使用本目录下的 app.py + Dockerfile 构建镜像并启动，
#    服务器上不需要安装 Python / pip，也不用手工敲 docker 命令。
#    只需要 app.py、requirements.txt、Dockerfile 与本脚本在同一目录。
#
#  用法:
#    sudo bash docker.sh                  # 首次: 一键部署；已部署: 进入管理菜单
#    sudo bash docker.sh up               # 构建镜像并启动（已在运行则重建）
#    sudo bash docker.sh stop             # 停止容器
#    sudo bash docker.sh start            # 启动已有容器
#    sudo bash docker.sh restart          # 重启容器
#    sudo bash docker.sh status           # 查看运行状态 / 访问地址
#    sudo bash docker.sh logs             # 查看日志（-f 实时跟随）
#    sudo bash docker.sh rebuild          # 无缓存重新构建镜像并重启
#    sudo bash docker.sh down             # 停止并删除容器（保留镜像与数据）
#    sudo bash docker.sh purge            # 删除容器 + 镜像 + 数据（危险，需确认）
#    sudo bash docker.sh shell            # 进入容器 shell
#    sudo bash docker.sh menu             # 打开管理菜单
#
#  可选参数（跟在子命令后面）:
#    --web-port 6060          宿主机 Web 端口
#    --smtp-port 25           宿主机 SMTP 端口（容器内固定 25）
#    --data-dir ./data        数据目录（数据库 / config.json / 部署标记）
#    --tag tempmail:2.3.0     镜像标签
#    --name temp-mail         容器名
#    --pip-mirror URL         pip 镜像源（国内服务器构建加速，
#                             如 https://pypi.tuna.tsinghua.edu.cn/simple）
#    --no-cache               构建时不使用缓存
#    -y, --yes                跳过确认
#
#  说明:
#    - 依赖 Docker（无需 docker-compose，脚本自己 build + run）
#    - 容器 --restart unless-stopped，宿主机重启后自动拉起
#    - 数据持久化在宿主机 data 目录，删容器不丢数据
#    - app.py 或 Dockerfile 有改动时，再次执行 up 会自动重建镜像
# ============================================================
set -u
set -u

SCRIPT_VERSION="2.0"
APP_NAME="TempMail"
APP_VERSION="2.2"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 避免容器内绝对路径（如 /app/data）被宿主环境改写
if [ -n "${MSYSTEM:-}" ]; then
    export MSYS_NO_PATHCONV=1
    export MSYS2_ARG_CONV_EXCL='*'
fi

# ---------------- 可覆盖默认值 ----------------
IMAGE_TAG="${IMAGE_TAG:-tempmail:${APP_VERSION}}"
CONTAINER="${CONTAINER:-temp-mail}"
WEB_PORT="${WEB_PORT:-6060}"
SMTP_PORT="${SMTP_PORT:-25}"
DATA_DIR="${DATA_DIR:-${SCRIPT_DIR}/data}"
TZ_VALUE="${TZ_VALUE:-Asia/Shanghai}"
REPORT_URL="${DEPLOY_REPORT_URL:-http://ac.atusu.cn/mail.php}"
FRONT_RETENTION_MINUTES="${FRONT_RETENTION_MINUTES:-10}"
BACKEND_RETENTION_DAYS="${BACKEND_RETENTION_DAYS:-7}"
MAILBOX_VALIDITY_HOURS="${MAILBOX_VALIDITY_HOURS:-24}"
PIP_MIRROR="${PIP_INDEX_URL:-}"
NO_CACHE=0
ASSUME_YES=0
FOLLOW=0
WEB_PORT_EXPLICIT=0
SMTP_PORT_EXPLICIT=0

ENV_FILE="${SCRIPT_DIR}/.env.docker"
LOG_MAX_SIZE="10m"
LOG_MAX_FILE="3"
PORT_TRY_RANGE=100

C_G="\033[32m"; C_Y="\033[33m"; C_R="\033[31m"; C_B="\033[36m"; C_0="\033[0m"
info() { echo -e "${C_B}[INFO]${C_0} $*"; }
ok()   { echo -e "${C_G}[ OK ]${C_0} $*"; }
warn() { echo -e "${C_Y}[WARN]${C_0} $*"; }
err()  { echo -e "${C_R}[FAIL]${C_0} $*"; }

usage() {
    sed -n '2,/^set -u$/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
    exit 0
}

# ============================================================
#  环境检查
# ============================================================

need_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        err "未检测到 docker 命令"
        echo "  安装 Docker（任选一种）:"
        echo "    curl -fsSL https://get.docker.com | sh"
        echo "    apt install -y docker.io && systemctl enable --now docker"
        echo "  安装后重新执行: bash docker.sh"
        exit 1
    fi
    if ! docker info >/dev/null 2>&1; then
        err "Docker 服务未运行或当前用户无权限"
        echo "  启动服务 : sudo systemctl start docker"
        echo "  加权限   : sudo usermod -aG docker \$USER  (重新登录生效)"
        echo "  临时提权 : sudo bash docker.sh up"
        exit 1
    fi
    if [ "$(id -u 2>/dev/null || echo 0)" != "0" ] && ! docker ps >/dev/null 2>&1; then
        err "当前用户无法访问 Docker（权限不足）"
        exit 1
    fi
}

check_sources() {
    local miss=0
    for f in app.py Dockerfile requirements.txt; do
        if [ ! -f "${SCRIPT_DIR}/${f}" ]; then
            err "缺少文件: ${SCRIPT_DIR}/${f}"
            miss=1
        fi
    done
    if [ "$miss" = "1" ]; then
        echo "  请把本脚本与 app.py / Dockerfile / requirements.txt 放在同一目录"
        exit 1
    fi
}

# ============================================================
#  配置读写
# ============================================================

load_env() {
    [ -f "$ENV_FILE" ] || return 0
    local k v
    while IFS='=' read -r k v; do
        case "$k" in
            ''|\#*) continue ;;
        esac
        v="${v%\"}"; v="${v#\"}"
        [ -n "$v" ] || continue
        case "$k" in
            WEB_PORT)                   WEB_PORT="$v" ;;
            SMTP_PORT)                  SMTP_PORT="$v" ;;
            DATA_DIR)                   DATA_DIR="$v" ;;
            IMAGE_TAG)                  IMAGE_TAG="$v" ;;
            CONTAINER)                  CONTAINER="$v" ;;
            TZ_VALUE)                   TZ_VALUE="$v" ;;
            DEPLOY_REPORT_URL)          REPORT_URL="$v" ;;
            FRONT_RETENTION_MINUTES)    FRONT_RETENTION_MINUTES="$v" ;;
            BACKEND_RETENTION_DAYS)     BACKEND_RETENTION_DAYS="$v" ;;
            MAILBOX_VALIDITY_HOURS)     MAILBOX_VALIDITY_HOURS="$v" ;;
            PIP_MIRROR)                 PIP_MIRROR="$v" ;;
        esac
    done < "$ENV_FILE"
}

save_env() {
    cat > "$ENV_FILE" <<EOF
# TempMail Docker 部署配置（由 docker.sh 自动生成，可手工修改后重启生效）
WEB_PORT=${WEB_PORT}
SMTP_PORT=${SMTP_PORT}
DATA_DIR=${DATA_DIR}
IMAGE_TAG=${IMAGE_TAG}
CONTAINER=${CONTAINER}
TZ_VALUE=${TZ_VALUE}
DEPLOY_REPORT_URL=${REPORT_URL}
FRONT_RETENTION_MINUTES=${FRONT_RETENTION_MINUTES}
BACKEND_RETENTION_DAYS=${BACKEND_RETENTION_DAYS}
MAILBOX_VALIDITY_HOURS=${MAILBOX_VALIDITY_HOURS}
PIP_MIRROR=${PIP_MIRROR}
EOF
}

# ============================================================
#  端口工具
# ============================================================

port_in_use() {
    local p="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}$" && return 0
        return 1
    elif command -v netstat >/dev/null 2>&1; then
        netstat -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}$" && return 0
        return 1
    elif command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"${p}" -sTCP:LISTEN >/dev/null 2>&1 && return 0
        return 1
    fi
    (exec 3<>"/dev/tcp/127.0.0.1/${p}") >/dev/null 2>&1 && return 0
    return 1
}

pick_free_port() {
    local start="$1" i p
    for i in $(seq 0 "$PORT_TRY_RANGE"); do
        p=$((start + i))
        port_in_use "$p" || { echo "$p"; return 0; }
    done
    echo ""
    return 1
}

# 端口是否已被本脚本的容器占用
port_owned_by_us() {
    local p="$1"
    container_exists || return 1
    docker port "$CONTAINER" 2>/dev/null | awk -F: '{print $NF}' | grep -qx "$p"
}

resolve_ports() {
    local cur
    # 容器已在运行：未显式指定端口时沿用其当前映射，避免误判为占用
    if container_running && [ "$WEB_PORT_EXPLICIT" != "1" ]; then
        cur="$(docker port "$CONTAINER" 6060/tcp 2>/dev/null | awk -F: 'NR==1{print $NF}')"
        [ -n "${cur:-}" ] && WEB_PORT="$cur"
    fi
    if port_in_use "$WEB_PORT" && ! port_owned_by_us "$WEB_PORT"; then
        if [ "$WEB_PORT_EXPLICIT" = "1" ]; then
            err "指定的 Web 端口 ${WEB_PORT} 已被其他程序占用，请换一个: --web-port 6080"
            exit 1
        fi
        local npid
        npid="$(pick_free_port "$WEB_PORT")"
        if [ -z "$npid" ]; then
            err "端口 ${WEB_PORT} 起始的 ${PORT_TRY_RANGE} 个端口都被占用，请用 --web-port 指定"
            exit 1
        fi
        warn "Web 端口 ${WEB_PORT} 已被占用，自动改用 ${npid}"
        WEB_PORT="$npid"
    fi
    if port_in_use "$SMTP_PORT" && ! port_owned_by_us "$SMTP_PORT"; then
        if [ "$SMTP_PORT_EXPLICIT" = "1" ]; then
            warn "指定的 SMTP 端口 ${SMTP_PORT} 被占用，邮件接收可能不可用（Web 不受影响）"
            return 0
        fi
        local nsp
        nsp="$(pick_free_port 2525)"
        if [ -n "$nsp" ]; then
            warn "SMTP 端口 ${SMTP_PORT} 已被占用（postfix/exim 等），自动改用 ${nsp}"
            warn "注意：改端口后外部投递需显式指定端口，MX 收信建议使用标准 25"
            SMTP_PORT="$nsp"
        else
            warn "SMTP 端口 ${SMTP_PORT} 被占用且无空闲替代端口，邮件接收可能不可用（Web 不受影响）"
        fi
    fi
}

# ============================================================
#  容器 / 镜像
# ============================================================

container_exists() {
    docker inspect "$CONTAINER" >/dev/null 2>&1
}

container_running() {
    container_exists || return 1
    [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]
}

image_exists() {
    docker image inspect "$IMAGE_TAG" >/dev/null 2>&1
}

# 源码摘要：app.py + Dockerfile + requirements.txt，用于判断是否需要重新构建
source_digest() {
    local out=""
    if command -v sha256sum >/dev/null 2>&1; then
        out="$(sha256sum "${SCRIPT_DIR}/app.py" "${SCRIPT_DIR}/Dockerfile" "${SCRIPT_DIR}/requirements.txt" 2>/dev/null | sha256sum | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
        out="$(shasum -a 256 "${SCRIPT_DIR}/app.py" "${SCRIPT_DIR}/Dockerfile" "${SCRIPT_DIR}/requirements.txt" 2>/dev/null | shasum -a 256 | awk '{print $1}')"
    fi
    printf '%s' "${out:0:16}"
}

image_source() {
    docker image inspect -f '{{index .Config.Labels "org.tempmail.source"}}' "$IMAGE_TAG" 2>/dev/null
}

build_image() {
    local ctx="${SCRIPT_DIR}/.docker-build"
    local buildfile="${ctx}/Dockerfile"

    # 只把 app.py / requirements.txt / Dockerfile 放进构建上下文，
    # 避免把 bin/ 里的上百 MB 二进制一起发给 docker 守护进程。
    rm -rf "$ctx"
    mkdir -p "$ctx"
    cp -f "${SCRIPT_DIR}/app.py" "${ctx}/app.py" || { err "复制 app.py 失败"; return 1; }
    cp -f "${SCRIPT_DIR}/requirements.txt" "${ctx}/requirements.txt" || { err "复制 requirements.txt 失败"; return 1; }

    if [ -n "$PIP_MIRROR" ]; then
        sed "s|^RUN pip install --no-cache-dir -r requirements.txt|RUN pip install --no-cache-dir -i ${PIP_MIRROR} -r requirements.txt|" \
            "${SCRIPT_DIR}/Dockerfile" > "$buildfile" 2>/dev/null
        if grep -q -- "-i ${PIP_MIRROR}" "$buildfile" 2>/dev/null; then
            info "使用 pip 镜像源: ${PIP_MIRROR}"
        else
            cp -f "${SCRIPT_DIR}/Dockerfile" "$buildfile"
            warn "pip 镜像源注入失败，使用原始 Dockerfile"
        fi
    else
        cp -f "${SCRIPT_DIR}/Dockerfile" "$buildfile"
    fi

    info "构建镜像 ${IMAGE_TAG}（首次需下载基础镜像与依赖，请耐心等待）"
    local args
    args=(-t "$IMAGE_TAG" -f "$buildfile")
    [ "$NO_CACHE" = "1" ] && args+=(--no-cache)
    args+=(--label "org.tempmail.version=${APP_VERSION}"
           --label "org.tempmail.script=${SCRIPT_VERSION}"
           --label "org.tempmail.source=$(source_digest)")
    if docker build "${args[@]}" "$ctx"; then
        ok "镜像构建完成: ${IMAGE_TAG}"
        rm -rf "$ctx"
        return 0
    fi
    err "镜像构建失败，请检查网络 / 磁盘空间后重试"
    echo "  构建目录保留在 ${ctx}，可对比排查"
    echo "  提示: 国内服务器可加 --pip-mirror https://pypi.tuna.tsinghua.edu.cn/simple"
    echo "  也可给 Docker 配置镜像加速后重试: /etc/docker/daemon.json"
    return 1
}

run_container() {
    mkdir -p "$DATA_DIR"
    local envs
    envs=(
        -e "TZ=${TZ_VALUE}"
        -e "APP_HOST=0.0.0.0"
        -e "APP_PORT=${WEB_PORT}"
        -e "SMTP_HOST=0.0.0.0"
        -e "SMTP_PORT=25"
        -e "DB_FILE=/app/data/temp_mail.db"
        -e "CONFIG_FILE=/app/data/config.json"
        -e "FRONT_RETENTION_MINUTES=${FRONT_RETENTION_MINUTES}"
        -e "BACKEND_RETENTION_DAYS=${BACKEND_RETENTION_DAYS}"
        -e "MAILBOX_VALIDITY_HOURS=${MAILBOX_VALIDITY_HOURS}"
        -e "DEPLOY_REPORT_URL=${REPORT_URL}"
    )

    docker run -d \
        --name "$CONTAINER" \
        --restart unless-stopped \
        --log-opt "max-size=${LOG_MAX_SIZE}" \
        --log-opt "max-file=${LOG_MAX_FILE}" \
        -p "${WEB_PORT}:${WEB_PORT}" \
        -p "${SMTP_PORT}:25" \
        -v "${DATA_DIR}:/app/data" \
        "${envs[@]}" \
        "$IMAGE_TAG" >/dev/null
}

wait_ready() {
    local i=0 max=60
    info "等待服务就绪…"
    while [ "$i" -lt "$max" ]; do
        if docker exec "$CONTAINER" python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:${WEB_PORT}/api/setup/status',timeout=2)" >/dev/null 2>&1; then
            ok "服务已就绪"
            return 0
        fi
        if ! container_running; then
            err "容器启动后退出了，最近日志:"
            docker logs --tail 40 "$CONTAINER" 2>&1 | sed 's/^/    /'
            return 1
        fi
        sleep 2
        i=$((i + 2))
    done
    warn "60 秒内未确认就绪，请查看日志: docker logs -f ${CONTAINER}"
    return 1
}

get_public_ip() {
    local ip=""
    command -v curl >/dev/null 2>&1 || { echo ""; return 0; }
    ip="$(curl -s --noproxy '*' --max-time 5 "http://127.0.0.1:${WEB_PORT}/api/setup/status" 2>/dev/null \
          | grep -o '"public_ip":"[0-9.]*"' | head -n1 | cut -d'"' -f4)"
    [ -n "$ip" ] && { echo "$ip"; return 0; }
    for url in http://ip.3322.net https://ipv4.icanhazip.com https://api.ipify.org; do
        ip="$(curl -4 -fsS --noproxy '*' --max-time 3 "$url" 2>/dev/null \
              | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' | head -n1)"
        [ -n "$ip" ] && { echo "$ip"; return 0; }
    done
    echo ""
}

# ============================================================
#  命令实现
# ============================================================

print_summary() {
    local ip size
    ip="$(get_public_ip)"
    size="$(du -sh "$DATA_DIR" 2>/dev/null | awk '{print $1}')"
    [ "${size:-0}" = "0" ] && size="空"
    echo ""
    echo -e "${C_G}============================================================${C_0}"
    echo -e "${C_G}  📬 TempMail Docker 部署完成 (源码版 v${SCRIPT_VERSION})${C_0}"
    echo -e "${C_G}============================================================${C_0}"
    echo -e "  程序版本 : ${C_B}v${APP_VERSION}${C_0} (app.py + Dockerfile 构建)"
    echo -e "  镜像     : ${C_B}${IMAGE_TAG}${C_0}"
    echo -e "  容器     : ${C_B}${CONTAINER}${C_0} ($(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null))"
    echo -e "  数据目录 : ${C_B}${DATA_DIR}${C_0}${size:+ (${size})}"
    echo -e "  配置文件 : ${C_B}${ENV_FILE}${C_0}"
    echo -e "  Web 端口 : ${C_B}${WEB_PORT}${C_0}（容器内同端口）"
    echo -e "  SMTP端口 : ${C_B}${SMTP_PORT}${C_0} -> 容器 25"
    if [ -n "$ip" ]; then
        echo -e "  服务器IP : ${C_B}${ip}${C_0}"
        echo -e "  访问链接 : ${C_B}http://${ip}:${WEB_PORT}${C_0}"
    else
        echo -e "  访问链接 : ${C_B}http://<服务器公网IP>:${WEB_PORT}${C_0}"
    fi
    echo -e "  本机链接 : ${C_B}http://127.0.0.1:${WEB_PORT}${C_0}"
    echo ""
    echo -e "  下一步: 浏览器打开上面的访问链接，填写主域名保存"
    echo -e "          DNS: A 记录 -> ${ip:-服务器公网IP}，MX 记录 -> 你的域名"
    echo -e "          端口映射: 需在防火墙/安全组放行 ${WEB_PORT} 与 ${SMTP_PORT}"
    echo -e "  常用命令:"
    echo -e "    bash docker.sh status      # 状态"
    echo -e "    bash docker.sh logs -f     # 实时日志"
    echo -e "    bash docker.sh restart     # 重启"
    echo -e "    bash docker.sh down        # 停止并删除容器"
    echo -e "  版权/授权: ${C_B}开源版 · 禁止商用 · 开发者 atusu.cn/mail${C_0}"
    echo -e "${C_G}============================================================${C_0}"
    echo ""
}

do_up() {
    need_docker
    check_sources

    if container_running; then
        warn "容器 ${CONTAINER} 已在运行，将按当前配置重建（数据保留）"
    fi

    resolve_ports
    save_env

    local need_build=0 cur_src
    if ! image_exists || [ "$NO_CACHE" = "1" ]; then
        need_build=1
    else
        cur_src="$(source_digest)"
        if [ -n "$cur_src" ] && [ "$(image_source)" != "$cur_src" ]; then
            info "检测到 app.py / Dockerfile 有改动，自动重新构建镜像"
            need_build=1
        fi
    fi
    if [ "$need_build" = "1" ]; then
        build_image || exit 1
    else
        info "镜像 ${IMAGE_TAG} 已是最新，跳过构建"
    fi

    if container_exists; then
        docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    fi
    if ! run_container; then
        err "容器启动失败"
        exit 1
    fi
    wait_ready || true
    print_summary
}

do_start() {
    need_docker
    if ! container_exists; then
        warn "容器不存在，执行首次部署"
        do_up
        return 0
    fi
    if container_running; then
        ok "容器已在运行"
        return 0
    fi
    docker start "$CONTAINER" >/dev/null && ok "容器已启动"
    wait_ready || true
    print_summary
}

do_stop() {
    need_docker
    container_exists || { warn "容器不存在，无需停止"; return 0; }
    if container_running; then
        docker stop "$CONTAINER" >/dev/null && ok "容器已停止（数据保留在 ${DATA_DIR}）"
    else
        ok "容器本就处于停止状态"
    fi
}

do_restart() {
    need_docker
    container_exists || { warn "容器不存在，执行首次部署"; do_up; return 0; }
    docker restart "$CONTAINER" >/dev/null && ok "容器已重启"
    wait_ready || true
}

do_status() {
    need_docker
    if ! container_exists; then
        warn "尚未部署（容器 ${CONTAINER} 不存在）"
        echo "  执行部署: bash docker.sh up"
        return 0
    fi
    local status started ports size ip
    status="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null)"
    started="$(docker inspect -f '{{.State.StartedAt}}' "$CONTAINER" 2>/dev/null)"
    ports="$(docker port "$CONTAINER" 2>/dev/null | tr '\n' ' ')"
    size="$(du -sh "$DATA_DIR" 2>/dev/null | awk '{print $1}')"
    [ "${size:-0}" = "0" ] && size="空"
    echo ""
    echo "  容器     : ${CONTAINER}  (${status})"
    echo "  镜像     : $(docker inspect -f '{{.Config.Image}}' "$CONTAINER" 2>/dev/null)"
    echo "  启动时间 : ${started}"
    echo "  端口映射 : ${ports:-未映射}"
    echo "  数据目录 : ${DATA_DIR}${size:+ (${size})}"
    echo "  重启策略 : $(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER" 2>/dev/null)"
    if [ "$status" = "running" ]; then
        ip="$(get_public_ip)"
        echo "  访问地址 : http://${ip:-<服务器公网IP>}:${WEB_PORT}"
        echo "  本机地址 : http://127.0.0.1:${WEB_PORT}"
        echo "  健康状态 : $(docker exec "$CONTAINER" python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:${WEB_PORT}/api/setup/status',timeout=3)" >/dev/null 2>&1 && echo '正常' || echo '异常（查看 logs）')"
        echo ""
        echo "  最近日志:"
        docker logs --tail 12 "$CONTAINER" 2>&1 | sed 's/^/    /'
    fi
    echo ""
}

do_logs() {
    need_docker
    container_exists || { err "容器不存在，无法查看日志"; exit 1; }
    if [ "$FOLLOW" = "1" ]; then
        docker logs -f --tail 200 "$CONTAINER"
    else
        docker logs --tail 200 "$CONTAINER" 2>&1
        echo ""
        info "实时跟随: bash docker.sh logs -f"
    fi
}

do_rebuild() {
    NO_CACHE=1
    need_docker
    check_sources
    resolve_ports
    save_env
    build_image || exit 1
    container_exists && docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    run_container || { err "容器启动失败"; exit 1; }
    wait_ready || true
    print_summary
}

do_down() {
    need_docker
    if container_exists; then
        docker rm -f "$CONTAINER" >/dev/null && ok "容器已删除（镜像与数据保留）"
    else
        warn "容器不存在"
    fi
    echo "  数据仍在: ${DATA_DIR}"
    echo "  重新部署: bash docker.sh up"
}

do_purge() {
    need_docker
    echo -e "${C_R}即将删除：容器 ${CONTAINER}、镜像 ${IMAGE_TAG}、数据目录 ${DATA_DIR}${C_0}"
    echo -e "${C_R}⚠️  此操作不可恢复，数据库中的邮件与域名配置将全部丢失！${C_0}"
    if [ "$ASSUME_YES" != "1" ]; then
        printf "确认请输入 yes: "
        read -r answer
        [ "$answer" = "yes" ] || { info "已取消"; return 0; }
    fi
    container_exists && docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    docker image rm -f "$IMAGE_TAG" >/dev/null 2>&1 || true
    if [ -d "$DATA_DIR" ] && [ "$DATA_DIR" != "/" ] && [ "$DATA_DIR" != "$SCRIPT_DIR" ]; then
        rm -rf -- "$DATA_DIR"
    fi
    rm -f "$ENV_FILE"
    ok "已清理完成"
}

do_shell() {
    need_docker
    container_running || { err "容器未运行"; exit 1; }
    docker exec -it "$CONTAINER" /bin/sh
}

do_menu() {
    need_docker
    while true; do
        echo ""
        echo -e "${C_B}============================================================${C_0}"
        echo -e "${C_B}  📬 ${APP_NAME} Docker 管理菜单 (源码版 v${SCRIPT_VERSION})${C_0}"
        echo -e "${C_B}============================================================${C_0}"
        if container_exists; then
            echo "  当前状态: 容器 $(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null) · Web ${WEB_PORT} · SMTP ${SMTP_PORT}"
        else
            echo "  当前状态: 尚未部署"
        fi
        echo "  ----------------------------------------------------------"
        echo "   1) 部署 / 重建容器（保留数据）"
        echo "   2) 停止容器"
        echo "   3) 重启容器"
        echo "   4) 查看状态"
        echo "   5) 查看日志"
        echo "   6) 实时日志（Ctrl+C 退出）"
        echo "   7) 无缓存重新构建镜像"
        echo "   8) 删除容器（保留镜像与数据）"
        echo "   9) 彻底清理（容器+镜像+数据）"
        echo "   s) 进入容器 shell"
        echo "   0) 退出"
        echo "  ----------------------------------------------------------"
        printf "  请选择: "
        read -r choice
        case "$choice" in
            1) do_up ;;
            2) do_stop ;;
            3) do_restart ;;
            4) do_status ;;
            5) do_logs ;;
            6) FOLLOW=1; do_logs ;;
            7) do_rebuild ;;
            8) do_down ;;
            9) do_purge ;;
            s|S) do_shell ;;
            0|q|Q) break ;;
            '') ;;
            *) warn "无效选项: ${choice}" ;;
        esac
    done
}

# ============================================================
#  参数解析
# ============================================================

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            up|deploy|install)   CMD="up" ;;
            start)               CMD="start" ;;
            stop)                CMD="stop" ;;
            restart|reload)      CMD="restart" ;;
            status|info|ps)      CMD="status" ;;
            logs|log)            CMD="logs" ;;
            rebuild|build)       CMD="rebuild" ;;
            down|remove|rm)      CMD="down" ;;
            purge|uninstall)     CMD="purge" ;;
            shell|exec|bash)     CMD="shell" ;;
            menu)                CMD="menu" ;;
            help|-h|--help)      CMD="help" ;;
            -f|--follow)         FOLLOW=1 ;;
            -y|--yes)            ASSUME_YES=1 ;;
            --no-cache)          NO_CACHE=1 ;;
            --web-port)          WEB_PORT="${2:-}"; WEB_PORT_EXPLICIT=1; shift || true ;;
            --smtp-port)         SMTP_PORT="${2:-}"; SMTP_PORT_EXPLICIT=1; shift || true ;;
            --data-dir)          DATA_DIR="${2:-}"; shift || true ;;
            --tag)               IMAGE_TAG="${2:-}"; shift || true ;;
            --name)              CONTAINER="${2:-}"; shift || true ;;
            --pip-mirror)        PIP_MIRROR="${2:-}"; shift || true ;;
            --web-port=*)        WEB_PORT="${1#*=}"; WEB_PORT_EXPLICIT=1 ;;
            --smtp-port=*)       SMTP_PORT="${1#*=}"; SMTP_PORT_EXPLICIT=1 ;;
            --data-dir=*)        DATA_DIR="${1#*=}" ;;
            --tag=*)             IMAGE_TAG="${1#*=}" ;;
            --name=*)            CONTAINER="${1#*=}" ;;
            --pip-mirror=*)      PIP_MIRROR="${1#*=}" ;;
            *)
                err "未知参数: $1"
                echo "  查看帮助: bash docker.sh help"
                exit 1
                ;;
        esac
        shift
    done
}

# ============================================================
#  入口
# ============================================================

main() {
    load_env
    CMD=""
    parse_args "$@"

    # 数据目录转绝对路径（相对路径以脚本目录为基准）
    case "$DATA_DIR" in
        /*) ;;
        *) DATA_DIR="${SCRIPT_DIR}/${DATA_DIR}" ;;
    esac

    if [ -z "$CMD" ]; then
        if container_exists 2>/dev/null; then
            do_menu
        else
            CMD="up"
        fi
    fi

    case "$CMD" in
        up)      do_up ;;
        start)   do_start ;;
        stop)    do_stop ;;
        restart) do_restart ;;
        status)  do_status ;;
        logs)    do_logs ;;
        rebuild) do_rebuild ;;
        down)    do_down ;;
        purge)   do_purge ;;
        shell)   do_shell ;;
        menu)    do_menu ;;
        help|'') usage ;;
    esac
}

main "$@"
