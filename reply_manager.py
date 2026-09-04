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


def sanitize_llm_output(text, max_chars: int = _MAX_PUBLISH_CHARS) -> str:
    """LLM 输出发布前净化：剥引号包裹、去 markdown 装饰、硬截断。

    防护目标：好友可在动态/评论内容里注入指令（间接 prompt injection），
    净化保证最终发布到 QQ 空间的文本短、纯、无装饰。
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
    """调用 Host LLM，返回文本；失败返回空串。带总超时防 Host 端挂住卡死队列。"""
    try:
        model = ""
        try:
            model = plugin.config.plugin.text_model
        except AttributeError:
            pass
        result = await asyncio.wait_for(
            plugin.ctx.llm.generate(prompt, model=model),
            timeout=60,
        )
        return str(result.get("response") or "").strip()
    except asyncio.TimeoutError:
        logger.error("LLM 生成超时（>60s）")
        return ""
    except Exception as e:
        logger.error(f"LLM 生成失败: {e}")
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
