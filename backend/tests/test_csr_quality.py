import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.taxonomy import CANONICAL_DOMAINS, clean_major_category
from app.services import csr_synthesis
from app.services.csr_synthesis import (
    UNECE_R155_ANNEX5_GUIDELINES,
    enforce_csr_coverage,
    get_csr_traceability_maps,
)


def _item(raw_id, name="AssetA"):
    return {
        "raw_id": raw_id,
        "asset_name": name,
        "sub_title": "子类 / Sub",
        "title": "标题 / Title",
        "req": "需求 / Requirement",
    }


class CoverageContractTests(unittest.TestCase):
    def test_complete_coverage(self):
        items = [_item("CSR_1"), _item("CSR_2")]
        cons = [
            {"source_requirement_ids": ["CSR_1"], "cybersecurity_requirement": "A"},
            {"source_requirement_ids": ["CSR_2"], "cybersecurity_requirement": "B"},
        ]
        out, report = enforce_csr_coverage(items, cons)
        self.assertTrue(report["complete"])
        self.assertTrue(report["all_covered"])
        self.assertEqual(report["missing_raw_ids"], [])
        self.assertEqual(len(out), 2)

    def test_missing_ids_get_fallback_entries(self):
        items = [_item("CSR_1"), _item("CSR_2"), _item("CSR_3")]
        cons = [{"source_requirement_ids": ["CSR_1"], "cybersecurity_requirement": "A"}]
        out, report = enforce_csr_coverage(items, cons)
        self.assertFalse(report["complete"])
        self.assertTrue(report["all_covered"])
        self.assertEqual(set(report["missing_raw_ids"]), {"CSR_2", "CSR_3"})
        self.assertEqual(report["fallback_count"], 2)
        covered = {sid for c in out for sid in c["source_requirement_ids"]}
        self.assertEqual(covered, {"CSR_1", "CSR_2", "CSR_3"})

    def test_hallucinated_ids_are_stripped(self):
        items = [_item("CSR_1")]
        cons = [{"source_requirement_ids": ["CSR_1", "CSR_FAKE"], "cybersecurity_requirement": "A"}]
        out, report = enforce_csr_coverage(items, cons)
        self.assertEqual(report["extra_raw_ids"], ["CSR_FAKE"])
        self.assertEqual(out[0]["source_requirement_ids"], ["CSR_1"])

    def test_duplicate_ids_are_reported_but_kept(self):
        items = [_item("CSR_1"), _item("CSR_2")]
        cons = [
            {"source_requirement_ids": ["CSR_1"], "cybersecurity_requirement": "A"},
            {"source_requirement_ids": ["CSR_1", "CSR_2"], "cybersecurity_requirement": "B"},
        ]
        out, report = enforce_csr_coverage(items, cons)
        self.assertEqual(report["duplicate_raw_ids"], {"CSR_1": 2})
        self.assertTrue(report["all_covered"])

    def test_merge_and_split_are_both_allowed(self):
        items = [_item("CSR_1"), _item("CSR_2"), _item("CSR_3"), _item("CSR_4")]
        cons = [
            # merge: CSR_1 + CSR_2 -> one CSR
            {"source_requirement_ids": ["CSR_1", "CSR_2"], "cybersecurity_requirement": "Merged"},
            # split: CSR_3 -> two CSRs
            {"source_requirement_ids": ["CSR_3"], "cybersecurity_requirement": "Split A"},
            {"source_requirement_ids": ["CSR_3"], "cybersecurity_requirement": "Split B"},
            {"source_requirement_ids": ["CSR_4"], "cybersecurity_requirement": "Direct"},
        ]
        out, report = enforce_csr_coverage(items, cons)
        self.assertTrue(report["all_covered"])
        self.assertEqual(report["duplicate_raw_ids"], {"CSR_3": 2})
        raw_to_csrs = {}
        for item in out:
            for sid in item["source_requirement_ids"]:
                raw_to_csrs.setdefault(sid, []).append(item["cybersecurity_requirement"])
        self.assertEqual(len(raw_to_csrs["CSR_1"]), 1)
        self.assertEqual(len(raw_to_csrs["CSR_2"]), 1)
        self.assertEqual(sorted(raw_to_csrs["CSR_3"]), ["Split A", "Split B"])
        self.assertEqual(raw_to_csrs["CSR_4"], ["Direct"])

    def test_missing_verification_is_filled_and_reported(self):
        items = [_item("CSR_1")]
        cons = [{"source_requirement_ids": ["CSR_1"], "cybersecurity_requirement": "A"}]
        out, report = enforce_csr_coverage(items, cons)
        self.assertEqual(report["verification_missing_count"], 1)
        self.assertTrue(out[0]["verification_method"].startswith("检查/测试"))
        self.assertEqual(out[0]["_coverage_status"], "verification_missing")


