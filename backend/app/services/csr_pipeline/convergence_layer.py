"""CSR Pipeline V2 — Phase 3.5 全局深度收敛层 (Round-2 Global Convergence)

在第一轮切片判定与合并（Phase 2 & Phase 3）后，需求已从 400+ 条收敛至约 200 条候选。
本层以各个【功能分类（Topic）】为单位，在同一上下文窗口中对同领域内的所有候选条目进行跨批次、跨资产全局深度收敛。

核心原则：
1. 跨资产同类安全措施归一：例如针对所有调试接口的身份认证、针对所有固件安装前的验签、针对所有审计日志的防篡改追加写入，提炼为项目级通用规范（Universal CSR）。
2. 资产列表全量无损继承：合并后的 CSR 自动聚合所有成员条目的 applicable_assets。
3. 原始源需求 ID 100% 继承：所有 member 的 source_ids 全量并入代表条目。
"""
from __future__ import annotations
import re
from typing import Any
from .models import PipelineRecord, PipelineConfig
from .llm_proxy import LLMProxy


# ============================================================================
# Prompt 模板
# ============================================================================

GLOBAL_CONVERGENCE_SYSTEM = """你是汽车网络安全工程专家（ISO 21434 / UNECE R155）。
当前任务是对同一功能分类下的网络安全需求候选条目进行【项目级（全局）深度收敛与通用化合并】。

【项目级通用提炼原则】：
本项目目标是形成整车/域控制器级别的【通用网络安全需求规范（Universal Project-Level CSR）】。
不同子模块、不同接口、不同传感器在同一技术防护方向上的要求，应当合并为一条具有广泛约束力的项目级通用 CSR。

【合并规则】：
1. 技术控制措施实质相同必须合并：
   - 调试认证类：对各类调试接口（JTAG/USB/UART/以太网等）的身份认证与会话建立，合并为通用调试身份认证要求。
   - 调试授权/访问控制：对调试接口读写特权寄存器、内存、固件的基于角色的最小权限访问控制，合并为通用调试访问控制要求。
   - 日志防篡改：对各类日志（调试日志、崩溃转储、刷写日志、操作审计）的只追加写入、防篡改保护与未授权修改检测，合并为通用审计日志防篡改保护要求。
   - 固件与更新完整性校验：在安装、执行或生效前对固件包、升级包、配置数据的来源真实性与完整性校验，合并为通用固件与数据完整性校验要求。
   - 报文新鲜度与防重放：对外部报文、控制命令或数据的新鲜度校验与重放检测，合并为通用防重放要求。
   - 通信异常检测与抗 DoS：对外部通信接口的异常高负载流量检测、速率限制与服务可用性保障，合并为通用流量异常限制与抗 DoS 要求。
   - 敏感数据静态加密：对外部非易失存储中的敏感数据、密钥、日志的设备绑定加密存储，合并为通用静态加密存储要求。
   - 隐私数据保护与脱敏：对个人数据、视频流、位置信息的最小化、去标识化与目的限制访问，合并为通用隐私数据保护要求。
2. 资产列表自动继承：合并后，所有参与合并的资产名称将自动聚合到适用资产列表（Applicable Assets）中，不会丢失任何资产覆盖信息。
3. 技术机制有本质差异的不合并：如“物理防拆检测”与“逻辑访问控制”不得合并；“会话超时退出”与“静态加密存储”不得合并。

输出格式（严格 JSON）：
{
  "merged_groups": [
    {
      "canonical_text": "【主体】...【要求】... / [Subject]... [Requirement]...",
      "member_ids": ["ID1", "ID2"]
    }
  ]
}
注意：
1. member_ids 为参与合并的条目 ID 列表（必须包含至少 2 个条目）。未参与合并的独立条目无需在 merged_groups 中列出。
2. canonical_text 必须为专业、严谨的中英文双语项目级通用规范表述，保留所有关键安全约束和强度（'应/shall'）。
"""

