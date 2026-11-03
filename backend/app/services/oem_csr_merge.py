"""OEM requirements + fixed CSR catalog merge.

The fixed CSR catalog is the canonical "our" requirement set.  This module:
  1. atomizes OEM requirements with the LLM (original language preserved);
  2. maps each OEM atom to the fixed catalog (full / partial / gap);
  3. builds one final table containing:
       - same/partial: fixed CSR + OEM source;
       - tara_only: fixed CSR reached by TARA mapping but not by OEM;
       - catalog_only: fixed CSR not reached by either source;
       - customer_only: OEM atom with no sufficient fixed CSR;
       - tara_gap: TARA requirement that has no fixed CSR target.

The free-form "17-category alignment" path is intentionally not used here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.services.csr_mapping import map_tara_requirements
from app.services.csr_synthesis import _call_llm_json
from app.services.req_merge import atomize_and_dedup_customer_requirements

OEM_ATOM_BATCH_SIZE = 20
OEM_MAPPING_BATCH_SIZE = 25


def _norm_ids(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        parts = [str(x) for x in value]
    else:
        parts = re.split(r"[;,，；\n|]+", str(value))
    result: List[str] = []
    for part in parts:
        part = part.strip()
        if part and part not in result:
            result.append(part)
    return result


def _content_hash(prefix: str, text: str) -> str:
    return prefix + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8].upper()


def _plain(text: str) -> str:
    return re.sub(r"[\s，。；;、,./\\]+", "", str(text or "").lower())


def _atomization_prompt() -> str:
    return """你是汽车网络安全需求工程专家。请把下面一批 OEM（客户）信息安全需求拆解为原子需求。

【规则】
1. 一条原子需求 = 一个责任主体 + 一个保护对象 + 一个行为 + 一个可验证结果。
2. 复合句中出现“并/且/以及/同时”连接不同动作、不同对象或不同结果时，必须拆条。
3. 不同动作必须分开：认证、授权、审计、拒绝、记录、告警、证据保留、资源限制、会话终止、数据遮蔽、数据排除、默认禁用与临时启用。
4. 保留 OEM 原文的语言和含义；不得新增、改写、概括或遗漏；不要翻译。
5. 每条原子的 statement 必须是完整陈述：要有明确的主体（谁）、对象（对什么）和动作/结果；
   原文中的代词（其/该/它）必须替换为具体对象，例如“防止其被获取”必须写成
   “防止密码文件被未授权获取”，不得输出这种没有对象的碎片。
6. 拆分并列动作时，每条原子都要补全主体与对象，不能只保留“防止…”“应…”这类片段。
7. 每条原子的 source_customer_ids 必须来自输入中对应 OEM 需求的 external_id；不得编造。
8. 输入中的每一个 external_id 必须至少出现在一条原子中。
7. 只输出 JSON，不要输出 Markdown。

