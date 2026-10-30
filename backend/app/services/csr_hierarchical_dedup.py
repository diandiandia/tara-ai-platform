"""Two-level whole-requirement dedup followed by atomization and atomic dedup.

This module implements the requested workflow:

    per-asset dedup
    -> pairwise group dedup (16 -> 8 -> 4)
    -> K1 absorbs K2/K3/K4 in order
    -> per-requirement atomization
    -> atomic-level dedup
    -> source-ID coverage validation

The LLM decides duplicates (a full all-pairs scan for small groups, and
candidate pairs for larger groups) and performs the final atomization.
Candidate generation, record pass-through, union-find merging and traceability
checks are deterministic code.  Merged records retain every distinct source
statement so atomization cannot drop details that only one duplicate held.
"""

from __future__ import annotations

import re
from collections import defaultdict
from types import SimpleNamespace
from typing import Dict, List, Optional, Sequence, Set, Tuple

from app.services.csr_synthesis import (
    _call_llm_json,
    _content_hash_id,
    _normalize_id_list,
    _rewrite_atom_subject,
    extract_subject_and_clauses,
    semantic_tokens,
)

# Candidate generation is deliberately permissive; the LLM makes the final
# duplicate decision.  Different assets often describe the same control with
# different wording or in different languages, so Jaccard on full text alone
# has poor recall.  We combine Jaccard, Dice and containment, and always let
# the LLM adjudicate the resulting candidates.
CANDIDATE_SIMILARITY = 0.25
MAX_CANDIDATES_PER_RECORD = 8
# Atoms are expected to be much closer to each other than whole requirements;
# a higher threshold keeps the LLM adjudication focused on plausible duplicates.
ATOM_CANDIDATE_SIMILARITY = 0.40
ATOM_MAX_CANDIDATES_PER_RECORD = 5
ATOM_BATCH_SIZE = 10
PAIR_BATCH_SIZE = 10
# Small groups are additionally full-scanned by the LLM: every record is shown
# together, so semantic duplicates are not lost just because their wording
# differs.  Larger groups use bounded candidate pairs; an all-pairs response
# for 30+ long bilingual requirements is slow and can time out.
FULL_SCAN_LIMIT = 20


def _asset_attr(asset, name: str, default=""):
    if isinstance(asset, dict):
        return asset.get(name, default)
    return getattr(asset, name, default)


def _text(record: dict) -> str:
    return str(
        record.get("text")
        or record.get("cybersecurity_requirement")
        or record.get("statement")
        or ""
    ).strip()


def _normalized_text(record: dict) -> str:
    return re.sub(r"\s+", "", _text(record).lower())


def _source_ids(record: dict) -> List[str]:
    return _normalize_id_list(
        record.get("source_requirement_ids")
        or record.get("source_tara_ids")
        or []
    )


def make_record(asset, requirement: dict, req_map: Optional[dict] = None) -> dict:
    """Convert one TARA requirement into a stable record for dedup."""
    req_map = req_map or {}
    text = str(requirement.get("cybersecurity_requirement") or "").strip()
    rid = str(
        requirement.get("cybersecurity_requirement_id")
        or requirement.get("csr_id")
        or ""
    ).strip()
    if not rid or rid.upper() == "N/A":
        rid = req_map.get(text) or _content_hash_id("CSR_", text)
    asset_id = _asset_attr(asset, "id", 0)
    asset_name = _asset_attr(asset, "name", "") or str(asset_id)
    record_id = f"ASSET-{asset_id}::{rid}"
    source_ids = _normalize_id_list(
        requirement.get("source_requirement_ids")
        or requirement.get("source_tara_ids")
    )
    if rid and rid not in source_ids:
        source_ids.append(rid)
    if not source_ids:
        source_ids = [rid]
    source_entry = {
        "text": text,
        "source_requirement_ids": list(source_ids),
        "applicable_assets": [asset_name],
        "asset_name": asset_name,
        "subject": requirement.get("subject") or "",
        "verification_method": requirement.get("verification_method") or "",
        "acceptance_criteria": requirement.get("acceptance_criteria") or "",
        "allocated_to": requirement.get("allocated_to") or "",
    }
    return {
        "record_id": record_id,
        "asset_id": asset_id,
        "asset_name": asset_name,
        "source_requirement_ids": list(source_ids),
        "source_texts": [source_entry],
        "text": text,
        "title": requirement.get("title") or "",
        "subject": requirement.get("subject") or "",
        "requirement_scope": requirement.get("requirement_scope") or "",
        "owner": requirement.get("owner") or "",
        "allocated_to": requirement.get("allocated_to") or "",
        "allocation_type": requirement.get("allocation_type") or "",
        "verification_method": requirement.get("verification_method") or "",
        "acceptance_criteria": requirement.get("acceptance_criteria") or "",
        "requirement_type": requirement.get("requirement_type") or "product",
        "goal_ids": _normalize_id_list(requirement.get("goal_ids")),
        "control_ids": _normalize_id_list(requirement.get("control_ids")),
        "claim_ids": _normalize_id_list(requirement.get("claim_ids")),
        "source_ids": _normalize_id_list(requirement.get("source_ids")),
        "dependencies": _normalize_id_list(requirement.get("dependencies")),
        "rationale": requirement.get("rationale") or "",
        "open_issue": requirement.get("open_issue") or "",
        "applicable_assets": [asset_name],
        "merged_from": [record_id],
    }


