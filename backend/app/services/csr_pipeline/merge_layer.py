"""CSR Pipeline V2 — Phase 3 合并层

优先级固定：等价合并 → 互补合并 → 规范化 → 同构归组。
每步输出是下一步的输入，每步只操作上一步未合并的条目。
"""
from __future__ import annotations
from .models import PipelineRecord, PipelineConfig
from .llm_proxy import LLMProxy
from .rule_validator import CanonicalValidator, IsomorphicValidator


# ============================================================================
# Prompt 模板
# ============================================================================

COMPLEMENT_CHECK_SYSTEM = """你是网络安全需求分析专家。判断以下需求对是否为互补关系：
一条是「动作」要求（应校验/验证/认证/检测 X），另一条是「失败处理」要求（应拒绝/丢弃/阻止未通过校验的 X）。

互补对特征：
1. 两者作用于同一安全目标（同一主体、同一对象）
2. 一条描述正常的安全动作，另一条描述失败/异常时的处理方式
3. 例如："应认证主机身份" + "应拒绝未通过认证的会话" → 互补对

输出格式：
{
  "complement_pairs": [
    {
      "action_id": "ASSET-1::CSR-001",
      "failure_handler_id": "ASSET-1::CSR-002",
      "pair_description": "认证主机身份 + 拒绝未认证会话"
    }
  ]
}
如果没有任何互补对，输出空数组。"""

COMPLEMENT_SELF_CHECK = """你是审核员。检查以下互补对判定：

- 是否有不该配对的条目被错误配对（动作和失败处理不是同一安全目标）？
- 是否有应该配对但被遗漏的互补对？

输出格式：
{
  "has_errors": true/false,
  "all_correct": true/false,
  "corrections": ["错误描述"]
}"""


CANONICALIZE_SYSTEM = """你是网络安全需求编写专家。请根据多条语义等价的原始需求，生成一条规范化的标准表述。

要求：
1. 保留所有技术细节的并集（不能遗漏任何一条原文的技术要求）
2. 不弱化要求强度（"应/必须/shall"不能变成"宜/可/should"）
3. 保留所有数字阈值
4. 保留所有限定条件（接口类型、生命周期阶段、触发条件）
5. 保留所有否定语义（"不得/禁止/拒绝"必须保留）
6. 中英文双语输出，格式：中文 / English
7. 语言简洁，但不能牺牲完整性

输出格式：
{
  "canonical_text": "应在固件写入前验证其数字签名的完整性和真实性，拒绝未通过校验的写入 / Shall verify the integrity and authenticity of the digital signature before firmware writes, and reject writes that fail verification."
}"""

CANONICALIZE_SELF_CHECK = """你是审核员。检查以下规范表述是否：
1. 弱化了原文的要求强度（情态词从"应"降为"可"）
2. 丢失了原文的数字、否定语义、限定条件
3. 遗漏了某个原文中的关键技术要求

输出格式：
{
  "has_errors": true/false,
  "all_correct": true/false,
  "corrections": ["错误描述"]
}"""


ISOMORPHIC_SYSTEM = """你是网络安全需求分析专家。找出以下独立需求中动作相同、约束相同、宾语不同且可枚举的条目，归组为一条封闭枚举式表述。

判定标准：
1. 动作语义相同（如都是"关闭"、"验证"、"记录"）
2. 约束条件相同（限定条件、情态强度、例外条件一致）
3. 宾语是具体可枚举对象，归组后能列全
4. 宾语属于同一上位概念（如都是调试接口类型、都是传感器数据类型）

归组规则：
- "无条件关闭"与"关闭但保留受控维护启用"不能归组（例外条件不同）
- 抽象类别宾语不与具体宾语归组（如"关闭所有调试接口"不与"关闭 JTAG"归组）
- 不能用"等""此类"开放词

输出格式：
{
  "isomorphic_groups": [
    {
      "group_description": "关闭调试接口",
      "member_ids": ["ASSET-1::CSR-001", "ASSET-1::CSR-002"],
      "object_labels": {"ASSET-1::CSR-001": "JTAG", "ASSET-1::CSR-002": "SWD"},
      "enumerated_text": "应关闭 JTAG、SWD 调试接口 / Shall disable JTAG and SWD debug interfaces"
    }
  ]
}"""

