from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api import auth, project, diagram, asset, tara, settings, report, websocket
from app.core.database import Base, engine

# 初始化数据库结构（SQLite 自动建表，安全备用）
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="TARA AI 分析平台 API",
    description="支持 AI 辅助的汽车网络安全分析平台 v3",
    version="3.0"
)

# 跨域资源共享配置 (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # 生产环境建议替换为具体的前端地址
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载 API 路由
app.include_router(auth.router, prefix="/api")
app.include_router(project.router, prefix="/api")
app.include_router(diagram.router, prefix="/api")
app.include_router(asset.router, prefix="/api")
app.include_router(tara.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
app.include_router(report.router, prefix="/api")
app.include_router(websocket.router)

@app.get("/")
def read_root():
    return {
        "name": "TARA AI Platform API",
        "version": "3.0",
        "status": "healthy"
    }

if __name__ == "__main__":
    import uvicorn
    # 本地启动测试服务器
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
