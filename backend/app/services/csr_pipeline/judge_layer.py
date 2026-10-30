"""CSR Pipeline V2 — Phase 2 判定层

主题内分块等价判定 + 自检 → 并查集归并 → 全局小组名归类 → 回原文裁决。
LLM 只输出 ID 分组，不写文本。
"""
from __future__ import annotations
from .models import PipelineRecord, PipelineConfig
from .llm_proxy import LLMProxy


# ============================================================================
# Prompt 模板
# ============================================================================

JUDGE_EQUIVALENCE_SYSTEM = """你是汽车网络安全工程专家（ISO 21434）。
当前任务是对同一功能分类下的安全需求进行项目级（全局）等价判定与去重合并。

【项目级提炼原则】：
本项目需要将各个不同子资产（如多个调试接口、多路传感器数据、多条通信报文）上具有相同/相似安全控制措施的需求，提炼合并为一条全局通用的网络安全要求（CSR），系统会自动将其关联的所有资产名称聚合到适用资产列表中。

【判定规则】：
1. 跨资产技术等价：若不同资产在技术防护目标、控制措施、检验方式上实质相同（例如：多个调试接口都要求“对调试终端进行身份认证”、“对接收命令进行新鲜度与防重放校验”、“生产阶段关闭硬件调试口”等），即便原资产对象或前缀不同，也【必须】判定为等价并归入同一组！
2. 语义与措辞容错：中英文双语、近义词表达差异（如“应验证”、“必须校验”、“需实施检测”）判定为等价。
3. 约束强度不同不合并：要求强度有本质差异的（如“完全禁用” vs “条件允许启用”）不得合并。
4. 控制技术措施不同不合并：技术方向不同的（如“会话超时退出” vs “密码算法加密”）不得合并。

输出格式：
{
  "groups": [
    {
      "group_name": "系统应对外部调试命令进行新鲜度校验并拒绝重放",
      "member_ids": ["ASSET-1::CSR-001", "ASSET-2::CSR-005"]
    }
  ]
}

group_name 为该等价组最精炼通用的安全要求表述（不带具体资产前缀），member_ids 为等价记录 ID 列表。未被分组的独立记录无需列出。"""

JUDGE_SELF_CHECK_SYSTEM = """你是汽车网络安全需求审核员。请检查以下等价分组是否合理：

审核要点：
1. 鼓励并支持合理的跨资产通用控制要求合并（如对多个调试资产统一要求身份认证、对多个传感器统一要求完整性校验是完全合理的，不要因为资产主体不同而拆散）。
2. 重点检查是否有【技术措施完全不同】的条目被错误合并（如把会话超时与数据加密合在一起）。
3. 检查是否有明显的同类要求被遗漏未合并（如有，请在 missing_merges 中指出）。

输出格式：
{
  "has_errors": true/false,
  "all_correct": true/false,
  "corrections": [],
  "missing_merges": []
}"""


GLOBAL_GROUP_SYSTEM = """你是网络安全需求分析专家。以下是从不同分块中产出的等价组名列表。
请判断哪些组名在语义上属于同一条项目级通用安全需求，可进一步合并。

判定规则：
1. 表达同一安全控制措施的组名合并为一条（如“调试命令新鲜度校验”与“验证调试命令新鲜性并防重放”合并）。
2. 技术目标完全不同的不得合并。

输出格式：
{
  "merged_groups": [
    {
      "merged_name": "系统应对外部调试命令执行新鲜度校验并拒绝重放",
      "source_group_names": ["调试命令新鲜度校验", "验证调试命令新鲜性并防重放"]
    }
  ]
}"""

GLOBAL_GROUP_SELF_CHECK = """你是需求分析审核员。请检查以下全局组名归类：

- 是否有不该归类的组名被错误合并（技术目标不同、强度不同）？
- 是否有该归类但被遗漏的组名？

输出格式：
{
  "has_errors": true/false,
  "all_correct": true/false,
  "corrections": []
}"""