ISOMORPHIC_SELF_CHECK = """你是审核员。检查以下同构归组结果：

1. 宾语是否列全（有无遗漏组内某条的宾语）
2. 约束条件是否一致（有无弱化或强化）
3. 是否有不适合归组的条目被错误归入
4. 例外条件不同的条目是否被正确拆分

输出格式：
{
  "has_errors": true/false,
  "all_correct": true/false,
  "corrections": ["错误描述"]
}"""


# ============================================================================
# 主函数
# ============================================================================

def merge_all(
    records: list[PipelineRecord],
    proxy: LLMProxy,
    config: PipelineConfig,
) -> list[PipelineRecord]:
    """合并层主函数。四步串行：互补 → 规范化 → 同构。"""

    # Step 1: 互补合并识别
    records = _identify_complements(records, proxy, config)

    # Step 2: 规范化表述 + 程序校验
    records = _canonicalize(records, proxy, config)

    # Step 3: 同构归组 + 程序校验
    records = _group_isomorphic(records, proxy, config)

    return records


# ============================================================================
# Step 1: 互补合并
# ============================================================================

def _identify_complements(
    records: list[PipelineRecord],
    proxy: LLMProxy,
    config: PipelineConfig,
) -> list[PipelineRecord]:
    """互补合并识别：规则匹配 + LLM 确认。"""
    if len(records) < 2:
        return records

    candidates = _get_independent_or_reps(records)
    candidate_pairs = _match_complement_candidates(candidates, config)

    if not candidate_pairs:
        return records

    record_map = {rec.record_id: rec for rec in records}
    pair_counter = 0

    # 分批发给 LLM 确认（每批 <= 25 对）
    for batch_idx in range(0, len(candidate_pairs), 25):
        batch = candidate_pairs[batch_idx:batch_idx + 25]
        items = []
        for act_id, fail_id in batch:
            act_rec = _find_record(candidates, act_id)
            fail_rec = _find_record(candidates, fail_id)
            if act_rec and fail_rec:
                items.append({
                    "action_id": act_id,
                    "action_text": act_rec.requirement_text,
                    "failure_handler_id": fail_id,
                    "failure_handler_text": fail_rec.requirement_text,
                })

        if not items:
            continue

        sc = proxy.judge_with_self_check(
            stage="complement-merge",
            system_prompt=COMPLEMENT_CHECK_SYSTEM,
            check_prompt=COMPLEMENT_SELF_CHECK,
            user_payload={"candidate_pairs": items},
            input_ids=[i["action_id"] for i in items] + [i["failure_handler_id"] for i in items],
            input_texts_abstract=[f"{i['action_text'][:100]} + {i['failure_handler_text'][:100]}" for i in items],
        )

        if not sc.agreed:
            continue

        decisions = sc.first_result if sc.first_result else {}
        pairs = decisions.get("complement_pairs", []) or []

        for pair in pairs:
            act_id = pair.get("action_id", "")
            fail_id = pair.get("failure_handler_id", "")
            pair_counter += 1
            pair_id = f"COMP-{pair_counter}"

            for rid in [act_id, fail_id]:
                rec = record_map.get(rid)
                if rec:
                    rec.complement_pair_id = pair_id
                    rec.merge_stage = "COMPLEMENT"
                    if rid == act_id:
                        rec.complement_role = "ACTION"
                    else:
                        rec.complement_role = "FAILURE_HANDLER"
                    rec.audit_log.append({
                        "action": "complement_paired",
                        "pair_id": pair_id,
                        "pair_description": pair.get("pair_description", ""),
                    })

    return records


