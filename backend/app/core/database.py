import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

# 数据库连接 URL（默认为 SQLite，生产环境可配置为 PostgreSQL）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(PROJECT_ROOT, 'tara_local.db')}")

IS_SQLITE = DATABASE_URL.startswith("sqlite")

# 创建数据库引擎
# 对于 SQLite，需要 check_same_thread=False
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 15} if IS_SQLITE else {}
)

if IS_SQLITE:
    # WAL 让读事务不再阻塞写事务的提交。默认的 delete 模式下，只要还有一个读事务
    # 未结束，写者就拿不到排他锁，画布保存会被卡住直到超时报 database is locked。
    # busy_timeout 是连接级设置，必须每条新连接都重新下发。
    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

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