from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import relationship
from app.core.database import Base

class Diagram(Base):
    __tablename__ = "diagrams"

    id = Column(Integer, primary_key=True, index=True)
    domain_id = Column(Integer, ForeignKey("domains.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(100), nullable=False)
    version_no = Column(Integer, default=1, nullable=False) # 用于乐观锁控制 (BR-16)
    snapshot_json = Column(Text, nullable=False, default="{}") # 存储 React Flow 拓扑数据
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    domain = relationship("Domain", back_populates="diagrams")
    
    # 级联删除关联的资产 (BR-04/09, DFD删除级联清除关联资产规则)
    assets = relationship("Asset", back_populates="diagram", cascade="all, delete-orphan")
