"""已处理记录存储（防重复回复/评论）。

格式：processed_list.json = { 说说tid: [已处理评论tid, ...] }
- LRU 容量裁剪（feeds 200 / 每 feed 评论 100）
- 原子落盘（tmp + os.replace）到 ctx.paths.data_dir
- 相对上游 utils.py 的改动：去模块级全局，实例化到对象
"""

import asyncio
import json
import os
import time
from pathlib import Path


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


def set_processed_store_logger(custom_logger):
    global logger
    logger = custom_logger


class ProcessedStore:
    MAX_FEEDS = 200
    MAX_COMMENTS_PER_FEED = 100
    # 防抖落盘间隔（秒）：mark 只改内存置 dirty，距上次落盘超过该间隔才真正写盘，
    # 否则由 call_later 调度延迟落盘（防最后一批丢）。job 边界由插件 worker 调 flush() 兜底。
    _FLUSH_INTERVAL_SEC = 2.0

    def __init__(self, data_dir: str | Path):
        self._data_dir = Path(data_dir)
        self._lock = asyncio.Lock()
        self._cache: dict[str, list] | None = None
        self._dirty = False
        self._last_flush_time: float = 0.0
        self._flush_handle: asyncio.TimerHandle | None = None

    def _path(self) -> Path:
        return self._data_dir / "processed_list.json"

    async def _load(self) -> dict[str, list]:
        if self._cache is not None:
            return self._cache
        async with self._lock:
            if self._cache is None:
                path = self._path()
                if path.exists():
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        self._cache = data if isinstance(data, dict) else {}
                    except Exception as e:
                        logger.error(f"加载已处理列表失败: {e}")
                        self._cache = {}
                else:
                    logger.info("未找到已处理列表，创建新列表")
                    self._cache = {}
        return self._cache

    async def is_processed(self, fid: str, comment_tid=None) -> bool:
        """fid 或 (fid, comment_tid) 是否已处理过。comment_tid 统一按 str 比较。"""
        processed = await self._load()
        if comment_tid is None:
            return fid in processed
        return str(comment_tid) in processed.get(fid, [])

    async def mark_processed(self, fid: str, comment_tid=None) -> bool:
        """标记一条说说（及可选评论）为已处理，防抖落盘（批量修改只触发少量写盘）。

        该 fid 移到字典末尾（LRU touch），仍活跃的条目不会被容量裁剪淘汰。
        落盘策略：距上次落盘超过 _FLUSH_INTERVAL_SEC 立即写；否则调度延迟写。
        返回值表示内存标记是否成功（落盘异步，失败记日志）。
        """
        processed = await self._load()
        async with self._lock:
            comments = processed.pop(fid, [])
            if comment_tid is not None:
                comment_tid_str = str(comment_tid)
                if comment_tid_str not in comments:
                    comments.append(comment_tid_str)
                    if len(comments) > self.MAX_COMMENTS_PER_FEED:
                        comments = comments[-self.MAX_COMMENTS_PER_FEED:]
            processed[fid] = comments
            while len(processed) > self.MAX_FEEDS:
                processed.pop(next(iter(processed)))
            self._dirty = True
            self._schedule_flush_locked()
            return True

    def _schedule_flush_locked(self) -> None:
        """锁内调用：到点立即落盘，否则用 call_later 防抖（需运行中的事件循环）。"""
        now = time.monotonic()
        if now - self._last_flush_time >= self._FLUSH_INTERVAL_SEC:
            self._write_to_disk()
            return
        if self._flush_handle is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # 无事件循环时跳过延迟调度，由 flush() 兜底
            delay = self._FLUSH_INTERVAL_SEC - (now - self._last_flush_time)
            self._flush_handle = loop.call_later(delay, self._write_to_disk)

    def _write_to_disk(self) -> None:
        """原子落盘（tmp + os.replace）。同步执行，写失败只记日志。"""
        if not self._dirty or self._cache is None:
            return
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        try:
            path = self._path()
            self._data_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = str(path) + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
            self._dirty = False
            self._last_flush_time = time.monotonic()
        except Exception as e:
            logger.error(f"保存已处理列表失败: {e}")

    async def flush(self) -> None:
        """显式落盘（dirty 才写）。队列 worker 在 job 边界调用。"""
        async with self._lock:
            self._write_to_disk()

    def stats(self) -> dict:
        """当前缓存统计（/动态状态 用）。"""
        if self._cache is None:
            return {"loaded": False}
        return {"loaded": True, "feeds": len(self._cache)}