def _candidate_key(record: dict) -> tuple:
    # Scope is a hard semantic boundary.  Owner/allocation are deliberately
    # left to the LLM: the same generic control can be owned or allocated
    # differently on different assets, and blocking on those fields would hide
    # real cross-asset duplicates.
    return (str(record.get("requirement_scope") or ""),)


def _share_asset(a: dict, b: dict) -> bool:
    aa = {str(x) for x in (a.get("applicable_assets") or []) if str(x).strip()}
    bb = {str(x) for x in (b.get("applicable_assets") or []) if str(x).strip()}
    return bool(aa & bb)


def _records_compatible(a: dict, b: dict) -> bool:
    av = str(a.get("requirement_scope") or "").strip()
    bv = str(b.get("requirement_scope") or "").strip()
    if av and bv and av != bv:
        return False
    av = str(a.get("owner") or "").strip()
    bv = str(b.get("owner") or "").strip()
    if av and bv and av != bv:
        return False
    av = str(a.get("requirement_type") or "").strip()
    bv = str(b.get("requirement_type") or "").strip()
    if av and bv and av != bv:
        return False

    shared = _share_asset(a, b)
    if shared:
        # Same asset: the verification/acceptance and allocation must agree.
        for field in ("verification_method", "acceptance_criteria"):
            av = str(a.get(field) or "").strip()
            bv = str(b.get(field) or "").strip()
            if av and bv and av != bv:
                return False
        a_alloc = str(a.get("allocated_to") or "").strip()
        b_alloc = str(b.get("allocated_to") or "").strip()
        if a_alloc and b_alloc and a_alloc != b_alloc:
            return False
    # Different assets: the same generic control may use different
    # verification/acceptance and may be allocated to corresponding components
    # of each asset.  The merged record unions those fields.
    return True


def _record_payload(record: dict) -> dict:
    entries = record.get("source_texts") or []
    statements = []
    if len(entries) > 1:
        statements = [
            {
                "source_requirement_ids": entry.get("source_requirement_ids") or [],
                "text": entry.get("text") or "",
            }
            for entry in entries
        ]
    return {
        "record_id": record.get("record_id") or "",
        "text": _text(record),
        "statements": statements,
        "source_requirement_ids": _source_ids(record),
        "applicable_assets": record.get("applicable_assets") or [],
        "asset_name": record.get("asset_name") or "",
        "requirement_scope": record.get("requirement_scope") or "",
        "requirement_type": record.get("requirement_type") or "",
        "owner": record.get("owner") or "",
        "allocated_to": record.get("allocated_to") or "",
        "verification_method": record.get("verification_method") or "",
        "acceptance_criteria": record.get("acceptance_criteria") or "",
        "subject": record.get("subject") or "",
    }


def _similarity_tokens(record: dict) -> Set[str]:
    text = _text(record).lower()
    # semantic_tokens drops bare numbers (e.g. "3 秒", "action 39"), which
    # makes unrelated numeric variants look identical.  Keep numeric literals
    # in the candidate score only; the global tokenizer is left unchanged.
    return semantic_tokens(text) | set(re.findall(r"\d+(?:\.\d+)?", text))


def _candidate_similarity(a: dict, b: dict) -> float:
    """Lexical recall score for candidate blocking.

    Containment and Dice are more forgiving than Jaccard when one requirement
    is a more detailed version of the other.  The score is only a cheap
    prefilter; the LLM makes the actual duplicate decision.
    """
    a_norm = _normalized_text(a)
    b_norm = _normalized_text(b)
    if a_norm and a_norm == b_norm:
        return 1.0
    a_tokens = _similarity_tokens(a)
    b_tokens = _similarity_tokens(b)
    if not a_tokens or not b_tokens:
        return 0.0
    inter = len(a_tokens & b_tokens)
    jaccard = inter / len(a_tokens | b_tokens)
    dice = 2 * inter / (len(a_tokens) + len(b_tokens))
    containment = inter / min(len(a_tokens), len(b_tokens))
    # Any of the three may be the only signal for a cross-asset pair: one
    # requirement can be a more detailed version of the other, so containment
    # must not be discounted.
    return max(jaccard, dice, containment)


def generate_candidate_pairs(
    records: Sequence[dict],
    similarity_threshold: float = CANDIDATE_SIMILARITY,
    max_candidates: int = MAX_CANDIDATES_PER_RECORD,
) -> List[Tuple[int, int]]:
    """Generate candidate index pairs using blocking plus token similarity."""
    buckets: Dict[tuple, List[int]] = defaultdict(list)
    for idx, record in enumerate(records):
        buckets[_candidate_key(record)].append(idx)

    pairs: Set[Tuple[int, int]] = set()
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        for i, a_idx in enumerate(bucket):
            a = records[a_idx]
            scored = []
            for b_idx in bucket[i + 1:]:
                sim = _candidate_similarity(a, records[b_idx])
                if sim >= similarity_threshold:
                    scored.append((sim, b_idx))
            scored.sort(reverse=True)
            for _sim, b_idx in scored[:max_candidates]:
                pairs.add((a_idx, b_idx))
    return sorted(pairs)


