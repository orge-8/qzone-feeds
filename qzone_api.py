"""QQ空间协议层（自 Maizone qzone_api.py 移植，含 5 处修正）。

修正清单（相对上游）：
1. 上传响应解析 eval() → json.loads（切片 {...}）
2. 图片下载带 cookies（相册图床 403 规避）
3. cookies/processed_list 不落盘在代码目录（本模块只管协议，数据走 ctx.paths.data_dir）
4. logger 改为可选注入，默认静默
5. 去掉 create_qzone_api() 的本地文件依赖，由 cookie_manager 构造 QzoneAPI
"""

import asyncio
import base64
import json
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx
import json5
import bs4

from .image_compress import compress_image_bytes


# ===== logger（可选注入）=====
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


def set_qzoneapi_logger(custom_logger):
    global logger
    logger = custom_logger


# ===== 图片识别管理器（由 plugin 注入 VisionManager）=====
class NoImageManager:
    async def get_image_description(self, url: str, image_base64: str) -> str:
        return "[图片]"

    def is_cached(self, url: str) -> bool:
        return False


image_manager: Any = NoImageManager()


def set_image_manager(manager):
    """设置图片识别器实例（VisionManager）。"""
    global image_manager
    image_manager = manager


# ===== 辅助函数 =====
def generate_gtk(skey: str) -> str:
    """特定协议算法，生成QQ空间的gtk值"""
    hash_val = 5381
    for i in range(len(skey)):
        hash_val += (hash_val << 5) + ord(skey[i])
    return str(hash_val & 2147483647)


def get_picbo_and_richval(upload_result) -> tuple[str | None, str | None]:
    """从上传结果中提取图片的picbo和richval值用于发表图片说说"""
    if not isinstance(upload_result, dict) or "ret" not in upload_result:
        logger.error("获取图片picbo和richval失败: 返回数据不合法")
        return None, None
    if upload_result.get("ret") != 0:
        logger.error(f"上传图片失败: {upload_result}")
        return None, None

    try:
        picbo = upload_result["data"]["url"].split("&bo=")[1]
        richval = ",{},{},{},{},{},{},,{},{}".format(
            upload_result["data"]["albumid"],
            upload_result["data"]["lloc"],
            upload_result["data"]["sloc"],
            upload_result["data"]["type"],
            upload_result["data"]["height"],
            upload_result["data"]["width"],
            upload_result["data"]["height"],
            upload_result["data"]["width"],
        )
        return picbo, richval
    except (KeyError, IndexError) as e:
        logger.error(f"提取picbo和richval失败: {e}")
        return None, None


def extract_code_html(html_content: str) -> Any | None:
    """从QQ空间响应的HTML内容中提取响应码code的值（frameElement.callback 剥壳）"""
    try:
        soup = bs4.BeautifulSoup(html_content, "html.parser")
        script_tags = soup.find_all("script")
        for script in script_tags:
            if script.string and "frameElement.callback" in script.string:
                script_content = script.string
                start_index = script_content.find("frameElement.callback(") + len("frameElement.callback(")
                end_index = script_content.rfind(");")
                if 0 < start_index < end_index:
                    json_str = script_content[start_index:end_index].strip()
                    if json_str.endswith(";"):
                        json_str = json_str[:-1]
                    # 兼容外层再包一层 _Callback(...) 时残留的右括号
                    for candidate in (json_str, json_str.rstrip(")")):
                        try:
                            data = json5.loads(candidate)
                            break
                        except Exception:
                            data = None
                    if isinstance(data, dict) and "code" in data:
                        return data.get("code")
                    else:
                        continue
        return None
    except Exception:
        return None


def extract_code_json(json_response) -> Any | None:
    """从QQ空间响应的json内容中提取code值，如果不存在则返回None"""
    try:
        if isinstance(json_response, str):
            data = json.loads(json_response)
        else:
            data = json_response
        return data.get("code", None)
    except (json.JSONDecodeError, KeyError, AttributeError):
        return None


def image_to_base64(image: bytes) -> str:
    """将图片转换为base64字符串"""
    pic_base64 = base64.b64encode(image)
    return str(pic_base64)[2:-1]


