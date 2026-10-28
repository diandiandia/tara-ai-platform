#!/usr/bin/env bash
# ==============================================================================
# TARA AI 汽车网络安全分析平台 - 一键重启服务
# ==============================================================================
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

if systemctl list-unit-files | grep -q "tara-backend.service" 2>/dev/null; then
    echo -e "${BLUE}正在通过 systemctl 重启托管服务...${NC}"
    sudo systemctl restart tara-backend tara-celery tara-frontend
    echo -e "${GREEN}✓ 服务已重启完成！${NC}"
    bash "$PROJECT_ROOT/status.sh"
else
    bash "$PROJECT_ROOT/stop.sh"
    sleep 1
    bash "$PROJECT_ROOT/start.sh"
fi
