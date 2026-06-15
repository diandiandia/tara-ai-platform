# TARA AI 汽车网络安全分析平台 - 运行与测试指南

本指南旨在指导开发与测试人员在本地或服务器环境部署、启动并测试 **TARA AI 平台 (v3)** 全栈系统。

---

## 1. 环境依赖与准备

运行本平台需要以下系统环境支持：
*   **操作系统**：Linux (建议 Ubuntu 20.04/22.04+) 或 macOS
*   **运行时环境**：
    *   **Python**：3.10 及以上版本 (建议使用 virtualenv 虚拟环境)
    *   **Node.js**：v18 及以上版本 (建议使用配套的 npm)
*   **中间件服务**：
    *   **Redis**：需要运行在默认端口 `6379`，作为 Celery 异步队列代理及 WebSocket 并发编辑锁的同步介质。

---

## 2. 数据库与后端部署启动

### Step 2.1: 启动 Redis 缓存服务
确保 Redis 服务已在后台正常运行：
```bash
# Ubuntu/Debian 系统启动并检查
sudo service redis-server start
redis-cli ping  # 应返回 PONG
```

### Step 2.2: 初始化 Python 虚拟环境与依赖
进入 `backend` 文件夹，安装依赖并执行数据库迁移：
```bash
cd /home/ubuntu/tara-ai-platform/backend

# 1. 创建虚拟环境并激活
python3 -m venv venv
source venv/bin/activate

# 2. 升级 pip 并安装平台依赖
pip install --upgrade pip
pip install -r requirements.txt
```

### Step 2.3: 初始化 SQLite 数据库与默认管理员
```bash
# 1. 一键建表初始化
python manage.py init-db

# 2. 创建默认管理员用户 (用户名: admin, 密码: Admin123)
python manage.py create-admin --user admin --pwd Admin123
```