class CookieExpiredError(Exception):
    """cookie 失效（登录态丢失），由调用方捕获后强制刷新 cookie 重试。"""


# 图片下载上限：防恶意大文件/超大图撑爆内存（base64 展开再放大 1.33 倍）
_MAX_IMAGE_BYTES = 10 * 1024 * 1024

# 允许携带 Qzone cookie 下载图片的域名后缀（防凭据外发到第三方主机）
_QZONE_IMAGE_HOST_SUFFIXES = (".qzone.qq.com", ".qzonestyle.gtimg.cn", ".gtimg.cn", ".qq.com")
# 图片下载最多跟随几次重定向（每跳都要重新校验域名白名单）
_MAX_REDIRECT_HOPS = 3
_REDIRECT_STATUS = (301, 302, 303, 307, 308)


def _is_allowed_image_host(url: str) -> bool:
    """图片 URL 是否属于 QQ 图床域名。

    图片 URL 来自远端返回的 HTML/JSON，不可信；携带 cookie 请求前必须校验，
    否则一条恶意 <img src> 就能把 p_skey 送到攻击者主机。
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return bool(host) and host.endswith(_QZONE_IMAGE_HOST_SUFFIXES)


# 登录类错误码：出现即认为登录态失效
_LOGIN_ERROR_CODES = {1000000, 1000001, 1000002, 1000003, -3000, -3001, -14}


class QzoneAPI:
    # QQ空间url常量
    UPLOAD_IMAGE_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
    EMOTION_PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
    DOLIKE_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
    COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    LIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
    ZONE_LIST_URL = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"

    def __init__(self, cookies_dict: dict | None = None):
        self.cookies = cookies_dict or {}
        self.uin = self.cookies.get("uin", "").lstrip("o0")  # uin 从cookies中提取，去除前导o和0
        self.qq_nickname = ""
        self.gtk2 = ""
        self.last_image_upload_failed = False
        # 共享 httpx 客户端（lazy 创建，aclose() 关闭）：避免每请求重建 TCP+TLS
        self._client: httpx.AsyncClient | None = None
        if "p_skey" in self.cookies:
            self.gtk2 = generate_gtk(self.cookies["p_skey"])

    def _get_client(self) -> httpx.AsyncClient:
        """获取共享 AsyncClient（lazy 创建）。各请求用 per-request timeout 覆盖。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(follow_redirects=True)
        return self._client

    async def aclose(self) -> None:
        """关闭共享客户端（job 结束时由队列 worker 调用）。"""
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception:
                pass
        self._client = None

    def _check_login_code(self, code) -> None:
        """登录类错误码 → 抛 CookieExpiredError 交给上层重登。"""
        try:
            if code is not None and int(code) in _LOGIN_ERROR_CODES:
                raise CookieExpiredError(f"登录态失效（code={code}）")
        except (TypeError, ValueError):
            pass

    async def _download_image_bytes(self, url: str, with_cookies: bool = True) -> bytes | None:
        """下载图片返回 bytes（流式 + 大小上限）。

        with_cookies=True（默认）：Qzone 相册图床需要登录态，带 cookies 防 403。
        with_cookies=False：用户提供的任意 URL（如 /动态发图 的图片地址）——
        绝不携带 Qzone cookie 出站，防止凭据外发到第三方服务器。
        """
        if not url:
            return None
        # 携带 cookie 出站前先校验域名：图片 URL 来自远端，不可信
        if with_cookies and not _is_allowed_image_host(url):
            logger.warning(f"图片域名不在图床白名单，已拒绝携带 cookie 下载: {url}")
            return None
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Referer": "https://qzone.qq.com/",
        }
        cookies = self.cookies if with_cookies else None
        try:
            client = self._get_client()
            # 流式下载 + 大小上限：Content-Length 预检 + 累计截断
            # 手动跟随重定向（follow_redirects=False）：每一跳都重新校验域名白名单，
            # 否则一条 302 就能把 cookie 带出白名单之外
            current = url
            for _hop in range(_MAX_REDIRECT_HOPS + 1):
                async with client.stream("GET", current, headers=headers, cookies=cookies,
                                         timeout=15, follow_redirects=False) as response:
                    if response.status_code in _REDIRECT_STATUS:
                        loc = response.headers.get("location")
                        if not loc:
                            logger.warning(f"重定向缺少 Location 头: {current}")
                            return None
                        nxt = str(httpx.URL(current).join(loc))
                        if with_cookies and not _is_allowed_image_host(nxt):
                            logger.warning(f"重定向目标不在图床白名单，中止下载（防凭据外发）: {nxt}")
                            return None
                        current = nxt
                        continue
                    if response.status_code != 200:
                        logger.error(f"请求失败: {url} 状态码: {response.status_code}")
                        return None
                    clen = response.headers.get("content-length")
                    try:
                        if clen and int(clen) > _MAX_IMAGE_BYTES:
                            logger.warning(f"图片超过大小上限（{int(clen)} bytes），拒绝下载: {url}")
                            return None
                    except ValueError:
                        pass
                    chunks = []
                    total = 0
                    async for chunk in response.aiter_bytes(65536):
                        total += len(chunk)
                        if total > _MAX_IMAGE_BYTES:
                            logger.warning(f"图片超过大小上限（>{_MAX_IMAGE_BYTES} bytes），中止下载: {url}")
                            return None
                        chunks.append(chunk)
                    return b"".join(chunks)
            logger.warning(f"图片重定向次数超限（>{_MAX_REDIRECT_HOPS}），放弃下载: {url}")
            return None
        except httpx.RequestError as e:
            logger.warning(f"图片请求异常: {url} - {e}")
            return None
        except Exception as e:
            logger.warning(f"图片下载异常: {url} - {e}")
            return None

    async def get_image_base64_by_url(self, url: str, with_cookies: bool = True) -> str | None:
        """从指定URL获取图片并转base64（/动态发图 命令用；内部走 _download_image_bytes）。"""
        content = await self._download_image_bytes(url, with_cookies=with_cookies)
        if content is None:
            return None
        return base64.b64encode(content).decode("utf-8")

    async def upload_image(self, image: bytes) -> dict | None:
        """上传图片到QQ空间。响应切片 {...} 后 json.loads（弃用上游 eval）。"""
        client = self._get_client()
        res = await client.request(
            method="POST",
            url=self.UPLOAD_IMAGE_URL,
            timeout=60,
            data={
                    "filename": "filename",
                    "zzpanelkey": "",
                    "uploadtype": "1",
                    "albumtype": "7",
                    "exttype": "0",
                    "skey": self.cookies.get("skey", ""),
                    "zzpaneluin": self.uin,
                    "p_uin": self.uin,
                    "uin": self.uin,
                    "p_skey": self.cookies.get("p_skey", ""),
                    "output_type": "json",
                    "qzonetoken": "",
                    "refer": "shuoshuo",
                    "charset": "utf-8",
                    "output_charset": "utf-8",
                    "upload_hd": "1",
                    "hd_width": "2048",
                    "hd_height": "10000",
                    "hd_quality": "96",
                    "backUrls": "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,"
                                "http://119.147.64.75/cgi-bin/upload/cgi_upload_image",
                    "url": "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image?g_tk=" + self.gtk2,
                    "base64": "1",
                    "picfile": image_to_base64(image),
                },
                headers={
                    "referer": "https://user.qzone.qq.com/" + str(self.uin),
                    "origin": "https://user.qzone.qq.com",
                },
                cookies=self.cookies,
            )
        if res.status_code == 200:
            try:
                # 响应可能带前后噪声，切片出 {...} 再解析（弃用上游 eval）
                return json.loads(res.text[res.text.find("{"): res.text.rfind("}") + 1])
            except Exception as e:
                logger.error(f"解析上传响应失败: {e}")
                return None
        else:
            logger.error(f"上传图片失败: 状态码 {res.status_code}")
            return None

    async def publish_emotion(self, content: str, images: list[bytes] | None = None) -> str | None:
        """发表说说。成功返回tid。

        副作用：调用后可读 self.last_image_upload_failed——传了图但全部上传失败
        （此时会静默降级为纯文本发布），调用方据此向用户如实回报。
        """
        if images is None:
            images = []
        self.last_image_upload_failed = False

        post_data = {
            "syn_tweet_verson": "1",  # 官方拼写错误，勿改
            "paramstr": "1",
            "who": "1",
            "con": content,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": "1",
            "to_sign": "0",
            "hostuin": self.uin,
            "code_version": "1",
            "format": "json",
            "qzreferrer": "https://user.qzone.qq.com/" + str(self.uin),
        }

        if len(images) > 0:
            pic_bos = []
            richvals = []
            for img in images:
                upload_result = await self.upload_image(img)
                if upload_result:
                    picbo, richval = get_picbo_and_richval(upload_result)
                    if picbo and richval:
                        pic_bos.append(picbo)
                        richvals.append(richval)

            if pic_bos:
                post_data["pic_bo"] = ",".join(pic_bos)
                post_data["richtype"] = "1"
                post_data["richval"] = "\t".join(richvals)  # richval 用 TAB 连接
            else:
                # 传了图但全部上传失败：QQ空间协议只能降级纯文本，标记给调用方
                self.last_image_upload_failed = True

        client = self._get_client()
        res = await client.request(
            method="POST",
            url=self.EMOTION_PUBLISH_URL,
            timeout=10,
            params={"g_tk": self.gtk2, "uin": self.uin},
            data=post_data,
            headers={
                "referer": "https://user.qzone.qq.com/" + str(self.uin),
                "origin": "https://user.qzone.qq.com",
            },
            cookies=self.cookies,
        )
        if res.status_code == 200:
            code = extract_code_json(res.text)
            self._check_login_code(code)
            if code != 0:
                logger.error(f"发表说说失败，响应内容: {res.text[:500]}")
                return None
            try:
                return res.json().get("tid")
            except Exception as e:
                logger.error(f"解析发表结果失败: {e}")
                return None
        else:
            logger.error(f"发表说说失败: 状态码 {res.status_code} 内容: {res.text[:300]}")
            return None

    async def like(self, fid: str, target_qq: str) -> bool:
        """点赞指定说说。"""
        uin = self.uin
        post_data = {
            "qzreferrer": f"https://user.qzone.qq.com/{uin}",
            "opuin": uin,
            "unikey": f"http://user.qzone.qq.com/{target_qq}/mood/{fid}",
            "curkey": f"http://user.qzone.qq.com/{target_qq}/mood/{fid}",
            "appid": 311,
            "from": 1,
            "typeid": 0,
            "abstime": int(time.time()),
            "fid": fid,
            "active": 0,
            "format": "json",
            "fupdate": 1,
        }
        client = self._get_client()
        res = await client.request(
            method="POST",
            url=self.DOLIKE_URL,
            timeout=10,
            params={"g_tk": self.gtk2},
            data=post_data,
            headers={
                "referer": "https://user.qzone.qq.com/" + str(self.uin),
                "origin": "https://user.qzone.qq.com",
            },
            cookies=self.cookies,
        )
        if res.status_code == 200:
            code = extract_code_json(res.text)
            self._check_login_code(code)
            if code != 0:
                logger.error("点赞失败" + res.text[:300])
                return False
            return True
        else:
            logger.error("点赞失败: " + res.text[:300])
            return False

    async def comment(self, fid: str, target_qq: str, content: str) -> bool:
        """评论指定说说。"""
        uin = self.uin
        post_data = {
            "topicId": f"{target_qq}_{fid}__1",
            "uin": uin,
            "hostUin": target_qq,
            "feedsType": 100,
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "plat": "qzone",
            "source": "ic",
            "platformid": 52,
            "format": "fs",
            "ref": "feeds",
            "content": content,
        }
        client = self._get_client()
        res = await client.request(
            method="POST",
            url=self.COMMENT_URL,
            timeout=10,
            params={"g_tk": self.gtk2},
            data=post_data,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                "referer": "https://user.qzone.qq.com/" + str(self.uin),
                "origin": "https://user.qzone.qq.com",
            },
            cookies=self.cookies,
        )
        if res.status_code == 200:
            code = extract_code_html(res.text)
            self._check_login_code(code)
            if code != 0:
                logger.error("评论失败" + res.text[:300])
                return False
            return True
        else:
            logger.error("评论失败: " + res.text[:300])
            return False

    async def reply(
        self,
        fid: str,
        target_qq: str,
        target_nickname: str,
        target_comment_qq: str,
        content: str,
        comment_tid: str,
    ) -> bool:
        """回复指定评论。必须带 commentId + commentUin 才能定位真实评论。"""
        uin = self.uin
        if not target_comment_qq or not comment_tid:
            logger.error("回复失败：缺少评论者QQ或评论ID，无法定位真实评论")
            return False

        target_comment_qq = str(target_comment_qq)
        comment_tid = str(comment_tid)
        # 昵称来自远端数据，去掉富文本 token 定界符防止 @ 格式注入（F-007）
        safe_nickname = str(target_nickname).replace("{", "(").replace("}", ")")
        post_data = {
            "topicId": f"{target_qq}_{fid}__1",
            "uin": uin,
            "hostUin": target_qq,
            "feedsType": 100,
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "content": f"@{{uin:{target_comment_qq},nick:{safe_nickname},auto:1}}{content}",
            "format": "fs",
            "plat": "qzone",
            "source": "ic",
            "platformid": 52,
            "ref": "feeds",
            "commentId": comment_tid,
            "commentUin": target_comment_qq,
            "richtype": "",
            "richval": "",
            "paramstr": "1",
        }
        client = self._get_client()
        res = await client.request(
            method="POST",
            url=self.COMMENT_URL,
            timeout=10,
            params={"g_tk": self.gtk2},
            data=post_data,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
                "referer": "https://user.qzone.qq.com/" + str(self.uin),
                "origin": "https://user.qzone.qq.com",
            },
            cookies=self.cookies,
        )
        if res.status_code == 200:
            code = extract_code_html(res.text)
            self._check_login_code(code)
            if code != 0:
                logger.error("回复失败" + res.text[:300])
                return False
            return True
        else:
            logger.error(f"回复失败，错误码: {res.status_code}")
            return False

    async def _describe_images(self, urls: list[str], max_images: int, concurrency: int,
                               compress: bool = True, max_edge: int = 1024, quality: int = 80) -> list[str]:
        """并发下载+描述图片，保序返回（VLM 单张常需 20s+，串行会导致命令超时）。

        compress=True 时送 VLM 前压缩（长边 ≤max_edge + JPEG 质量 quality），
        降低 token 消耗与耗时；压缩失败（缺 Pillow/解码失败）回退原图。
        图片 bytes 内部直传，只在送 VLM 边界做一次 base64 编码（省一次全量编解码）。
        """
        urls = [u for u in urls if u][:max_images]
        if not urls:
            return []
        sem = asyncio.Semaphore(max(1, concurrency))

        async def describe(url: str):
            async with sem:
                try:
                    # 缓存命中时无需下载（由 VisionManager 按 URL 判缓存）
                    if image_manager.is_cached(url):
                        return await image_manager.get_image_description(url, "")
                    raw = await self._download_image_bytes(url, with_cookies=True)
                    if not raw:
                        logger.warning(f"获取图片失败: {url}")
                        return "[图片（加载失败）]"
                    payload = raw
                    if compress:
                        out = await asyncio.to_thread(compress_image_bytes, raw, max_edge, quality)
                        if out:
                            payload = out
                            if len(out) < len(raw):
                                logger.info(
                                    f"图片压缩 {len(raw) / 1024:.0f}KB → {len(out) / 1024:.0f}KB"
                                    f"（长边≤{max_edge} 质量{quality}，省 {100 - len(out) * 100 // len(raw)}%）")
                        else:
                            logger.warning(
                                f"图片压缩失败，回退原图 {len(raw) / 1024:.0f}KB"
                                f"（缺 Pillow 或解码失败，检查 manifest 依赖 pillow）")
                    return await image_manager.get_image_description(
                        url, base64.b64encode(payload).decode("utf-8"))
                except Exception as e:
                    logger.warning(f"获取图片描述失败: {e}")
                    return "[图片（识别失败）]"

        results = await asyncio.gather(*[describe(u) for u in urls], return_exceptions=True)
        # 保序补占位：异常项（含 CancelledError 等 BaseException）不能静默丢弃，
        # 否则结果条数变少、后续图片编号整体前移错位
        return [r if isinstance(r, str) else "[图片（识别失败）]" for r in results]

    async def get_list(self, target_qq: str, num: int, filter: bool = True, describe_images: bool = True,
                       max_images: int = 9, image_concurrency: int = 3,
                       compress: bool = True, max_edge: int = 1024, quality: int = 80) -> list[dict[str, Any]]:
        """获取指定QQ号的说说列表（jsonp 剥壳 _preloadCallback(...)）。

        评论在 msg["commentlist"]（name/content/uin/tid/createTime，楼中楼 list_3[]）。
        describe_images=False 时跳过图片下载与VLM描述（reply_manager 回评时用，省时省流量）。
        max_images / image_concurrency：限制单条动态图片数与并发度，防 VLM 慢导致命令超时。
        compress / max_edge / quality：送 VLM 前压缩图片（省 token），失败回退原图。
        """
        logger.info(f"即将获取 {target_qq} 的说说列表...num={num} filter={filter}")
        client = self._get_client()
        res = await client.request(
            method="GET",
            url=self.LIST_URL,
            timeout=10,
            params={
                "g_tk": self.gtk2,
                "uin": target_qq,
                "ftype": 0,
                "sort": 0,
                "pos": 0,
                "num": num,
                "replynum": 100,
                "callback": "_preloadCallback",
                "code_version": 1,
                "format": "jsonp",
                "need_comment": 1,
                "need_private_comment": 1,
            },
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Referer": f"https://user.qzone.qq.com/{target_qq}",
                "Host": "user.qzone.qq.com",
                "Connection": "keep-alive",
            },
            cookies=self.cookies,
        )

        if res.status_code != 200:
            logger.error("访问失败: " + str(res.status_code))
            return []

        data = res.text
        if data.startswith("_preloadCallback(") and data.endswith(");"):
            json_str = data[len("_preloadCallback("):-2]
        else:
            json_str = data

        try:
            json_data = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.error(f"解析msglist响应失败: {e}")
            return [{"error": "解析说说列表响应失败"}]

        try:
            uin_nickname = json_data.get("logininfo").get("name")
            self.qq_nickname = uin_nickname
        except AttributeError:
            uin_nickname = ""

        if json_data.get("code") != 0:
            code = json_data.get("code")
            self._check_login_code(code)
            return [{"error": json_data.get("message") or f"code={code}"}]

        feeds_list = []
        msglist = json_data.get("msglist") or []
        if not msglist:
            logger.warning("msglist为空或None")

        for msg in msglist:
            # filter=True 时跳过自己已评论过的说说（读别人空间用；读自己的空间 target_qq==uin 不过滤）
            is_comment = False
            if filter and target_qq != str(self.uin):
                commentlist = msg.get("commentlist")
                if isinstance(commentlist, list):
                    for comment in commentlist:
                        if comment.get("name") == uin_nickname:
                            is_comment = True
                            break
            if is_comment:
                continue

            timestamp = msg.get("created_time", "")
            if timestamp:
                try:
                    time_tuple = time.localtime(int(timestamp))
                    created_time = time.strftime("%Y-%m-%d %H:%M:%S", time_tuple)
                except (TypeError, ValueError, OverflowError, OSError):
                    # 非法时间戳（字符串/异常大值）兜底，不炸整个 get_list
                    created_time = str(timestamp)
            else:
                created_time = str(msg.get("createTime", "unknown"))
            tid = str(msg.get("tid", ""))
            content = msg.get("content", "")

            # 图片与视频封面 → VLM 描述（并发，保序）
            images = []
            if describe_images:
                urls = []
                for pic in (msg.get("pic") or []):
                    # VLM 只需小图：smallurl 优先（下载字节数比 url1 大图低 5~10 倍）
                    urls.append(pic.get("smallurl") or pic.get("url1") or pic.get("pic_id"))
                for video in (msg.get("video") or []):
                    urls.append(video.get("url1") or video.get("pic_url"))
                images = await self._describe_images(urls, max_images, image_concurrency,
                                                     compress=compress, max_edge=max_edge, quality=quality)

            # 视频播放地址
            videos = []
            for video in (msg.get("video") or []):
                url = video.get("url3")
                if url:
                    videos.append(url)

            # 转发内容
            rt_con = ""
            rt_data = msg.get("rt_con") or {}
            if isinstance(rt_data, dict):
                rt_con = rt_data.get("content", "")

            # 评论（含楼中楼）
            def _safe_int(value):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None

            comments = []
            for comment in (msg.get("commentlist") or []):
                comment_tid_value = _safe_int(comment.get("tid"))
                for sub_comment in (comment.get("list_3") or []):
                    comments.append({
                        "content": sub_comment.get("content", ""),
                        "qq_account": str(sub_comment.get("uin", "")),
                        "nickname": sub_comment.get("name", ""),
                        "comment_tid": _safe_int(sub_comment.get("tid")),
                        "created_time": sub_comment.get("createTime", "") or comment.get("createTime2", ""),
                        "parent_tid": comment_tid_value,
                    })
                comments.append({
                    "content": comment.get("content", ""),
                    "qq_account": str(comment.get("uin", "")),
                    "nickname": comment.get("name", ""),
                    "comment_tid": comment_tid_value,
                    "created_time": comment.get("createTime", "") or comment.get("createTime2", ""),
                    "parent_tid": None,
                })

            feeds_list.append({
                "target_qq": str(target_qq),
                "tid": tid,
                "created_time": created_time,
                "content": content,
                "images": images,
                "videos": videos,
                "rt_con": rt_con,
                "comments": comments,
            })

        return feeds_list

    async def get_qzone_list(self, describe_images: bool = True,
                             max_images: int = 9, image_concurrency: int = 3,
                             compress: bool = True, max_edge: int = 1024, quality: int = 80) -> list[dict[str, Any]]:
        """获取好友动态流（feeds3_html_more，剥壳 _Callback(...) + undefined→null + json5.loads）。

        只保留 appid=='311'（说说）；HTML 用 BS4 从 div.img-box 抠 img[src]，过滤 qzonestyle.gtimg.cn。
        describe_images=False 时跳过图片下载与VLM描述。
        compress / max_edge / quality：送 VLM 前压缩图片（省 token），失败回退原图。
        """
        client = self._get_client()
        res = await client.request(
            method="GET",
            url=self.ZONE_LIST_URL,
            timeout=10,
            params={
                "uin": self.uin,
                "scope": 0,
                "view": 1,
                "filter": "all",
                "flag": 1,
                "applist": "all",
                "pagenum": 1,
                "aisortEndTime": 0,
                "aisortOffset": 0,
                "aisortBeginTime": 0,
                "begintime": 0,
                "format": "json",
                "g_tk": self.gtk2,
                "useutf8": 1,
                "outputhtmlfeed": 1,
            },
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Referer": f"https://user.qzone.qq.com/{self.uin}",
                "Host": "user.qzone.qq.com",
                "Connection": "keep-alive",
            },
            cookies=self.cookies,
        )

        if res.status_code != 200:
            logger.error("访问失败: " + str(res.status_code))
            return []

        data = res.text
        if data.startswith("_Callback(") and data.endswith(");"):
            data = data[len("_Callback("):-2]
        # 只在 JSON 结构位（:, [ { 之后 / , ] } 之前）替换 undefined，
        # 避免全文替换把说说正文里的英文单词 undefined 改写成 null
        data = re.sub(r"(?<=[:,\[])\s*undefined(?=\s*[,\]}])", "null", data)
        try:
            data_dict = json5.loads(data)
            if isinstance(data_dict, dict):
                data_json = data_dict.get("data", {}).get("data", [])
            else:
                logger.error("无效的JSON数据")
                data_json = []
        except Exception as e:
            logger.error(f"解析错误: {e}")
            data_json = []

        try:
            feeds_list = []
            for feed in data_json:
                if not feed:
                    continue
                # 只保留说（appid=311）
                if str(feed.get("appid", "")) != "311":
                    continue
                target_qq = feed.get("uin", "")
                tid = feed.get("key", "")
                if not target_qq or not tid:
                    logger.error(f"无效的说说数据: target_qq={target_qq}, tid={tid}")
                    continue
                # 提前过滤自己的说说（好友动态流不含自己），省 BS4 解析与图片下载
                if str(target_qq) == str(self.uin):
                    continue

                html_content = feed.get("html", "")
                if not html_content:
                    logger.error(f"说说内容为空: UIN={target_qq}, TID={tid}")
                    continue

                soup = bs4.BeautifulSoup(html_content, "html.parser")
                created_time = feed.get("feedstime", "").strip()

                # 正文
                text_div = soup.find("div", class_="f-info")
                text = text_div.get_text(strip=True) if text_div else ""

                # 转发内容
                rt_con = ""
                txt_box = soup.select_one("div.txt-box")
                if txt_box:
                    rt_con = txt_box.get_text(strip=True)
                    if "：" in rt_con:
                        rt_con = rt_con.split("：", 1)[1].strip()

                # 图片URL
                image_urls = []
                img_box = soup.find("div", class_="img-box")
                if img_box:
                    for img in img_box.find_all("img"):
                        src = img.get("src")
                        if src and isinstance(src, str) and not src.startswith("http://qzonestyle.gtimg.cn"):
                            image_urls.append(src)
                # 视频缩略图
                img_tag = soup.select_one("div.video-img img")
                if img_tag and "src" in img_tag.attrs:
                    image_urls.append(img_tag["src"])
                unique_urls = list(dict.fromkeys(image_urls))

                # VLM 描述（并发，保序）
                images = []
                if describe_images:
                    images = await self._describe_images(unique_urls, max_images, image_concurrency,
                                                         compress=compress, max_edge=max_edge, quality=quality)

                # 视频url
                videos = []
                video_div = soup.select_one("div.img-box.f-video-wrap.play")
                if video_div and "url3" in video_div.attrs:
                    videos.append(video_div["url3"])

                # 评论（HTML流里的）
                comments_list = []
                comment_items = soup.select("li.comments-item.bor3")
                for item in comment_items:
                    qq_account = item.get("data-uin", "")
                    comment_tid = item.get("data-tid", "")
                    nickname = item.get("data-nick", "")
                    content_div = item.select_one("div.comments-content")
                    if content_div:
                        for op in content_div.select("div.comments-op"):
                            op.decompose()
                        content = content_div.get_text(" ", strip=True)
                    else:
                        content = ""
                    comment_time_span = item.select_one("span.state")
                    comment_time = comment_time_span.get_text(strip=True) if comment_time_span else ""
                    parent_tid = None
                    parent_div = item.find_parent("div", class_="mod-comments-sub")
                    if parent_div:
                        parent_li = parent_div.find_parent("li", class_="comments-item")
                        if parent_li:
                            parent_tid = parent_li.get("data-tid")
                    comments_list.append({
                        "qq_account": str(qq_account),
                        "nickname": nickname,
                        "comment_tid": int(comment_tid) if isinstance(comment_tid, str) and comment_tid.isdigit() else 0,
                        "content": content,
                        "created_time": comment_time,
                        "parent_tid": int(parent_tid) if isinstance(parent_tid, str) and parent_tid.isdigit() else None,
                    })

                feeds_list.append({
                    "target_qq": str(target_qq),
                    "tid": str(tid),
                    "created_time": created_time,
                    "content": text,
                    "images": images,
                    "videos": videos,
                    "rt_con": rt_con,
                    "comments": comments_list,
                })

            logger.info(f"成功解析 {len(feeds_list)} 条最新说说")
            return feeds_list
        except Exception as e:
            logger.error(f"解析说说错误：{str(e)}")
            return []
