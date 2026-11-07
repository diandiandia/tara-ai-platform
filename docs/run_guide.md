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

## 2. Linux 原生一键部署与运行 (推荐)

为简化 Linux 服务器环境的部署复杂度，项目已提供全套自动化脚本。在项目根目录下执行一条指令即可完成环境依赖检查、虚拟环境创建、依赖安装、数据库初始化、默认管理员创建、前端生产构建与后台服务拉起：

### Step 2.1: 执行一键部署
```bash
# 赋予脚本执行权限并启动部署
chmod +x *.sh
./deploy.sh
```

部署脚本会自动完成后端 FastAPI (8000端口)、Celery 跑批 Worker 及前端静态反代服务 (3000端口) 的后台常驻拉起。

### Step 2.2: 运维管理指令
```bash
./status.sh   # 查看所有服务运行状态及访问地址
./stop.sh     # 一键停止所有后台服务
./restart.sh  # 一键重启所有服务
./start.sh    # 快速启动服务（无需重复构建）
```

### Step 2.3: 排查新建、删除画布无响应

如果日志出现 `address already in use`，表示新的后端进程无法绑定 8000，
不代表占用端口的旧后端也已退出。不要同时使用 Systemd、手动 Uvicorn 和
Docker 启动同一个端口。部署脚本在检测到 `tara-backend.service` 后会沿用
Systemd，不再额外启动后台进程。

先在实际部署服务器上查看：

```bash
sudo ss -ltnp 'sport = :8000'
sudo systemctl status tara-backend.service --no-pager
sudo systemctl show tara-backend.service -p MainPID -p ExecStart --no-pager
sudo journalctl -u tara-backend.service -n 50 --no-pager
```

核对监听 PID 与服务 MainPID；如果不一致，确认监听进程的启动来源和工作目录，
通过其对应的管理方式停止重复实例，再启动需要保留的服务。不要按进程名批量杀进程。
监听端口的旧实例可能仍在提供请求，因此仅看重启失败的日志无法确定某次删除的错误。

升级后，在后端的实际数据库配置环境中执行：

```bash
cd backend
./venv/bin/python manage.py migrate
```

该命令会给已有 `tara_steps` 表补齐 `asset_id` 索引，避免删除画布时，
级联查询资产历史分析步骤反复扫描整表；它不会删除分析数据。
前端需重新构建并部署。新建、删除请求等待上限为 30 秒，
删除失败原因会保留在 DFD 列表上方。请求超时不能证明服务器没有执行，
尤其是新建超时后，应先刷新列表确认，避免再次创建重复画布。

创建超时仍然发生时，查看同一 `DFD request=<ID>` 的日志：

```bash
sudo journalctl -u tara-backend.service --since '5 minutes ago' --no-pager
```

- `stage=received`：请求已到达后端路由，尚未完成数据库依赖和认证。
- `stage=validate`：认证已完成，开始查询域控和项目状态。
- `stage=commit`：开始插入并提交；如果长期停在这里，应检查数据库写锁和磁盘。
- `stage=refresh`：提交已成功，正在读取新建记录。
- `stage=created`：记录已读取；后续 `outcome=response_ready` 表示响应已生成，
  **不代表浏览器已经收到响应**。

日志包含累计 `elapsed_ms`，不记录画布名称、令牌或查询参数。
成功响应的 `X-Request-ID` 可与浏览器 Network 对照。
如果点击后没有 `stage=received`，需结合前端代理日志和监听进程排查；
仅凭缺少日志不能断定数据库有问题。不要通过增大前端超时或自动重试 POST 掩盖故障。

DFD 请求回归测试：

```bash
cd frontend
node --experimental-vm-modules --test tests/canvasStore.test.mjs
```

工作台使用 `/api/domains/{id}/workbench-summary` 查询结果是否存在及合并需求数量，
不再仅为按钮状态下载完整 TARA 结果或逐条需求来源。客户需求数量也使用独立计数接口。
域控对象替换但 ID 和状态不变时不会重复加载；切换域控会取消旧摘要请求。
完整分析结果仍在结果页加载，Excel 导出逻辑不变。
升级时必须同时部署后端新接口与前端构建；这些接口不需要数据库迁移。

后端创建及诊断日志回归测试（使用隔离数据库）：

