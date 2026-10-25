#!/usr/bin/env bash
# ==============================================================================
# TARA AI 汽车网络安全分析平台 - 服务状态查询
# ==============================================================================
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$PROJECT_ROOT/logs"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}=======================================================${NC}"
echo -e "${GREEN}            TARA AI 平台服务运行状态                   ${NC}"
echo -e "${BLUE}=======================================================${NC}"

# 1. Redis
if command -v redis-cli &> /dev/null && redis-cli ping &> /dev/null; then
    echo -e "  ● Redis 缓存服务:       ${GREEN}运行中 (Running)${NC}"
else
    echo -e "  ● Redis 缓存服务:       ${RED}未运行 (Stopped)${NC}"
fi

# 2. Backend
if [ -f "$LOG_DIR/backend.pid" ] && kill -0 "$(cat "$LOG_DIR/backend.pid")" 2>/dev/null; then
    echo -e "  ● 后端 API (端口 8000):   ${GREEN}运行中 (PID: $(cat "$LOG_DIR/backend.pid"))${NC}"
else
    echo -e "  ● 后端 API (端口 8000):   ${RED}已停止 (Stopped)${NC}"
fi

# 3. Celery
if [ -f "$LOG_DIR/celery.pid" ] && kill -0 "$(cat "$LOG_DIR/celery.pid")" 2>/dev/null; then
    echo -e "  ● Celery 跑批 Worker:   ${GREEN}运行中 (PID: $(cat "$LOG_DIR/celery.pid"))${NC}"
else
    echo -e "  ● Celery 跑批 Worker:   ${RED}已停止 (Stopped)${NC}"
fi

# 4. Frontend
if [ -f "$LOG_DIR/frontend.pid" ] && kill -0 "$(cat "$LOG_DIR/frontend.pid")" 2>/dev/null; then
    echo -e "  ● 前端服务 (端口 3000):   ${GREEN}运行中 (PID: $(cat "$LOG_DIR/frontend.pid"))${NC}"
else
    echo -e "  ● 前端服务 (端口 3000):   ${RED}已停止 (Stopped)${NC}"
fi

echo -e "${BLUE}-------------------------------------------------------${NC}"
SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "$SERVER_IP" ]; then
    SERVER_IP="127.0.0.1"
fi
echo -e "  🌐 前端访问地址: http://${SERVER_IP}:3000"
echo -e "  📖 API 交互文档: http://${SERVER_IP}:8000/docs"

echo -e "\n${YELLOW}查看实时日志命令:${NC}"
echo -e "  tail -f logs/backend.log"
echo -e "  tail -f logs/frontend.log"
echo -e "  tail -f logs/celery.log"
echo -e "${BLUE}=======================================================${NC}\n"
