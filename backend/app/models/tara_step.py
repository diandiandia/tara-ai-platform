from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, JSON, func, Index
from sqlalchemy.orm import relationship
from app.core.database import Base

class TaraStep(Base):
    __tablename__ = "tara_steps"
    __table_args__ = (
        # 按 run_id + asset_id + stage 取步骤是最热的查询路径，缺失索引时
        # 每次都要扫全表（该表存放了全库 98% 的字节量）。
        Index("ix_tara_steps_run_asset_stage", "run_id", "asset_id", "stage"),
        Index("ix_tara_steps_asset_id", "asset_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("tara_runs.id", ondelete="CASCADE"), nullable=False)
    asset_id = Column(Integer, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    stage = Column(String(10), nullable=False) # stage1, stage2, stage3, stage4, stage5
    status = Column(String(20), default="pending", nullable=False) # pending, running, completed, failed
    input_hash = Column(String(64), nullable=False) # 用于增量分析与断点续跑 (BR-45)
    
    # 结构化分析结果结论
    # 格式: {"ai_output": {}, "is_human_modified": bool, "modification_reason": str, "final_output": {}}
    analysis_result = Column(JSON, nullable=False, default=dict)
    
    fail_reason = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0, nullable=False)
    started_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    run = relationship("TaraRun", back_populates="steps")
    asset = relationship("Asset", back_populates="steps")