def _get_independent_or_reps(records: list[PipelineRecord]) -> list[PipelineRecord]:
    """获取独立条目或等价类代表。"""
    result = []
    seen_groups: set[str] = set()
    for rec in records:
        if rec.merge_stage in ("COMPLEMENT", "ISOMORPHIC"):
            continue
        gid = rec.equivalence_group_id
        if gid:
            if gid not in seen_groups:
                seen_groups.add(gid)
                result.append(rec)
        else:
            result.append(rec)
    return result


def _match_complement_candidates(
    records: list[PipelineRecord],
    config: PipelineConfig,
) -> list[tuple[str, str]]:
    """规则引擎匹配互补候选对。"""
    patterns = config.complement_patterns or []
    pairs = []

    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            # 互补对必须属于同一资产且同一功能主题
            if records[i].asset_name != records[j].asset_name:
                continue
            if records[i].topic_label and records[j].topic_label and records[i].topic_label != records[j].topic_label:
                continue

            ti = records[i].requirement_text.lower()
            tj = records[j].requirement_text.lower()
            for action_verbs, failure_verbs in patterns:
                i_is_action = any(v.lower() in ti for v in action_verbs)
                j_is_failure = any(v.lower() in tj for v in failure_verbs)
                j_is_action = any(v.lower() in tj for v in action_verbs)
                i_is_failure = any(v.lower() in ti for v in failure_verbs)

                if i_is_action and j_is_failure:
                    pairs.append((records[i].record_id, records[j].record_id))
                    break
                elif j_is_action and i_is_failure:
                    pairs.append((records[j].record_id, records[i].record_id))
                    break
    return pairs


def _find_record(records: list[PipelineRecord], record_id: str) -> PipelineRecord | None:
    for rec in records:
        if rec.record_id == record_id:
            return rec
    return None


# ============================================================================
# Step 2: 规范化表述 + 程序校验
# ============================================================================

def _canonicalize(
    records: list[PipelineRecord],
    proxy: LLMProxy,
    config: PipelineConfig,
) -> list[PipelineRecord]:
    """规范化：等价类和互补对生成 canonical text，程序校验。"""
    validator = CanonicalValidator(config)

    # 等价类
    eq_groups: dict[str, list[PipelineRecord]] = {}
    for rec in records:
        if rec.merge_stage == "NONE" and rec.equivalence_group_id:
            eq_groups.setdefault(rec.equivalence_group_id, []).append(rec)

    for gid, members in eq_groups.items():
        if len(members) >= 2:
            _canonicalize_group(members, proxy, validator, "等价合并", gid)

    # 互补对
    comp_pairs: dict[str, list[PipelineRecord]] = {}
    for rec in records:
        if rec.merge_stage == "COMPLEMENT" and rec.complement_pair_id:
            comp_pairs.setdefault(rec.complement_pair_id, []).append(rec)

    for pid, members in comp_pairs.items():
        _canonicalize_group(members, proxy, validator, "互补合并", pid)

    return records


