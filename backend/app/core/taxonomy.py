"""Shared ISO/SAE 21434 security-domain taxonomy.

Both the TARA stage prompts (worker/tasks.py) and the item-level CSR synthesis
service (services/csr_synthesis.py) must use this single list. Keeping two
different lists caused the model to emit domain labels that did not match the
downstream grouping keys.
"""

DOMAIN_PREFIX_MAP = {
    "硬件安全 / Hardware Security": "CSR-HW",
    "系统安全 / System Security": "CSR-SYS",
    "密码安全与密钥管理 / Cryptography & Key Management": "CSR-CRY",
    "应用安全 / Application Security": "CSR-APP",
    "数据安全与隐私保护 / Data Security & Privacy": "CSR-DAT",
    "安全监控、测试与响应 / Security Monitoring, Testing & Response": "CSR-MON",
    "通信安全 / Communication Security": "CSR-COM",
    "安全开发、供应链与运维 / Secure Development, Supply Chain & Operations": "CSR-OPS",
    "通用安全与其它 / General & Other Security": "CSR-GEN",
}

MAJOR_ORDER = {
    "硬件安全 / Hardware Security": 1,
    "系统安全 / System Security": 2,
    "密码安全与密钥管理 / Cryptography & Key Management": 3,
    "应用安全 / Application Security": 4,
    "数据安全与隐私保护 / Data Security & Privacy": 5,
    "安全监控、测试与响应 / Security Monitoring, Testing & Response": 6,
    "通信安全 / Communication Security": 7,
    "安全开发、供应链与运维 / Secure Development, Supply Chain & Operations": 8,
    "通用安全与其它 / General & Other Security": 9,
}

CANONICAL_DOMAINS = list(MAJOR_ORDER.keys())

# Ready-to-inject bullet list for prompts. Keep the exact canonical strings.
DOMAIN_PROMPT_LIST = "\n".join(f"     * {d}" for d in CANONICAL_DOMAINS)


def clean_major_category(domain_str: str) -> str:
    """Map a free-form model label to one canonical domain.

    The LLM occasionally paraphrases a domain (for example
    "Security Monitoring & Defense"). This function is intentionally
    substring-based, but all prompt-side enumerations should use
    CANONICAL_DOMAINS so this is only a repair path.
    """
    s = str(domain_str or "").lower()
    if "hardware" in s or "硬件" in s:
        return "硬件安全 / Hardware Security"
    if "system" in s or "系统" in s:
        return "系统安全 / System Security"
    if "crypt" in s or "key" in s or "密码" in s or "密钥" in s:
        return "密码安全与密钥管理 / Cryptography & Key Management"
    if "app" in s or "应用" in s:
        return "应用安全 / Application Security"
    if "data" in s or "privacy" in s or "数据" in s or "隐私" in s:
        return "数据安全与隐私保护 / Data Security & Privacy"
    if "monitor" in s or "防护" in s or "监控" in s or "test" in s or "测试" in s or "respond" in s or "响应" in s:
        return "安全监控、测试与响应 / Security Monitoring, Testing & Response"
    if "comm" in s or "通信" in s or "通讯" in s or "transmission" in s:
        return "通信安全 / Communication Security"
    if "dev" in s or "ops" in s or "开发" in s or "运维" in s or "生产" in s or "supply" in s or "chain" in s or "供应链" in s:
        return "安全开发、供应链与运维 / Secure Development, Supply Chain & Operations"
    return "通用安全与其它 / General & Other Security"


# 逻辑访问控制类关键词（认证/授权/最小权限/会话）——应归「系统安全」，而不是「硬件安全」
_ACCESS_CONTROL_KEYWORDS = [
    "身份认证", "认证", "授权", "访问控制", "最小权限", "会话", "权限",
    "authentication", "authorization", "access control", "least privilege", "session",
]

# 物理/硬件类关键词——这些才应该留在「硬件安全」
_PHYSICAL_KEYWORDS = [
    "物理防拆", "外壳", "封装", "侧信道", "故障注入", "探针", "pcb",
    "tamper", "side-channel", "side channel", "fault injection", "bga", "efuse", "otp",
]


