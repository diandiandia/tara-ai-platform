import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app.models  # noqa: F401 - register all tables
from app.core.database import Base
from app.models.csr_catalog import CsrCatalogEntry, CsrCatalogVersion, TaraCsrMapping
from app.models.domain import Domain
from app.models.tara_run import TaraRun
from app.services.csr_catalog import (
    get_active_catalog,
    get_active_catalog_version,
    import_csr_catalog,
    parse_csr_catalog_xlsx,
)
from app.services.csr_mapping import (
    _validate_mapping_batch,
    build_mapping_report_data,
    map_tara_requirements,
    persist_mappings,
)


def _catalog_workbook(rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["需求ID", "所属模块", "需求名称", "需求描述"])
    for row in rows:
        ws.append(row)
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    wb.save(path)
    return path


def _requirement(tara_id, asset_id=1, asset_name="ADCU", text=None):
    return {
        "tara_req_id": tara_id,
        "source_tara_ids": [tara_id],
        "title": tara_id,
        "text": text or f"{tara_id} requirement",
        "asset_id": asset_id,
        "asset_name": asset_name,
        "asset_type": "software",
        "protocol": "",
        "asset_description": "",
        "requirement_scope": "item",
        "owner": "Tier1",
        "allocated_to": "ADCU",
    }


class CatalogImportTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.path = _catalog_workbook([
            ["CS-HW-001", "硬件与物理安全", "调试接口封堵", "量产件应封闭调试接口"],
            ["CS-DAT-006", "数据安全、隐私与安全日志", "安全事件日志记录", "应记录安全事件日志"],
            ["CS-DBG-003", "调试、工程模式与生产接口", "工程模式管控", "工程模式应受控"],
        ])

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        os.unlink(self.path)

    def test_parse_and_import_are_idempotent(self):
        parsed = parse_csr_catalog_xlsx(self.path)
        self.assertEqual(parsed["row_count"], 3)
        self.assertEqual(parsed["rows"][0]["csr_id"], "CS-HW-001")

        first = import_csr_catalog(self.db, self.path)
        self.assertTrue(first["created"])
        self.assertEqual(first["row_count"], 3)
        self.assertEqual(len(get_active_catalog(self.db)), 3)

        second = import_csr_catalog(self.db, self.path)
        self.assertFalse(second["created"])
        self.assertEqual(second["version"], first["version"])
        self.assertEqual(self.db.query(CsrCatalogEntry).count(), 3)
        self.assertEqual(self.db.query(CsrCatalogVersion).count(), 1)

    def test_duplicate_catalog_id_is_rejected(self):
        bad = _catalog_workbook([
            ["CS-1", "M", "N1", "D1"],
            ["CS-1", "M", "N2", "D2"],
        ])
        try:
            with self.assertRaises(ValueError):
                parse_csr_catalog_xlsx(bad)
        finally:
            os.unlink(bad)


class MappingValidationTests(unittest.TestCase):
    def setUp(self):
        self.catalog_ids = {"CS-HW-001", "CS-BOOT-001", "CS-DAT-006"}
        self.batch = [
            _requirement("T1"),
            _requirement("T2", asset_id=2, asset_name="TBOX"),
            _requirement("T3", asset_id=2, asset_name="TBOX"),
            _requirement("T4"),
        ]

    def test_one_to_many_many_to_one_gap_partial_and_hallucination(self):
        parsed = {
            "mappings": [
                {"tara_id": "T1", "target_csr_ids": ["CS-HW-001", "CS-BOOT-001"], "match_type": "full", "confidence": 0.9, "rationale": "r1"},
                {"tara_id": "T2", "target_csr_ids": ["CS-HW-001"], "match_type": "full", "confidence": 0.8, "rationale": "r2"},
                {"tara_id": "T3", "target_csr_ids": [], "match_type": "gap", "confidence": 0.4, "rationale": "no target", "missing_aspect": "目录缺少该项"},
                {"tara_id": "T4", "target_csr_ids": ["CS-DAT-006", "CS-FAKE-999"], "match_type": "partial", "confidence": 0.5, "rationale": "r4", "missing_aspect": "只覆盖日志记录"},
            ]
        }
        rows = _validate_mapping_batch(self.batch, parsed, self.catalog_ids)

        by_tara = {}
        for row in rows:
            by_tara.setdefault(row["tara_req_id"], []).append(row)

        self.assertEqual({row["target_csr_id"] for row in by_tara["T1"]}, {"CS-HW-001", "CS-BOOT-001"})
        self.assertEqual({row["target_csr_id"] for row in by_tara["T2"]}, {"CS-HW-001"})
        self.assertEqual(by_tara["T3"][0]["match_type"], "gap")
        self.assertEqual(by_tara["T3"][0]["target_csr_id"], "")
        self.assertTrue(by_tara["T3"][0]["needs_review"])
        self.assertEqual(by_tara["T4"][0]["match_type"], "partial")
        self.assertTrue(by_tara["T4"][0]["needs_review"])
        self.assertNotIn("CS-FAKE-999", {row["target_csr_id"] for row in rows})
        self.assertIn("目录外", by_tara["T4"][0]["rationale"])

    def test_missing_tara_output_becomes_error_row(self):
        parsed = {"mappings": [
            {"tara_id": "T1", "target_csr_ids": ["CS-HW-001"], "match_type": "full", "confidence": 0.9},
        ]}
        rows = _validate_mapping_batch(self.batch, parsed, self.catalog_ids)
        by_tara = {row["tara_req_id"]: row for row in rows}
        self.assertEqual(by_tara["T1"]["match_type"], "full")
        for missing in ("T2", "T3", "T4"):
            self.assertEqual(by_tara[missing]["match_type"], "error")
            self.assertTrue(by_tara[missing]["needs_review"])


class MappingPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.path = _catalog_workbook([
            ["CS-HW-001", "硬件与物理安全", "调试接口封堵", "量产件应封闭调试接口"],
            ["CS-BOOT-001", "安全启动与运行完整性", "逐级验签启动链", "逐级验签启动"],
            ["CS-DAT-006", "数据安全、隐私与安全日志", "安全事件日志记录", "应记录安全事件日志"],
        ])
        import_csr_catalog(self.db, self.path)
        self.domain = Domain(id=1, project_id=1, name="ADCU", status="completed", progress=100)
        self.run = TaraRun(id=1, domain_id=1, status="completed", progress=100)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        os.unlink(self.path)

    def _fake_llm(self, settings, system_prompt, payload, temperature=None):
        mappings = []
        for requirement in payload["requirements"]:
            tara_id = requirement["tara_id"]
            if tara_id == "T1":
                mappings.append({"tara_id": tara_id, "target_csr_ids": ["CS-HW-001", "CS-BOOT-001"], "match_type": "full", "confidence": 0.9, "rationale": "auth"})
            elif tara_id == "T2":
                mappings.append({"tara_id": tara_id, "target_csr_ids": ["CS-HW-001"], "match_type": "full", "confidence": 0.8, "rationale": "auth2"})
            elif tara_id == "T3":
                mappings.append({"tara_id": tara_id, "target_csr_ids": [], "match_type": "gap", "confidence": 0.3, "rationale": "missing", "missing_aspect": "no target"})
            else:
                mappings.append({"tara_id": tara_id, "target_csr_ids": ["CS-DAT-006"], "match_type": "partial", "confidence": 0.5, "rationale": "partial", "missing_aspect": "仅日志"})
        return {"mappings": mappings}

    def test_map_persist_and_report(self):
        requirements = [
            _requirement("T1"),
            _requirement("T2", asset_id=2, asset_name="TBOX"),
            _requirement("T3", asset_id=2, asset_name="TBOX"),
            _requirement("T4"),
        ]
        catalog = get_active_catalog(self.db)
        with patch("app.services.csr_mapping._call_llm_json", side_effect=self._fake_llm):
            mapped = map_tara_requirements(
                requirements,
                catalog,
                {"api_key": "test", "api_base_url": "http://fake", "model_name": "fake"},
                batch_size=2,
            )
        self.assertEqual(mapped["summary"]["full_count"], 2)
        self.assertEqual(mapped["summary"]["partial_count"], 1)
        self.assertEqual(mapped["summary"]["gap_count"], 1)

        saved = persist_mappings(
            self.db, self.domain, self.run, mapped["rows"],
            catalog_version=get_active_catalog_version(self.db).version,
        )
        self.assertGreaterEqual(saved, 5)
        self.assertEqual(self.db.query(TaraCsrMapping).count(), saved)

        report = build_mapping_report_data(self.db, 1, 1)
        self.assertIsNotNone(report)
        rows = {row["csr_id"]: row for row in report["csr_rows"]}
        self.assertEqual(set(rows), {"CS-HW-001", "CS-BOOT-001", "CS-DAT-006"})
        self.assertEqual(rows["CS-HW-001"]["matched_tara_count"], 2)
        self.assertIn("T1", rows["CS-HW-001"]["source_requirement_ids"])
        self.assertIn("T2", rows["CS-HW-001"]["source_requirement_ids"])
        self.assertEqual(rows["CS-BOOT-001"]["source_requirement_ids"], ["T1"])
        self.assertEqual(report["summary"]["input_count"], 4)
        self.assertEqual(report["summary"]["full_count"], 2)
        self.assertEqual(report["summary"]["partial_count"], 1)
        self.assertEqual(report["summary"]["gap_count"], 1)
        self.assertTrue(any(row["tara_req_id"] == "T3" for row in report["unmapped"]))
        self.assertEqual(report["tara_to_targets"]["T1"], ["CS-BOOT-001", "CS-HW-001"])


if __name__ == "__main__":
    unittest.main()
