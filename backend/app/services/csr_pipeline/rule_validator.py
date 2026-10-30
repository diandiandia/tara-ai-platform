"""CSR Pipeline V2 — 程序规则校验器

纯代码，不依赖 LLM。可复现、可审计。
用于替代人工抽检，覆盖 LLM 典型错误。
"""
from __future__ import annotations
import re
from typing import Any
from .models import PipelineRecord, Atom, PipelineConfig


# ---------------------------------------------------------------------------
# ValidationResult
# ---------------------------------------------------------------------------

class ValidationResult:
    __slots__ = ("valid", "errors", "warnings")
    def __init__(self, valid: bool = True, errors: list[str] | None = None, warnings: list[str] | None = None):
        self.valid = valid
        self.errors = errors or []
        self.warnings = warnings or []

    def __bool__(self) -> bool:
        return self.valid

    def to_dict(self) -> dict:
        return {"valid": self.valid, "errors": self.errors, "warnings": self.warnings}


# ============================================================================
# 文本规范化（用于哈希去重和校验预处理）
# ============================================================================

def normalize_text(text: str) -> str:
    """规范化文本：去空格、全半角统一、英文小写、去标点差异。"""
    text = str(text or "")
    # 全角转半角
    result = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            result.append(" ")
        else:
            result.append(ch)
    text = "".join(result)
    # 去空白
    text = re.sub(r"\s+", "", text)
    # 英文小写
    text = text.lower()
    # 去常见标点差异
    text = re.sub(r"[.,;:!?。，；：！？、""''「」『』【】（）()《》<>{}]", "", text)
    return text


# ============================================================================
# 规范表述校验器
# ============================================================================