def correct_security_domain(domain: str, text: str) -> str:
    """按「控制机制」而非「资产类型」纠正安全域。

    典型场景：调试接口的身份认证/会话准入/最小权限，虽然作用在硬件调试口上，
    但控制手段是逻辑访问控制，应归「系统安全」，而不是「硬件安全」。
    只在「当前被判为硬件安全、且正文是逻辑访问控制、且不涉及物理防护」时才纠正。
    """
    if domain != "硬件安全 / Hardware Security":
        return domain
    t = str(text or "").lower()
    has_access = any(k in t for k in _ACCESS_CONTROL_KEYWORDS)
    has_physical = any(k in t for k in _PHYSICAL_KEYWORDS)
    if has_access and not has_physical:
        return "系统安全 / System Security"
    return domain


# ---------------------------------------------------------------------------
# 功能分类（17 类，对齐客户规范，用于「合并去重 → 原子化 → 分类」的最终分类）
# ---------------------------------------------------------------------------

FUNCTIONAL_CATEGORIES = [
    ("4.1", "硬件安全", "Hardware Security"),
    ("4.2", "系统加固", "System Hardening"),
    ("4.3", "访问控制", "Access Control"),
    ("4.4", "安全调试", "Secure Debug"),
    ("4.5", "本地刷写安全", "Local Flashing Security"),
    ("4.6", "OTA 刷写安全", "OTA Flashing Security"),
    ("4.7", "安全启动", "Secure Boot"),
    ("4.8", "安全存储", "Secure Storage"),
    ("4.9", "数据脱敏与隐私安全", "Data Masking & Privacy Security"),
    ("4.10", "安全诊断", "Secure Diagnostics"),
    ("4.11", "安全日志", "Security Logging"),
    ("4.12", "密码、密钥与证书安全", "Cryptography, Key & Certificate Security"),
    ("4.13", "传感器安全", "Sensor Security"),
    ("4.14", "防火墙安全", "Firewall Security"),
    ("4.15", "车内通信安全", "In-Vehicle Communication Security"),
    ("4.16", "远程通讯安全", "Remote Communication Security"),
    ("4.17", "安全开发与供应链安全", "Secure Development & Supply Chain Security"),
]

FUNCTIONAL_CATEGORY_NAMES = [f"{code} {name}" for code, name, _en in FUNCTIONAL_CATEGORIES]

_FUNCTIONAL_PREFIXES = [
    "CSR-HW", "CSR-HARDEN", "CSR-AC", "CSR-DBG", "CSR-FLASH", "CSR-OTA",
    "CSR-BOOT", "CSR-STOR", "CSR-DATA", "CSR-DIAG", "CSR-LOG", "CSR-CRY",
    "CSR-SENSOR", "CSR-FW", "CSR-COM", "CSR-REMOTE", "CSR-OPS",
]

FUNCTIONAL_PREFIX_MAP = {
    full: prefix
    for full, prefix in zip(FUNCTIONAL_CATEGORY_NAMES, _FUNCTIONAL_PREFIXES)
}

# Ready-to-inject bullet list for prompts.
FUNCTIONAL_PROMPT_LIST = "\n".join(
    f"     * {code} {name}（{en}）" for code, name, en in FUNCTIONAL_CATEGORIES
)

