#!/usr/bin/env bash
# ==============================================================================
# TARA AI 汽车网络安全分析平台 - 快速启动所有服务
# ==============================================================================
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"
mkdir -p "$PROJECT_ROOT/logs"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# 先清理已有旧进程
bash "$PROJECT_ROOT/stop.sh" > /dev/null 2>&1 || true

echo -e "${BLUE}正在快速启动 TARA AI 平台...${NC}"

# 检查 Redis
if command -v redis-cli &> /dev/null && redis-cli ping &> /dev/null; then
    echo -e "  ✓ Redis: 运行中"
else
    echo -e "  * 正在启动 Redis..."
    sudo systemctl start redis 2>/dev/null || sudo service redis-server start 2>/dev/null || redis-server --daemonize yes 2>/dev/null || true
fi

# 检查虚拟环境
if [ ! -d "$PROJECT_ROOT/backend/venv" ]; then
    echo -e "${YELLOW}未检测到后端虚拟环境，正在转入完整部署流程...${NC}"
    bash "$PROJECT_ROOT/deploy.sh"
    exit 0
fi

# 启动后端
cd "$PROJECT_ROOT/backend"
nohup ./venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > "$PROJECT_ROOT/logs/backend.log" 2>&1 &
echo $! > "$PROJECT_ROOT/logs/backend.pid"
echo -e "  ✓ 后端 API 已启动 (PID: $(cat "$PROJECT_ROOT/logs/backend.pid"))"

# 启动 Celery
nohup ./venv/bin/celery -A app.core.celery_app worker --loglevel=info > "$PROJECT_ROOT/logs/celery.log" 2>&1 &
echo $! > "$PROJECT_ROOT/logs/celery.pid"
echo -e "  ✓ Celery Worker 已启动 (PID: $(cat "$PROJECT_ROOT/logs/celery.pid"))"

# 启动前端
cd "$PROJECT_ROOT/frontend"
if [ ! -d "dist" ]; then
    echo -e "  * 首次运行缺少 dist 目录，正在构建前端..."
    npm run build
fi
nohup npm run preview -- --host 0.0.0.0 --port 3000 > "$PROJECT_ROOT/logs/frontend.log" 2>&1 &
echo $! > "$PROJECT_ROOT/logs/frontend.pid"
echo -e "  ✓ 前端服务已启动 (PID: $(cat "$PROJECT_ROOT/logs/frontend.pid"))"

cd "$PROJECT_ROOT"
sleep 2

SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "$SERVER_IP" ]; then
    SERVER_IP="127.0.0.1"
fi

echo -e "\n${GREEN}✓ TARA AI 平台已启动！${NC}"
echo -e "  🌐 前端访问地址: http://${SERVER_IP}:3000"
echo -e "  📖 API 交互文档: http://${SERVER_IP}:8000/docs\n"
