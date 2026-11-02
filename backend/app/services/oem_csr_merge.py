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
5. 每条原子的 source_customer_ids 必须来自输入中对应 OEM 需求的 external_id；不得编造。
6. 输入中的每一个 external_id 必须至少出现在一条原子中。
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
            "category": item.get("category") or "",
            "section": item.get("section") or "",
        })
    return atoms


def _validate_atoms(batch: Sequence[dict], parsed: dict) -> Tuple[List[dict], Set[str]]:
    input_ids = {str(item["external_id"]) for item in batch if item.get("external_id")}
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
        atoms.append({
            "source_customer_ids": source_ids,
            "subject": str(item.get("subject") or "").strip(),
            "statement": statement,
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


def build_final_rows(
    catalog: Sequence[dict],
    tara_mapping_rows: Sequence,
    atoms: Sequence[dict],
    oem_mapping: dict,
) -> Tuple[List[dict], dict]:
    """Build the final OEM + fixed-CSR table with explicit resolutions."""
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
    oem_by_atom: Dict[str, List[dict]] = defaultdict(list)
    for row in oem_mapping.get("rows") or []:
        atom_id = str(row.get("tara_req_id") or "").strip()
        atom = atom_by_id.get(atom_id)
        if atom is None:
            continue
        oem_by_atom[atom_id].append(row)
        target = str(row.get("target_csr_id") or "").strip()
        if target:
            oem_by_target[target].append((atom, row))

    rows: List[dict] = []
    resolution_counts: Dict[str, int] = defaultdict(int)
    catalog_partial_count = 0
    oem_partial_row_count = 0
    oem_only_count = 0

    for entry in catalog:
        csr_id = entry["csr_id"]
        tara_rows = tara_by_target.get(csr_id, [])
        oem_pairs = oem_by_target.get(csr_id, [])
        has_tara = bool(tara_rows)
        has_oem = bool(oem_pairs)
        tara_needs_review = any(
            str(_row_attr(row, "match_type")) == "partial"
            or float(_row_attr(row, "confidence") or 0.0) < 0.6
            for row in tara_rows
        )

        if has_oem:
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
                ) if x]
            ),
        })
        resolution_counts[resolution] += 1
        if resolution == "partial":
            catalog_partial_count += 1

    # OEM requirements that have no target, or only a partial target, must
    # remain visible as OEM-specific rows (the fixed CSR does not fully cover them).
    oem_seq = 0
    for atom in atoms:
        atom_rows = oem_by_atom.get(atom["atom_id"], [])
        targets = [
            str(row.get("target_csr_id") or "") for row in atom_rows
            if row.get("target_csr_id")
        ]
        has_target = bool(targets)
        has_partial = any(str(row.get("match_type")) == "partial" for row in atom_rows)
        has_error = any(str(row.get("match_type")) == "error" for row in atom_rows)
        if has_target and not has_partial and not has_error:
            continue
        oem_seq += 1
        missing = [
            str(row.get("missing_aspect") or "").strip()
            for row in atom_rows
            if str(row.get("missing_aspect") or "").strip()
        ]
        rows.append({
            "final_id": f"FCSR-OEM-{oem_seq:03d}",
            "requirement_title": atom.get("subject") or "OEM 专属需求 / OEM Specific Requirement",
            "subject": atom.get("subject") or "系统 / System",
            "requirement_text": atom["statement"],
            "functional_category": atom.get("category") or "OEM 需求 / OEM Requirement",
            "security_domain": atom.get("category") or "OEM 需求 / OEM Requirement",
            "control_category": "OEM 客户需求 / Customer Requirement",
            "source_customer_ids": list(atom["source_customer_ids"]),
            "source_tara_ids": [],
            "source_catalog_csr_ids": targets,
            "resolution": "customer_only" if not has_target else "partial",
            "needs_review": has_partial or has_error,
            "confidence": max(
                [float(row.get("confidence") or 0.0) for row in atom_rows] or [1.0]
            ),
            "rationale": "",
            "missing_aspect": "；".join(missing),
            "conflict": False,
            "conflict_note": "",
            "additional_info": "；".join(
                [x for x in (
                    ([f"部分映射到: {', '.join(targets)}"] if targets else [])
                    + missing
                ) if x]
            ),
        })
        resolution_counts[rows[-1]["resolution"]] += 1
        oem_only_count += 1
        if rows[-1]["resolution"] == "partial":
            oem_partial_row_count += 1

    # TARA requirements with no fixed CSR target are kept as TARA-specific rows.
    tara_seq = 0
    for gap in tara_gaps.values():
        tara_seq += 1
        rows.append({
            "final_id": f"FCSR-TARA-{tara_seq:03d}",
            "requirement_title": "TARA 专属需求 / TARA Specific Requirement",
            "subject": "系统 / System",
            "requirement_text": gap["tara_text"],
            "functional_category": "TARA 目录缺口 / TARA Catalog Gap",
            "security_domain": "TARA 目录缺口 / TARA Catalog Gap",
            "control_category": "固定 CSR 目录未覆盖 / Not Covered by Fixed CSR",
            "source_customer_ids": [],
            "source_tara_ids": gap["source_tara_ids"],
            "source_catalog_csr_ids": [],
            "resolution": "tara_gap",
            "needs_review": True,
            "confidence": 0.0,
            "rationale": gap.get("rationale") or "",
            "missing_aspect": gap.get("missing_aspect") or "",
            "conflict": False,
            "conflict_note": "",
            "additional_info": "；".join(
                [x for x in (gap.get("rationale"), gap.get("missing_aspect")) if x]
            ),
        })
        resolution_counts["tara_gap"] += 1

    input_customer_ids = {sid for atom in atoms for sid in atom["source_customer_ids"]}
    customer_covered = {
        sid for row in rows for sid in row["source_customer_ids"]
    }
    input_tara_ids = {
        sid for row in tara_mapping_rows for sid in _tara_source_ids(row)
    }
    tara_covered = {
        sid for row in rows for sid in row["source_tara_ids"]
    }
    report = {
        "customer_input_count": len(input_customer_ids),
        "customer_covered_count": len(input_customer_ids & customer_covered),
        "customer_missing_ids": sorted(input_customer_ids - customer_covered),
        "tara_input_count": len(input_tara_ids),
        "tara_covered_count": len(input_tara_ids & tara_covered),
        "tara_missing_ids": sorted(input_tara_ids - tara_covered),
        "catalog_count": len(catalog),
        "final_row_count": len(rows),
        "same_count": resolution_counts["same"],
        "partial_count": resolution_counts["partial"],
        "catalog_partial_count": catalog_partial_count,
        "oem_partial_row_count": oem_partial_row_count,
        "oem_specific_count": oem_only_count,
        "tara_only_count": resolution_counts["tara_only"],
        "catalog_only_count": resolution_counts["catalog_only"],
        "customer_only_count": resolution_counts["customer_only"],
        "tara_gap_count": resolution_counts["tara_gap"],
        "all_covered": not (input_customer_ids - customer_covered) and not (input_tara_ids - tara_covered),
    }
    return rows, report