BACK_TO_ORIGINAL_SYSTEM = """你是网络安全需求审核专家。以下是一个项目级全局等价类中的候选需求。
请确认它们是否表达了相同的核心安全控制要求（支持跨资产提炼）：
1. 核心技术措施一致的保留在 confirmed 中；
2. 仅当某条目技术措施方向完全无关时，才放入 removed 拆出。

输出格式：
{
  "confirmed": ["ASSET-1::CSR-001", "ASSET-2::CSR-005"],
  "removed": [],
  "all_equivalent": true
}"""

BACK_TO_ORIGINAL_SELF_CHECK = """你是审核员。请检查以下回原文裁决结果：

- 被拆出的条目是否真的技术方向不同？
- 留在组内的条目是否表达了相似的核心控制措施？

输出格式：
{
  "has_errors": true/false,
  "all_correct": true/false,
  "corrections": []
}"""


# ============================================================================
# Union-Find
# ============================================================================

class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}
        self.rank: dict[str, int] = {}

    def find(self, x: str) -> str:
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x: str, y: str):
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self.rank.get(rx, 0) < self.rank.get(ry, 0):
            self.parent[rx] = ry
        elif self.rank.get(rx, 0) > self.rank.get(ry, 0):
            self.parent[ry] = rx
        else:
            self.parent[ry] = rx
            self.rank[rx] = self.rank.get(rx, 0) + 1

    def groups(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for node in self.parent:
            root = self.find(node)
            result.setdefault(root, []).append(node)
        return result


# ============================================================================
# 主函数
# ============================================================================

def judge_equivalence(
    records: list[PipelineRecord],
    proxy: LLMProxy,
    config: PipelineConfig,
) -> list[PipelineRecord]:
    """判定层主函数。四步串行。"""
    if len(records) <= 1:
        return records

    # Step 1: 主题内分块等价判定
    records = _judge_within_topics(records, proxy, config)

    # Step 2: 并查集归并局部结果
    records = _union_find_merge(records, proxy, config)

    # Step 3: 全局小组名归类
    records = _global_group_merge(records, proxy, config)

    # Step 4: 回原文最终裁决
    records = _back_to_original_ruling(records, proxy, config)

    return records


# ============================================================================
# Step 1: 主题内分块等价判定
# ============================================================================

def _extract_core_requirement(text: str) -> str:
    """提取核心要求文本（剥离【主体】...等资产特有前缀，聚焦核心技术措施）。"""
    import re
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


def _judge_within_topics(
    records: list[PipelineRecord],
    proxy: LLMProxy,
    config: PipelineConfig,
) -> list[PipelineRecord]:
    """按 topic_label 分类直接分块，放宽切片粒度，实现跨资产纵览与通用合并。"""
    topic_groups: dict[str, list[PipelineRecord]] = {}
    for rec in records:
        topic = rec.topic_label or "4.2 系统加固"
        topic_groups.setdefault(topic, []).append(rec)

    chunk_size = config.chunk_size
    overlap = config.chunk_overlap

    for topic, group in topic_groups.items():
        if len(group) <= 1:
            continue

        # 分块（带重叠滑动窗口）
        chunks = []
        start = 0
        while start < len(group):
            chunk = group[start:start + chunk_size]
            chunks.append(chunk)
            if start + chunk_size >= len(group):
                break
            start += max(1, chunk_size - overlap)

        # 每块 LLM 判定
        for chunk in chunks:
            if len(chunk) <= 1:
                continue
            _judge_one_chunk(chunk, proxy, topic)

    return records


def _judge_one_chunk(
    chunk: list[PipelineRecord],
    proxy: LLMProxy,
    topic: str,
):
    """对单个分块做等价判定。"""
    items = [
        {
            "record_id": rec.record_id,
            "core_requirement": _extract_core_requirement(rec.requirement_text),
            "asset_name": rec.asset_name,
            "full_text": rec.requirement_text[:200],
        }
        for rec in chunk
    ]
    ids = [rec.record_id for rec in chunk]

    sc = proxy.judge_with_self_check(
        stage=f"judge-equivalence-{topic}",
        system_prompt=JUDGE_EQUIVALENCE_SYSTEM,
        check_prompt=JUDGE_SELF_CHECK_SYSTEM,
        user_payload={
            "requirements": items,
            "topic": topic,
        },
        input_ids=ids,
        input_texts_abstract=[rec.requirement_text[:200] for rec in chunk],
    )

    # 应用正向判定结果
    decisions = sc.first_result if sc.first_result else {}
    groups = decisions.get("groups", []) or []

    chunk_map = {rec.record_id: rec for rec in chunk}

    for g in groups:
        group_name = g.get("group_name", "")
        member_ids = g.get("member_ids", []) or []
        if len(member_ids) < 2:
            continue

        group_id = f"EQ-{topic}-{group_name[:20]}"
        for mid in member_ids:
            rec = chunk_map.get(mid)
            if rec:
                rec.equivalence_group_id = group_id
                rec.audit_log.append({
                    "action": "equivalence_grouped",
                    "group_name": group_name,
                    "group_id": group_id,
                })

    # 应用自检修正
    if sc.second_result and isinstance(sc.second_result, dict):
        corrections = sc.second_result.get("corrections", []) or []
        for corr in corrections:
            if isinstance(corr, dict):
                mid = corr.get("member_id", "")
                rec = chunk_map.get(mid)
                if rec and corr.get("action") == "remove":
                    rec.equivalence_group_id = ""
                    rec.audit_log.append({"action": "equivalence_removed_by_self_check", "reason": corr.get("reason")})

        missing_merges = sc.second_result.get("missing_merges", []) or []
        for idx, mm in enumerate(missing_merges):
            if isinstance(mm, dict):
                mids = mm.get("member_ids", []) or []
                if len(mids) >= 2:
                    gid = f"EQ-{topic}-MISSING-{idx+1}"
                    for mid in mids:
                        rec = chunk_map.get(mid)
                        if rec and not rec.equivalence_group_id:
                            rec.equivalence_group_id = gid


# ============================================================================
# Step 2: 并查集归并局部结果
# ============================================================================

def _union_find_merge(
    records: list[PipelineRecord],
    proxy: LLMProxy,
    config: PipelineConfig,
) -> list[PipelineRecord]:
    """纯程序：通过并查集连接等价关系。"""
    uf = UnionFind()

    # 同一 equivalence_group_id 的记录归并
    group_members: dict[str, list[PipelineRecord]] = {}
    for rec in records:
        gid = rec.equivalence_group_id
        if gid:
            group_members.setdefault(gid, []).append(rec)

    for gid, members in group_members.items():
        for i in range(1, len(members)):
            uf.union(members[0].record_id, members[i].record_id)

    # 通过 source_ids 交集连接跨块等价
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            si = set(records[i].source_ids)
            sj = set(records[j].source_ids)
            if si & sj:  # 共享源 ID → 同一等价类
                uf.union(records[i].record_id, records[j].record_id)

    # 重新分配 equivalence_group_id
    groups = uf.groups()
    gid_counter = 0
    record_map = {rec.record_id: rec for rec in records}

    for root, member_ids in groups.items():
        if len(member_ids) < 2:
            continue
        gid_counter += 1
        new_gid = f"EQ-GLOBAL-{gid_counter}"
        for mid in member_ids:
            rec = record_map.get(mid)
            if rec:
                rec.equivalence_group_id = new_gid

    return records


# ============================================================================
# Step 3: 全局小组名归类
# ============================================================================

def _global_group_merge(
    records: list[PipelineRecord],
    proxy: LLMProxy,
    config: PipelineConfig,
) -> list[PipelineRecord]:
    """全局小组名 LLM 归类 + 自检。"""
    # 收集所有非空的局部等价组名
    group_info: dict[str, dict] = {}
    for rec in records:
        gid = rec.equivalence_group_id
        if not gid:
            continue
        if gid not in group_info:
            group_info[gid] = {"group_id": gid, "member_ids": [], "representative_text": rec.requirement_text}
        group_info[gid]["member_ids"].append(rec.record_id)

    if len(group_info) < 2:
        return records

    # 每批 100 个组名发 LLM
    batch_size = config.global_group_batch_size
    group_names = list(group_info.values())

    for i in range(0, len(group_names), batch_size):
        batch = group_names[i:i + batch_size]
        _merge_global_groups_batch(batch, proxy, records)

    return records


def _merge_global_groups_batch(
    batch: list[dict],
    proxy: LLMProxy,
    records: list[PipelineRecord],
):
    """单批全局组名归类。"""
    items = [{"group_id": g["group_id"], "representative_text": g["representative_text"][:300]} for g in batch]

    sc = proxy.judge_with_self_check(
        stage="global-group-merge",
        system_prompt=GLOBAL_GROUP_SYSTEM,
        check_prompt=GLOBAL_GROUP_SELF_CHECK,
        user_payload={"group_names": items},
        input_ids=[g["group_id"] for g in batch],
        input_texts_abstract=[g["representative_text"][:300] for g in batch],
    )

    if not sc.agreed:
        return

    decisions = sc.first_result if sc.first_result else {}
    merged_groups = decisions.get("merged_groups", []) or []

    record_map = {rec.record_id: rec for rec in records}

    for mg in merged_groups:
        source_names = mg.get("source_group_names", []) or []
        if len(source_names) < 2:
            continue
        # 收集所有 source group 的成员
        all_members: set[str] = set()
        for g in batch:
            if g["group_id"] in source_names:
                all_members.update(g.get("member_ids", []))
        # 统一 group_id
        if all_members:
            new_gid = f"EQ-GLOBAL-MERGED-{source_names[0][:30]}"
            for mid in all_members:
                rec = record_map.get(mid)
                if rec:
                    rec.equivalence_group_id = new_gid


# ============================================================================
# Step 4: 回原文最终裁决
# ============================================================================

def _back_to_original_ruling(
    records: list[PipelineRecord],
    proxy: LLMProxy,
    config: PipelineConfig,
) -> list[PipelineRecord]:
    """回原文最终裁决：对每个全局等价类最后确认一次。"""
    group_members: dict[str, list[PipelineRecord]] = {}
    for rec in records:
        gid = rec.equivalence_group_id
        if gid:
            group_members.setdefault(gid, []).append(rec)

    for gid, members in group_members.items():
        if len(members) < 2:
            continue

        _rule_one_group(gid, members, proxy)

    return records


def _rule_one_group(
    group_id: str,
    members: list[PipelineRecord],
    proxy: LLMProxy,
):
    """对单个等价类做回原文裁决（支持跨资产提炼）。"""
    items = [
        {
            "record_id": rec.record_id,
            "core_requirement": _extract_core_requirement(rec.requirement_text),
            "asset_name": rec.asset_name,
        }
        for rec in members
    ]
    ids = [rec.record_id for rec in members]

    sc = proxy.judge_with_self_check(
        stage="back-to-original",
        system_prompt=BACK_TO_ORIGINAL_SYSTEM,
        check_prompt=BACK_TO_ORIGINAL_SELF_CHECK,
        user_payload={
            "group_id": group_id,
            "requirements": items,
        },
        input_ids=ids,
        input_texts_abstract=[rec.requirement_text[:300] for rec in members],
    )

    decisions = sc.first_result if sc.first_result else {}
    confirmed = set(decisions.get("confirmed", []) or [])
    removed = decisions.get("removed", []) or []

    member_map = {rec.record_id: rec for rec in members}

    removed_ids = set()
    for item in removed:
        if isinstance(item, dict):
            rid = item.get("id") or item.get("member_id", "")
            if rid:
                removed_ids.add(rid)
        elif isinstance(item, str) and item:
            removed_ids.add(item)

    # 吸收自检报告中的 corrections
    if sc.second_result and isinstance(sc.second_result, dict):
        corrections = sc.second_result.get("corrections", []) or []
        for corr in corrections:
            if isinstance(corr, dict) and corr.get("action") == "remove":
                rid = corr.get("member_id", "")
                if rid:
                    removed_ids.add(rid)

    # 如果 confirmed 为空且无明确 removed，则默认确认所有成员
    if not confirmed and not removed_ids:
        confirmed = set(member_map.keys())

    # 拆出被明确移出的条目
    for rid in removed_ids:
        rec = member_map.get(rid)
        if rec:
            rec.equivalence_group_id = ""
            rec.audit_log.append({
                "action": "back_to_original_split",
                "group_id": group_id,
            })

    # 若移出后组内剩余有效成员 < 2，则解散该组
    remaining = [rec for mid, rec in member_map.items() if (mid in confirmed or not confirmed) and mid not in removed_ids]
    if len(remaining) < 2:
        for rec in remaining:
            rec.equivalence_group_id = ""