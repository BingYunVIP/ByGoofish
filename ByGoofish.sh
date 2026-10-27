#!/usr/bin/env bash
# Native deployment and maintenance. Run with: bash ByGoofish.sh
set -Eeuo pipefail
umask 077

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
HELPER="$ROOT/scripts/native_deploy.py"
INTERNAL=no
if [[ ${1:-} == --internal ]]; then
    ROOT=$2 HELPER=$3
    INTERNAL=yes
    shift 3
fi
readonly ROOT HELPER
STATE="$ROOT/.local/bygoofish"
SERVICE=bygoofish.service
REMOTE=https://github.com/BingYunVIP/ByGoofish.git
RED='' GREEN='' YELLOW='' CYAN='' RESET=''
if [[ -t 1 && ${TERM:-dumb} != dumb && -z ${NO_COLOR+x} ]]; then
    RED=$'\033[31m' GREEN=$'\033[32m' YELLOW=$'\033[33m' CYAN=$'\033[36m' RESET=$'\033[0m'
fi
info() { printf '%s%s%s\n' "$CYAN" "$*" "$RESET"; }
ok() { printf '%s✓ %s%s\n' "$GREEN" "$*" "$RESET"; }
warn() { printf '%s! %s%s\n' "$YELLOW" "$*" "$RESET"; }
die() { printf '%s✗ %s%s\n' "$RED" "$*" "$RESET" >&2; exit 1; }
require_root() { [[ $EUID == 0 ]] || die "$1"; }
confirm() {
    local answer
    read -r -p "$1 [Y/n]：" answer || return 1
    [[ -z $answer || $answer == [Yy] || $answer == [Yy][Ee][Ss] ]]
}
service_active() { systemctl is-active --quiet "$SERVICE"; }

load_settings() {
    local output
    output=$(python3 "$HELPER" settings "$ROOT")
    mapfile -t SETTINGS <<< "$output"
    HOST=${SETTINGS[0]} PORT=${SETTINGS[1]} DB=${SETTINGS[2]}
    RUN_USER=${SETTINGS[3]} RUN_HOME=${SETTINGS[4]} EXISTS=${SETTINGS[5]}
    BROWSER_PATH=${SETTINGS[6]:-}
    SERVICE_PATH=${SETTINGS[7]:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}
}
load_resources() {
    local output
    output=$(python3 "$HELPER" resources "$ROOT")
    mapfile -t RESOURCES <<< "$output"
    CPUS=${RESOURCES[0]} MEMORY=${RESOURCES[1]} AVAILABLE=${RESOURCES[2]}
    SWAP=${RESOURCES[3]} DISK=${RESOURCES[4]} BROWSERS=${RESOURCES[5]}
}
run_owner() {
    local -a browser_env=()
    [[ -z ${BROWSER_PATH:-} ]] || browser_env=("PLAYWRIGHT_BROWSERS_PATH=$BROWSER_PATH")
    if [[ $(id -un) == "$RUN_USER" ]]; then
        env HOME="$RUN_HOME" "${browser_env[@]}" "$@"
    else
        runuser -u "$RUN_USER" -- env HOME="$RUN_HOME" "${browser_env[@]}" "$@"
    fi
}
project_git() {
    local directory=$1 owner
    shift
    owner=$(stat -c %U "$directory")
    if [[ $EUID == 0 && $owner != root ]]; then
        runuser -u "$owner" -- git -C "$directory" "$@"
    else
        git -C "$directory" "$@"
    fi
}
progress() { info "[$1] $2"; }
logged() {
    "$@" >> "$LOG" 2>&1
}
download() {
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 --fail --location --silent --show-error \
        --connect-timeout 15 --max-time 300 --retry 2 --output "$2.part" "$1"
    mv -- "$2.part" "$2"
}

