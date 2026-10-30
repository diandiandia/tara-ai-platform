"""Deterministic tests for the hierarchical CSR dedup pipeline.

These tests do not call a real LLM.  The module-level ``_call_llm_json`` is
patched so the tests can exercise candidate blocking, cross-asset merging,
traceability preservation, atomization units and final coverage checks.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services import csr_hierarchical_dedup as dedup


def _requirement(rid, text, alloc="n1", verification="测试 / Test", acceptance="通过"):
    return {
        "cybersecurity_requirement_id": rid,
        "cybersecurity_requirement": text,
        "subject": "系统",
        "requirement_scope": "item",
        "owner": "Tier1",
        "allocated_to": alloc,
        "verification_method": verification,
        "acceptance_criteria": acceptance,
        "title": rid,
    }


def _asset(asset_id, name):
    return {"id": asset_id, "name": name}


def _fake_llm(settings, system_prompt, payload):
    if "duplicate_pairs" in system_prompt:
        records = payload.get("records", [])
        source_sets = [set(record.get("source_requirement_ids") or []) for record in records]
        if any("ID1" in ids for ids in source_sets) and any("ID2" in ids for ids in source_sets):
            a = next(r["record_id"] for r, ids in zip(records, source_sets) if "ID1" in ids)
            b = next(r["record_id"] for r, ids in zip(records, source_sets) if "ID2" in ids)
            return {"duplicate_pairs": [{"a": a, "b": b, "reason": "test duplicate"}]}
        return {"duplicate_pairs": []}

    if "原子化拆解" in system_prompt:
        atoms = []
        for unit in payload.get("requirements", []):
            atoms.append({
                "parent_record_id": unit.get("record_id"),
                "atom_id": "ATOM_" + str(unit.get("record_id")),
                "subject": "系统",
                "statement": "系统应记录调试操作。 / The system shall log debug operations.",
                "source_requirement_ids": unit.get("source_requirement_ids") or [],
                "requirement_scope": unit.get("requirement_scope") or "",
                "owner": unit.get("owner") or "",
                "allocated_to": unit.get("allocated_to") or "",
                "verification_method": unit.get("verification_method") or "",
                "acceptance_criteria": unit.get("acceptance_criteria") or "",
            })
        return {"atomic_requirements": atoms}

    return {}


class HierarchyShapeTests(unittest.TestCase):
    def test_pairwise_to_four_then_k1_absorbs_k2_k3_k4(self):
        groups = [[{"record_id": f"R{i}"}] for i in range(16)]
        calls = []

        def fake_dedup(records, settings, atom_mode=False, label=""):
            calls.append(label)
            return list(records)

        with patch.object(dedup, "dedup_records", side_effect=fake_dedup):
            result = dedup.hierarchical_dedup(groups, None)

        self.assertEqual(len(result), 16)
        self.assertEqual(sum(1 for c in calls if c.startswith("round-1-")), 8)
        self.assertEqual(sum(1 for c in calls if c.startswith("round-2-")), 4)
        self.assertEqual(
            [c for c in calls if c.startswith("absorb-")],
            ["absorb-K2", "absorb-K3", "absorb-K4"],
        )


class MergeTraceabilityTests(unittest.TestCase):
    def test_cross_asset_merge_keeps_both_statements_and_unions_metadata(self):
        r1 = dedup.make_record(
            _asset(1, "AssetA"),
            _requirement("ID1", "系统应记录调试操作。 / The system shall log debug operations.", alloc="n1"),
        )
        r2 = dedup.make_record(
            _asset(2, "AssetB"),
            _requirement(
                "ID2",
                "系统应记录调试操作并保留证据。 / The system shall log debug operations and retain evidence.",
                alloc="n2",
                verification="分析 / Analysis",
                acceptance="全部记录",
            ),
        )
        merged = dedup._merge_component([r1, r2], [0, 1])

        self.assertEqual(merged["source_requirement_ids"], ["ID1", "ID2"])
        self.assertEqual(merged["allocated_to"], "n1; n2")
        self.assertIn("测试 / Test", merged["verification_method"])
        self.assertIn("分析 / Analysis", merged["verification_method"])
        self.assertIn("通过", merged["acceptance_criteria"])
        self.assertIn("全部记录", merged["acceptance_criteria"])
        self.assertEqual(sorted(merged["applicable_assets"]), ["AssetA", "AssetB"])

        texts = merged["source_texts"]
        self.assertEqual(len(texts), 2)
        self.assertEqual(
            sorted(sid for item in texts for sid in item["source_requirement_ids"]),
            ["ID1", "ID2"],
        )

    def test_identical_texts_merge_into_one_source_entry(self):
        text = "系统应记录调试操作。 / The system shall log debug operations."
        r1 = dedup.make_record(_asset(1, "AssetA"), _requirement("ID1", text))
        r2 = dedup.make_record(_asset(2, "AssetB"), _requirement("ID2", text))
        merged = dedup._merge_component([r1, r2], [0, 1])

        self.assertEqual(len(merged["source_texts"]), 1)
        self.assertEqual(merged["source_texts"][0]["source_requirement_ids"], ["ID1", "ID2"])


class FullScanFallbackTests(unittest.TestCase):
    def test_full_scan_failure_falls_back_to_exact_lexical_pairs(self):
        text = "系统应记录调试操作。 / The system shall log debug operations."
        r1 = dedup.make_record(_asset(1, "AssetA"), _requirement("ID1", text))
        r2 = dedup.make_record(_asset(2, "AssetB"), _requirement("ID2", text))

        with patch.object(dedup, "_call_llm_json", side_effect=RuntimeError("timeout")):
            out = dedup.dedup_records(
                [r1, r2],
                {"api_key": "test", "api_base_url": "http://fake", "model_name": "fake"},
                label="fallback-test",
            )

        self.assertEqual(len(out), 1)
        self.assertEqual(dedup.source_ids_of(out[0]), ["ID1", "ID2"])


class AtomMergeSafetyTests(unittest.TestCase):
    def test_same_statement_with_punctuation_is_safe(self):
        a = {"text": "系统应检测未授权修改。 / The system shall detect unauthorized modification."}
        b = {"text": "系统应检测未授权修改 / The system shall detect unauthorized modification"}
        self.assertTrue(dedup._atom_merge_safe(a, b))

    def test_different_condition_is_not_safe(self):
        a = {"text": "系统应检测未授权修改。 / The system shall detect unauthorized modification."}
        b = {"text": "系统应检测未授权删除。 / The system shall detect unauthorized deletion."}
        self.assertFalse(dedup._atom_merge_safe(a, b))

    def test_unsafe_llm_accepted_atom_pair_is_not_merged(self):
        a = dedup.make_record(
            _asset(1, "AssetA"),
            _requirement("ID1", "系统应检测未授权修改。 / The system shall detect unauthorized modification."),
        )
        b = dedup.make_record(
            _asset(1, "AssetA"),
            _requirement("ID2", "系统应检测未授权删除。 / The system shall detect unauthorized deletion."),
        )
        with patch.object(dedup, "_full_scan_duplicate_pairs", return_value=[(0, 1)]):
            out = dedup.dedup_records(
                [a, b],
                {"api_key": "test", "api_base_url": "http://fake", "model_name": "fake"},
                atom_mode=True,
                label="atom-safety",
            )
        self.assertEqual(len(out), 2)


class CompatibilityTests(unittest.TestCase):
    def _record(self, asset_id, asset_name, alloc="n1", verification="测试 / Test"):
        return dedup.make_record(
            _asset(asset_id, asset_name),
            _requirement("ID" + str(asset_id), "系统应记录调试操作。", alloc=alloc, verification=verification),
        )

    def test_disjoint_assets_may_differ_in_allocation_and_verification(self):
        a = self._record(1, "AssetA", alloc="n1", verification="测试 / Test")
        b = self._record(2, "AssetB", alloc="n2", verification="分析 / Analysis")
        self.assertTrue(dedup._records_compatible(a, b))

    def test_same_asset_must_match_allocation_and_verification(self):
        a = self._record(1, "AssetA", alloc="n1", verification="测试 / Test")
        b = self._record(1, "AssetA", alloc="n2", verification="分析 / Analysis")
        self.assertFalse(dedup._records_compatible(a, b))

    def test_different_scope_is_never_compatible(self):
        a = self._record(1, "AssetA")
        b = self._record(2, "AssetB")
        b["requirement_scope"] = "operational_environment"
        self.assertFalse(dedup._records_compatible(a, b))

    def test_cross_asset_candidate_is_not_blocked_by_allocation(self):
        a = self._record(1, "AssetA", alloc="n1")
        b = self._record(2, "AssetB", alloc="n2")
        # Same normalized text: always a candidate.
        pairs = dedup.generate_candidate_pairs([a, b])
        self.assertEqual(pairs, [(0, 1)])


class AtomizationUnitTests(unittest.TestCase):
    def test_merged_record_yields_one_unit_per_distinct_statement(self):
        r1 = dedup.make_record(
            _asset(1, "AssetA"),
            _requirement("ID1", "系统应记录调试操作。 / The system shall log debug operations."),
        )
        r2 = dedup.make_record(
            _asset(2, "AssetB"),
            _requirement("ID2", "系统应保留调试证据。 / The system shall retain debug evidence."),
        )
        merged = dedup._merge_component([r1, r2], [0, 1])
        units = dedup._atomization_units([merged])

        self.assertEqual(len(units), 2)
        self.assertEqual(sorted(units[0]["source_requirement_ids"] + units[1]["source_requirement_ids"]), ["ID1", "ID2"])
        self.assertTrue(all(unit["record_id"].startswith(merged["record_id"] + "::T") for unit in units))

    def test_pipeline_merges_cross_asset_and_keeps_traceability(self):
        assets_data = [
            {
                "asset": _asset(1, "AssetA"),
                "requirements": [
                    _requirement("ID1", "系统应记录调试操作。 / The system shall log debug operations.", alloc="n1"),
                ],
            },
            {
                "asset": _asset(2, "AssetB"),
                "requirements": [
                    _requirement(
                        "ID2",
                        "系统应记录调试操作并保留证据。 / The system shall log debug operations and retain evidence.",
                        alloc="n2",
                        verification="分析 / Analysis",
                        acceptance="全部记录",
                    ),
                ],
            },
        ]

        with patch.object(dedup, "_call_llm_json", side_effect=_fake_llm):
            finals, report = dedup.run_pipeline(assets_data, {"api_key": "test", "api_base_url": "http://fake", "model_name": "fake"})

        self.assertTrue(report["all_covered"], report)
        self.assertEqual(report["missing"], [])
        self.assertEqual(report["extra"], [])
        # Both source requirements atomize to the same atomic statement, so
        # the atomic dedup step merges them into one row with both IDs.
        self.assertEqual(len(finals), 1)
        self.assertEqual(dedup.source_ids_of(finals[0]), ["ID1", "ID2"])
        self.assertEqual(sorted(finals[0]["applicable_assets"]), ["AssetA", "AssetB"])
        self.assertEqual(finals[0]["allocated_to"], "n1; n2")


class CoverageValidationTests(unittest.TestCase):
    def test_validate_final_reports_missing_and_extra_ids(self):
        rows = [{"source_requirement_ids": ["ID1", "ID_EXTRA"]}]
        report = dedup.validate_final(rows, {"ID1", "ID2"})
        self.assertFalse(report["all_covered"])
        self.assertEqual(report["missing"], ["ID2"])
        self.assertEqual(report["extra"], ["ID_EXTRA"])
        self.assertEqual(report["covered_count"], 1)


if __name__ == "__main__":
    unittest.main()
