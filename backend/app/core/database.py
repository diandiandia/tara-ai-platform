import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# 数据库连接 URL（默认为 SQLite，生产环境可配置为 PostgreSQL）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(PROJECT_ROOT, 'tara_local.db')}")

# 创建数据库引擎
# 对于 SQLite，需要 check_same_thread=False
engine = create_engine(
    DATABASE_URL, 
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
)

# 创建 Session 局部工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 声明式基类
Base = declarative_base()

# FastAPI 数据库连接依赖
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
