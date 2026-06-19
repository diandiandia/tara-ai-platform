# TARA AI Platform (车载网络安全威胁分析与风险评估平台)

TARA AI Platform 是一款专门面向汽车行业的网络安全威胁分析与风险评估（Threat Analysis and Risk Assessment, TARA）平台。项目遵循 **ISO 21434** 汽车网络安全标准，为汽车安全工程师提供从数据流拓扑建模（DFD）、资产提取、危害场景分析、威胁建模、可行性评估到网络安全要求（CSR/CSO）定义的全生命周期评估工具。

---

## 🌟 核心功能特色

### 1. 多分支树状 TARA 评估模型 (与 pyTara_V 深度对齐)
- **多维度影响评估**：支持从安全 (Safety)、财务 (Financial)、运营 (Operational) 和隐私 (Privacy) 四个维度评估危害场景，自动合并计算综合影响等级。
- **五维可行性分析**：基于攻击耗时、专家水准、系统知识、攻击机会和设备成本等 5 个维度打分，通过查表法计算攻击可行性等级。
- **风险等级与处置决策联动**：自动查表判定风险值 (Risk Value 1-5)，联动匹配风险处置策略 (Avoid, Reduce, Share, Retain)。
- **安全要求 (CSR/CSO) 去重收敛**：自动提取针对特定安全控制 of 控制目标 (CSO)，并使用 AI 算法对分配到设备的网络安全要求 (CSR) 进行精炼去重。
- **增量分析与人工修改继承**：采用哈希指纹匹配，当资产属性未变化时自动跳过 AI 调用；且能完美继承安全员已确认或调整过的人工结论。
- **Excel 对齐评估表与控制矩阵**：全新设计“TARA 评估详情表 (与Excel对齐)”和“项目级安全控制要求矩阵”两大审阅看板，支持与导出 Excel 模板完全一致的平铺列展示与中英文多语言一键切换。
- **行内编辑与增删同步**：支持安全专家对所有行字段（损害场景、各项打分、处置决策、控制手段及安全要求等）进行行内编辑与整行删除或快捷增加，所有更改将原子化实时同步至数据库。
- **CAF Level 手动标定与容错**：自动根据 AF Level 初始化并联动计算风险值，同时支持专家手动标定覆盖（Calibration Override）最终的 CAF 等级，并支持不同大小写输入容错。

### 2. 交互式数据流图 (DFD) 画布
- 支持拖拽放置 **外部实体 (Entity)**、**处理过程 (Process)**、**数据存储 (Storage)**、**接口 (Interface)** 和 **信任边界 (Boundary)** 等 DFD 元素。新引入的“接口”节点专门对应硬件资产分类下的调试或外部物理接口（如 USB、JTAG、串口）。
- **智能端点重连**：直接拖拽连线端点即可自由更换 source / target 端点，支持数据流向与协议属性编辑。
- **布局自适应与内部滚动**：优化了工作台视图结构，画布卡片区与资产矩阵区按比例（flex 弹性比例）共用视口高度，并支持区域独立滚动，解决多功能图场景下的页面溢出问题。
- **协同与乐观锁控制**：提供乐观锁版本控制，保障多人协同编辑时不覆盖他人的工作成果。

### 3. AI 智能一键画图助手
- **双向需求对话**：内置 AI 拓扑助理对话框，工程师可通过文字沟通（支持多行换行）告知安全设计场景。
- **一键清空与重构**：点击“一键生成 DFD 图”按钮，画布会自动清空并完美渲染出 AI 确认好的拓扑，工程师可在画布上继续拖拽与细化修改。

### 4. 项目归档解耦设计
- **自动进度推导**：随域控分析状态实时推导项目进度（草稿 / 进行中 / 分析完成）。
- **手动归档冷冻**：分析跑批结束后，非归档状态下工作台仍保持完全可编辑，支持增删子域控及重跑 TARA；唯有管理员手动点击“归档锁定”后，项目才会置为只读冷冻。

### 5. 高性能合规报告导出
- **Excel (.xlsx) 导出**：导出标准 36 列平铺的 ISO 21434 TARA 矩阵报表，包含高级灰配色表头与自适应列宽。
- **CSV 导出**：导出完全一致的 36 列矩阵，并采用 `utf-8-sig` (带有 BOM) 编码，解决 Windows Excel 打开中文时乱码的问题。

---

## 🛠 技术栈说明

