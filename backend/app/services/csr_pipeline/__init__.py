"""CSR Pipeline V2 — 管道编排器

feature flag: use_v2_pipeline → 新管道 | 旧管道
核心管道通用，项目特有部分通过 config 注入。
"""
from __future__ import annotations
from .models import PipelineConfig, PipelineContext, PipelineRecord, FinalOutput
from .config import default_config, rebuild_config
from .hash_dedup import hash_dedup, compute_stats as hash_dedup_stats
from .conservative_atomize import conservative_atomize
from .boundary_mapper import map_boundary_class
from .topic_classifier import classify_topics
from .judge_layer import judge_equivalence
from .merge_layer import merge_all
from .output_layer import finalize_output
from .llm_proxy import LLMProxy, LLMSettings


def create_context(
    config: PipelineConfig | None = None,
    llm_settings: LLMSettings | None = None,
) -> PipelineContext:
    """创建管道上下文。"""
    cfg = config or default_config()
    proxy = LLMProxy(settings=llm_settings) if llm_settings else LLMProxy(settings=None)
    return PipelineContext(config=cfg, llm_proxy=proxy)


def run_pipeline(
    records: list[PipelineRecord],
    context: PipelineContext,
) -> FinalOutput:
    """完整管道执行：四阶段串行，任意阶段失败不中断。"""

    print(f"\n[CSR-Pipeline-V2] 开始执行，输入 {len(records)} 条")
    config = context.config
    proxy = context.llm_proxy
    initial_records = list(records)
    initial_count = len(records)

    # ==========================================================================
    # Phase 1: 预处理层（纯程序优先，LLM 兜底）
    # ==========================================================================
    print("[CSR-Pipeline-V2] Phase 1: 预处理层")

    # 1.1 哈希去重（纯程序，无 LLM）
    records = hash_dedup(records, config)
    stats_phase1 = hash_dedup_stats(initial_count, records)

    # 1.2 保守原子化（LLM + 程序校验）
    try:
        if proxy.settings:
            records = conservative_atomize(records, proxy, config)
    except Exception as e:
        print(f"[CSR-Pipeline-V2] WARNING: 原子化阶段异常，跳过: {e}")

    # 1.3 安全边界映射（规则优先，LLM 兜底）
    try:
        records = map_boundary_class(records, proxy, config)
    except Exception as e:
        print(f"[CSR-Pipeline-V2] WARNING: 边界映射阶段异常，跳过: {e}")

    # 1.4 功能分类（LLM + 自检）
    try:
        if proxy.settings:
            records = classify_topics(records, proxy, config)
        else:
            for rec in records:
                rec.topic_label = "未分类"
    except Exception as e:
        print(f"[CSR-Pipeline-V2] WARNING: 功能分类阶段异常，跳过: {e}")
        for rec in records:
            if not rec.topic_label:
                rec.topic_label = "未分类"

    # 统计 Phase 1
    stats_phase1["classified_count"] = sum(1 for r in records if r.topic_label != "未分类")
    stats_phase1["atoms_count"] = sum(len(r.atoms) for r in records)

    # ==========================================================================
    # Phase 2: 判定层（LLM 判定 + 自检 + 并查集 + 回原文裁决）
    # ==========================================================================
    print(f"[CSR-Pipeline-V2] Phase 2: 判定层 ({len(records)} 条)")

    try:
        if proxy.settings and len(records) > 1:
            records = judge_equivalence(records, proxy, config)
    except Exception as e:
        print(f"[CSR-Pipeline-V2] WARNING: 判定层异常，跳过: {e}")

    stats_phase2 = {
        "equivalence_groups": len(set(r.equivalence_group_id for r in records if r.equivalence_group_id)),
        "uncertain_after_judge": sum(1 for r in records if r.is_uncertain),
    }

    # ==========================================================================
    # Phase 3: 合并层（规范化 + 同构归组）
    # ==========================================================================
    print(f"[CSR-Pipeline-V2] Phase 3: 合并层")

    try:
        if proxy.settings:
            records = merge_all(records, proxy, config)
    except Exception as e:
        print(f"[CSR-Pipeline-V2] WARNING: 合并层异常，跳过: {e}")

    stats_phase3 = {
        "canonicalized": sum(1 for r in records if r.canonical_text and r.canonical_origin == "LLM"),
        "complement_pairs": len(set(r.complement_pair_id for r in records if r.complement_pair_id)),
        "isomorphic_groups": len(set(r.isomorphic_group_id for r in records if r.isomorphic_group_id)),
    }

    # ==========================================================================
    # Phase 4: 输出层（纯程序）
    # ==========================================================================
    print("[CSR-Pipeline-V2] Phase 4: 输出层")

    output = finalize_output(records, config, original_records=initial_records)

    # 汇总统计
    output.stats = {
        **output.stats,
        "phase1": stats_phase1,
        "phase2": stats_phase2,
        "phase3": stats_phase3,
    }

    print(f"[CSR-Pipeline-V2] 完成: {output.stats['total_input']} → {output.stats['total_output']} "
          f"(去重率 {output.stats['dedup_rate']:.1%})")

    return output


def run_standalone(
    records: list[PipelineRecord],
    config: PipelineConfig | None = None,
    llm_settings: LLMSettings | None = None,
) -> FinalOutput:
    """独立运行管道（不带已有上下文）。"""
    ctx = create_context(config, llm_settings)
    return run_pipeline(records, ctx)


__all__ = [
    "run_pipeline",
    "run_standalone",
    "create_context",
    "PipelineConfig",
    "PipelineContext",
    "PipelineRecord",
    "FinalOutput",
    "LLMProxy",
    "LLMSettings",
    "default_config",
    "rebuild_config",
]