import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.auth import get_current_user
from app.api.req_merge import router
from app.core.database import Base, get_db
from app.models.domain import Domain
from app.models.project import Project
from app.models.req_merge import CustomerRequirement, MergedRequirement
from app.models.tara_run import TaraRun, get_results_run
from app.models.tara_step import TaraStep
from app.models.user import User


class TestWorkbenchSummary(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            project = Project(name="Test")
            db.add(project)
            db.flush()
            domains = [Domain(project_id=project.id, name=name) for name in ("A", "B")]
            db.add_all(domains)
            db.commit()
            self.project_id = project.id
            self.domain_id, self.other_domain_id = [domain.id for domain in domains]
        app = FastAPI()
        app.include_router(router, prefix="/api")

        def isolated_db():
            with Session(self.engine) as db:
                yield db

        app.dependency_overrides[get_db] = isolated_db
        app.dependency_overrides[get_current_user] = lambda: User(role="analyst")
        self.client = TestClient(app)
        self.statements = []
        event.listen(self.engine, "before_cursor_execute", self.capture_sql)

    def capture_sql(self, connection, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def add_run(self, status, with_step=False, domain_id=None):
        with Session(self.engine) as db:
            run = TaraRun(domain_id=domain_id or self.domain_id, status=status)
            db.add(run)
            db.flush()
            if with_step:
                db.add(TaraStep(
                    run_id=run.id, asset_id=1, stage="stage1", input_hash="test",
                    analysis_result={"large": "x" * 1_000_000},
                ))
            db.commit()
            return run.id

    def summary(self, domain_id=None):
        return self.client.get(
            f"/api/domains/{domain_id or self.domain_id}/workbench-summary"
        )

    def test_empty_domain_and_missing_domain(self):
        self.assertEqual(self.summary().json(), {
            "domain_id": self.domain_id, "run_id": None,
            "has_tara_results": False, "merged_count": 0,
        })
        self.assertEqual(self.summary(999).status_code, 404)

    def test_summary_is_small_and_never_reads_analysis_or_source_payloads(self):
        run_id = self.add_run("completed", with_step=True)
        with Session(self.engine) as db:
            db.add_all([
                MergedRequirement(
                    project_id=self.project_id, domain_id=self.domain_id,
                    final_id=str(i), requirement_text="x" * 10000,
                ) for i in range(200)
            ])
            db.add(MergedRequirement(
                project_id=self.project_id, domain_id=self.other_domain_id,
                final_id="other",
            ))
            db.commit()
        self.statements.clear()
        response = self.summary()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "domain_id": self.domain_id, "run_id": run_id,
            "has_tara_results": True, "merged_count": 200,
        })
        self.assertLess(len(response.content), 200)
        self.assertEqual(len(self.statements), 4)
        sql = "\n".join(self.statements)
        self.assertNotIn("analysis_result", sql)
        self.assertNotIn("requirement_text", sql)
        self.assertNotIn("merged_requirement_sources", sql)

    def test_latest_finished_run_is_preferred_over_newer_running_run(self):
        finished = self.add_run("completed", with_step=True)
        self.add_run("running")
        self.assertEqual(self.summary().json()["run_id"], finished)
        self.assertTrue(self.summary().json()["has_tara_results"])
        for status in ("failed", "cancelled"):
            latest = self.add_run(status)
            with Session(self.engine) as db:
                self.assertEqual(get_results_run(db, self.domain_id).id, latest)
            data = self.summary().json()
            self.assertEqual(data["run_id"], latest)
            self.assertFalse(data["has_tara_results"])

    def test_running_run_fallback_and_domain_isolation(self):
        run = self.add_run("running", with_step=True)
        self.add_run("completed", with_step=True, domain_id=self.other_domain_id)
        self.assertEqual(self.summary().json()["run_id"], run)
        self.assertTrue(self.summary().json()["has_tara_results"])

    def test_customer_count_matches_list_without_loading_text(self):
        with Session(self.engine) as db:
            db.add_all([
                CustomerRequirement(
                    project_id=self.project_id, external_id=str(i), text="x" * 10000,
                ) for i in range(3)
            ])
            db.commit()
        self.statements.clear()
        response = self.client.get(
            f"/api/projects/{self.project_id}/customer-requirements/count"
        )
        self.assertEqual(response.json(), {"count": 3})
        self.assertEqual(len(self.statements), 1)
        self.assertNotIn("customer_requirements.text", self.statements[0])
        rows = self.client.get(
            f"/api/projects/{self.project_id}/customer-requirements"
        ).json()
        self.assertEqual(response.json()["count"], len(rows))
