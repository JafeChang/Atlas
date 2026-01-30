#!/bin/bash
#
# Atlas服务启动脚本
#

PROJECT_DIR="/mnt/c/Users/bestz/Documents/projects/Atlas"
cd "$PROJECT_DIR" || exit 1

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}    Atlas 服务管理脚本${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# 获取本机IP
LOCAL_IP=$(hostname -I | awk '{print $1}')
echo -e "本机IP: ${YELLOW}${LOCAL_IP}${NC}"
echo ""

case "$1" in
    start)
        echo -e "${GREEN}启动Atlas服务...${NC}"
        echo ""

        # 检查Docker服务
        echo "1. 检查Docker服务..."
        if docker ps | grep -q atlas-postgres; then
            echo -e "   ${GREEN}✓${NC} Docker服务已运行"
        else
            echo "   启动Docker服务..."
            docker-compose up -d
            echo -e "   ${GREEN}✓${NC} Docker服务已启动"
        fi
        echo ""

        # 检查FastAPI服务
        echo "2. 检查FastAPI服务..."
        if pgrep -f uvicorn > /dev/null; then
            echo -e "   ${YELLOW}!${NC} FastAPI服务已在运行"
        else
            echo "   启动FastAPI服务..."
            source .venv/bin/activate
            nohup python3 -m uvicorn atlas.web.fastapi_app:app \
                --host 0.0.0.0 \
                --port 8000 \
                --log-level info \
                > atlas_api.log 2>&1 &
            echo -e "   ${GREEN}✓${NC} FastAPI服务已启动"
        fi
        echo ""

        # 等待服务就绪
        echo "等待服务就绪..."
        sleep 3

        # 测试服务
        echo "3. 测试服务连接..."
        if curl -s http://localhost:8000/health > /dev/null; then
            echo -e "   ${GREEN}✓${NC} FastAPI服务正常"
        else
            echo -e "   ${RED}✗${NC} FastAPI服务异常"
            echo "   查看日志: tail -f atlas_api.log"
        fi
        echo ""

        echo -e "${GREEN}========================================${NC}"
        echo -e "${GREEN}  访问地址:${NC}"
        echo -e "${GREEN}========================================${NC}"
        echo ""
        echo -e "API文档: ${YELLOW}http://localhost:8000/api/docs${NC}"
        echo -e "API文档: ${YELLOW}http://${LOCAL_IP}:8000/api/docs${NC}"
        echo ""
        echo -e "健康检查: ${YELLOW}curl http://localhost:8000/health${NC}"
        echo -e "健康检查: ${YELLOW}curl http://${LOCAL_IP}:8000/health${NC}"
        echo ""
        echo -e "Flower监控: ${YELLOW}http://localhost:5555${NC}"
        echo -e "MinIO控制台: ${YELLOW}http://localhost:9000${NC}"
        echo ""
        echo -e "${GREEN}========================================${NC}"
        echo -e "${GREEN}  服务已启动${NC}"
        echo -e "${GREEN}========================================${NC}"
        ;;

    stop)
        echo -e "${YELLOW}停止Atlas服务...${NC}"
        echo ""

        # 停止FastAPI
        echo "1. 停止FastAPI服务..."
        if pgrep -f uvicorn > /dev/null; then
            pkill -f uvicorn
            echo -e "   ${GREEN}✓${NC} FastAPI服务已停止"
        else
            echo -e "   ${YELLOW}!${NC} FastAPI服务未运行"
        fi
        echo ""

        # 询问是否停止Docker
        echo "2. Docker服务状态:"
        docker ps --format "table {{.Names}}\t{{.Status}}"
        echo ""
        read -p "是否停止Docker服务? (y/N): " stop_docker
        if [ "$stop_docker" = "y" ] || [ "$stop_docker" = "Y" ]; then
            docker-compose down
            echo -e "   ${GREEN}✓${NC} Docker服务已停止"
        else
            echo "   Docker服务保持运行"
        fi
        echo ""

        echo -e "${GREEN}========================================${NC}"
        echo -e "${GREEN}  服务已停止${NC}"
        echo -e "${GREEN}========================================${NC}"
        ;;

    restart)
        echo "重启Atlas服务..."
        $0 stop
        echo ""
        $0 start
        ;;

    status)
        echo -e "${GREEN}========================================${NC}"
        echo -e "${GREEN}  Atlas服务状态${NC}"
        echo -e "${GREEN}========================================${NC}"
        echo ""

        # Docker服务
        echo "1. Docker服务:"
        docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
        echo ""

        # FastAPI服务
        echo "2. FastAPI服务:"
        if pgrep -f uvicorn > /dev/null; then
            echo -e "   ${GREEN}✓ 运行中${NC}"
            echo -e "   PID: $(pgrep -f uvicorn)"
        else
            echo -e "   ${RED}✗ 未运行${NC}"
        fi
        echo ""

        # 端口监听
        echo "3. 端口监听:"
        netstat -tuln | grep -E "8000|5555|9000|5433|6379" | awk '{print "   " $4 " -> " $7}'
        echo ""

        # 健康检查
        echo "4. 健康检查:"
        if curl -s http://localhost:8000/health > /dev/null 2>&1; then
            echo -e "   ${GREEN}✓${NC} FastAPI服务正常"
        else
            echo -e "   ${RED}✗${NC} FastAPI服务异常"
        fi
        echo ""

        echo -e "${GREEN}========================================${NC}"
        ;;

    logs)
        echo "查看FastAPI日志 (Ctrl+C退出):"
        echo ""
        tail -f atlas_api.log
        ;;

    test)
        echo "测试API连接..."
        echo ""

        # 健康检查
        echo "1. 健康检查:"
        curl -s http://localhost:8000/health | python3 -m json.tool
        echo ""

        # API概览
        echo "2. API概览:"
        curl -s http://localhost:8000/api/v1/overview | python3 -m json.tool
        echo ""

        echo -e "${GREEN}✓${NC} 测试完成"
        ;;

    *)
        echo "用法: $0 {start|stop|restart|status|logs|test}"
        echo ""
        echo "命令说明:"
        echo "  start   - 启动所有服务"
        echo "  stop    - 停止所有服务"
        echo "  restart - 重启所有服务"
        echo "  status  - 查看服务状态"
        echo "  logs    - 查看FastAPI日志"
        echo "  test    - 测试API连接"
        echo ""
        exit 1
        ;;
esac
