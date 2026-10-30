"""CSR Pipeline V2 — Phase 1.1 哈希去重

纯程序，确定性可复现。在所有 LLM 调用之前执行。
消除完全重复和高度相似的条目，降低 LLM 调用量。
"""
from __future__ import annotations
import hashlib
from .models import PipelineRecord, PipelineConfig
from .rule_validator import normalize_text, edit_distance_similarity


def hash_dedup(records: list[PipelineRecord], config: PipelineConfig) -> list[PipelineRecord]:
    """
    两级去重：
    1. 精确哈希去重：规范化文本后 MD5 相同 → 自动合并
    2. 近精确去重：编辑距离 > threshold 但哈希不同 → 标记 near_duplicate，不在此步合并
    """
    if len(records) <= 1:
        return records

    # Phase 1: 精确哈希去重
    hash_map: dict[str, PipelineRecord] = {}
    exact_removed = 0

    for rec in records:
        norm_text = normalize_text(rec.requirement_text)
        h = hashlib.md5(norm_text.encode("utf-8")).hexdigest()

        if h in hash_map:
            # 合并到已有的代表记录
            existing = hash_map[h]
            existing.source_ids = list(set(existing.source_ids + rec.source_ids))
            existing.merge_source_ids = list(set(existing.merge_source_ids + [rec.record_id] + rec.merge_source_ids))
            existing.audit_log.append({
                "action": "hash_dedup_exact",
                "merged_record_id": rec.record_id,
                "reason": f"精确哈希匹配 ({h[:8]})",
            })
            exact_removed += 1
        else:
            hash_map[h] = rec

    result = list(hash_map.values())

    # Phase 2: 近精确去重（标记，不合并）
    near_dup_count = 0
    threshold = config.fuzzy_dedup_threshold

    if len(result) > 1:
        for i in range(len(result)):
            for j in range(i + 1, len(result)):
                if result[i].near_duplicate and result[j].near_duplicate:
                    continue
                sim = edit_distance_similarity(
                    result[i].requirement_text,
                    result[j].requirement_text,
                )
                if sim >= threshold:
                    # 两条都标记
                    if not result[i].near_duplicate:
                        result[i].near_duplicate = True
                        result[i].audit_log.append({
                            "action": "near_duplicate_marked",
                            "similar_to": result[j].record_id,
                            "similarity": round(sim, 4),
                        })
                    if not result[j].near_duplicate:
                        result[j].near_duplicate = True
                        result[j].audit_log.append({
                            "action": "near_duplicate_marked",
                            "similar_to": result[i].record_id,
                            "similarity": round(sim, 4),
                        })
                    near_dup_count += 2

    print(f"[CSR-Pipeline] 哈希去重: {len(records)} → {len(result)} "
          f"(精确消除 {exact_removed}, 标记近重复 {near_dup_count})")

    return result


def compute_stats(original_count: int, result: list[PipelineRecord]) -> dict:
    """计算哈希去重统计。"""
    near_dup = sum(1 for r in result if r.near_duplicate)
    exact_saved = original_count - len(result)
    return {
        "exact_hash_dedup_saved": exact_saved,
        "near_dup_deferred": near_dup,
        "hash_dedup_after": len(result),
    }