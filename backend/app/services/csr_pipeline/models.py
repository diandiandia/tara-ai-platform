"""CSR Pipeline V2 — 通用数据模型

所有数据结构只依赖 RequirementSchema / SummarizedRequirementSchema 的必填字段，
不依赖项目特有的【主体】格式、17 类分类、特定资产命名。
"""
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Atom — 原子化后的最小单元
# ---------------------------------------------------------------------------

@dataclass
class Atom:
    atom_id: str
    text: str
    action_verb: str = ""
    object_phrase: str = ""
    constraint_phrase: str = ""
    parent_record_id: str = ""


# ---------------------------------------------------------------------------
# SelfCheckResult — LLM 自检结果
# ---------------------------------------------------------------------------

@dataclass
class SelfCheckResult:
    agreed: bool
    confidence: str = "MEDIUM"  # HIGH / MEDIUM / LOW
    first_result: dict | None = None
    second_result: dict | None = None
    discrepancies: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PipelineRecord — 管道中流转的唯一数据载体
# ---------------------------------------------------------------------------

@dataclass
class PipelineRecord:
    # ===== Schema 必有字段（通用，来自 RequirementSchema） =====
    record_id: str
    requirement_text: str
    source_ids: list[str]                # 子条目级 ID 集合（只增不删）
    asset_name: str
    requirement_scope: str = ""
    requirement_type: str = "product"
    owner: str = ""
    allocated_to: str = ""
    verification_method: str = ""
    acceptance_criteria: str = ""
    rationale: str = ""
    dependencies: list[str] = field(default_factory=list)
    goal_ids: list[str] = field(default_factory=list)
    control_ids: list[str] = field(default_factory=list)
    claim_ids: list[str] = field(default_factory=list)
    applicable_assets: list[str] = field(default_factory=list)

    # ===== 管道动态抽取字段 =====
    action_verbs: list[str] = field(default_factory=list)
    object_phrases: list[str] = field(default_factory=list)
    constraint_phrases: list[str] = field(default_factory=list)
    boundary_class: str = ""
    topic_label: str = ""
    topic_uncertain: bool = False
    near_duplicate: bool = False         # 编辑距离 > 0.95，推迟到回原文裁决
    atoms: list[Atom] = field(default_factory=list)

    # ===== 合并状态 =====
    merge_stage: str = "NONE"            # NONE | EQUIVALENCE | COMPLEMENT | ISOMORPHIC
    merge_source_ids: list[str] = field(default_factory=list)  # 合并来源的 record_id
    equivalence_group_id: str = ""       # 所属等价类组 ID
    complement_pair_id: str = ""         # 所属互补对 ID
    complement_role: str = ""            # ACTION / FAILURE_HANDLER / ""
    isomorphic_group_id: str = ""        # 所属同构组 ID
    isomorphic_object_label: str = ""    # 同构归组中对应的宾语
    canonical_text: str = ""             # 规范化后的表述
    canonical_origin: str = ""           # LLM / FALLBACK_ORIGINAL
    csr_number: str = ""                 # 最终编号，如 CSR-DBG-001

    # ===== 质量标记 =====
    is_uncertain: bool = False
    is_validation_failed: bool = False
    self_check_info: dict = field(default_factory=dict)
    audit_log: list[dict] = field(default_factory=list)
    stage_failures: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.applicable_assets and self.asset_name:
            self.applicable_assets = [self.asset_name]

    def mark_merged(self, stage: str, source_record_ids: list[str]):
        self.merge_stage = stage
        self.merge_source_ids = list(set(self.merge_source_ids + source_record_ids))

    def mark_uncertain(self, reason: str, stage: str = ""):
        self.is_uncertain = True
        self.audit_log.append({"action": "mark_uncertain", "stage": stage, "reason": reason})

    def mark_validation_failed(self, errors: list[str]):
        self.is_validation_failed = True
        self.audit_log.append({"action": "validation_failed", "errors": errors})

    def add_audit_entry(self, entry: dict):
        self.audit_log.append(entry)


# ---------------------------------------------------------------------------
# FinalOutput — 管道最终输出
# ---------------------------------------------------------------------------

@dataclass
class FinalOutput:
    records: list             = field(default_factory=list)  # list[PipelineRecord] 去重后的输出记录
    all_records: list         = field(default_factory=list)  # list[PipelineRecord] 全部记录
    stats: dict               = field(default_factory=dict)
    uncertain_records: list   = field(default_factory=list)  # list[PipelineRecord] 存疑记录
    validation_failed_records: list = field(default_factory=list)
    coverage_ok: bool         = True
    missing_source_ids: list  = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "records_count": len(self.records),
            "all_records_count": len(self.all_records),
            "stats": self.stats,
            "uncertain_count": len(self.uncertain_records),
            "validation_failed_count": len(self.validation_failed_records),
            "coverage_ok": self.coverage_ok,
            "missing_source_ids": self.missing_source_ids,
        }


# ---------------------------------------------------------------------------
# PipelineContext — 管道全局上下文
# ---------------------------------------------------------------------------

@dataclass
class PipelineContext:
    db: Any = None
    domain: Any = None
    steps: list = field(default_factory=list)
    assets: list = field(default_factory=list)
    settings: dict | None = None
    req_map: dict = field(default_factory=dict)
    config: Any = None                   # PipelineConfig
    llm_proxy: Any = None                # LLMProxy
    all_source_ids: set = field(default_factory=set)
    log_dir: str = ""
    cleanup_log: list[dict] = field(default_factory=list)
    uncertainty_list: list[dict] = field(default_factory=list)
    pipeline_version: str = "v2.0"

    def add_uncertainty(self, entry: dict):
        self.uncertainty_list.append(entry)


# ---------------------------------------------------------------------------
# PipelineConfig — 可配置层（在 config.py 中实例化）
# ---------------------------------------------------------------------------

@dataclass
class PipelineConfig:
    # ===== 哈希去重 =====
    fuzzy_dedup_threshold: float = 0.95

    # ===== 安全边界映射 =====
    boundary_patterns: dict = field(default_factory=dict)  # {"类名": ["关键词1", "关键词2"]}
    fallback_to_llm_boundary: bool = True

    # ===== 主题分类 =====
    topic_labels: list[str] = field(default_factory=list)

    # ===== 分块参数 =====
    chunk_size: int = 25
    chunk_overlap: int = 12
    global_group_batch_size: int = 100
    classify_batch_size: int = 35

    # ===== 规则引擎 =====
    action_verb_dict: list[str] = field(default_factory=list)
    complement_patterns: list[tuple] = field(default_factory=list)  # [(动作词集, 失败处理词集)]
    synonym_table: dict = field(default_factory=dict)  # {"预定义策略": ["按策略", "根据策略"]}

    # ===== 分类编号（可插拔） =====
    classify_fn: Callable | None = None
    numbering_prefix_map: dict = field(default_factory=dict)
    csr_number_prefix_map: dict = field(default_factory=dict)  # 如 {"4.4 安全调试": "CSR-DBG"}

    # ===== 降级 =====
    max_uncertainty_rate: float = 0.15
    max_retries: int = 3
    temperature: float = 0.0