"""宿主人设与表达风格注入：读 MaiBot 全局配置，作为**语气参考**拼进 prompt。

两段内容，来源同一个 `[personality]` 段（MaiBot `src/config/official_configs.py`
的 `PersonalityConfig`，main 分支实测字段）：

    await ctx.config.get("personality.personality", "")   # 人设：麦麦是谁、什么性格
    await ctx.config.get("personality.reply_style", "")   # 表达风格：平时怎么措辞

- `personality`：身份与性格 → 决定"是个什么角色"
- `reply_style`**（表达风格）**：措辞习惯（简短 / 吐槽 / 贴吧风 / 不用比喻…）
  → 决定"话怎么说"，对 20 字短评的影响比人设更直接
- `behavior_style` 不收：它是 Planner 的行动准则（何时插话、何时沉默），
  与"发一条评论"的场景无关，塞进来只会让模型跑题
- `multiple_reply_style` / `multiple_probability` 不收：那是宿主自己的随机换风格
  机制，插件侧再叠一层随机会让评论风格不可复现，排障时无从归因

与 person_context 的区别（两者不是一件事，别混）
------------------------------------------------
- `person_context`：查**好友**在人物库里的昵称与印象 → "你对 TA 的了解"
- `host_persona`（本模块）：查**bot 自己**的人设与风格 → "你平时怎么说话"

为什么是"语气参考"而不是"你是 X"
--------------------------------
v1.3.3 事故（「双喜临门」通稿腔）的直接结论是：给模型一句自称句
（"你是 XXX"）会让它进入自我指涉状态，产出的正是官方汇报腔。
人设注入如果照抄 `你是{persona}`，等于把刚修好的坑重开一遍。
所以这里只给**风格参考**，并在末句显式声明"纪律优先于风格"：

    【说话风格参考】<personality>
    【表达风格】<reply_style>
    （这段只说口气和用词习惯，不改变你在跟谁说话；
      与后文的【发言纪律】冲突时，一律以发言纪律为准。）

注入位置：模板之后、STYLE_GUARD 之前。纪律必须留在 prompt 最末
（末尾指令对模型的约束力最强），人设与风格只能排在它前面。

降级策略（全部静默，绝不阻塞评论）
--------------------------------
- 无 `ctx.config` / 读超时 / 抛异常 → 空串
- 返回非字符串（Host 键缺失可能回 None / 空 dict）→ 空串，不 str() 化
- `[persona].persona_prompt` 覆盖项非空 → 整体接管（人设+风格都不读宿主）
- `[persona].reply_style_prompt` 非空 → 只覆盖表达风格，人设仍读宿主
- 结果按配置键分别做 TTL 缓存（**空结果也负缓存**），避免每条评论都走 Host RPC

截断
----
宿主 `personality` 真机上是几百字长文，`reply_style` 一般较短但也可写成多句；
而评论上限只有 20 字。两段都是"语气锚"不是"知识库"，塞满只会稀释纪律段的权重，
故各自按句截断（人设 600 可配 / 风格默认 100 且硬上限 100，见
`_STYLE_MAX_CHARS_CEILING`）。
"""

import asyncio
import inspect
import time

# 单次 Host RPC 超时（秒）：config.get 挂死时不能拖垮评论循环
_QUERY_TIMEOUT_SEC = 5.0

# 宿主配置键（bot_config.toml [personality]）
_HOST_PERSONALITY_KEY = "personality.personality"
_HOST_REPLY_STYLE_KEY = "personality.reply_style"

# 注入标记（幂等判定 + 日志诊断用）
PERSONA_MARKER = "【说话风格参考】"
REPLY_STYLE_MARKER = "【表达风格】"

# 段模板。刻意不出现"你是""以…身份"这类自称句式（见模块头）。
_TAIL_HINT = (
    "\n（这段只说口气和用词习惯，不改变你在跟谁说话；"
    "与后文的【发言纪律】冲突时，一律以发言纪律为准。）"
)

# 截断时优先切在这些字符之后（句子边界）
_SENTENCE_ENDS = "。！？!?\n"

# 默认值（配置段缺失/字段读不到时使用）
_DEFAULT_MAX_CHARS = 600
_DEFAULT_REPLY_STYLE_MAX_CHARS = 100
_DEFAULT_TTL_SEC = 600

