"""评论语域纪律：代码级强制的口语化规则 + 书面套话拦截 + 主动弃评出口。

v1.7.0 起本模块是 `comment_policy` 的**薄包装**：词表、判定顺序、引导文本、
策略钳制全部收拢在 `comment_policy.py`（零插件内依赖的纯函数模块）。
这里保留所有公开名字（STYLE_GUARD / RETRY_HINT / SKIP_SENTINELS /
_STIFF_MARKERS / _ROMANTIC_MARKERS 与 5 个函数），是为了不破坏既有 308 项
行为测试与三条生成路径的调用面 —— 重构不等于换 API。

为什么必须是"代码级"而不是"模板默认值"
--------------------------------------
[auto].comment_prompt / [reply].prompt / [auto].atme_prompt 都是**用户可编辑**
模板，且首次运行即落盘 config.toml，之后**不随插件升级更新**。任何写在配置
默认值里的规则，对已部署实例都等于没写——v1.2.9 加的身份锚点就栽在这里：
代码里的 _DEFAULT_COMMENT_PROMPT 改了，真机 config.toml 仍是旧模板。
因此凡属"发布安全的硬纪律"，一律由 `comment_policy` 在**代码层**拼接，绕开模板。

真机事故（2026-10-03）
--------------------
好友动态「喜报，无料有了，我奶找到了」→ bot 评论
「恭喜！双喜临门，无料和奶奶都到位啦！」被围观嘲笑。拆开是两类问题：
1. **语域错位（真凶）**：「双喜临门」「都到位啦」是官方通稿/书面总结腔。
2. **硬解梗**：「我奶找到了」是圈内说法，bot 按字面当成一件真事解读。

用户定的方向：**读不懂就别装懂、宁可短**。拆成三件事：
- `STYLE_GUARD`：追加到每条 prompt 末尾的发言纪律；
- `looks_like_stiff_register()`：输出侧 fail-closed 拦书面套话，给一次重写机会；
- `SKIP_SENTINELS`：给模型一个**主动弃评出口**（输出 [跳过] 即不评论）。
"""

from .comment_policy import (
    FEW_SHOT,
    RETRY_HINT,
    SKIP_SENTINELS,
    STYLE_GUARD,
    UNDERSTANDING_CHECK,
    _ROMANTIC_MARKERS,
    _STIFF_MARKERS,
    build_guard_text,
    build_style_retry_text,
    looks_like,
)

__all__ = [
    "STYLE_GUARD", "RETRY_HINT", "SKIP_SENTINELS", "FEW_SHOT", "UNDERSTANDING_CHECK",
    "_STIFF_MARKERS", "_ROMANTIC_MARKERS",
    "looks_like_skip", "looks_like_stiff_register", "looks_like_romantic_register",
    "append_style_guard", "build_style_retry_prompt",
]


def looks_like_skip(text) -> bool:
    """文本是否为模型主动弃评（输出 [跳过] 之类标记）。

    只看开头若干字符：模型偶尔会在标记后补一句解释，那也应当算弃评。
    """
    if not text:
        return False
    t = str(text).strip()
    if not t:
        return False
    return t.startswith(SKIP_SENTINELS)


def looks_like_stiff_register(text) -> bool:
    """文本是否为书面通稿腔（成语/祝福语/公文体/汇报体）。fail-closed。"""
    return looks_like(text, _STIFF_MARKERS)


def looks_like_romantic_register(text) -> bool:
    """评论是否越入亲昵/恋爱语域（认领他人表白、对好友公开回爱）。

    fail-closed：命中即不发布，**且不给重写机会**（v1.6.0 起的取舍）。
    """
    return looks_like(text, _ROMANTIC_MARKERS)


def append_style_guard(prompt: str, policy=None) -> str:
    """在 prompt 末尾追加引导段（few-shot + 理解校验 + 发言纪律），幂等。

    不传 policy 时用默认策略（引导开启）—— 三条生成路径与既有单测都走这里。
    """
    return build_guard_text(prompt, policy)


def build_style_retry_prompt(prompt: str, bad_text: str, policy=None) -> str:
    """构造语域重写 prompt：把不合格输出作为反例回灌。"""
    return build_style_retry_text(prompt, bad_text, policy)