GLOBAL_CONVERGENCE_SELF_CHECK = """你是汽车网络安全需求审核员。请检查以下全局收敛合并结果：
1. 审核合并是否合理：支持并鼓励合理的跨部件、跨接口同类安全控制措施通用合并；
2. 重点检查是否有【技术防护机制完全不同】的条目被错误合在了一起；
3. 检查是否有明显的同类措施未被合并（如有，可在 missing_merges 中指出）。

输出格式：
{
  "has_errors": false,
  "all_correct": true,
  "corrections": [],
  "missing_merges": []
}
"""


def _extract_core(text: str) -> str:
    """提取核心要求文本用于模型精简比对。"""
    if not text:
        return ""
    m = re.search(r"【要求】\s*(.*?)(?:/\s*\[Requirement\]|$)", text, re.DOTALL)
    if m and len(m.group(1).strip()) >= 5:
        return m.group(1).strip()
    m2 = re.search(r"\[Requirement\]\s*(.*)", text, re.DOTALL)
    if m2 and len(m2.group(1).strip()) >= 5:
        return m2.group(1).strip()
    cleaned = re.sub(r"^【主体】.*?(?:；|;|。|\n|【要求】)", "", text).strip()
    return cleaned if len(cleaned) >= 5 else text


def global_convergence_pass(
    records: list[PipelineRecord],
    proxy: LLMProxy,
    config: PipelineConfig,
) -> list[PipelineRecord]:
    """Phase 3.5: 第二轮同功能分类全局收敛。"""
    if len(records) <= 1:
        return records

    print(f"[CSR-Pipeline-V2] Phase 3.5: 开始第二轮全局深度收敛，输入 {len(records)} 条候选")

    # 按 topic_label 分类归集
    topics: dict[str, list[PipelineRecord]] = {}
    for rec in records:
        t = rec.topic_label or "4.2 系统加固"
        topics.setdefault(t, []).append(rec)

    final_records: list[PipelineRecord] = []

    for topic, group in topics.items():
        if len(group) <= 1:
            final_records.extend(group)
            continue

        print(f"[CSR-Pipeline-V2] 收敛主题 [{topic}]: {len(group)} 条候选")

        try:
            converged_group = _converge_topic_group(group, proxy, topic, config)
            final_records.extend(converged_group)
            print(f"[CSR-Pipeline-V2] 主题 [{topic}] 收敛完成: {len(group)} → {len(converged_group)} 条")
        except Exception as e:
            print(f"[CSR-Pipeline-V2] WARNING: 主题 [{topic}] 收敛异常，保留原记录: {e}")
            final_records.extend(group)

    print(f"[CSR-Pipeline-V2] Phase 3.5: 全局深度收敛完成: {len(records)} → {len(final_records)} 条")
    return final_records


def _converge_topic_group(
    group: list[PipelineRecord],
    proxy: LLMProxy,
    topic: str,
    config: PipelineConfig,
) -> list[PipelineRecord]:
    """对单个主题下的所有记录执行收敛。若条目数较大（>45）则分批处理后汇总。"""
    max_chunk = 45

    if len(group) <= max_chunk:
        return _converge_single_chunk(group, proxy, topic, f"conv-{topic}")

    # 若条目过多（如 4.2 系统加固 80+ 条），分块执行后连通
    print(f"[CSR-Pipeline-V2] 主题 [{topic}] 条目较多 ({len(group)})，执行分块多批收敛")
    chunk_size = 35
    overlap = 12

    chunks = []
    start = 0
    while start < len(group):
        chunks.append(group[start:start + chunk_size])
        if start + chunk_size >= len(group):
            break
        start += max(1, chunk_size - overlap)

    # 记录所有成员的合并关系 (并查集)
    parent: dict[str, str] = {}
    canonical_texts: dict[str, str] = {}

    def find(x: str) -> str:
        if x not in parent:
            parent[x] = x
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for idx, chunk in enumerate(chunks):
        c_res, c_groups = _judge_chunk_groups(chunk, proxy, topic, f"conv-{topic}-p{idx+1}")
        for g in c_groups:
            mids = g.get("member_ids", [])
            ctext = g.get("canonical_text", "")
            if len(mids) >= 2:
                for i in range(1, len(mids)):
                    union(mids[0], mids[i])
                root = find(mids[0])
                if ctext:
                    canonical_texts[root] = ctext

    # 基于连通分量构建收敛记录
    id_to_rec = {r.record_id: r for r in group}
    clusters: dict[str, list[str]] = {}
    for r in group:
        root = find(r.record_id)
        clusters.setdefault(root, []).append(r.record_id)

    result: list[PipelineRecord] = []
    for root, member_ids in clusters.items():
        if len(member_ids) < 2:
            result.append(id_to_rec[member_ids[0]])
            continue

        members = [id_to_rec[mid] for mid in member_ids if mid in id_to_rec]
        if not members:
            continue

        rep = members[0]
        c_text = canonical_texts.get(root) or canonical_texts.get(find(root)) or rep.requirement_text

        # 聚合资产与源 ID
        all_sources = set()
        all_assets = set()
        all_merge_sources = set()
        for m in members:
            all_sources.update(m.source_ids)
            all_assets.update(getattr(m, "applicable_assets", [m.asset_name]) or [m.asset_name])
            all_merge_sources.update(m.merge_source_ids)
            all_merge_sources.add(m.record_id)

        rep.canonical_text = c_text
        rep.requirement_text = c_text
        rep.source_ids = sorted(list(all_sources))
        rep.applicable_assets = sorted(list(all_assets))
        rep.merge_source_ids = sorted(list(all_merge_sources))
        result.append(rep)

    return result