- **后端**：
  - **核心框架**：Python 3.10+ / FastAPI
  - **ORM & 数据库**：SQLAlchemy / SQLite (`tara.db`)
  - **任务队列**：Celery / Redis (用于 LLM 分析的异步多分支并发跑批)
  - **大模型接口**：兼容 DeepSeek-v4-flash 等 OpenAI 规范格式接口
- **前端**：
  - **核心框架**：React 19 / Vite
  - **画布引擎**：ReactFlow v11
  - **状态管理**：Zustand
  - **样式规范**：Vanilla CSS (毛玻璃拟物化风格，支持暗黑模式) / TailwindCSS 兼容

---

## 🚀 快速启动指南

### 方式一：Docker Compose 启动 (推荐)
一键启动完整生产环境服务（包括 API、Web 前端、Redis 及 Celery Worker 异步跑批器）：
```bash
docker-compose up -d --build
```
- **FastAPI 后端 API**: `http://localhost:8000`
- **React 前端 Web 平台**: `http://localhost:3000`

---

### 方式二：本地开发调试启动

#### 1. 启动依赖服务 (Redis)
确保本地 Redis 服务已启动，并在端口 `6379` 上监听。

#### 2. 后端服务启动
1. 进入 backend 目录，创建并激活虚拟环境：
   ```bash
   cd backend
   python -m venv venv
   source venv/bin/activate  # Windows 下使用 venv\Scripts\activate
   ```
2. 安装 Python 依赖：
   ```bash
   pip install -r requirements.txt
   ```
3. 初始化数据库并创建初始管理员（首次运行需要）：
   ```bash
   ./venv/bin/python manage.py init-db
   ./venv/bin/python manage.py create-admin --user admin --pwd Admin123
   ```
4. 启动 FastAPI API 服务（具备热重载）：
   ```bash
   ./venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
   ```
5. 启动 Celery Worker 跑批任务进程：
   ```bash
   ./venv/bin/celery -A app.core.celery_app worker --loglevel=info
   ```

#### 3. 前端服务启动
1. 进入 frontend 目录：
   ```bash
   cd ../frontend
   ```
2. 安装 npm 依赖包：
   ```bash
   npm install
   ```
3. 启动 Vite 开发调试服务：
   ```bash
   npm run dev -- --host 0.0.0.0 --port 3000
   ```

---

## 🧪 自动化测试运行

为保证代码修改后功能的正确性，后端包含完整的接口与业务单元测试用例。
在 `/backend` 目录下，运行如下命令执行测试：
```bash
PYTHONPATH=. ./venv/bin/python -m unittest test_endpoints.py
```
- 测试使用独立的 `tara_test.db` SQLite 文件，测试结束后会自动清理，不会对 `tara.db` 生产数据产生任何影响。

---

## 📁 项目目录结构

```text
tara-ai-platform/
├── backend/                   # 后端工程
│   ├── app/                   # FastAPI 应用逻辑
│   │   ├── api/               # API 路由层 (project, diagram, tara, report)
│   │   ├── core/              # 核心配置、数据库连接、Celery 实例
│   │   ├── models/            # SQLAlchemy 数据模型
│   │   ├── schemas/           # Pydantic 传输 Schema 验证
│   │   └── worker/            # Celery 异步跑批任务
│   ├── Dockerfile
│   ├── requirements.txt
│   └── test_endpoints.py      # 后端单元测试用例
├── frontend/                  # 前端工程
│   ├── src/
│   │   ├── components/        # React 组件 (DfdEditor, Workbench, TaraResults)
│   │   ├── stores/            # Zustand 状态仓库 (canvasStore, projectStore)
│   │   └── App.jsx
│   ├── package.json
│   └── vite.config.js
├── docker-compose.yml         # 多容器统管编排
├── tara.db                    # 生产数据库文件
└── .gitignore                 # 版本排除规则
```

---

## ⚠️ 安全与开发注意事项
1. **API Keys 存储安全**：大模型 API Key 会在系统启动后，在“系统设置”页面由管理员在前端配置并密文加密存储在数据库中，本地开发时切勿直接硬编码在代码库中。
2. **异步进程修改热更**：本地开发中修改了 `tasks.py` 内部的任务逻辑后，**必须重启 Celery Worker 进程**，代码修改方可生效（我们已在代码中加入了 `task_failure` 全局阻断监听，防止未重启进程导致参数绑定冲突卡死的问题）。
