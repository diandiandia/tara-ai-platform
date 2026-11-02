import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.req_merge import atomize_and_dedup_customer_requirements


class CustomerAtomizationFallbackTests(unittest.TestCase):
    def test_deterministic_atomization_preserves_all_customer_ids(self):
        customer_reqs = [
            {
                "external_id": "OEM-REQ-001",
                "section": "4.1 硬件安全",
                "category": "硬件安全",
                "text": "1. 控制器应具备物理防拆检测功能，开盖时产生告警；2. 调试接口在量产后应永久关闭；3. 外部Flash存储应进行硬件绑定加密。",
            },
            {
                "external_id": "OEM-REQ-045",
                "section": "5.2 外壳防护要求",
                "category": "结构安全",
                "text": "ADCU控制器应支持物理防拆检测功能，开盖时产生安全告警。",
            },
            {
                "external_id": "OEM-REQ-002",
                "section": "4.15 车内通信安全",
                "category": "通信安全",
                "text": "总线通信应采用SecOC进行消息认证码MAC校验与新鲜度值校验。",
            },
        ]

        atoms = atomize_and_dedup_customer_requirements(customer_reqs)

        self.assertTrue(len(atoms) >= 3)
        all_ids = {cid for atom in atoms for cid in atom["source_customer_ids"]}
        self.assertEqual(all_ids, {"OEM-REQ-001", "OEM-REQ-045", "OEM-REQ-002"})

        anti_tamper = next(
            atom for atom in atoms
            if "防拆" in atom["clause"] or "开盖" in atom["clause"]
        )
        self.assertIn("OEM-REQ-001", anti_tamper["source_customer_ids"])
        self.assertIn("OEM-REQ-045", anti_tamper["source_customer_ids"])


if __name__ == "__main__":
    unittest.main()
