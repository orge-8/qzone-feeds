"""回复自己动态下的新评论：扫描 → 去重过滤 → LLM 生成回复 → 调 reply API。

相对上游 utils.reply_feed() 的改动：
- 去掉 PersonInfo db 依赖（评论自带 nickname）
- 落盘改 ProcessedStore（data_dir），去模块级全局
- LLM prompt 简化（可配置模板）
- describe_images=False 读自己的动态（不下载图，省时省流量）
"""

import asyncio
import datetime
import random
import re
import time

from .comment_style import (append_style_guard, build_style_retry_prompt,
                            looks_like_skip, looks_like_stiff_register)


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


def set_reply_manager_logger(custom_logger):
    global logger
    logger = custom_logger


# 发布到 QQ 空间的 LLM 输出长度上限（防 prompt injection 诱导长文/刷屏）
_MAX_PUBLISH_CHARS = 100
# markdown 装饰符（LLM 输出有时带 **bold**、`code` 等，说说里是乱码）
_MARKDOWN_RE = re.compile(r"[*_`#>~]+")

# LLM 拒答/元回复特征词。
# 这类文本是模型在对**我们**说话（拒绝执行创作要求），不是给好友的评论；
# 一旦发布出去就变成"bot 公开教训好友"。真机事故：
#   好友动态「一年之后，阿哈将成为路边一坨」→ bot 评论
#   「你的描述中存在不文明且不恰当的表述……因此我不能按照你的要求进行创作」
# 只要命中就判为拒答，直接跳过评论（fail-closed）。
_REFUSAL_MARKERS = (
    "按照你的要求",
    "不能按照",
    "无法按照",
    "不符合健康",
    "不文明",
    "不恰当的表述",
    "不当的表述",
    "健康的交流规范",
    "健康的交流准则",
    "交流准则",
    "友善的语言",
    "请使用文明",
    "我不能提供",
    "无法提供",
    "我无法完成",
    "我不能完成",
    "作为人工智能",
    "作为AI",
    "作为一个AI",
    "作为语言模型",
    "作为大模型",
    "换个话题",
    "抱歉，我不能",
    "抱歉,我不能",
    # 真机变体（2026-09-15 事故）："不良引导和危险暗示，不符合健康的交流准则……
    # 共同营造良好的网络环境" —— 与旧样本同源不同词，逐个收录
    "不良引导",
    "危险暗示",
    "良好的网络环境",
    "网络交流",
)


def looks_like_refusal(text: str) -> bool:
    """文本是否为 LLM 的拒答/元回复（而非对好友说的话）。

    宁可漏判也不误判？不——这里反过来：拒答文本发布出去是**公开事故**，
    误判（把正常评论当拒答而少发一条）只是少一次互动。
    因此采用较宽的特征命中策略。
    """
    if not text:
        return False
    t = str(text)
    return any(m in t for m in _REFUSAL_MARKERS)


# 身份错位特征（v1.2.9 事故）：自动评论是发在**好友**的动态下，好友不是 bot 的主人。
# 真机事故（2026-09-26）：好友动态「第一次做bot」→ bot 评论
#   「嘿嘿，谁让你把我调教得这么厉害的，认输吧主人[得意]」
# —— LLM 顺着动态原文入戏，把发动态的好友当成了自己的主人。
# 发布出去等于 bot 在好友空间公开认错了爹，属公开事故；与拒答同哲学 fail-closed。
_IDENTITY_MARKERS = (
    "主人",
    "主人酱",
    "我的主人",
    "master",
    "Master",
)


def looks_like_identity_confusion(text: str) -> bool:
    """评论是否把好友误认成了主人/创造了 bot 的人（身份错位）。

    自动评论场景下，评论对象永远是 bot 主人的**好友**——对好友称「主人」
    无论上下文多顺都是身份错位。命中即跳过评论（fail-closed）：
    误拦只是少一条评论，错发是「bot 认错主人」的公开事故。
    注意：只拦「主人」类称呼，不拦「老板」「大佬」等常规调侃称呼。
    """
    if not text:
        return False
    t = str(text)
    return any(m in t for m in _IDENTITY_MARKERS)