def _full_scan_duplicate_pairs(
    records: Sequence[dict],
    settings: Optional[dict],
    atom_mode: bool = False,
) -> Optional[List[Tuple[int, int]]]:
    """Let the LLM compare all records in a small group at once.

    Returns the accepted pairs, or ``None`` when the scan was not attempted or
    failed (so the caller can fall back to lexical candidates).
    """
    if not settings:
        return None
    if len(records) < 2 or len(records) > FULL_SCAN_LIMIT:
        return []

    if atom_mode:
        rules = """1. 先看 applicable_assets：来源资产有交集时，只有同一责任主体、同一保护对象、同一动作/控制目标、同一 requirement_scope、同一 owner、同一 allocated_to、同一 verification_method、同一 acceptance_criteria、同一适用条件，才算重复。
2. 来源资产不相交时，按控制语义判断：同一保护对象、同一动作/控制目标、同一适用条件才算重复。两条记录的主体可以分别命名两个资产上承担相同职责的对应组件；allocated_to、verification_method、acceptance_criteria 可以不同，合并后保留全部值。
3. 原子级只有语义和文字内容完全相同的重复才可合并；条件、枚举、字段不同（例如“未授权修改”与“未授权删除”）必须保持独立原子，不得合并。
4. 不同动作必须分开：认证、授权、审计、拒绝、记录、告警、证据保留、资源限制、会话终止、数据遮蔽、数据排除、默认禁用与临时启用；保护对象不同（固件、日志、会话、命令、内存数据等）也不得合并。
5. 只是文字相似、部分重叠、条件不同、对象不同、验证不同的，不是重复；不确定时不要输出该对。"""
    else:
        rules = """1. 先看 applicable_assets：来源资产有交集时，只有同一责任主体、同一保护对象、同一行为/控制目标、同一适用条件、同一验证方法、同一验收准则，才算重复。
2. 来源资产不相交时，按控制语义判断：同一保护对象、同一行为/控制目标、同一适用条件才算重复。两条记录的主体可以分别命名两个资产上承担相同职责的对应组件；allocated_to、verification_method、acceptance_criteria 可以不同，合并后保留全部值。
3. 如果两条需求的主体指向功能上不同的对象或不同动作（例如认证 vs 审计、读 vs 写、固件 vs 日志），即使控制目标相似，也不是重复。
4. 一条需求包含多个独立“应/不应/不得”，或只是部分重叠、条件不同的，不是重复。
5. 不能因为文字相似就合并；不确定时不要输出该对。"""
    system_prompt = f"""你是 ISO/SAE 21434 网络安全需求工程师。下面给出同一去重组的全部整条需求，请逐对比较并找出所有真正重复的需求对。
【规则】
{rules}
6. 如果一条记录带有 statements（多个来源语句），必须把它的每条来源语句都与对方全部来源语句比较；只有全部内容都能对应且不丢失细节时才可判重复，否则不是重复。合并后所有来源语句都会保留。
7. 必须比较每一对记录，输出所有重复对，不要只输出最相似的一对。
8. 只输出判断，不重写需求，不新增 ID；不确定时不要输出该对。
【输出 JSON】{{"duplicate_pairs":[{{"a":"record-id","b":"record-id","reason":"..."}}]}}"""

    try:
        parsed = _call_llm_json(
            settings,
            system_prompt,
            {"records": [_record_payload(record) for record in records]},
        )
    except Exception as e:
        print(f"[TARA-AI] 全量重复扫描失败，退回词面候选: {e}")
        return None

    by_id = {str(record.get("record_id") or ""): idx for idx, record in enumerate(records)}
    by_source: Dict[str, int] = {}
    for idx, record in enumerate(records):
        for sid in _source_ids(record):
            by_source.setdefault(sid, idx)
    accepted: List[Tuple[int, int]] = []
    for item in parsed.get("duplicate_pairs", []) or parsed.get("duplicates", []) or []:
        a_id = str(item.get("a") or "").strip()
        b_id = str(item.get("b") or "").strip()
        i = by_id.get(a_id, by_source.get(a_id))
        j = by_id.get(b_id, by_source.get(b_id))
        if i is None or j is None or i == j:
            continue
        if j < i:
            i, j = j, i
        if _records_compatible(records[i], records[j]):
            accepted.append((i, j))
    return accepted


