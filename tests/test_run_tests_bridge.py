"""把主力行为测试 `tests/run_tests.py` 接入 pytest 门禁。

背景（门禁假绿的根因）
--------------------
`run_gates.py:106` 固定执行 `python -m pytest -q tests/`，而 pytest 默认只收集
`test_*.py` / `*_test.py`。`run_tests.py` 两者都不匹配 —— 它是自带 `main()`、
用 `check()` 记结果、`sys.exit(0/1)` 的独立脚本，因此 **308 项行为测试从未进入门禁**。
门禁长期显示「PASS 2 / SKIP 0 / FAIL 0」，实际只跑了 6 个 test_webui_display.py。

本文件作为桥接：pytest 收集它 → 子进程跑 `run_tests.py` → 断言退出码与结果汇总行。
之所以用子进程而不是 import，是因为 `run_tests.py` 会往 `sys.modules` 注入
`maibot_sdk` stub 并改全局状态，与 pytest 进程内已导入的真实 SDK 冲突。

这样 `gate.sh` / `run_gates.py` 一行都不用改（唯一门禁入口不该为单个插件改动）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
RUN_TESTS = TESTS_DIR / "run_tests.py"

# 当前基线 308 项。用「下限」而不是写死 308：用例增加不应让门禁变红，
# 但用例被大批删除（或脚本只跑了个空壳）必须报警。
MIN_EXPECTED_CASES = 300


def test_full_behavior_suite_passes() -> None:
    assert RUN_TESTS.is_file(), f"找不到主力测试脚本：{RUN_TESTS}"

    proc = subprocess.run(
        [sys.executable, str(RUN_TESTS)],
        cwd=str(PLUGIN_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    out = (proc.stdout or "") + (proc.stderr or "")

    assert proc.returncode == 0, (
        f"run_tests.py 退出码 {proc.returncode}（存在未通过项）\n"
        f"--- 输出尾部 ---\n{out[-3000:]}"
    )

    match = re.search(r"结果：(\d+)/(\d+)\s*通过", out)
    assert match, f"未找到结果汇总行，脚本可能没跑完\n--- 输出尾部 ---\n{out[-2000:]}"

    passed, total = int(match.group(1)), int(match.group(2))
    assert total >= MIN_EXPECTED_CASES, (
        f"用例总数 {total} 低于下限 {MIN_EXPECTED_CASES}，"
        "可能是测试脚本被截断或用例被误删"
    )
    assert passed == total, (
        f"主力测试存在未通过项：{passed}/{total}\n--- 输出尾部 ---\n{out[-3000:]}"
    )
