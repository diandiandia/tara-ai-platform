from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import relationship
from app.core.database import Base

class Asset(Base):
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True, index=True)
    domain_id = Column(Integer, ForeignKey("domains.id", ondelete="CASCADE"), nullable=False)
    diagram_id = Column(Integer, ForeignKey("diagrams.id", ondelete="CASCADE"), nullable=True) # 来源 DFD
    name = Column(String(100), nullable=False)
    asset_type = Column(String(50), nullable=False) # data, software, hardware, communication
    protocol = Column(String(50), nullable=True)
    description = Column(Text, nullable=True)
    status = Column(String(20), default="draft", nullable=False) # draft (待核对), confirmed (已确认), rejected (已拒绝)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    domain = relationship("Domain", back_populates="assets")
    diagram = relationship("Diagram", back_populates="assets")
    
    # 级联删除关联的 TARA 分析步骤
    steps = relationship("TaraStep", back_populates="asset", cascade="all, delete-orphan")