【输出 JSON】
{
  "atoms": [
    {
      "source_customer_ids": ["OEM-REQ-001"],
      "subject": "责任主体",
      "statement": "原子需求原文",
      "category": "需求分类（可继承）",
      "section": "章节（可继承）"
    }
  ]
}"""


def _fallback_atoms_for_requirements(customer_reqs: Sequence[dict]) -> List[dict]:
    """Deterministic fallback used only when the LLM is unavailable/fails."""
    deterministic = atomize_and_dedup_customer_requirements(list(customer_reqs))
    atoms = []
    for item in deterministic:
        statement = str(item.get("clause") or "").strip()
        if not statement:
            continue
        source_ids = sorted(item.get("source_customer_ids") or [])
        atoms.append({
            "source_customer_ids": source_ids,
            "subject": item.get("subject") or "",
            "statement": statement,
            "source_text": statement,
            "category": item.get("category") or "",
            "section": item.get("section") or "",
        })
    return atoms


def _validate_atoms(batch: Sequence[dict], parsed: dict) -> Tuple[List[dict], Set[str]]:
    input_ids = {str(item["external_id"]) for item in batch if item.get("external_id")}
    text_by_id = {
        str(item["external_id"]): str(item.get("text") or "")
        for item in batch
        if item.get("external_id")
    }
    raw_atoms = parsed.get("atoms") or parsed.get("requirements") or []
    atoms: List[dict] = []
    covered: Set[str] = set()
    for item in raw_atoms:
        if not isinstance(item, dict):
            continue
        source_ids = [
            sid for sid in _norm_ids(item.get("source_customer_ids") or item.get("customer_ids"))
            if sid in input_ids
        ]
        statement = str(item.get("statement") or item.get("requirement_text") or "").strip()
        if not source_ids or not statement:
            continue
        covered.update(source_ids)
        source_text = "；".join(
            dict.fromkeys(text_by_id[sid] for sid in source_ids if text_by_id.get(sid))
        )
        atoms.append({
            "source_customer_ids": source_ids,
            "subject": str(item.get("subject") or "").strip(),
            "statement": statement,
            "source_text": source_text,
            "category": str(item.get("category") or "").strip(),
            "section": str(item.get("section") or "").strip(),
        })
    return atoms, covered


def _atomize_batch(batch: Sequence[dict], settings: Optional[dict], depth: int = 0) -> List[dict]:
    if not settings:
        return _fallback_atoms_for_requirements(batch)
    payload = {
        "requirements": [
            {
                "external_id": item.get("external_id") or "",
                "section": item.get("section") or "",
                "category": item.get("category") or "",
                "text": item.get("text") or "",
            }
            for item in batch
        ]
    }
    try:
        parsed = _call_llm_json(settings, _atomization_prompt(), payload, temperature=0.0)
        atoms, covered = _validate_atoms(batch, parsed)
        missing = {
            str(item["external_id"])
            for item in batch
            if item.get("external_id") and str(item["external_id"]) not in covered
        }
        if missing and len(batch) > 1 and depth < 2:
            mid = len(batch) // 2
            return (
                _atomize_batch(batch[:mid], settings, depth + 1)
                + _atomize_batch(batch[mid:], settings, depth + 1)
            )
        # Preserve any OEM requirement the model omitted.
        if missing:
            fallback_reqs = [
                item for item in batch if str(item.get("external_id")) in missing
            ]
            atoms.extend(_fallback_atoms_for_requirements(fallback_reqs))
        return atoms
    except Exception as exc:
        if len(batch) > 1 and depth < 2:
            mid = len(batch) // 2
            return (
                _atomize_batch(batch[:mid], settings, depth + 1)
                + _atomize_batch(batch[mid:], settings, depth + 1)
            )
        print(f"[TARA-AI] OEM 原子化失败，使用规则兜底: {exc}")
        return _fallback_atoms_for_requirements(batch)


def _dedupe_atoms(atoms: Sequence[dict]) -> List[dict]:
    """Only merge exact/subsumed OEM statements; never drop a condition."""
    merged: List[dict] = []
    for atom in atoms:
        key = _plain(atom["statement"])
        if not key:
            continue
        matched = None
        for existing in merged:
            other = _plain(existing["statement"])
            if not other:
                continue
            if key == other or key in other or other in key:
                matched = existing
                break
        if matched is None:
            merged.append(dict(atom, source_customer_ids=list(atom["source_customer_ids"])))
            continue
        for sid in atom["source_customer_ids"]:
            if sid not in matched["source_customer_ids"]:
                matched["source_customer_ids"].append(sid)
        if len(atom["statement"]) > len(matched["statement"]):
            matched["statement"] = atom["statement"]
            matched["subject"] = atom.get("subject") or matched.get("subject") or ""
        source_text = atom.get("source_text") or ""
        if source_text and source_text not in (matched.get("source_text") or ""):
            matched["source_text"] = "；".join(
                dict.fromkeys(filter(None, [matched.get("source_text"), source_text]))
            )
    for atom in merged:
        atom["source_customer_ids"] = sorted(atom["source_customer_ids"])
        atom["atom_id"] = _content_hash(
            "OEM_ATOM_",
            atom["statement"] + "|" + "|".join(atom["source_customer_ids"]),
        )
    return merged


def atomize_oem_requirements(
    customer_reqs: Sequence[dict],
    settings: Optional[dict],
    batch_size: int = OEM_ATOM_BATCH_SIZE,
) -> dict:
    """Atomize OEM requirements and preserve every original customer ID."""
    batches = [
        list(customer_reqs[start:start + batch_size])
        for start in range(0, len(customer_reqs), batch_size)
    ]
    atoms: List[dict] = []
    for index, batch in enumerate(batches, start=1):
        print(f"[TARA-AI] OEM 原子化批次 {index}/{len(batches)}: {len(batch)} 条")
        atoms.extend(_atomize_batch(batch, settings))
    atoms = _dedupe_atoms(atoms)
    input_ids = {str(item["external_id"]) for item in customer_reqs if item.get("external_id")}
    covered = {sid for atom in atoms for sid in atom["source_customer_ids"]}
    missing = sorted(input_ids - covered)
    return {
        "atoms": atoms,
        "input_count": len(input_ids),
        "atom_count": len(atoms),
        "covered_count": len(input_ids & covered),
        "missing_customer_ids": missing,
    }


def map_oem_atoms_to_catalog(
    atoms: Sequence[dict],
    catalog: Sequence[dict],
    settings: dict,
    batch_size: int = OEM_MAPPING_BATCH_SIZE,
    max_workers: int = 4,
) -> dict:
    """Map OEM atoms to fixed CSRs; returns atom-level mapping rows."""
    requirements = [
        {
            "tara_req_id": atom["atom_id"],
            "source_tara_ids": list(atom["source_customer_ids"]),
            "text": atom["statement"],
            "source_text": atom.get("source_text") or "",
            "title": atom.get("subject") or "",
            "asset_id": 0,
            "asset_name": "OEM",
            "asset_type": "OEM",
            "protocol": "",
            "asset_description": "",
            "requirement_scope": "",
            "owner": "OEM",
            "allocated_to": "",
        }
        for atom in atoms
    ]
    return map_tara_requirements(
        requirements,
        catalog,
        settings,
        batch_size=batch_size,
        max_workers=max_workers,
        source_label="OEM",
        allow_conflict=True,
    )


def _row_attr(row, name: str, default=""):
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _tara_source_ids(row) -> List[str]:
    ids = _norm_ids(_row_attr(row, "source_tara_ids"))
    primary = str(_row_attr(row, "tara_req_id") or "").strip()
    if primary and primary not in ids:
        ids.append(primary)
    return ids


def _coverage_verification_prompt() -> str:
    return """你是汽车网络安全需求评审专家。下面给出若干条 OEM 信息安全需求、它们的原子需求，
