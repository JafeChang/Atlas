#!/bin/bash
# Atlas 项目环境设置脚本

set -e  # 遇到错误立即退出

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 日志函数
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查命令是否存在
check_command() {
    if command -v "$1" >/dev/null 2>&1; then
        return 0
    else
        return 1
    fi
}

# 主函数
main() {
    log_info "开始设置 Atlas 开发环境..."

    # 检查 Python 版本
    log_info "检查 Python 版本..."
    if check_command python3; then
        PYTHON_CMD="python3"
    elif check_command python; then
        PYTHON_CMD="python"
    else
        log_error "未找到 Python，请先安装 Python 3.13+"
        exit 1
    fi

    PYTHON_VERSION=$($PYTHON_CMD --version | cut -d' ' -f2)
    log_info "发现 Python 版本: $PYTHON_VERSION"

    # 检查 uv 是否已安装
    if check_command uv; then
        log_success "uv 已安装"
        UV_CMD="uv"
    else
        log_warning "uv 未安装，尝试安装..."
        if check_command curl; then
            log_info "使用官方安装脚本安装 uv..."
            curl -LsSf https://astral.sh/uv/install.sh | sh
            export PATH="$HOME/.cargo/bin:$PATH"
            UV_CMD="$HOME/.cargo/bin/uv"

            if ! check_command "$UV_CMD"; then
                log_error "uv 安装失败"
                exit 1
            fi
            log_success "uv 安装成功"
        else
            log_error "未找到 curl，无法安装 uv"
            exit 1
        fi
    fi

    # 创建虚拟环境
    log_info "创建 Python 虚拟环境..."
    if [ ! -d ".venv" ]; then
        $UV_CMD venv
        log_success "虚拟环境创建成功"
    else
        log_info "虚拟环境已存在"
    fi

    # 激活虚拟环境
    log_info "激活虚拟环境..."
    source .venv/bin/activate

    # 安装依赖
    log_info "安装项目依赖..."
    $UV_CMD pip install -e .
    $UV_CMD pip install -e ".[dev]"

    log_success "依赖安装完成"

    # 创建必要目录
    log_info "创建项目目录结构..."
    mkdir -p data/{raw,processed,indexes}
    mkdir -p logs
    mkdir -p config
    log_success "目录结构创建完成"

    # 检查配置文件
    log_info "检查配置文件..."
    if [ ! -f "config/config.yaml" ]; then
        log_warning "主配置文件不存在，请参考文档创建"
    fi

    if [ ! -f "config/sources.yaml" ]; then
        log_warning "数据源配置文件不存在，请参考文档创建"
    fi

    # 创建环境变量文件
    if [ ! -f "config/.env.local" ]; then
        log_info "创建本地环境变量文件..."
        cat > config/.env.local << EOF
# Atlas 本地环境配置
ATLAS_ENV=development
ATLAS_DEBUG=true
ATLAS_LOG_LEVEL=INFO
ATLAS_DATA_DIR=./data
ATLAS_LOG_DIR=./logs
EOF
        log_success "本地环境变量文件创建成功"
    fi

    # 运行测试
    log_info "运行测试套件..."
    if $UV_CMD run pytest tests/ -v; then
        log_success "所有测试通过"
    else
        log_warning "部分测试失败，请检查环境配置"
    fi

    # 验证 CLI
    log_info "验证 CLI 工具..."
    if $UV_CMD run atlas --help >/dev/null 2>&1; then
        log_success "CLI 工具正常"
    else
        log_error "CLI 工具异常，请检查安装"
        exit 1
    fi

    # 设置 Git hooks (如果存在 .git 目录)
    if [ -d ".git" ]; then
        log_info "设置 Git hooks..."
        if [ -f "scripts/pre-commit" ]; then
            cp scripts/pre-commit .git/hooks/
            chmod +x .git/hooks/pre-commit
            log_success "Git hooks 设置完成"
        fi
    fi

    log_success "Atlas 开发环境设置完成！"
    echo
    log_info "下一步："
    echo "  1. 激活虚拟环境: source .venv/bin/activate"
    echo "  2. 运行系统检查: atlas check"
    echo "  3. 初始化系统: atlas init"
    echo "  4. 查看系统状态: atlas status"
    echo
}

# 错误处理
trap 'log_error "设置过程中发生错误，请检查上面的错误信息"; exit 1' ERR

# 运行主函数
main "$@"