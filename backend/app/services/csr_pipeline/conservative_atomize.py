"""CSR Pipeline V2 — Phase 1.2 保守原子化

默认不拆。只有明确属于不同安全目标的并列才拆。
同一安全目标的"动作+失败处理"不拆；同一动作的并列宾语不拆。
"""
from __future__ import annotations
from .models import PipelineRecord, Atom, PipelineConfig
from .llm_proxy import LLMProxy
from .rule_validator import ConservativeAtomizeValidator


CONSERVATIVE_ATOMIZE_SYSTEM = """你是一个网络安全需求分析专家，负责判断一条安全需求是否需要拆分成多个独立的原子条目。

规则：
1. 默认不拆。只有明确属于「不同安全目标」的并列才拆。
2. 同一安全目标的「动作 + 失败处理」不拆。
   例如："应验证签名，并拒绝未通过校验的写入" → 不拆（验证签名和拒绝失败写入是同一目标的动作和后果）。
3. 同一动作的并列宾语不拆。
   例如："应记录会话建立、关键命令和刷写操作" → 不拆（记录是同一动作，宾语不同但并列）。
4. 不同安全目标的并列才拆。
   例如："应认证主机身份，并记录所有审计日志" → 拆（认证和审计是两个不同的安全目标）。

输出格式：
{
  "should_split": true/false,
  "atoms": [
    {"index": 1, "text": "第一条原子要求文本"},
    {"index": 2, "text": "第二条原子要求文本"}
  ],
  "reason": "简短说明判断理由"
}

如果 should_split 为 false，atoms 为空数组。"""

CONSERVATIVE_ATOMIZE_SELF_CHECK = """你是需求分析审核员。请检查以下原子化拆分决策是否正确：

- 是否有不应该拆的条目被错误拆分了（动作+失败处理对、并列宾语）？
- 是否有应该拆的不同安全目标没有被拆分？
- 拆分后的原子文本是否完整保留了原文的情态、否定、数字、条件？

输入包含原始需求文本和正向拆分的决策。
如有错误，请输出修正后的结果。确保：
- has_errors: true 如果发现有错误
- corrections: 列出具体的错误项

输出格式：
{
  "has_errors": true/false,
  "all_correct": true/false,
  "corrections": ["错误描述1", "错误描述2"],
  "final_should_split": true/false,
  "final_atoms": [{"index": 1, "text": "..."}]
}"""


def conservative_atomize(
    records: list[PipelineRecord],
    proxy: LLMProxy,
    config: PipelineConfig,
) -> list[PipelineRecord]:
    """保守原子化主函数。"""
    validator = ConservativeAtomizeValidator()
    result: list[PipelineRecord] = []

    # 收集所有需要 LLM 判断的记录
    to_check: list[PipelineRecord] = []
    for rec in records:
        text = rec.requirement_text
        # 规则预判：如果文本很短或没有并列标记，跳过
        if len(text) < 20 or not _has_compound_marker(text):
            # 单条，无需拆分
            result.append(rec)
            continue
        to_check.append(rec)

    if not to_check:
        return result

    # 批量 LLM 判断（每批不超过 config.chunk_size）
    for i in range(0, len(to_check), config.chunk_size):
        batch = to_check[i:i + config.chunk_size]
        _atomize_batch(batch, proxy, validator, config)
        result.extend(batch)

    return result


def _has_compound_marker(text: str) -> bool:
    """检查文本是否含并列标记。"""
    import re
    markers = ["；", ";", "，并", ", and", "。同时", "同时", "并且", "以及"]
    return any(m in text for m in markers)


def _atomize_batch(
    batch: list[PipelineRecord],
    proxy: LLMProxy,
    validator: ConservativeAtomizeValidator,
    config: PipelineConfig,
):
    """批量处理原子化。"""
    items = [
        {
            "record_id": rec.record_id,
            "text": rec.requirement_text,
        }
        for rec in batch
    ]

    ids = [rec.record_id for rec in batch]
    texts_abstract = [rec.requirement_text[:200] for rec in batch]

    sc = proxy.judge_with_self_check(
        stage="conservative-atomize",
        system_prompt=CONSERVATIVE_ATOMIZE_SYSTEM,
        check_prompt=CONSERVATIVE_ATOMIZE_SELF_CHECK,
        user_payload={"requirements": items},
        input_ids=ids,
        input_texts_abstract=texts_abstract,
    )

    if sc.agreed and sc.confidence in ("HIGH", "MEDIUM"):
        decisions = sc.first_result if sc.first_result else {}
    else:
        decisions = sc.second_result if sc.second_result else {}

    if not decisions:
        # LLM 失败 → 不做任何拆分，原样保留
        for rec in batch:
            rec.mark_uncertain("conservative_atomize: LLM 判定失败", "atomize")
        return

    results_list = decisions.get("results") or decisions.get("final_results") or []

    # 如果返回的是单条结果
    if not results_list and isinstance(decisions, dict):
        if "should_split" in decisions:
            results_list = [{**{"record_id": batch[0].record_id if batch else ""}, **decisions}]

    record_map = {rec.record_id: rec for rec in batch}

    for item in results_list:
        rid = item.get("record_id", "")
        rec = record_map.get(rid)
        if not rec:
            continue

        should_split = item.get("should_split") or item.get("final_should_split")
        if not should_split:
            continue  # 不拆，保持原样

        atoms_data = item.get("atoms") or item.get("final_atoms") or []
        if not atoms_data:
            continue

        # 程序校验无损性
        atom_texts = [a.get("text", "") for a in atoms_data if a.get("text")]
        val_result = validator.check(rec.requirement_text, atom_texts)

        if val_result.valid:
            # 拆分
            for atom_data in atoms_data:
                text = atom_data.get("text", "").strip()
                if not text:
                    continue
                idx = atom_data.get("index", len(rec.atoms) + 1)
                atom = Atom(
                    atom_id=f"{rec.record_id}#{idx}",
                    text=text,
                    parent_record_id=rec.record_id,
                )
                rec.atoms.append(atom)
            rec.audit_log.append({
                "action": "conservative_atomize",
                "split_count": len(rec.atoms),
            })
        else:
            # 校验失败 → 不拆
            rec.mark_validation_failed([f"atomize: {e}" for e in val_result.errors])
            rec.mark_uncertain("conservative_atomize: 无损性校验失败", "atomize")