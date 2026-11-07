from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.csr_catalog import TaraCsrMapping
from app.models.domain import Domain
from app.models.req_merge import MergeConflict, OemUnmappedRequirement
from app.models.tara_run import TaraRun
from app.models.tara_step import TaraStep


def cleanup_tara_history(db: Session, domain_id: int | None = None) -> dict:
    """Caller owns the transaction; retain latest attempt and latest success."""
    query = db.query(Domain.id, Domain.status)
    if domain_id is not None:
        query = query.filter(Domain.id == domain_id)
    deleted = {}
    retained = []
    skipped = []
    for id_, status in query.all():
        runs = db.query(TaraRun.id, TaraRun.status).filter(
            TaraRun.domain_id == id_
        ).order_by(TaraRun.id.desc()).all()
        if status == "running" or any(s in ("pending", "running") for _, s in runs):
            skipped.append(id_)
            continue
        if not runs:
            continue
        keep = {runs[0][0]}
        success = next((id_ for id_, s in runs if s == "completed"), None)
        if success is not None:
            keep.add(success)
        retained.extend(sorted(keep))
        obsolete = select(TaraRun.id).where(
            TaraRun.domain_id == id_, TaraRun.id.not_in(keep)
        )
        for model in (TaraStep, TaraCsrMapping, MergeConflict, OemUnmappedRequirement):
            count = db.query(model).filter(model.run_id.in_(obsolete)).delete(
                synchronize_session=False
            )
            deleted[model.__tablename__] = deleted.get(model.__tablename__, 0) + count
        count = db.query(TaraRun).filter(
            TaraRun.domain_id == id_, TaraRun.id.not_in(keep)
        ).delete(synchronize_session=False)
        deleted["tara_runs"] = deleted.get("tara_runs", 0) + count
    return {"deleted": deleted, "retained_run_ids": retained, "skipped_domain_ids": skipped}