def format_comment_time(raw) -> str:
    """把评论文本时间格式化为可读时间，供 prompt 注入。

    两条取评论路径的 created_time 格式不同：
    - JSON 路径（get_list，回评用）：原始 createTime，多为 unix 时间戳串
    - HTML 路径（get_qzone_list）：span.state 文本，形如 "3小时前"/"昨天 10:23"

    时间戳 → "YYYY-MM-DD HH:MM:SS"（本地时区，与真实谈天时间对齐）；
    已是可读文本 → 原样返回；缺失/无法解析 → "未知"。
    绝不返回空串：prompt 里出现"评论时间：；"会让 LLM 以为时间信息被吞了。
    """
    if raw is None:
        return "未知"
    s = str(raw).strip()
    if not s:
        return "未知"
    if s.isdigit():
        try:
            num = int(s)
            if num > 10_000_000_000:  # 13 位毫秒时间戳 → 秒
                num //= 1000
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(num))
        except (ValueError, OverflowError, OSError):
            return s
    return s


def sanitize_llm_output(text, max_chars: int = _MAX_PUBLISH_CHARS) -> str:
    """LLM 输出发布前净化：剥引号包裹、去 markdown 装饰、硬截断。

    防护目标：好友可在动态/评论内容里注入指令（间接 prompt injection），
    净化保证最终发布到 QQ 空间的文本短、纯、无装饰。
    注意：本函数**不判断**拒答，调用方需另用 looks_like_refusal() 拦截。
    """
    if not text:
        return ""
    t = str(text).strip()
    for _ in range(2):
        t = t.strip().strip("\"'""''`")
    t = _MARKDOWN_RE.sub("", t).strip()
    if len(t) > max_chars:
        t = t[:max_chars]
    return t


async def _llm_generate(plugin, prompt: str) -> str:
    """调用 Host LLM，返回文本；失败返回空串。带总超时防 Host 端挂住卡死队列。

    任务名/模型名经 plugin.resolve_llm_params 解析（兼容 MaiBot 1.2.5 的语义拆分），
    失败日志同时打出 kwargs——只有 Host 错误文本时分不清名字是配置填的还是 SDK 默认值。

    超时分层（2026-09-19 修复 replyer E_TIMEOUT）：
    - RPC 传输层：SDK call_capability 的 timeout_ms 具名参数（SDK 自行消费，
      不会混入业务 args），默认 30s——低于 thinking 模型（replyer 任务
      deepseek-v4-pro-think，Host slow_threshold=120s）的正常耗时下限，
      导致思考稍久就被 RPC 层误杀。这里抬到 120s 对齐 Host slow_threshold。
    - 插件总闸：wait_for 130s > RPC 120s，保持总闸兜底略高于传输层的防御性设计。
    """
    kwargs: dict = {}
    try:
        try:
            cfg = plugin.config.plugin
            kwargs = plugin.resolve_llm_params(
                getattr(cfg, "text_task", ""),
                getattr(cfg, "text_model", ""),
                getattr(cfg, "text_model_name", ""),
            )
        except (AttributeError, RuntimeError):
            kwargs = {}
        result = await asyncio.wait_for(
            plugin.ctx.llm.generate(prompt, timeout_ms=120_000, **kwargs),
            timeout=130,
        )
        return str(result.get("response") or "").strip()
    except asyncio.TimeoutError:
        logger.error("LLM 生成超时（>130s）")
        return ""
    except Exception as e:
        logger.error(f"LLM 生成失败: {e}（kwargs={kwargs!r}）")
        return ""


async def _one_comment_attempt(plugin, prompt: str) -> tuple[str, str]:
    """单次生成 + 全部发布前检查。

    Returns:
        (text, reason)——text 非空表示"可发布"且已净化；
        text 为空串 + reason 表示"不要发布"，reason 供日志诊断：
        empty / llm_skip / refusal / identity / stiff
    """
    raw = await _llm_generate(plugin, append_style_guard(prompt))
    if not raw:
        return "", "empty"
    # 弃评标记在净化**之前**判：净化会剥引号，万一模型写成「"[跳过]"」也能认出来
    if looks_like_skip(raw):
        return "", "llm_skip"
    text = sanitize_llm_output(raw)
    if not text:
        return "", "empty"
    if looks_like_refusal(text):
        # 拒答文本绝不能发布（会变成 bot 公开教训好友）
        return "", "refusal"
    if looks_like_identity_confusion(text):
        # 身份错位文本绝不能发布——评论对象是好友不是主人（2026-09-26 事故）
        return "", "identity"
    if looks_like_stiff_register(text):
        # 书面通稿腔（2026-10-03 事故）。带文本返回，供调用方重写时当反例
        return text, "stiff"
    return text, "ok"


