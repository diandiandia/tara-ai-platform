from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, func
from sqlalchemy.orm import Session, relationship
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


def get_results_run(db: Session, domain_id: int) -> TaraRun | None:
    query = db.query(TaraRun).filter(TaraRun.domain_id == domain_id)
    finished = query.filter(
        TaraRun.status.in_(["completed", "failed", "cancelled"])
    ).order_by(TaraRun.id.desc()).first()
    return finished if finished is not None else query.order_by(TaraRun.id.desc()).first()
