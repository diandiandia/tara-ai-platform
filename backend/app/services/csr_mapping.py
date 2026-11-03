"""Map TARA requirements onto a fixed CSR catalog.

Unlike CSR synthesis, this module never asks the LLM to invent or rewrite a
CSR.  The target catalog is fixed; the LLM only chooses catalog IDs (zero,
one, or many) for each TARA requirement.  Gaps and partial matches are
recorded explicitly and are never silently forced into a target row.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy.orm import Session

from app.models.asset import Asset
from app.models.csr_catalog import CsrCatalogEntry, CsrCatalogVersion, TaraCsrMapping
from app.models.domain import Domain
from app.models.tara_run import TaraRun
from app.models.tara_step import TaraStep
from app.services.csr_catalog import get_active_catalog, get_active_catalog_version
from app.services.csr_synthesis import _call_llm_json

DEFAULT_BATCH_SIZE = 25
CATALOG_VERSION_FALLBACK = "unknown"


def _content_hash(prefix: str, text: str) -> str:
    return prefix + hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:8].upper()


def _norm_ids(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        parts = [str(x) for x in value]
    else:
        parts = re.split(r"[;,，；\n|/]+", str(value))
    result: List[str] = []
    for part in parts:
        part = str(part).strip()
        if part and part not in result:
            result.append(part)
    return result


def _settings_from_db(db: Session) -> Optional[dict]:
    from app.models.system_settings import SystemSettings

    settings = db.query(SystemSettings).first()
    if not settings or not settings.api_key:
        return None
    return {
        "api_base_url": settings.api_base_url,
        "model_name": settings.model_name or "deepseek-flash",
        "api_key": settings.api_key,
    }


def _run_and_domain(db: Session, domain_id: int, run_id: Optional[int] = None):
    domain = db.query(Domain).filter(Domain.id == domain_id).first()
    if not domain:
        raise ValueError(f"domain {domain_id} not found")
    if run_id is None:
        run = (
            db.query(TaraRun)
            .filter(TaraRun.domain_id == domain_id, TaraRun.status == "completed")
            .order_by(TaraRun.id.desc())
            .first()
        )
    else:
        run = (
            db.query(TaraRun)
            .filter(TaraRun.id == run_id, TaraRun.domain_id == domain_id)
            .first()
        )
    if not run:
        raise ValueError(f"no completed TARA run found for domain {domain_id}")
    return domain, run


def collect_tara_requirements(
    db: Session,
    domain_id: int,
    run_id: Optional[int] = None,
    limit: Optional[int] = None,
) -> dict:
    """Collect Stage5 TARA requirements for one domain/run.

    Each returned item keeps the primary TARA ID plus all original source IDs
    that the Stage5 requirement already carried.
    """
    domain, run = _run_and_domain(db, domain_id, run_id)
    assets = {
        asset.id: asset
        for asset in db.query(Asset).filter(Asset.domain_id == domain_id).all()
    }
    steps = (
        db.query(TaraStep)
        .filter(
            TaraStep.run_id == run.id,
            TaraStep.stage == "stage5",
            TaraStep.status == "completed",
        )
        .all()
    )

    requirements: List[dict] = []
    seen: Set[Tuple[int, str]] = set()
    for step in steps:
        asset = assets.get(step.asset_id)
        if asset is None or getattr(asset, "status", "confirmed") == "rejected":
            continue
        final_output = (step.analysis_result or {}).get("final_output", {})
        raw_requirements = final_output.get("requirements") or final_output.get("summarized_csrs") or []
        for index, requirement in enumerate(raw_requirements):
            text = str(
                requirement.get("cybersecurity_requirement")
                or requirement.get("requirement_text")
                or requirement.get("full_text")
                or ""
            ).strip()
            if not text or text == "N/A":
                continue
            primary_id = str(
                requirement.get("cybersecurity_requirement_id")
                or requirement.get("csr_id")
                or ""
            ).strip()
            if not primary_id or primary_id.upper() == "N/A":
                primary_id = _content_hash("CSR_", text)
            source_ids = _norm_ids(
                requirement.get("source_requirement_ids")
                or requirement.get("source_tara_ids")
            )
            if primary_id not in source_ids:
                source_ids.append(primary_id)
            key = (asset.id, primary_id)
            if key in seen:
                continue
            seen.add(key)
            requirements.append({
                "tara_req_id": primary_id,
                "source_tara_ids": source_ids,
                "title": requirement.get("title") or requirement.get("requirement_title") or "",
                "text": text,
                "asset_id": asset.id,
                "asset_name": asset.name or str(asset.id),
                "asset_type": getattr(asset, "asset_type", "") or "",
                "protocol": getattr(asset, "protocol", "") or "",
                "asset_description": getattr(asset, "description", "") or "",
                "requirement_scope": requirement.get("requirement_scope") or "",
                "owner": requirement.get("owner") or "",
                "allocated_to": requirement.get("allocated_to") or "",
                "row_index": index,
            })
            if limit is not None and len(requirements) >= limit:
                break
        if limit is not None and len(requirements) >= limit:
            break

    return {
        "domain": domain,
        "run": run,
        "requirements": requirements,
        "input_count": len(requirements),
    }


def _catalog_payload(catalog: Sequence[dict]) -> List[dict]:
    return [
        {
            "csr_id": row["csr_id"],
            "module": row["module"],
            "name": row["name"],
            "description": row["description"],
        }
        for row in catalog
    ]


def _mapping_prompt(source_label: str = "TARA", allow_conflict: bool = False) -> str:
    conflict_rule = (
        f"   - conflict：{source_label} 需求与目录 CSR 针对同一对象/动作/条件给出互斥要求"
        "（例如一方要求“必须禁用调试口”，另一方要求“经授权可启用”）；"
        "此时 target_csr_ids 必须包含冲突的 CSR，conflict_note 必须写明矛盾点。\n"
        if allow_conflict
        else ""
    )
    return f"""你是汽车网络安全需求映射专家。下面给出一个固定的 CSR 目录和一批 {source_label} 信息安全需求。
