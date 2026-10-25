#!/usr/bin/env bash
# ==============================================================================
# TARA AI 汽车网络安全分析平台 - Linux 一键部署与启动脚本 (方案2: 原生生产模式)
# ==============================================================================
set -e

# 定位脚本所在的项目根目录
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

# 颜色输出配置
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}=======================================================${NC}"
echo -e "${GREEN}      TARA AI 平台 - Linux 一键自动化部署脚本          ${NC}"
echo -e "${BLUE}=======================================================${NC}"

# 检测 sudo 权限
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo &> /dev/null; then
        SUDO="sudo"
    fi
fi

# 自动安装系统包辅助函数 (支持 Debian/Ubuntu/CentOS)
install_sys_pkg() {
    local pkg="$1"
    echo -e "  ${YELLOW}* 正在自动安装系统依赖包: $pkg ...${NC}"
    if command -v apt-get &> /dev/null; then
        $SUDO apt-get update -qq > /dev/null 2>&1 || true
        $SUDO apt-get install -y -qq "$pkg" > /dev/null 2>&1 || true
    elif command -v yum &> /dev/null; then
        $SUDO yum install -y "$pkg" > /dev/null 2>&1 || true
    elif command -v dnf &> /dev/null; then
        $SUDO dnf install -y "$pkg" > /dev/null 2>&1 || true
    fi
}

# 如果用户传入 --docker 参数，则调用 Docker Compose 部署
if [ "$1" == "--docker" ]; then
    echo -e "${YELLOW}[提示] 检测到 --docker 参数，正在调用 Docker Compose 方案...${NC}"
    if ! command -v docker &> /dev/null; then
        echo -e "${RED}[错误] 未检测到 Docker 命令，请先安装 Docker！${NC}"
        exit 1
    fi
    docker compose up -d --build
    echo -e "${GREEN}[成功] Docker 容器已拉起，请访问 http://localhost:3000${NC}"
    exit 0
fi

# ------------------------------------------------------------------------------
# 步骤 1: 基础环境检测与系统依赖自动安装
# ------------------------------------------------------------------------------
echo -e "\n${BLUE}[1/6] 检查系统环境与基础组件...${NC}"

# 1. 检查 Python 3
if ! command -v python3 &> /dev/null; then
    echo -e "  ${YELLOW}* 未检测到 Python 3，尝试自动安装...${NC}"
    install_sys_pkg "python3 python3-pip python3-venv"
    if ! command -v python3 &> /dev/null; then
        echo -e "${RED}[错误] 未能自动安装 Python 3，请手动运行: sudo apt update && sudo apt install -y python3 python3-pip python3-venv${NC}"
        exit 1
    fi
fi
PYTHON_VER=$(python3 --version 2>&1)
echo -e "  ✓ Python: $PYTHON_VER"

# 检查 python3-venv 支持
if ! python3 -m venv --help &> /dev/null; then
    echo -e "  ${YELLOW}* 缺少 python3-venv 模块，尝试自动安装...${NC}"
    install_sys_pkg "python3-venv"
fi

# 2. 检查 Node.js 与 npm
if ! command -v node &> /dev/null || ! command -v npm &> /dev/null; then
    echo -e "  ${YELLOW}* 未检测到 Node.js/npm，尝试通过 NodeSource 自动配置安装 Node.js 20...${NC}"
    if command -v curl &> /dev/null && command -v apt-get &> /dev/null; then
        curl -fsSL https://deb.nodesource.com/setup_20.x | $SUDO -E bash - > /dev/null 2>&1 || true
        $SUDO apt-get install -y nodejs > /dev/null 2>&1 || true
    fi
    if ! command -v node &> /dev/null || ! command -v npm &> /dev/null; then
        echo -e "${RED}[错误] 请先安装 Node.js (v18+) 与 npm，例如运行:${NC}"
        echo -e "       curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -"
        echo -e "       sudo apt-get install -y nodejs"
        exit 1
    fi
