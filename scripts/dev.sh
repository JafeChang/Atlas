#!/bin/bash
# Atlas 开发辅助脚本

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

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
    command -v "$1" >/dev/null 2>&1
}

# 确保在项目根目录
ensure_project_root() {
    if [ ! -f "pyproject.toml" ] || [ ! -d "src/atlas" ]; then
        log_error "请在 Atlas 项目根目录运行此脚本"
        exit 1
    fi
}

# 激活虚拟环境
activate_venv() {
    if [ -d ".venv" ]; then
        source .venv/bin/activate
        log_info "虚拟环境已激活"
    else
        log_error "虚拟环境不存在，请先运行 ./scripts/setup.sh"
        exit 1
    fi
}

# 显示帮助信息
show_help() {
    cat << EOF
Atlas 开发辅助脚本

用法: $0 [命令] [选项]

命令:
  install         安装/更新依赖
  test            运行测试
  test-watch      监控文件变化并自动运行测试
  lint            代码检查
  format          代码格式化
  clean           清理临时文件
  run             运行 Atlas
  check           系统状态检查
  init            初始化系统
  docs            生成文档
  build           构建项目
  deploy          部署项目
  dev             启动开发模式
  help            显示此帮助信息

选项:
  -v, --verbose   详细输出
  -q, --quiet     静默模式

示例:
  $0 test                 # 运行所有测试
  $0 test --unit          # 只运行单元测试
  $0 lint --fix           # 修复代码格式问题
  $0 dev                  # 启动开发模式

EOF
}

# 安装依赖
cmd_install() {
    log_info "安装/更新项目依赖..."
    activate_venv

    if check_command uv; then
        uv pip install -e .
        uv pip install -e ".[dev]"
    else
        pip install -e .
        pip install -e ".[dev]"
    fi

    log_success "依赖安装完成"
}

# 运行测试
cmd_test() {
    log_info "运行测试套件..."
    activate_venv

    local test_args=""
    if [ "$1" = "--unit" ]; then
        test_args="-m unit"
    elif [ "$1" = "--integration" ]; then
        test_args="-m integration"
    elif [ "$1" = "--coverage" ]; then
        test_args="--cov=src --cov-report=html --cov-report=term"
    fi

    if check_command uv; then
        uv run pytest tests/ $test_args -v
    else
        python -m pytest tests/ $test_args -v
    fi
}

# 监控测试
cmd_test_watch() {
    log_info "启动测试监控..."
    activate_venv

    if ! check_command watchdog; then
        log_info "安装 watchdog..."
        pip install watchdog
    fi

    # 使用 watchdog 监控文件变化
    python -c "
import time
import subprocess
import sys
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class TestHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_run = 0
        self.debounce = 2  # 2秒防抖

    def on_modified(self, event):
        if event.is_directory:
            return

        # 只监控 Python 文件
        if not event.src_path.endswith('.py'):
            return

        current_time = time.time()
        if current_time - self.last_run < self.debounce:
            return

        self.last_run = current_time
        print(f'\n检测到文件变化: {event.src_path}')
        self.run_tests()

    def run_tests(self):
        try:
            result = subprocess.run([
                sys.executable, '-m', 'pytest', 'tests/', '-v', '-x'
            ], capture_output=True, text=True)

            if result.returncode == 0:
                print('✅ 测试通过')
            else:
                print('❌ 测试失败')
                print(result.stdout)
                print(result.stderr)
        except Exception as e:
            print(f'❌ 运行测试时出错: {e}')

if __name__ == '__main__':
    event_handler = TestHandler()
    observer = Observer()
    observer.schedule(event_handler, 'src/', recursive=True)
    observer.schedule(event_handler, 'tests/', recursive=True)

    print('🔍 开始监控文件变化...')
    print('📝 修改 Python 文件将自动运行测试')
    print('按 Ctrl+C 停止监控')

    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print('\n👋 停止监控')
    observer.join()
"
}

# 代码检查
cmd_lint() {
    log_info "运行代码检查..."
    activate_venv

    local fix_fix=""
    if [ "$1" = "--fix" ]; then
        fix_fix="--fix"
    fi

    # Ruff
    if check_command ruff; then
        ruff check src/ tests/ $fix_fix
    else
        log_warning "ruff 未安装，跳过 Ruff 检查"
    fi

    # MyPy
    if check_command mypy; then
        mypy src/
    else
        log_warning "mypy 未安装，跳过类型检查"
    fi

    log_success "代码检查完成"
}