你的任务不是改写或生成 CSR，而是判断每条 {source_label} 需求能被目录中的哪些 CSR 覆盖。

【规则】
1. 只能使用目录中存在的 csr_id，严禁编造、改写或新增 CSR。
2. 一条 {source_label} 需求可以映射到 0 条、1 条或多条 CSR；多条 {source_label} 需求也可以映射到同一条 CSR。
3. match_type 只能是：
   - full：目录 CSR 完整覆盖该 {source_label} 需求；
   - partial：只覆盖一部分，必须在 missing_aspect 写清缺什么；
   - gap：目录中没有能覆盖它的 CSR，target_csr_ids 必须为空数组；
{conflict_rule}4. 不同动作、不同保护对象、不同适用条件不能强行映射到同一条 CSR；不确定时用 partial 或 gap，不要硬凑。
5. 必须为输入中的每一条需求 ID（JSON 字段名 tara_id）返回且只返回一条记录；该 ID 必须原样返回。
6. confidence 为 0 到 1 的小数；rationale 用一两句话说明依据。
7. 只输出 JSON，不要输出 Markdown。

【输出 JSON】
{{
  "mappings": [
    {{
      "tara_id": "SOURCE_ID",
      "target_csr_ids": ["CS-HW-001"],
      "match_type": "full",
      "confidence": 0.9,
      "rationale": "……",
      "missing_aspect": ""
    }}
  ]
}}"""

def _error_row(requirement: dict, reason: str) -> dict:
    return {
        "tara_req_id": requirement["tara_req_id"],
        "source_tara_ids": list(requirement.get("source_tara_ids") or [requirement["tara_req_id"]]),
        "tara_text": requirement.get("text") or "",
        "asset_id": requirement.get("asset_id"),
        "asset_name": requirement.get("asset_name") or "",
        "target_csr_id": "",
        "match_type": "error",
        "confidence": 0.0,
        "rationale": reason,
        "missing_aspect": "",
        "needs_review": True,
    }


def _validate_mapping_batch(
    batch: Sequence[dict],
    parsed: dict,
    catalog_ids: Set[str],
    allow_conflict: bool = False,
) -> List[dict]:
    input_ids = {item["tara_req_id"] for item in batch}
    catalog_lookup = {csr_id.lower(): csr_id for csr_id in catalog_ids}
    items = parsed.get("mappings") or parsed.get("results") or []
    by_tara: Dict[str, List[dict]] = defaultdict(list)
    for item in items:
        if not isinstance(item, dict):
            continue
        tara_id = str(item.get("tara_id") or item.get("requirement_id") or "").strip()
        if tara_id in input_ids:
            by_tara[tara_id].append(item)

    rows: List[dict] = []
    for requirement in batch:
        tara_id = requirement["tara_req_id"]
        entries = by_tara.get(tara_id) or []
        if not entries:
            rows.append(_error_row(requirement, "LLM 未返回该 TARA 需求的映射结果"))
            continue

        targets: List[str] = []
        dropped: List[str] = []
        match_types: List[str] = []
        confidences: List[float] = []
        rationales: List[str] = []
        missing_aspects: List[str] = []
        conflict_notes: List[str] = []

        for entry in entries:
            raw_targets = _norm_ids(
                entry.get("target_csr_ids")
                or entry.get("target_csr_id")
                or entry.get("csr_ids")
            )
            for target in raw_targets:
                canonical = catalog_lookup.get(target.lower())
                if canonical:
                    if canonical not in targets:
                        targets.append(canonical)
                elif target not in dropped:
                    dropped.append(target)

            raw_type = str(entry.get("match_type") or "").strip().lower()
            allowed_types = {"full", "partial", "gap"}
            if allow_conflict:
                allowed_types.add("conflict")
            if raw_type not in allowed_types:
                raw_type = ""
            match_types.append(raw_type)
            try:
                confidence = float(entry.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            confidences.append(max(0.0, min(1.0, confidence)))
            rationale = str(entry.get("rationale") or "").strip()
            if rationale and rationale not in rationales:
                rationales.append(rationale)
            missing = str(entry.get("missing_aspect") or "").strip()
            if missing and missing not in missing_aspects:
                missing_aspects.append(missing)
            conflict_note = str(entry.get("conflict_note") or "").strip()
            if conflict_note and conflict_note not in conflict_notes:
                conflict_notes.append(conflict_note)

        needs_review = bool(dropped) or any(
            value in {"partial", "conflict"} for value in match_types
        ) or any(value < 0.6 for value in confidences)
        if dropped:
            rationales.append("LLM 返回了目录外 ID，已剔除: " + ", ".join(dropped))

        if not targets:
            if "conflict" in match_types:
                rows.append(_error_row(requirement, "LLM 标记 conflict 但未给出冲突 CSR"))
                continue
            match_type = "gap"
            needs_review = True
            if not missing_aspects:
                missing_aspects.append("目录中没有能覆盖该 TARA 需求的 CSR")
            rows.append({
                "tara_req_id": tara_id,
                "source_tara_ids": list(requirement.get("source_tara_ids") or [tara_id]),
                "tara_text": requirement.get("text") or "",
                "asset_id": requirement.get("asset_id"),
                "asset_name": requirement.get("asset_name") or "",
                "target_csr_id": "",
                "match_type": match_type,
                "confidence": max(confidences) if confidences else 0.0,
                "rationale": " | ".join(rationales),
                "missing_aspect": "；".join(missing_aspects),
                "needs_review": needs_review,
            })
            continue

        # A target exists.  If any item was partial, or any missing aspect was
        # provided, keep the worst status rather than upgrading to full.
        if "conflict" in match_types:
            match_type = "conflict"
            needs_review = True
        elif any(value in {"partial", "gap"} for value in match_types) or missing_aspects:
            match_type = "partial"
            needs_review = True
        else:
            match_type = "full"
        confidence = max(confidences) if confidences else 0.0
        for target in targets:
            rows.append({
                "tara_req_id": tara_id,
                "source_tara_ids": list(requirement.get("source_tara_ids") or [tara_id]),
                "tara_text": requirement.get("text") or "",
                "asset_id": requirement.get("asset_id"),
                "asset_name": requirement.get("asset_name") or "",
                "target_csr_id": target,
                "match_type": match_type,
                "confidence": confidence,
                "rationale": " | ".join(rationales),
                "missing_aspect": "；".join(missing_aspects),
                "conflict_note": "；".join(conflict_notes),
                "needs_review": needs_review,
            })
    return rows


def _call_mapping_batch(
    settings: dict,
    catalog_payload: Sequence[dict],
    batch: Sequence[dict],
    catalog_ids: Set[str],
    depth: int = 0,
    source_label: str = "TARA",
    allow_conflict: bool = False,
) -> List[dict]:
    payload = {
        "catalog": list(catalog_payload),
        "requirements": [
            {
                "tara_id": item["tara_req_id"],
                "asset_name": item.get("asset_name") or "",
                "asset_type": item.get("asset_type") or "",
                "protocol": item.get("protocol") or "",
                "asset_description": item.get("asset_description") or "",
                "title": item.get("title") or "",
                "text": item.get("text") or "",
                "requirement_scope": item.get("requirement_scope") or "",
                "owner": item.get("owner") or "",
                "allocated_to": item.get("allocated_to") or "",
            }
            for item in batch
        ],
    }
    try:
        parsed = _call_llm_json(settings, _mapping_prompt(source_label, allow_conflict), payload, temperature=0.0)
        rows = _validate_mapping_batch(batch, parsed, catalog_ids, allow_conflict=allow_conflict)
        failed = any(row.get("match_type") == "error" for row in rows)
        if failed and len(batch) > 1 and depth < 2:
            print(f"[TARA-AI] 映射批次不完整，拆分为 {len(batch)} 条重试")
            mid = len(batch) // 2
            return (
                _call_mapping_batch(settings, catalog_payload, batch[:mid], catalog_ids, depth + 1, source_label, allow_conflict)
                + _call_mapping_batch(settings, catalog_payload, batch[mid:], catalog_ids, depth + 1, source_label, allow_conflict)
            )
        return rows
    except Exception as exc:
        if len(batch) > 1 and depth < 2:
            print(f"[TARA-AI] 映射批次失败，拆分为 {len(batch)} 条重试: {exc}")
            mid = len(batch) // 2
            return (
                _call_mapping_batch(settings, catalog_payload, batch[:mid], catalog_ids, depth + 1, source_label, allow_conflict)
                + _call_mapping_batch(settings, catalog_payload, batch[mid:], catalog_ids, depth + 1, source_label, allow_conflict)
            )
        return [_error_row(item, f"LLM/解析失败: {exc}") for item in batch]


def map_tara_requirements(
    requirements: Sequence[dict],
    catalog: Sequence[dict],
    settings: Optional[dict],
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_workers: int = 4,
    source_label: str = "TARA",
    allow_conflict: bool = False,
) -> dict:
    """Map requirements to catalog rows. Returns rows plus a coverage summary."""
    if not catalog:
        raise ValueError("CSR catalog is empty")
    catalog_ids = {row["csr_id"] for row in catalog}
    catalog_payload = _catalog_payload(catalog)

    batches = [
        list(requirements[start:start + batch_size])
        for start in range(0, len(requirements), batch_size)
    ]
    rows: List[dict] = []
    if not settings:
        for item in requirements:
            rows.append(_error_row(item, "LLM 未配置，无法执行映射"))
    elif max_workers > 1 and len(batches) > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: List[Optional[List[dict]]] = [None] * len(batches)

        def run_batch(index: int, batch: Sequence[dict]) -> List[dict]:
            print(f"[TARA-AI] CSR 映射批次 {index + 1}/{len(batches)}: {len(batch)} 条")
            try:
                return _call_mapping_batch(settings, catalog_payload, batch, catalog_ids, source_label=source_label, allow_conflict=allow_conflict)
            except Exception as exc:  # defensive: worker must not kill the pool
                return [_error_row(item, f"映射批次异常: {exc}") for item in batch]

        with ThreadPoolExecutor(max_workers=min(max_workers, len(batches))) as executor:
            futures = {
                executor.submit(run_batch, index, batch): index
                for index, batch in enumerate(batches)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception as exc:
                    results[index] = [
                        _error_row(item, f"映射批次异常: {exc}") for item in batches[index]
                    ]
        for result in results:
            rows.extend(result or [])
    else:
        for index, batch in enumerate(batches, start=1):
            print(f"[TARA-AI] CSR 映射批次 {index}/{len(batches)}: {len(batch)} 条")
            rows.extend(_call_mapping_batch(settings, catalog_payload, batch, catalog_ids, source_label=source_label, allow_conflict=allow_conflict))

    # De-duplicate rows in case the model returned the same target twice.
    unique: Dict[Tuple, dict] = {}
    for row in rows:
        key = (row["asset_id"], row["tara_req_id"], row["target_csr_id"])
        if key not in unique:
            unique[key] = row
    rows = list(unique.values())

    by_tara: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        by_tara[row["tara_req_id"]].append(row)

    full_count = partial_count = gap_count = error_count = conflict_count = 0
    for tara_id, tara_rows in by_tara.items():
        types = {row["match_type"] for row in tara_rows}
        if "error" in types:
            error_count += 1
        elif "conflict" in types:
            conflict_count += 1
        elif "gap" in types or not any(row["target_csr_id"] for row in tara_rows):
            gap_count += 1
        elif "partial" in types:
            partial_count += 1
        else:
            full_count += 1

    target_ids = {row["target_csr_id"] for row in rows if row["target_csr_id"]}
    mapped_ids = {row["tara_req_id"] for row in rows if row["target_csr_id"]}
    summary = {
        "input_count": len(requirements),
        "mapping_rows": len(rows),
        "mapped_count": len(mapped_ids),
        "full_count": full_count,
        "partial_count": partial_count,
        "gap_count": gap_count,
        "error_count": error_count,
        "conflict_count": conflict_count,
        "target_covered_count": len(target_ids),
        "catalog_count": len(catalog),
        "low_confidence_count": len({
            row["tara_req_id"] for row in rows if row.get("confidence", 0) < 0.6
        }),
    }
    return {
        "rows": rows,
        "summary": summary,
        "tara_to_targets": _build_tara_to_targets(rows),
    }


def _build_tara_to_targets(rows: Iterable[dict]) -> Dict[str, List[str]]:
    result: Dict[str, Set[str]] = defaultdict(set)
    for row in rows:
        target = row.get("target_csr_id") or ""
        if not target:
            continue
        result[row["tara_req_id"]].add(target)
        for source_id in row.get("source_tara_ids") or []:
            result[str(source_id)].add(target)
    return {key: sorted(values) for key, values in result.items()}


def persist_mappings(
    db: Session,
    domain: Domain,
    run: TaraRun,
    mappings: Sequence[dict],
    catalog_version: str,
    replace: bool = True,
) -> int:
    """Persist mapping rows. Re-running replaces the run's previous mapping."""
    if replace:
        db.query(TaraCsrMapping).filter(
            TaraCsrMapping.domain_id == domain.id,
            TaraCsrMapping.run_id == run.id,
        ).delete(synchronize_session=False)

    seen: Set[Tuple] = set()
    for row in mappings:
        target = row.get("target_csr_id") or ""
        key = (row.get("asset_id"), row.get("tara_req_id"), target)
        if key in seen:
            continue
        seen.add(key)
        db.add(TaraCsrMapping(
            project_id=domain.project_id,
            domain_id=domain.id,
            run_id=run.id,
            asset_id=row.get("asset_id"),
            asset_name=row.get("asset_name") or "",
            tara_req_id=row.get("tara_req_id") or "",
            source_tara_ids="; ".join(row.get("source_tara_ids") or []),
            tara_text=row.get("tara_text") or "",
            catalog_version=catalog_version,
            target_csr_id=target,
            match_type=row.get("match_type") or "error",
            confidence=float(row.get("confidence") or 0.0),
            rationale=row.get("rationale") or "",
            missing_aspect=row.get("missing_aspect") or "",
            conflict_note=row.get("conflict_note") or "",
            needs_review=bool(row.get("needs_review")),
        ))
    db.commit()
    return len(seen)


