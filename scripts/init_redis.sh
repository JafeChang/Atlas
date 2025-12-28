#!/bin/bash
###############################################################################
# Redis初始化脚本
#
# 功能:
# 1. 检查Redis安装状态
# 2. 启动Redis服务器
# 3. 配置Redis (可选)
# 4. 测试Redis连接
# 5. 显示连接信息
#
# TASK-003-1: Redis配置和部署
###############################################################################

set -e  # 遇到错误立即退出

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 配置 (从环境变量读取，使用默认值作为fallback)
REDIS_HOST="${ATLAS_REDIS_HOST:-localhost}"
REDIS_PORT="${ATLAS_REDIS_PORT:-6379}"
REDIS_PASSWORD="${ATLAS_REDIS_PASSWORD:-}"

# 打印带颜色的消息
print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ $1${NC}"
}

print_header() {
    echo -e "\n${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}\n"
}

# 检查命令是否存在
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# 检查Redis是否安装
check_redis_installation() {
    print_header "检查Redis安装"

    if command_exists redis-server; then
        REDIS_VERSION=$(redis-server --version | grep -oP 'v=\K[0-9.]+' || echo "unknown")
        print_success "Redis已安装: $REDIS_VERSION"
        return 0
    else
        print_error "Redis未安装"
        print_info "请安装Redis:"
        echo "  sudo apt-get update"
        echo "  sudo apt-get install -y redis-server"
        return 1
    fi
}

# 检查Redis服务状态
check_redis_status() {
    print_header "检查Redis服务状态"

    if command_exists redis-cli; then
        if redis-cli ping >/dev/null 2>&1; then
            print_success "Redis服务器正在运行"
            return 0
        else
            print_warning "Redis服务器未运行"
            return 1
        fi
    else
        print_error "redis-cli未找到"
        return 1
    fi
}

# 启动Redis服务
start_redis() {
    print_header "启动Redis服务"

    # 方式1: 使用service命令
    if command_exists service; then
        print_info "使用service命令启动Redis..."
        sudo service redis-server start
        sleep 2

        if redis-cli ping >/dev/null 2>&1; then
            print_success "Redis启动成功"
            return 0
        fi
    fi

    # 方式2: 使用systemctl
    if command_exists systemctl; then
        print_info "使用systemctl启动Redis..."
        sudo systemctl start redis-server
        sleep 2

        if redis-cli ping >/dev/null 2>&1; then
            print_success "Redis启动成功"
            return 0
        fi
    fi

    # 方式3: 直接启动redis-server
    if command_exists redis-server; then
        print_info "直接启动redis-server..."
        redis-server --daemonize yes
        sleep 2

        if redis-cli ping >/dev/null 2>&1; then
            print_success "Redis启动成功"
            return 0
        fi
    fi

    print_error "Redis启动失败"
    return 1
}

# 配置Redis (可选)
configure_redis() {
    print_header "配置Redis"

    print_warning "此步骤将修改Redis配置文件"
    read -p "是否继续? (y/N): " -n 1 -r
    echo

    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_info "跳过配置步骤"
        return 0
    fi

    local REDIS_CONF=""
    if [ -f "/etc/redis/redis.conf" ]; then
        REDIS_CONF="/etc/redis/redis.conf"
    elif [ -f "/etc/redis.conf" ]; then
        REDIS_CONF="/etc/redis.conf"
    else
        print_error "找不到Redis配置文件"
        return 1
    fi

    print_info "使用配置文件: $REDIS_CONF"

    # 备份配置文件
    sudo cp "$REDIS_CONF" "${REDIS_CONF}.backup.$(date +%Y%m%d_%H%M%S)"
    print_success "配置文件已备份"

    # 设置最大内存
    print_info "配置Redis内存限制..."
    read -p "最大内存 (例如: 256mb, 留空跳过): " MAX_MEMORY

    if [ -n "$MAX_MEMORY" ]; then
        sudo sed -i "s/^# maxmemory .*/maxmemory $MAX_MEMORY/" "$REDIS_CONF"
        sudo sed -i "/^maxmemory /! s/^.*maxmemory.*$/maxmemory $MAX_MEMORY/" "$REDIS_CONF"
        print_success "最大内存设置为: $MAX_MEMORY"

        # 设置内存淘汰策略
        sudo sed -i "s/^# maxmemory-policy .*/maxmemory-policy allkeys-lru/" "$REDIS_CONF"
        sudo sed -i "/^maxmemory-policy /! s/^.*maxmemory-policy.*$/maxmemory-policy allkeys-lru/" "$REDIS_CONF"
        print_success "内存淘汰策略: allkeys-lru"
    fi

    # 持久化配置
    print_info "配置持久化..."
    read -p "启用AOF持久化? (y/N): " -n 1 -r
    echo

    if [[ $REPLY =~ ^[Yy]$ ]]; then
        sudo sed -i "s/^appendonly no/appendonly yes/" "$REDIS_CONF"
        sudo sed -i "/^appendonly /! s/^.*appendonly.*$/appendonly yes/" "$REDIS_CONF"
        print_success "AOF持久化已启用"
    fi

    print_info "重启Redis以应用配置..."
    sudo service redis-server restart
    sleep 2

    if redis-cli ping >/dev/null 2>&1; then
        print_success "Redis配置完成并重启成功"
    else
        print_error "Redis重启失败"
        return 1
    fi
}