```bash
cd backend
python -m pytest -q tests/test_diagram_requests.py tests/test_diagram_indexes.py tests/test_workbench_summary.py
```

工作台轻量加载与域控切换回归测试：

```bash
cd frontend
node --experimental-vm-modules --test tests/canvasStore.test.mjs tests/workbench.test.mjs
```

审阅页分别跟踪结果与资产的加载状态，任一请求未完成时不会显示“没有找到分析记录”。
请求失败显示独立错误与“重新加载评估数据”，不会被其他成功请求清除；
只有两项请求和表格转换成功后才判断数据为空。看到下载失败时不要重新启动分析。

```bash
cd frontend
node --experimental-vm-modules --test tests/taraReview.test.mjs
```

---

## 3. 手动分步部署与本地开发模式 (可选)

### TARA 历史保留与一次性清理

每次分析成功结束后，自动保留每个域控最新一次任务及最新成功任务，
删除更早任务及其步骤、CSR 映射、冲突和 OEM 缺口。存在 pending/running
任务或域控正在分析时跳过，避免破坏增量分析正在读取的旧结果。
项目、画布、资产、客户需求、固定目录和当前合并需求不删除。
最新失败/取消任务与最近成功任务都保留；历史自动清理失败会记录异常，
不把已经成功的分析改成失败。历史清理后，较老的人工修改不再可追溯。

已有库可在实际数据库配置环境执行一次：

```bash
cd backend
./venv/bin/python manage.py cleanup-history
```

该命令先使用 SQLite 在线备份 API 创建并检查一致性备份，再开启写事务清理。
备份位于项目 `backups_local/before_history_cleanup_*.db`，包含清理前历史，
需按访问权限和备份策略管理。命令不会自动删除备份，也不执行 VACUUM；
数据库文件未立即缩小是正常的，释放的页会复用。此命令当前仅支持 SQLite。
线上运行命令前核对 DATABASE_URL 与备份磁盘空间。

```bash
python -m pytest -q tests/test_tara_history.py
```

如需针对单个模块进行代码调试或深入排查，可按照以下分步指南手动启动：

### Step 3.1: 启动 Redis 缓存服务
确保 Redis 服务已在后台正常运行：
```bash
# Ubuntu/Debian 系统启动并检查
sudo service redis-server start
redis-cli ping  # 应返回 PONG
```

### Step 3.2: 初始化 Python 虚拟环境与依赖
进入 `backend` 文件夹，安装依赖并执行数据库迁移：
```bash
cd backend  # 项目根目录下执行

# 1. 创建虚拟环境并激活
python3 -m venv venv
source venv/bin/activate

# 2. 升级 pip 并安装平台依赖
pip install --upgrade pip
pip install -r requirements.txt
```

### Step 3.3: 初始化 SQLite 数据库与默认管理员
```bash
# 1. 一键建表初始化
python manage.py init-db

# 2. 创建默认管理员用户 (用户名: admin, 密码: Admin123)
python manage.py create-admin --user admin --pwd Admin123
```

### Step 3.4: 启动 FastAPI 后端 API 服务
```bash
# 激活虚拟环境并启动测试服务器 (监听 8000 端口)
source venv/bin/activate
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```
启动成功后，可在浏览器访问 Swagger API 交互文档：[http://localhost:8000/docs](http://localhost:8000/docs)

### Step 3.5: 启动 Celery 异步跑批任务进程
打开另一个终端，激活虚拟环境并启动异步 Worker：
```bash
cd backend  # 项目根目录下执行
source venv/bin/activate
celery -A app.core.celery_app worker --loglevel=info
```

### Step 3.6: 前端生产构建与预览启动
在另一终端进入 `frontend` 文件夹：
```bash
cd frontend  # 项目根目录下执行

# 1. 安装 npm 包
npm install

# 2. 编译生产静态文件
npm run build

# 3. 启动生产静态反代预览服务 (监听 3000 端口)
npm run preview -- --host 0.0.0.0 --port 3000
```
> **注意**：线上服务器部署请勿运行 `npm run dev`，避免暴露开发中间件与触发外部扫描语法报错。开发调试可在本地机器运行 `npm run dev`。
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
在项目根目录下，执行以下命令构建并启动全栈镜像：
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