def map_domain_run_to_fixed_catalog(
    db: Session,
    domain: Domain,
    run: TaraRun,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_workers: int = 4,
    allow_conflict: bool = True,
) -> dict:
    """Map one finished TARA run onto the active fixed CSR catalog and persist.

    Used automatically at the end of a TARA run.  Returns a small status dict
    instead of raising when a prerequisite is missing, so a TARA run is never
    failed just because the catalog / LLM is not ready.
    """
    version = get_active_catalog_version(db)
    catalog = get_active_catalog(db)
    if version is None or not catalog:
        return {"status": "skipped", "reason": "no_active_catalog", "persisted_rows": 0}

    settings = _settings_from_db(db)
    if not settings:
        return {"status": "skipped", "reason": "no_llm_settings", "persisted_rows": 0}

    collected = collect_tara_requirements(db, domain.id, run_id=run.id)
    if not collected["requirements"]:
        return {"status": "skipped", "reason": "no_requirements", "persisted_rows": 0}

    mapped = map_tara_requirements(
        collected["requirements"],
        catalog,
        settings,
        batch_size=batch_size,
        max_workers=max_workers,
        allow_conflict=allow_conflict,
    )
    saved = persist_mappings(
        db,
        collected["domain"],
        collected["run"],
        mapped["rows"],
        catalog_version=version.version,
        replace=True,
    )
    return {
        "status": "completed",
        "catalog_version": version.version,
        "persisted_rows": saved,
        "summary": mapped["summary"],
    }