def _judge_pairs(
    records: Sequence[dict],
    pairs: Sequence[Tuple[int, int]],
    settings: Optional[dict],
    atom_mode: bool = False,
) -> List[Tuple[int, int]]:
    """Return accepted duplicate pairs. LLM judges; exact text is fallback."""
    if not pairs:
        return []

    accepted: List[Tuple[int, int]] = []
    # Exact text duplicates do not need an LLM round; still enforce field
    # compatibility.
    unresolved = []
    for i, j in pairs:
        a, b = records[i], records[j]
        if _normalized_text(a) and _normalized_text(a) == _normalized_text(b) and _records_compatible(a, b):
            accepted.append((i, j))
        else:
            unresolved.append((i, j))

    if not unresolved:
        return accepted

    if not settings:
        return accepted

    if atom_mode:
        system_prompt = """你是 ISO/SAE 21434 网络安全需求工程师。请判断候选原子需求对是否真正重复。
【规则】
1. 先看 applicable_assets。来源资产有交集时，只有同一责任主体、同一保护对象、同一动作/控制目标、同一 requirement_scope、同一 owner、同一 allocated_to、同一 verification_method、同一 acceptance_criteria、同一适用条件，才算重复。
2. 来源资产不相交时，按控制语义判断：同一保护对象、同一动作/控制目标、同一适用条件才算重复。主体可以分别命名两个资产上承担相同职责的对应组件；allocated_to、verification_method、acceptance_criteria 可以不同，合并后保留全部值。
3. 原子级只有语义和文字内容完全相同的重复才可合并；条件、枚举、字段不同（例如“未授权修改”与“未授权删除”）必须保持独立原子，不得合并。
4. 不同动作必须分开：认证、授权、审计、拒绝、记录、告警、证据保留、资源限制、会话终止、数据遮蔽、数据排除、默认禁用与临时启用；保护对象不同也不得合并。
5. 只是文字相似、部分重叠、条件不同、对象不同、验证不同的，不是重复；不确定时 duplicate=false。
6. 如果一条记录带有 statements（多个来源语句），必须把它的每条来源语句都与对方全部来源语句比较；只有全部内容都能对应且不丢失细节时才可判重复。合并后所有来源语句都会保留。
7. 只输出判断，不重写需求，不新增 ID。
【输出 JSON】{"decisions":[{"a":"record-id","b":"record-id","duplicate":true,"reason":"..."}]}"""
    else:
        system_prompt = """你是 ISO/SAE 21434 网络安全需求工程师。请判断候选整条需求对是否真正重复。
【规则】
1. 先看 applicable_assets。来源资产有交集时，只有同一责任主体、同一保护对象、同一行为/控制目标、同一适用条件、同一验证方法、同一验收准则，才算重复。
2. 来源资产不相交时，按控制语义判断：同一保护对象、同一行为/控制目标、同一适用条件才算重复。主体可以分别命名两个资产上承担相同职责的对应组件；allocated_to、verification_method、acceptance_criteria 可以不同，合并后保留全部值。
3. 如果两条需求的主体指向功能上不同的对象或不同动作（例如认证 vs 审计、读 vs 写、固件 vs 日志），即使控制目标相似，也不是重复。
4. 一条需求包含多个独立“应/不应/不得”，或只是部分重叠、条件不同的，不是重复。
5. 不能因为文字相似就合并；不确定时 duplicate=false。
6. 如果一条记录带有 statements（多个来源语句），必须把它的每条来源语句都与对方全部来源语句比较；只有全部内容都能对应且不丢失细节时才可判重复。合并后所有来源语句都会保留。
7. 只输出判断，不重写需求，不新增 ID。
【输出 JSON】{"decisions":[{"a":"record-id","b":"record-id","duplicate":true,"reason":"..."}]}"""

    for start in range(0, len(unresolved), PAIR_BATCH_SIZE):
        batch = unresolved[start:start + PAIR_BATCH_SIZE]
        payload = {
            "pairs": [
                {"a": _record_payload(records[i]), "b": _record_payload(records[j])}
                for i, j in batch
            ]
        }
        try:
            parsed = _call_llm_json(settings, system_prompt, payload)
            decisions = parsed.get("decisions", []) or []
            by_key = {}
            for idx, (i, j) in enumerate(batch):
                record_ids = (records[i].get("record_id"), records[j].get("record_id"))
                by_key[frozenset(record_ids)] = (i, j)
                source_ids_i = _source_ids(records[i])
                source_ids_j = _source_ids(records[j])
                if len(source_ids_i) == 1 and len(source_ids_j) == 1:
                    by_key[frozenset((source_ids_i[0], source_ids_j[0]))] = (i, j)
            for decision in decisions:
                a_id = str(decision.get("a") or "").strip()
                b_id = str(decision.get("b") or "").strip()
                key = frozenset((a_id, b_id))
                if key not in by_key:
                    continue
                if not decision.get("duplicate"):
                    continue
                i, j = by_key[key]
                if _records_compatible(records[i], records[j]):
                    accepted.append((i, j))
        except Exception as e:
            print(f"[TARA-AI] 候选重复判断失败，仅保留精确重复: {e}")
    return accepted


def _chunked_full_scan_pairs(
    records: Sequence[dict],
    settings: Optional[dict],
) -> List[Tuple[int, int]]:
    """Atomic dedup for large atom sets: full-scan chunks plus cross-chunk rescue.

    Sending hundreds of atoms as individual candidate pairs is slow and
    expensive.  Chunked all-pairs scans keep each LLM request bounded, and a
    high-similarity cross-chunk pass catches duplicates that straddle chunks.
    """
    accepted: List[Tuple[int, int]] = []
    accepted_set: Set[Tuple[int, int]] = set()
    for start in range(0, len(records), FULL_SCAN_LIMIT):
        chunk = list(records[start:start + FULL_SCAN_LIMIT])
        result = _full_scan_duplicate_pairs(chunk, settings, atom_mode=True)
        if result is None:
            pairs = generate_candidate_pairs(
                chunk, ATOM_CANDIDATE_SIMILARITY, ATOM_MAX_CANDIDATES_PER_RECORD
            )
            result = _judge_pairs(chunk, pairs, settings, atom_mode=True)
        for i, j in result:
            pair = (start + i, start + j)
            if pair not in accepted_set:
                accepted_set.add(pair)
                accepted.append(pair)
        print(f"[TARA-AI] 原子分块扫描 {start // FULL_SCAN_LIMIT + 1}: {len(chunk)} 条")

    cross: List[Tuple[float, int, int]] = []
    exact_cross: List[Tuple[int, int]] = []
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            if i // FULL_SCAN_LIMIT == j // FULL_SCAN_LIMIT:
                continue
            if (
                _normalized_text(records[i])
                and _normalized_text(records[i]) == _normalized_text(records[j])
            ):
                exact_cross.append((i, j))
            similarity = _candidate_similarity(records[i], records[j])
            if similarity >= 0.60:
                cross.append((similarity, i, j))
    cross.sort(reverse=True)
    exact_set = set(exact_cross)
    cross_pairs = [
        (i, j) for i, j in exact_cross if (i, j) not in accepted_set
    ]
    cross_pairs.extend(
        (i, j) for _similarity, i, j in cross[:40]
        if (i, j) not in accepted_set and (i, j) not in exact_set
    )
    if cross_pairs:
        accepted.extend(_judge_pairs(records, cross_pairs, settings, atom_mode=True))
    return accepted