以及每个原子已映射到的固定 CSR（候选）。

请判断：**这些候选 CSR 的联合**是否完整覆盖了该 OEM 需求（注意是联合覆盖，不是单条覆盖）。

【判定标准】
1. full：所有原子需求都能由候选 CSR 覆盖（可以是一条或多条联合），没有遗漏。
2. partial：候选覆盖了大部分内容，但仍缺少某个具体方面；在 missing_aspect 写清缺什么。
3. gap：候选为空，或候选与需求无关。
4. target_csr_ids 只能从候选里选，不得引入候选之外的 ID；full 时至少给出一条。
5. rationale 用一两句话说明依据。
6. 只输出 JSON，不要输出 Markdown。

【输出 JSON】
{
  "requirements": [
    {
      "requirement_id": "OEM-REQ-001",
      "coverage": "full",
      "target_csr_ids": ["CS-HW-001", "CS-SYS-010"],
      "missing_aspect": "",
      "rationale": "……"
    }
  ]
}"""


def _heuristic_requirement_coverage(
    customer_reqs: Sequence[dict],
    atoms: Sequence[dict],
    oem_mapping: dict,
) -> dict:
    """Fallback when the LLM is unavailable: strict atom-level aggregation."""
    rows_by_atom: Dict[str, List[dict]] = defaultdict(list)
    for row in oem_mapping.get("rows") or []:
        rows_by_atom[str(row.get("tara_req_id") or "")].append(row)

    atoms_by_customer: Dict[str, List[dict]] = defaultdict(list)
    for atom in atoms:
        for cid in atom["source_customer_ids"]:
            atoms_by_customer[cid].append(atom)

    result = {}
    for req in customer_reqs:
        cid = str(req.get("external_id") or "")
        if not cid:
            continue
        targets: List[str] = []
        missing: List[str] = []
        for atom in atoms_by_customer.get(cid, []):
            for row in rows_by_atom.get(atom["atom_id"], []):
                target = str(row.get("target_csr_id") or "").strip()
                if target and target not in targets:
                    targets.append(target)
                gap = str(row.get("missing_aspect") or "").strip()
                if gap and gap not in missing:
                    missing.append(gap)
        if not atoms_by_customer.get(cid):
            coverage = "gap"
        elif not targets:
            coverage = "gap"
        elif any(str(row.get("match_type")) == "conflict"
                 for atom in atoms_by_customer.get(cid, [])
                 for row in rows_by_atom.get(atom["atom_id"], [])):
            coverage = "partial"
        elif missing:
            coverage = "partial"
        else:
            coverage = "full"
        result[cid] = {
            "coverage": coverage,
            "target_csr_ids": targets,
            "missing_aspect": "；".join(missing),
            "rationale": "规则聚合（未启用大模型复核）",
        }
    return result


def verify_oem_requirement_coverage(
    customer_reqs: Sequence[dict],
    atoms: Sequence[dict],
    oem_mapping: dict,
    catalog: Sequence[dict],
    settings: Optional[dict],
    batch_size: int = 8,
) -> dict:
    """Requirement-level check: can the mapped CSRs jointly cover each OEM requirement?

    Atom-level mapping is deliberately allowed to be partial; this pass combines the
    mapped CSRs per OEM requirement so a requirement covered by several CSRs is still
    treated as covered (per the agreed rule).
    """
    if not settings:
        return _heuristic_requirement_coverage(customer_reqs, atoms, oem_mapping)

    catalog_by_id = {row["csr_id"]: row for row in catalog}
    rows_by_atom: Dict[str, List[dict]] = defaultdict(list)
    for row in oem_mapping.get("rows") or []:
        rows_by_atom[str(row.get("tara_req_id") or "")].append(row)
    atoms_by_customer: Dict[str, List[dict]] = defaultdict(list)
    for atom in atoms:
        for cid in atom["source_customer_ids"]:
            atoms_by_customer[cid].append(atom)

    items = []
    for req in customer_reqs:
        cid = str(req.get("external_id") or "")
        if not cid:
            continue
        atom_items = []
        candidates: List[str] = []
        for atom in atoms_by_customer.get(cid, []):
            atom_rows = rows_by_atom.get(atom["atom_id"], [])
            targets = [
                str(r.get("target_csr_id") or "") for r in atom_rows
                if r.get("target_csr_id")
            ]
            for target in targets:
                if target not in candidates:
                    candidates.append(target)
            atom_items.append({
                "atom": atom["statement"],
                "match_type": (atom_rows[0].get("match_type") if atom_rows else "gap"),
                "target_csr_ids": targets,
                "missing_aspect": "；".join(
                    str(r.get("missing_aspect") or "").strip()
                    for r in atom_rows if str(r.get("missing_aspect") or "").strip()
                ),
            })
        items.append({
            "requirement_id": cid,
            "text": req.get("text") or "",
            "atoms": atom_items,
            "candidates": [
                {
                    "csr_id": cid2,
                    "name": catalog_by_id.get(cid2, {}).get("name", ""),
                    "description": catalog_by_id.get(cid2, {}).get("description", ""),
                }
                for cid2 in candidates
            ],
        })

    result: Dict[str, dict] = {}
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        payload = {"requirements": batch}
        try:
            parsed = _call_llm_json(
                settings, _coverage_verification_prompt(), payload, temperature=0.0
            )
        except Exception as exc:  # noqa: BLE001 - fall back per batch
            print(f"[TARA-AI] OEM 需求级覆盖复核失败，使用规则兜底: {exc}")
            fallback = _heuristic_requirement_coverage(
                [{"external_id": item["requirement_id"], "text": item["text"]} for item in batch],
                atoms,
                oem_mapping,
            )
            result.update(fallback)
            continue

        by_id = {}
        for entry in parsed.get("requirements") or []:
            if isinstance(entry, dict):
                rid = str(entry.get("requirement_id") or "").strip()
                if rid:
                    by_id[rid] = entry

        for item in batch:
            rid = item["requirement_id"]
            entry = by_id.get(rid) or {}
            coverage = str(entry.get("coverage") or "").strip().lower()
            if coverage not in {"full", "partial", "gap"}:
                coverage = "partial" if item["candidates"] else "gap"
            candidate_ids = {c["csr_id"] for c in item["candidates"]}
            targets = [
                str(t).strip() for t in (entry.get("target_csr_ids") or [])
                if str(t).strip() in candidate_ids or str(t).strip() in catalog_by_id
            ]
            if coverage == "full" and not targets:
                coverage = "partial"
            if not targets and coverage == "full":
                coverage = "gap"
            result[rid] = {
                "coverage": coverage,
                "target_csr_ids": targets,
                "missing_aspect": str(entry.get("missing_aspect") or "").strip(),
                "rationale": str(entry.get("rationale") or "").strip(),
            }
    return result


def build_final_rows(
    catalog: Sequence[dict],
    tara_mapping_rows: Sequence,
    atoms: Sequence[dict],
    oem_mapping: dict,
    requirement_coverage: Optional[dict] = None,
) -> Tuple[List[dict], List[dict], List[dict], dict]:
    """Build the final table: one row per fixed CSR, plus conflicts / OEM gaps.

    The fixed CSR catalog is the canonical requirement set (TARA requirements
    are mapped onto it).  OEM atoms only enrich those rows; atoms that cannot
    be fully placed are returned separately instead of being appended to the
    final table.
    """
    atom_by_id = {atom["atom_id"]: atom for atom in atoms}
    tara_by_target: Dict[str, List] = defaultdict(list)
    tara_gaps: Dict[str, dict] = {}
    for row in tara_mapping_rows:
        target = str(_row_attr(row, "target_csr_id") or "").strip()
        text = str(_row_attr(row, "tara_text") or "").strip()
        source_ids = _tara_source_ids(row)
        if target:
            tara_by_target[target].append(row)
        else:
            primary = str(_row_attr(row, "tara_req_id") or "").strip()
            if primary and primary not in tara_gaps:
                tara_gaps[primary] = {
                    "tara_req_id": primary,
                    "source_tara_ids": source_ids,
                    "tara_text": text,
                    "rationale": str(_row_attr(row, "rationale") or ""),
                    "missing_aspect": str(_row_attr(row, "missing_aspect") or ""),
                }

    oem_by_target: Dict[str, List[Tuple[dict, dict]]] = defaultdict(list)
    oem_conflicts_by_target: Dict[str, List[Tuple[dict, dict]]] = defaultdict(list)
    oem_by_atom: Dict[str, List[dict]] = defaultdict(list)
    for row in oem_mapping.get("rows") or []:
        atom_id = str(row.get("tara_req_id") or "").strip()
        atom = atom_by_id.get(atom_id)
        if atom is None:
            continue
        oem_by_atom[atom_id].append(row)
        target = str(row.get("target_csr_id") or "").strip()
        if target and str(row.get("match_type")) == "conflict":
            oem_conflicts_by_target[target].append((atom, row))
        elif target:
            oem_by_target[target].append((atom, row))

    coverage_by_id = requirement_coverage or {}
    covered_requirement_ids = {
        cid for cid, info in coverage_by_id.items()
        if str(info.get("coverage") or "").strip().lower() == "full"
    }

    rows: List[dict] = []
    conflicts: List[dict] = []
    resolution_counts: Dict[str, int] = defaultdict(int)
    catalog_partial_count = 0

    for entry in catalog:
        csr_id = entry["csr_id"]
        tara_rows = tara_by_target.get(csr_id, [])
        oem_pairs = oem_by_target.get(csr_id, [])
        conflict_pairs = oem_conflicts_by_target.get(csr_id, [])
        has_tara = bool(tara_rows)
        has_oem = bool(oem_pairs)
        has_conflict = bool(conflict_pairs)
        tara_needs_review = any(
            str(_row_attr(row, "match_type")) == "partial"
            or float(_row_attr(row, "confidence") or 0.0) < 0.6
            for row in tara_rows
        )

        if has_oem:
            if coverage_by_id:
                # 需求级判定：该 CSR 关联的每条 OEM 需求若都已被（可能多条 CSR 联合）覆盖，
                # 这一行就是 same；否则 partial。
                oem_req_ids = {
                    cid for atom, _row in oem_pairs for cid in atom["source_customer_ids"]
                }
                all_full = bool(oem_req_ids) and oem_req_ids <= covered_requirement_ids
            else:
                all_full = all(str(row.get("match_type")) == "full" for _, row in oem_pairs)
            resolution = "same" if all_full else "partial"
            needs_review = (
                (not all_full)
                or tara_needs_review
                or any(
                    float(row.get("confidence") or 0.0) < 0.6
                    for _, row in oem_pairs
                )
            )
        elif has_tara:
            resolution = "tara_only"
            needs_review = tara_needs_review
        else:
            resolution = "catalog_only"
            needs_review = False
        if has_conflict:
            needs_review = True

        customer_ids: Set[str] = set()
        for atom, _row in oem_pairs:
            customer_ids.update(atom["source_customer_ids"])
        tara_ids: Set[str] = set()
        for row in tara_rows:
            tara_ids.update(_tara_source_ids(row))

        rationales: List[str] = []
        missing_aspects: List[str] = []
        oem_texts: List[str] = []
        for atom, row in oem_pairs:
            rationale = str(row.get("rationale") or "").strip()
            if rationale and rationale not in rationales:
                rationales.append(rationale)
            missing = str(row.get("missing_aspect") or "").strip()
            if missing and missing not in missing_aspects:
                missing_aspects.append(missing)
            if atom.get("statement") and atom["statement"] not in oem_texts:
                oem_texts.append(atom["statement"])
        for row in tara_rows:
            rationale = str(_row_attr(row, "rationale") or "").strip()
            if rationale and rationale not in rationales:
                rationales.append(rationale)
            missing = str(_row_attr(row, "missing_aspect") or "").strip()
            if missing and missing not in missing_aspects:
                missing_aspects.append(missing)

        conflict_notes = []
        for atom, row in conflict_pairs:
            note = str(row.get("conflict_note") or "").strip()
            if note and note not in conflict_notes:
                conflict_notes.append(note)
            conflicts.append({
                "oem_atom_id": atom["atom_id"],
                "source_customer_ids": list(atom["source_customer_ids"]),
                "oem_text": atom["statement"],
                "catalog_csr_id": csr_id,
                "catalog_name": entry["name"],
                "catalog_text": entry["description"],
                "confidence": float(row.get("confidence") or 0.0),
                "rationale": str(row.get("rationale") or ""),
                "conflict_note": note or "OEM 与固定 CSR 存在互斥要求",
                "resolution": "pending_review",
            })

        subject = oem_pairs[0][0].get("subject") if oem_pairs else "系统 / System"
        rows.append({
            "final_id": f"FCSR-{csr_id}",
            "requirement_title": entry["name"],
            "subject": subject or "系统 / System",
            "requirement_text": entry["description"],
            "functional_category": entry["module"],
            "security_domain": entry["module"],
            "control_category": entry["name"],
            "source_customer_ids": sorted(customer_ids),
            "source_tara_ids": sorted(tara_ids),
            "source_catalog_csr_ids": [csr_id],
            "resolution": resolution,
            "needs_review": needs_review,
            "confidence": min(
                [float(row.get("confidence") or 0.0) for _, row in oem_pairs]
                or [1.0]
            ),
            "rationale": "；".join(rationales),
            "missing_aspect": "；".join(missing_aspects),
            "conflict": False,
            "conflict_note": "",
            "additional_info": "；".join(
                [x for x in (
                    rationales + missing_aspects
                    + ([f"OEM原文: {' / '.join(oem_texts)}"] if oem_texts else [])
                    + ([f"OEM冲突: {'；'.join(conflict_notes)}"] if conflict_notes else [])
                ) if x]
            ),
        })
        resolution_counts[resolution] += 1
        if resolution == "partial":
            catalog_partial_count += 1

    # OEM 原子需求：没有完整落点（gap / partial / conflict）的单独列出。
    # 有完整落点的原子已经作为来源写进对应的固定 CSR 行，不再重复。
    oem_unmapped: List[dict] = []
    for atom in atoms:
        if coverage_by_id and atom["source_customer_ids"] and all(
            cid in covered_requirement_ids for cid in atom["source_customer_ids"]
        ):
            # 需求级已判定为覆盖：即使单个原子是 partial，也不再列为未覆盖
            continue
        atom_rows = oem_by_atom.get(atom["atom_id"], [])
        normal_rows = [row for row in atom_rows if str(row.get("match_type")) != "conflict"]
        conflict_rows = [row for row in atom_rows if str(row.get("match_type")) == "conflict"]
        targets = [
            str(row.get("target_csr_id") or "") for row in normal_rows
            if row.get("target_csr_id")
        ]
        has_target = bool(targets)
        has_partial = any(str(row.get("match_type")) == "partial" for row in normal_rows)
        has_error = any(str(row.get("match_type")) == "error" for row in normal_rows)
        has_conflict = bool(conflict_rows)
        if has_target and not has_partial and not has_error:
            # 完整落点（即使同时存在冲突）已进入最终需求表 / Merge_Conflicts
            continue
        status = "conflict" if has_conflict else ("partial" if has_partial else "unmapped")
        missing = [
            str(row.get("missing_aspect") or "").strip()
            for row in normal_rows
            if str(row.get("missing_aspect") or "").strip()
        ]
        rationales = [
            str(row.get("rationale") or "").strip()
            for row in atom_rows
            if str(row.get("rationale") or "").strip()
        ]
        conflict_notes = [
            str(row.get("conflict_note") or "").strip()
            for row in conflict_rows
            if str(row.get("conflict_note") or "").strip()
        ]
        oem_unmapped.append({
            "oem_atom_id": atom["atom_id"],
            "source_customer_ids": list(atom["source_customer_ids"]),
            "oem_text": atom["statement"],
            "category": atom.get("category") or "",
            "status": status,
            "target_csr_ids": targets,
            "missing_aspect": "；".join(missing),
            "rationale": "；".join(rationales),
            "conflict_note": "；".join(conflict_notes),
            "confidence": max(
                [float(row.get("confidence") or 0.0) for row in atom_rows] or [0.0]
            ),
        })

    input_customer_ids = {sid for atom in atoms for sid in atom["source_customer_ids"]}
    customer_covered = {
        sid for row in rows for sid in row["source_customer_ids"]
    }
    customer_unmapped_ids = {
        sid for item in oem_unmapped for sid in item["source_customer_ids"]
    }
    input_tara_ids = {
        sid for row in tara_mapping_rows for sid in _tara_source_ids(row)
    }
    tara_covered = {
        sid for row in rows for sid in row["source_tara_ids"]
    }
    tara_gap_ids = {
        sid for gap in tara_gaps.values() for sid in gap["source_tara_ids"]
    }
    report = {
        "customer_input_count": len(input_customer_ids),
        "customer_covered_count": len(input_customer_ids & customer_covered),
        "customer_unmapped_count": len(input_customer_ids & customer_unmapped_ids),
        "customer_missing_ids": sorted(
            input_customer_ids - customer_covered - customer_unmapped_ids
        ),
        "tara_input_count": len(input_tara_ids),
        "tara_covered_count": len(input_tara_ids & tara_covered),
        "tara_missing_ids": sorted(input_tara_ids - tara_covered - tara_gap_ids),
        "catalog_count": len(catalog),
        "final_row_count": len(rows),
        "same_count": resolution_counts["same"],
        "partial_count": resolution_counts["partial"],
        "catalog_partial_count": catalog_partial_count,
        "tara_only_count": resolution_counts["tara_only"],
        "catalog_only_count": resolution_counts["catalog_only"],
        "tara_gap_count": len(tara_gaps),
        "oem_unmapped_count": len(oem_unmapped),
        "oem_unmapped_atom_count": sum(
            1 for item in oem_unmapped if item["status"] == "unmapped"
        ),
        "oem_partial_atom_count": sum(
            1 for item in oem_unmapped if item["status"] == "partial"
        ),
        "oem_conflict_atom_count": sum(
            1 for item in oem_unmapped if item["status"] == "conflict"
        ),
        "oem_requirement_count": len(coverage_by_id),
        "oem_requirement_covered": len(covered_requirement_ids),
        "oem_requirement_partial": sum(
            1 for info in coverage_by_id.values()
            if str(info.get("coverage") or "").strip().lower() == "partial"
        ),
        "oem_requirement_gap": sum(
            1 for info in coverage_by_id.values()
            if str(info.get("coverage") or "").strip().lower() == "gap"
        ),
        "conflict_count": len(conflicts),
        "conflict_atom_count": len({item["oem_atom_id"] for item in conflicts}),
        "all_covered": (
            not (input_customer_ids - customer_covered - customer_unmapped_ids)
            and not (input_tara_ids - tara_covered - tara_gap_ids)
        ),
    }
    return rows, conflicts, oem_unmapped, report


def merge_oem_requirements_with_fixed_csrs(
    db,
    project_id: int,
    domain_id: int,
    customer_reqs: Sequence[dict],
    domain,
    run,
) -> Tuple[List[dict], List[dict], List[dict], dict]:
    """Full OEM + fixed-CSR merge pipeline for the API."""
    from app.models.csr_catalog import TaraCsrMapping
    from app.models.system_settings import SystemSettings
    from app.services.csr_catalog import get_active_catalog, get_active_catalog_version

    version = get_active_catalog_version(db)
    catalog = get_active_catalog(db)
    if version is None or not catalog:
        raise ValueError(
            "尚未导入固定 CSR 目录：请在「系统配置 → 固定 CSR 目录」导入目录 xlsx"
            "（列示例：需求编号 / 分类领域 / 需求描述）。TARA 分析结束时会自动"
            "映射到该目录，之后即可合并需求。"
        )

    tara_rows = (
        db.query(TaraCsrMapping)
        .filter(
            TaraCsrMapping.domain_id == domain_id,
            TaraCsrMapping.run_id == run.id,
            TaraCsrMapping.catalog_version == version.version,
        )
        .all()
    )
    if not tara_rows:
        raise ValueError(
            "该域控/run 还没有 TARA→固定 CSR 映射结果：请重新执行一次 TARA 分析"
            "（映射会在分析结束后自动完成），再合并需求。"
        )

    settings_obj = db.query(SystemSettings).first()
    settings = None
    if settings_obj and settings_obj.api_key:
        settings = {
            "api_base_url": settings_obj.api_base_url,
            "model_name": settings_obj.model_name or "deepseek-flash",
            "api_key": settings_obj.api_key,
        }
    if not settings:
        raise ValueError("系统未配置可用的 LLM，无法执行 OEM 原子化/映射")

    atomized = atomize_oem_requirements(customer_reqs, settings)
    if atomized["missing_customer_ids"]:
        raise ValueError(f"OEM 原子化未覆盖客户需求: {atomized['missing_customer_ids']}")

    mapped = map_oem_atoms_to_catalog(atomized["atoms"], catalog, settings)
    requirement_coverage = verify_oem_requirement_coverage(
        customer_reqs, atomized["atoms"], mapped, catalog, settings
    )
    rows, conflicts, oem_unmapped, report = build_final_rows(
        catalog,
        tara_rows,
        atomized["atoms"],
        mapped,
        requirement_coverage=requirement_coverage,
    )
    report.update({
        "oem_input_count": atomized["input_count"],
        "oem_atom_count": atomized["atom_count"],
        "oem_mapping_rows": mapped["summary"]["mapping_rows"],
        "oem_full_count": mapped["summary"]["full_count"],
        "oem_partial_count": mapped["summary"]["partial_count"],
        "oem_gap_count": mapped["summary"]["gap_count"],
        "oem_error_count": mapped["summary"]["error_count"],
        "oem_conflict_count": mapped["summary"].get("conflict_count", 0),
        "catalog_version": version.version,
    })
    if not report["all_covered"]:
        raise ValueError(f"最终合并追溯断裂: {report}")
    return rows, conflicts, oem_unmapped, report


def persist_merge_conflicts(
    db,
    project_id: int,
    domain_id: int,
    run_id: int,
    conflicts: Sequence[dict],
    replace: bool = True,
) -> int:
    """Persist OEM vs fixed-CSR conflicts for human review (separate table)."""
    from app.models.req_merge import MergeConflict

    if replace:
        db.query(MergeConflict).filter(
            MergeConflict.domain_id == domain_id,
            MergeConflict.run_id == run_id,
        ).delete(synchronize_session=False)

    seen: Set[Tuple] = set()
    for item in conflicts:
        key = (item.get("oem_atom_id"), item.get("catalog_csr_id"))
        if key in seen:
            continue
        seen.add(key)
        db.add(MergeConflict(
            project_id=project_id,
            domain_id=domain_id,
            run_id=run_id,
            oem_atom_id=item.get("oem_atom_id") or "",
            source_customer_ids="; ".join(item.get("source_customer_ids") or []),
            oem_text=item.get("oem_text") or "",
            catalog_csr_id=item.get("catalog_csr_id") or "",
            catalog_name=item.get("catalog_name") or "",
            catalog_text=item.get("catalog_text") or "",
            confidence=float(item.get("confidence") or 0.0),
            rationale=item.get("rationale") or "",
            conflict_note=item.get("conflict_note") or "",
            status="Pending",
        ))
    db.commit()
    return len(seen)


def persist_oem_unmapped_requirements(
    db,
    project_id: int,
    domain_id: int,
    run_id: int,
    items: Sequence[dict],
    replace: bool = True,
) -> int:
    """Persist OEM atoms without a full fixed-CSR landing (separate table)."""
    from app.models.req_merge import OemUnmappedRequirement

    if replace:
        db.query(OemUnmappedRequirement).filter(
            OemUnmappedRequirement.domain_id == domain_id,
            OemUnmappedRequirement.run_id == run_id,
        ).delete(synchronize_session=False)

    seen: Set[str] = set()
    for item in items:
        atom_id = item.get("oem_atom_id") or ""
        if not atom_id or atom_id in seen:
            continue
        seen.add(atom_id)
        db.add(OemUnmappedRequirement(
            project_id=project_id,
            domain_id=domain_id,
            run_id=run_id,
            oem_atom_id=atom_id,
            source_customer_ids="; ".join(item.get("source_customer_ids") or []),
            oem_text=item.get("oem_text") or "",
            category=item.get("category") or "",
            status=item.get("status") or "unmapped",
            target_csr_ids="; ".join(item.get("target_csr_ids") or []),
            missing_aspect=item.get("missing_aspect") or "",
            rationale=item.get("rationale") or "",
            conflict_note=item.get("conflict_note") or "",
            confidence=float(item.get("confidence") or 0.0),
        ))
    db.commit()
    return len(seen)


def collect_oem_unmapped_rows(
    db, domain_id: int, run_id: Optional[int] = None
) -> List[dict]:
    """Return OEM atoms without a full landing for exports/API."""
    from app.models.req_merge import OemUnmappedRequirement

    query = db.query(OemUnmappedRequirement).filter(
        OemUnmappedRequirement.domain_id == domain_id
    )
    if run_id is not None:
        query = query.filter(OemUnmappedRequirement.run_id == run_id)
    rows = query.order_by(OemUnmappedRequirement.id).all()
    return [
        {
            "id": row.id,
            "run_id": row.run_id,
            "oem_atom_id": row.oem_atom_id,
            "source_customer_ids": row.source_customer_ids or "",
            "oem_text": row.oem_text or "",
            "category": row.category or "",
            "status": row.status or "unmapped",
            "target_csr_ids": row.target_csr_ids or "",
            "missing_aspect": row.missing_aspect or "",
            "rationale": row.rationale or "",
            "conflict_note": row.conflict_note or "",
            "confidence": row.confidence,
        }
        for row in rows
    ]


def collect_merge_conflict_rows(db, domain_id: int, run_id: Optional[int] = None) -> List[dict]:
    """Return conflict rows for reports/API; newest run if run_id omitted."""
    from app.models.req_merge import MergeConflict

    query = db.query(MergeConflict).filter(MergeConflict.domain_id == domain_id)
    if run_id is not None:
        query = query.filter(MergeConflict.run_id == run_id)
    rows = query.order_by(MergeConflict.id).all()
    return [
        {
            "id": row.id,
            "run_id": row.run_id,
            "oem_atom_id": row.oem_atom_id,
            "source_customer_ids": row.source_customer_ids or "",
            "oem_text": row.oem_text or "",
            "catalog_csr_id": row.catalog_csr_id or "",
            "catalog_name": row.catalog_name or "",
            "catalog_text": row.catalog_text or "",
            "confidence": row.confidence,
            "rationale": row.rationale or "",
            "conflict_note": row.conflict_note or "",
            "status": row.status or "Pending",
        }
        for row in rows
    ]
