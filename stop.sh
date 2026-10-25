#!/usr/bin/env bash
# ==============================================================================
# TARA AI 汽车网络安全分析平台 - 停止所有后台服务
# ==============================================================================
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$PROJECT_ROOT/logs"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${YELLOW}正在停止 TARA AI 平台所有服务...${NC}"

# 1. 依据 PID 停止
stop_pid() {
    local pid_file="$1"
    local name="$2"
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo -e "  * 正在终止 $name (PID: $pid)..."
            kill "$pid" 2>/dev/null || true
            sleep 1
            if kill -0 "$pid" 2>/dev/null; then
                kill -9 "$pid" 2>/dev/null || true
            fi
        fi
        rm -f "$pid_file"
    fi
}

stop_pid "$LOG_DIR/frontend.pid" "前端服务 (端口 3000)"
stop_pid "$LOG_DIR/backend.pid" "后端 API 服务 (端口 8000)"
stop_pid "$LOG_DIR/celery.pid" "Celery Worker 进程"

# 2. 兜底按端口与特征清理残留进程
echo -e "  * 检查并清理端口占用..."
if command -v fuser &> /dev/null; then
    fuser -k 3000/tcp > /dev/null 2>&1 || true
    fuser -k 8000/tcp > /dev/null 2>&1 || true
fi

# 清理残留的 uvicorn 与 celery 进程
pkill -f "uvicorn app.main:app" > /dev/null 2>&1 || true
pkill -f "celery -A app.core.celery_app" > /dev/null 2>&1 || true
pkill -f "vite preview --host 0.0.0.0 --port 3000" > /dev/null 2>&1 || true

echo -e "${GREEN}✓ 所有服务已成功停止！${NC}"