# 代码格式化
cmd_format() {
    log_info "格式化代码..."
    activate_venv

    # Black
    if check_command black; then
        black src/ tests/
    else
        log_warning "black 未安装，跳过 Black 格式化"
    fi

    # isort
    if check_command isort; then
        isort src/ tests/
    else
        log_warning "isort 未安装，跳过 isort 格式化"
    fi

    log_success "代码格式化完成"
}

# 清理临时文件
cmd_clean() {
    log_info "清理临时文件..."

    # Python 缓存
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find . -type f -name "*.pyc" -delete 2>/dev/null || true
    find . -type f -name "*.pyo" -delete 2>/dev/null || true

    # 测试覆盖率
    rm -rf htmlcov/ 2>/dev/null || true
    rm -f .coverage 2>/dev/null || true

    # 构建文件
    rm -rf build/ dist/ *.egg-info/ 2>/dev/null || true

    # IDE 文件
    find . -name ".DS_Store" -delete 2>/dev/null || true
    find . -name "*.swp" -delete 2>/dev/null || true
    find . -name "*.swo" -delete 2>/dev/null || true

    log_success "清理完成"
}

# 运行 Atlas
cmd_run() {
    log_info "启动 Atlas..."
    activate_venv

    if check_command uv; then
        uv run atlas "$@"
    else
        python -m atlas "$@"
    fi
}

# 系统检查
cmd_check() {
    log_info "运行系统检查..."
    activate_venv
    cmd_run check
}

# 系统初始化
cmd_init() {
    log_info "初始化系统..."
    activate_venv
    cmd_run init
}

# 生成文档
cmd_docs() {
    log_info "生成文档..."
    activate_venv

    if [ -d "docs" ]; then
        # 如果存在 Sphinx 配置
        if [ -f "docs/conf.py" ]; then
            cd docs
            make html
            log_success "文档生成完成，查看 docs/_build/html/index.html"
        else
            log_warning "未找到 Sphinx 配置，跳过文档生成"
        fi
    else
        log_warning "docs 目录不存在"
    fi
}

# 构建项目
cmd_build() {
    log_info "构建项目..."
    activate_venv

    if check_command uv; then
        uv build
    else
        python -m build
    fi

    log_success "项目构建完成"
}

# 开发模式
cmd_dev() {
    log_info "启动开发模式..."
    activate_venv

    # 运行系统检查
    cmd_check

    # 启动测试监控（在后台）
    if [ "$1" = "--watch" ]; then
        cmd_test_watch &
        TEST_PID=$!
        log_info "测试监控已启动 (PID: $TEST_PID)"
    fi

    log_info "开发环境已就绪"
    log_info "可用的开发命令:"
    echo "  atlas check        - 检查系统状态"
    echo "  atlas status       - 查看系统状态"
    echo "  atlas init         - 初始化系统"
    echo "  atlas collect      - 执行数据采集"
    echo "  atlas logs         - 查看日志"

    if [ -n "$TEST_PID" ]; then
        # 等待用户中断
        trap "kill $TEST_PID 2>/dev/null || true" EXIT
        while true; do
            sleep 1
        done
    fi
}

# 主函数
main() {
    # 确保在项目根目录
    ensure_project_root

    # 解析命令
    case "${1:-help}" in
        install)
            cmd_install
            ;;
        test)
            cmd_test "$2"
            ;;
        test-watch)
            cmd_test_watch
            ;;
        lint)
            cmd_lint "$2"
            ;;
        format)
            cmd_format
            ;;
        clean)
            cmd_clean
            ;;
        run)
            shift
            cmd_run "$@"
            ;;
        check)
            cmd_check
            ;;
        init)
            cmd_init
            ;;
        docs)
            cmd_docs
            ;;
        build)
            cmd_build
            ;;
        dev)
            cmd_dev "$2"
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            log_error "未知命令: $1"
            show_help
            exit 1
            ;;
    esac
}

# 运行主函数
main "$@"