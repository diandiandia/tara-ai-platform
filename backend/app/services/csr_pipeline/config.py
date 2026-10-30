"""CSR Pipeline V2 — 本项目的默认配置

核心管道代码不依赖任何项目特有字段。
所有项目特有的映射表、分类标签、编号规则都在这里定义。
换项目只需替换此文件，管道代码不变。
"""
from __future__ import annotations
from .models import PipelineConfig

# =============================================================================
# 动作词词典（用于结构化抽取, 规则引擎优先匹配，匹配不到走 LLM）
# =============================================================================
ACTION_VERB_DICT: list[str] = [
    "认证", "授权", "访问控制", "权限",
    "验证", "校验", "检测", "检查",
    "加密", "解密", "签名", "验签",
    "记录", "审计", "日志",
    "关闭", "禁用", "开启", "启用", "锁定",
    "保护", "防护", "隔离", "过滤",
    "拒绝", "丢弃", "阻止", "断开", "终止",
    "监控", "检测", "告警", "响应",
    "备份", "恢复", "回滚",
    "更新", "升级", "刷写",
    "存储", "销毁", "擦除",
    "标识", "注册", "注销",
    "同步", "校准",
    "authenticate", "authorize", "verify", "validate", "check",
    "encrypt", "decrypt", "sign",
    "close", "disable", "enable", "lock",
    "protect", "isolate", "filter",
    "reject", "discard", "block", "terminate", "disconnect",
    "monitor", "detect", "alert", "respond",
    "log", "audit", "record",
    "store", "destroy", "erase",
    "update", "upgrade", "flash",
    "backup", "recover",
]

# =============================================================================
# 安全边界类映射表（规则引擎前缀匹配 + 最长匹配优先）
# =============================================================================
BOUNDARY_PATTERNS: dict[str, list[str]] = {
    "调试入口": [
        "JTAG", "SWD", "debug port", "调试接口", "调试端口",
        "调试口", "debug interface", "debug port", "BDM",
    ],
    "调试服务": [
        "UDS", "诊断服务", "diagnostic service", "诊断会话",
        "diagnostic session", "诊断访问", "XCP", "CCP",
    ],
    "审计日志/调试通道": [
        "审计", "日志", "audit", "log", "logging",
        "安全事件记录", "事件记录", "event log",
    ],
    "固件写入": [
        "OTA", "FOTA", "SOTA", "刷写", "固件更新", "固件升级",
        "firmware", "flash", "programming", "写入", "更新包",
        "software update", "远程更新",
    ],
    "感知数据流": [
        "摄像头", "雷达", "激光雷达", "毫米波", "超声波", "传感器", "IMU",
        "GNSS", "GPS", "点云", "图像帧", "data stream", "sensor",
        "camera", "radar", "lidar", "ultrasonic", "perception",
    ],
    "隐私数据": [
        "个人", "位置", "隐私", "PII", "privacy", "location",
        "personal data", "biometric", "生物特征", "驾驶员监控",
        "DMS", "OMS", "座舱监控", "人脸",
    ],
    "调试数据通道": [
        "USB", "串口", "serial", "UART", "SPI", "I2C",
        "数据通道", "调试通道", "trace", "ETM", "ITM",
    ],
    "安全通信": [
        "TLS", "SecOC", "MAC", "安全通信", "secure communication",
        "E2E", "端到端", "传输层安全", "通信保护", "报文认证",
    ],
    "密钥与证书": [
        "密钥", "证书", "PKI", "HSM", "key", "certificate",
        "密码", "加密算法", "crypto", "签名", "验签",
        "密钥管理", "密钥存储",
    ],
    "安全启动与存储": [
        "安全启动", "secure boot", "安全存储", "secure storage",
        "可信启动", "信任根", "RoT", "trusted boot",
        "安全区域", "隔离区", "TEE", "REE",
    ],
    "车身网络": [
        "CAN", "LIN", "FlexRay", "以太网", "Ethernet",
        "车内网络", "in-vehicle", "车载网络", "网关", "gateway",
        "域控制", "车身控制", "BCM",
    ],
}

