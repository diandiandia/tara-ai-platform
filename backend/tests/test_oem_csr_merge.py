import os
import sys
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app.models  # noqa: F401
from app.core.database import Base
from app.models.domain import Domain
from app.models.project import Project
from app.models.req_merge import MergedRequirement, MergedRequirementSource
from app.models.tara_run import TaraRun
from app.services.oem_csr_merge import (
    atomize_oem_requirements,
    build_final_rows,
    map_oem_atoms_to_catalog,
)
from app.services.req_merge import persist_merged_requirements
from app.api.report import collect_merged_requirement_rows


def _catalog():
    return [
        {"csr_id": "CS-A", "module": "调试", "name": "A", "description": "A desc"},
        {"csr_id": "CS-B", "module": "日志", "name": "B", "description": "B desc"},
        {"csr_id": "CS-C", "module": "通信", "name": "C", "description": "C desc"},
    ]


def _tara_rows():
    return [
        {"target_csr_id": "CS-A", "tara_req_id": "T1", "source_tara_ids": "T1; T2", "tara_text": "T1 text", "match_type": "full", "confidence": 0.9, "rationale": "r1", "missing_aspect": ""},
        {"target_csr_id": "CS-B", "tara_req_id": "T3", "source_tara_ids": "T3", "tara_text": "T3 text", "match_type": "partial", "confidence": 0.7, "rationale": "r3", "missing_aspect": "m3"},
        {"target_csr_id": "", "tara_req_id": "T4", "source_tara_ids": "T4", "tara_text": "T4 text", "match_type": "gap", "confidence": 0.3, "rationale": "r4", "missing_aspect": "m4"},
    ]


def _atoms():
    return [
        {"atom_id": "A1", "source_customer_ids": ["OEM1"], "subject": "S1", "statement": "OEM1 text", "category": "C", "section": "S"},
        {"atom_id": "A2", "source_customer_ids": ["OEM2"], "subject": "S2", "statement": "OEM2 text", "category": "C", "section": "S"},
        {"atom_id": "A3", "source_customer_ids": ["OEM3"], "subject": "S3", "statement": "OEM3 text", "category": "C", "section": "S"},
    ]


class AtomizationTests(unittest.TestCase):
    def test_atomizes_and_preserves_all_customer_ids(self):
        customer_reqs = [
            {"external_id": "OEM1", "section": "S", "category": "C", "text": "控制器应防拆并关闭调试口。"},
            {"external_id": "OEM2", "section": "S", "category": "C", "text": "应记录安全事件。"},
        ]

        def fake_llm(settings, prompt, payload, temperature=None):
            return {"atoms": [
                {"source_customer_ids": ["OEM1"], "subject": "控制器", "statement": "控制器应防拆。"},
                {"source_customer_ids": ["OEM1"], "subject": "调试口", "statement": "调试口应关闭。"},
                {"source_customer_ids": ["OEM2"], "subject": "日志", "statement": "应记录安全事件。"},
            ]}

        with patch("app.services.oem_csr_merge._call_llm_json", side_effect=fake_llm):
            result = atomize_oem_requirements(customer_reqs, {"api_key": "x"})
        self.assertEqual(result["input_count"], 2)
        self.assertEqual(result["missing_customer_ids"], [])
        self.assertEqual(result["atom_count"], 3)
        covered = {sid for atom in result["atoms"] for sid in atom["source_customer_ids"]}
        self.assertEqual(covered, {"OEM1", "OEM2"})

    def test_exact_subsumed_atoms_are_deduped_without_dropping_ids(self):
        customer_reqs = [{"external_id": "OEM1", "text": "x"}]

        def fake_llm(settings, prompt, payload, temperature=None):
            return {"atoms": [
                {"source_customer_ids": ["OEM1"], "subject": "日志", "statement": "应记录安全事件。"},
                {"source_customer_ids": ["OEM1"], "subject": "日志", "statement": "应记录安全事件"},
            ]}

        with patch("app.services.oem_csr_merge._call_llm_json", side_effect=fake_llm):
            result = atomize_oem_requirements(customer_reqs, {"api_key": "x"})
        self.assertEqual(len(result["atoms"]), 1)
        self.assertEqual(result["atoms"][0]["source_customer_ids"], ["OEM1"])