def _canonicalize_group(
    members: list[PipelineRecord],
    proxy: LLMProxy,
    validator: CanonicalValidator,
    merge_type: str,
    group_id: str,
):
    """对一组记录做规范化。"""
    originals = [rec.requirement_text for rec in members]
    ids = [rec.record_id for rec in members]

    sc = proxy.generate_with_self_check(
        stage=f"canonicalize-{merge_type}",
        system_prompt=CANONICALIZE_SYSTEM,
        check_prompt=CANONICALIZE_SELF_CHECK,
        user_payload={"requirements": [{"record_id": r.record_id, "text": r.requirement_text} for r in members]},
        input_ids=ids,
        input_texts_full=originals,
    )

    canonical = ""
    if sc.agreed:
        canonical = (sc.first_result or {}).get("canonical_text", "")

    # 程序规则校验
    val_result = validator.check(originals, canonical)

    if val_result.valid and canonical:
        # 成功：所有成员共享 canonical
        for rec in members:
            rec.canonical_text = canonical
            rec.canonical_origin = "LLM"
            rec.mark_merged(merge_type, [m.record_id for m in members if m.record_id != rec.record_id])
            rec.audit_log.append({"action": "canonicalized", "merge_type": merge_type, "self_check_agreed": sc.agreed})
    else:
        # 回退：用信息最全的原文
        fallback = max(originals, key=len) if originals else ""
        for rec in members:
            rec.canonical_text = fallback
            rec.canonical_origin = "FALLBACK_ORIGINAL"
            rec.is_validation_failed = True
            rec.audit_log.append({
                "action": "canonicalize_fallback",
                "merge_type": merge_type,
                "validation_errors": val_result.errors,
            })


# ============================================================================
# Step 3: 同构归组
# ============================================================================

def _group_isomorphic(
    records: list[PipelineRecord],
    proxy: LLMProxy,
    config: PipelineConfig,
) -> list[PipelineRecord]:
    """同构归组：按功能分类分块，LLM 识别 + 程序校验。"""
    iso_validator = IsomorphicValidator(config)
    independent = [
        rec for rec in records
        if rec.merge_stage == "NONE" or (rec.merge_stage in ("EQUIVALENCE", "COMPLEMENT") and rec.canonical_text)
    ]

    if len(independent) < 2:
        return records

    groups_by_topic: dict[str, list[PipelineRecord]] = {}
    for rec in independent:
        topic = rec.topic_label or "未分类"
        groups_by_topic.setdefault(topic, []).append(rec)

    record_map = {rec.record_id: rec for rec in records}
    iso_counter = 0

    for topic, topic_records in groups_by_topic.items():
        if len(topic_records) < 2:
            continue

        for b_idx in range(0, len(topic_records), 25):
            batch = topic_records[b_idx:b_idx + 25]
            if len(batch) < 2:
                continue

            items = [{"record_id": rec.record_id, "text": rec.canonical_text or rec.requirement_text} for rec in batch]
            ids = [rec.record_id for rec in batch]

            sc = proxy.judge_with_self_check(
                stage="isomorphic-group",
                system_prompt=ISOMORPHIC_SYSTEM,
                check_prompt=ISOMORPHIC_SELF_CHECK,
                user_payload={"requirements": items, "topic": topic},
                input_ids=ids,
                input_texts_abstract=[(rec.canonical_text or rec.requirement_text)[:200] for rec in batch],
            )

            if not sc.agreed:
                continue

            decisions = sc.first_result if sc.first_result else {}
            iso_groups = decisions.get("isomorphic_groups", []) or []

            for ig in iso_groups:
                member_ids = ig.get("member_ids", []) or []
                object_labels = ig.get("object_labels", {}) or {}
                enumerated_text = ig.get("enumerated_text", "")

                if len(member_ids) < 2:
                    continue

                group_members = [record_map[mid] for mid in member_ids if mid in record_map]
                if len(group_members) < 2:
                    continue

                val_result = iso_validator.check(group_members, enumerated_text)

                iso_counter += 1
                gid = f"ISO-{iso_counter}"
                if val_result.valid and enumerated_text:
                    for rec in group_members:
                        rec.isomorphic_group_id = gid
                        rec.isomorphic_object_label = object_labels.get(rec.record_id, "")
                        rec.canonical_text = enumerated_text
                        rec.merge_stage = "ISOMORPHIC"
                        rec.audit_log.append({"action": "isomorphic_grouped", "group_id": gid})
                else:
                    for rec in group_members:
                        rec.is_validation_failed = True
                        rec.audit_log.append({
                            "action": "isomorphic_failed",
                            "group_id": gid,
                            "errors": val_result.errors,
                        })

    return records