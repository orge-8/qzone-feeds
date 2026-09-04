"""Cookie 管理：通过 napcat-adapter 自动获取 QQ空间 cookie + 节流 + data_dir 原子落盘。

相对上游 cookie.py 的改动：
- 只保留 adapter 方式（用户已确认，napcat-adapter 为硬依赖）
- 落盘到 ctx.paths.data_dir（不再写代码目录）
- 节流时间实例化到对象（去模块级全局）
"""

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


def set_cookie_manager_logger(custom_logger):
    global logger
    logger = custom_logger


def parse_cookie_string(cookie_str: str) -> dict:
    """将 'k=v; k2=v2' 形式的cookie字符串解析为字典。"""
    cookies = {}
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        if key not in cookies:
            cookies[key] = value
    return cookies


class CookieExpiredError(Exception):
    """cookie 失效（由 qzone_api 判定后抛出，这里也导出一份便于复用）。"""


class CookieManager:
    """adapter 取 cookie + 节流 + data_dir 落盘。"""

    def __init__(self, plugin, data_dir: str | Path):
        """
        Args:
            plugin: 插件实例（可访问 .ctx.api）
            data_dir: 数据目录（ctx.paths.data_dir）
        """
        self._plugin = plugin
        self._data_dir = Path(data_dir)
        self._cookies: dict | None = None
        self._last_refresh_time: float = 0.0
        # 节流间隔（秒），默认60分钟，由 plugin.on_load 从 config.cookie.refresh_interval_min 覆盖
        self.refresh_interval_sec: int = 60 * 60

    def _cookies_path(self) -> Path:
        return self._data_dir / "cookies.json"

    def _get_api(self):
        try:
            return self._plugin.ctx.api
        except AttributeError:
            return None

    def _config_interval(self):
        try:
            minutes = int(self._plugin.config.cookie.refresh_interval_min)
            if minutes > 0:
                self.refresh_interval_sec = minutes * 60
        except (AttributeError, TypeError, ValueError):
            pass

    def load_from_disk(self) -> dict | None:
        """启动时从 data_dir 恢复 cookie（可选，省一次 adapter 调用）。"""
        path = self._cookies_path()
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("uin") and data.get("p_skey"):
                self._cookies = data
                logger.info(f"已从 {path} 恢复 cookie（uin={str(data.get('uin', '')).lstrip('o0')}）")
                return data
        except Exception as e:
            logger.warning(f"读取本地cookie文件失败: {e}")
        return None

    async def _fetch_by_adapter(self, force: bool) -> dict | None:
        """通过 napcat-adapter 获取 cookie。节流期内且非 force 时直接用缓存。"""
        if not force and self._cookies and (time.time() - self._last_refresh_time) < self.refresh_interval_sec:
            logger.debug("cookie 节流期内，使用内存缓存")
            return self._cookies

        api = self._get_api()
        if api is None:
            logger.error("API 上下文未设置，无法调用 napcat-adapter 获取 cookie")
            return self._cookies  # 缓存兜底

        try:
            result = await api.call("adapter.napcat.account.get_cookies", params={"domain": "user.qzone.qq.com"})
        except Exception as e:
            logger.error(f"调用 napcat-adapter 获取 cookie 异常: {e}")
            return self._cookies

        if not isinstance(result, dict) or result.get("status") != "ok" or "cookies" not in result.get("data", {}):
            # 只打结构与错误消息，不打完整 result——adapter 异常时可能把 cookie 串放进 error 字段
            status = result.get("status") if isinstance(result, dict) else type(result).__name__
            err_msg = result.get("message") or result.get("error") if isinstance(result, dict) else ""
            if isinstance(err_msg, dict):
                err_msg = "（结构化错误，已省略）"
            logger.error(f"获取 cookie 失败: status={status}, message={err_msg}")
            return self._cookies

        cookie_str = result["data"]["cookies"]
        parsed = parse_cookie_string(cookie_str)
        if not parsed.get("uin") or not parsed.get("p_skey"):
            logger.error(f"cookie 缺少 uin 或 p_skey，字段: {sorted(parsed.keys())}")
            return self._cookies

        self._cookies = parsed
        self._last_refresh_time = time.time()
        self._save_to_disk(parsed)
        logger.info(f"adapter 获取 cookie 成功（uin={parsed['uin'].lstrip('o0')}）")
        return parsed

    def _save_to_disk(self, cookies: dict) -> bool:
        """原子落盘到 data_dir/cookies.json（tmp + os.replace），并收紧文件权限。"""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            path = self._cookies_path()
            tmp_path = str(path) + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
            # cookie 含 p_skey/skey 登录态，收紧为仅属主可读写（Windows 上为尽力而为）
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return True
        except Exception as e:
            logger.error(f"保存 cookie 失败: {e}")
            return False

    async def get_cookies(self, force: bool = False) -> dict | None:
        """获取可用 cookie。force=True 跳过节流强制重取（自动重登用）。

        Returns:
            dict | None: cookie字典；彻底失败返回 None
        """
        self._config_interval()
        cookies = await self._fetch_by_adapter(force)
        if cookies and cookies.get("uin") and cookies.get("p_skey"):
            return cookies
        return None

    def get_age_sec(self) -> float | None:
        """当前缓存 cookie 的年龄（秒），无缓存返回 None。"""
        if self._last_refresh_time <= 0:
            return None
        return time.time() - self._last_refresh_time
