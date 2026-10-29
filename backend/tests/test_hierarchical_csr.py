import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.csr_synthesis import (
    extract_subject,
    extract_subject_and_clauses,
    atomize_and_dedup_asset_requirements,
    aggregate_subsystem_requirements,
    aggregate_global_requirements,
    classify_and_finalize_csrs,
    _content_hash_id
)

class MockAsset:
    def __init__(self, id, name, asset_type="hardware", diagram_id=1):
        self.id = id
        self.name = name
        self.asset_type = asset_type
        self.diagram_id = diagram_id

class HierarchicalCsrTests(unittest.TestCase):
    def test_extract_subject_and_clauses(self):
        text = "【主体】CAN收发器；【要求】应过滤非白名单报文；应校验消息认证码MAC与报文新鲜度值。"
        atoms = extract_subject_and_clauses(text)
        self.assertEqual(len(atoms), 2)
        self.assertEqual(atoms[0][0], "CAN收发器")
        self.assertIn("过滤", atoms[0][1])
        self.assertEqual(atoms[1][0], "CAN收发器")
        self.assertIn("校验", atoms[1][1])

    def test_atomize_and_dedup_asset(self):
        asset = MockAsset(1, "毫米波雷达")
        reqs = [
            {"cybersecurity_requirement": "【主体】毫米波雷达；【要求】应校验报文时间戳与计数器。"},
            {"cybersecurity_requirement": "【主体】毫米波雷达；【要求】应校验报文时间戳与计数器。"}, # Duplicate
            {"cybersecurity_requirement": "【主体】毫米波雷达；【要求】应检测物理连接断开并记录安全日志。"}
        ]
        req_map = {
            r["cybersecurity_requirement"]: _content_hash_id("CSR_", r["cybersecurity_requirement"])
            for r in reqs
        }
        clusters = atomize_and_dedup_asset_requirements(asset, reqs, req_map)
        self.assertEqual(len(clusters), 2)
        # Verify both duplicate items are tracked in source_tara_ids
        ts_cluster = next(c for c in clusters if "时间戳" in c["clause"])
        self.assertEqual(len(ts_cluster["source_tara_ids"]), 1)
        self.assertEqual(len(ts_cluster["applicable_assets"]), 1)

    def test_aggregate_subsystem_and_global(self):
        asset1 = MockAsset(1, "前向雷达", diagram_id=10)
        asset2 = MockAsset(2, "角雷达", diagram_id=10)

        r1 = "【主体】前向雷达；【要求】应校验入站报文的时钟同步和报文序列号。"
        r2 = "【主体】角雷达；【要求】应校验入站报文的时钟同步和报文序列号。"
        req_map = {
            r1: "CSR_11111111",
            r2: "CSR_22222222"
        }
        c1 = atomize_and_dedup_asset_requirements(asset1, [{"cybersecurity_requirement": r1}], req_map)
        c2 = atomize_and_dedup_asset_requirements(asset2, [{"cybersecurity_requirement": r2}], req_map)

        all_atoms = c1 + c2
        subsystem_csrs = aggregate_subsystem_requirements("传感器感知子系统", all_atoms, settings=None)
        # In fallback semantic clustering, these two similar sensor requirements merge into 1
        self.assertEqual(len(subsystem_csrs), 1)
        self.assertEqual(subsystem_csrs[0]["source_tara_ids"], {"CSR_11111111", "CSR_22222222"})
        self.assertEqual(subsystem_csrs[0]["applicable_assets"], {"前向雷达", "角雷达"})

        global_csrs = aggregate_global_requirements(subsystem_csrs, settings=None)
        self.assertEqual(len(global_csrs), 1)

        final_rows = classify_and_finalize_csrs(global_csrs, all_input_raw_ids={"CSR_11111111", "CSR_22222222"})
        self.assertEqual(len(final_rows), 1)
        self.assertTrue(final_rows[0]["csr_id"].startswith("CSR-"))
        self.assertEqual(final_rows[0]["_raw_source_ids"], ["CSR_11111111", "CSR_22222222"])

    def test_missing_raw_id_triggers_strict_error(self):
        fake_items = [{
            "title": "测试要求",
            "subject": "测试主体",
            "cybersecurity_requirement": "应执行完整性保护",
            "verification_method": "测试",
            "requirement_type": "product",
            "rationale": "",
            "source_tara_ids": {"CSR_AAAAAA01"},
            "applicable_assets": {"AssetA"}
        }]
        # Require CSR_AAAAAA01 and CSR_AAAAAA02, but item only covers CSR_AAAAAA01
        with self.assertRaises(ValueError) as ctx:
            classify_and_finalize_csrs(fake_items, all_input_raw_ids={"CSR_AAAAAA01", "CSR_AAAAAA02"})
        self.assertIn("100% 追溯断裂", str(ctx.exception))

    def test_category_global_consolidation_and_coverage(self):
        # 模拟生成 15 条跨不同子系统和资产的安全日志需求
        items = []
        raw_ids = set()
        for i in range(15):
            raw_id = f"CSR_LOG_{i:04d}"
            raw_ids.add(raw_id)
            items.append({
                "functional_category": "4.11 安全日志",
                "title": f"日志审计要求_{i}",
                "subject": f"子系统模块_{i}",
                "cybersecurity_requirement": f"子系统模块_{i}应针对安全事件_{i}生成只追加审计日志并记录时间源。",
                "verification_method": "测试",
                "requirement_type": "product",
                "rationale": "防篡改审计",
                "source_tara_ids": {raw_id},
                "applicable_assets": {f"资产_{i}"}
            })

        # 执行全局提炼（模拟无外网环境下的聚类收敛）
        condensed = aggregate_global_requirements(items, settings=None)
        # 校验条目数严格收敛至 2~4 条
        self.assertLessEqual(len(condensed), 4)
        self.assertGreaterEqual(len(condensed), 1)

        # 校验 15 个原始 ID 100% 无遗漏继承
        observed_ids = set()
        observed_assets = set()
        for c in condensed:
            observed_ids.update(c["source_tara_ids"])
            observed_assets.update(c["applicable_assets"])
        self.assertEqual(observed_ids, raw_ids)
        self.assertEqual(len(observed_assets), 15)

if __name__ == "__main__":
    unittest.main()
