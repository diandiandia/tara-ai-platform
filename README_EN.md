# TARA AI Platform (Automotive Threat Analysis and Risk Assessment Platform)

TARA AI Platform is a professional Threat Analysis and Risk Assessment (TARA) platform designed specifically for the automotive industry. Following the **ISO 21434** automotive cybersecurity standard, the platform provides cybersecurity engineers with full-lifecycle assessment tools, ranging from Data Flow Diagram (DFD) topological modeling, asset extraction, damage scenario analysis, threat modeling, attack feasibility assessment, to Cybersecurity Requirement (CSR/CSO) definitions.

---

## 🌟 Key Features

### 1. Multi-Branch Tree TARA Assessment Model (Aligned with pyTara_V)
- **Multi-Dimensional Impact Assessment**: Supports assessing damage scenarios across four dimensions: Safety, Financial, Operational, and Privacy (SFOP), and automatically calculates the overall impact level.
- **Five-Dimensional Feasibility Analysis**: Scores attack feasibility from 5 dimensions (Elapsed Time, Specialist Expertise, Knowledge of the Item, Opportunity, and Equipment Cost) based on the lookup table method.
- **Risk Level & Decision Logic Coupling**: Automatically determines the Risk Value (1-5) via look-up tables and links it to risk treatment strategies (Avoid, Reduce, Share, Retain).
- **Cybersecurity Requirement (CSR/CSO) Deduplication & Convergence**: Automatically extracts Cybersecurity Goals (CSO) and utilizes AI algorithms to refine and deduplicate Cybersecurity Requirements (CSR) allocated to devices.
- **Incremental Analysis & Human override Inheritance**: Employs hash fingerprint matching to skip LLM calls when asset attributes remain unchanged, and seamlessly inherits confirmed or overridden conclusions from safety engineers.

### 2. Interactive Data Flow Diagram (DFD) Canvas
- Supports drag-and-drop placement of **External Entity**, **Process**, **Data Store**, **Interface**, and **Trust Boundary** DFD elements. The newly introduced "Interface" node is designed to represent JTAG, USB, serial, and other physical or debug interfaces under the Hardware asset category.
- **Smart Endpoint Reconnection**: Drag and drop link endpoints directly to reconnect source/target nodes, supporting flow direction and protocol property edits.
- **Adaptive Layout & Internal Scrolling**: Optimizes the workbench layout structure. The DFD cards and Extracted Assets Table share the viewport height proportionally (using flex layout), supporting independent internal scrolling to solve layout overflows when multiple diagrams (15+) are present.
- **Collaboration & Optimistic Locking**: Provides optimistic locking version control to prevent users from overwriting each other's work during collaborative edits.

### 3. AI-Powered Auto-Drawing Assistant
- **Bidirectional Conversational Modeling**: Built-in conversational AI assistant where engineers can chat (supporting multiline inputs) to describe security topologies.
- **One-Click Clear & Regenerate**: Click "One-Click DFD Generation" to clear the canvas and automatically render the AI-modeled topology, allowing engineers to continue manual adjustments.

### 4. Project Archive & Decoupling
- **Automatic Progress Derivation**: Real-time project progress derivation (Draft / In Progress / Completed) based on the analysis status of sub-domain controllers.
- **Manual Freeze & Archiving**: Before archiving, the workspace remains fully editable (supporting adding sub-domains and running TARA analysis). Once the administrator manually clicks "Archive Lock", the project is frozen into a read-only state.

### 5. High-Performance Compliance Report Export
- **Excel (.xlsx) Export**: Generates a standard 36-column flat ISO 21434 TARA matrix report, featuring a clean color palette and auto-fit columns.
- **CSV Export**: Exports the identical 36-column matrix encoded in `utf-8-sig` (with BOM) to resolve Chinese character encoding issues when opened in Windows Excel.

---

## 🛠 Technology Stack

