"""容错解析 LLM 返回的 JSON。

LLM（尤其 gpt-4o-mini / agnes 类小模型）常在 JSON 字符串值里塞未转义的双引号、
尾随逗号、单引号键，标准 json.loads 直接挂。这里用 json5（纯 Python、最宽容）优先解析，
失败再用正则提取最外层数组/对象兜底。

调用方：发现引擎、拆解引擎、源侦察兵 三处 LLM-JSON 解析统一走这里。
"""
import json
import logging
import re

try:
    import json5
    _HAVE_JSON5 = True
except ImportError:  # pragma: no cover
    _HAVE_JSON5 = False

logger = logging.getLogger(__name__)


def parse_llm_json(raw: str, fallback_to_list: bool = True):
    """把 LLM 返回的 JSON 字符串解析成 list / dict。

    - 自动去 ```json 围栏
    - 兼容未转义引号、尾逗号、单引号键（json5）
    - 外层混有解释文字时，用正则提取最外层 [] / {}
    解析不出：fallback_to_list=True 返回 []，否则返回 None。
    """
    if not raw:
        return [] if fallback_to_list else None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()

    # 1) 整段直接解析（json5 最宽容）
    obj = _try_parse(raw)
    if isinstance(obj, (list, dict)):
        return obj

    # 2) 正则提取最外层数组/对象再解析（容外层解释文字）
    candidates = []
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if m:
        candidates.append(m.group())
    m2 = re.search(r"\{.*\}", raw, re.DOTALL)
    if m2:
        candidates.append(m2.group())

    for c in candidates:
        obj = _try_parse(c)
        if isinstance(obj, (list, dict)):
            return obj

    if fallback_to_list:
        logger.error(f"LLM JSON 解析失败: {raw[:200]}")
        return []
    return None


def _try_parse(s: str):
    if _HAVE_JSON5:
        try:
            return json5.loads(s)
        except Exception:
            pass
    try:
        return json.loads(s)
    except Exception:
        return None