# 表达风格段的**代码级硬上限**（v1.6.0）
#
# 为什么必须有这一层，而不是只改配置默认值：
# config.toml 首次运行即落盘，之后**不随插件升级更新**——已部署实例的 toml 里
# 早写着 `reply_style_max_chars = 300`，改 plugin.py 的 default 对它们等于没改。
# v1.2.9 的身份锚点、v1.3.3 的发言纪律都栽在同一个坑里，这里不再重犯。
#
# 100 的依据：真机 `reply_style` 实测 416 字，而评论上限只有 20 字；
# 300 字时比例 15:1，风格段会把末尾的【发言纪律】挤成弱势指令。
# 放宽上限＝改这个常量（有意的摩擦：每加一个字都在稀释纪律）。
_STYLE_MAX_CHARS_CEILING = 100

# 宿主读取结果缓存：key -> {"value": str, "at": float, "loaded": bool}
_CACHE: dict = {}


class NoLogger:
    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass

    def debug(self, msg):
        pass


logger = NoLogger()


def set_host_persona_logger(custom_logger):
    global logger
    logger = custom_logger


def reset_persona_cache():
    """清空缓存（测试/热重载/改完宿主配置用）。"""
    _CACHE.clear()


# ── 截断 ─────────────────────────────────────────────────────────────────────
def truncate_persona(text, max_chars: int = _DEFAULT_MAX_CHARS) -> str:
    """按句截断文本，保持可读、不留半句。

    Args:
        text: 原始文本（人设或表达风格）
        max_chars: 上限字符数，<=0 表示不截断

    Returns:
        截断后的文本（超长时补省略号）。
    """
    t = str(text or "").strip()
    if not t:
        return ""
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = _DEFAULT_MAX_CHARS
    if limit <= 0 or len(t) <= limit:
        return t
    head = t[:limit]
    # 在句末切；切点太靠前（<半个窗口）则退化为硬截断，避免只留下一个短句
    cut = max(head.rfind(c) for c in _SENTENCE_ENDS)
    if cut >= limit // 2:
        head = head[:cut + 1]
    return head.rstrip() + "…"


# ── 配置读取 ─────────────────────────────────────────────────────────────────
def _persona_cfg(plugin) -> dict:
    """读 [persona] 段配置，缺失/异常时用模块默认值。

    Returns:
        {"enabled", "override", "style_override", "max_chars",
         "style_max_chars", "ttl"}
    """
    cfg = {
        "enabled": True,
        "override": "",
        "style_override": "",
        "max_chars": _DEFAULT_MAX_CHARS,
        "style_max_chars": _DEFAULT_REPLY_STYLE_MAX_CHARS,
        "ttl": _DEFAULT_TTL_SEC,
    }
    try:
        section = plugin.config.persona
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return cfg
    if section is None:
        return cfg

    def _get(name, default, cast):
        try:
            return cast(getattr(section, name, default))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return default

    cfg["enabled"] = _get("enable_host_persona", True, bool)
    cfg["override"] = _get("persona_prompt", "", lambda v: str(v or "").strip())
    cfg["style_override"] = _get("reply_style_prompt", "",
                                 lambda v: str(v or "").strip())
    cfg["max_chars"] = _get("persona_max_chars", _DEFAULT_MAX_CHARS, int)
    style_chars = _get("reply_style_max_chars",
                       _DEFAULT_REPLY_STYLE_MAX_CHARS, int)
    if style_chars > _STYLE_MAX_CHARS_CEILING:
        logger.info(f"表达风格注入 {style_chars} 字超出硬上限，"
                    f"钳制为 {_STYLE_MAX_CHARS_CEILING} 字"
                    f"（上限见 host_persona._STYLE_MAX_CHARS_CEILING）")
        style_chars = _STYLE_MAX_CHARS_CEILING
    cfg["style_max_chars"] = style_chars
    cfg["ttl"] = max(0, _get("persona_cache_ttl_sec", _DEFAULT_TTL_SEC, int))
    return cfg


# ── 宿主读取 ─────────────────────────────────────────────────────────────────
async def _read_host_field(plugin, key: str) -> str:
    """读宿主 bot_config.toml 的一个点分键。任何失败返回空串。"""
    try:
        host_cfg = plugin.ctx.config
    except (AttributeError, RuntimeError, TypeError):
        logger.debug(f"ctx.config 不可用，跳过宿主 {key}")
        return ""

    try:
        pending = host_cfg.get(key, "")
        # config.get 在部分 Host 实现里是同步的，两种都兼容
        if inspect.isawaitable(pending):
            raw = await asyncio.wait_for(pending, timeout=_QUERY_TIMEOUT_SEC)
        else:
            raw = pending
    except asyncio.TimeoutError:
        logger.debug(f"读取宿主 {key} 超时（>{_QUERY_TIMEOUT_SEC}s）")
        return ""
    except Exception as e:
        logger.debug(f"读取宿主 {key} 失败: {e}")
        return ""

    # 只接受字符串：键缺失时 Host 可能回 None / {}，str() 化会把 "{}" 塞进 prompt
    if not isinstance(raw, str):
        if raw not in (None, "", {}):
            logger.debug(f"宿主 {key} 非字符串（{type(raw).__name__}），按空处理")
        return ""
    return raw.strip()


