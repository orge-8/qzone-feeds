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
                    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    try:
                        prompt = prompt_tpl.format(
                            bot_name=bot_name,
                            content=content,
                            nickname=comment.get("nickname", "好友"),
                            comment_content=comment.get("content", ""),
                            created_time=comment.get("created_time", "") or current_time,
                        )
                    except (KeyError, IndexError):
                        # 模板占位符不匹配时回退默认模板
                        prompt = (
                            f"你在QQ空间自己的说说「{content}」下收到评论。"
                            f"评论者：{comment.get('nickname', '好友')}；评论内容：{comment.get('content', '')}。"
                            "请直接输出回复内容，口语化、不超过50字、不要引号和多余说明。"
                        )
                    logger.info(f"正在回复 {comment.get('nickname')} 的评论: {comment.get('content', '')[:30]}")
                    reply_message = sanitize_llm_output(await _llm_generate(self._plugin, prompt))
                    if not reply_message:
                        # 空回复也标记已处理，避免下一轮对同一评论无限重试
                        logger.warning("LLM 回复内容为空，标记已处理并跳过")
                        await self._store.mark_processed(fid, comment["comment_tid"])
                        continue
                    if looks_like_refusal(reply_message):
                        # 拒答文本绝不能发布（会变成 bot 公开教训好友）
                        logger.warning(f"LLM 返回拒答内容，已跳过回复: {reply_message[:50]}")
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
