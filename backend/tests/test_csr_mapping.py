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
from app.models.asset import Asset
from app.models.csr_catalog import CsrCatalogEntry, CsrCatalogVersion, TaraCsrMapping
from app.models.domain import Domain
from app.models.system_settings import SystemSettings
from app.models.tara_run import TaraRun
from app.models.tara_step import TaraStep
from app.services.csr_catalog import (
    get_active_catalog,
    get_active_catalog_version,
    import_csr_catalog,
    parse_csr_catalog_xlsx,
)
from app.services.csr_mapping import (
    _validate_mapping_batch,
    build_mapping_report_data,
    map_domain_run_to_fixed_catalog,
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

    def test_conflict_is_recorded_only_when_allowed(self):
        parsed = {"mappings": [
            {
                "tara_id": "T1",
                "target_csr_ids": ["CS-HW-001"],
                "match_type": "conflict",
                "confidence": 0.7,
                "rationale": "互斥",
                "conflict_note": "TARA 要求禁用调试口，目录允许授权启用",
            },
        ]}
        allowed = _validate_mapping_batch(
            self.batch[:1], parsed, self.catalog_ids, allow_conflict=True
        )
        self.assertEqual(allowed[0]["match_type"], "conflict")
        self.assertEqual(
            allowed[0]["conflict_note"], "TARA 要求禁用调试口，目录允许授权启用"
        )
        self.assertTrue(allowed[0]["needs_review"])

        not_allowed = _validate_mapping_batch(self.batch[:1], parsed, self.catalog_ids)
        self.assertNotEqual(not_allowed[0]["match_type"], "conflict")

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

    def test_tara_conflict_row_is_reported_and_exported(self):
        version = get_active_catalog_version(self.db).version
        self.db.add(TaraCsrMapping(
            project_id=1,
            domain_id=1,
            run_id=1,
            asset_id=1,
            asset_name="ADCU",
            tara_req_id="T9",
            source_tara_ids="T9",
            tara_text="量产件必须永久禁用调试口",
            catalog_version=version,
            target_csr_id="CS-HW-001",
            match_type="conflict",
            confidence=0.6,
            rationale="互斥",
            missing_aspect="",
            conflict_note="目录允许授权后启用调试口，与必须禁用互斥",
            needs_review=True,
        ))
        self.db.commit()

        report = build_mapping_report_data(self.db, 1, 1)
        self.assertEqual(report["summary"]["conflict_count"], 1)
        self.assertEqual(len(report["conflicts"]), 1)
        conflict = report["conflicts"][0]
        self.assertEqual(conflict["tara_req_id"], "T9")
        self.assertEqual(conflict["target_csr_id"], "CS-HW-001")
        self.assertEqual(conflict["conflict_note"], "目录允许授权后启用调试口，与必须禁用互斥")
        self.assertTrue(conflict["target_csr_text"])

        statuses = {row["csr_id"]: row["mapping_status"] for row in report["csr_rows"]}
        self.assertEqual(statuses["CS-HW-001"], "conflict")


class AutoMappingTests(unittest.TestCase):
    """TARA 跑批结束时自动执行的映射：无目录/无大模型时跳过，正常时落库。"""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.domain = Domain(id=1, project_id=1, name="ADCU", status="completed", progress=100)
        self.run = TaraRun(id=1, domain_id=1, status="completed", progress=100)
        self.db.add_all([self.domain, self.run])
        self.db.add(Asset(
            id=1, domain_id=1, name="ADCU", asset_type="hardware", status="confirmed",
        ))
        self.db.add(TaraStep(
            run_id=1,
            asset_id=1,
            stage="stage5",
            status="completed",
            input_hash="hash",
            analysis_result={"final_output": {"requirements": [{
                "cybersecurity_requirement_id": "T1",
                "cybersecurity_requirement": "调试接口应在量产阶段禁用",
                "source_requirement_ids": ["R1"],
            }]}},
        ))
        self.db.commit()
        self.catalog_path = _catalog_workbook([
            ["CS-HW-001", "硬件与物理安全", "调试接口封堵", "量产件应封闭调试接口"],
        ])

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        os.unlink(self.catalog_path)

    def test_skips_when_no_catalog(self):
        result = map_domain_run_to_fixed_catalog(self.db, self.domain, self.run)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_active_catalog")
        self.assertEqual(self.db.query(TaraCsrMapping).count(), 0)

    def test_skips_when_no_llm_settings(self):
        import_csr_catalog(self.db, self.catalog_path)
        result = map_domain_run_to_fixed_catalog(self.db, self.domain, self.run)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_llm_settings")

    def test_maps_run_and_persists_rows(self):
        import_csr_catalog(self.db, self.catalog_path)
        self.db.add(SystemSettings(
            api_base_url="http://fake", api_key="test", model_name="fake",
        ))
        self.db.commit()

        prompts = []

        def fake_llm(settings, prompt, payload, temperature=None):
            prompts.append(prompt)
            return {"mappings": [
                {
                    "tara_id": requirement["tara_id"],
                    "target_csr_ids": ["CS-HW-001"],
                    "match_type": "full",
                    "confidence": 0.9,
                    "rationale": "cover",
                }
                for requirement in payload["requirements"]
            ]}

        with patch("app.services.csr_mapping._call_llm_json", side_effect=fake_llm):
            result = map_domain_run_to_fixed_catalog(self.db, self.domain, self.run)

        self.assertEqual(result["status"], "completed")
        self.assertGreaterEqual(result["persisted_rows"], 1)
        self.assertEqual(self.db.query(TaraCsrMapping).count(), result["persisted_rows"])
        self.assertEqual(result["summary"]["full_count"], 1)
        # 自动映射开启冲突识别
        self.assertIn("conflict", prompts[0])
        report = build_mapping_report_data(self.db, 1, 1)
        self.assertEqual(report["summary"]["full_count"], 1)
        self.assertEqual(report["summary"]["conflict_count"], 0)


if __name__ == "__main__":
    unittest.main()
