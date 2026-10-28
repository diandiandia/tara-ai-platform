#!/usr/bin/env bash
# ==============================================================================
# TARA AI 汽车网络安全分析平台 - 快速启动所有服务
# ==============================================================================
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"
mkdir -p "$PROJECT_ROOT/logs"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# 检查 Redis
if command -v redis-cli &> /dev/null && redis-cli ping &> /dev/null; then
    echo -e "  ✓ Redis: 运行中"
else
    echo -e "  * 正在启动 Redis..."
    sudo systemctl start redis 2>/dev/null || sudo service redis-server start 2>/dev/null || redis-server --daemonize yes 2>/dev/null || true
fi

# 如果注册了 Systemd 服务，直接使用 systemctl 拉起
if systemctl list-unit-files | grep -q "tara-backend.service" 2>/dev/null; then
    echo -e "${BLUE}正在通过 systemctl 启动托管服务...${NC}"
    sudo systemctl start tara-backend tara-celery tara-frontend
    echo -e "${GREEN}✓ 服务已启动完成！${NC}"
    bash "$PROJECT_ROOT/status.sh"
    exit 0
fi

# 独立运行模式
bash "$PROJECT_ROOT/stop.sh" > /dev/null 2>&1 || true

echo -e "${BLUE}正在后台拉起 TARA AI 平台服务...${NC}"

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
    echo -e "  * 缺少 dist 目录，正在编译前端静态文件..."
    npm run build
fi
nohup npm run preview -- --host 0.0.0.0 --port 3000 > "$PROJECT_ROOT/logs/frontend.log" 2>&1 &
echo $! > "$PROJECT_ROOT/logs/frontend.pid"
echo -e "  ✓ 前端服务已启动 (PID: $(cat "$PROJECT_ROOT/logs/frontend.pid"))"

cd "$PROJECT_ROOT"
sleep 2

bash "$PROJECT_ROOT/status.sh"