# =============================================================================
# 同义词表（程序校验例外条件保留时使用）
# =============================================================================
SYNONYM_TABLE: dict[str, list[str]] = {
    "预定义策略": ["按策略", "根据策略", "按预定义策略", "预定义"],
    "受控": ["受控", "受控维护", "受控启用", "受控访问"],
    "授权": ["授权", "经授权", "已授权", "有权限"],
    "维护": ["维护", "维护模式", "维护状态", "售后维护"],
    "临时": ["临时", "暂时", "一次性", "限时"],
    "量产": ["量产", "生产", "出厂", "交付"],
    "开发": ["开发", "开发阶段", "研发", "调试"],
}

# =============================================================================
# 互补对识别模式（动作词 vs 失败处理词配对）
# =============================================================================
COMPLEMENT_PATTERNS: list[tuple[list[str], list[str]]] = [
    (["认证", "authenticate"], ["拒绝", "reject", "阻止", "block", "丢弃", "discard"]),
    (["授权", "authorize"], ["拒绝", "reject", "阻止", "block"]),
    (["验证", "校验", "verify", "validate", "check"], ["拒绝", "reject", "丢弃", "discard", "阻止", "block"]),
    (["检测", "detect"], ["拒绝", "reject", "阻止", "block", "告警", "alert", "记录", "log"]),
    (["签名", "sign"], ["拒绝", "reject", "丢弃", "discard"]),
]

# =============================================================================
# 主题标签集（直接使用项目 17 类功能分类，前置到判定层做分组维度）
# 分类前置的好处：跨类条目永不相遇，LLM 判定只在类内做，误判风险大幅降低
# =============================================================================
TOPIC_LABELS: list[str] = [
    "4.1 硬件安全",
    "4.2 系统加固",
    "4.3 访问控制",
    "4.4 安全调试",
    "4.5 本地刷写安全",
    "4.6 OTA 刷写安全",
    "4.7 安全启动",
    "4.8 安全存储",
    "4.9 数据脱敏与隐私安全",
    "4.10 安全诊断",
    "4.11 安全日志",
    "4.12 密码、密钥与证书安全",
    "4.13 传感器安全",
    "4.14 防火墙安全",
    "4.15 车内通信安全",
    "4.16 远程通讯安全",
    "4.17 安全开发与供应链安全",
]

# =============================================================================
# 编号前缀映射（可插拔，本项目使用 17 类分类的 FUNCTIONAL_PREFIX_MAP）
# =============================================================================
def _build_numbering_prefix_map() -> dict:
    try:
        from app.core.taxonomy import FUNCTIONAL_CATEGORY_NAMES, FUNCTIONAL_PREFIX_MAP
        return dict(FUNCTIONAL_PREFIX_MAP)
    except ImportError:
        return {}


# =============================================================================
# 默认配置
# =============================================================================

def get_default_config() -> PipelineConfig:
    return PipelineConfig(
        fuzzy_dedup_threshold=0.95,
        boundary_patterns=BOUNDARY_PATTERNS,
        fallback_to_llm_boundary=True,
        topic_labels=TOPIC_LABELS,
        chunk_size=25,
        chunk_overlap=12,
        global_group_batch_size=100,
        classify_batch_size=35,
        action_verb_dict=ACTION_VERB_DICT,
        complement_patterns=COMPLEMENT_PATTERNS,
        synonym_table=SYNONYM_TABLE,
        classify_fn=None,       # 由调用方注入 classify_and_finalize_csrs
        numbering_prefix_map=_build_numbering_prefix_map(),
        csr_number_prefix_map=_build_numbering_prefix_map(),
        max_uncertainty_rate=0.15,
        max_retries=3,
        temperature=0.0,
    )


default_config = get_default_config


def rebuild_config(**overrides) -> PipelineConfig:
    cfg = get_default_config()
    for k, v in overrides.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    return cfg