# 关键字锚定表：命中这些词时优先归入对应分类（中英双语，源自 GB 44495 / R155 / 21434 与行业实践）
FUNCTIONAL_KEYWORDS = {
    "4.1 硬件安全": [
        "pcb", "pcba", "丝印", "走线", "布线", "封装", "bga", "lga", "探针", "测试点",
        "防拆", "外壳", "开盖", "物理篡改", "篡改", "侧信道", "故障注入", "hsm", "ehsm",
        "efuse", "otp", "smpu", "信任根", "root of trust", "熔丝", "fuse", "不可逆禁用",
        "存储绑定", "安全芯片", "tpm", "物理",
    ],
    "4.2 系统加固": [
        "系统配置", "内核加固", "裁剪", "最小化", "禁用服务", "禁用端口", "kallsyms", "dmesg",
        "dm-verity", "selinux", "提权", "资源耗尽", "拒绝服务", "降级", "白名单",
        "只读文件系统", "安全基线", "系统加固",
    ],
    "4.3 访问控制": [
        "认证", "授权", "鉴权", "最小权限", "权限", "角色", "会话", "rbac", "acl",
        "身份鉴别", "越权", "默认凭据", "共享凭据", "访问控制",
    ],
    "4.4 安全调试": [
        "调试", "debug", "jtag", "swd", "uart", "调试会话", "调试命令", "调试认证",
        "调试授权", "调试审计", "调试密码", "debug authentication", "debug lock",
        "生命周期状态", "lcs", "挑战应答", "调试证书", "调试使能",
    ],
    "4.5 本地刷写安全": [
        "刷写", "刷新", "烧录", "编程", "产线刷写", "诊断刷写", "uds", "0x34", "0x36",
        "0x37", "can 刷写", "以太网刷写", "doip", "fbl", "刷写包签名", "刷写包完整性",
        "刷写授权", "刷写失败恢复", "flashing",
    ],
    "4.6 OTA 刷写安全": [
        "ota", "fota", "sota", "远程升级", "云端下发", "升级包", "升级服务器", "升级授权",
        "升级回滚", "升级失败恢复", "防降级", "升级通道",
    ],
    "4.7 安全启动": [
        "安全启动", "secure boot", "可信启动", "度量启动", "启动链", "bootrom", "bootloader",
        "引导程序", "固件完整性", "签名验证", "验签", "根信任", "防回滚", "安全版本号", "恢复模式",
    ],
    "4.8 安全存储": [
        "安全存储", "密钥存储", "敏感数据存储", "加密存储", "tee", "安全分区", "存储加密",
        "掉电保护", "防读取", "存储完整性", "安全文件系统", "存储",
    ],
    "4.9 数据脱敏与隐私安全": [
        "脱敏", "去标识", "匿名", "假名", "数据分类", "最小化", "保留期限", "跨境", "隐私",
        "个人信息", "用户告知", "同意", "数据泄露", "privacy",
    ],
    "4.10 安全诊断": [
        "诊断", "27服务", "27 服务", "security access", "种子", "诊断会话", "诊断权限",
        "诊断写操作", "标定", "诊断超时", "尝试次数", "diagnostic",
    ],
    "4.11 安全日志": [
        "安全日志", "审计", "日志记录", "日志完整性", "防篡改", "日志存储", "日志导出",
        "告警", "安全事件", "时间戳", "防重放", "日志保留", "audit", "log",
    ],
    "4.12 密码、密钥与证书安全": [
        "密码算法", "加密", "解密", "签名", "验签", "哈希", "国密", "sm2", "sm3", "sm4",
        "aes", "rsa", "ecc", "密钥", "证书", "根证书", "证书链", "证书吊销", "随机数",
        "crypto", "encrypt",
    ],
    "4.13 传感器安全": [
        "雷达", "摄像头", "激光雷达", "点云", "图像", "视频", "感知", "传感器",
        "信号可用性", "信号完整性", "传感器欺骗", "信号注入", "遮挡", "标定", "radar", "camera",
    ],
    "4.14 防火墙安全": [
        "防火墙", "网络隔离", "域隔离", "idps", "入侵检测", "入侵防御", "异常流量",
        "报文过滤", "端口过滤", "安全域", "vlan", "网关", "firewall", "intrusion",
    ],
    "4.15 车内通信安全": [
        "can", "can fd", "lin", "flexray", "以太网", "车载总线", "报文", "帧", "新鲜度",
        "防重放", "报文认证", "secoc", "e2e", "总线访问控制", "ethernet",
    ],
    "4.16 远程通讯安全": [
        "远程", "车联网", "t-box", "蜂窝", "4g", "5g", "wlan", "蓝牙", "v2x", "云端",
        "后台", "tls", "安全通道", "远程控制", "远程诊断", "远程升级", "remote", "telematics",
    ],
    "4.17 安全开发与供应链安全": [
        "代码扫描", "sast", "dast", "sca", "漏洞扫描", "漏洞修复", "安全编码", "sbom",
        "供应链", "第三方组件", "渗透测试", "安全测试", "威胁分析", "sdl", "供应商安全",
    ],
}

# 供 prompt 注入的关键字锚定段
FUNCTIONAL_KEYWORDS_PROMPT = "\n".join(
    f"     * {cat}：{' / '.join(kws)}" for cat, kws in FUNCTIONAL_KEYWORDS.items()
)

