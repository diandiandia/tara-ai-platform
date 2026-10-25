#!/usr/bin/env bash
# ==============================================================================
# TARA AI 汽车网络安全分析平台 - 一键重启服务
# ==============================================================================
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "$PROJECT_ROOT/stop.sh"
sleep 1
bash "$PROJECT_ROOT/start.sh"
