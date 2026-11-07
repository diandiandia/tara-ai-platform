import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models import Domain, Project, Asset, Diagram, TaraRun, TaraStep
from app.models.csr_catalog import TaraCsrMapping
from app.models.req_merge import MergeConflict, OemUnmappedRequirement, MergedRequirement
from app.services.tara_history import cleanup_tara_history


class TestTaraHistory(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        @event.listens_for(self.engine, "connect")
        def enable_fk(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        project = Project(name="test")
        self.db.add(project)
        self.db.flush()
        self.domain = Domain(project_id=project.id, name="test")
        self.db.add(self.domain)
        self.db.flush()
        diagram = Diagram(domain_id=self.domain.id, title="keep")
        self.db.add(diagram)
        self.db.flush()
        asset = Asset(domain_id=self.domain.id, diagram_id=diagram.id, name="keep", asset_type="data")
        self.db.add(asset)
        self.db.flush()
        self.asset_id = asset.id
        self.db.add(MergedRequirement(
            project_id=project.id, domain_id=self.domain.id, final_id="keep",
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def run_record(self, status, domain=None):
        domain = domain or self.domain
        run = TaraRun(domain_id=domain.id, status=status)
        self.db.add(run)
        self.db.flush()
        self.db.add(TaraStep(
            run_id=run.id, asset_id=self.asset_id, stage="stage1",
            input_hash="keep", analysis_result={"final_output": {"value": run.id}},
        ))
        self.db.add(TaraCsrMapping(
            project_id=domain.project_id, domain_id=domain.id, run_id=run.id,
            asset_id=self.asset_id, tara_req_id=str(run.id), catalog_version="v",
        ))
        self.db.add(MergeConflict(
            project_id=domain.project_id, domain_id=domain.id, run_id=run.id,
            oem_atom_id=str(run.id), catalog_csr_id="csr",
        ))
        self.db.add(OemUnmappedRequirement(
            project_id=domain.project_id, domain_id=domain.id, run_id=run.id,
            oem_atom_id=str(run.id),
        ))
        self.db.commit()
        return run.id

    def test_latest_success_and_latest_attempt_retained_with_dependents(self):
        old = self.run_record("completed")
        latest_success = self.run_record("completed")
        latest = self.run_record("failed")
        result = cleanup_tara_history(self.db)
        self.db.commit()
        self.assertEqual(result["retained_run_ids"], [latest_success, latest])
        for model in (TaraRun, TaraStep, TaraCsrMapping, MergeConflict, OemUnmappedRequirement):
            self.assertEqual(self.db.query(model).count(), 2)
        self.assertEqual(result["deleted"]["tara_steps"], 1)
        self.assertEqual(self.db.query(Diagram).count(), 1)
        self.assertEqual(self.db.query(Asset).count(), 1)
        self.assertEqual(self.db.query(MergedRequirement).count(), 1)
        self.assertIsNone(self.db.get(TaraRun, old))
        self.assertTrue(all(n == 0 for n in cleanup_tara_history(self.db)["deleted"].values()))

    def test_active_domain_is_untouched(self):
        self.run_record("completed")
        self.run_record("running")
        result = cleanup_tara_history(self.db)
        self.assertEqual(result["skipped_domain_ids"], [self.domain.id])
        self.assertEqual(self.db.query(TaraRun).count(), 2)

    def test_each_domain_keeps_its_own_latest_and_rollback_restores_history(self):
        self.run_record("failed")
        latest = self.run_record("cancelled")
        other = Domain(project_id=self.domain.project_id, name="other")
        self.db.add(other)
        self.db.commit()
        other_run = self.run_record("completed", other)
        result = cleanup_tara_history(self.db)
        self.assertEqual(set(result["retained_run_ids"]), {latest, other_run})
        self.db.rollback()
        self.assertEqual(self.db.query(TaraRun).count(), 3)

    def test_domain_filter_never_removes_other_domains(self):
        self.run_record("completed")
        self.run_record("completed")
        result = cleanup_tara_history(self.db, 999)
        self.assertEqual(result["deleted"], {})
        self.assertEqual(self.db.query(TaraRun).count(), 2)
