"""CSR Pipeline V2 — Phase 1.4 主题粗分

LLM 批量分类，为每条需求打上主题标签。
自检不一致的条目标记 topic_uncertain，不强制按主题分块。
"""
from __future__ import annotations
from .models import PipelineRecord, PipelineConfig
from .llm_proxy import LLMProxy


def _topic_system_prompt(labels: list[str]) -> str:
    label_list = "\n".join(f"  - {l}" for l in labels)
    return f"""你是汽车网络安全需求分析专家（ISO 21434）。请为每条安全需求分配一个最匹配的功能分类。

可用分类（必须从以下列表中选择，不能自创；每条只选一个）：
{label_list}

分类判定指南：
- 4.1 硬件安全：JTAG/SWD 物理调试口、PCB 防护、侧信道、硬件防拆、芯片物理安全
- 4.2 系统加固：OS 内核加固、系统服务最小化、进程隔离、安全配置基线、系统参数保护
- 4.3 访问控制：身份认证、权限管理、最小权限、RBAC、会话管理、登录控制
- 4.4 安全调试：调试接口认证/授权/关闭、调试会话控制、调试日志保护（作用是调试口的控制措施）
- 4.5 本地刷写安全：本地固件刷写验证、刷写授权、刷写完整性检查
- 4.6 OTA 刷写安全：OTA/FOTA/SOTA 更新包验证、升级过程安全、回滚保护
- 4.7 安全启动：安全启动链、Boot ROM 校验、信任根、启动顺序验证
- 4.8 安全存储：密钥/敏感数据安全存储、NVM 保护、安全区域隔离
- 4.9 数据脱敏与隐私安全：PII 脱敏、位置数据保护、生物特征保护、驾驶员/乘客隐私
- 4.10 安全诊断：UDS 诊断服务安全、诊断会话控制、诊断访问授权
- 4.11 安全日志：安全事件记录、审计日志完整性、日志不可否认性、入侵检测日志
- 4.12 密码、密钥与证书安全：密钥管理/生成/存储/销毁、证书验证、PKI、HSM、密码算法使用
- 4.13 传感器安全：摄像头/雷达/激光雷达/IMU/GNSS 传感器数据完整性/认证/防欺骗
- 4.14 防火墙安全：网络安全域隔离、防火墙规则、流量过滤、ACL
- 4.15 车内通信安全：CAN/LIN/FlexRay/Ethernet 消息认证、SecOC、车内 E2E 保护
- 4.16 远程通讯安全：TLS/DTLS/V2X/C-V2X 安全、外网通信加密、远程接入安全
- 4.17 安全开发与供应链安全：安全开发流程、供应链安全、SBOM、安全运维

关键区分：
- 调试接口的认证 → 4.4 安全调试（不是 4.3 访问控制）
- 调试接口的关闭/禁用 → 4.4 安全调试（不是 4.1 硬件安全）
- 固件刷写签名验证 → 4.5/4.6（刷写安全，不是 4.12 密码安全）
- UDS 诊断安全 → 4.10 安全诊断（不是 4.4 安全调试）

输出格式：
{{"results": [{{"record_id": "ASSET-1::CSR-001", "topic_label": "4.4 安全调试"}}]}}"""


TOPIC_SELF_CHECK_SYSTEM = """你是汽车网络安全需求分析审核员。请检查以下功能分类结果是否正确：

请重点检查以下易混淆场景：
1. 调试接口相关的控制措施 —— 应归 4.4 安全调试，不是 4.3 访问控制或 4.1 硬件安全
2. 固件刷写安全 —— 应归 4.5 或 4.6，不是 4.12 密码安全
3. UDS 诊断 —— 应归 4.10 安全诊断，不是 4.4 安全调试
4. 传感器数据安全 —— 应归 4.13 传感器安全，不是 4.15 车内通信安全

输入包含原始需求文本和正向分类结果。
如有错误，请输出修正后的分类标签。

输出格式：
{
  "has_errors": true/false,
  "all_correct": true/false,
  "corrections": [
    {"record_id": "ASSET-1::CSR-001", "original_label": "4.3 访问控制", "corrected_label": "4.4 安全调试", "reason": "该需求是调试接口的认证控制，应归 4.4"}
  ]
}"""


def classify_topics(
    records: list[PipelineRecord],
    proxy: LLMProxy,
    config: PipelineConfig,
) -> list[PipelineRecord]:
    """主题粗分主函数。"""
    labels = config.topic_labels or []
    if not labels:
        # 无标签配置，跳过
        for rec in records:
            rec.topic_label = "未分类"
        return records

    batch_size = config.classify_batch_size

    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        _classify_topic_batch(batch, proxy, labels)

    return records


def _classify_topic_batch(
    batch: list[PipelineRecord],
    proxy: LLMProxy,
    labels: list[str],
):
    """批量主题分类。"""
    items = [
        {"record_id": rec.record_id, "text": rec.requirement_text}
        for rec in batch
    ]
    ids = [rec.record_id for rec in batch]

    sc = proxy.judge_with_self_check(
        stage="topic-classify",
        system_prompt=_topic_system_prompt(labels),
        check_prompt=TOPIC_SELF_CHECK_SYSTEM,
        user_payload={"requirements": items, "available_labels": labels},
        input_ids=ids,
        input_texts_abstract=[rec.requirement_text[:200] for rec in batch],
    )

    # 首选正向结果
    decisions = sc.first_result if sc.agreed else sc.second_result
    if not decisions:
        decisions = {}

    results_list = decisions.get("results", []) or []

    # 构建映射
    label_map: dict[str, str] = {}
    for item in results_list:
        rid = item.get("record_id", "")
        lbl = item.get("topic_label", "")
        if rid and lbl and lbl in labels:
            label_map[rid] = lbl

    # 如果有自检修正，应用修正
    if not sc.agreed and sc.second_result:
        corrections = sc.second_result.get("corrections", []) or []
        for corr in corrections:
            rid = corr.get("record_id", "")
            corrected = corr.get("corrected_label", "")
            if rid and corrected and corrected in labels:
                label_map[rid] = corrected

    # 赋值
    for rec in batch:
        lbl = label_map.get(rec.record_id, "")
        if lbl and lbl in labels:
            rec.topic_label = lbl
        else:
            rec.topic_label = "未分类"

        if not sc.agreed:
            rec.topic_uncertain = True
            rec.audit_log.append({
                "action": "topic_classify_uncertain",
                "discrepancies": sc.discrepancies,
            })
        else:
            rec.audit_log.append({
                "action": "topic_classified",
                "label": rec.topic_label,
                "confidence": sc.confidence,
            })