# 易混类别判定约束（必须写进 prompt，避免"调试口认证"被硬件抢走）
FUNCTIONAL_CONFUSION_RULES = """- 调试认证/会话/命令/审计/完整性 → 4.4 安全调试；调试口物理禁用/熔丝/OTP/防拆 → 4.1 硬件安全
- 本地刷写/诊断刷写/产线刷写/UDS/CAN/以太网 → 4.5 本地刷写安全；OTA/FOTA/云端下发/远程升级 → 4.6 OTA 刷写安全
- 诊断 27 服务/种子密钥/诊断会话 → 4.10 安全诊断；调试口 JTAG/SWD/调试授权 → 4.4 安全调试
- 加密/签名/哈希/密钥/证书 → 4.12；数据脱敏/隐私/个人信息 → 4.9
- 雷达/摄像头/点云/图像 → 4.13；防火墙/入侵检测 → 4.14"""

# 强物理关键词：命中则强制归 4.1 硬件安全（移除容易误伤的泛化词“篡改”、“物理”）
_STRONG_PHYSICAL = [
    "物理防拆", "外壳防拆", "开盖检测", "丝印", "pcb", "探针", "测试点", "芯片封装",
    "侧信道", "故障注入", "efuse", "smpu", "bga", "lga", "不可逆禁用", "量产禁用", "量产熔断",
]


def classify_functional_category(text: str) -> str:
    """
    忠实评估需求文本属于 17 类功能分类中的哪一类。
    基于上下文核心机制与加权关键词评分，避免单一词汇强行关联（例如避免因“防篡改”强行关联到硬件安全）。
    """
    from collections import Counter
    t = str(text or "").lower()

    # 1. 强物理硬件特征（仅限物理防拆、走线隐藏、探针、不可逆禁用等真正硬件机制）
    if any(k in t for k in _STRONG_PHYSICAL):
        return "4.1 硬件安全"

    # 2. 数据脱敏与隐私保护（优先识别个人数据、用户同意、去标识化，避免被调试或存储误占）
    if any(k in t for k in ["脱敏", "去标识", "匿名", "假名", "个人信息", "个人数据", "用户同意", "用户告知", "隐私"]):
        return "4.9 数据脱敏与隐私安全"

    # 3. 安全日志与审计（核心特征：安全事件日志、防篡改仅追加记录、时间源绑定，避免被硬件篡改或调试强行抢走）
    if any(k in t for k in ["安全日志", "审计记录", "仅追加", "日志防篡改", "日志存储", "日志条目", "审计数据", "日志记录", "安全事件记录", "审计日志"]):
        return "4.11 安全日志"

    # 4. 安全调试（限定为 JTAG/SWD/UART/调试会话/外部调试等专用调试机制）
    if any(k in t for k in ["调试会话", "调试接口", "调试命令", "调试认证", "调试授权", "jtag", "swd", "uart调试", "外部调试"]):
        return "4.4 安全调试"

    # 5. OTA 刷写安全
    if any(k in t for k in ["ota", "fota", "sota", "远程升级", "云端下发", "升级包", "下载地址"]):
        return "4.6 OTA 刷写安全"

    # 6. 本地刷写安全
    if any(k in t for k in ["本地刷写", "产线刷写", "诊断刷写", "刷写包", "0x34", "0x36", "fbl", "烧录"]):
        return "4.5 本地刷写安全"

    # 7. 安全启动（基于信任根的度量启动、启动组件验签、引导程序防回滚）
    if any(k in t for k in ["安全启动", "secure boot", "度量启动", "启动链", "bootrom", "bootloader", "只读信任根", "引导程序", "防回滚"]):
        return "4.7 安全启动"

    # 8. 安全诊断（UDS 0x27 安全访问服务、种子密钥、诊断特权会话）
    if any(k in t for k in ["27服务", "27 服务", "security access", "种子密钥", "诊断会话", "诊断权限", "安全诊断"]):
        return "4.10 安全诊断"

    # 9. 车内通信安全（CAN/CAN-FD/LIN/车载以太网总线、SecOC、报文新鲜度、报文过滤）
    if any(k in t for k in ["can", "can fd", "can/canfd", "lin", "flexray", "总线报文", "车载总线", "车载以太网", "secoc", "新鲜度", "报文防重放", "报文过滤", "入站报文"]):
        return "4.15 车内通信安全"

    # 10. 传感器安全（传感器欺骗检测、雷达点云真实性、摄像头原始流、传感器物理线束防护）
    if any(k in t for k in ["传感器欺骗", "点云", "视频流", "图像感知", "雷达感知", "传感器原始", "传感器线束", "串行链路"]):
        return "4.13 传感器安全"

    # 11. 防火墙安全（防火墙过滤、域隔离、入侵检测与防御 IDPS）
    if any(k in t for k in ["防火墙", "idps", "入侵检测", "入侵防御", "网络隔离", "域隔离", "异常流量"]):
        return "4.14 防火墙安全"

    # 12. 密码、密钥与证书安全（密码算法、数字证书、密钥全生命周期）
    if any(k in t for k in ["密钥", "证书", "密码算法", "对称加密", "非对称加密", "sm2", "sm3", "sm4", "aes", "rsa", "ecc"]):
        return "4.12 密码、密钥与证书安全"

    # 13. 安全存储（TEE隔离存储、敏感数据加密存储、安全文件系统）
    if any(k in t for k in ["安全存储", "加密存储", "敏感数据存储", "隔离存储", "tee存储"]):
        return "4.8 安全存储"

    # 14. 远程通讯安全（车联网、T-Box、车云TLS通道、蜂窝网络）
    if any(k in t for k in ["车联网", "t-box", "蜂窝", "4g", "5g", "车云通信", "v2x"]):
        return "4.16 远程通讯安全"

    # 15. 安全开发与供应链安全（代码扫描、SBOM、漏洞管理、渗透测试）
    if any(k in t for k in ["代码扫描", "sast", "dast", "sca", "sbom", "第三方组件", "供应链", "渗透测试", "安全编码"]):
        return "4.17 安全开发与供应链安全"

    # 16. 访问控制（身份鉴别、基于角色的权限访问控制 RBAC）
    if any(k in t for k in ["访问控制", "身份认证", "身份鉴别", "最小权限", "鉴权", "rbac", "会话管理"]):
        return "4.3 访问控制"

    # 17. 加权关键词计分兜底（忠实评估，不强行联系）
    scores = Counter()
    for cat, kws in FUNCTIONAL_KEYWORDS.items():
        for kw in kws:
            if kw in t:
                scores[cat] += 2 if len(kw) >= 3 else 1

    if scores:
        return scores.most_common(1)[0][0]

    # 最终自然兜底：系统加固
    return "4.2 系统加固"


