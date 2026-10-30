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

JUDGE_EQUIVALENCE_SYSTEM = """你是网络安全需求分析专家。以下需求已经按功能分类（如 4.4 安全调试、4.12 密码安全等）分组，请判断组内哪些需求是语义等价的。

判定规则（严格遵守）：
1. 技术要求和约束强度都相同才判等价。仅主题相近但要求细节不同的 → 不等价。
2. 动作相同但对象不同 → 不等价（对象不同意味着不同安全目标）。
3. 动作与失败处理互补（如"应认证"+"应拒绝未通过认证"）→ 不等价（留给互补合并阶段处理）。
4. 同一要求的中英文版本 → 等价。
5. 仅在措辞上有差异（如"应验证" vs "必须校验"）但要求相同的 → 等价。
6. 不确定 → 默认不判等价。
7. 同组内的条目已经属于同一功能分类，不需要再比较分类。

输出格式：
{
  "groups": [
    {
      "group_name": "应验证固件签名",
      "member_ids": ["ASSET-1::CSR-001", "ASSET-2::CSR-005"]
    }
  ]
}

group_name 应为最简洁的语义摘要（不带主体前缀），member_ids 为判为等价的记录 ID 列表。
每条记录最多出现在一个组中。未被分组的记录不需要在输出中列出。"""

JUDGE_SELF_CHECK_SYSTEM = """你是需求分析审核员。请检查以下等价分组是否合理：

- 是否有不该合并的条目被错误分到同一组（对象不同、安全目标不同、约束强度不同）？
- 是否有该合并的条目被遗漏（主题相同、措辞不同但要求实际上相同）？
- 是否有某个组的 member_ids 包含了明显不属于该组的条目？

输入包含原始需求列表和正向判定的等价分组。

输出格式：
{
  "has_errors": true/false,
  "all_correct": true/false,
  "corrections": [
    {
      "group_name": "应验证固件签名",
      "action": "remove",
      "member_id": "ASSET-1::CSR-003",
      "reason": "该需求是关于代码签名验证，与固件签名是不同的安全目标"
    }
  ],
  "missing_merges": [
    {
      "member_ids": ["ASSET-1::CSR-010", "ASSET-2::CSR-020"],
      "reason": "这两条都要求关闭调试接口，措辞不同但要求相同"
    }
  ]
}"""


GLOBAL_GROUP_SYSTEM = """你是网络安全需求分析专家。以下是从不同分块中产出的等价组名列表。

判断哪些组名在语义上是等价的（即表述的是同一条安全需求，只是措辞不同）。

判定规则：
1. 主体不同但要求语义相同 → 可归一类（如"ECU 应验证签名"和"网关应验证签名"语义相同）
2. 要求强度不同 → 不得归一类（如"应加密" vs "宜加密"）
3. 对象不同 → 不得归一类（如"关闭 JTAG" vs "关闭串口"，虽然都是关闭，但对象不同）

输出格式：
{
  "merged_groups": [
    {
      "merged_name": "应验证固件签名完整性和真实性",
      "source_group_names": ["应验证固件签名", "固件签名校验", "应验证OTA固件签名"]
    }
  ]
}"""

GLOBAL_GROUP_SELF_CHECK = """你是需求分析审核员。请检查以下全局组名归类：

- 是否有不该归类的组名被错误合并（对象不同、强度不同）？
- 是否有该归类但被遗漏的组名？

输出格式：
{
  "has_errors": true/false,
  "all_correct": true/false,
  "corrections": ["错误描述"]
}"""


BACK_TO_ORIGINAL_SYSTEM = """你是网络安全需求审核专家。以下是一个全局等价类中的全部原始需求文本。

请逐条判断：这些需求是否真的全部等价？如果不等价，哪些应该拆出去？

原则：
1. 不确定 → 默认拆开（宁可漏合并，不能错合并）
2. 主体不同可能导致技术要求差异 → 必须拆开验证
3. 约束条件不同（如"量产阶段" vs "全生命周期"）→ 必须拆开

输出格式：
{
  "confirmed": ["ASSET-1::CSR-001", "ASSET-2::CSR-005"],
  "removed": [
    {
      "id": "ASSET-3::CSR-010",
      "reason": "该需求要求'开发阶段'与其余条目的'量产阶段'约束不同，技术要求有差异"
    }
  ],
  "all_equivalent": false
}"""

