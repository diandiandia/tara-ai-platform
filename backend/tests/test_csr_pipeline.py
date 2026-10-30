"""Unit tests for CSR Pipeline V2 and its integration with csr_synthesis."""

import unittest
from types import SimpleNamespace

from app.services.csr_pipeline import (
    run_pipeline,
    create_context,
    default_config,
    PipelineRecord,
    FinalOutput,
)
from app.services.csr_pipeline.hash_dedup import hash_dedup
from app.services.csr_pipeline.boundary_mapper import map_boundary_class
from app.services.csr_pipeline.output_layer import finalize_output
from app.services.csr_pipeline.llm_proxy import LLMProxy
from app.services.csr_synthesis import hierarchical_synthesize_project_csrs


class TestCSRPipelineV2(unittest.TestCase):

    def setUp(self):
        self.config = default_config()
        self.context = create_context(config=self.config)

    def test_hash_dedup_exact(self):
        """Exact duplicates should merge, keeping all source IDs and applicable assets."""
        r1 = PipelineRecord(
            record_id="ASSET-1::CSR-001",
            requirement_text="系统应关闭JTAG调试接口。",
            source_ids=["TARA-1"],
            asset_name="Gateway",
        )
        r2 = PipelineRecord(
            record_id="ASSET-2::CSR-002",
            requirement_text="  系统应关闭JTAG调试接口  ",
            source_ids=["TARA-2"],
            asset_name="TBox",
        )
        r3 = PipelineRecord(
            record_id="ASSET-1::CSR-003",
            requirement_text="系统应在刷写前校验固件签名。",
            source_ids=["TARA-3"],
            asset_name="Gateway",
        )

        deduped = hash_dedup([r1, r2, r3], self.config)
        self.assertEqual(len(deduped), 2)

        # Find the merged record for JTAG
        jtag_rec = next(r for r in deduped if "JTAG" in r.requirement_text)
        self.assertIn("TARA-1", jtag_rec.source_ids)
        self.assertIn("TARA-2", jtag_rec.source_ids)
        self.assertIn("Gateway", jtag_rec.applicable_assets)
        self.assertIn("TBox", jtag_rec.applicable_assets)

    def test_boundary_mapping_rules(self):
        """Boundary mapping should recognize keywords via rule matching without LLM."""
        r1 = PipelineRecord(
            record_id="R1",
            requirement_text="【主体】网关；【要求】应关闭JTAG调试端口",
            source_ids=["S1"],
            asset_name="Gateway",
        )
        r2 = PipelineRecord(
            record_id="R2",
            requirement_text="【主体】TBox；【要求】应支持OTA远程固件更新包验签",
            source_ids=["S2"],
            asset_name="TBox",
        )
        proxy = LLMProxy(settings=None)
        mapped = map_boundary_class([r1, r2], proxy, self.config)
        self.assertEqual(mapped[0].boundary_class, "调试入口")
        self.assertEqual(mapped[1].boundary_class, "固件写入")

    def test_finalize_output_and_numbering(self):
        """Output layer should assign taxonomy prefix numbers and verify 100% coverage."""
        r1 = PipelineRecord(
            record_id="R1",
            requirement_text="应关闭JTAG调试端口",
            source_ids=["S1", "S2"],
            asset_name="Gateway",
            topic_label="4.4 安全调试",
        )
        r2 = PipelineRecord(
            record_id="R2",
            requirement_text="应验证OTA固件签名",
            source_ids=["S3"],
            asset_name="TBox",
            topic_label="4.6 OTA 刷写安全",
        )

        out = finalize_output([r1, r2], self.config)
        self.assertTrue(out.coverage_ok)
        self.assertEqual(len(out.missing_source_ids), 0)
        self.assertEqual(len(out.records), 2)

        prefixes = [r.csr_number.split("-")[1] for r in out.records]
        self.assertIn("DBG", prefixes)
        self.assertIn("OTA", prefixes)

    def test_end_to_end_pipeline_fallback_graceful(self):
        """End-to-end pipeline run without LLM credentials degrades gracefully without losing IDs."""
        r1 = PipelineRecord(
            record_id="A1::01",
            requirement_text="系统应关闭JTAG调试接口。",
            source_ids=["TARA-101"],
            asset_name="ADCU",
        )
        r2 = PipelineRecord(
            record_id="A2::01",
            requirement_text="系统应关闭JTAG调试接口。",
            source_ids=["TARA-102"],
            asset_name="Gateway",
        )
        r3 = PipelineRecord(
            record_id="A1::02",
            requirement_text="系统应记录所有安全审计日志。",
            source_ids=["TARA-103"],
            asset_name="ADCU",
        )

        out = run_pipeline([r1, r2, r3], self.context)
        self.assertIsInstance(out, FinalOutput)
        self.assertEqual(len(out.records), 2)
        self.assertTrue(out.coverage_ok)

        all_covered_ids = set().union(*[r.source_ids for r in out.records])
        self.assertEqual(all_covered_ids, {"TARA-101", "TARA-102", "TARA-103"})

    def test_hierarchical_synthesize_project_csrs_v2_integration(self):
        """Integration test with hierarchical_synthesize_project_csrs in csr_synthesis."""
        step5 = SimpleNamespace(
            stage="stage5",
            asset_id=1,
            analysis_result={
                "final_output": {
                    "requirements": [
                        {
                            "csr_id": "CSR-01",
                            "cybersecurity_requirement": "【主体】网关；【要求】应关闭JTAG调试端口",
                            "source_requirement_ids": ["TARA-REQ-1"],
                            "verification_method": "测试",
                        },
                        {
                            "csr_id": "CSR-02",
                            "cybersecurity_requirement": "【主体】网关；【要求】应记录所有安全审计日志",
                            "source_requirement_ids": ["TARA-REQ-2"],
                            "verification_method": "审查",
                        },
                    ]
                }
            },
        )
        asset = SimpleNamespace(id=1, name="网关", status="confirmed")

        final_rows = hierarchical_synthesize_project_csrs(
            db=None,
            domain=None,
            steps=[step5],
            assets=[asset],
        )

        self.assertIsInstance(final_rows, list)
        self.assertEqual(len(final_rows), 2)

        observed_ids = set().union(*[set(r["_raw_source_ids"]) for r in final_rows])
        self.assertEqual(observed_ids, {"TARA-REQ-1", "CSR-01", "TARA-REQ-2", "CSR-02"})

        for row in final_rows:
            self.assertTrue(row["number"].startswith("CSR-"))
            self.assertIn("cybersecurity_requirement", row)
            self.assertIn("applicable_assets", row)
            self.assertEqual(row["applicable_assets"], "网关")


if __name__ == "__main__":
    unittest.main()