- **Backend**:
  - **Core Framework**: Python 3.10+ / FastAPI
  - **ORM & Database**: SQLAlchemy / SQLite (`tara_local.db` by default)
  - **Task Queue**: Celery / Redis (for concurrent asynchronous multi-branch TARA analysis)
  - **LLM API Integration**: Compatible with OpenAI-standard schemas (e.g., DeepSeek-v4-flash)
- **Frontend**:
  - **Core Framework**: React 19 / Vite
  - **Canvas Engine**: ReactFlow v11
  - **State Management**: Zustand
  - **Styles**: Vanilla CSS (glassmorphism/neumorphism aesthetics supporting Dark Mode) / TailwindCSS compatible

---

## 🚀 Quick Start Guide

### Option 1: Docker Compose (Recommended)
Spin up the entire environment (API backend, Web frontend, Redis, and Celery Worker runner) in one command:
```bash
docker-compose up -d --build
```
- **FastAPI Backend API**: `http://localhost:8000`
- **React Web Application**: `http://localhost:3000`

---

### Option 2: Local Development Setup

#### 1. Start Prerequisite Services (Redis)
Ensure that Redis is running locally on port `6379`.

#### 2. Backend Startup
1. Navigate to the `backend` folder and initialize a Python virtual environment:
   ```bash
   cd backend
   python -m venv venv
   source venv/bin/activate  # On Windows, use venv\Scripts\activate
   ```
2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Initialize the database and create a default administrator user (required for the first run):
   ```bash
   ./venv/bin/python manage.py init-db
   ./venv/bin/python manage.py create-admin --user admin --pwd Admin123
   ```
4. Start the FastAPI API server with hot reloading enabled:
   ```bash
   ./venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
   ```
5. Launch the Celery Worker task runner:
   ```bash
   ./venv/bin/celery -A app.core.celery_app worker --loglevel=info
   ```

#### 3. Frontend Startup
1. Navigate to the `frontend` folder:
   ```bash
   cd ../frontend
   ```
2. Install npm packages:
   ```bash
   npm install
   ```
3. Run the Vite development server:
   ```bash
   npm run dev -- --host 0.0.0.0 --port 3000
   ```

---

## 🧪 Automated Testing

To ensure code stability, a complete suite of unit and integration tests is provided.
Run the following command under the `/backend` folder:
```bash
PYTHONPATH=. ./venv/bin/python -m unittest test_endpoints.py
```
- The test suite uses an isolated `tara_test.db` SQLite database, which is automatically deleted after runs, ensuring zero impact on your production database.

---

## 📁 Project Directory Structure

```text
tara-ai-platform/
├── backend/                   # Backend project
│   ├── app/                   # FastAPI application logic
│   │   ├── api/               # API Router Layer (project, diagram, tara, report)
│   │   ├── core/              # Global Configurations, Database, Celery
│   │   ├── models/            # SQLAlchemy Database Models
│   │   ├── schemas/           # Pydantic Schemas
│   │   └── worker/            # Celery Asynchronous Tasks
│   ├── Dockerfile
│   ├── requirements.txt
│   └── test_endpoints.py      # Integration and Unit Tests
├── frontend/                  # Frontend project
│   ├── src/
│   │   ├── components/        # React Components (DfdEditor, Workbench, TaraResults)
│   │   ├── stores/            # Zustand Stores (canvasStore, projectStore)
│   │   └── App.jsx
│   ├── package.json
│   └── vite.config.js
├── docker-compose.yml         # Container Orchestration
├── tara_local.db              # SQLite Production Database
└── .gitignore                 # Git ignore patterns
```

---

## ⚠️ Security & Development Notes
1. **API Keys Storage Security**: The LLM API Key is configured by the administrator on the "System Settings" page and stored in the database securely. Do not hardcode API Keys anywhere in the codebase.
2. **Asynchronous Process Updates**: After modifying celery tasks inside `tasks.py`, **you must restart the Celery Worker process** to apply changes. A global `task_failure` listener is integrated to prevent freezes caused by parameter matching issues.
