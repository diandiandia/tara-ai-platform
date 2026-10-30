"""CSR Pipeline V2 — Phase 4 输出层

纯程序层：
1. 溯源覆盖校验：确保所有 TARA 需求 ID 都被输出中的至少一条覆盖
2. 编号：按功能分类前缀 + 序号生成规范编号
3. 存疑审计汇总
4. 生成 FinalOutput
"""
from __future__ import annotations
import re
from .models import PipelineRecord, FinalOutput, PipelineConfig


def finalize_output(
    records: list[PipelineRecord],
    config: PipelineConfig,
) -> FinalOutput:
    """输出层主函数。纯程序、确定性输出。"""

    # Step 1: 按合并结果去重
    merged_records = _dedup_merged(records)

    # Step 2: 编号
    _assign_csr_numbers(merged_records, config)

    # Step 3: 溯源覆盖校验
    coverage_ok, missing = _verify_coverage(records, merged_records)

    # Step 4: 统计与审计
    return _build_output(records, merged_records, coverage_ok, missing, config)


def _dedup_merged(records: list[PipelineRecord]) -> list[PipelineRecord]:
    """按合并结果去重，保留代表记录。"""
    seen_groups: set[str] = set()
    result = []

    for rec in records:
        # 等价合并类：只保留第一个代表
        if rec.equivalence_group_id:
            if rec.equivalence_group_id not in seen_groups:
                seen_groups.add(rec.equivalence_group_id)
                result.append(rec)
            continue

        # 互补对：ACTION 角色代表
        if rec.merge_stage == "COMPLEMENT":
            pid = rec.complement_pair_id
            if pid and pid not in seen_groups:
                seen_groups.add(pid)
                result.append(rec)
            continue

        # 同构归组：只保留第一个代表
        if rec.isomorphic_group_id:
            if rec.isomorphic_group_id not in seen_groups:
                seen_groups.add(rec.isomorphic_group_id)
                result.append(rec)
            continue

        # 独立条目
        result.append(rec)

    return result


def _assign_csr_numbers(
    records: list[PipelineRecord],
    config: PipelineConfig,
):
    """按功能分类前缀 + 序号分配编号。"""
    prefix_map = config.csr_number_prefix_map or {}
    counters: dict[str, int] = {}

    for rec in records:
        topic = rec.topic_label or "未分类"

        # 从 topic_label 提取序号前缀，如 "4.4 安全调试" → "4.4"
        match = re.match(r"(\d+\.\d+)", topic)
        key = match.group(1) if match else topic

        prefix = prefix_map.get(topic, prefix_map.get(key, "CSR"))
        counters.setdefault(prefix, 0)
        counters[prefix] += 1

        rec.csr_number = f"{prefix}-{counters[prefix]:03d}"


def _verify_coverage(
    all_records: list[PipelineRecord],
    output_records: list[PipelineRecord],
) -> tuple[bool, list[str]]:
    """溯源覆盖校验：所有原始 TARA 需求 ID 是否都被至少一条输出覆盖。"""
    all_source_ids: set[str] = set()
    for rec in all_records:
        all_source_ids.update(rec.source_ids)

    covered_ids: set[str] = set()
    for rec in output_records:
        covered_ids.update(rec.source_ids)
        covered_ids.update(rec.merge_source_ids)

    missing = all_source_ids - covered_ids
    return len(missing) == 0, list(missing)


def _build_output(
    all_records: list[PipelineRecord],
    output_records: list[PipelineRecord],
    coverage_ok: bool,
    missing_ids: list[str],
    config: PipelineConfig,
) -> FinalOutput:
    """构建最终输出。"""

    # 收集所有存疑记录
    uncertain_records = [rec for rec in all_records if rec.is_uncertain]
    validation_failed = [rec for rec in all_records if rec.is_validation_failed]

    # 统计
    stats = {
        "total_input": len(all_records),
        "total_output": len(output_records),
        "dedup_rate": round(1 - len(output_records) / max(len(all_records), 1), 4),
        "uncertain_count": len(uncertain_records),
        "validation_failed_count": len(validation_failed),
        "coverage_ok": coverage_ok,
        "missing_source_ids": missing_ids,
        "csr_numbers_assigned": sum(1 for rec in output_records if rec.csr_number),
    }

    # 按编号排序
    output_records.sort(key=lambda r: r.csr_number or "ZZZ")

    return FinalOutput(
        records=output_records,
        all_records=all_records,
        stats=stats,
        uncertain_records=uncertain_records,
        validation_failed_records=validation_failed,
        coverage_ok=coverage_ok,
        missing_source_ids=missing_ids,
    )