#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qzone-feeds 冒烟测试（FakeHost，不启动 MaiBot、不联网）。

运行（须用装了 maibot-plugin-sdk 的解释器）：
    python tests/smoke_test.py

退出码 0 = 全部通过；非 0 = 冒烟失败。

为什么这个文件必须存在
----------------------
`run_gates.py:92-94` 只有看到 `tests/smoke_test.py` 才会执行冒烟步骤；
缺了它，门禁只跑 check_plugin + pytest（当时仅 6 个 test_webui_display.py），
输出「PASS 2 / FAIL 0 / 门禁全绿」却**从未加载过插件** —— 这就是 v1.6.1 的假绿。

断言分四类，都是「改错了不会抛异常、只会静默失效」的类型：
1. manifest 与现实一致（版本 / id / capabilities / 版本区间）；
2. 组件清单：7 个 @Command 的名字、类型、handler 名、正则（防装饰器绑错人）；
3. handler 方法在插件类上真实存在（组件清单只查名字抓不到「绑到错函数」）；
4. 真实生命周期 on_load / on_unload —— 冒烟的核心价值是「插件能否加载并干净收尾」。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import traceback
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
for _p in (str(PLUGIN_DIR), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fakehost  # noqa: E402,F401  （缺了要立刻报错，不能静默跳过检查）
from fakehost import (  # noqa: E402
    FakeHost,
    bind_context,
    build_context,
    get_default_config,
    load_plugin_module,
)

PLUGIN_ID = "org.orge-8.qzone-feeds"

#: 7 个 @Command：(组件名, handler 方法名, 应能匹配的样例命令)。
#: 组件名是 ASCII，用户敲的是中文，匹配全靠 command_pattern。
EXPECTED_COMMANDS: dict[str, tuple[str, str]] = {
    "qzone_feeds": ("cmd_friend_feeds", "/好友动态 5"),
    "qzone_msglist": ("cmd_msglist", "/说说 2472005478 3"),
    "qzone_msglist_interact": ("cmd_msglist_interact", "/说说互动 2472005478"),
    "qzone_publish": ("cmd_publish", "/动态发 今天天气不错"),
    "qzone_publish_image": ("cmd_publish_image", "/动态发图 配图 | https://example.com/a.jpg"),
    "qzone_reply_comments": ("cmd_reply_comments", "/回复评论"),
    "qzone_status": ("cmd_status", "/动态状态"),
}

#: 不应被任何命令误吞的普通聊天文本
ORDINARY_TEXTS = ("你好呀", "/help", "今天天气不错", "说说而已", "动态状态是什么")

REQUIRED_CAPABILITIES = {
    "send.text", "llm.generate", "api.call",
    "config.get", "person.get_id", "person.get_value",
}


# ═══════════════════════════════════════════ 各项检查


def check_manifest_matches_reality() -> list[str]:
    """manifest 必须与代码现实一致；错了不会抛异常，只会静默失效。"""
    failures: list[str] = []
    manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))

    version = str(manifest.get("version") or "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        failures.append(f"version 必须是三段式语义版本，实际 {version!r}")
    if str(manifest.get("id") or "") != PLUGIN_ID:
        failures.append(f"manifest id 应为 {PLUGIN_ID}，实际 {manifest.get('id')!r}")

    declared = set(manifest.get("capabilities") or [])
    missing = sorted(REQUIRED_CAPABILITIES - declared)
    if missing:
        failures.append(f"manifest 缺少必需能力声明：{missing}")

    for section in ("host_application", "sdk"):
        rng = manifest.get(section) or {}
        if not rng.get("min_version") or not rng.get("max_version"):
            failures.append(f"{section} 缺 min_version / max_version")
    return failures


def check_component_inventory(plugin) -> list[str]:
    """组件清单：数量 + 名字 + 类型 + handler 名 + 正则。专抓装饰器绑错人。"""
    failures: list[str] = []
    components = plugin.get_components()
    found = {item["name"]: item for item in components}

    if len(components) != len(EXPECTED_COMMANDS):
        failures.append(f"组件总数应为 {len(EXPECTED_COMMANDS)}，实际 {len(components)}")
    missing = sorted(set(EXPECTED_COMMANDS) - set(found))
    if missing:
        failures.append(f"缺少组件：{missing}")
    unexpected = sorted(set(found) - set(EXPECTED_COMMANDS))
    if unexpected:
        failures.append(f"多出未预期的组件：{unexpected}")

    plugin_cls = type(plugin)
    for name, (expected_handler, sample) in EXPECTED_COMMANDS.items():
        item = found.get(name)
        if item is None:
            continue
        if item.get("type") != "COMMAND":
            failures.append(f"{name}: 类型应为 COMMAND，实际 {item.get('type')!r}")
        meta = item.get("metadata") or {}
        handler = meta.get("handler_name")
        if handler != expected_handler:
            failures.append(
                f"{name}: handler 应为 {expected_handler!r}，实际 {handler!r}"
                "（装饰器与 def 之间插了辅助方法时会这样）"
            )
        # handler 方法必须真的存在——组件清单只查名字抓不到「绑到错函数」
        if not callable(getattr(plugin_cls, expected_handler, None)):
            failures.append(f"{name}: 插件类上找不到可调用的 {expected_handler}")
        pattern = meta.get("command_pattern") or ""
        if not pattern:
            failures.append(f"{name}: 没有 command_pattern")
        else:
            try:
                if not re.fullmatch(pattern, sample):
                    failures.append(f"{name}: 正则匹配不上样例命令 {sample!r}")
                for ordinary in ORDINARY_TEXTS:
                    if re.fullmatch(pattern, ordinary):
                        failures.append(f"{name}: 误匹配普通文本 {ordinary!r}")
            except re.error as exc:
                failures.append(f"{name}: command_pattern 不是合法正则：{exc}")
    return failures


def check_default_config(plugin) -> list[str]:
    """默认配置必须能构造且关键开关生效（配置模型坏了不会抛异常，只会静默降级）。"""
    failures: list[str] = []
    cfg = plugin.config
    try:
        if cfg.plugin.enabled is not True:
            failures.append("默认配置 plugin.enabled 应为 True")
        if not (0.0 <= float(cfg.auto.comment_probability) <= 1.0):
            failures.append(f"auto.comment_probability 越界：{cfg.auto.comment_probability!r}")
        if int(cfg.auto.short_content_chars) <= 0:
            failures.append(f"auto.short_content_chars 应为正数：{cfg.auto.short_content_chars!r}")
    except AttributeError as exc:
        failures.append(f"默认配置缺字段：{exc}")
    return failures


def check_lifecycle(plugin) -> list[str]:
    """真实 on_load / on_unload：插件能否加载、关键成员是否初始化、卸载是否干净收尾。

    全程走 FakeHost 的 rpc_call，不联网；cookie 探活会拿到假响应并走 error 日志，
    这是预期行为（不抛异常即可），不是冒烟失败。
    """
    failures: list[str] = []

    async def drive() -> None:
        await plugin.on_load()
        for attr in ("_cookie_mgr", "_store", "_reply_mgr", "_atme_mgr",
                     "_auto_loop", "_queue", "_worker_task"):
            if getattr(plugin, attr, None) is None:
                failures.append(f"on_load 后 {attr} 未初始化")
        if plugin._auto_loop is not None and not plugin._auto_loop.is_running:
            failures.append("on_load 后自动任务循环未启动")
        await plugin.on_unload()
        if plugin._auto_loop is not None and plugin._auto_loop.is_running:
            failures.append("on_unload 后自动任务循环仍在运行")
        if getattr(plugin, "_worker_task", None) is not None:
            failures.append("on_unload 后串行队列 worker 未回收")

    asyncio.run(drive())
    return failures


# ═══════════════════════════════════════════ 运行


def _build_plugin():
    module = load_plugin_module(PLUGIN_DIR)
    plugin = module.create_plugin()
    host = FakeHost(plugin_id=PLUGIN_ID)
    ctx = build_context(PLUGIN_ID, rpc_call=host.rpc_call)
    bind_context(plugin, ctx, get_default_config(type(plugin).config_model))
    return plugin


def main() -> int:
    try:
        plugin = _build_plugin()
    except Exception:  # noqa: BLE001
        detail = "FAIL  插件装载\n" + traceback.format_exc(limit=6)
        print(detail)
        print(detail.splitlines()[0], file=sys.stderr)
        return 1

    checks = [
        ("manifest 与现实一致（版本/id/能力/版本区间）", lambda: check_manifest_matches_reality()),
        ("组件清单（7 命令：名字/类型/handler/正则）", lambda: check_component_inventory(plugin)),
        ("默认配置可构造且关键字段生效", lambda: check_default_config(plugin)),
        ("生命周期 on_load / on_unload 干净收尾", lambda: check_lifecycle(plugin)),
    ]

    bad = 0
    for name, check in checks:
        try:
            problems = check() or []
        except Exception:  # noqa: BLE001
            problems = ["检查本身抛异常：\n" + traceback.format_exc(limit=4)]
        if problems:
            bad += 1
        print(f"{'FAIL' if problems else 'PASS'}  {name}")
        for problem in problems:
            print(f"      · {problem}")

    print("-" * 60)
    if bad:
        summary = f"冒烟未通过：{bad}/{len(checks)} 项有问题"
        print(summary)
        # run_gates.py 的摘要取 (stdout + stderr) 的最后一行；插件日志走 stderr，
        # 不补这一行的话门禁摘要会显示 cookie 探活的 error 日志，而不是冒烟结论。
        print(summary, file=sys.stderr)
        return 1
    summary = f"冒烟全部通过：{len(checks)}/{len(checks)}（已真实加载插件）"
    print(summary)
    print(summary, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
