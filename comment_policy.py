"""评论决策规则集（v1.7.0 重构）：把散落的决策逻辑收拢到一处。

收拢范围（对应方案文档 11.2 的 12 个决策点）
------------------------------------------
A 段素材决策（prompt 之前，决定「给 LLM 看什么」），原先散在 `auto_tasks.process_feeds`：
  1 素材守卫 / 2 转发归属降级 / 3 截断披露 / 4 缺图披露 / 5 短正文+@+缺图降级 /
  6 评论概率（仍由调用方按概率判，本层只产出 has_material）
B 段发布前决策（LLM 之后，决定「让不让发布」），原先散在 `comment_style` + `reply_manager`：
  4 类词表 / 8 步判定顺序 / 重写策略 / 8 个 reason

边界 B：配置只放「开关 + 阈值」，规则内容、判定顺序、reason 枚举、fail-closed 默认值、
长度上限全部**代码级写死**。`HARD_RULE_IDS` 三类（拒答 / 身份错位 / 亲昵语域）在任何
配置下都无条件执行 —— `resolve_policy()` 根本不读取能关掉它们的字段。理由见
`comment_style.py` 文件头：`config.toml` 首次落盘后不随插件升级更新，写进配置默认值的
规则对已部署实例等于没写（v1.2.9 / v1.3.3 / v1.6.0 三次踩过同一个坑）。

本模块**零插件内依赖、纯函数**（不 import 任何其他 qzf 模块），避免与
`comment_style` / `reply_manager` 形成循环依赖；两个旧模块保留全部公开名字作为薄包装，
保证既有 308 项行为测试的调用面不破。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

# ══════════════════════════════════════════════════════════════════
# 一、代码级硬约束（绝不进 config.toml）
# ══════════════════════════════════════════════════════════════════

#: C1：发布评论的最大字数。配置只能调小，上限写死在代码里。
MAX_COMMENT_CHARS_CEILING = 20
DEFAULT_MAX_COMMENT_CHARS = 20

#: 边界 B：三类 fail-closed 硬兜底，任何配置下都不可关。
HARD_RULE_IDS = ("refusal", "identity", "romantic")

SKIP_SENTINELS = ("[跳过]", "[不评论]", "[略过]")

# ── 发言纪律（代码级，追加到每条评论/回复 prompt 末尾）──────────────────
STYLE_GUARD = (
    "\n\n【发言纪律·必须遵守】"
    "1. 不要复述、总结或点评对方说了什么，不要写总结式开头。"
    "2. 像朋友随手回一句，最多20字，一句话能说完就别写第二句，宁可短。"
    "3. 对方的梗、缩写、黑话读不懂就直说读不懂（「没看懂」「啥意思」），"
    "不许猜、不许脑补、不许硬解释成一个像样的意思。"
    "4. 不许用成语、祝福语、书面语和官方腔"
    "（如：双喜临门、都到位、愿你、祝您、请查收）。"
    "5. 实在接不上话，只输出 [跳过] 三个字，不要硬凑。"
)

RETRY_HINT = (
    "\n\n你上一句是「{bad}」——这是书面通稿腔，不是朋友在说话，不能发布。"
    "请重写一句更短更口语的，最多15字；读不懂就说读不懂；"
    "实在接不上话就只输出 [跳过]。"
)

#: C2 正向 few-shot。排在纪律段**之前**，纪律段仍留在 prompt 最末（v1.4.0 定的顺序）。
FEW_SHOT = (
    "\n\n【随手回一句的样子·只学语气，别抄内容】"
    "好友发「周末爬山累瘫了」→「哈哈腿还在吗」"
    "好友发「新出的游戏真上头」→「我也想玩」"
    "好友发一句看不懂的梗 →「没看懂」"
)

#: C3 理解校验。四次真机事故的根因都是「误解素材」，必须在下笔前显式确认。
UNDERSTANDING_CHECK = (
    "\n\n【下笔前先确认】"
    "这条是谁发的、在对谁说、你有没有看到全部素材（没看到的图不要猜）。"
    "只要有一处没把握，就别硬解释，直接输出 [跳过]。"
)

# ── B 段：4 类词表（原样搬自 comment_style / reply_manager，一字未改）──
_STIFF_MARKERS = (
    "双喜临门", "可喜可贺", "好事成双", "皆大欢喜", "锦上添花", "万事如意",
    "心想事成", "未来可期", "前程似锦", "乘风破浪", "砥砺前行", "鹏程万里",
    "步步高升", "圆满结束", "完美收官", "更上一层楼", "一举两得", "如愿以偿",
    "实至名归", "名副其实", "叹为观止", "感人肺腑", "一语中的", "大快人心",
    "祝您", "祝愿", "愿你", "谨祝", "顺祝", "值此", "请查收", "敬请期待",
    "感谢您的", "您辛苦了", "建议您", "推荐您", "为您",
    "都到位", "值得拥有", "不容错过", "强烈推荐",
    "综上所述", "总而言之", "总结一下",
)

_ROMANTIC_MARKERS = (
    "宝宝", "宝贝", "老婆", "老公", "亲爱的", "达令",
    "爱你", "喜欢你", "想你了", "亲亲你", "抱抱你", "嫁给我", "娶你", "这份爱",
    "我收下啦", "这句我收下",
)

_REFUSAL_MARKERS = (
    "按照你的要求", "不能按照", "无法按照", "不符合健康", "不文明", "不恰当的表述",
    "不当的表述", "健康的交流规范", "健康的交流准则", "交流准则", "友善的语言",
    "请使用文明", "我不能提供", "无法提供", "我无法完成", "我不能完成",
    "作为人工智能", "作为AI", "作为一个AI", "作为语言模型", "作为大模型",
    "换个话题", "抱歉，我不能", "抱歉,我不能",
    "不良引导", "危险暗示", "良好的网络环境", "网络交流",
)

_IDENTITY_MARKERS = ("主人", "主人酱", "我的主人", "master", "Master")

# ── A 段：转发截断与动态类型（C4）────────────────────────────────────
_RT_TRUNCATION_TAILS = ("...", "…", "。。")

#: C4 动态类型分流：打卡/签到类短动态默认只点赞不评论（qzone-ultra 的同名做法）。
#: 刻意保守 —— 只在「短正文 + 命中打卡词 + 无有效图描述」时降级，
#: 话题类短说说不受影响（v1.2.2 立的规矩：图挂了不该拖累正常评论）。
CHECKIN_MARKERS = ("打卡", "签到", "连续第", "今日份")
CHECKIN_MAX_PLAIN_CHARS = 15


# ══════════════════════════════════════════════════════════════════
# 二、策略对象与代码级钳制（边界 B）
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Policy:
    """从配置解析出的策略。字段只有开关与阈值，不含任何规则内容。"""

    #: 总开关：只控制 C2 few-shot / C3 理解校验 / C4 动态分流这类**新增引导**。
    #: 关闭时所有既有 fail-closed 拦截仍无条件生效（不减少任何现有拦截）。
    enabled: bool = True
    #: C1 发布字数上限，已被 resolve_policy 钳到 1..MAX_COMMENT_CHARS_CEILING。
    max_comment_chars: int = DEFAULT_MAX_COMMENT_CHARS
    #: stiff 命中后是否给一次重写机会；关闭 = 直接弃评（不放松拦截，只是少一步补救）。
    stiff_retry_enabled: bool = True
    #: C5 规则命中计数，仅观测，不参与任何判定。
    hit_counter_enabled: bool = False


def _cfg_get(cfg, name, default):
    """同时兼容 pydantic 配置对象与 dict / SimpleNamespace；任何异常都退回默认值。"""
    if cfg is None:
        return default
    try:
        if isinstance(cfg, dict):
            return cfg.get(name, default)
        return getattr(cfg, name, default)
    except Exception:  # noqa: BLE001 —— 配置对象形态不受我们控制
        return default


def resolve_policy(plugin) -> Policy:
    """从插件实例取 [auto] 配置并做**代码级钳制**。

    `plugin=None` / 配置缺失 / 字段类型异常一律退回默认策略 —— 测试里大量用
    `process_feeds(None, ...)`，真机上老实例 config.toml 也未必有新字段。
    """
    cfg = None
    try:
        cfg = plugin.config.auto
    except Exception:  # noqa: BLE001
        try:
            cfg = plugin.config.get("auto") if isinstance(plugin.config, dict) else None
        except Exception:  # noqa: BLE001
            cfg = None

    enabled = bool(_cfg_get(cfg, "comment_policy_enabled", True))
    try:
        raw_chars = int(_cfg_get(cfg, "max_comment_chars", DEFAULT_MAX_COMMENT_CHARS))
    except (TypeError, ValueError):
        raw_chars = DEFAULT_MAX_COMMENT_CHARS
    # 上限写死：配置填 100 也只认 20。
    max_chars = max(1, min(raw_chars, MAX_COMMENT_CHARS_CEILING))

    return Policy(
        enabled=enabled,
        max_comment_chars=max_chars,
        stiff_retry_enabled=bool(_cfg_get(cfg, "stiff_retry_enabled", True)),
        hit_counter_enabled=bool(_cfg_get(cfg, "rule_hit_counter_enabled", False)),
    )


def is_hard_rule(rule_id: str) -> bool:
    """边界 B 的可测试接口：三类硬兜底恒生效，不受任何配置影响。"""
    return rule_id in HARD_RULE_IDS


# ══════════════════════════════════════════════════════════════════
# 三、B 段：有序规则表 + 判定顺序 + 命中计数（C5）
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Rule:
    id: str
    markers: tuple[str, ...]
    reason: str
    hard: bool
    #: 命中后是否给一次重写机会（只有 stiff 为 True）
    retry: bool


#: 判定顺序即表顺序，与 v1.6.1 的 `_one_comment_attempt` 完全一致：
#: refusal → identity → romantic → stiff → ok
RULES: tuple[Rule, ...] = (
    Rule("refusal", _REFUSAL_MARKERS, "refusal", True, False),
    Rule("identity", _IDENTITY_MARKERS, "identity", True, False),
    Rule("romantic", _ROMANTIC_MARKERS, "romantic", True, False),
    Rule("stiff", _STIFF_MARKERS, "stiff", False, True),
)
RULE_BY_ID = {rule.id: rule for rule in RULES}

_HIT_COUNTER: Counter[str] = Counter()


def record_hit(rule_id: str) -> None:
    _HIT_COUNTER[rule_id] += 1


def get_hit_counts() -> dict[str, int]:
    return dict(_HIT_COUNTER)


def reset_hit_counts() -> None:
    _HIT_COUNTER.clear()


def _matches(text: str, markers: tuple[str, ...]) -> bool:
    return any(m in text for m in markers)


def looks_like(text, markers: tuple[str, ...]) -> bool:
    """旧 `looks_like_*` 函数的统一实现（薄包装用）。"""
    if not text:
        return False
    return _matches(str(text), markers)


def evaluate(text: str, policy: Policy | None = None) -> tuple[str, str]:
    """按固定顺序跑全部发布前规则，返回 `(reason, 命中的规则 id)`。

    所有规则无条件执行 —— `policy.enabled` 只控制新增引导，不参与判定；
    三类 hard 规则更是在 `resolve_policy` 里就没有可关的字段。
    """
    policy = policy or Policy()
    for rule in RULES:
        if _matches(str(text or ""), rule.markers):
            if policy.hit_counter_enabled:
                record_hit(rule.id)
            return rule.reason, rule.id
    return "ok", ""


def clamp_publish_text(text: str, policy: Policy | None = None) -> str:
    """C1：发布前把文本硬截到策略字数（默认 20，上限 20 写死）。

    注意刻意**不改** `sanitize_llm_output` 的 100 字默认行为：那是净化步骤，
    stiff 检测与重写反例回灌都依赖完整文本，且既有 A16 用例断言它硬截 100 字。
    """
    policy = policy or Policy()
    if not text:
        return text
    limit = max(1, min(int(policy.max_comment_chars), MAX_COMMENT_CHARS_CEILING))
    return str(text)[:limit]


def build_guard_text(prompt: str, policy: Policy | None = None) -> str:
    """组装 prompt 末尾的引导段（C2 + C3 + 纪律段），纪律段恒在最末。

    幂等：已含「【发言纪律」就不重复追加（三条生成路径共用、重试 prompt 会二次调用）。
    """
    policy = policy or Policy()
    guidance = ""
    if policy.enabled:
        guidance = FEW_SHOT + UNDERSTANDING_CHECK
    if not prompt:
        return guidance + STYLE_GUARD
    p = str(prompt)
    if "【发言纪律" in p:
        return p
    return p + guidance + STYLE_GUARD


def build_style_retry_text(prompt: str, bad_text: str,
                           policy: Policy | None = None) -> str:
    """语域重写 prompt：把不合格输出作为反例回灌。"""
    base = build_guard_text(prompt, policy)
    return base + RETRY_HINT.format(bad=str(bad_text or "").strip()[:50])


# ══════════════════════════════════════════════════════════════════
# 四、A 段：素材决策（6 个决策点收拢到一个纯函数）
# ══════════════════════════════════════════════════════════════════


@dataclass
class MaterialDecision:
    content: str
    has_material: bool
    skip_comment: bool
    reason: str
    feed_type: str
    dropped_images: int
    #: 去空白后的正文长度，供调用方复刻 v1.6.1「正文仅N字」的诊断日志
    plain_content_chars: int = 0


def rt_looks_truncated(text: str) -> bool:
    """转发正文是否带截断标记（尾部省略号）。不假设接口一定截断，看数据本身。"""
    t = str(text or "").rstrip()
    return bool(t) and t.endswith(_RT_TRUNCATION_TAILS)


def classify_feed(feed: dict) -> str:
    """C4 动态类型分流：只识别「打卡/签到」类短动态，其余一律 `normal`。

    保守口径：命中打卡词 + 去空白后正文 ≤ 15 字 + 无转发内容 + 无有效图描述，
    才判 `checkin`。话题类短说说（如「五维介质新企划」）不受影响。
    """
    content = str(feed.get("content", "") or "")
    plain = re.sub(r"\s+", "", content)
    rt_con = str(feed.get("rt_con", "") or "").strip()
    images = [d for d in (feed.get("images") or [])
              if d and not str(d).lstrip().startswith("[图片")]
    if (plain and len(plain) <= CHECKIN_MAX_PLAIN_CHARS and not rt_con and not images
            and any(m in content for m in CHECKIN_MARKERS)):
        return "checkin"
    return "normal"


def decide_material(feed: dict, *, short_content_chars: int = 15,
                    policy: Policy | None = None) -> MaterialDecision:
    """把 `process_feeds` 里 6 个素材决策点收拢为一次纯函数判定。

    行为与 v1.6.1 逐条等价；新增的只有 C4 `checkin` 降级（受 policy.enabled 控制）。
    第 6 点「评论概率」仍由调用方按 `random.random() <= comment_probability` 判，
    本函数只产出 `has_material` 供其使用。
    """
    policy = policy or Policy()
    # ① 素材守卫：正文 / 转发内容 / 非占位符图片描述
    text_material = (str(feed.get("content", "") or "").strip()
                     or str(feed.get("rt_con", "") or "").strip())
    image_material = [d for d in (feed.get("images") or [])
                      if d and not str(d).lstrip().startswith("[图片")]
    has_material = bool(text_material or image_material)

    # ② 只把真实存在的素材拼进 prompt（占位符一律剔除）
    content = str(feed.get("content", "") or "")
    rt_con = str(feed.get("rt_con", "") or "").strip()
    if rt_con:
        # ② 转发归属：带上原作者名，否则「老大」这类称呼会失去所指（2026-11-03 事故）
        rt_author = str(feed.get("rt_author", "") or "").strip()
        if rt_author:
            content += f"（转发 {rt_author} 的说说：{rt_con}）"
        else:
            content += f"（转发别人的说说：{rt_con}）"
        # ③ 截断披露：命中才加，不命中一个字不加
        if rt_looks_truncated(rt_con):
            content += ("【转发内容在原页只显示到这里、后面被「查看全文」折叠了，"
                        "你看到的是前半段，不要当完整内容理解，也不要猜后半段】")
    for desc in image_material:
        content += f"[图: {desc}]"

    # ④ 缺图披露：静默剔除会让模型把残缺内容当完整事实推理（2026-11-01 事故）
    dropped = len(feed.get("images") or []) - len(image_material)
    if dropped > 0:
        content += (f"【本说说另有 {dropped} 张图未能识别，你看不到图里的内容；"
                    f"不要猜测、不要提及、不要评论这些图】")

    # ⑤ 短正文 + @ + 缺图 → 只点赞不评论
    skip_comment = False
    reason = ""
    raw_content = str(feed.get("content", "") or "")
    plain_len = len(re.sub(r"\s+", "", raw_content))
    mentions_someone = "@" in raw_content
    if (dropped > 0 and not image_material and mentions_someone
            and plain_len < short_content_chars):
        skip_comment = True
        reason = "short_at_no_image"

    # C4 动态类型分流（新增，受总开关控制；保守口径见 classify_feed）
    feed_type = "normal"
    if policy.enabled and not skip_comment and has_material:
        feed_type = classify_feed(feed)
        if feed_type == "checkin":
            skip_comment = True
            reason = "checkin"

    return MaterialDecision(
        content=content,
        has_material=has_material,
        skip_comment=skip_comment,
        reason=reason,
        feed_type=feed_type,
        dropped_images=dropped,
        plain_content_chars=plain_len,
    )
