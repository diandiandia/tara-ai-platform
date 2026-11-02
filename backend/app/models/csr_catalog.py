from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship

from app.core.database import Base


class CsrCatalogVersion(Base):
    """Imported CSR catalog version (one row per distinct xlsx content hash)."""

    __tablename__ = "csr_catalog_versions"

    id = Column(Integer, primary_key=True, index=True)
    version = Column(String(80), nullable=False, unique=True, index=True)
    source_file = Column(String(500), nullable=True)
    sheet_name = Column(String(200), nullable=True)
    row_count = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=False, index=True)
    created_at = Column(DateTime, server_default=func.now())

    entries = relationship(
        "CsrCatalogEntry",
        back_populates="catalog_version",
        cascade="all, delete-orphan",
    )


class CsrCatalogEntry(Base):
    """A fixed target CSR from the customer/company CSR catalog."""

    __tablename__ = "csr_catalog"
    __table_args__ = (
        UniqueConstraint("version_id", "csr_id", name="uq_csr_catalog_version_csr"),
    )

    id = Column(Integer, primary_key=True, index=True)
    version_id = Column(
        Integer,
        ForeignKey("csr_catalog_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    csr_id = Column(String(100), nullable=False, index=True)
    module = Column(String(200), nullable=False)
    name = Column(String(500), nullable=False)
    description = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    catalog_version = relationship("CsrCatalogVersion", back_populates="entries")


class TaraCsrMapping(Base):
    """Many-to-many mapping between TARA requirements and fixed catalog CSRs.

    A TARA requirement may map to zero (gap/error), one, or many target CSRs.
    One target CSR may be referenced by many TARA requirements.  Gap and error
    rows use an empty ``target_csr_id`` so a missing match is visible rather
    than silently dropped.
    """

    __tablename__ = "tara_csr_mapping"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "asset_id",
            "tara_req_id",
            "target_csr_id",
            name="uq_tara_csr_mapping_source_target",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    domain_id = Column(Integer, ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(Integer, ForeignKey("tara_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    asset_id = Column(Integer, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True)
    asset_name = Column(String(300), nullable=True)

    tara_req_id = Column(String(120), nullable=False, index=True)
    source_tara_ids = Column(Text, nullable=True)  # semicolon-separated original IDs
    tara_text = Column(Text, nullable=True)

    catalog_version = Column(String(80), nullable=False, index=True)
    target_csr_id = Column(String(100), nullable=False, default="", index=True)
    match_type = Column(String(20), nullable=False, default="gap")  # full/partial/gap/error
    confidence = Column(Float, nullable=False, default=0.0)
    rationale = Column(Text, nullable=True)
    missing_aspect = Column(Text, nullable=True)
    needs_review = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, server_default=func.now())
