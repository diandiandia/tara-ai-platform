"""CSR Pipeline V2 — LLM 代理

封装 LLM 调用、自检机制、JSONL 日志。
所有 LLM 交互通过此模块，管道其他部分不直接调用 LLM API。
"""
from __future__ import annotations
import hashlib
import json
import os
import time
from typing import Any
from .models import SelfCheckResult

LLMSettings = dict


# ---------------------------------------------------------------------------
# LLM 调用底层
# ---------------------------------------------------------------------------

def _call_llm_json_internal(
    settings: dict | None,
    system_prompt: str,
    user_payload: dict | str,
    model_name: str = "deepseek-chat",
    temperature: float = 0.0,
    max_retries: int = 3,
) -> dict:
    """底层 LLM JSON 调用，带重试。"""
    import openai

    if not settings:
        return {}

    api_key = settings.get("api_key", "")
    api_base = settings.get("api_base_url", "https://api.deepseek.com/v1")
    model = settings.get("model_name", model_name)

    client = openai.OpenAI(api_key=api_key, base_url=api_base)

    user_text = json.dumps(user_payload, ensure_ascii=False) if isinstance(user_payload, dict) else str(user_payload)

    last_error = None
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                response_format={"type": "json_object"},
                timeout=120,
            )
            raw = resp.choices[0].message.content or ""
            return _safe_json_parse(raw)
        except Exception as e:
            last_error = str(e)
            if attempt < max_retries - 1:
                time.sleep(min(2 ** attempt, 10))
    return {"_error": f"LLM 调用失败 (retried {max_retries}x): {last_error}"}


