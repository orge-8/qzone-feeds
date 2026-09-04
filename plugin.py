"""qzone-feeds 插件主入口。

- 生命周期：on_load（注入/启动）/ on_unload（drain 队列 + cancel）/ on_config_update
- 管理员鉴权：is_local_operator 放行 + admin_ids 白名单（'qq:123' / '123'）
- 6 个 @Command：动态发 / 动态发图 / 好友动态 / 说说(读+赞评) / 回复评论 / 动态状态
- 串行队列：asyncio.Queue 单 worker，自动任务也走同一队列；cookie 失效自动重登
- 任务级无总超时（网络请求自带超时）；命令侧等待结果有 queue_timeout_sec 保护
"""

import asyncio
import random
import re

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

from .auto_tasks import AutoTaskLoop, _is_in_silent_period, process_feeds, run_auto_job
from .cookie_manager import CookieManager
from .processed_store import ProcessedStore
from .qzone_api import (
    CookieExpiredError,
    QzoneAPI,
    image_to_base64,
    set_image_manager,
    set_qzoneapi_logger,
)
from .reply_manager import ReplyManager
from .vision import VisionManager, set_vision_logger


# ===== 配置模型 =====
class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "基础配置"
    __ui_order__ = 0
    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: float = Field(default=1.0, description="配置版本号（Host 版本策略必填）")
    text_model: str = Field(default="replyer", description="LLM任务名（replyer/utils等）")


class AdminConfig(PluginConfigBase):
    __ui_label__ = "管理员"
    __ui_order__ = 1
    admin_ids: list[str] = Field(default_factory=list, description="管理员QQ号列表，支持 'qq:123' 或 '123'")
    auto_read_blacklist: list[str] = Field(default_factory=list, description="自动读动态时跳过的QQ号")


class CookieConfig(PluginConfigBase):
    __ui_label__ = "Cookie"
    __ui_order__ = 2
    refresh_interval_min: int = Field(default=60, description="cookie 刷新节流间隔（分钟）")


class ReadConfig(PluginConfigBase):
    __ui_label__ = "读动态"
    __ui_order__ = 3
    default_count: int = Field(default=5, description="默认读取条数")
    max_count: int = Field(default=15, description="单次读取上限")
    enable_image_description: bool = Field(default=True, description="是否启用图片VLM描述")
    vision_model: str = Field(default="", description="视觉模型任务名，留空则显示[图片]占位符")


class PublishConfig(PluginConfigBase):
    __ui_label__ = "发动态"
    __ui_order__ = 4
    max_text_length: int = Field(default=2000, description="说说正文最大长度")


class ReplyConfig(PluginConfigBase):
    __ui_label__ = "回复评论"
    __ui_order__ = 5
    scan_count: int = Field(default=5, description="默认扫描自己最新动态条数")
    max_replies_per_run: int = Field(default=10, description="单次回复评论上限")
    reply_interval_sec: int = Field(default=3, description="两条回复之间的基础间隔秒数")
    prompt: str = Field(
        default=("你是{bot_name}，你在QQ空间自己的说说下收到了评论。"
                 "说说内容：{content}；评论者：{nickname}；评论内容：{comment_content}；评论时间：{created_time}。"
                 "请直接输出回复内容，口语化、不超过50字、不要引号和多余说明。"),
        description="回复评论的LLM提示词模板",
    )


class AutoConfig(PluginConfigBase):
    __ui_label__ = "自动任务"
    __ui_order__ = 6
    enable_auto_read: bool = Field(default=False, description="定时自动读好友动态并点赞/评论")
    enable_auto_reply: bool = Field(default=False, description="自动回复自己动态的新评论")
    interval_min: int = Field(default=30, description="自动任务循环间隔（分钟）")
    silent_hours: str = Field(default="23:00-07:30", description="静默时段 HH:MM-HH:MM，逗号分隔多段，支持跨零点")
    like_probability: float = Field(default=0.9, description="自动读每条动态后点赞的概率 0~1")
    comment_probability: float = Field(default=0.6, description="自动读每条动态后评论的概率 0~1")
    action_interval_sec: int = Field(default=3, description="逐条处理动态的基础间隔秒数")
    comment_prompt: str = Field(
        default=("好友{target_name}发了说说：{content}。"
                 "请以 bot 身份写一条自然的评论，口语化、不超过40字、只输出评论内容。"),
        description="自动评论好友动态的LLM提示词模板",
    )