def merge_oem_requirements_with_fixed_csrs(
    db,
    project_id: int,
    domain_id: int,
    customer_reqs: Sequence[dict],
    domain,
    run,
) -> Tuple[List[dict], dict]:
    """Full OEM + fixed-CSR merge pipeline for the API."""
    from app.models.csr_catalog import TaraCsrMapping
    from app.models.system_settings import SystemSettings
    from app.services.csr_catalog import get_active_catalog, get_active_catalog_version

    version = get_active_catalog_version(db)
    catalog = get_active_catalog(db)
    if version is None or not catalog:
        raise ValueError("请先导入固定 CSR 目录")

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
        raise ValueError("该域控/run 还没有 TARA→固定 CSR 映射结果，请先执行映射")

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
    rows, report = build_final_rows(
        catalog,
        tara_rows,
        atomized["atoms"],
        mapped,
    )
    report.update({
        "oem_input_count": atomized["input_count"],
        "oem_atom_count": atomized["atom_count"],
        "oem_mapping_rows": mapped["summary"]["mapping_rows"],
        "oem_full_count": mapped["summary"]["full_count"],
        "oem_partial_count": mapped["summary"]["partial_count"],
        "oem_gap_count": mapped["summary"]["gap_count"],
        "oem_error_count": mapped["summary"]["error_count"],
        "catalog_version": version.version,
    })
    if not report["all_covered"]:
        raise ValueError(f"最终合并追溯断裂: {report}")
    return rows, report
