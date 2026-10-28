import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.req_merge import (
    atomize_and_dedup_customer_requirements,
    prepare_tara_atomic_requirements,
    create_merge_batches,
    enforce_merge_coverage_bidirectional,
    classify_and_finalize_merged_csrs,
    merge_requirements_with_llm,
    persist_merged_requirements,
)
from app.core.database import Base, engine, SessionLocal
from app.models.project import Project
from app.models.domain import Domain
from app.models.req_merge import CustomerRequirement, MergedRequirement, MergedRequirementSource


class ReqMergePipelineTests(unittest.TestCase):
    def test_customer_atomization_and_internal_dedup(self):
        """测试客户需求原子化拆解与客户内部去重（Step 1 & 2）。"""
        customer_reqs = [
            {
                "external_id": "OEM-REQ-001",
                "section": "4.1 硬件安全",
                "category": "硬件安全",
                "text": "1. 控制器应具备物理防拆检测功能，开盖时产生告警；2. 调试接口在量产后应永久关闭；3. 外部Flash存储应进行硬件绑定加密。",
            },
            {
                # 与 OEM-REQ-001 第 1 条要求高度相似（客户自身跨章节冗余）
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

        # 1. 验证拆解出原子项
        self.assertTrue(len(atoms) >= 3)

        # 2. 验证客户内部去重：OEM-REQ-001 与 OEM-REQ-045 的防拆要求合并，并保留两个编号
        anti_tamper_atom = next(
            (a for a in atoms if "防拆" in a["clause"] or "开盖" in a["clause"]),
            None
        )
        self.assertIsNotNone(anti_tamper_atom)
        self.assertIn("OEM-REQ-001", anti_tamper_atom["source_customer_ids"])
        self.assertIn("OEM-REQ-045", anti_tamper_atom["source_customer_ids"])

        # 3. 验证所有输入的原始客户 ID 都在原子集合的并集中
        all_atoms_cids = {cid for a in atoms for cid in a["source_customer_ids"]}
        self.assertEqual(all_atoms_cids, {"OEM-REQ-001", "OEM-REQ-045", "OEM-REQ-002"})

    def test_tara_csrs_zero_disassembly(self):
        """测试我方 TARA CSR 零拆解保持（Step 3）。"""
        tara_csrs = [
            {
                "csr_id": "CSR-HW-001",
                "security_domain": "4.1 硬件安全",
                "title": "总线物理防窃听与防非授权接入",
                "requirement": "【主体】CAN收发器；【要求】应采用物理密封连接器与开盖检测微动开关，在遭受物理非授权拆卸时触发安全事件上报。",
                "source_raw_ids": "CSR_4F36BD3F, CSR_12345678",
            },
            {
                "csr_id": "CSR-COM-001",
                "security_domain": "4.15 车内通信安全",
                "title": "CAN入站报文认证与防重放",
                "requirement": "【主体】通信控制器；【要求】应基于SecOC协议对入站CAN报文进行MAC认证与新鲜度值校验。",
                "source_raw_ids": "CSR_5A29D104",
            },
        ]

        tara_atoms = prepare_tara_atomic_requirements(tara_csrs)

        # 零拆解：数量严格等于输入数量
        self.assertEqual(len(tara_atoms), 2)
        self.assertEqual(tara_atoms[0]["csr_id"], "CSR-HW-001")
        self.assertEqual(tara_atoms[0]["source_raw_ids"], "CSR_4F36BD3F, CSR_12345678")
        self.assertEqual(tara_atoms[1]["csr_id"], "CSR-COM-001")

    def test_bidirectional_coverage_contract(self):
        """测试代码级双向 100% 覆盖契约（Step 4 & 覆盖率断言）。"""
        customer_reqs = [
            {"external_id": "C-1", "text": "需求 1"},
            {"external_id": "C-2", "text": "需求 2 (未被 LLM 匹配)"},
        ]
        tara_csrs = [
            {"csr_id": "T-1", "requirement": "TARA 需求 1"},
            {"csr_id": "T-2", "requirement": "TARA 需求 2 (未被 LLM 匹配)"},
        ]

        # 模拟 LLM 只返回了 C-1 和 T-1 的合并项，还带了一个幻觉 ID "FAKE-99"
        raw_merged = [
            {
                "requirement_title": "合并需求 1",
                "requirement_text": "【主体】ECU；【要求】完成某项控制",
                "source_customer_ids": ["C-1", "FAKE-99"],
                "source_tara_ids": ["T-1"],
                "resolution": "merge",
            }
        ]

        consolidated, report = enforce_merge_coverage_bidirectional(customer_reqs, tara_csrs, raw_merged)

        # 1. 验证幻觉 ID 被剥除
        item1 = next(m for m in consolidated if "C-1" in m["source_customer_ids"])
        self.assertNotIn("FAKE-99", item1["source_customer_ids"])

        # 2. 验证缺失的 C-2 被自动兜底为 customer_only
        item_c2 = next((m for m in consolidated if "C-2" in m["source_customer_ids"]), None)
        self.assertIsNotNone(item_c2)
        self.assertEqual(item_c2["resolution"], "customer_only")
        self.assertEqual(item_c2["source_tara_ids"], [])

        # 3. 验证缺失的 T-2 被自动兜底为 tara_only
        item_t2 = next((m for m in consolidated if "T-2" in m["source_tara_ids"]), None)
        self.assertIsNotNone(item_t2)
        self.assertEqual(item_t2["resolution"], "tara_only")
        self.assertEqual(item_t2["source_customer_ids"], [])

        # 4. 验证覆盖率指标：客户 100% 覆盖，TARA 100% 覆盖
        self.assertTrue(report["all_covered"])
        self.assertEqual(report["customer_covered_count"], 2)
        self.assertEqual(report["tara_covered_count"], 2)

    def test_classify_and_finalize_fcsr_numbering(self):
        """测试 17 分类标准排序与 FCSR-{PREFIX}-{NNN} 规范编号分配（Step 5）。"""
        merged_items = [
            {
                "functional_category": "4.15 车内通信安全",
                "requirement_title": "CAN通信校验",
                "requirement_text": "【主体】通信控制器；【要求】应校验CAN报文MAC。",
            },
            {
                "functional_category": "4.1 硬件安全",
                "requirement_title": "物理防拆",
                "requirement_text": "【主体】外壳；【要求】应具备开盖微动开关检测防拆。",
            },
            {
                "functional_category": "4.1 硬件安全",
                "requirement_title": "调试接口关闭",
                "requirement_text": "【主体】JTAG接口；【要求】量产后应通过电子熔丝不可逆关闭调试端口。",
            },
        ]

        final_csrs = classify_and_finalize_merged_csrs(merged_items)

        # 验证按 17 类顺序排序并忠实纠偏分类（4.1 硬件安全 -> 4.4 安全调试 -> 4.15 车内通信安全）
        self.assertEqual(final_csrs[0]["functional_category"], "4.1 硬件安全")
        self.assertEqual(final_csrs[0]["final_id"], "FCSR-HW-001")
        self.assertEqual(final_csrs[1]["functional_category"], "4.4 安全调试")
        self.assertEqual(final_csrs[1]["final_id"], "FCSR-DBG-001")
        self.assertEqual(final_csrs[2]["functional_category"], "4.15 车内通信安全")
        self.assertEqual(final_csrs[2]["final_id"], "FCSR-COM-001")

    def test_full_pipeline_with_sqlite_persistence(self):
        """全流程集成测试：客户需求拆解去重 -> 与 TARA 需求合并 -> 17 分类 -> SQLite 数据库落库与穿透追溯。"""
        db = SessionLocal()
        try:
            # 准备临时测试项目与域控
            proj = Project(name="Test ReqMerge Proj")
            db.add(proj)
            db.flush()
            dom = Domain(name="Test Domain", project_id=proj.id)
            db.add(dom)
            db.commit()

            customer_reqs = [
                {
                    "external_id": "OEM-SEC-01",
                    "section": "硬件接口安全",
                    "category": "硬件安全",
                    "text": "控制器应具备开盖防拆检测；外部调试接口必须在量产后永久禁用。",
                },
                {
                    "external_id": "OEM-SEC-02",
                    "section": "车载网络安全",
                    "category": "通信安全",
                    "text": "入站CAN报文应通过SecOC校验新鲜度值与消息认证码。",
                },
            ]

            tara_csrs = [
                {
                    "csr_id": "CSR-HW-001",
                    "security_domain": "4.1 硬件安全",
                    "title": "调试接口硬件锁死",
                    "requirement": "【主体】调试接口；【要求】在量产阶段应熔断物理熔丝永久禁用JTAG/SWD等硬件调试接口。",
                    "source_raw_ids": "CSR_TAMPER_001, CSR_DEBUG_002",
                },
                {
                    "csr_id": "CSR-COM-001",
                    "security_domain": "4.15 车内通信安全",
                    "title": "CAN报文SecOC认证",
                    "requirement": "【主体】通信控制器；【要求】应通过SecOC模块对入站CAN报文进行MAC认证与新鲜度值防重放校验。",
                    "source_raw_ids": "CSR_CAN_003",
                },
                {
                    "csr_id": "CSR-LOG-001",
                    "security_domain": "4.11 安全日志",
                    "title": "安全事件本地审计存储",
                    "requirement": "【主体】日志模块；【要求】应将完整性校验失败等安全事件记录至防篡改审计存储。",
                    "source_raw_ids": "CSR_LOG_004",
                },
            ]

            # 运行合并流水线（本地测试使用规则降级机制，秒级完成且确定性校验）
            merged, report = merge_requirements_with_llm(db, proj.id, customer_reqs, tara_csrs)

            self.assertTrue(report["all_covered"])
            self.assertEqual(report["customer_covered_count"], 2)
            self.assertEqual(report["tara_covered_count"], 3)

            # 数据库持久化落库
            tara_raw_map = {tc["csr_id"]: tc["source_raw_ids"] for tc in tara_csrs}
            persisted = persist_merged_requirements(
                db, proj.id, dom.id, merged, tara_raw_map=tara_raw_map
            )

            # 校验落库记录
            db_records = db.query(MergedRequirement).filter(MergedRequirement.domain_id == dom.id).all()
            self.assertEqual(len(db_records), len(persisted))

            # 校验双向来源关联
            sources = db.query(MergedRequirementSource).all()
            customer_sources = [s.source_id for s in sources if s.source_type == "customer"]
            tara_sources = [s.source_id for s in sources if s.source_type == "tara"]

            # 校验 100% 原始编号存在
            self.assertIn("OEM-SEC-01", customer_sources)
            self.assertIn("OEM-SEC-02", customer_sources)
            self.assertIn("CSR-HW-001", tara_sources)
            self.assertIn("CSR-COM-001", tara_sources)
            self.assertIn("CSR-LOG-001", tara_sources)

            # 校验底层 TARA Raw ID 穿透保存
            com_record = next(r for r in db_records if "FCSR-COM" in r.final_id)
            self.assertIn("CSR_CAN_003", com_record.source_tara_raw_ids)

        finally:
            # 清理测试数据
            db.query(MergedRequirementSource).filter(MergedRequirementSource.merged_id.in_(
                db.query(MergedRequirement.id).filter(MergedRequirement.domain_id == dom.id)
            )).delete(synchronize_session=False)
            db.query(MergedRequirement).filter(MergedRequirement.domain_id == dom.id).delete(synchronize_session=False)
            db.query(Domain).filter(Domain.id == dom.id).delete(synchronize_session=False)
            db.query(Project).filter(Project.id == proj.id).delete(synchronize_session=False)
            db.commit()
            db.close()


if __name__ == "__main__":
    unittest.main()
