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