class CanonicalValidator:
    """校验 LLM 生成的 canonical text 是否保留了原文的关键信息。"""

    # 情态形容词（不应在 canonical 中弱化的词）
    MODAL_STRONG = {"应", "必须", "不得", "禁止", "shall", "must", "shall not", "must not"}
    MODAL_WEAK = {"宜", "可", "建议", "should", "may", "recommend"}

    # 否定词集
    NEGATION_WORDS = {"不得", "禁止", "拒绝", "丢弃", "阻止", "断开", "终止",
                      "not", "no", "reject", "discard", "block", "terminate", "deny"}

    def __init__(self, config: PipelineConfig | None = None):
        self.config = config
        self._synonym_table = config.synonym_table if config else {}

    def check(self, originals: list[str], canonical: str) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        orig_text = " ".join(originals)
        orig_lower = orig_text.lower()
        canon_lower = canonical.lower()

        # 1. 情态检查
        modal_errors = self._check_modal(orig_text, canonical)
        errors.extend(modal_errors)

        # 2. 否定检查
        neg_errors = self._check_negation(orig_text, orig_lower, canonical, canon_lower)
        errors.extend(neg_errors)

        # 3. 数字检查
        num_errors = self._check_numbers(orig_text, canonical)
        errors.extend(num_errors)

        # 4. 条件检查
        cond_warnings = self._check_conditions(orig_lower, canon_lower)
        warnings.extend(cond_warnings)

        return ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    def _check_modal(self, orig: str, canon: str) -> list[str]:
        errors = []
        has_strong_orig = bool(self.MODAL_STRONG & set(orig))
        has_weak_canon = bool(self.MODAL_WEAK & set(canon))
        has_strong_canon = bool(self.MODAL_STRONG & set(canon))

        if has_strong_orig and has_weak_canon and not has_strong_canon:
            errors.append("情态弱化: 原文含强情态词(应/必须/shall)，输出仅含弱情态词(宜/可/建议)")
        if has_strong_orig and not has_strong_canon and not has_weak_canon:
            errors.append("情态丢失: 原文含强情态词(应/必须/shall)，输出未包含任何情态词")
        return errors

    def _check_negation(self, orig: str, orig_lower: str, canon: str, canon_lower: str) -> list[str]:
        errors = []
        orig_negs = [w for w in self.NEGATION_WORDS if w in orig or w.lower() in orig_lower]
        canon_negs = [w for w in self.NEGATION_WORDS if w in canon or w.lower() in canon_lower]

        for nw in orig_negs:
            if not any(cn == nw or cn.lower() == nw.lower() for cn in canon_negs):
                # 检查是否有同义替换
                found_syn = False
                for syn_list in self._synonym_table.values():
                    if nw in syn_list or nw.lower() in [s.lower() for s in syn_list]:
                        for syn in syn_list:
                            if syn in canon or syn.lower() in canon_lower:
                                found_syn = True
                                break
                if not found_syn:
                    errors.append(f"否定丢失: 原文含否定词'{nw}'，输出未保留")
        return errors

    def _check_numbers(self, orig: str, canon: str) -> list[str]:
        errors = []
        orig_nums = set(re.findall(r"\d+(?:\.\d+)?", orig))
        canon_nums = set(re.findall(r"\d+(?:\.\d+)?", canon))
        # 只检查 ≥ 2 位的数字（1 位数字可能是编号或计数，忽略）
        orig_significant = {n for n in orig_nums if len(n) >= 2}
        canon_significant = {n for n in canon_nums if len(n) >= 2}
        if orig_significant - canon_significant:
            errors.append(f"数字丢失: 原文含数字 {orig_significant - canon_significant}，输出未保留")
        return errors

    def _check_conditions(self, orig_lower: str, canon_lower: str) -> list[str]:
        warnings = []
        # 检查常见限定条件是否保留
        condition_markers = [
            "量产", "开发", "售后", "维护", "禁用后", "启用前",
            "校验前", "认证后", "授权后", "通过后", "失败时",
            "production", "development", "after-sales", "maintenance",
        ]
        for marker in condition_markers:
            if marker in orig_lower and marker not in canon_lower:
                warnings.append(f"条件可能丢失: 原文含'{marker}'，输出未包含")
        return warnings

    def check_complement(self, action_text: str, failure_text: str, canonical: str) -> ValidationResult:
        """互补对校验：canonical 必须同时包含动作和失败处理。"""
        errors = []
        canon_lower = canonical.lower()
        act_lower = action_text.lower()
        fail_lower = failure_text.lower()

        # 动作部分的否定词必须在 canonical 中
        act_negs = {w for w in self.NEGATION_WORDS if w.lower() in act_lower}
        canon_negs = {w for w in self.NEGATION_WORDS if w.lower() in canon_lower}
        if act_negs - canon_negs:
            errors.append(f"互补动作否定丢失: {act_negs - canon_negs}")

        # 失败处理部分的否定词必须在 canonical 中
        fail_negs = {w for w in self.NEGATION_WORDS if w.lower() in fail_lower}
        if fail_negs - canon_negs:
            errors.append(f"互补失败处理否定丢失: {fail_negs - canon_negs}")

        return ValidationResult(valid=len(errors) == 0, errors=errors)


# ============================================================================
# 同构归组校验器
# ============================================================================

