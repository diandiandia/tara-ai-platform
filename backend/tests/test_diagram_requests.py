import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.auth import get_current_user
from app.api.diagram import logger, router
from app.core.database import Base, get_db
from app.models.diagram import Diagram
from app.models.domain import Domain
from app.models.project import Project
from app.models.user import User


class TestDiagramRequests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            project = Project(name="Test")
            db.add(project)
            db.flush()
            domain = Domain(project_id=project.id, name="Test")
            db.add(domain)
            db.commit()
            self.domain_id = domain.id

        def isolated_db():
            with Session(self.engine) as db:
                yield db

        app = FastAPI()
        app.include_router(router, prefix="/api")
        app.dependency_overrides[get_db] = isolated_db
        app.dependency_overrides[get_current_user] = lambda: User(role="analyst")
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def test_create_preserves_payload_and_logs_database_stages(self):
        with self.assertLogs(logger, level="INFO") as logs:
            response = self.client.post(
                "/api/diagrams",
                params={"domain_id": self.domain_id, "title": "Private title"},
                json={},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {
            "id", "domain_id", "title", "version_no", "snapshot_json", "updated_at",
        })
        self.assertEqual(response.json()["snapshot_json"], "{}")
        self.assertEqual(response.json()["version_no"], 1)
        trace = "\n".join(logs.output)
        request_id = response.headers["X-Request-ID"]
        self.assertTrue(all(f"request={request_id}" in line for line in logs.output))
        stages = ["received", "validate", "commit", "refresh", "created"]
        positions = [trace.index(f"stage={stage}") for stage in stages]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("outcome=response_ready status=200 elapsed_ms=", trace)
        self.assertNotIn("Private title", trace)
        with Session(self.engine) as db:
            self.assertEqual(db.query(Diagram).count(), 1)

    def test_domain_lock_still_rejects_creation(self):
        with Session(self.engine) as db:
            db.get(Domain, self.domain_id).status = "running"
            db.commit()
        with self.assertLogs(logger, level="INFO") as logs:
            response = self.client.post(
                "/api/diagrams",
                params={"domain_id": self.domain_id, "title": "Test"},
            )
        self.assertEqual(response.status_code, 400)
        trace = "\n".join(logs.output)
        self.assertIn("stage=validate", trace)
        self.assertNotIn("stage=commit", trace)
        self.assertIn("outcome=failed elapsed_ms=", trace)
        with Session(self.engine) as db:
            self.assertEqual(db.query(Diagram).count(), 0)

    def test_commit_failure_is_not_reported_as_success(self):
        client = TestClient(self.client.app, raise_server_exceptions=False)
        try:
            with patch.object(Session, "commit", side_effect=RuntimeError("test failure")):
                with self.assertLogs(logger, level="INFO") as logs:
                    response = client.post(
                        "/api/diagrams",
                        params={"domain_id": self.domain_id, "title": "Test"},
                    )
        finally:
            client.close()
        self.assertEqual(response.status_code, 500)
        trace = "\n".join(logs.output)
        self.assertIn("stage=commit", trace)
        self.assertNotIn("stage=created", trace)
        self.assertIn("outcome=failed elapsed_ms=", trace)
        with Session(self.engine) as db:
            self.assertEqual(db.query(Diagram).count(), 0)
