import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text

import manage
from app.core.database import Base
from app.models.tara_step import TaraStep


class TestDiagramDeletionIndexes(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_existing_database_migration_adds_asset_index_idempotently(self):
        index = next(
            index for index in TaraStep.__table__.indexes
            if index.name == "ix_tara_steps_asset_id"
        )
        index.drop(self.engine)
        with self.engine.connect() as connection:
            before = connection.execute(text(
                "EXPLAIN QUERY PLAN SELECT * FROM tara_steps WHERE asset_id = 1"
            )).all()
        self.assertTrue(any("SCAN tara_steps" in row[3] for row in before))

        with patch.object(manage, "engine", self.engine):
            manage.migrate()
            manage.migrate()

        names = {index["name"] for index in inspect(self.engine).get_indexes("tara_steps")}
        self.assertIn("ix_tara_steps_asset_id", names)
        with self.engine.connect() as connection:
            after = connection.execute(text(
                "EXPLAIN QUERY PLAN SELECT * FROM tara_steps WHERE asset_id = 1"
            )).all()
        self.assertTrue(any("ix_tara_steps_asset_id" in row[3] for row in after))
        self.assertFalse(any("SCAN tara_steps" in row[3] for row in after))


if __name__ == "__main__":
    unittest.main()
