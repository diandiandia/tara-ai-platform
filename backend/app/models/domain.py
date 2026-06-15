from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, func
from sqlalchemy.orm import relationship
from app.core.database import Base

class Domain(Base):
    __tablename__ = "domains"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(50), nullable=False)
    status = Column(String(20), default="not_started", nullable=False) # not_started, running, completed, failed
    progress = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    project = relationship("Project", back_populates="domains")

    # 级联删除关联的功能图、资产、分析记录 (BR-04/09)
    diagrams = relationship("Diagram", back_populates="domain", cascade="all, delete-orphan")
    assets = relationship("Asset", back_populates="domain", cascade="all, delete-orphan")
    runs = relationship("TaraRun", back_populates="domain", cascade="all, delete-orphan")