fi
NODE_VER=$(node -v 2>&1)
NPM_VER=$(npm -v 2>&1)
echo -e "  ✓ Node.js: $NODE_VER, npm: $NPM_VER"

# 3. 检查与自动安装 Redis
echo -e "  * 检查 Redis 缓存服务..."
if ! command -v redis-server &> /dev/null && ! command -v redis-cli &> /dev/null; then
    echo -e "  ${YELLOW}* 未检测到 Redis，尝试自动安装 redis-server ...${NC}"
    install_sys_pkg "redis-server"
fi

# 启动 Redis 服务
if command -v redis-cli &> /dev/null && redis-cli ping &> /dev/null; then
    echo -e "  ✓ Redis: 运行中 (PONG)"
else
    echo -e "  * 正在拉起 Redis 服务..."
    $SUDO systemctl enable redis-server > /dev/null 2>&1 || true
    $SUDO systemctl start redis-server > /dev/null 2>&1 || $SUDO systemctl start redis > /dev/null 2>&1 || $SUDO service redis-server start > /dev/null 2>&1 || redis-server --daemonize yes > /dev/null 2>&1 || true
    sleep 1
    if command -v redis-cli &> /dev/null && redis-cli ping &> /dev/null; then
        echo -e "  ✓ Redis 启动成功！"
    else
        echo -e "  ${YELLOW}⚠️ 提示: Redis 未能成功响应。若后台跑批任务需要，请手动执行: sudo service redis-server start${NC}"
    fi
fi

# ------------------------------------------------------------------------------
# 步骤 2: 停止旧服务并清理端口占用
# ------------------------------------------------------------------------------
echo -e "\n${BLUE}[2/6] 检查并清理旧进程与端口...${NC}"
mkdir -p "$PROJECT_ROOT/logs"

if [ -f "$PROJECT_ROOT/stop.sh" ]; then
    bash "$PROJECT_ROOT/stop.sh" > /dev/null 2>&1 || true
fi

# ------------------------------------------------------------------------------
# 步骤 3: 初始化后端 Python 虚拟环境与自动安装项目依赖
# ------------------------------------------------------------------------------
echo -e "\n${BLUE}[3/6] 自动安装后端 Python 依赖并初始化数据库...${NC}"
cd "$PROJECT_ROOT/backend"

if [ ! -d "venv" ]; then
    echo -e "  * 创建 Python 虚拟隔离环境 (backend/venv)..."
    python3 -m venv venv
fi

# 激活虚拟环境
source venv/bin/activate
echo -e "  * 正在更新 pip 并自动安装 requirements.txt (FastAPI, Celery, SQLAlchemy 等)..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo -e "  ✓ 后端 Python 依赖包全部安装完成！"

echo -e "  * 正在初始化 SQLite 数据库及数据表结构 (manage.py init-db)..."
python manage.py init-db

echo -e "  * 正在创建/确认默认管理员账号 (admin / Admin123)..."
python manage.py create-admin --user admin --pwd Admin123

deactivate
cd "$PROJECT_ROOT"

# ------------------------------------------------------------------------------
# 步骤 4: 前端依赖自动安装与生产静态编译构建
# ------------------------------------------------------------------------------
echo -e "\n${BLUE}[4/6] 自动安装前端依赖并编译生产静态代码...${NC}"
cd "$PROJECT_ROOT/frontend"

if [ ! -d "node_modules" ]; then
    echo -e "  * 正在自动执行 npm install 安装前端依赖包 (React, Vite, Lucide 等)..."
    npm install --silent
    echo -e "  ✓ 前端 npm 依赖包安装完成！"
else
    echo -e "  ✓ 前端依赖已就绪 (node_modules 已存在)"
fi

echo -e "  * 正在执行生产静态编译 (npm run build)..."
npm run build
echo -e "  ✓ 前端生产静态包已生成 (dist/)！"
cd "$PROJECT_ROOT"