def _mapping_rows(db: Session, domain_id: int, run_id: int) -> List[TaraCsrMapping]:
    return (
        db.query(TaraCsrMapping)
        .filter(TaraCsrMapping.domain_id == domain_id, TaraCsrMapping.run_id == run_id)
        .order_by(TaraCsrMapping.tara_req_id, TaraCsrMapping.target_csr_id)
        .all()
    )


def build_mapping_report_data(
    db: Session,
    domain_id: int,
    run_id: int,
) -> Optional[dict]:
    """Build catalog-based CSR rows, mapping details, and coverage summary."""
    version = get_active_catalog_version(db)
    if version is None:
        return None
    catalog = get_active_catalog(db)
    mappings = [
        row for row in _mapping_rows(db, domain_id, run_id)
        if row.catalog_version == version.version
    ]
    catalog_by_id = {row["csr_id"]: row for row in catalog}

    by_target: Dict[str, List[TaraCsrMapping]] = defaultdict(list)
    for row in mappings:
        if row.target_csr_id:
            by_target[row.target_csr_id].append(row)

    csr_rows: List[dict] = []
    for entry in catalog:
        matched = by_target.get(entry["csr_id"], [])
        source_ids: Set[str] = set()
        assets: Set[str] = set()
        rationales: List[str] = []
        missing_aspects: List[str] = []
        confidences: List[float] = []
        statuses: Set[str] = set()
        for row in matched:
            source_ids.update(_norm_ids(row.source_tara_ids))
            source_ids.add(row.tara_req_id)
            if row.asset_name:
                assets.add(row.asset_name)
            if row.rationale and row.rationale not in rationales:
                rationales.append(row.rationale)
            if row.missing_aspect and row.missing_aspect not in missing_aspects:
                missing_aspects.append(row.missing_aspect)
            confidences.append(float(row.confidence or 0.0))
            statuses.add(row.match_type)

        if not matched:
            mapping_status = "catalog_unmapped"
            needs_review = False
        elif "conflict" in statuses:
            mapping_status = "conflict"
            needs_review = True
        elif "partial" in statuses:
            mapping_status = "partial"
            needs_review = True
        else:
            mapping_status = "full"
            needs_review = any(value < 0.6 for value in confidences)
        if any(row.needs_review for row in matched):
            needs_review = True

        sorted_sources = sorted(source_ids)
        sorted_assets = sorted(assets)
        csr_rows.append({
            "number": entry["csr_id"],
            "csr_id": entry["csr_id"],
            "functional_category": entry["module"],
            "security_domain": entry["module"],
            "control_category": entry["module"],
            "sub_category": entry["module"],
            "requirement_title": entry["name"],
            "title": entry["name"],
            "subject": "",
            "cybersecurity_requirement": entry["description"],
            "verification_method": "待定 / TBD",
            "requirement_type": (
                "process" if entry["module"] in {
                    "全生命周期保障（过程类）", "合规证据与测试输入"
                } else "product"
            ),
            "rationale": "；".join(rationales),
            "requirement_scope": "",
            "owner": "",
            "allocated_to": "",
            "allocation_type": "",
            "acceptance_criteria": "",
            "goal_ids": [],
            "control_ids": [],
            "claim_ids": [],
            "control_type": "",
            "implementation_type": "",
            "control_objective": "",
            "control_point": "",
            "source_ids": [],
            "dependencies": [],
            "open_issue": "；".join(missing_aspects),
            "applicable_assets": "\n".join(sorted_assets),
            "assets_count": len(sorted_assets),
            "source_cybersecurity_requirement_ids": "; ".join(sorted_sources),
            "source_tara_requirements": "; ".join(sorted_sources),
            "source_count": len(sorted_sources),
            "source_requirement_ids": sorted_sources,
            "_raw_source_ids": sorted_sources,
            "_tier2_source_ids": sorted_sources,
            "_coverage_status": mapping_status,
            "needs_review": "Yes" if needs_review else "No",
            "mapping_status": mapping_status,
            "matched_tara_count": len({row.tara_req_id for row in matched}),
            "catalog_version": version.version,
        })

    mapping_details = [
        {
            "tara_req_id": row.tara_req_id,
            "source_tara_ids": "; ".join(_norm_ids(row.source_tara_ids)),
            "asset_id": row.asset_id,
            "asset_name": row.asset_name or "",
            "target_csr_id": row.target_csr_id or "",
            "target_csr_name": catalog_by_id.get(row.target_csr_id, {}).get("name", ""),
            "match_type": row.match_type,
            "confidence": row.confidence,
            "rationale": row.rationale or "",
            "missing_aspect": row.missing_aspect or "",
            "needs_review": "Yes" if row.needs_review else "No",
            "tara_text": row.tara_text or "",
            "conflict_note": row.conflict_note or "",
            "target_csr_text": catalog_by_id.get(row.target_csr_id, {}).get("description", ""),
        }
        for row in mappings
    ]

    unmapped = [
        detail for detail in mapping_details
        if not detail["target_csr_id"] or detail["match_type"] in {"gap", "error"}
    ]

    by_tara_rows: Dict[str, List[TaraCsrMapping]] = defaultdict(list)
    for row in mappings:
        by_tara_rows[row.tara_req_id].append(row)
    tara_status: Dict[str, str] = {}
    for tara_id, tara_rows in by_tara_rows.items():
        types = {row.match_type for row in tara_rows}
        has_target = any(row.target_csr_id for row in tara_rows)
        if "error" in types:
            tara_status[tara_id] = "error"
        elif "conflict" in types:
            tara_status[tara_id] = "conflict"
        elif not has_target:
            tara_status[tara_id] = "gap"
        elif "partial" in types:
            tara_status[tara_id] = "partial"
        else:
            tara_status[tara_id] = "full"

    summary = {
        "input_count": len(by_tara_rows),
        "mapping_rows": len(mappings),
        "mapped_count": sum(
            1 for value in tara_status.values() if value in {"full", "partial", "conflict"}
        ),
        "full_count": sum(1 for value in tara_status.values() if value == "full"),
        "partial_count": sum(1 for value in tara_status.values() if value == "partial"),
        "gap_count": sum(1 for value in tara_status.values() if value == "gap"),
        "error_count": sum(1 for value in tara_status.values() if value == "error"),
        "conflict_count": sum(1 for value in tara_status.values() if value == "conflict"),
        "target_covered_count": len(by_target),
        "catalog_count": len(catalog),
        "catalog_unmapped_count": sum(1 for row in csr_rows if row["mapping_status"] == "catalog_unmapped"),
        "low_confidence_count": len({
            row.tara_req_id for row in mappings
            if float(row.confidence or 0.0) < 0.6
        }),
        "catalog_version": version.version,
    }

    return {
        "csr_rows": csr_rows,
        "mapping_details": mapping_details,
        "conflicts": [
            detail for detail in mapping_details
            if detail["match_type"] == "conflict"
        ],
        "unmapped": unmapped,
        "summary": summary,
        "tara_to_targets": _build_tara_to_targets(
            [
                {
                    "tara_req_id": row.tara_req_id,
                    "source_tara_ids": _norm_ids(row.source_tara_ids),
                    "target_csr_id": row.target_csr_id,
                }
                for row in mappings
            ]
        ),
    }


def collect_catalog_mapping_report_data(db: Session, domain: Domain, steps: Sequence[TaraStep]) -> Optional[dict]:
    """Report helper: use the latest run in ``steps`` and return mapping data."""
    if not domain or not steps:
        return None
    run_id = getattr(steps[0], "run_id", None)
    if run_id is None:
        return None
    return build_mapping_report_data(db, domain.id, run_id)