class IsomorphicValidator:
    """校验同构归组的正确性。"""

    def __init__(self, config: PipelineConfig | None = None):
        self.config = config
        self._synonym_table = config.synonym_table if config else {}
        # 开放词
        self._open_words = {"等", "此类", "等等", "et al", "etc", "…", "..."}

    def check(self, group: list[PipelineRecord], canonical: str) -> ValidationResult:
        errors: list[str] = []

        # 1. 宾语并集完整性
        obj_errors = self._check_object_coverage(group, canonical)
        errors.extend(obj_errors)

        # 2. 约束一致性
        cons_errors = self._check_constraint_consistency(group, canonical)
        errors.extend(cons_errors)

        # 3. 封闭性
        closed_errors = self._check_closed_set(canonical)
        errors.extend(closed_errors)

        # 4. 例外条件保留
        except_errors = self._check_exception_preserved(group, canonical)
        errors.extend(except_errors)

        return ValidationResult(valid=len(errors) == 0, errors=errors)

    def _check_object_coverage(self, group: list[PipelineRecord], canonical: str) -> list[str]:
        errors = []
        canon_lower = canonical.lower()
        for rec in group:
            objects = rec.object_phrases
            if not objects:
                continue
            covered = False
            for obj in objects:
                if obj.lower() in canon_lower:
                    covered = True
                    break
                # 同义词检查
                for syn_list in self._synonym_table.values():
                    if obj in syn_list:
                        for syn in syn_list:
                            if syn.lower() in canon_lower:
                                covered = True
                                break
            if not covered:
                errors.append(f"宾语丢失: record '{rec.record_id}' 的宾语 '{objects}' 未在枚举表述中找到")
        return errors

    def _check_constraint_consistency(self, group: list[PipelineRecord], canonical: str) -> list[str]:
        errors = []
        canon_lower = canonical.lower()
        for rec in group:
            # 检查每条的 constraint 是否都在 canonical 中
            for cons in rec.constraint_phrases:
                if not cons:
                    continue
                if cons.lower() not in canon_lower:
                    # 同义词检查
                    found = False
                    for syn_list in self._synonym_table.values():
                        if cons in syn_list:
                            for syn in syn_list:
                                if syn.lower() in canon_lower:
                                    found = True
                                    break
                    if not found:
                        errors.append(f"约束不一致: record '{rec.record_id}' 的约束 '{cons}' 在枚举表述中未体现")
        return errors

    def _check_closed_set(self, canonical: str) -> list[str]:
        errors = []
        for word in self._open_words:
            if word in canonical:
                errors.append(f"非封闭: 枚举表述含开放词'{word}'")
        return errors

    def _check_exception_preserved(self, group: list[PipelineRecord], canonical: str) -> list[str]:
        """双向关键词集合覆盖：canonical 例外关键词集 ⊇ 原文例外关键词集。"""
        errors = []
        canon_lower = canonical.lower()

        # 从原文抽取例外关键词
        except_keywords = self._extract_exception_keywords(group)
        # 从 canonical 抽取例外关键词
        canon_keywords = self._extract_exception_keywords_text(canonical)

        for kw in except_keywords:
            kw_lower = kw.lower()
            covered = kw_lower in canon_keywords
            if not covered:
                for syn_list in self._synonym_table.values():
                    if kw in syn_list:
                        for syn in syn_list:
                            if syn.lower() in canon_keywords:
                                covered = True
                                break
            if not covered:
                errors.append(f"例外条件丢失: 原文例外关键词'{kw}'未在枚举表述中保留")
        return errors

    def _extract_exception_keywords(self, group: list[PipelineRecord]) -> set[str]:
        """从组内所有记录的原文中抽取例外条件关键词。"""
        markers = {
            "受控", "维护", "授权", "预定义策略", "按策略",
            "临时", "量产", "开发", "售后",
            "仅允许", "仅", "必须满足", "前提下",
            "controlled", "maintenance", "authorized",
            "predefined", "temporary", "production", "development",
        }
        found: set[str] = set()
        for rec in group:
            text = rec.requirement_text + " " + (rec.canonical_text or "")
            for marker in markers:
                if marker.lower() in text.lower():
                    found.add(marker)
            # 也检查 constraint_phrases
            for cons in rec.constraint_phrases:
                for marker in markers:
                    if marker.lower() in cons.lower():
                        found.add(marker)
        return found

    def _extract_exception_keywords_text(self, text: str) -> set[str]:
        markers = {
            "受控", "维护", "授权", "预定义策略", "按策略",
            "临时", "量产", "开发", "售后",
            "仅允许", "仅", "必须满足", "前提下",
            "controlled", "maintenance", "authorized",
            "predefined", "temporary", "production", "development",
        }
        text_lower = text.lower()
        return {m for m in markers if m.lower() in text_lower}


# ============================================================================
# 损性校验（保守原子化时使用）
# ============================================================================