### Step 2.4: 启动 FastAPI 后端 API 服务
```bash
# 激活虚拟环境并启动测试服务器 (监听 8000 端口)
source venv/bin/activate
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```
启动成功后，可在浏览器访问 Swagger API 交互文档：[http://localhost:8000/docs](http://localhost:8000/docs)

### Step 2.5: 启动 Celery 异步跑批任务进程
打开另一个终端，激活虚拟环境并启动异步 Worker：
```bash
cd /home/ubuntu/tara-ai-platform/backend
source venv/bin/activate
celery -A app.core.celery_app worker --loglevel=info
```

---

## 3. 前端部署与运行

进入 `frontend` 文件夹，执行依赖安装并启动 Vite 热更新服务器：
```bash
cd /home/ubuntu/tara-ai-platform/frontend

# 1. 安装 npm 包
npm install

# 2. 启动前端测试服务并监听 3000 端口
npm run dev -- --host 0.0.0.0 --port 3000
```
启动成功后，访问前端控制台页面：[http://localhost:3000/](http://localhost:3000/)

---

## 4. 全栈系统功能测试流程 (Walkthrough)

测试人员可按照以下 6 个核心阶段对系统进行全功能回归与探索性测试：

### 阶段 ①：用户登录与安全控制 (BR-64)
1. 打开 [http://localhost:3000/](http://localhost:3000/)，输入管理员账号 `admin` 与密码 `Admin123` 登录。
2. 登录成功后，顶部导航栏应显示当前用户名及 `管理员` 角色标签。

### 阶段 ②：创建项目与域控引导 (BR-03, BR-4.2.2)
1. 点击右上角 **“创建新项目”**，在弹出的玻璃态模态框中输入项目名称（最多50字）和描述，点击保存。
2. 在卡片列表中点击刚创建的项目进入工作台。
3. 点击左侧栏 **“新建子域控”**，输入名称并保存。此时应能正常弹出**引导模态框**，为您提供 `[开始 DFD 绘图分析]` 和 `[继续创建子域控]` 两个选择。

### 阶段 ③：DFD 拓扑画图与本地容灾 (BR-13, BR-16, BR-17, BR-72)
1. 点击域控树中的节点，在右侧点击 **“新建 DFD 画布”** 按钮创建一个画布并进入。
2. **排他锁验证**：当您处于编辑页时，顶部应显示“排他锁定中”。若多开浏览器，非锁定用户应强制变为只读模式并提示当前编辑者姓名。
3. **AI 绘图生成**：在右侧 AI 助手的文本输入框中输入 “车载诊断拓扑” 并点击 **“一键生成拓扑图”**，确认后画布应自动加载出 OBD接口、网关ECU 及诊断进程等 DFD 节点，并完成自适应布局。
4. **自动保存防抖**：修改任意节点名称或连线后，等待 2 秒，右上角状态栏应自动显示已保存。
5. **乐观锁并发测试**：若遭遇版本冲突修改冲突，保存时会弹出 409 Conflict 对话框，引导您刷新。您的最新修改已被安全保存在 `LocalStorage` 中以防丢失。

### 阶段 ④：资产提取与 AI 智能去重 (BR-25, BR-33, BR-35)
1. 返回工作台，点击 **“自动从画布提取资产”**。表格将自动加载出刚才画布中绘制的各种软硬件、数据和通信协议资产。
2. 点击 **“AI 资产去重”**。如果提取的资产有拼写相似或相同项，AI 弹窗将给出合并建议。
3. 点击确认去重，被建议删除的资产状态会自动变为 `Rejected`，且备注会自动追加合并链路，完美留存历史核对痕迹。

### 阶段 ⑤：启动异步 TARA 分析 (BR-36, BR-40, BR-45, BR-69)
1. 勾选将资产列表中的至少一个资产置为 `Confirmed` (已确认) 状态。（若没有 confirmed 资产，直接点击启动分析会报错拦截）。
2. 点击 **“启动 TARA 分析”**，左侧域控树状态显示为 `分析中`，进度条从 0% 跑至 100%。测试期间可点击红色的取消按钮测试强行撤销运行。
3. 分析完成后，点击 **“查看 TARA 结果”**。

### 阶段 ⑥：专家审阅与脱敏报告导出 (BR-51, BR-57, BR-77)
1. 在 5 阶段审阅表里双击行，修改 AI 的评估结论（必须填入修改理由完成安全审计留痕）。
2. 点击“项目级安全控制矩阵”选项卡，查看 CSO (网络安全目标) 和 CSR (要求) 的矩阵输出。如果在阶段④里将风险设定为 Accept 或 Transfer，在此处应能观察到对应的 CSR 被豁免。
3. 在上方面板勾选 **“导出脱敏版”** 并点击 **“立即下载”**，系统会过滤敏感漏洞攻击链条，仅保留 CSR 安全需求后输出完美排版的 XLSX/DOCX 文件。

---

## 5. 使用 Docker Compose 一键启动 (推荐)

如果您希望在全新的物理机或虚拟机中快速启动，或者在独立 Docker 容器内测试，我们已为您编写好了全套 Docker 配置文件（`Dockerfile` 及 `docker-compose.yml`）。

### Step 5.1: 启动容器服务
在项目根目录 `/home/ubuntu/tara-ai-platform` 下，执行以下命令构建并启动全栈镜像：
```bash
# 构建镜像并在后台拉起所有服务（Redis, Backend, Celery, Frontend）
docker compose up -d --build
```
该命令将：
*   拉起官方轻量级 `redis:7-alpine` 容器。
*   构建 Python 3.10 后端容器，映射主机的 `tara.db` 数据库及 `exports/` 导出卷。
*   构建 Celery 后端跑批容器。
*   构建基于多阶段构建的 Node.js 编译产物，并利用 `Nginx:alpine` 容器路由代理 `/api` 与 `/ws` 请求。

### Step 5.2: 检查服务状态
```bash
# 查看容器运行状态
docker compose ps
```
运行状态均应为 `Up`。

### Step 5.3: 测试与访问
容器运行后，可直接在主机访问服务，映射端口与本地运行一致：
*   **前端网页端口**：[http://localhost:3000/](http://localhost:3000/)
*   **后端 API 文档 (Swagger)**：[http://localhost:8000/docs](http://localhost:8000/docs)
*   **管理员登录**：用户名为 `admin`，密码为 `Admin123`
*   **停止所有服务**：
    ```bash
    docker compose down
    ```