# ------------------------------------------------------------------------------
# 步骤 5: 启动全栈生产服务 (后台守护进程常驻运行)
# ------------------------------------------------------------------------------
echo -e "\n${BLUE}[5/6] 启动后台服务进程 (关掉终端也能常驻运行)...${NC}"

# 1. 启动 FastAPI 后端服务 (端口 8000)
cd "$PROJECT_ROOT/backend"
nohup ./venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > "$PROJECT_ROOT/logs/backend.log" 2>&1 &
echo $! > "$PROJECT_ROOT/logs/backend.pid"
echo -e "  ✓ FastAPI 后端 API 已启动 (PID: $(cat "$PROJECT_ROOT/logs/backend.pid"), 监听 0.0.0.0:8000)"

# 2. 启动 Celery 异步跑批进程
nohup ./venv/bin/celery -A app.core.celery_app worker --loglevel=info > "$PROJECT_ROOT/logs/celery.log" 2>&1 &
echo $! > "$PROJECT_ROOT/logs/celery.pid"
echo -e "  ✓ Celery 异步跑批 Worker 已启动 (PID: $(cat "$PROJECT_ROOT/logs/celery.pid"))"

# 3. 启动前端静态反代服务 (端口 3000)
cd "$PROJECT_ROOT/frontend"
nohup npm run preview -- --host 0.0.0.0 --port 3000 > "$PROJECT_ROOT/logs/frontend.log" 2>&1 &
echo $! > "$PROJECT_ROOT/logs/frontend.pid"
echo -e "  ✓ 前端生产反代服务已启动 (PID: $(cat "$PROJECT_ROOT/logs/frontend.pid"), 监听 0.0.0.0:3000)"

cd "$PROJECT_ROOT"

# ------------------------------------------------------------------------------
# 步骤 6: 健康检查与部署结果展示
# ------------------------------------------------------------------------------
echo -e "\n${BLUE}[6/6] 服务健康检查...${NC}"
sleep 2

# 获取本机 IP
SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "$SERVER_IP" ]; then
    SERVER_IP="127.0.0.1"
fi

# 检查后端
if curl -s -f http://127.0.0.1:8000/ > /dev/null 2>&1; then
    echo -e "  ✓ 后端 API 健康检查: ${GREEN}正常 (Healthy)${NC}"
else
    echo -e "  ${YELLOW}⚠️ 后端启动中或日志异常，请查看 logs/backend.log${NC}"
fi

# 检查前端
if curl -s -f http://127.0.0.1:3000/ > /dev/null 2>&1; then
    echo -e "  ✓ 前端服务健康检查: ${GREEN}正常 (Healthy)${NC}"
else
    echo -e "  ${YELLOW}⚠️ 前端启动中或日志异常，请查看 logs/frontend.log${NC}"
fi

echo -e "\n${BLUE}=======================================================${NC}"
echo -e "${GREEN}             🎉 TARA AI 平台全栈部署成功！            ${NC}"
echo -e "${BLUE}=======================================================${NC}"
echo -e "  🌐 ${GREEN}前端访问地址${NC}: http://${SERVER_IP}:3000"
echo -e "  📖 ${GREEN}后端 API 文档${NC}: http://${SERVER_IP}:8000/docs"
echo -e "  👤 ${GREEN}默认登录账号${NC}: admin"
echo -e "  🔑 ${GREEN}默认登录密码${NC}: Admin123"
echo -e "-------------------------------------------------------"
echo -e "  📁 运行日志目录: ${PROJECT_ROOT}/logs/"
echo -e "     - 后端日志: tail -f logs/backend.log"
echo -e "     - 前端日志: tail -f logs/frontend.log"
echo -e "     - 跑批日志: tail -f logs/celery.log"
echo -e "-------------------------------------------------------"
echo -e "  ⚙️  常用运维管理指令:"
echo -e "     - 查看运行状态: ./status.sh"
echo -e "     - 停止所有服务: ./stop.sh"
echo -e "     - 重启所有服务: ./restart.sh"
echo -e "${BLUE}=======================================================${NC}\n"