# 测试Redis连接
test_redis() {
    print_header "测试Redis连接"

    if [ -n "$REDIS_PASSWORD" ]; then
        print_info "使用密码连接Redis..."
        if redis-cli -a "$REDIS_PASSWORD" ping >/dev/null 2>&1; then
            print_success "Redis连接测试成功 (使用密码)"
        else
            print_error "Redis连接测试失败 (密码错误或服务未启动)"
            return 1
        fi
    else
        print_info "无密码连接Redis..."
        if redis-cli ping >/dev/null 2>&1; then
            print_success "Redis连接测试成功 (无密码)"
        else
            print_error "Redis连接测试失败"
            return 1
        fi
    fi

    # 获取Redis信息
    print_info "Redis服务器信息:"
    redis-cli info server | grep -E "redis_version|os|tcp_port|uptime_in_days" || true

    # 测试基本操作
    print_info "测试基本操作..."
    redis-cli SET test_key "test_value" >/dev/null 2>&1
    VALUE=$(redis-cli GET test_key 2>/dev/null)

    if [ "$VALUE" = "test_value" ]; then
        print_success "SET/GET操作测试成功"
        redis-cli DEL test_key >/dev/null 2>&1
    else
        print_warning "SET/GET操作测试失败"
    fi

    return 0
}

# 显示连接信息
show_connection_info() {
    print_header "Redis连接信息"

    echo "Redis配置:"
    echo "  主机: ${REDIS_HOST}"
    echo "  端口: ${REDIS_PORT}"
    echo "  密码: ${REDIS_PASSWORD:-<未设置>}"
    echo ""

    # Celery连接URL
    if [ -n "$REDIS_PASSWORD" ]; then
        CELERY_BROKER="redis://:${REDIS_PASSWORD}@${REDIS_HOST}:${REDIS_PORT}/0"
        CELERY_BACKEND="redis://:${REDIS_PASSWORD}@${REDIS_HOST}:${REDIS_PORT}/1"
    else
        CELERY_BROKER="redis://${REDIS_HOST}:${REDIS_PORT}/0"
        CELERY_BACKEND="redis://${REDIS_HOST}:${REDIS_PORT}/1"
    fi

    echo "Celery配置 (添加到.env.local):"
    echo "  ATLAS_REDIS_HOST=${REDIS_HOST}"
    echo "  ATLAS_REDIS_PORT=${REDIS_PORT}"
    if [ -n "$REDIS_PASSWORD" ]; then
        echo "  ATLAS_REDIS_PASSWORD=${REDIS_PASSWORD}"
    fi
    echo ""
    echo "  ATLAS_CELERY_BROKER_URL=${CELERY_BROKER}"
    echo "  ATLAS_CELERY_RESULT_BACKEND=${CELERY_BACKEND}"
    echo ""

    # Python连接示例
    print_info "Python连接示例:"
    echo "  from redis import Redis"
    echo "  r = Redis(host='${REDIS_HOST}', port=${REDIS_PORT}"
    if [ -n "$REDIS_PASSWORD" ]; then
        echo "            , password='${REDIS_PASSWORD}'"
    fi
    echo "            , decode_responses=True)"
    echo "  print(r.ping())"
}

# 主函数
main() {
    print_header "Atlas Redis初始化"

    # 检查安装
    if ! check_redis_installation; then
        print_error "请先安装Redis"
        exit 1
    fi

    # 检查状态
    if ! check_redis_status; then
        # 启动Redis
        if ! start_redis; then
            print_error "无法启动Redis"
            exit 1
        fi
    fi

    # 可选配置
    read -p "是否配置Redis? (y/N): " -n 1 -r
    echo

    if [[ $REPLY =~ ^[Yy]$ ]]; then
        configure_redis
    fi

    # 测试连接
    test_redis

    # 显示连接信息
    show_connection_info

    print_success "Redis初始化完成!"
    echo ""
    print_info "下一步:"
    echo "  1. 更新.env.local文件"
    echo "  2. 运行环境检查: python3 scripts/check_redis_env.py"
    echo "  3. 开始Celery集成"
}

# 运行主函数
main "$@"
