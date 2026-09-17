"""
日报正文归档 —— 把每天「实际送达」的内容落盘，供月刊 / 复盘使用。

**为什么需要**：日报正文是当天生成、推给群、然后就丢掉的。机会库
(opportunity_library.json) 只存主题名 + 指纹 + 日期，正文（AI 摘要、实操步骤、
抄作业模板）不落盘 —— 月底想做月刊时无料可用。2026-09-17 用户要做月刊才发现
这个问题，此模块补齐。

**只归档实际送达的条目**（与 history 去重标记同口径）：没送出去的内容不该
进归档，否则月刊里会出现用户从没见过的东西。

文件布局：storage/archive/YYYY-MM.jsonl，一行一条，跨天累积。
写入按 (date, url/operator_handle) 去重，CI 重试重投不会写重复行。
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _clean(value):
    """把 dataclass / 嵌套对象转成可 JSON 化的结构。"""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "isoformat"):  # datetime / date
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return str(value)


def opportunity_record(item, date_cst: str, region: str) -> dict:
    """一条机会的完整正文（够月刊做深加工）。"""
    return _clean({
        "date": date_cst,
        "module": "opportunity",
        "region": region,                       # domestic / international
        "url": getattr(item, "url", ""),
        "title": getattr(item, "title", ""),
        "translation": getattr(item, "translation", ""),
        "summary": getattr(item, "ai_summary", "") or getattr(item, "summary", ""),
        "opportunity_hint": getattr(item, "opportunity_hint", ""),
        "practical_steps": getattr(item, "practical_steps", ""),
        "copy_template": getattr(item, "copy_template", None) or {},
        "difficulty": getattr(item, "difficulty", ""),
        "quality_flag": getattr(item, "quality_flag", ""),
        "startup_index": getattr(item, "startup_index", 0),
        "authenticity": getattr(item, "authenticity", 0),
        "code_dependency": getattr(item, "code_dependency", 0),
        "source_name": getattr(item, "source_name", "") or getattr(item, "source", ""),
        "published": getattr(item, "published", None),
        "topic_key": getattr(item, "topic_key", ""),
        "repeat_count": getattr(item, "repeat_count", 0),
        "gate_reason": getattr(item, "gate_reason", ""),
    })


def teardown_record(td: dict, date_cst: str) -> dict:
    """一个操盘手拆解的完整正文。"""
    rec = {k: v for k, v in (td or {}).items() if k != "content"}
    rec.update({"date": date_cst, "module": "teardown"})
    rec.setdefault("operator_handle", "")
    return _clean(rec)


class DailyArchive:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, date_cst: str) -> Path:
        return self.root / f"{date_cst[:7]}.jsonl"

    @staticmethod
    def _key(rec: dict):
        return (rec.get("date"), rec.get("url") or rec.get("operator_handle"))

    def _seen(self, path: Path, module: str) -> set:
        seen = set()
        if not path.exists():
            return seen
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("module") == module:
                    seen.add(self._key(rec))
        except Exception as e:
            logger.warning("归档读取失败（本次不去重，可能写重复行）: %s", e)
        return seen

    def append(self, date_cst: str, module: str, records: list) -> int:
        """追加归档，返回实际写入条数。同一 (date, url/handle) 不重复写。"""
        records = [r for r in (records or []) if r]
        if not records:
            return 0
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self._path(date_cst)
            seen = self._seen(path, module)
            written = 0
            with open(path, "a", encoding="utf-8") as f:
                for rec in records:
                    key = self._key(rec)
                    if key in seen:
                        continue
                    seen.add(key)
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    written += 1
            if written:
                logger.info(f"已归档 {module} {written} 条 → {path}")
            return written
        except Exception as e:
            # 归档失败绝不影响推送结果（归档是给月刊用的，不是推送链路的一环）
            logger.warning(f"归档写入失败（不影响推送）: {e}")
            return 0
