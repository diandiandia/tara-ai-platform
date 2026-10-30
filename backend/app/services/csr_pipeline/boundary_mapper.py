"""CSR Pipeline V2 — Phase 1.3 安全边界映射

规则引擎优先（可配置映射表，前缀匹配 + 最长匹配优先）。
规则匹配不上的 → LLM 批量兜底。
"""
from __future__ import annotations
from .models import PipelineRecord, PipelineConfig
from .llm_proxy import LLMProxy


BOUNDARY_LLM_SYSTEM = """你是汽车网络安全分析专家。请分析每条安全需求的作用对象，将其归入以下安全边界类之一：

可用边界类：
- 调试入口：JTAG、SWD、调试接口、调试端口等物理调试访问点
- 调试服务：UDS、诊断服务、诊断会话等诊断协议服务
- 审计日志/调试通道：审计记录、安全日志、事件记录
- 固件写入：OTA、FOTA、刷写、固件更新、软件更新
- 感知数据流：摄像头、雷达、激光雷达、传感器、GNSS、IMU 等数据流
- 隐私数据：个人数据、位置信息、生物特征、PII
- 调试数据通道：USB、串口、UART、SPI、I2C 等数据通道
- 安全通信：TLS、SecOC、MAC、安全通信协议
- 密钥与证书：密钥管理、证书、PKI、加密算法
- 安全启动与存储：安全启动、安全存储、可信启动、信任根
- 车身网络：CAN、LIN、FlexRay、以太网、车载网络、网关
- 通用：无法明确归入以上任何类的，标记为通用

输出格式：
{
  "results": [
    {"record_id": "ASSET-1::CSR-001", "boundary_class": "调试入口"},
    {"record_id": "ASSET-1::CSR-002", "boundary_class": "通用"}
  ]
}"""


def map_boundary_class(
    records: list[PipelineRecord],
    proxy: LLMProxy,
    config: PipelineConfig,
) -> list[PipelineRecord]:
    """安全边界映射主函数。规则优先，LLM 兜底。"""
    patterns = config.boundary_patterns or {}
    unmatched: list[PipelineRecord] = []

    # Phase 1: 规则匹配（前缀匹配 + 最长匹配优先）
    for rec in records:
        text = rec.requirement_text
        # 从文本中提取主体信息
        subject = _extract_subject_heuristic(text) or rec.asset_name or ""

        cls = _match_boundary_rule(subject, text, patterns)
        if cls:
            rec.boundary_class = cls
            rec.audit_log.append({"action": "boundary_mapped", "method": "rule", "result": cls})
        else:
            unmatched.append(rec)

    if not unmatched:
        return records

    # Phase 2: LLM 兜底
    if config.fallback_to_llm_boundary and proxy.settings:
        _llm_classify_boundary(unmatched, proxy, config)

    # 未匹配的归入"通用"
    for rec in unmatched:
        if not rec.boundary_class:
            rec.boundary_class = "通用"
            rec.audit_log.append({"action": "boundary_mapped", "method": "fallback", "result": "通用"})

    return records


def _extract_subject_heuristic(text: str) -> str:
    """从文本中试探提取主体信息。不依赖【主体】格式。"""
    import re
    # 尝试匹配【主体】格式
    m = re.search(r"【主体】(.*?)【要求】", text)
    if m:
        return m.group(1).strip()
    # 尝试匹配 "Subject: xxx" 格式
    m = re.search(r"\[Subject\]\s*(.*?)\s*\[Requirement\]", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # 提取第一句的主语部分
    parts = re.split(r"[，,。.;；\n]", text)
    if parts:
        first = parts[0].strip()
        if len(first) < 100:
            return first
    return ""


def _match_boundary_rule(subject: str, text: str, patterns: dict[str, list[str]]) -> str | None:
    """规则匹配：前缀/核心词匹配 + 最长匹配优先。"""
    search_text = (subject + " " + text).lower()
    best_match: str | None = None
    best_len: int = 0

    for cls_name, keywords in patterns.items():
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in search_text:
                kw_len = len(kw_lower)
                if kw_len > best_len:
                    best_match = cls_name
                    best_len = kw_len

    return best_match


def _llm_classify_boundary(
    records: list[PipelineRecord],
    proxy: LLMProxy,
    config: PipelineConfig,
):
    """LLM 批量分类未匹配的边界。"""
    batch_size = config.classify_batch_size

    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        items = [{"record_id": rec.record_id, "text": rec.requirement_text} for rec in batch]
        ids = [rec.record_id for rec in batch]

        result = proxy.classify(
            stage="boundary-classify",
            system_prompt=BOUNDARY_LLM_SYSTEM,
            items=items,
            input_ids=ids,
        )

        results_list = result.get("results", []) or []
        for item in results_list:
            rid = item.get("record_id", "")
            cls = item.get("boundary_class", "通用")
            for rec in records:
                if rec.record_id == rid:
                    rec.boundary_class = cls
                    rec.audit_log.append({"action": "boundary_mapped", "method": "llm", "result": cls})
                    break