def _plain_text(record: dict) -> str:
    return re.sub(r"[\s，。；;、,./\\]+", "", _text(record).lower())


def _atom_merge_safe(a: dict, b: dict) -> bool:
    """Atomic merges must not drop a condition or enumeration.

    Two atoms may be merged only when their statements are the same after
    punctuation normalization, or when one statement literally contains the
    other (the longer text then subsumes the shorter).  Conditions such as
    "detect unauthorized modification" vs "detect unauthorized deletion" must
    stay as separate atoms.
    """
    a_text = _plain_text(a)
    b_text = _plain_text(b)
    if not a_text or not b_text:
        return False
    return a_text == b_text or a_text in b_text or b_text in a_text


def _merge_source_texts(records: Sequence[dict], indexes: Sequence[int]) -> List[dict]:
    """Keep every distinct source statement in a merged component.

    A whole-requirement duplicate decision groups near-identical requirements,
    but their wording or detail may differ.  Keeping each distinct statement
    (with its own original source IDs) means the final atomization cannot lose
    content that only one of the merged requirements contained.
    """
    merged: Dict[str, dict] = {}
    order: List[str] = []
    for i in indexes:
        record = records[i]
        entries = record.get("source_texts") or [{
            "text": _text(record),
            "source_requirement_ids": _source_ids(record),
            "subject": record.get("subject") or "",
            "verification_method": record.get("verification_method") or "",
            "acceptance_criteria": record.get("acceptance_criteria") or "",
            "allocated_to": record.get("allocated_to") or "",
        }]
        for entry in entries:
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            key = re.sub(r"\s+", "", text.lower())
            ids = _normalize_id_list(entry.get("source_requirement_ids"))
            if key not in merged:
                merged[key] = {
                    "text": text,
                    "source_requirement_ids": list(ids),
                    "applicable_assets": list(entry.get("applicable_assets") or record.get("applicable_assets") or []),
                    "asset_name": entry.get("asset_name") or record.get("asset_name") or "",
                    "subject": entry.get("subject") or "",
                    "verification_method": entry.get("verification_method") or "",
                    "acceptance_criteria": entry.get("acceptance_criteria") or "",
                    "allocated_to": entry.get("allocated_to") or "",
                }
                order.append(key)
                continue
            target = merged[key]
            for sid in ids:
                if sid not in target["source_requirement_ids"]:
                    target["source_requirement_ids"].append(sid)
            for asset_name in entry.get("applicable_assets") or []:
                if asset_name not in target["applicable_assets"]:
                    target["applicable_assets"].append(asset_name)
            for field in ("verification_method", "acceptance_criteria"):
                value = str(entry.get(field) or "").strip()
                if value and value not in str(target.get(field) or ""):
                    target[field] = "；".join(x for x in (target.get(field), value) if x)
            for field in ("allocated_to",):
                value = str(entry.get(field) or "").strip()
                if value and value not in str(target.get(field) or ""):
                    target[field] = "; ".join(x for x in (target.get(field), value) if x)
    return [merged[key] for key in order]


def _merge_component(records: Sequence[dict], indexes: Sequence[int]) -> dict:
    chosen = sorted(indexes, key=lambda i: ((" / " in _text(records[i])), len(_text(records[i]))), reverse=True)
    rep = dict(records[chosen[0]])
    source_ids = set()
    assets = set()
    merged_from = set()
    allocations: List[str] = []
    for i in indexes:
        source_ids.update(_source_ids(records[i]))
        assets.update(records[i].get("applicable_assets") or [])
        merged_from.update(records[i].get("merged_from") or [records[i].get("record_id")])
        for part in re.split(r"[;；]", str(records[i].get("allocated_to") or "")):
            part = part.strip()
            if part and part not in allocations:
                allocations.append(part)
    # Cross-asset duplicates can legitimately have different allocations,
    # verification methods and acceptance criteria; preserve all of them
    # instead of silently keeping only the representative's values.
    if allocations:
        rep["allocated_to"] = "; ".join(allocations)
    for field in ("verification_method", "acceptance_criteria"):
        values: List[str] = []
        for i in indexes:
            value = str(records[i].get(field) or "").strip()
            if value and value not in values:
                values.append(value)
        if values:
            rep[field] = "；".join(values)
    if not str(rep.get("owner") or "").strip():
        for i in indexes:
            value = str(records[i].get("owner") or "").strip()
            if value:
                rep["owner"] = value
                break
    rep["record_id"] = _content_hash_id(
        "DEDUP_",
        "|".join(sorted(source_ids)) + "||" + "|".join(sorted(x for x in merged_from if x)),
    )
    rep["source_requirement_ids"] = sorted(source_ids)
    rep["source_tara_ids"] = sorted(source_ids)
    rep["source_texts"] = _merge_source_texts(records, indexes)
    rep["applicable_assets"] = sorted(assets)
    rep["merged_from"] = sorted(x for x in merged_from if x)
    rep["_merged_count"] = len(indexes)
    return rep


