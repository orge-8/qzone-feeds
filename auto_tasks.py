"""自动任务：定时读好友动态并点赞/评论 + 自动回复自己动态的新评论。

- AutoTaskLoop：定时循环（interval_min 分钟）+ 静默时段检查 + 复用插件串行队列
- run_auto_job：队列 worker 中执行的实际任务（读动态→概率评论→概率点赞→自动回评）
- 静默时段解析自 Maizone tasks.py 原样移植（支持跨零点、多段）
"""

import asyncio
import datetime
import random

from .reply_manager import _llm_generate, sanitize_llm_output


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


def set_auto_tasks_logger(custom_logger):
    global logger
    logger = custom_logger


def _parse_time_to_minutes(time_str: str) -> int | None:
    """'HH:MM' → 分钟数；解析失败返回 None。"""
    try:
        if ":" not in time_str:
            return None
        hour_str, minute_str = time_str.split(":", 1)
        hour = int(hour_str.strip())
        minute = int(minute_str.strip())
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
        return None
    except (ValueError, AttributeError):
        return None


def _is_in_silent_period(silent_hours_config: str) -> bool:
    """检查当前时间是否在静默时段内。格式 'HH:MM-HH:MM'，逗号分隔多段，支持跨零点。"""
    if not silent_hours_config or not str(silent_hours_config).strip():
        return False
    try:
        now = datetime.datetime.now()
        current_time = now.hour * 60 + now.minute
        for period in str(silent_hours_config).split(","):
            period = period.strip()
            if not period or "-" not in period:
                continue
            start_str, end_str = period.split("-", 1)
            start_time = _parse_time_to_minutes(start_str.strip())
            end_time = _parse_time_to_minutes(end_str.strip())
            if start_time is None or end_time is None:
                continue
            if start_time <= end_time:
                # 不跨天，如 12:00-14:00
                if start_time <= current_time <= end_time:
                    return True
            else:
                # 跨天，如 23:00-07:00
                if current_time >= start_time or current_time <= end_time:
                    return True
        return False
    except Exception as e:
        logger.error(f"解析静默时段配置失败: {e}")
        return False


_DEFAULT_COMMENT_PROMPT = (
    "好友{target_name}发了说说：{content}。"
    "请以 bot 身份写一条自然的评论，口语化、不超过40字、只输出评论内容。"
)


async def process_feeds(
    plugin,
    api,
    store,
    feeds_list: list,
    like_probability: float = 1.0,
    comment_probability: float = 1.0,
    blacklist: set | None = None,
    action_interval: float = 3.0,
    comment_prompt_tpl: str | None = None,
) -> dict:
    """一批说说的点赞+评论核心流程。

    /说说 命令（概率 1.0，显式指令即显式意图）与自动任务（配置概率）共用。
    去重走 store，逐条间隔 action_interval + 随机，处理完无论成败立即标记。

    Returns:
        {"handled": 实际处理条数, "liked": 点赞数, "commented": 评论数}
    """
    blacklist = blacklist or set()
    if not comment_prompt_tpl:
        comment_prompt_tpl = _DEFAULT_COMMENT_PROMPT
    stats = {"handled": 0, "liked": 0, "commented": 0}
    for feed in feeds_list:
        target_qq = str(feed.get("target_qq", ""))
        fid = str(feed.get("tid", ""))
        if not fid:
            continue
        if target_qq in blacklist:
            logger.info(f"跳过黑名单QQ {target_qq} 的说说")
            continue
        if await store.is_processed(fid):
            await store.mark_processed(fid)  # touch 保持 LRU 活跃
            continue
        stats["handled"] += 1
        await asyncio.sleep(action_interval + random.random())
        content = feed.get("content", "")
        if feed.get("rt_con"):
            content += f"（转发: {feed['rt_con']}）"
        for img in (feed.get("images") or []):
            content += f"[图: {img}]"
        try:
            if random.random() <= comment_probability:
                prompt = comment_prompt_tpl.format(target_name=target_qq, content=content)
                comment_text = sanitize_llm_output(await _llm_generate(plugin, prompt))
                if comment_text:
                    ok = await api.comment(fid, target_qq, comment_text)
                    if ok:
                        stats["commented"] += 1
                        logger.info(f"评论 {fid} 成功: {comment_text[:30]}")
                    else:
                        logger.error(f"评论 {fid} 失败")
            if random.random() <= like_probability:
                ok = await api.like(fid, target_qq)
                if ok:
                    stats["liked"] += 1
                    logger.info(f"点赞 {fid} 成功")
                else:
                    logger.error(f"点赞 {fid} 失败")
        except Exception as e:
            logger.error(f"处理说说 {fid} 出错: {e}")
        # 无论成败立即标记，避免重复处理
        await store.mark_processed(fid)
    return stats


