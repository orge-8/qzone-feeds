"""已处理记录存储（防重复回复/评论）。

格式：processed_list.json = { 说说tid: [已处理评论tid, ...] }
- LRU 容量裁剪（feeds 200 / 每 feed 评论 100）
- 原子落盘（tmp + os.replace）到 ctx.paths.data_dir
- 相对上游 utils.py 的改动：去模块级全局，实例化到对象
"""

import asyncio
import json
import os
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

    def __init__(self, data_dir: str | Path):
        self._data_dir = Path(data_dir)
        self._lock = asyncio.Lock()
        self._cache: dict[str, list] | None = None

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
        """标记一条说说（及可选评论）为已处理，立即原子落盘。

        该 fid 移到字典末尾（LRU touch），仍活跃的条目不会被容量裁剪淘汰。
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
            try:
                path = self._path()
                self._data_dir.mkdir(parents=True, exist_ok=True)
                tmp_path = str(path) + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(processed, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, path)
                return True
            except Exception as e:
                logger.error(f"保存已处理列表失败: {e}")
                return False

    def stats(self) -> dict:
        """当前缓存统计（/动态状态 用）。"""
        if self._cache is None:
            return {"loaded": False}
        return {"loaded": True, "feeds": len(self._cache)}