def _safe_json_parse(raw: str) -> dict:
    """安全解析 LLM 返回的 JSON，处理 markdown 代码块包裹。"""
    text = raw.strip()
    # 去掉 ```json ... ``` 包裹
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 尝试提取第一个 { ... } 块
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        return {"_raw": text, "_parse_error": True}


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def _log_llm_call(
    log_dir: str,
    stage: str,
    prompt_hash: str,
    input_ids: list[str],
    input_texts_abstract: list[str],
    raw_response: str,
    parsed_output: dict,
    self_check_info: dict | None = None,
):
    """JSONL 日志落盘。"""
    if not log_dir:
        return
    try:
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "csr_pipeline_llm.jsonl")
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "stage": stage,
            "prompt_hash": prompt_hash,
            "input_count": len(input_ids),
            "input_ids": input_ids[:50],  # 截断
            "input_texts_abstract": input_texts_abstract[:50],
            "raw_response": raw_response[:5000],
            "parsed_output": parsed_output,
            "self_check_info": self_check_info,
        }
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _prompt_hash(system_prompt: str) -> str:
    return hashlib.sha256(system_prompt.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# LLMProxy — 对外接口
# ---------------------------------------------------------------------------

class LLMProxy:
    def __init__(
        self,
        settings: dict | None = None,
        log_dir: str = "",
        temperature: float = 0.0,
        max_retries: int = 3,
    ):
        self.settings = settings
        self.log_dir = log_dir
        self.temperature = temperature
        self.max_retries = max_retries

    # ===== 基础调用 =====

    def call(
        self,
        stage: str,
        system_prompt: str,
        user_payload: dict | str,
        input_ids: list[str] | None = None,
        input_texts_abstract: list[str] | None = None,
    ) -> dict:
        """单次 LLM 调用（无自检）。"""
        result = _call_llm_json_internal(
            self.settings,
            system_prompt,
            user_payload,
            temperature=self.temperature,
            max_retries=self.max_retries,
        )
        _log_llm_call(
            self.log_dir,
            stage,
            _prompt_hash(system_prompt),
            input_ids or [],
            input_texts_abstract or [],
            json.dumps(result, ensure_ascii=False),
            result,
        )
        return result

    # ===== 判定类调用（带自检） =====

    def judge_with_self_check(
        self,
        stage: str,
        system_prompt: str,
        check_prompt: str,
        user_payload: dict | str,
        input_ids: list[str] | None = None,
        input_texts_abstract: list[str] | None = None,
    ) -> SelfCheckResult:
        """
        判定 + 反向验证自检。
        `check_prompt` 必须是反向验证视角的 prompt（"有没有错"而非"再判一次"）。
        """
        # 第一次调用（正向判定）
        first_result = self.call(
            f"{stage}-forward",
            system_prompt,
            user_payload,
            input_ids=input_ids,
            input_texts_abstract=input_texts_abstract,
        )

        if first_result.get("_error"):
            return SelfCheckResult(
                agreed=False,
                confidence="LOW",
                first_result=first_result,
                discrepancies=[first_result["_error"]],
            )

        # 第二次调用（反向自检）—— 输入 = 第一次的结果 + 原文
        check_payload = {
            "original_input": user_payload,
            "forward_judgment": first_result,
        }
        if isinstance(user_payload, dict):
            check_payload["original_input"] = user_payload

        second_result = self.call(
            f"{stage}-self-check",
            check_prompt,
            check_payload,
            input_ids=input_ids,
            input_texts_abstract=input_texts_abstract,
        )

        # 比较两次结果
        agreed, confidence, discrepancies = self._compare_results(
            first_result, second_result
        )

        sc = SelfCheckResult(
            agreed=agreed,
            confidence=confidence,
            first_result=first_result,
            second_result=second_result,
            discrepancies=discrepancies,
        )

        _log_llm_call(
            self.log_dir,
            f"{stage}-self-check-summary",
            _prompt_hash(check_prompt),
            input_ids or [],
            input_texts_abstract or [],
            json.dumps(sc.__dict__, ensure_ascii=False, default=str),
            sc.__dict__,
            self_check_info=sc.__dict__,
        )

        return sc

    # ===== 分类类调用 =====

    def classify(
        self,
        stage: str,
        system_prompt: str,
        items: list[dict],
        input_ids: list[str] | None = None,
    ) -> dict:
        """批量分类，单次调用。"""
        return self.call(
            stage,
            system_prompt,
            {"items": items},
            input_ids=input_ids or [],
            input_texts_abstract=[str(i.get("text", ""))[:200] for i in items],
        )

    # ===== 生成类调用 =====

    def generate(
        self,
        stage: str,
        system_prompt: str,
        user_payload: dict | str,
        input_ids: list[str] | None = None,
        input_texts_full: list[str] | None = None,
    ) -> dict:
        """文本生成，记录完整输入原文（用于审计复现）。"""
        return self.call(
            stage,
            system_prompt,
            user_payload,
            input_ids=input_ids or [],
            input_texts_abstract=input_texts_full or [],
        )

    def generate_with_self_check(
        self,
        stage: str,
        system_prompt: str,
        check_prompt: str,
        user_payload: dict | str,
        input_ids: list[str] | None = None,
        input_texts_full: list[str] | None = None,
    ) -> SelfCheckResult:
        """生成 + 反向验证自检。"""
        first_result = self.generate(
            f"{stage}-forward",
            system_prompt,
            user_payload,
            input_ids=input_ids,
            input_texts_full=input_texts_full,
        )

        if first_result.get("_error"):
            return SelfCheckResult(
                agreed=False,
                confidence="LOW",
                first_result=first_result,
                discrepancies=[first_result["_error"]],
            )

        check_payload = {
            "original_input": user_payload,
            "forward_output": first_result,
        }

        second_result = self.generate(
            f"{stage}-self-check",
            check_prompt,
            check_payload,
            input_ids=input_ids,
            input_texts_full=input_texts_full,
        )

        agreed, confidence, discrepancies = self._compare_results(first_result, second_result)

        sc = SelfCheckResult(
            agreed=agreed,
            confidence=confidence,
            first_result=first_result,
            second_result=second_result,
            discrepancies=discrepancies,
        )

        _log_llm_call(
            self.log_dir,
            f"{stage}-self-check-summary",
            _prompt_hash(check_prompt),
            input_ids or [],
            input_texts_full or [],
            json.dumps(sc.__dict__, ensure_ascii=False, default=str),
            sc.__dict__,
            self_check_info=sc.__dict__,
        )

        return sc

    # ===== 内部 =====

    def _compare_results(self, first: dict, second: dict) -> tuple[bool, str, list[str]]:
        """比较两次调用结果。"""
        if second.get("_error") or second.get("_parse_error"):
            return False, "LOW", ["自检调用失败"]

        # 如果第二次结果明确标注了错误
        if second.get("has_errors") or second.get("corrections"):
            corrections = second.get("corrections", [])
            disc = [str(c) for c in corrections]
            return False, "LOW", disc

        # 如果第二次结果整体一致
        if second.get("all_correct") or second.get("agreed"):
            return True, "HIGH", []

        # 默认中等置信度
        return True, "MEDIUM", []