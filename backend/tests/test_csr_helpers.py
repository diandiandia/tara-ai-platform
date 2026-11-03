import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.taxonomy import CANONICAL_DOMAINS, clean_major_category
from app.services.csr_synthesis import (
    extract_subject,
    extract_subject_and_clauses,
    get_csr_traceability_maps,
    semantic_tokens,
    _jaccard,
)


class CsrHelperTests(unittest.TestCase):
    def test_extract_subject_and_clauses(self):
        text = "【主体】CAN收发器；【要求】应过滤非白名单报文；应校验消息认证码MAC与报文新鲜度值。"
        atoms = extract_subject_and_clauses(text)
        self.assertEqual(len(atoms), 2)
        self.assertEqual(atoms[0][0], "CAN收发器")
        self.assertIn("过滤", atoms[0][1])
        self.assertEqual(atoms[1][0], "CAN收发器")
        self.assertIn("校验", atoms[1][1])

    def test_extract_subject(self):
        text = "【主体】安全网关；【要求】应阻止未知源IP的报文转发。"
        self.assertEqual(extract_subject(text), "安全网关")

        en_text = "[Subject] HSM; [Requirement] Shall store root keys securely."
        self.assertEqual(extract_subject(en_text), "HSM")

    def test_semantic_tokens_and_jaccard(self):
        t1 = semantic_tokens("控制器应支持固件完整性校验与防回滚")
        t2 = semantic_tokens("控制器应支持固件完整性校验与防回退")
        sim = _jaccard(t1, t2)
        self.assertTrue(sim > 0.8)


class TaxonomyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