validate_platform() {
    local distro=$1 version=$2 architecture=$3 minimum
    case "$distro" in
        debian) minimum=12 ;;
        ubuntu) minimum=22.04 ;;
        *) warn "仅支持 Debian 12+ 或 Ubuntu 22.04+。"; return 1 ;;
    esac
    [[ $version =~ ^[0-9]+([.][0-9]+)*$ ]] || return 1
    dpkg --compare-versions "$version" ge "$minimum" || { warn "系统版本过旧。"; return 1; }
    [[ $architecture == x86_64 || $architecture == aarch64 ]] || { warn "仅支持 amd64、arm64。"; return 1; }
}
check_system() {
    [[ $(uname -s) == Linux ]] || die "请在 Linux 中运行。"
    # /etc/os-release is a root-managed OS file, not application input.
    # shellcheck disable=SC1091
    source /etc/os-release
    DISTRO=$ID VERSION=$VERSION_ID ARCH=$(uname -m)
    validate_platform "$DISTRO" "$VERSION" "$ARCH" || die "当前系统不满足部署条件。"
    [[ -d /run/systemd/system ]] || die "未检测到运行中的 systemd；请先启用 systemd 后再部署。"
    require_root "安装需要 root 权限，请使用 sudo bash ByGoofish.sh。"
    [[ -f $ROOT/Start.py && -f $ROOT/requirements.lock && -f $ROOT/frontend/package-lock.json ]] ||
        die "项目文件不完整；请把脚本放在完整项目根目录。"
    [[ $ROOT != *$'\n'* && $ROOT != *$'\r'* ]] || die "目录名称包含不支持的控制字符。"
}
prepare_state() {
    if [[ ! -d $ROOT/.local ]]; then
        install -d -m 700 -o "$RUN_USER" "$ROOT/.local"
    fi
    run_owner test -x "$ROOT/.local" || die "服务用户无法访问项目 .local 目录，请先修正该目录权限。"
    mkdir -p "$STATE"
    chmod 700 "$STATE"
    LOG="$STATE/deploy-$(date +%Y%m%d-%H%M%S)-$$.log"
    touch "$LOG"
    chmod 600 "$LOG"
    chown -R "$RUN_USER:" "$STATE"
    info "详细部署日志：$LOG"
    exec {DEPLOY_LOCK}>"$STATE/deploy.lock"
    flock -n "$DEPLOY_LOCK" || die "另一个部署任务正在运行，请稍后再试。"
}
check_capacity() {
    load_resources
    info "检测：${CPUS}核 / ${MEMORY}MB内存 / ${SWAP}MB Swap / ${DISK}MB可用磁盘"
    (( MEMORY >= 960 )) || die "内存不足1GB，无法可靠运行应用和浏览器；请升级配置。"
    (( DISK >= 6144 )) || die "至少需要6GB可用磁盘用于依赖、构建和回退。"
    if (( MEMORY < 2048 && SWAP < 2048 )); then
        warn "当前内存较小，安装和浏览器运行可能需要 Swap。"
        if confirm "是否创建专用2GB Swap"; then
            (( DISK >= 8192 )) || die "创建 Swap 并完成安装需要至少8GB可用磁盘。"
            create_swap
        else
            warn "已保留原有 Swap 配置；内存不足时安装可能失败。"
        fi
    fi
    if (( MEMORY < 4096 )); then
        BUILD_MEMORY=768
        DOWNLOADS=1
    else
        BUILD_MEMORY=2048
        DOWNLOADS=3
    fi
    if (( CPUS <= 2 )); then DOWNLOADS=1; fi
    info "部署策略：单应用进程，浏览器并发${BROWSERS}，分阶段安装和构建。"
}
create_swap() {
    local swapfile=/var/lib/bygoofish/swapfile filesystem
    [[ ! -e $swapfile ]] || die "专用 Swap 文件已存在；请检查后重试，不会覆盖。"
    filesystem=$(findmnt -n -o FSTYPE -T /var/lib)
    [[ $filesystem == ext4 || $filesystem == xfs ]] || die "当前文件系统不支持此自动 Swap 流程；请手动配置 Swap。"
    mkdir -p /var/lib/bygoofish
    chmod 700 /var/lib/bygoofish
    if ! dd if=/dev/zero of="$swapfile" bs=1M count=2048 status=none; then
        rm -f -- "$swapfile"
        die "Swap 文件写入失败，已清理未完成的文件。"
    fi
    chmod 600 "$swapfile"
    if ! mkswap "$swapfile" >> "$LOG" 2>&1; then
        rm -f -- "$swapfile"
        die "Swap 格式化失败，未修改原有 Swap。"
    fi
    if ! swapon "$swapfile" >> "$LOG" 2>&1; then
        rm -f -- "$swapfile"
        die "启用 Swap 失败；原有 Swap 未改动。"
    fi
    if ! grep -Fq "$swapfile " /etc/fstab; then
        printf '%s none swap sw 0 0\n' "$swapfile" >> /etc/fstab
    fi
    ok "专用 Swap 已启用并配置开机加载。"
}
install_system_packages() {
    progress 1/6 "检查和安装系统依赖（详细输出记录到部署日志）"
    logged env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 update
    logged env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends \
        ca-certificates curl git xz-utils tar python3 python3-venv python3-pip python3-dev \
        build-essential pkg-config libffi-dev libssl-dev xvfb xauth iproute2 procps util-linux rsync
}
select_python() {
    PYTHON_BIN=$(command -v python3)
    if "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3,11))'; then
        return
    fi
    progress 2/6 "安装项目专用 Python 3.12，不修改系统 Python"
    local bootstrap="$STATE/tools/bootstrap"
    if [[ ! -x $bootstrap/bin/uv ]]; then
        logged run_owner python3 -m venv "$bootstrap"
        logged run_owner "$bootstrap/bin/python" -m pip install --disable-pip-version-check 'uv==0.12.7'
    fi
    logged run_owner env UV_PYTHON_INSTALL_DIR="$STATE/python" UV_CONCURRENT_DOWNLOADS="$DOWNLOADS" \
        "$bootstrap/bin/uv" python install 3.12
    PYTHON_BIN=$(run_owner env UV_PYTHON_INSTALL_DIR="$STATE/python" "$bootstrap/bin/uv" python find --managed-python 3.12)
}
lock_matches() {
    "$1" "$HELPER" lock-check "$2/requirements.lock" >> "$LOG" 2>&1
}
prepare_python() {
    local source=$1 reuse=$2 fingerprint minor
    progress 2/6 "准备项目独立 Python 依赖"
    if [[ $reuse == yes && -x $ROOT/.venv/bin/python ]] && lock_matches "$ROOT/.venv/bin/python" "$source" &&
        logged run_owner "$ROOT/.venv/bin/python" -m pip check; then
        NEXT_VENV=$(readlink -f "$ROOT/.venv")
        ok "现有虚拟环境与锁定依赖一致，直接复用。"
        return
    fi
    select_python
    fingerprint=$(sha256sum "$source/requirements.lock")
    fingerprint=${fingerprint:0:16}
    minor=$("$PYTHON_BIN" -c 'import sys; print("%s.%s" % sys.version_info[:2])')
    NEXT_VENV="$STATE/envs/py${minor}-${fingerprint}"
    if [[ -f $NEXT_VENV/.complete ]] && lock_matches "$NEXT_VENV/bin/python" "$source"; then
        return
    fi
    # Environments are created at their final absolute path; moving a venv breaks entry-point shebangs.
    if [[ -e $NEXT_VENV ]]; then
        NEXT_VENV="$NEXT_VENV-$(date +%s)-$$"
    fi
    logged run_owner "$PYTHON_BIN" -m venv "$NEXT_VENV"
    logged run_owner "$NEXT_VENV/bin/python" -m pip install --disable-pip-version-check 'pip==26.2.1'
    logged run_owner "$NEXT_VENV/bin/python" -m pip install --disable-pip-version-check --prefer-binary -r "$source/requirements.lock"
    logged run_owner "$NEXT_VENV/bin/python" -m pip check
    lock_matches "$NEXT_VENV/bin/python" "$source"
    run_owner touch "$NEXT_VENV/.complete"
}
prepare_node() {
    local version archive distribution expected actual node_arch
    progress 3/6 "检查 Node.js 22 和前端构建工具"
    if command -v node >/dev/null && [[ $(node --version) == v22.* ]] && command -v npm >/dev/null; then
        NODE_BIN=$(dirname "$(command -v node)")
        return
    fi
    NODE_BIN="$STATE/tools/node22/bin"
    if [[ -x $NODE_BIN/node && $("$NODE_BIN/node" --version) == v22.* ]]; then
        return
    fi
    download https://nodejs.org/dist/index.json "$WORK/node-index.json"
    version=$(python3 "$HELPER" node-release "$WORK/node-index.json")
    [[ $version =~ ^v22\.[0-9]+\.[0-9]+$ ]] || die "Node.js 版本信息无效。"
    node_arch=x64
    [[ $ARCH != aarch64 ]] || node_arch=arm64
    distribution="node-$version-linux-$node_arch"
    archive="$distribution.tar.xz"
    download "https://nodejs.org/dist/$version/SHASUMS256.txt" "$WORK/SHASUMS256.txt"
    download "https://nodejs.org/dist/$version/$archive" "$WORK/$archive"
    expected=$(awk -v name="$archive" '$2 == name {print $1}' "$WORK/SHASUMS256.txt")
    actual=$(sha256sum "$WORK/$archive")
    [[ $expected =~ ^[0-9a-f]{64}$ && $expected == "${actual%% *}" ]] || die "Node.js 下载校验失败。"
    tar -xJf "$WORK/$archive" -C "$WORK" --no-same-owner
    mkdir -p "$STATE/tools"
    mv -- "$WORK/$distribution" "$STATE/tools/$distribution"
    ln -s "$STATE/tools/$distribution" "$STATE/tools/node22.new"
    mv -Tf -- "$STATE/tools/node22.new" "$STATE/tools/node22"
    chown -R "$RUN_USER:" "$STATE/tools"
}
build_frontend() {
    local source=$1
    progress 4/6 "在独立目录构建前端"
    mkdir -p "$WORK/frontend"
    rsync -a --exclude=node_modules --exclude=dist --exclude=.vite "$source/frontend/" "$WORK/frontend/"
    chown -R "$RUN_USER:" "$WORK"
    (
        cd "$WORK/frontend"
        logged run_owner env PATH="$NODE_BIN:$PATH" nice -n 10 npm ci --no-audit --no-fund
        logged run_owner env PATH="$NODE_BIN:$PATH" nice -n 10 npm run typecheck
        logged run_owner env PATH="$NODE_BIN:$PATH" NODE_OPTIONS="--max-old-space-size=$BUILD_MEMORY" \
            VITE_OUT_DIR="$WORK/frontend-stage" nice -n 10 npm run build
    )
}
prepare_browser() {
    local source=$1 platform
    local -a compatibility=()
    progress 5/6 "检查项目浏览器和本地渲染"
    if logged run_owner "$NEXT_VENV/bin/python" "$source/scripts/playwright_doctor.py"; then
        logged run_owner xvfb-run -a "$NEXT_VENV/bin/python" "$HELPER" headed-check "$source"
        return
    fi
    platform=x64
    [[ $ARCH != aarch64 ]] || platform=arm64
    if { [[ $DISTRO == ubuntu ]] && dpkg --compare-versions "$VERSION" gt 24.04; } ||
       { [[ $DISTRO == debian ]] && dpkg --compare-versions "$VERSION" gt 13; }; then
        compatibility=(--platform "ubuntu24.04-$platform")
        warn "当前系统使用 Ubuntu 24.04 兼容浏览器构建，必须通过实际启动检查。"
    fi
    # Install system libraries as root, but browser files as the service user.
    logged "$NEXT_VENV/bin/python" -m playwright install-deps chromium
    logged run_owner "$NEXT_VENV/bin/python" "$source/scripts/playwright_doctor.py" --install "${compatibility[@]}"
    logged run_owner xvfb-run -a "$NEXT_VENV/bin/python" "$HELPER" headed-check "$source"
}
preflight() {
    local source=$1
    progress 6/6 "使用隔离数据库检查 HTTP、前端入口和数据库升级"
    logged run_owner python3 "$HELPER" preflight "$source" "$NEXT_VENV/bin/python" "$WORK/frontend-stage" "$DB" "$LOG"
    ok "离线预检通过，未启动真实账号任务。"
}
check_port() {
    if ! python3 "$HELPER" port "$HOST" "$PORT" >> "$LOG" 2>&1; then
        die "端口 $PORT 被其他程序占用或监听地址不可用，请更换端口。"
    fi
}
configure_access() {
    local entered
    if [[ $EXISTS == no && ! -f $STATE/settings.json ]]; then
        read -r -p "访问端口 [8080]：" entered || die "已取消部署。"
        PORT=${entered:-8080}
        if [[ ! $PORT =~ ^[0-9]{1,5}$ ]]; then die "端口必须在1–65535之间。"; fi
        if (( 10#$PORT < 1 || 10#$PORT > 65535 )); then die "端口必须在1–65535之间。"; fi
        PORT=$((10#$PORT))
        HOST=0.0.0.0
    fi
    if [[ $EXISTS == no ]] || ! service_active; then
        check_port
    fi
}
check_database_access() {
    local directory
    directory=$(dirname "$DB")
    if [[ ! -d $directory ]]; then install -d -m 700 -o "$RUN_USER" "$directory"; fi
    run_owner test -w "$directory" || die "服务用户无法写入数据库目录，请修正目录权限后重试。"
    if [[ -e $DB ]]; then
        if ! run_owner test -r "$DB" || ! run_owner test -w "$DB"; then
            die "服务用户无法读写现有数据库。"
        fi
    fi
}
save_backup() {
    BACKUP="$(dirname "$ROOT")/ByGoofish-backups/update-$(date +%Y%m%d-%H%M%S)-$$"
    info "保存仓库外代码、配置和一致性数据库备份……"
    logged python3 "$HELPER" backup "$ROOT" "$BACKUP" "$DB"
    chmod 700 "$(dirname "$BACKUP")"
    ok "备份完成：$BACKUP"
}
switch_venv() {
    OLD_VENV=''
    if [[ -e $ROOT/.venv || -L $ROOT/.venv ]]; then
        OLD_VENV=$(readlink -f "$ROOT/.venv" 2>/dev/null || true)
    fi
    if [[ $NEXT_VENV == "$OLD_VENV" ]]; then return; fi
    if [[ -d $ROOT/.venv && ! -L $ROOT/.venv ]]; then
        LEGACY_VENV="$STATE/envs/legacy-$(date +%s)-$$"
        mkdir -p "$STATE/envs"
        # Preserve existing absolute entry points by using the stable .venv alias again on rollback.
        mv -- "$ROOT/.venv" "$LEGACY_VENV"
        OLD_VENV=$LEGACY_VENV
        VENV_SWITCHED=yes
    fi
    VENV_SWITCHED=yes
    ln -s "$NEXT_VENV" "$STATE/venv-link.$$"
    mv -Tf -- "$STATE/venv-link.$$" "$ROOT/.venv"
    if [[ -n ${BACKUP:-} ]]; then python3 "$HELPER" record-runtime "$BACKUP" "$OLD_VENV"; fi
}
install_service() {
    local base=/etc/systemd/system/bygoofish.service
    SERVICE_WRITTEN=yes
    if [[ $EXISTS == no ]]; then
        BASE_CREATED=yes
        cat > "$base" <<'UNIT'
[Unit]
Description=ByGoofish native service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple

[Install]
WantedBy=multi-user.target
UNIT
    fi
    python3 "$HELPER" service "$ROOT" /etc/systemd/system/bygoofish.service.d \
        "$HOST" "$PORT" "$DB" "$RUN_USER" "$RUN_HOME" "$BROWSERS" "${BROWSER_PATH:-}" "${NODE_BIN:-}" "${SERVICE_PATH:-}"
    chown -R "$RUN_USER:" "$STATE"
    systemctl daemon-reload
    systemctl enable "$SERVICE" >> "$LOG" 2>&1
}
health_url() {
    local host=$HOST
    [[ $host != 0.0.0.0 && $host != localhost ]] || host=127.0.0.1
    [[ $host != :: ]] || host=::1
    [[ $host != *:* ]] || host="[$host]"
    printf 'http://%s:%s/health' "$host" "$PORT"
}
wait_health() {
    local response attempt
    for ((attempt=0; attempt<45; attempt++)); do
        if service_active; then
            response=$(curl --noproxy '*' --silent --fail --max-time 2 "$(health_url)" 2>/dev/null) || response=''
            if printf '%s' "$response" | python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(d.get("status") != "healthy")' 2>/dev/null; then
                return
            fi
        fi
        sleep 1
    done
    return 1
}
show_service_error() {
    systemctl status "$SERVICE" --no-pager -n 12 2>&1 | python3 "$HELPER" redact-stream || true
}
show_access() {
    local discover=${1:-no} address=$HOST candidate family=4
    [[ $HOST != :: ]] || family=6
    if [[ $HOST == 0.0.0.0 || $HOST == :: ]]; then
        if [[ -f $STATE/public-ip ]]; then address=$(cat "$STATE/public-ip"); else address=''; fi
        if [[ -n $address ]] && ! python3 -c 'import ipaddress,sys; a=ipaddress.ip_address(sys.argv[1]); raise SystemExit(not a.is_global or a.version != int(sys.argv[2]))' "$address" "$family" 2>/dev/null; then
            address=''
        fi
        if [[ $discover == yes ]]; then
            candidate=$(python3 "$HELPER" public-address "$family") || candidate=''
            if [[ -z $candidate ]]; then
                candidate=$(curl "-$family" --noproxy '*' --proto '=https' --silent --fail --max-time 5 https://api64.ipify.org 2>/dev/null) || candidate=''
            fi
            if [[ -n $candidate ]] && python3 -c 'import ipaddress,sys; raise SystemExit(not ipaddress.ip_address(sys.argv[1]).is_global)' "$candidate" 2>/dev/null; then
                address=$candidate
                printf '%s\n' "$address" > "$STATE/public-ip"
                chown "$RUN_USER:" "$STATE/public-ip"
            fi
        fi
        if [[ -z $address ]]; then
            warn "公网 IP 未能自动确认；访问地址：http://<服务器公网IP>:$PORT"
        else
            [[ $address != *:* ]] || address="[$address]"
            ok "访问地址：http://$address:$PORT"
        fi
        info "若外网无法访问，请在云安全组和防火墙放行 TCP $PORT。"
    else
        [[ $address != *:* ]] || address="[$address]"
        ok "访问地址：http://$address:$PORT"
        [[ $HOST != 127.0.0.1 && $HOST != ::1 && $HOST != localhost ]] || info "沿用当前仅本机访问配置。"
    fi
}
deployment_cleanup() {
    local code=$? current_head='' restart_safe=yes
    trap - EXIT INT TERM ERR
    set +e
    if (( code != 0 )); then
        warn "本次部署未完成；详细日志：${LOG:-未创建}"
        if [[ ${STOPPED:-no} == yes && ${STARTED:-no} == no ]]; then
            if [[ -n ${OLD_HEAD:-} && -n ${NEW_HEAD:-} ]]; then
                current_head=$(project_git "$ROOT" rev-parse HEAD 2>/dev/null)
            fi
            if [[ -n ${NEW_HEAD:-} && $current_head == "$NEW_HEAD" ]]; then
                # Both trees were checked clean before the fast-forward. Never overwrite business data.
                if project_git "$ROOT" read-tree -u -m "$NEW_HEAD" "$OLD_HEAD"; then
                    project_git "$ROOT" update-ref HEAD "$OLD_HEAD" "$NEW_HEAD" || warn "版本引用回退未完成，请使用备份处理。"
                else
                    warn "代码回退未完成，请使用备份处理。"
                fi
            fi
            if [[ ${VENV_SWITCHED:-no} == yes && -n ${OLD_VENV:-} ]]; then
                ln -s "$OLD_VENV" "$ROOT/.venv.rollback"
                mv -Tf -- "$ROOT/.venv.rollback" "$ROOT/.venv"
                [[ $(readlink -f "$ROOT/.venv") == "$OLD_VENV" ]] || restart_safe=no
            fi
            if [[ ${SERVICE_WRITTEN:-no} == yes ]]; then
                python3 "$HELPER" restore-service "$ROOT" "$BACKUP" "${BASE_CREATED:-no}" || restart_safe=no
                chown "$RUN_USER:" "$STATE/settings.json" "$STATE/service.env" 2>/dev/null || true
                systemctl daemon-reload
            fi
            if [[ -f ${BACKUP:-}/static/index.html ]]; then
                cp -- "$BACKUP/static/index.html" "$ROOT/static/index.html" || restart_safe=no
            fi
            if [[ -n ${OLD_HEAD:-} ]]; then
                if [[ $(project_git "$ROOT" rev-parse HEAD 2>/dev/null) != "$OLD_HEAD" ]] ||
                    [[ -n $(project_git "$ROOT" status --porcelain --untracked-files=normal) ]]; then
                    restart_safe=no
                    warn "检测到部署期间代码变动，已保留改动；请核对代码与备份后重启服务。"
                fi
            fi
            if [[ ${WAS_ACTIVE:-no} == yes && $restart_safe == yes ]]; then
                systemctl start "$SERVICE" || warn "旧服务启动失败，请检查日志。"
            elif [[ $restart_safe == no ]]; then
                warn "回退检查未通过，服务保持停止以便核查。"
            fi
            warn "业务数据库未回写旧快照。"
        elif [[ ${STARTED:-no} == yes ]]; then
            warn "新服务已尝试运行；为保护可能产生的业务数据，不自动覆盖数据库或强制回退。"
        fi
    fi
    if [[ -n ${WORK:-} && -d $WORK && $WORK == "$STATE"/staging-* ]]; then
        rm -rf -- "$WORK"
    fi
    exit "$code"
}
begin_deployment() {
    check_system
    command -v python3 >/dev/null || {
        env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 update
        env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y python3
    }
    load_settings
    prepare_state
    WORK=$(mktemp -d "$STATE/staging-XXXXXX")
    chown "$RUN_USER:" "$WORK"
    STOPPED=no STARTED=no WAS_ACTIVE=no VENV_SWITCHED=no SERVICE_WRITTEN=no BASE_CREATED=no
    trap deployment_cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'printf "部署步骤失败（第%s行）。\n" "$LINENO" >&2' ERR
}
foreground_command() {
    local commandline invocation caller=${SUDO_USER:-$(id -un)}
    local -a environment=(env "HOME=$RUN_HOME" "PATH=${NODE_BIN:-/usr/bin}:${SERVICE_PATH:-/usr/local/bin:/usr/bin:/bin}"
        "API_HOST=$HOST" "API_PORT=$PORT" "DB_PATH=$DB" WEB_CONCURRENCY=1
        "MAX_CONCURRENT_BROWSERS=$BROWSERS" PYTHONDONTWRITEBYTECODE=1 SQL_LOG_ENABLED=false)
    [[ -z ${BROWSER_PATH:-} ]] || environment+=("PLAYWRIGHT_BROWSERS_PATH=$BROWSER_PATH")
    printf -v invocation '%q ' "${environment[@]}" /usr/bin/xvfb-run -a "$ROOT/.venv/bin/python" -u "$ROOT/Start.py"
    printf -v commandline 'cd %q && %s' "$ROOT" "$invocation"
    if [[ $caller == "$RUN_USER" ]]; then
        printf '%s\n' "$commandline"
    elif [[ $caller == root ]]; then
        printf 'runuser -u %q -- bash -c %q\n' "$RUN_USER" "$commandline"
    else
        printf 'sudo -u %q -- bash -c %q\n' "$RUN_USER" "$commandline"
    fi
}
finish_deployment() {
    local create=$1 fresh=$2
    if [[ $create == yes ]]; then
        STARTED=yes
        systemctl reset-failed "$SERVICE" >> "$LOG" 2>&1 || true
        systemctl start "$SERVICE"
        if ! wait_health; then
            show_service_error
            die "服务健康检查未通过；请使用菜单5查看日志。"
        fi
        ok "服务已运行，并启用开机自启和失败自动重启。"
    else
        info "安装完成，尚未启动后台服务。前台运行命令："
        foreground_command
    fi
    if [[ -n ${BACKUP:-} ]]; then
        python3 "$HELPER" prune "$ROOT" "$BACKUP"
        python3 "$HELPER" prune-envs "$ROOT" "$BACKUP"
    fi
    show_access yes
    if [[ $fresh == yes ]]; then
        if [[ $create == no ]] || python3 "$HELPER" default-login "$(health_url)"; then
            # Deliberately terminal-only, never passed through logged()/tee or written to deployment records.
            printf '\n首次部署默认账号：admin\n首次部署默认密码：admin123\n'
        else
            info "初始密码由已有服务配置指定，请使用你设置的密码登录。"
        fi
    elif [[ $fresh == no ]]; then
        info "已有数据库：继续使用原来的登录账号和密码。"
    fi
    ok "部署完成。日志：$LOG"
}
first_install() {
    local create=yes fresh
    begin_deployment
    check_capacity
    configure_access
    check_database_access
    if [[ $EXISTS == no ]] && ! confirm "是否创建后台服务"; then create=no; fi
    fresh=$(python3 "$HELPER" fresh "$DB")
    install_system_packages
    prepare_python "$ROOT" yes
    prepare_node
    build_frontend "$ROOT"
    prepare_browser "$ROOT"
    preflight "$ROOT"
    STOPPED=yes
    if [[ $EXISTS == yes ]] && service_active; then
        WAS_ACTIVE=yes
        systemctl stop "$SERVICE"
    fi
    python3 "$HELPER" assert-stopped "$DB"
    check_port
    save_backup
    switch_venv
    logged "$NEXT_VENV/bin/python" "$ROOT/scripts/publish_frontend.py" "$WORK/frontend-stage"
    if [[ $create == yes ]]; then
        install_service
    else
        python3 "$HELPER" service "$ROOT" "$WORK/no-service" "$HOST" "$PORT" "$DB" "$RUN_USER" "$RUN_HOME" "$BROWSERS" "${BROWSER_PATH:-}" "${NODE_BIN:-}" "${SERVICE_PATH:-}"
        chown -R "$RUN_USER:" "$STATE"
    fi
    finish_deployment "$create" "$fresh"
}
check_clean() {
    [[ -d $ROOT/.git ]] || die "更新要求完整 Git 克隆；当前文件未改动，请先使用 Git 版本安装。"
    [[ $(project_git "$ROOT" symbolic-ref --short HEAD 2>/dev/null) == main ]] || die "当前不是 main 分支，已停止自动更新。"
    [[ -z $(project_git "$ROOT" status --porcelain --untracked-files=normal) ]] || die "存在未提交的修改或文件；请先处理，本次不会覆盖。"
}
update_latest() {
    local candidate advertised _ query_status
    # Safety gates precede package installation, build, and service changes.
    require_root "更新需要 root 权限。"
    check_clean
    begin_deployment
    info "检查 main 最新版本……"
    OLD_HEAD=$(project_git "$ROOT" rev-parse HEAD)
    if timeout 60 env GIT_TERMINAL_PROMPT=0 git -c http.lowSpeedLimit=1024 -c http.lowSpeedTime=30 \
        ls-remote --exit-code --heads "$REMOTE" refs/heads/main > "$WORK/latest-ref" 2>> "$LOG"; then
        :
    else
        query_status=$?
        if [[ $query_status == 2 ]]; then
            die "远程 main 分支尚未发布；请先发布确认后的本地版本，再使用更新。当前服务保持运行。"
        fi
        die "无法查询最新版，请检查网络和仓库访问权限；当前服务未改变。"
    fi
    read -r advertised _ < "$WORK/latest-ref"
    [[ $advertised =~ ^[0-9a-f]{40,64}$ ]] || die "远程版本信息无效，已停止更新。"
    if [[ $advertised == "$OLD_HEAD" ]]; then ok "当前已是最新版，无需更新。"; return; fi
    load_resources
    (( DISK >= 6144 )) || die "至少需要6GB可用磁盘准备新版代码、依赖和回退资料。"
    candidate="$WORK/candidate"
    if ! logged timeout 300 env GIT_TERMINAL_PROMPT=0 git -c http.lowSpeedLimit=1024 -c http.lowSpeedTime=30 \
        clone --quiet --single-branch --branch main -- "$REMOTE" "$candidate"; then
        die "无法下载最新版，请检查网络和仓库访问权限；当前服务未改变。"
    fi
    NEW_HEAD=$(project_git "$candidate" rev-parse HEAD)
    if [[ $NEW_HEAD == "$OLD_HEAD" ]]; then ok "当前已是最新版，无需更新。"; return; fi
    if ! project_git "$candidate" merge-base --is-ancestor "$OLD_HEAD" "$NEW_HEAD"; then
        die "本机存在尚未发布的提交，或与远程版本分叉；已保留当前版本，不会强制覆盖。"
    fi
    check_capacity
    chown -R "$RUN_USER:" "$candidate"
    configure_access
    check_database_access
    install_system_packages
    prepare_python "$candidate" no
    prepare_node
    build_frontend "$candidate"
    prepare_browser "$candidate"
    preflight "$candidate"
    logged project_git "$candidate" bundle create "$WORK/candidate.bundle" main
    # Abort if another operator edited the repository during preparation.
    check_clean
    [[ $(project_git "$ROOT" rev-parse HEAD) == "$OLD_HEAD" ]] || die "本机版本在准备期间发生变化，请重试。"
    STOPPED=yes
    if [[ $EXISTS == yes ]] && service_active; then
        WAS_ACTIVE=yes
        systemctl stop "$SERVICE"
    fi
    python3 "$HELPER" assert-stopped "$DB"
    check_port
    save_backup
    # Validate a final stopped snapshot too, before any live schema is touched.
    preflight "$candidate"
    logged project_git "$ROOT" fetch --no-tags "$WORK/candidate.bundle" main
    logged project_git "$ROOT" merge --ff-only "$NEW_HEAD"
    switch_venv
    logged "$NEXT_VENV/bin/python" "$ROOT/scripts/publish_frontend.py" "$WORK/frontend-stage"
    install_service
    finish_deployment yes no
}
restart_service() {
    require_root "重启服务需要 root 权限。"
    load_settings
    [[ $EXISTS == yes ]] || die "尚未创建服务，请先选择首次部署。"
    mkdir -p "$STATE"
    exec {DEPLOY_LOCK}>"$STATE/deploy.lock"
    flock -n "$DEPLOY_LOCK" || die "部署任务正在运行，暂不能重启。"
    info "正在重启服务并等待健康检查……"
    systemctl reset-failed "$SERVICE" 2>/dev/null || true
    systemctl restart "$SERVICE"
    if wait_health; then
        ok "服务重启成功；闲鱼账号状态请在后台账号页查看。"
        show_access
    else
        show_service_error
        die "服务未通过健康检查，请选择查看日志。"
    fi
}
system_status() {
    command -v python3 >/dev/null || { warn "尚未安装 Python，请先选择首次部署。"; return; }
    load_settings
    load_resources
    info "系统：$(. /etc/os-release; printf '%s' "$PRETTY_NAME") / $(uname -m)"
    printf '资源：%s核 / 内存 %sMB（可用%sMB）/ Swap %sMB / 可用磁盘%sMB\n' "$CPUS" "$MEMORY" "$AVAILABLE" "$SWAP" "$DISK"
    printf '项目：%s\n' "$ROOT"
    if project_git "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        printf '版本：%s\n' "$(project_git "$ROOT" log -1 --format='%h %cs')"
    fi
    if [[ $EXISTS == yes ]]; then
        systemctl show "$SERVICE" --no-pager -p ActiveState -p SubState -p UnitFileState -p MainPID -p ActiveEnterTimestamp
        local since elapsed
        since=$(systemctl show "$SERVICE" -p ActiveEnterTimestampMonotonic --value)
        if [[ $since =~ ^[0-9]+$ ]] && (( since > 0 )); then
            elapsed=$(awk -v since="$since" '{printf "%.0f", $1-since/1000000}' /proc/uptime)
            printf '本次运行时长：%s天 %s小时 %s分钟\n' "$((elapsed/86400))" "$((elapsed%86400/3600))" "$((elapsed%3600/60))"
        fi
        if curl --noproxy '*' --silent --fail --max-time 3 "$(health_url)" 2>/dev/null |
            python3 -c 'import json,sys; raise SystemExit(json.load(sys.stdin).get("status") != "healthy")' 2>/dev/null; then
            ok "应用健康检查通过；不代表闲鱼账号已认证。"
        else warn "应用健康检查未通过。"; fi
        show_access
    else
        warn "尚未创建后台服务，请选择首次部署。"
    fi
}
view_logs() {
    local result=0 directory
    command -v systemctl >/dev/null || { warn "未找到 systemd。"; return; }
    command -v python3 >/dev/null || { warn "查看脱敏日志需要 Python3，请先选择首次部署。"; return; }
    directory=$(systemctl show "$SERVICE" -p WorkingDirectory --value 2>/dev/null) || directory=''
    [[ -n $directory ]] || { warn "尚未创建服务，请先选择首次部署。"; return; }
    [[ $(realpath -m -- "$directory") == "$ROOT" ]] || { warn "同名服务属于其他项目目录，不读取其日志。"; return; }
    info "显示最近200条服务日志；按 Ctrl+C 返回菜单。"
    trap ':' INT
    journalctl -u "$SERVICE" -n 200 -f --no-pager -o short-iso | python3 "$HELPER" redact-stream || result=$?
    trap - INT
    [[ $result == 0 || $result == 130 ]] || warn "日志读取结束；如遇权限不足，请用 sudo 运行脚本。"
}
dispatch() {
    local action=$1 runner
    if [[ $action != status && $action != logs && $EUID != 0 ]]; then
        command -v sudo >/dev/null || { warn "此操作需要 root，请用 root 运行脚本。"; return 1; }
        sudo bash "$ROOT/ByGoofish.sh" --action "$action"
        return
    fi
    if [[ $action == update ]]; then
        # Immutable runner: updating ByGoofish.sh/helper must not replace the executing program.
        runner=$(mktemp -d /tmp/bygoofish-runner-XXXXXX)
        cp -- "$ROOT/ByGoofish.sh" "$runner/runner.sh"
        cp -- "$HELPER" "$runner/native_deploy.py"
        local result=0
        if bash "$runner/runner.sh" --internal "$ROOT" "$runner/native_deploy.py" --action update; then
            :
        else
            result=$?
        fi
        rm -rf -- "$runner"
        return "$result"
    fi
    bash "$ROOT/ByGoofish.sh" --action "$action"
}
main() {
    local choice action
    if [[ ${1:-} == --action ]]; then
        case ${2:-} in
            install) first_install ;;
            update) if [[ $INTERNAL == yes ]]; then update_latest; else dispatch update; fi ;;
            restart) restart_service ;;
            status) system_status ;;
            logs) view_logs ;;
            *) die "未知操作。" ;;
        esac
        return
    fi
    trap ':' INT
    while true; do
        printf '\n%s========== ByGoofish 管理 ==========%s\n' "$CYAN" "$RESET"
        printf '1、首次部署\n2、更新到最新版\n3、重启服务\n4、系统状态\n5、查看日志\n0、退出菜单\n===================================\n'
        if ! read -r -p '请选择：' choice; then printf '\n'; return; fi
        case "$choice" in
            1) action=install ;;
            2) action=update ;;
            3) action=restart ;;
            4) action=status ;;
            5) action=logs ;;
            0) ok "已退出，后台服务保持运行。"; return ;;
            *) warn "请输入0–5。"; continue ;;
        esac
        if ! dispatch "$action"; then warn "操作未完成；可以处理提示后重新选择。"; fi
    done
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