async def generate_guarded_comment(plugin, prompt: str) -> tuple[str, str]:
    """生成一条**可发布**的评论/回复文本，带语域纪律与 fail-closed 拦截。

    三条生成路径（自动评论 / 回评自己说说的评论 / 被@回复）共用本函数，
    保证纪律一致——只在某一条路径上修，等于没修。

    处理顺序（每一步都不可省）：
    1. 追加代码级发言纪律（见 comment_style 模块头，绕开用户模板）；
    2. 生成 → 弃评标记 / 净化 / 拒答 / 身份错位 / 语域 逐项检查；
    3. 语域不合格时给**一次**重写机会（把不合格句当反例回灌）；
    4. 重写仍不合格 → 放弃发布（宁可少一条评论，不发通稿腔）。

    Returns:
        (text, reason)：text 为空串表示"不要发布"；非空即可直接调发布 API。
    """
    text, reason = await _one_comment_attempt(plugin, prompt)
    if reason != "stiff":
        if reason == "llm_skip":
            logger.info("LLM 主动弃评（读不懂/接不上），本条不评论")
        return text, reason

    logger.warning(f"评论语域不合格（书面通稿腔），尝试重写: {text[:40]}")
    text2, reason2 = await _one_comment_attempt(
        plugin, build_style_retry_prompt(prompt, text))
    if reason2 == "stiff":
        logger.warning(f"重写后仍是书面通稿腔，放弃发布: {text2[:40]}")
        return "", "stiff_dropped"
    if reason2 == "ok":
        logger.info(f"重写后合格: {text2}")
    return text2, reason2