def normalize_functional_category(value: str) -> str:
    """Normalize LLM output (code, name, or free text) to a canonical 17-category name."""
    s = str(value or "").strip()
    if not s:
        return ""
    if s in FUNCTIONAL_CATEGORY_NAMES:
        return s
    for full in FUNCTIONAL_CATEGORY_NAMES:
        code = full.split(" ", 1)[0]
        if s == code:
            return full
    for full in FUNCTIONAL_CATEGORY_NAMES:
        name = full.split(" ", 1)[1]
        if name and (name in s or s in name):
            return full
    # 旧 8 域兜底映射
    legacy_map = {
        "硬件安全 / Hardware Security": "4.1 硬件安全",
        "系统安全 / System Security": "4.2 系统加固",
        "密码安全与密钥管理 / Cryptography & Key Management": "4.12 密码、密钥与证书安全",
        "应用安全 / Application Security": "4.3 访问控制",
        "数据安全与隐私保护 / Data Security & Privacy": "4.9 数据脱敏与隐私安全",
        "安全监控、测试与响应 / Security Monitoring, Testing & Response": "4.11 安全日志",
        "通信安全 / Communication Security": "4.15 车内通信安全",
        "安全开发、供应链与运维 / Secure Development, Supply Chain & Operations": "4.17 安全开发与供应链安全",
        "通用安全与其它 / General & Other Security": "4.2 系统加固",
    }
    return legacy_map.get(clean_major_category(s), "")


def correct_functional_category(llm_category: str, text: str):
    """规范化大模型分类结果。
    
    优先且完全信任大模型返回的分类；仅在未配置 LLM 或 LLM 返回空时走离线规则兜底。
    """
    llm_cat = normalize_functional_category(llm_category)
    if llm_cat:
        return llm_cat, "llm", False
    rule_cat = classify_functional_category(text)
    return rule_cat, "rule_fallback", True