async def _fetch_host_text(plugin, key: str, label: str,
                           max_chars: int, ttl: int) -> str:
    """带 TTL 缓存的宿主字段读取（含空结果负缓存）。"""
    now = time.time()
    try:
        ttl_sec = max(0, int(ttl))
    except (TypeError, ValueError):
        ttl_sec = _DEFAULT_TTL_SEC

    entry = _CACHE.get(key)
    if entry and entry["loaded"] and ttl_sec > 0 and (now - entry["at"]) < ttl_sec:
        return truncate_persona(entry["value"], max_chars)

    value = await _read_host_field(plugin, key)
    _CACHE[key] = {"value": value, "at": now, "loaded": True}
    if value:
        # 真机自证信号：只在真正读到时打一次 INFO（缓存期内不重复）
        logger.info(f"{label}已加载: {len(value)}字（缓存{ttl_sec}s，来自 {key}）")
    else:
        logger.debug(f"{label}为空（宿主未配置 {key} 或读取失败），本轮不注入")
    return truncate_persona(value, max_chars)


async def fetch_host_persona(plugin, override: str = "",
                             max_chars: int = _DEFAULT_MAX_CHARS,
                             ttl: int = _DEFAULT_TTL_SEC) -> str:
    """取当前生效的人设文本（覆盖项优先，其次宿主，带 TTL 缓存）。

    Args:
        plugin: 插件实例（用 plugin.ctx.config）
        override: 覆盖项，非空则直接用它（不读宿主）
        max_chars: 截断上限
        ttl: 宿主读取结果缓存秒数，0 表示每次重读

    Returns:
        截断后的人设文本；任何失败路径返回 ""。
    """
    ov = str(override or "").strip()
    if ov:
        return truncate_persona(ov, max_chars)
    return await _fetch_host_text(plugin, _HOST_PERSONALITY_KEY, "宿主人设",
                                  max_chars, ttl)


async def fetch_reply_style(plugin, override: str = "",
                            max_chars: int = _DEFAULT_REPLY_STYLE_MAX_CHARS,
                            ttl: int = _DEFAULT_TTL_SEC) -> str:
    """取当前生效的表达风格文本（`personality.reply_style`，可被覆盖项替换）。"""
    ov = str(override or "").strip()
    if ov:
        return truncate_persona(ov, max_chars)
    return await _fetch_host_text(plugin, _HOST_REPLY_STYLE_KEY, "宿主表达风格",
                                  max_chars, ttl)


# ── 注入 ─────────────────────────────────────────────────────────────────────
def build_persona_block(persona: str, reply_style: str = "") -> str:
    """把（人设, 表达风格）包成 prompt 片段；两段都空返回空串（不留痕迹）。"""
    p = str(persona or "").strip()
    s = str(reply_style or "").strip()
    if not p and not s:
        return ""
    lines = []
    if p:
        lines.append(PERSONA_MARKER + p)
    if s:
        lines.append(REPLY_STYLE_MARKER + s)
    return "\n\n" + "\n".join(lines) + _TAIL_HINT


async def inject_persona(plugin, prompt: str) -> str:
    """在 prompt 末尾（纪律段之前）追加入设与表达风格参考段。

    幂等：prompt 已含人设段则原样返回。plugin 为 None / 无 config 时原样返回。

    Returns:
        注入后的 prompt；无人设无风格时与入参完全一致。
    """
    if not prompt:
        return prompt
    if plugin is None:
        return prompt
    p = str(prompt)
    if PERSONA_MARKER in p or REPLY_STYLE_MARKER in p:
        return p

    cfg = _persona_cfg(plugin)
    if not cfg["enabled"]:
        return p

    # persona_prompt 非空 = 整体接管：人设与风格都不读宿主（语义与 v1.4.0 一致）
    if cfg["override"]:
        block = build_persona_block(cfg["override"])
        return p + block if block else p

    persona = await fetch_host_persona(plugin, max_chars=cfg["max_chars"],
                                       ttl=cfg["ttl"])
    style = await fetch_reply_style(plugin, override=cfg["style_override"],
                                    max_chars=cfg["style_max_chars"],
                                    ttl=cfg["ttl"])
    block = build_persona_block(persona, style)
    return p + block if block else p
