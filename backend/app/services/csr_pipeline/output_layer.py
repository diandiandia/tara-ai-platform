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
    original_records: list[PipelineRecord] | None = None,
) -> FinalOutput:
    """输出层主函数。纯程序、确定性输出。"""
    all_input_records = original_records if original_records is not None else records

    # Step 1: 按合并结果去重并聚合溯源与资产
    merged_records = _dedup_merged(records)

    # Step 1.5: 纯程序层后置精准折叠（彻底消除规范文本完全相同或高相似的条目）
    merged_records = _post_merge_canonical_dedup(merged_records)

    # Step 1.8: 清理【主体】【要求】等结构化标签残留，输出自然规范文本
    _clean_requirement_tags(merged_records)

    # Step 2: 编号
    _assign_csr_numbers(merged_records, config)

    # Step 3: 溯源覆盖校验
    coverage_ok, missing = _verify_coverage(all_input_records, merged_records)

    # Step 4: 统计与审计
    return _build_output(all_input_records, merged_records, coverage_ok, missing, config)


def _dedup_merged(records: list[PipelineRecord]) -> list[PipelineRecord]:
    """按合并结果去重，并将组内所有成员的溯源 ID、资产等聚合到代表记录中。"""
    groups: dict[str, list[PipelineRecord]] = {}
    independent: list[PipelineRecord] = []

    for rec in records:
        if rec.isomorphic_group_id:
            key = f"iso::{rec.isomorphic_group_id}"
            groups.setdefault(key, []).append(rec)
        elif rec.merge_stage == "COMPLEMENT" and rec.complement_pair_id:
            key = f"comp::{rec.complement_pair_id}"
            groups.setdefault(key, []).append(rec)
        elif rec.equivalence_group_id:
            key = f"eq::{rec.equivalence_group_id}"
            groups.setdefault(key, []).append(rec)
        else:
            independent.append(rec)

    result: list[PipelineRecord] = []

    for key, members in groups.items():
        if not members:
            continue
        # 选择代表条目
        if key.startswith("comp::"):
            # 互补对优先选择 ACTION 角色代表
            rep = next((m for m in members if m.complement_role == "ACTION"), members[0])
        else:
            rep = members[0]

        # 聚合所有成员的 source_ids, merge_source_ids, applicable_assets 等
        all_source_ids = set(rep.source_ids)
        all_merge_source_ids = set(rep.merge_source_ids)
        all_assets = set(getattr(rep, "applicable_assets", [rep.asset_name]) or [rep.asset_name])
        all_goal_ids = set(rep.goal_ids)
        all_control_ids = set(rep.control_ids)
        all_claim_ids = set(rep.claim_ids)
        all_dependencies = set(rep.dependencies)

        for m in members:
            if m.record_id != rep.record_id:
                all_merge_source_ids.add(m.record_id)
            all_source_ids.update(m.source_ids)
            all_merge_source_ids.update(m.merge_source_ids)
            all_assets.update(getattr(m, "applicable_assets", [m.asset_name]) or [m.asset_name])
            all_goal_ids.update(m.goal_ids)
            all_control_ids.update(m.control_ids)
            all_claim_ids.update(m.claim_ids)
            all_dependencies.update(m.dependencies)

        rep.source_ids = sorted(list(all_source_ids))
        rep.merge_source_ids = sorted(list(all_merge_source_ids))
        rep.applicable_assets = sorted(list(all_assets))
        rep.goal_ids = sorted(list(all_goal_ids))
        rep.control_ids = sorted(list(all_control_ids))
        rep.claim_ids = sorted(list(all_claim_ids))
        rep.dependencies = sorted(list(all_dependencies))

        if rep.canonical_text:
            rep.requirement_text = rep.canonical_text

        result.append(rep)

    for rec in independent:
        if not getattr(rec, "applicable_assets", None) and rec.asset_name:
            rec.applicable_assets = [rec.asset_name]
        if rec.canonical_text:
            rec.requirement_text = rec.canonical_text
        result.append(rec)

    return result