class TaxonomyAndGuidelineTests(unittest.TestCase):
    def test_domains_are_unique_and_canonical(self):
        self.assertEqual(len(CANONICAL_DOMAINS), len(set(CANONICAL_DOMAINS)))
        self.assertIn("安全监控、测试与响应 / Security Monitoring, Testing & Response", CANONICAL_DOMAINS)
        self.assertIn("安全开发、供应链与运维 / Secure Development, Supply Chain & Operations", CANONICAL_DOMAINS)

    def test_legacy_domain_aliases_map_to_canonical(self):
        self.assertEqual(
            clean_major_category("Security Monitoring & Defense"),
            "安全监控、测试与响应 / Security Monitoring, Testing & Response",
        )
        self.assertEqual(
            clean_major_category("Secure Development & Operations"),
            "安全开发、供应链与运维 / Secure Development, Supply Chain & Operations",
        )

    def test_guidelines_do_not_hardcode_technologies(self):
        text = "\n".join(UNECE_R155_ANNEX5_GUIDELINES.values())
        forbidden = [
            "TLS 1.2", "TLS 1.3", "SecOC", "mTLS", "MACsec",
            "UDS 0x27", "AES-GCM", "AES-256", "SM4", "RSA-4096",
            "FIPS 140-2", "50ms", "12位", "IEEE 802.1X", "eFuse", "OTP",
        ]
        hits = [term for term in forbidden if term in text]
        self.assertEqual(hits, [], f"guidelines still contain concrete technology terms: {hits}")


class TraceabilityStrictnessTests(unittest.TestCase):
    def test_strict_mode_does_not_guess_substring_matches(self):
        stage5_out = {
            "requirements": [{
                "cybersecurity_requirement": "alpha beta",
                "cybersecurity_requirement_id": "CSR-001",
            }],
            "summarized_csrs": [{
                "cybersecurity_requirement": "alpha beta gamma",
                "csr_id": "CSR_X",
            }],
        }
        req_map = {"alpha beta": "CSR_AAAAAAAA"}

        _, strict_map = get_csr_traceability_maps(stage5_out, req_map, strict=True)
        self.assertEqual(strict_map, {})

        _, loose_map = get_csr_traceability_maps(stage5_out, req_map, strict=False)
        self.assertEqual(loose_map, {"CSR_X": ["CSR_AAAAAAAA"]})


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return {"choices": [{"message": {"content": json.dumps(self._payload, ensure_ascii=False)}}]}


class _FakeSettings:
    api_key = "test-key"
    api_base_url = "http://fake.local/v1"
    model_name = "fake-model"


class _FakeQuery:
    def first(self):
        return _FakeSettings()


class _FakeDB:
    def query(self, _model):
        return _FakeQuery()


class CoverageRetryTests(unittest.TestCase):
    def test_missing_coverage_triggers_one_repair_retry(self):
        items = [_item("CSR_1"), _item("CSR_2")]
        calls = []

        def fake_post(url, headers, payload, max_retries=3, initial_delay=1.0):
            calls.append(payload)
            if len(calls) == 1:
                return _FakeResponse({"consolidated_csrs": [{
                    "sub_category": "子类 / Sub",
                    "title": "标题 / Title",
                    "cybersecurity_requirement": "需求 / Requirement",
                    "verification_method": "测试 / Test",
                    "requirement_type": "product",
                    "source_requirement_ids": ["CSR_1"],
                }]})
            return _FakeResponse({"consolidated_csrs": [{
                "sub_category": "子类 / Sub",
                "title": "标题 / Title",
                "cybersecurity_requirement": "需求 / Requirement",
                "verification_method": "测试 / Test",
                "requirement_type": "product",
                "source_requirement_ids": ["CSR_1", "CSR_2"],
            }]})

        with patch.object(csr_synthesis, "post_llm_request_with_retry", side_effect=fake_post):
            _, report = csr_synthesis.synthesize_domain_csrs_with_llm(
                _FakeDB(), "硬件安全 / Hardware Security", items
            )

        self.assertEqual(len(calls), 2)
        self.assertTrue(report["all_covered"])
        self.assertEqual(report.get("mode"), "llm_coverage_retry")


if __name__ == "__main__":
    unittest.main()