async def run_auto_job(plugin, api, store, reply_manager) -> dict:
    """自动任务主体（在队列 worker 中执行）：读好友动态+点赞/评论 + 回复新评论。

    Returns:
        {"ok": bool, "summary": str}
    """
    cfg = plugin.config.auto
    summary_parts = []

    # ===== 1. 自动读好友动态并点赞/评论 =====
    if bool(getattr(cfg, "enable_auto_read", False)):
        try:
            feeds_list = await api.get_qzone_list(describe_images=False)
            if (isinstance(feeds_list, list) and feeds_list
                    and isinstance(feeds_list[0], dict) and feeds_list[0].get("error")):
                summary_parts.append(f"读好友动态失败: {feeds_list[0]['error']}")
            else:
                blacklist = set()
                try:
                    blacklist = {str(x) for x in plugin.config.admin.auto_read_blacklist or []}
                except AttributeError:
                    pass
                stats = await process_feeds(
                    plugin, api, store, feeds_list,
                    like_probability=float(getattr(cfg, "like_probability", 0.9)),
                    comment_probability=float(getattr(cfg, "comment_probability", 0.6)),
                    blacklist=blacklist,
                    action_interval=float(getattr(cfg, "action_interval_sec", 3)),
                    comment_prompt_tpl=str(getattr(cfg, "comment_prompt", "") or _DEFAULT_COMMENT_PROMPT),
                )
                summary_parts.append(
                    f"处理{stats['handled']}条好友动态（拉取{len(feeds_list)}，赞{stats['liked']}/评{stats['commented']}）")
        except Exception as e:
            logger.error(f"自动读好友动态异常: {e}")
            summary_parts.append(f"自动读好友动态异常: {e}")

    # ===== 2. 自动回复自己动态的新评论 =====
    if bool(getattr(cfg, "enable_auto_reply", False)):
        try:
            ok, msg = await reply_manager.reply_new_comments(api)
            summary_parts.append(("回评: " if ok else "回评失败: ") + msg)
        except Exception as e:
            logger.error(f"自动回评异常: {e}")
            summary_parts.append(f"自动回评异常: {e}")

    return {"ok": True, "summary": "；".join(summary_parts) if summary_parts else "无启用的自动任务"}


class AutoTaskLoop:
    """定时循环：interval_min 分钟 → 静默时段检查 → 生成 job 投入插件串行队列。"""

    def __init__(self, plugin, enqueue_fn):
        """
        Args:
            plugin: 插件实例
            enqueue_fn: async fn(job_name, run_fn) —— 投入插件串行队列的回调
        """
        self._plugin = plugin
        self._enqueue = enqueue_fn
        self.is_running = False
        self.task: asyncio.Task | None = None

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        self.task = asyncio.create_task(self._loop())
        logger.info("自动任务循环已启动")

    async def stop(self):
        if not self.is_running:
            return
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("自动任务循环已停止")

    def _interval_min(self) -> int:
        try:
            v = int(self._plugin.config.auto.interval_min)
            return max(v, 1)
        except (AttributeError, TypeError, ValueError):
            return 30

    def _silent_hours(self) -> str:
        try:
            return str(self._plugin.config.auto.silent_hours or "")
        except AttributeError:
            return ""

    def _should_run(self) -> bool:
        try:
            cfg = self._plugin.config.auto
            return bool(getattr(cfg, "enable_auto_read", False) or getattr(cfg, "enable_auto_reply", False))
        except AttributeError:
            return False

    async def _loop(self):
        while self.is_running:
            try:
                await asyncio.sleep(self._interval_min() * 60)
                if not self._should_run():
                    continue
                if _is_in_silent_period(self._silent_hours()):
                    logger.info("当前处于静默时段，跳过本轮自动任务")
                    continue
                from .auto_tasks import run_auto_job  # 延迟引用避免循环导入
                await self._enqueue("auto_job", run_auto_job)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"自动任务循环出错: {e}")
                await asyncio.sleep(300)