def dedup_records(records: Sequence[dict], settings: Optional[dict], atom_mode: bool = False, label: str = "") -> List[dict]:
    """Deduplicate a list of records, preserving every source ID."""
    if len(records) < 2:
        return list(records)

    input_ids = {sid for record in records for sid in _source_ids(record)}
    accepted: List[Tuple[int, int]] = []
    if atom_mode and settings and len(records) > FULL_SCAN_LIMIT:
        accepted = _chunked_full_scan_pairs(records, settings)
        full_scanned = True
        pairs = []
    else:
        full_scan_result = None
        if settings and len(records) <= FULL_SCAN_LIMIT:
            full_scan_result = _full_scan_duplicate_pairs(records, settings, atom_mode=atom_mode)
        full_scanned = full_scan_result is not None
        if full_scanned:
            accepted.extend(full_scan_result)
            print(
                f"[TARA-AI] 全量扫描 {label}: {len(records)} 条 -> "
                f"{len(accepted)} 对重复"
            )
        accepted_set = set(accepted)
        threshold = ATOM_CANDIDATE_SIMILARITY if atom_mode else CANDIDATE_SIMILARITY
        max_candidates = ATOM_MAX_CANDIDATES_PER_RECORD if atom_mode else MAX_CANDIDATES_PER_RECORD
        if full_scanned:
            # The full scan already compared every pair in this small group.
            # Only keep deterministic exact-text pairs from the lexical pass; do
            # not spend another LLM round on pairs the full scan already judged.
            pairs = [
                pair for pair in generate_candidate_pairs(records, threshold, max_candidates)
                if pair not in accepted_set
                and _normalized_text(records[pair[0]])
                and _normalized_text(records[pair[0]]) == _normalized_text(records[pair[1]])
            ]
            # Rescue near-identical pairs the full scan may have missed.  These are
            # few and still judged by the LLM, so recall does not depend solely on
            # one large all-pairs response.
            pairs.extend(
                pair for pair in generate_candidate_pairs(records, 0.60, 3)
                if pair not in accepted_set and pair not in pairs
            )
        else:
            pairs = [
                pair for pair in generate_candidate_pairs(records, threshold, max_candidates)
                if pair not in accepted_set
            ]
        if pairs:
            print(f"[TARA-AI] 词面候选 {label}: {len(pairs)} 对")
        accepted.extend(_judge_pairs(records, pairs, settings, atom_mode=atom_mode))
    if atom_mode:
        accepted = [
            (i, j) for i, j in accepted
            if _atom_merge_safe(records[i], records[j])
        ]
    if accepted:
        print(f"[TARA-AI] {label} 接受重复 {len(accepted)} 对")
        for i, j in accepted[:10]:
            print(
                f"    {_source_ids(records[i])} <-> {_source_ids(records[j])}: "
                f"{_text(records[i])[:50]} || {_text(records[j])[:50]}"
            )
    parent = list(range(len(records)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, j in accepted:
        union(i, j)

    components: Dict[int, List[int]] = defaultdict(list)
    for i in range(len(records)):
        components[find(i)].append(i)

    output = []
    for indexes in components.values():
        if len(indexes) == 1:
            output.append(dict(records[indexes[0]]))
        else:
            output.append(_merge_component(records, indexes))

    output_ids = {sid for record in output for sid in _source_ids(record)}
    if input_ids != output_ids:
        raise ValueError(
            f"dedup source traceability broken ({label}): "
            f"missing={sorted(input_ids - output_ids)[:10]}, "
            f"extra={sorted(output_ids - input_ids)[:10]}"
        )
    return output


def hierarchical_dedup(groups: List[List[dict]], settings: Optional[dict]) -> List[dict]:
    """Reduce to at most four groups, then let K1 absorb K2/K3/K4 in order.

    This follows the requested 16 -> 8 -> 4 structure.  Each absorb call
    deduplicates the combined group, so the accumulator always contains the
    deduplicated union of everything compared so far.
    """
    groups = [list(g) for g in groups if g]
    if not groups:
        return []
    round_no = 0
    while len(groups) > 4:
        round_no += 1
        next_groups = []
        for i in range(0, len(groups), 2):
            if i + 1 < len(groups):
                combined = groups[i] + groups[i + 1]
                next_groups.append(dedup_records(combined, settings, atom_mode=False, label=f"round-{round_no}-pair-{i // 2}"))
            else:
                next_groups.append(groups[i])
        groups = next_groups
        print(f"[TARA-AI] 层级去重第 {round_no} 轮: {len(groups)} 组")
    if len(groups) == 1:
        return groups[0]
    acc = groups[0]
    for index, group in enumerate(groups[1:], start=2):
        acc = dedup_records(acc + group, settings, atom_mode=False, label=f"absorb-K{index}")
        print(f"[TARA-AI] K1 吸收 K{index} 后: {len(acc)} 条")
    return acc


def _atomization_units(records: Sequence[dict]) -> List[dict]:
    """Expand merged records into one atomization unit per distinct statement.

    A merged whole-requirement group may contain several distinct source
    statements.  Atomizing only the representative would silently drop details
    that only the other statement contained.  Each unit therefore carries its
    own original source IDs and metadata; identical atoms are merged later by
    the atomic-level dedup.
    """
    units: List[dict] = []
    for record in records:
        entries = record.get("source_texts")
        if not entries:
            entries = [{
                "text": _text(record),
                "source_requirement_ids": _source_ids(record),
                "subject": record.get("subject") or "",
                "verification_method": record.get("verification_method") or "",
                "acceptance_criteria": record.get("acceptance_criteria") or "",
                "allocated_to": record.get("allocated_to") or "",
            }]
        for index, entry in enumerate(entries):
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            unit = dict(record)
            unit["parent_record_id"] = record.get("record_id")
            unit["record_id"] = f"{record.get('record_id')}::T{index}"
            unit["text"] = text
            unit["cybersecurity_requirement"] = text
            unit["_cross_asset_merged"] = bool(
                len(record.get("source_texts") or []) > 1
                or len(record.get("applicable_assets") or []) > 1
            )
            unit["source_requirement_ids"] = (
                _normalize_id_list(entry.get("source_requirement_ids"))
                or _source_ids(record)
            )
            for field in ("subject", "verification_method", "acceptance_criteria", "allocated_to", "asset_name"):
                if entry.get(field):
                    unit[field] = entry[field]
            if entry.get("applicable_assets"):
                unit["applicable_assets"] = list(entry["applicable_assets"])
            units.append(unit)
    return units


def atomize_records(records: Sequence[dict], settings: Optional[dict]) -> List[dict]:
    """Atomize the final deduplicated whole requirements, preserving parent IDs."""
    if not records:
        return []
    units = _atomization_units(records)
    system_prompt = """你是 ISO/SAE 21434 网络安全需求工程师。请对最终去重后的整条需求做原子化拆解。
【规则】
1. 输入中的每一条 record 都是一条独立来源语句；必须逐条拆解，不得跳过，也不得把不同 record 合并。
2. 一条原子需求 = 一个责任主体 + 一个保护对象 + 一个行为 + 一个可验证结果。
3. subject 必须是组件、系统、模块或角色；不得使用对象、事件、数据、条件、日志、命令、会话、身份、文件、资源。
4. 一条原子需求只能有一个行为；出现“并/且/以及/同时”连接不同动作/对象/结果时拆条。
5. 认证、授权、审计、日志、恢复、配置、资源限制必须分开。
6. statement 必须中英文对照，格式“中文 / English”。
7. 每条原子的 source_requirement_ids 必须继承其 parent_record 的 source_requirement_ids；不得丢失、不得编造。
8. 保留 parent 的 scope/owner/allocated/verification/acceptance 等字段。
【输出 JSON】{"atomic_requirements":[{"parent_record_id":"...","atom_id":"...","subject":"...","statement":"...","source_requirement_ids":["..."],"requirement_scope":"...","owner":"...","allocated_to":"...","verification_method":"...","acceptance_criteria":"..."}]}"""

    atoms = []
    total_batches = max(1, (len(units) + ATOM_BATCH_SIZE - 1) // ATOM_BATCH_SIZE)
    for start in range(0, len(units), ATOM_BATCH_SIZE):
        batch = units[start:start + ATOM_BATCH_SIZE]
        batch_no = start // ATOM_BATCH_SIZE + 1
        print(f"[TARA-AI] 原子化批次 {batch_no}/{total_batches}: {len(batch)} 条")
        payload = [
            {
                "record_id": unit.get("record_id"),
                "text": _text(unit),
                "source_requirement_ids": _source_ids(unit),
                "subject": unit.get("subject") or "",
                "requirement_scope": unit.get("requirement_scope") or "",
                "owner": unit.get("owner") or "",
                "allocated_to": unit.get("allocated_to") or "",
                "verification_method": unit.get("verification_method") or "",
                "acceptance_criteria": unit.get("acceptance_criteria") or "",
            }
            for unit in batch
        ]
        if not settings:
            for unit in batch:
                atoms.extend(_rule_atoms_from_parent(unit))
            continue
        try:
            parsed = _call_llm_json(settings, system_prompt, {"requirements": payload})
            for item in parsed.get("atomic_requirements", []) or parsed.get("atoms", []) or []:
                parent_id = str(item.get("parent_record_id") or "").strip()
                parent = next((u for u in batch if u.get("record_id") == parent_id), None)
                if parent is None:
                    source_ids = set(_normalize_id_list(item.get("source_requirement_ids")))
                    parent = next((u for u in batch if set(_source_ids(u)) & source_ids), None)
                if parent is None:
                    continue
                statement = str(item.get("statement") or item.get("cybersecurity_requirement") or "").strip()
                if not statement:
                    continue
                atoms.append(_atom_from_parent(parent, statement, item))
        except Exception as e:
            print(f"[TARA-AI] 原子化批次 {start // ATOM_BATCH_SIZE + 1} 失败，规则兜底: {e}")
            for unit in batch:
                atoms.extend(_rule_atoms_from_parent(unit))

    # Every unit's source IDs must survive atomization.
    covered = {sid for atom in atoms for sid in _source_ids(atom)}
    for unit in units:
        missing = [sid for sid in _source_ids(unit) if sid not in covered]
        if missing:
            atoms.extend(_rule_atoms_from_parent(unit))
    return atoms


def _atom_from_parent(parent: dict, statement: str, item: dict) -> dict:
    atom = {
        "record_id": item.get("atom_id") or _content_hash_id(
            "ATOM_", f"{parent.get('record_id')}:{statement}"
        ),
        "parent_record_id": parent.get("record_id"),
        "source_requirement_ids": sorted(_source_ids(parent)),
        "source_tara_ids": sorted(_source_ids(parent)),
        "text": statement,
        "cybersecurity_requirement": statement,
        "subject": item.get("subject") or parent.get("subject") or parent.get("asset_name") or "",
        "title": parent.get("title") or statement[:80],
        "sub_title": parent.get("sub_title") or "",
        "asset_name": parent.get("asset_name") or "",
        "applicable_assets": list(parent.get("applicable_assets") or []),
        "requirement_scope": item.get("requirement_scope") or parent.get("requirement_scope") or "",
        "owner": item.get("owner") or parent.get("owner") or "",
        "allocated_to": item.get("allocated_to") or parent.get("allocated_to") or "",
        "allocation_type": parent.get("allocation_type") or "",
        "verification_method": item.get("verification_method") or parent.get("verification_method") or "",
        "acceptance_criteria": item.get("acceptance_criteria") or parent.get("acceptance_criteria") or "",
        "requirement_type": parent.get("requirement_type") or "product",
        "goal_ids": parent.get("goal_ids") or [],
        "control_ids": parent.get("control_ids") or [],
        "claim_ids": parent.get("claim_ids") or [],
        "source_ids": parent.get("source_ids") or [],
        "dependencies": parent.get("dependencies") or [],
        "rationale": parent.get("rationale") or "",
        "open_issue": parent.get("open_issue") or "",
        "merged_from": parent.get("merged_from") or [],
    }
    fallback_asset = "系统" if parent.get("_cross_asset_merged") else (parent.get("asset_name") or "")
    return _rewrite_atom_subject(
        SimpleNamespace(name=fallback_asset), atom
    )


def _rule_atoms_from_parent(parent: dict) -> List[dict]:
    text = _text(parent)
    parsed = extract_subject_and_clauses(text)
    if not parsed:
        parsed = [("", text, text)]
    atoms = []
    for subject, _clause, statement in parsed:
        atoms.append(_atom_from_parent(parent, statement, {"subject": subject}))
    return atoms


def validate_final(finals: Sequence[dict], expected_ids: Set[str]) -> dict:
    observed = {sid for row in finals for sid in _source_ids(row)}
    texts = [_normalized_text(row) for row in finals if _normalized_text(row)]
    high_similarity_pairs = 0
    for i in range(len(finals)):
        for j in range(i + 1, len(finals)):
            if _candidate_similarity(finals[i], finals[j]) >= 0.80:
                high_similarity_pairs += 1
    return {
        "input_count": len(expected_ids),
        "output_count": len(finals),
        "covered_count": len(observed & expected_ids),
        "missing": sorted(expected_ids - observed),
        "extra": sorted(observed - expected_ids),
        "all_covered": expected_ids == observed,
        "multi_source_rows": sum(1 for row in finals if len(_source_ids(row)) > 1),
        "merged_rows": sum(1 for row in finals if int(row.get("_merged_count") or 1) > 1),
        "whole_grouped_rows": sum(1 for row in finals if len(row.get("merged_from") or []) > 1),
        "exact_duplicate_text_rows": len(texts) - len(set(texts)),
        "high_similarity_pairs": high_similarity_pairs,
    }


def run_pipeline(
    assets_data: Sequence[dict],
    settings: Optional[dict],
    req_map: Optional[dict] = None,
) -> Tuple[List[dict], dict]:
    """Run per-asset dedup -> hierarchical dedup -> atomize -> atomic dedup.

    assets_data: [{"asset": asset_or_dict, "requirements": [stage5 requirement dicts]}]
    """
    expected_ids: Set[str] = set()
    per_asset_groups: List[List[dict]] = []
    for item in assets_data:
        asset = item["asset"]
        requirements = item.get("requirements") or []
        records = []
        for requirement in requirements:
            record = make_record(asset, requirement, req_map)
            expected_ids.update(_source_ids(record))
            if _text(record):
                records.append(record)
        if not records:
            continue
        deduped = dedup_records(records, settings, atom_mode=False, label=f"asset-{_asset_attr(asset, 'id', 0)}")
        per_asset_groups.append(deduped)
        print(f"[TARA-AI] 资产 [{_asset_attr(asset, 'name', '')}] {len(records)} -> {len(deduped)}")

    final_group = hierarchical_dedup(per_asset_groups, settings)
    print(f"[TARA-AI] 整条需求最终组: {len(final_group)} 条")
    atoms = atomize_records(final_group, settings)
    print(f"[TARA-AI] 原子化: {len(atoms)} 条")
    atoms = dedup_records(atoms, settings, atom_mode=True, label="atomic-dedup")
    print(f"[TARA-AI] 原子级去重: {len(atoms)} 条")
    report = validate_final(atoms, expected_ids)
    if not report["all_covered"]:
        raise ValueError(f"最终追溯断裂: {report}")
    return atoms, report


def source_ids_of(record: dict) -> List[str]:
    return _source_ids(record)
