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

# 检查服务运行状态通用函数
check_status() {
    local label="$1"
    local sys_service="$2"
    local pid_file="$3"
    local port="$4"
    
    if systemctl is-active "$sys_service" > /dev/null 2>&1; then
        echo -e "  ● $label:   ${GREEN}运行中 (Systemd: Active)${NC}"
    elif [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        echo -e "  ● $label:   ${GREEN}运行中 (PID: $(cat "$pid_file"))${NC}"
    elif [ -n "$port" ] && (ss -tulpn 2>/dev/null | grep -q ":$port ") || (netstat -tulpn 2>/dev/null | grep -q ":$port "); then
        echo -e "  ● $label:   ${GREEN}运行中 (监听端口 $port)${NC}"
    else
        echo -e "  ● $label:   ${RED}已停止 (Stopped)${NC}"
    fi
}

# 2. Backend
check_status "后端 API (端口 8000)" "tara-backend.service" "$LOG_DIR/backend.pid" "8000"

# 3. Celery
check_status "Celery 跑批 Worker" "tara-celery.service" "$LOG_DIR/celery.pid" ""

# 4. Frontend
check_status "前端服务 (端口 3000)" "tara-frontend.service" "$LOG_DIR/frontend.pid" "3000"

echo -e "${BLUE}-------------------------------------------------------${NC}"
SERVER_IP=$(curl -s --connect-timeout 2 http://ifconfig.me 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "$SERVER_IP" ]; then
    SERVER_IP="127.0.0.1"
fi
echo -e "  🌐 前端访问地址: http://${SERVER_IP}:3000"
echo -e "  📖 API 交互文档: http://${SERVER_IP}:8000/docs"

echo -e "\n${YELLOW}查看运行日志:${NC}"
if systemctl is-active tara-backend.service > /dev/null 2>&1; then
    echo -e "  journalctl -u tara-frontend -f"
    echo -e "  journalctl -u tara-backend -f"
    echo -e "  journalctl -u tara-celery -f"
else
    echo -e "  tail -f logs/backend.log"
    echo -e "  tail -f logs/frontend.log"
    echo -e "  tail -f logs/celery.log"
fi
echo -e "${BLUE}=======================================================${NC}\n"