class QueueConfig(PluginConfigBase):
    __ui_label__ = "队列"
    __ui_order__ = 7
    retry_on_auth_fail: int = Field(default=1, description="cookie 失效自动重登重试次数")
    queue_timeout_sec: int = Field(default=120, description="命令侧等待队列结果的超时秒数（自动任务不受限）")


class QzoneFeedsConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    cookie: CookieConfig = Field(default_factory=CookieConfig)
    read: ReadConfig = Field(default_factory=ReadConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    reply: ReplyConfig = Field(default_factory=ReplyConfig)
    auto: AutoConfig = Field(default_factory=AutoConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)


# ===== 工具 =====
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def _parse_qq_id(value) -> str:
    """'qq:123' / '123' → '123'。"""
    s = str(value or "").strip()
    if s.lower().startswith("qq:"):
        s = s[3:]
    return s


def format_feed(feed: dict, index: int | None = None) -> str:
    """渲染单条动态为紧凑文本。"""
    prefix = f"{index}." if index is not None else ""
    lines = []
    title = f"【{feed.get('target_qq', '?')}】{feed.get('created_time', '')}"
    if prefix:
        title = f"{prefix} {title}"
    lines.append(title)
    body = feed.get("content", "")
    if feed.get("rt_con"):
        body += f"\n↪ 转发: {feed['rt_con']}"
    if body:
        lines.append(body)
    images = feed.get("images") or []
    if images:
        lines.append("🖼 " + " / ".join(images))
    videos = feed.get("videos") or []
    if videos:
        lines.append(f"🎬 视频 x{len(videos)}")
    comments = feed.get("comments") or []
    for c in comments[:2]:
        lines.append(f"💬 {c.get('nickname', '?')}: {c.get('content', '')}")
    lines.append("──────")
    return "\n".join(lines)


# ===== 插件 =====
class QzoneFeedsPlugin(MaiBotPlugin):
    config_model = QzoneFeedsConfig

    def __init__(self):
        super().__init__()
        self._cookie_mgr: CookieManager | None = None
        self._store: ProcessedStore | None = None
        self._reply_mgr: ReplyManager | None = None
        self._auto_loop: AutoTaskLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._worker_task: asyncio.Task | None = None
        self._last_publish_result = "（无）"
        self._last_reply_result = "（无）"

    # ---------- 生命周期 ----------
    async def on_load(self):
        logger = self.ctx.logger
        set_qzoneapi_logger(logger)
        set_vision_logger(logger)
        from . import auto_tasks as _auto
        from . import cookie_manager as _cm
        from . import processed_store as _ps
        from . import reply_manager as _rm
        _auto.set_auto_tasks_logger(logger)
        _cm.set_cookie_manager_logger(logger)
        _ps.set_processed_store_logger(logger)
        _rm.set_reply_manager_logger(logger)

        data_dir = self._resolve_data_dir()
        self._cookie_mgr = CookieManager(self, data_dir)
        self._cookie_mgr.load_from_disk()
        self._store = ProcessedStore(data_dir)
        self._reply_mgr = ReplyManager(self, self._store)
        # 注入真实 VisionManager（上游 NoImageManager 缺陷修正）
        set_image_manager(VisionManager(self))

        # 启动串行队列 worker
        self._queue = asyncio.Queue(maxsize=10)
        self._worker_task = asyncio.create_task(self._worker())

        # 启动自动任务
        self._auto_loop = AutoTaskLoop(self, self._enqueue_job)
        await self._auto_loop.start()

        # 探活：获取一次 cookie
        cookies = await self._cookie_mgr.get_cookies()
        if cookies:
            logger.info(f"qzone-feeds 已加载，cookie 可用（uin={str(cookies.get('uin', '')).lstrip('o0')}）")
        else:
            logger.error("qzone-feeds 已加载，但 cookie 获取失败，请检查 napcat-adapter 连接")

    async def on_unload(self):
        if self._auto_loop:
            await self._auto_loop.stop()
        # 先 drain 队列中尚未执行的 job，回填失败结果（防止等待方永久挂起）
        if self._queue is not None:
            while True:
                try:
                    job = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                cb = job.get("callback")
                if cb:
                    try:
                        await cb({"ok": False, "msg": "插件正在卸载，任务未执行"})
                    except Exception:
                        pass
                self._queue.task_done()
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._worker_task = None
        self._queue = None

    async def on_config_update(self, scope: str, config_data: dict, version: str):
        del config_data, version
        # 自动任务开关变化：重启循环
        if scope in ("plugin", "auto", "") and self._auto_loop:
            await self._auto_loop.stop()
            self._auto_loop = AutoTaskLoop(self, self._enqueue_job)
            await self._auto_loop.start()

    def _resolve_data_dir(self) -> str:
        try:
            d = self.ctx.paths.data_dir
            if d:
                return str(d)
        except AttributeError:
            pass
        # SDK < 2.6 回退：插件目录下 data/
        import os
        d = os.path.join(str(getattr(self, "plugin_dir", ".")), "data")
        os.makedirs(d, exist_ok=True)
        return d

    # ---------- 鉴权 ----------
    def _is_admin(self, kwargs) -> bool:
        """is_local_operator 直接过；否则比对 admin_ids（兼容 'qq:123' / '123'）。"""
        if kwargs.get("is_local_operator"):
            return True
        user_id = _parse_qq_id(kwargs.get("user_id", ""))
        if not user_id:
            return False
        try:
            admin_ids = self.config.admin.admin_ids or []
        except (AttributeError, RuntimeError):
            return False
        for aid in admin_ids:
            if _parse_qq_id(aid) == user_id:
                return True
        return False

    async def _deny(self, stream_id) -> tuple:
        await self.ctx.send.text("该命令仅管理员可用", stream_id)
        return False, "权限不足", 1

    # ---------- 串行队列 ----------
    async def _enqueue_job(self, job_name: str, run_fn, wait_timeout: float | None = None) -> dict:
        """投入队列并等待完成。

        wait_timeout=None（自动任务）：无限等待，长任务不会被误杀；
        wait_timeout=数值（命令侧）：等待结果超时则返回错误，不再永久挂起。
        任务级不设总超时——所有网络请求自带超时，长任务由条数自然延长。
        """
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        async def callback(result: dict):
            if not fut.done():
                fut.set_result(result)

        try:
            await asyncio.wait_for(
                self._queue.put({"name": job_name, "run": run_fn, "callback": callback}),
                timeout=10,
            )
        except asyncio.TimeoutError:
            return {"ok": False, "msg": "队列已满（10个任务），请稍后再试"}

        if wait_timeout is None:
            return await fut
        try:
            return await asyncio.wait_for(fut, timeout=wait_timeout)
        except asyncio.TimeoutError:
            # 超时后 worker 稍后回填时 fut 已 done，callback 的 done 检查保证不炸
            return {"ok": False, "msg": f"等待任务结果超时（>{int(wait_timeout)}s），任务仍在后台执行"}

    async def _worker(self):
        while True:
            job = await self._queue.get()
            result: dict
            interrupted = False
            try:
                result = await self._execute_job(job)
            except asyncio.CancelledError:
                # on_unload 中断：先回填挂起的 future，让等待方不永久挂起
                result = {"ok": False, "msg": "插件正在卸载，任务已中断"}
                interrupted = True
            except Exception as e:
                result = {"ok": False, "msg": f"执行失败: {e}"}
            try:
                cb = job.get("callback")
                if cb:
                    await cb(result)
            except Exception as e:
                self.ctx.logger.error(f"任务回调失败: {e}")
            self._queue.task_done()
            if interrupted:
                raise asyncio.CancelledError()

    async def _execute_job(self, job) -> dict:
        retry = int(self.config.queue.retry_on_auth_fail or 0)
        last_err = None
        for attempt in range(retry + 1):
            cookies = await self._cookie_mgr.get_cookies(force=(attempt > 0))
            if not cookies:
                return {"ok": False, "msg": "cookie 获取失败，请检查 napcat-adapter"}
            api = QzoneAPI(cookies)
            try:
                if job["name"] == "auto_job":
                    return await job["run"](self, api, self._store, self._reply_mgr)
                return await job["run"](api)
            except CookieExpiredError as e:
                last_err = e
                self.ctx.logger.warning(f"登录态失效（第{attempt + 1}次），强制刷新 cookie 重试")
                continue
        return {"ok": False, "msg": f"登录态失效且重登后仍失败: {last_err}"}

    async def _run_command_job(self, stream_id: str, run_fn):
        """命令侧入口：入队 → 回发结果到 stream_id。等待结果有超时保护。"""
        try:
            wait_timeout = float(self.config.queue.queue_timeout_sec or 120)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            wait_timeout = 120.0
        try:
            result = await self._enqueue_job("command", run_fn, wait_timeout=wait_timeout)
        except Exception as e:
            result = {"ok": False, "msg": f"任务提交失败: {e}"}
        await self.ctx.send.text(str(result.get("msg", "")), stream_id)
        return result

    # ---------- 命令 ----------
    @Command("qzone_publish", pattern=r"^\s*[/／]\s*动态发\s+(?P<text>.+)$")
    async def cmd_publish(self, **kwargs):
        stream_id = kwargs["stream_id"]
        if not self._is_admin(kwargs):
            return await self._deny(stream_id)
        text = kwargs.get("matched_groups", {}).get("text", "").strip()
        max_len = int(self.config.publish.max_text_length or 2000)
        if len(text) > max_len:
            await self.ctx.send.text(f"正文超长（{len(text)}/{max_len}）", stream_id)
            return False, "正文超长", 1

        async def run(api: QzoneAPI) -> dict:
            tid = await api.publish_emotion(text)
            if tid:
                self._last_publish_result = f"发布成功 tid={tid}"
                return {"ok": True, "msg": f"说说已发布（tid={tid}）"}
            self._last_publish_result = "发布失败"
            return {"ok": False, "msg": "说说发布失败，详见日志"}

        await self._run_command_job(stream_id, run)
        return True, "发说说", 1

    @Command("qzone_publish_image", pattern=r"^\s*[/／]\s*动态发图\s+(?P<text>.+?)\s*\|\s*(?P<image>\S+)\s*$")
    async def cmd_publish_image(self, **kwargs):
        stream_id = kwargs["stream_id"]
        if not self._is_admin(kwargs):
            return await self._deny(stream_id)
        matched = kwargs.get("matched_groups", {})
        text = matched.get("text", "").strip()
        image_url = matched.get("image", "").strip()
        if not text:
            await self.ctx.send.text("正文不能为空，格式：/动态发图 正文 | 图片URL", stream_id)
            return False, "正文为空", 1
        if not _URL_RE.match(image_url):
            await self.ctx.send.text("图片地址必须是 http(s) URL，格式：/动态发图 正文 | 图片URL", stream_id)
            return False, "图片URL不合法", 1

        async def run(api: QzoneAPI) -> dict:
            # 用户提供的任意 URL：不带 Qzone cookie 出站（防凭据外发）
            img_b64 = await api.get_image_base64_by_url(image_url, with_cookies=False)
            if not img_b64:
                return {"ok": False, "msg": "图片下载失败，请检查 URL"}
            import base64 as _b64
            image_bytes = _b64.b64decode(img_b64)
            tid = await api.publish_emotion(text, [image_bytes])
            if tid:
                self._last_publish_result = f"发布成功 tid={tid}"
                if getattr(api, "last_image_upload_failed", False):
                    # 图片全部上传失败时协议层静默降级为纯文本，如实回报
                    return {"ok": True, "msg": f"图片上传失败，已发布纯文本说说（tid={tid}）"}
                return {"ok": True, "msg": f"带图说说已发布（tid={tid}）"}
            self._last_publish_result = "发布失败"
            return {"ok": False, "msg": "带图说说发布失败，详见日志"}

        await self._run_command_job(stream_id, run)
        return True, "发带图说说", 1

    @Command("qzone_feeds", pattern=r"^\s*[/／]\s*好友动态(?:\s+(?P<count>\d{1,2}))?\s*$")
    async def cmd_friend_feeds(self, **kwargs):
        stream_id = kwargs["stream_id"]
        if not self._is_admin(kwargs):
            return await self._deny(stream_id)
        count = self._clip_count(kwargs.get("matched_groups", {}).get("count"))

        async def run(api: QzoneAPI) -> dict:
            feeds = await api.get_qzone_list(describe_images=self._vision_enabled())
            if not feeds:
                return {"ok": False, "msg": "好友动态获取为空"}
            if isinstance(feeds[0], dict) and feeds[0].get("error"):
                return {"ok": False, "msg": str(feeds[0]["error"])}
            blocks = [format_feed(f, i + 1) for i, f in enumerate(feeds[:count])]
            return {"ok": True, "msg": "\n".join(blocks)}

        await self._run_command_job(stream_id, run)
        return True, "读好友动态", 1

    @Command("qzone_msglist", pattern=r"^\s*[/／]\s*说说\s+(?P<qq>\d{5,12})(?:\s+(?P<count>\d{1,2}))?\s*$")
    async def cmd_msglist(self, **kwargs):
        """读指定 QQ 的说说列表（纯读，不点赞不评论）。互动用 /说说互动。"""
        stream_id = kwargs["stream_id"]
        if not self._is_admin(kwargs):
            return await self._deny(stream_id)
        qq = kwargs.get("matched_groups", {}).get("qq", "")
        count = self._clip_count(kwargs.get("matched_groups", {}).get("count"))

        async def run(api: QzoneAPI) -> dict:
            feeds = await api.get_list(qq, count, filter=True, describe_images=self._vision_enabled())
            if not feeds:
                return {"ok": False, "msg": f"QQ {qq} 的说说获取为空"}
            if isinstance(feeds[0], dict) and feeds[0].get("error"):
                return {"ok": False, "msg": str(feeds[0]["error"])}
            blocks = [format_feed(f, i + 1) for i, f in enumerate(feeds)]
            return {"ok": True, "msg": "\n".join(blocks)}

        await self._run_command_job(stream_id, run)
        return True, "读说说", 1

    @Command("qzone_msglist_interact", pattern=r"^\s*[/／]\s*说说互动\s+(?P<qq>\d{5,12})(?:\s+(?P<count>\d{1,2}))?\s*$")
    async def cmd_msglist_interact(self, **kwargs):
        """读指定 QQ 的说说列表并点赞评论（概率固定 1.0：显式指令即显式意图）。"""
        stream_id = kwargs["stream_id"]
        if not self._is_admin(kwargs):
            return await self._deny(stream_id)
        qq = kwargs.get("matched_groups", {}).get("qq", "")
        count = self._clip_count(kwargs.get("matched_groups", {}).get("count"))

        async def run(api: QzoneAPI) -> dict:
            feeds = await api.get_list(qq, count, filter=True, describe_images=self._vision_enabled())
            if not feeds:
                return {"ok": False, "msg": f"QQ {qq} 的说说获取为空"}
            if isinstance(feeds[0], dict) and feeds[0].get("error"):
                return {"ok": False, "msg": str(feeds[0]["error"])}
            blocks = [format_feed(f, i + 1) for i, f in enumerate(feeds)]
            # 点赞+评论（未处理过的条目逐条处理，去重走 processed_list）
            stats = await process_feeds(
                self, api, self._store, feeds,
                like_probability=1.0, comment_probability=1.0,
                action_interval=float(self.config.auto.action_interval_sec or 3),
                comment_prompt_tpl=str(self.config.auto.comment_prompt or ""),
            )
            blocks.append(
                f"已点赞 {stats['liked']} 条 / 评论 {stats['commented']} 条"
                f"（本轮实际处理 {stats['handled']}，其余为已处理或已跳过）")
            return {"ok": True, "msg": "\n".join(blocks)}

        await self._run_command_job(stream_id, run)
        return True, "读说说并点赞评论", 1

    @Command("qzone_reply_comments", pattern=r"^\s*[/／]\s*回复评论(?:\s+(?P<count>\d{1,2}))?\s*$")
    async def cmd_reply_comments(self, **kwargs):
        stream_id = kwargs["stream_id"]
        if not self._is_admin(kwargs):
            return await self._deny(stream_id)
        count = self._clip_count(kwargs.get("matched_groups", {}).get("count"))
        reply_mgr = self._reply_mgr
        store = self._store

        async def run(api: QzoneAPI) -> dict:
            ok, msg = await reply_mgr.reply_new_comments(api, scan_count=count)
            self._last_reply_result = msg
            return {"ok": ok, "msg": msg}

        del store
        await self._run_command_job(stream_id, run)
        return True, "回复评论", 1

    @Command("qzone_status", pattern=r"^\s*[/／]\s*动态状态\s*$")
    async def cmd_status(self, **kwargs):
        stream_id = kwargs["stream_id"]
        if not self._is_admin(kwargs):
            return await self._deny(stream_id)

        cookie_age = self._cookie_mgr.get_age_sec() if self._cookie_mgr else None
        cookie_desc = f"{cookie_age / 60:.0f} 分钟前刷新" if cookie_age is not None else "未刷新过（用缓存/未获取）"
        auto_cfg = self.config.auto
        auto_desc = []
        if auto_cfg.enable_auto_read:
            auto_desc.append(f"自动读动态(每{auto_cfg.interval_min}分钟,静默{auto_cfg.silent_hours})")
        if auto_cfg.enable_auto_reply:
            auto_desc.append("自动回评")
        queue_depth = self._queue.qsize() if self._queue is not None else -1
        msg = (
            f"队列深度: {queue_depth}\n"
            f"Cookie: {cookie_desc}\n"
            f"自动任务: {'；'.join(auto_desc) if auto_desc else '全部关闭'}\n"
            f"最近发布: {self._last_publish_result}\n"
            f"最近回评: {self._last_reply_result}"
        )
        if _is_in_silent_period(str(auto_cfg.silent_hours or "")):
            msg += "\n（当前处于静默时段）"
        await self.ctx.send.text(msg, stream_id)
        return True, "动态状态", 1

    # ---------- 辅助 ----------
    def _clip_count(self, raw) -> int:
        """条数参数裁剪到 [1, max_count]，空则用默认。"""
        try:
            cfg = self.config.read
            default = int(cfg.default_count or 5)
            max_count = int(cfg.max_count or 15)
        except AttributeError:
            default, max_count = 5, 15
        if raw is None:
            return default
        try:
            return max(1, min(int(raw), max_count))
        except (TypeError, ValueError):
            return default

    def _vision_enabled(self) -> bool:
        try:
            return bool(self.config.read.enable_image_description)
        except AttributeError:
            return True


def create_plugin() -> QzoneFeedsPlugin:
    return QzoneFeedsPlugin()