def _post_merge_canonical_dedup(records: list[PipelineRecord]) -> list[PipelineRecord]:
    """纯程序层：彻底消除最终规范文本完全相同或高相似（>=0.92）的条目。"""
    if len(records) <= 1:
        return records

    from difflib import SequenceMatcher

    def _normalize_text(t: str) -> str:
        if not t:
            return ""
        # 剥离主体与中英文标签，提取核心纯中文字符
        no_subj = re.sub(r"^【主体?】[^【]*【要求】", "", t)
        cn = no_subj.split("/")[0].strip()
        return re.sub(r"[\s\W]+", "", cn).lower()

    collapsed: list[PipelineRecord] = []
    used = set()

    for i in range(len(records)):
        if i in used:
            continue
        rep = records[i]
        rep_norm = _normalize_text(rep.requirement_text or rep.canonical_text or "")

        all_sources = set(rep.source_ids)
        all_assets = set(getattr(rep, "applicable_assets", [rep.asset_name]) or [rep.asset_name])
        all_merge_sources = set(rep.merge_source_ids)
        all_goals = set(rep.goal_ids)
        all_controls = set(rep.control_ids)
        all_claims = set(rep.claim_ids)
        all_deps = set(rep.dependencies)

        for j in range(i + 1, len(records)):
            if j in used:
                continue
            other = records[j]
            other_norm = _normalize_text(other.requirement_text or other.canonical_text or "")

            is_same_topic = (rep.topic_label or "") == (other.topic_label or "")
            is_match = False
            if rep_norm and rep_norm == other_norm:
                is_match = True
            elif is_same_topic and len(rep_norm) >= 12 and len(other_norm) >= 12:
                if SequenceMatcher(None, rep_norm, other_norm).ratio() >= 0.92:
                    is_match = True

            if is_match:
                used.add(j)
                all_sources.update(other.source_ids)
                all_assets.update(getattr(other, "applicable_assets", [other.asset_name]) or [other.asset_name])
                all_merge_sources.update(other.merge_source_ids)
                all_merge_sources.add(other.record_id)
                all_goals.update(other.goal_ids)
                all_controls.update(other.control_ids)
                all_claims.update(other.claim_ids)
                all_deps.update(other.dependencies)

        rep.source_ids = sorted(list(all_sources))
        rep.applicable_assets = sorted(list(all_assets))
        rep.merge_source_ids = sorted(list(all_merge_sources))
        rep.goal_ids = sorted(list(all_goals))
        rep.control_ids = sorted(list(all_controls))
        rep.claim_ids = sorted(list(all_claims))
        rep.dependencies = sorted(list(all_deps))
        collapsed.append(rep)

    return collapsed


def _clean_requirement_tags(records: list[PipelineRecord]):
    """清理需求描述中的【主体】【要求】[Subject][Requirement]等标签残留。"""
    for rec in records:
        text = rec.requirement_text or rec.canonical_text or ""
        if not text:
            continue
        cleaned = _strip_markup_tags(text)
        rec.requirement_text = cleaned
        rec.canonical_text = cleaned


def _strip_markup_tags(text: str) -> str:
    """去除【主体】和【要求】等标记，返回自然规范的中英文表述。"""
    if not text:
        return ""
    t = text
    t = re.sub(r"【要求】\s*", "", t)
    t = re.sub(r"\[Requirement\]\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"【主体】\s*", "", t)
    t = re.sub(r"\[Subject\]\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*[；;]\s*(?=应|shall|the|it|system|系统)", "，", t, flags=re.IGNORECASE)
    t = re.sub(r"^\s*[，,；;。]\s*", "", t)
    return t.strip()


def _assign_csr_numbers(
    records: list[PipelineRecord],
    config: PipelineConfig,
):
    """按功能分类前缀 + 序号分配编号。"""
    prefix_map = config.csr_number_prefix_map or config.numbering_prefix_map or {}
    counters: dict[str, int] = {}

    for rec in records:
        topic = rec.topic_label or "4.2 系统加固"

        # 从 topic_label 提取序号前缀，如 "4.4 安全调试" → "4.4"
        match = re.match(r"(\d+\.\d+)", topic)
        key = match.group(1) if match else topic

        prefix = prefix_map.get(topic, prefix_map.get(key, "CSR-HARDEN"))
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