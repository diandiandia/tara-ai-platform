from sqlalchemy import Column, Integer, String, DateTime, func
from sqlalchemy.orm import relationship
from app.core.database import Base

class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), nullable=False)
    description = Column(String(200), nullable=True)
    status = Column(String(20), default="draft", nullable=False) # draft, in_progress, completed
    is_archived = Column(Integer, default=0, nullable=False) # 0 = active, 1 = explicitly archived/locked
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # 级联删除关联的子域控 (BR-04/09)
    domains = relationship("Domain", back_populates="project", cascade="all, delete-orphan")
