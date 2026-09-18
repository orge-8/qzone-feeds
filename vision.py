"""读说说时的图片识别（VisionManager，自 Maizone vision.py 移植）。

把说说配图（base64）交给视觉模型生成文字描述。
vision_model 为空或调用失败时回退占位文本。
"""
import asyncio
import base64
import hashlib


# ===== logger =====
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


def set_vision_logger(custom_logger):
    global logger
    logger = custom_logger


# 占位文本（与 NoImageManager 保持一致）
PLACEHOLDER = "[图片]"
PLACEHOLDER_FAILED = "[图片（识别失败）]"
# 描述长度上限，防止 prompt 膨胀
MAX_DESC_CHARS = 200
# 单图识别超时（秒）
DESC_TIMEOUT_SEC = 60
# RPC 传输层超时：SDK call_capability 默认 30s，低于部分 VLM 慢响应场景，
# 需略小于插件总闸（wait_for DESC_TIMEOUT_SEC），保持总闸兜底。
DESC_RPC_TIMEOUT_MS = 55_000

_DESCRIBE_PROMPT = (
    "请客观描述这张图片的内容，不超过100字。"
    "包括主要人物/物体/场景/文字内容，不要评价，不要输出多余内容。"
)


def _guess_image_mime(image_base64: str) -> str:
    """按 magic bytes 探测图片 MIME（只解 base64 头部片段），失败默认 jpeg。"""
    try:
        head = base64.b64decode(image_base64[:32])
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return "image/webp"
        if head[:3] == b"GIF":
            return "image/gif"
        if head[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
    except Exception:
        pass
    return "image/jpeg"


class VisionManager:
    """基于 Host llm.generate 的图片描述生成器。

    模型名取自配置 read.vision_model；为空时退化为占位符。
    按图片 URL 做内存 LRU 缓存（read.enable_desc_cache / read.desc_cache_size），
    同一图片重复出现时不再下载与识别。缓存只驻内存，重启清空。
    """

    def __init__(self, plugin):
        self._plugin = plugin
        self._desc_cache: dict[str, str] = {}  # url -> 描述（插入序 LRU）
        # 内容哈希 -> 描述（插入序 LRU）：URL 作键的盲区——Qzone 图床 URL 每轮拉取
        # 都轮换签名 token（同一张图两轮 URL 不同），URL 缓存必然失效，导致同一批图
        # 每轮自动任务都重新 VLM（真机 09-18：同批图 31 分钟内描述了两遍）。
        # 图片字节 sha256 作二级键：URL 轮换了也没关系，下载（KB 级，便宜）后哈希
        # 命中即跳过 VLM（20s+，贵的部分）。
        self._hash_cache: dict[str, str] = {}
        # url -> 进行中的描述 Task（singleflight：同 URL 并发调用只跑一次 VLM）
        self._inflight: dict[str, asyncio.Task] = {}

    def _get_vision_params(self) -> dict:
        """llm.generate 的任务名/模型名 kwargs（兼容 MaiBot 1.2.5 语义拆分）。"""
        try:
            cfg = self._plugin.config.read
            return self._plugin.resolve_llm_params(
                getattr(cfg, "vision_task", ""),
                getattr(cfg, "vision_model", ""),
                getattr(cfg, "vision_model_name", ""),
            )
        except (AttributeError, RuntimeError):
            return {}

    def _get_enabled(self) -> bool:
        try:
            return bool(self._plugin.config.read.enable_image_description)
        except AttributeError:
            return True

    def _get_cache_enabled(self) -> bool:
        try:
            return bool(self._plugin.config.read.enable_desc_cache)
        except AttributeError:
            return True

    def _get_cache_size(self) -> int:
        try:
            return max(1, int(self._plugin.config.read.desc_cache_size or 200))
        except (AttributeError, TypeError, ValueError):
            return 200

    def is_cached(self, url: str) -> bool:
        """URL 是否已有缓存描述（命中即无需下载图片）。"""
        if not url or not self._get_cache_enabled():
            return False
        return url in self._desc_cache

    def is_available(self) -> bool:
        """视觉链路是否真正可用（开关开启 且 任务名/模型名已配置）。

        供调用方在下载图片**之前**短路：不可用时不必下载与压缩
        （真机实测过 6 张图白下载白压缩，最后只拿到占位符）。
        """
        return self._get_enabled() and bool(self._get_vision_params())

    def _cache_get(self, url: str) -> str | None:
        if not self._get_cache_enabled():
            return None
        desc = self._desc_cache.pop(url, None)
        if desc is not None:
            self._desc_cache[url] = desc  # LRU touch
        return desc

    def _cache_put(self, url: str, desc: str) -> None:
        if not url or not self._get_cache_enabled():
            return
        self._desc_cache.pop(url, None)
        self._desc_cache[url] = desc
        limit = self._get_cache_size()
        while len(self._desc_cache) > limit:
            self._desc_cache.pop(next(iter(self._desc_cache)))

    def _hash_cache_get(self, content_hash: str) -> str | None:
        if not content_hash or not self._get_cache_enabled():
            return None
        desc = self._hash_cache.pop(content_hash, None)
        if desc is not None:
            self._hash_cache[content_hash] = desc  # LRU touch
        return desc

    def _hash_cache_put(self, content_hash: str, desc: str) -> None:
        if not content_hash or not self._get_cache_enabled():
            return
        self._hash_cache.pop(content_hash, None)
        self._hash_cache[content_hash] = desc
        limit = self._get_cache_size()
        while len(self._hash_cache) > limit:
            self._hash_cache.pop(next(iter(self._hash_cache)))

    def _build_messages(self, image_base64: str) -> list[dict]:
        data_url = f"data:{_guess_image_mime(image_base64)};base64,{image_base64}"
        return [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _DESCRIBE_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]

    async def get_image_description(self, url: str, image_base64: str) -> str:
        """生成图片描述文本。任何失败都返回占位文本，不抛异常。

        url 用于缓存键；缓存命中时 image_base64 可为空串（无需下载）。
        仅真实识别成功的描述写入缓存（占位符不入缓存，避免坏结果固化）。

        singleflight 去重（v1.2.8）：同一 URL 被多个协程同时调用时（缓存均未命中），
        只有首个协程真正下载+调 VLM，其余协程 await 同一个 Task 共享结果——
        修复真机上同一张图被描述两次的问题（如 /说说 命令与自动任务撞车、
        同批 gather 里 URL 重复等场景）。
        """
        if url:
            cached = self._cache_get(url)
            if cached is not None:
                logger.info(f"图片描述命中缓存: {url[:80]}")
                return cached
        if not self._get_enabled():
            return PLACEHOLDER
        llm_kwargs = self._get_vision_params()
        if not llm_kwargs:
            # 任务名与模型名都为空：无法识别（旧行为：vision_model 留空 → 占位符）。
            # 必须打日志——否则表现为"图片都在、描述却全是[图片]"，且日志里毫无线索。
            logger.warning("视觉任务名与模型名均为空，未调用 VLM（请配置 [read] vision_task）")
            return PLACEHOLDER
        if not image_base64:
            return PLACEHOLDER_FAILED

        # singleflight：并发同 URL 只跑一次真实识别
        if url and url in self._inflight:
            logger.info(f"图片描述并发去重（等待进行中任务）: {url[:80]}")
            try:
                return await asyncio.shield(self._inflight[url])
            except asyncio.CancelledError:
                # 等待方被取消不应取消首个任务（其他等待方/结果写入仍需要它）
                return PLACEHOLDER_FAILED
            except Exception:
                # 首个任务失败：等待方同样拿到失败占位符（异常日志已由首个任务打出）
                return PLACEHOLDER_FAILED

        return await self._describe_and_cache(url, image_base64, llm_kwargs)

    async def _describe_and_cache(self, url: str, image_base64: str, llm_kwargs: dict) -> str:
        """真实执行识别并写缓存。以具名 Task 挂入 _inflight 供并发去重。

        内容哈希二级缓存：下载后的 base64 先算 sha256，命中说明同一张图
        （URL 轮换过签名）之前识别过，直接复用描述，跳过 VLM。
        """
        key = url or id(image_base64)
        task = asyncio.current_task()
        if url:
            self._inflight[url] = task  # type: ignore[assignment]
        try:
            content_hash = ""
            try:
                content_hash = hashlib.sha256(image_base64.encode("ascii")).hexdigest()
            except (ValueError, UnicodeEncodeError):
                content_hash = ""  # 非 ASCII base64（异常输入）不哈希，走正常识别
            if content_hash:
                hit = self._hash_cache_get(content_hash)
                if hit is not None:
                    logger.info(f"图片描述命中内容哈希缓存（URL 已轮换但图片相同）: {url[:80]}")
                    if url:
                        self._cache_put(url, hit)  # 顺手补上本轮 URL 键，下轮秒回
                    return hit
            desc = await self._describe_once(image_base64, llm_kwargs, key)
            if content_hash and desc and not desc.startswith("[图片"):
                self._hash_cache_put(content_hash, desc)
            return desc
        finally:
            if url and self._inflight.get(url) is task:
                self._inflight.pop(url, None)

    async def _describe_once(self, image_base64: str, llm_kwargs: dict, key) -> str:
        try:
            ctx = self._plugin.ctx
            result = await asyncio.wait_for(
                ctx.llm.generate(
                    prompt=self._build_messages(image_base64),
                    timeout_ms=DESC_RPC_TIMEOUT_MS,
                    **llm_kwargs,
                ),
                timeout=DESC_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.warning(f"图片描述生成超时（>{DESC_TIMEOUT_SEC}s）")
            return PLACEHOLDER_FAILED
        except Exception as e:
            logger.warning(f"图片描述生成失败: {e}（kwargs={llm_kwargs!r}）")
            return PLACEHOLDER_FAILED

        if not isinstance(result, dict):
            logger.warning(f"图片描述返回格式异常: {type(result)}")
            return PLACEHOLDER_FAILED
        description = str(result.get("response") or "").strip()
        if not description:
            logger.warning("图片描述返回为空")
            return PLACEHOLDER_FAILED
        if len(description) > MAX_DESC_CHARS:
            description = description[:MAX_DESC_CHARS]
        logger.info(f"图片描述生成成功（kwargs={llm_kwargs}，长度={len(description)}）")
        if key:
            self._cache_put(key, description)
        return description