class ReplyManager:
    def __init__(self, plugin, store):
        """
        Args:
            plugin: 插件实例（.ctx / .config）
            store: ProcessedStore 实例
        """
        self._plugin = plugin
        self._store = store

    def _cfg(self, name: str, default):
        try:
            return getattr(self._plugin.config.reply, name)
        except AttributeError:
            return default

    async def reply_new_comments(self, api, scan_count: int | None = None) -> tuple[bool, str]:
        """扫描自己最新动态的新评论并逐条回复。

        Args:
            api: QzoneAPI 实例（队列 worker 已备好 cookie）
            scan_count: 扫描自己最新动态条数，None 用配置默认

        Returns:
            (success, summary)
        """
        if scan_count is None:
            scan_count = int(self._cfg("scan_count", 5) or 5)
        max_replies = int(self._cfg("max_replies_per_run", 10) or 10)
        interval_base = float(self._cfg("reply_interval_sec", 3) or 3)
        prompt_tpl = str(self._cfg(
            "prompt",
            ("你是{bot_name}，你在QQ空间自己的说说下收到了评论。"
             "说说内容：{content}；评论者：{nickname}；评论内容：{comment_content}；评论时间：{created_time}。"
             "请直接输出回复内容，口语化、不超过50字、不要引号和多余说明。"),
        ))

        my_uin = api.uin
        # 读自己的动态：filter=False（不做"已评论跳过"过滤）、describe_images=False（不下载图）
        feeds_list = await api.get_list(my_uin, scan_count, filter=False, describe_images=False)
        if not feeds_list:
            return False, "获取自己的说说列表为空"
        if isinstance(feeds_list[0], dict) and feeds_list[0].get("error"):
            return False, str(feeds_list[0]["error"])

        bot_name = "我"
        try:
            if api.qq_nickname:
                bot_name = api.qq_nickname
        except AttributeError:
            pass

        reply_count = 0
        checked_feeds = 0
        for feed in feeds_list:
            fid = feed["tid"]
            target_qq = feed["target_qq"]
            # touch：自己的说说保持 LRU 活跃，防止评论记录被裁剪淘汰后重复回复
            await self._store.mark_processed(fid)
            checked_feeds += 1

            # 过滤出需要回复的评论
            list_to_reply = []
            for comment in (feed.get("comments") or []):
                comment_qq = str(comment.get("qq_account", "") or "").strip()
                comment_tid = comment.get("comment_tid")
                if not comment_qq.isdigit() or not comment_tid:
                    # 缺少评论者QQ或评论ID时无法定位评论，跳过
                    continue
                if comment_qq == str(my_uin):
                    # 只考虑不是自己的评论
                    continue
                if await self._store.is_processed(fid, comment_tid):
                    # 只考虑未处理过的评论
                    continue
                list_to_reply.append(comment)
                if reply_count + len(list_to_reply) >= max_replies:
                    break
            if reply_count >= max_replies:
                break
            if not list_to_reply:
                continue

            content = feed.get("content", "")
            for comment in list_to_reply:
                if reply_count >= max_replies:
                    break
                await asyncio.sleep(interval_base + random.random())
                comment_qq = str(comment.get("qq_account", ""))
                try:
                    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    comment_time_str = format_comment_time(comment.get("created_time"))
                    try:
                        prompt = prompt_tpl.format(
                            bot_name=bot_name,
                            content=content,
                            nickname=comment.get("nickname", "好友"),
                            comment_content=comment.get("content", ""),
                            # created_time：该评论的发布时间（已格式化）
                            created_time=comment_time_str,
                            # current_time / now_time：当前时间，供 LLM 判断"这条评论是多久前发的"
                            current_time=now_str,
                            now_time=now_str,
                        )
                        # 旧版模板可能不含时间占位符（config.toml 已落盘不会随插件升级更新），
                        # 缺什么补什么——否则升级后老配置拿不到时间上下文
                        missing = []
                        if "{created_time}" not in prompt_tpl:
                            missing.append(f"评论时间：{comment_time_str}")
                        if "{current_time}" not in prompt_tpl and "{now_time}" not in prompt_tpl:
                            missing.append(f"当前时间：{now_str}")
                        if missing:
                            prompt += ("（" + "；".join(missing)
                                       + "。请留意评论时间与当前时间的间隔，"
                                         "别把几天前的评论当成刚发的。）")
                    except (KeyError, IndexError):
                        # 模板占位符不匹配时回退默认模板（也带时间上下文）
                        prompt = (
                            f"你在QQ空间自己的说说「{content}」下收到评论。"
                            f"评论者：{comment.get('nickname', '好友')}；评论内容：{comment.get('content', '')}；"
                            f"评论时间：{comment_time_str}；当前时间：{now_str}。"
                            "请直接输出回复内容，口语化、不超过50字、不要引号和多余说明。"
                            "留意评论时间与当前时间的间隔，别把几天前的评论当成刚发的。"
                        )
                    logger.info(f"正在回复 {comment.get('nickname')} 的评论: {comment.get('content', '')[:30]}")
                    # 生成+全部发布前检查（纪律/净化/拒答/身份/语域）一次过，
                    # 与自动评论、被@回复共用同一套判定，避免三处判定漂移
                    reply_message, reason = await generate_guarded_comment(self._plugin, prompt)
                    if not reply_message:
                        # 空回复/被拦也标记已处理，避免下一轮对同一评论无限重试
                        logger.warning(f"回复为空或被拦截（{reason}），标记已处理并跳过")
                        await self._store.mark_processed(fid, comment["comment_tid"])
                        continue
                    result = await api.reply(
                        fid,
                        target_qq,
                        comment.get("nickname", "好友"),
                        comment_qq,
                        reply_message,
                        comment["comment_tid"],
                    )
                    if result:
                        logger.info(f"回复成功: {reply_message}")
                        reply_count += 1
                    else:
                        logger.error(f"回复 {comment.get('nickname')} 的评论失败")
                except Exception as e:
                    logger.error(f"回复评论 {comment.get('comment_tid')} 时出错: {e}")
                # 无论成功与否立即标记，避免下一轮重复回复同一条评论
                await self._store.mark_processed(fid, comment["comment_tid"])

        return True, f"检查{checked_feeds}条动态，回复{reply_count}条评论"
