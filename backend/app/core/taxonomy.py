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

# 强物理关键词：命中则强制归 4.1 硬件安全（LLM 不得覆盖）
_STRONG_PHYSICAL = [
    "物理", "丝印", "pcb", "封装", "防拆", "探针", "篡改", "hsm", "efuse",
    "smpu", "bga", "lga", "外壳", "侧信道", "存储绑定",
    "otp", "永久禁用", "不可逆禁用", "量产禁用",
]


def classify_functional_category(text: str) -> str:
    """Deterministic keyword classifier for the 17 functional categories.

    Used as a sanity check / fallback for the LLM classification; the LLM sees
    the full text and remains the primary classifier.
    """
    t = str(text or "").lower()

    def has(*kws):
        return any(k in t for k in kws)

    # 1) 物理/硬件层（最强，优先）
    if has(*_STRONG_PHYSICAL):
        return "4.1 硬件安全"
    # 2) 安全调试（调试主题优先，避免被"认证/日志"抢走）
    if has("调试", "debug", "jtag", "swd"):
        return "4.4 安全调试"
    # 3) OTA（在本地刷写之前，因为 OTA 常同时提到"刷写"）
    if has("ota", "fota", "远程升级", "远程刷写"):
        return "4.6 OTA 刷写安全"
    # 4) 本地刷写
    if has("刷写", "刷新", "uds", "产线刷写", "诊断刷写", "flashing"):
        return "4.5 本地刷写安全"
    # 5) 安全启动
    if has("安全启动", "启动", "回滚", "rollback", "secure boot"):
        return "4.7 安全启动"
    # 6) 数据脱敏 / 隐私（放在"存储"之前：脱敏存储应归隐私，不是存储）
    if has("脱敏", "去标识", "匿名", "隐私", "数据分类", "保留期限", "跨境", "privacy"):
        return "4.9 数据脱敏与隐私安全"
    # 7) 安全存储
    if has("存储", "storage"):
        return "4.8 安全存储"
    # 8) 安全诊断
    if has("诊断", "27服务", "27 服务", "diagnostic"):
        return "4.10 安全诊断"
    # 9) 安全日志
    if has("日志", "审计", "log", "audit"):
        return "4.11 安全日志"
    # 10) 密码 / 密钥 / 证书
    if has("加密", "签名", "验签", "密码算法", "哈希", "crypto", "encrypt"):
        return "4.12 密码、密钥与证书安全"
    if has("密钥", "key management"):
        return "4.12 密码、密钥与证书安全"
    if has("证书", "certificate"):
        return "4.12 密码、密钥与证书安全"
    # 11) 传感器
    if has("雷达", "摄像头", "点云", "图像", "感知", "传感器", "radar", "camera"):
        return "4.13 传感器安全"
    # 12) 防火墙
    if has("防火墙", "入侵", "idps", "网络隔离", "firewall", "intrusion"):
        return "4.14 防火墙安全"
    # 13) 车内通信
    if has("车内通信", "总线", "报文", "以太网", "ethernet"):
        return "4.15 车内通信安全"
    # 14) 远程通讯
    if has("远程", "外网", "云端", "remote", "telematics"):
        return "4.16 远程通讯安全"
    # 15) 安全开发与供应链
    if has("代码扫描", "漏洞", "sast", "sca", "sbom", "供应链", "渗透测试", "安全编码"):
        return "4.17 安全开发与供应链安全"
    # 16) 访问控制
    if has("认证", "授权", "最小权限", "访问控制", "会话", "权限"):
        return "4.3 访问控制"
    # 17) 兜底
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
    """Validate the LLM category against the deterministic rule classifier.

    Returns ``(category, source, needs_review)`` where ``source`` is one of
    ``llm`` / ``rule_fallback`` / ``rule_override``.
    """
    llm_cat = normalize_functional_category(llm_category)
    rule_cat = classify_functional_category(text)
    if not llm_cat:
        return rule_cat, "rule_fallback", True
    if llm_cat == rule_cat:
        return llm_cat, "llm", False
    t = str(text or "").lower()
    # 强物理关键词 + 规则判为硬件 → 强制覆盖
    if rule_cat == "4.1 硬件安全" and any(k in t for k in _STRONG_PHYSICAL):
        return "4.1 硬件安全", "rule_override", False
    return llm_cat, "llm", True