class OemMappingTests(unittest.TestCase):
    def test_maps_atoms_with_oem_prompt_label(self):
        captured = []

        def fake_llm(settings, prompt, payload, temperature=None):
            captured.append(prompt)
            return {"mappings": [
                {"tara_id": "A1", "target_csr_ids": ["CS-A"], "match_type": "full", "confidence": 0.9, "rationale": "full"},
                {"tara_id": "A2", "target_csr_ids": ["CS-B"], "match_type": "partial", "confidence": 0.7, "rationale": "partial", "missing_aspect": "missing"},
                {"tara_id": "A3", "target_csr_ids": [], "match_type": "gap", "confidence": 0.3, "rationale": "gap", "missing_aspect": "none"},
            ]}

        with patch("app.services.csr_mapping._call_llm_json", side_effect=fake_llm):
            result = map_oem_atoms_to_catalog(
                _atoms(), _catalog(), {"api_key": "x"}, batch_size=3
            )
        self.assertTrue(any("OEM" in prompt for prompt in captured))
        self.assertEqual(result["summary"]["full_count"], 1)
        self.assertEqual(result["summary"]["partial_count"], 1)
        self.assertEqual(result["summary"]["gap_count"], 1)
        self.assertEqual(result["summary"]["mapping_rows"], 3)


class FinalRowsTests(unittest.TestCase):
    def _rows(self):
        oem_mapping = {"rows": [
            {"tara_req_id": "A1", "source_tara_ids": ["OEM1"], "target_csr_id": "CS-A", "match_type": "full", "confidence": 0.9, "rationale": "o1", "missing_aspect": ""},
            {"tara_req_id": "A2", "source_tara_ids": ["OEM2"], "target_csr_id": "CS-B", "match_type": "partial", "confidence": 0.7, "rationale": "o2", "missing_aspect": "om2"},
            {"tara_req_id": "A3", "source_tara_ids": ["OEM3"], "target_csr_id": "", "match_type": "gap", "confidence": 0.4, "rationale": "o3", "missing_aspect": "om3"},
        ]}
        return build_final_rows(_catalog(), _tara_rows(), _atoms(), oem_mapping)

    def test_final_table_contains_all_six_resolutions(self):
        rows, report = self._rows()
        by_id = {row["final_id"]: row for row in rows}
        self.assertEqual(by_id["FCSR-CS-A"]["resolution"], "same")
        self.assertEqual(by_id["FCSR-CS-B"]["resolution"], "partial")
        self.assertEqual(by_id["FCSR-CS-C"]["resolution"], "catalog_only")
        self.assertTrue(any(row["resolution"] == "customer_only" for row in rows))
        self.assertTrue(any(row["resolution"] == "tara_gap" for row in rows))
        self.assertTrue(report["all_covered"])
        self.assertEqual(report["customer_missing_ids"], [])
        self.assertEqual(report["tara_missing_ids"], [])
        self.assertEqual(by_id["FCSR-CS-A"]["source_tara_ids"], ["T1", "T2"])
        self.assertEqual(by_id["FCSR-CS-A"]["source_catalog_csr_ids"], ["CS-A"])

    def test_persistence_keeps_customer_catalog_and_tara_sources(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            project = Project(name="OEM merge test")
            db.add(project)
            db.flush()
            domain = Domain(name="ADCU", project_id=project.id)
            db.add(domain)
            db.flush()
            run = TaraRun(domain_id=domain.id, status="completed")
            db.add(run)
            db.commit()

            rows, _report = self._rows()
            persisted = persist_merged_requirements(
                db, project.id, domain.id, rows, tara_raw_map={}
            )
            self.assertEqual(len(persisted), len(rows))
            sources = db.query(MergedRequirementSource).all()
            self.assertIn("OEM1", {s.source_id for s in sources if s.source_type == "customer"})
            self.assertIn("CS-A", {s.source_id for s in sources if s.source_type == "catalog"})
            self.assertIn("T1", {s.source_id for s in sources if s.source_type == "tara"})

            report_rows = {
                row["final_id"]: row
                for row in collect_merged_requirement_rows(db, domain.id)
            }
            self.assertEqual(report_rows["FCSR-CS-A"]["catalog_ids"], "CS-A")
            self.assertIn("T1", report_rows["FCSR-CS-A"]["source_tara_raw_ids"])
            self.assertEqual(report_rows["FCSR-CS-A"]["resolution"], "same")
        finally:
            db.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