def _judge_chunk_groups(
    chunk: list[PipelineRecord],
    proxy: LLMProxy,
    topic: str,
    stage_name: str,
) -> tuple[dict, list[dict]]:
    """调用 LLM 进行分块收敛判定，返回原始决策与组列表。"""
    items = [
        {
            "record_id": rec.record_id,
            "core_requirement": _extract_core(rec.requirement_text),
            "asset_name": rec.asset_name,
            "full_text": rec.requirement_text[:250],
        }
        for rec in chunk
    ]
    ids = [rec.record_id for rec in chunk]

    sc = proxy.judge_with_self_check(
        stage=stage_name,
        system_prompt=GLOBAL_CONVERGENCE_SYSTEM,
        check_prompt=GLOBAL_CONVERGENCE_SELF_CHECK,
        user_payload={
            "topic": topic,
            "candidates": items,
        },
        input_ids=ids,
        input_texts_abstract=[rec.requirement_text[:200] for rec in chunk],
    )

    decisions = sc.first_result if sc.first_result else {}
    groups = decisions.get("merged_groups", []) or []

    # 吸收自检报告中的 missing_merges
    if sc.second_result and isinstance(sc.second_result, dict):
        missing = sc.second_result.get("missing_merges", []) or []
        for mm in missing:
            if isinstance(mm, dict) and len(mm.get("member_ids", [])) >= 2:
                groups.append(mm)

    return decisions, groups


def _converge_single_chunk(
    group: list[PipelineRecord],
    proxy: LLMProxy,
    topic: str,
    stage_name: str,
) -> list[PipelineRecord]:
    """单批次（<= 45 条）完整收敛。"""
    _, groups = _judge_chunk_groups(group, proxy, topic, stage_name)

    if not groups:
        return group

    record_map = {r.record_id: r for r in group}
    consumed_ids = set()
    result: list[PipelineRecord] = []

    for g in groups:
        mids = [mid for mid in g.get("member_ids", []) if mid in record_map and mid not in consumed_ids]
        if len(mids) < 2:
            continue

        members = [record_map[mid] for mid in mids]
        rep = members[0]
        canonical = g.get("canonical_text") or rep.requirement_text

        all_sources = set()
        all_assets = set()
        all_merge_sources = set()

        for m in members:
            consumed_ids.add(m.record_id)
            all_sources.update(m.source_ids)
            all_assets.update(getattr(m, "applicable_assets", [m.asset_name]) or [m.asset_name])
            all_merge_sources.update(m.merge_source_ids)
            all_merge_sources.add(m.record_id)

        rep.canonical_text = canonical
        rep.requirement_text = canonical
        rep.source_ids = sorted(list(all_sources))
        rep.applicable_assets = sorted(list(all_assets))
        rep.merge_source_ids = sorted(list(all_merge_sources))
        result.append(rep)

    # 未被合并的条目保持原样
    for r in group:
        if r.record_id not in consumed_ids:
            result.append(r)

    return result
