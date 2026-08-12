#!/usr/bin/env python3
"""本机定时任务内层（由 run_bot.bat 调用）：跑 main.py --once，失败时向飞书群发告警。

8/11 教训：22:00 进程被强行终止（退出码 3221225786 = Ctrl+C 式退出），死了 8 秒，
没有任何告警——用户直到凌晨来问才知道。此后推送失败再也不会静默。

防御：
1. 子进程执行 main.py --once，30 分钟超时
2. 退出码非零 → 告警
3. 退出码为零但输出里没有「推送成功」→ 也告警（防内部 except 吞掉异常后的软失败）
4. 告警卡包含退出码 + 时间戳 + 日志末尾，发到飞书群
"""

import subprocess
import sys
import os
import signal
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "storage" / "bot_cron.log"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"


def _load_webhook() -> str:
    """从 .env 读取飞书 webhook（复用 main.load_config 避免读错）。"""
    try:
        from main import load_config
        return load_config().get("feishu", {}).get("webhook_url", "")
    except Exception:
        return os.environ.get("FEISHU_WEBHOOK_URL", "")


def _send_alert(webhook: str, rc: int, log_tail: str):
    """往飞书群发失败告警卡。"""
    if not webhook or "YOUR_WEBHOOK_TOKEN" in webhook:
        print(f"[告警] webhook 未配置或为占位符，不发送")
        return

    import json
    import httpx

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if log_tail:
        # 截断，飞书卡片内容限制约 30K 字符
        tail = log_tail[-2000:]
        if len(log_tail) > 2000:
            tail = f"…（截断，完整见 bot_cron.log）\n{tail}"

    md = (
        f"**OPC 日报推送失败**\n\n"
        f"退出码：`{rc}`\n"
        f"时间：{ts}"
    )
    if log_tail:
        md += f"\n\n**日志末尾：**\n```\n{tail}\n```"

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "⚠️ 日报推送失败"},
                "template": "red",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": md}}
            ],
        },
    }

    try:
        resp = httpx.post(
            webhook,
            json=card,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=15,
        )
        print(f"[告警] 飞书返回 {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[告警] 飞书发送也失败: {e}")


_ABORTED = {"sig": None}


def _on_signal(signum, frame):
    # 只记录"被信号中断"，真正的告警在 main 末尾发，避免 handler 里做重 IO
    _ABORTED["sig"] = signum


def main() -> int:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"=== run_bot 启动 {ts} ===")

    # 注册信号：即使被 Ctrl+C / SIGTERM 中断，也要能发出失败告警
    # （8/11 教训：进程被 3221225786 式终止时，静默失败无人知晓）
    try:
        signal.signal(signal.SIGINT, _on_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _on_signal)
    except (ValueError, OSError):
        pass  # 非主线程等情况下可能注册失败，忽略

    LOG.parent.mkdir(parents=True, exist_ok=True)

    rc = None
    output = ""
    try:
        proc = subprocess.run(
            [str(VENV_PY), "main.py", "--once"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,  # 30 分钟
        )
        rc = proc.returncode
        output = proc.stdout + "\n" + proc.stderr
    except subprocess.TimeoutExpired:
        rc = -1
        output = "[超时] main.py 超过 30 分钟未完成"
    except KeyboardInterrupt:
        rc = -3221225786
        output = "[被中断] run_bot 收到 Ctrl+C / SIGINT"
    except Exception as e:
        rc = -2
        output = f"[异常] run_bot 自身: {e}"

    # 子进程输出直接打印，由 bat 的 `>> storage\bot_cron.log` 统一收集。
    # 不要再 open 同一个文件——会和 bat 的重定向抢句柄导致 Windows 下
    # Permission denied，main.py 的真实输出（含推送失败原因）就全丢了。
    if output:
        print(output)

    # 被信号中断：优先发告警（覆盖 3221225786 式静默死亡）
    if _ABORTED["sig"] is not None:
        print(f"[被信号 {_ABORTED['sig']} 中断] 发告警")
        _send_alert(_load_webhook(), -3221225786, output)
        return 0

    # 判定成功：退出码为零 且 日志含「推送成功」
    success = (rc == 0) and ("推送成功" in output)

    if success:
        print(f"=== run_bot 完成 退出码 {rc} ===")
        return 0

    # 失败 → 告警
    has_push = "推送成功" in output
    print(
        f"[失败] 退出码={rc}, 含「推送成功」={has_push}, 发告警"
    )
    _send_alert(_load_webhook(), rc or -1, output)
    return 0  # 告警发出就算收工，不让 bat/cmd 因为非零退出导致额外告警


if __name__ == "__main__":
    sys.exit(main())