class ConservativeAtomizeValidator:
    """校验原子化拆分是否无损：拆分后的各部分是否并集覆盖了原文的关键信息。"""

    def check(self, original_text: str, atom_texts: list[str]) -> ValidationResult:
        errors: list[str] = []
        orig_lower = original_text.lower()
        combined_lower = " ".join(atom_texts).lower()

        # 1. 情态检查
        for mw in CanonicalValidator.MODAL_STRONG:
            if mw in original_text and mw.lower() not in combined_lower:
                errors.append(f"原子化情态丢失: '{mw}'")

        # 2. 否定检查
        for nw in CanonicalValidator.NEGATION_WORDS:
            if nw.lower() in orig_lower and nw.lower() not in combined_lower:
                errors.append(f"原子化否定丢失: '{nw}'")

        # 3. 数字检查
        orig_nums = set(re.findall(r"\d+(?:\.\d+)?", original_text))
        combined_nums = set(re.findall(r"\d+(?:\.\d+)?", combined_lower))
        significant = {n for n in orig_nums if len(n) >= 2}
        if significant - combined_nums:
            errors.append(f"原子化数字丢失: {significant - combined_nums}")

        return ValidationResult(valid=len(errors) == 0, errors=errors)


# ============================================================================
# 编辑器距离（用于近精确去重）
# ============================================================================

import difflib


def edit_distance_similarity(a: str, b: str) -> float:
    """编辑距离 / 序列相似度（0-1）。使用 Python 标准库 difflib (C 级别优化)。"""
    a = normalize_text(a)
    b = normalize_text(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    len_a, len_b = len(a), len(b)
    max_len = max(len_a, len_b)
    if abs(len_a - len_b) / max_len > 0.05:
        return 1.0 - (abs(len_a - len_b) / max_len)

    sm = difflib.SequenceMatcher(None, a, b)
    if sm.quick_ratio() < 0.95:
        return sm.quick_ratio()
    return sm.ratio()


# ============================================================================
# 结构化抽取（Phase 1.5）
# ============================================================================

class StructureExtractor:
    """从 requirement_text 中抽取 action_verbs, object_phrases, constraint_phrases。
    规则优先（内置词典匹配），匹配不到走 LLM。
    """

    def __init__(self, config: PipelineConfig):
        self._verb_dict = {v.lower() for v in (config.action_verb_dict or [])}

    def extract_action_verbs(self, text: str) -> list[str]:
        text_lower = text.lower()
        found = []
        for v in sorted(self._verb_dict, key=len, reverse=True):  # 最长匹配优先
            if v in text_lower:
                found.append(v)
                # 避免子串重复匹配
                text_lower = text_lower.replace(v, " " * len(v))
        return found

    def extract_object_phrases(self, text: str) -> list[str]:
        """简单规则抽取对象短语（后续 LLM 兜底）。"""
        objects = []
        # 匹配 "...的..." 模式获取主体
        subject_matches = re.findall(r"【主体】(.*?)【要求】", text)
        for m in subject_matches:
            objects.append(m.strip())
        # 匹配 "对...执行/进行" 模式
        target_matches = re.findall(r"对(.*?)(?:执行|进行|做)", text)
        for m in target_matches:
            objects.append(m.strip())
        # 匹配 "验证/保护/校验...的完整性/机密性" 模式中的对象
        attr_matches = re.findall(r"(验证|保护|校验|检查)(.*?)(?:的完整性|的机密性|的可用性|的授权)", text)
        for _, obj in attr_matches:
            objects.append(obj.strip())
        return list(set(objects)) if objects else []

    def extract_constraint_phrases(self, text: str) -> list[str]:
        """抽取约束条件：量产/开发/维护阶段、触发条件、例外条件等。"""
        constraints = []
        markers = [
            "量产阶段", "开发阶段", "维护阶段", "售后阶段",
            "生产阶段", "调试阶段",
            "建立会话前", "写入前", "生效前", "启用前", "禁用前",
            "校验前", "认证后", "授权后",
            "production", "development", "maintenance",
        ]
        text_lower = text.lower()
        for m in markers:
            if m.lower() in text_lower:
                constraints.append(m)
        # 匹配 "在...时/前/后" 模式
        when_matches = re.findall(r"在(.*?)(?:时|前|后|阶段|条件下)", text)
        for m in when_matches:
            if len(m) < 20:  # 避免匹配太长
                constraints.append(m.strip())
        return constraints