BACK_TO_ORIGINAL_SELF_CHECK = """你是审核员。请检查以下回原文裁决结果：

- 被拆出的条目是否真的不等价？
- 留在组内的条目是否全部等价？
- 有没有被拆出但实际上是等价的条目？

输出格式：
{
  "has_errors": true/false,
  "all_correct": true/false,
  "corrections": ["错误描述"]
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

def _judge_within_topics(
    records: list[PipelineRecord],
    proxy: LLMProxy,
    config: PipelineConfig,
) -> list[PipelineRecord]:
    """按 (boundary_class, topic_label) 二维分组，每组内分块 LLM 判等价。"""
    # 分组
    topic_groups: dict[tuple[str, str], list[PipelineRecord]] = {}
    for rec in records:
        # topic_uncertain 的记录不按 topic 分块，按 boundary_class 单维
        if rec.topic_uncertain:
            key = (rec.boundary_class or "通用", "__UNCERTAIN__")
        else:
            key = (rec.boundary_class or "通用", rec.topic_label or "未分类")
        topic_groups.setdefault(key, []).append(rec)

    # 每组分块判定
    chunk_size = config.chunk_size
    overlap = config.chunk_overlap

    for (bc, topic), group in topic_groups.items():
        if len(group) <= 1:
            continue

        # 分块（50% 重叠）
        chunks = []
        start = 0
        while start < len(group):
            chunk = group[start:start + chunk_size]
            chunks.append(chunk)
            start += chunk_size - overlap

        # 每块 LLM 判定
        for chunk in chunks:
            if len(chunk) <= 1:
                continue
            _judge_one_chunk(chunk, proxy, bc, topic)

    return records


def _judge_one_chunk(
    chunk: list[PipelineRecord],
    proxy: LLMProxy,
    boundary_class: str,
    topic: str,
):
    """对单个分块做等价判定。"""
    items = [
        {
            "record_id": rec.record_id,
            "text": rec.requirement_text,
            "asset_name": rec.asset_name,
        }
        for rec in chunk
    ]
    ids = [rec.record_id for rec in chunk]

    sc = proxy.judge_with_self_check(
        stage=f"judge-equivalence-{boundary_class}-{topic}",
        system_prompt=JUDGE_EQUIVALENCE_SYSTEM,
        check_prompt=JUDGE_SELF_CHECK_SYSTEM,
        user_payload={
            "requirements": items,
            "boundary_class": boundary_class,
            "topic": topic,
        },
        input_ids=ids,
        input_texts_abstract=[rec.requirement_text[:200] for rec in chunk],
    )

    if not sc.agreed:
        # 不一致 → 标记该块所有记录为 uncertain
        for rec in chunk:
            rec.is_uncertain = True
            rec.audit_log.append({
                "action": "equivalence_judge_uncertain",
                "stage": "chunk",
                "discrepancies": sc.discrepancies,
            })
        return

    # 应用正向判定结果
    decisions = sc.first_result if sc.first_result else {}
    groups = decisions.get("groups", []) or []

    chunk_map = {rec.record_id: rec for rec in chunk}

    for g in groups:
        group_name = g.get("group_name", "")
        member_ids = g.get("member_ids", []) or []
        if len(member_ids) < 2:
            continue

        group_id = f"EQ-{boundary_class}-{topic}-{group_name[:20]}"
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
    if sc.second_result:
        corrections = sc.second_result.get("corrections", []) or []
        for corr in corrections:
            mid = corr.get("member_id", "")
            rec = chunk_map.get(mid)
            if rec:
                if corr.get("action") == "remove":
                    rec.equivalence_group_id = ""
                    rec.audit_log.append({"action": "equivalence_removed_by_self_check", "reason": corr.get("reason")})


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
    """对单个等价类做回原文裁决。"""
    items = [
        {
            "record_id": rec.record_id,
            "text": rec.requirement_text,
            "asset_name": rec.asset_name,
            "boundary_class": rec.boundary_class,
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

    if not sc.agreed:
        # 不一致 → 全部标记 uncertain
        for rec in members:
            rec.equivalence_group_id = ""
            rec.is_uncertain = True
            rec.audit_log.append({
                "action": "back_to_original_uncertain",
                "group_id": group_id,
                "discrepancies": sc.discrepancies,
            })
        return

    decisions = sc.first_result if sc.first_result else {}
    confirmed = set(decisions.get("confirmed", []) or [])
    removed = decisions.get("removed", []) or []

    member_map = {rec.record_id: rec for rec in members}

    # 保留确认的
    for mid in list(member_map.keys()):
        if mid not in confirmed:
            rec = member_map[mid]
            rec.equivalence_group_id = ""
            rec.is_uncertain = True
            rec.audit_log.append({
                "action": "back_to_original_removed",
                "group_id": group_id,
                "reason": "未在确认列表中",
            })

    # 拆出的
    for item in removed:
        rid = item.get("id", "")
        reason = item.get("reason", "")
        rec = member_map.get(rid)
        if rec:
            rec.equivalence_group_id = ""
            rec.is_uncertain = True
            rec.audit_log.append({
                "action": "back_to_original_split",
                "group_id": group_id,
                "reason": reason,
            })