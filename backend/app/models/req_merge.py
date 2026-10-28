from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, func
from sqlalchemy.orm import relationship
from app.core.database import Base


class CustomerRequirement(Base):
    """客户（OEM）整车信息安全需求导入表。"""
    __tablename__ = "customer_requirements"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    external_id = Column(String(100), nullable=False)  # OEM 需求编号，如 SC-CS-HW-Req1
    section = Column(String(200), nullable=True)       # 所属章节，如 4 整车网络安全要求
    category = Column(String(200), nullable=True)      # 需求分类，如 4.1 硬件安全
    text = Column(Text, nullable=False)                # 整车网络安全需求描述
    source = Column(String(200), nullable=True)        # 来源文档 / 客户名称
    created_at = Column(DateTime, server_default=func.now())


class MergedRequirement(Base):
    """客户需求 + 某个域控的 TARA 需求合并后的最终（统一）信息安全需求表。

    合并是「按域控」进行的：ADCU 用 ADCU 的 TARA 需求 + OEM 需求；
    TBOX 用 TBOX 的 TARA 需求 + OEM 需求，各自独立产出最终需求。
    """
    __tablename__ = "merged_requirements"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    domain_id = Column(Integer, ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True)
    final_id = Column(String(100), nullable=False)          # 最终需求编号，如 FCSR-HW-001
    security_domain = Column(String(100), nullable=True)    # 安全域
    control_category = Column(String(200), nullable=True)   # 控制分类
    requirement_title = Column(String(300), nullable=True)  # 需求标题
    requirement_text = Column(Text, nullable=True)          # 统一需求（中英双语）
    source_tara_raw_ids = Column(Text, nullable=True)       # 追溯的 TARA 原始 ID（冗余，便于导出）
    additional_info = Column(Text, nullable=True)
    status = Column(String(50), default="Draft")            # Approved / Draft
    allocated_module = Column(String(200), nullable=True)   # 分配模块
    responsible_org = Column(String(200), nullable=True)    # 责任组织
    remarks = Column(Text, nullable=True)
    conflict_flag = Column(Boolean, default=False)          # 是否存在冲突
    conflict_note = Column(Text, nullable=True)             # 冲突说明
    resolution = Column(String(50), nullable=True)          # customer_wins / merge / both / manual
    created_at = Column(DateTime, server_default=func.now())

    sources = relationship(
        "MergedRequirementSource",
        back_populates="merged",
        cascade="all, delete-orphan",
    )


class MergedRequirementSource(Base):
    """最终需求的双向追溯链：一条最终需求可挂多条来源（客户 / TARA）。"""
    __tablename__ = "merged_requirement_sources"

    id = Column(Integer, primary_key=True, index=True)
    merged_id = Column(Integer, ForeignKey("merged_requirements.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type = Column(String(20), nullable=False)  # "customer" | "tara"
    source_id = Column(String(100), nullable=False)   # SC-CS-HW-Req1 | CSR-HW-001

    merged = relationship("MergedRequirement", back_populates="sources")
