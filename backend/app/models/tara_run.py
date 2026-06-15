from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, func
from sqlalchemy.orm import relationship
from app.core.database import Base

class TaraRun(Base):
    __tablename__ = "tara_runs"

    id = Column(Integer, primary_key=True, index=True)
    domain_id = Column(Integer, ForeignKey("domains.id", ondelete="CASCADE"), nullable=False)
    celery_task_id = Column(String(255), nullable=True) # 用于中止任务 (Cancel Run)
    status = Column(String(20), default="pending", nullable=False) # pending, running, completed, failed, cancelled
    progress = Column(Integer, default=0, nullable=False)
    started_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime, nullable=True)

    domain = relationship("Domain", back_populates="runs")
    steps = relationship("TaraStep", back_populates="run", cascade="all, delete-orphan")
