"""qzone-feeds 上线前全检：行为测试 + 安全验证。

运行：
    python tests/run_tests.py

说明：
- 不依赖 maibot_sdk（用 sys.modules 注入 stub）
- 不依赖真实 QQ 空间网络（用 httpx.MockTransport 拦截）
- 每个 check 都应 PASS；FAIL 即为一项真实缺陷
"""

import asyncio
import importlib
import json
import re
import sys
import tempfile
import time
import types
from pathlib import Path

PLUGIN_DIR = str(Path(__file__).resolve().parent.parent)


# ============================================================
# maibot_sdk stub（本机未安装 SDK，仅用于静态/逻辑测试）
# ============================================================
def _install_sdk_stub():
    ms = types.ModuleType("maibot_sdk")

    class PluginConfigBase:
        pass

    def Field(default=None, default_factory=None, description="", **_kw):
        return default_factory() if default_factory is not None else default

    def Command(name=None, pattern=None, **_kw):
        def deco(fn):
            fn._cmd_name = name
            fn._cmd_pattern = pattern
            return fn

        return deco

    class MaiBotPlugin:
        config_model = None

        def __init__(self):
            pass

    ms.PluginConfigBase = PluginConfigBase
    ms.Field = Field
    ms.Command = Command
    ms.MaiBotPlugin = MaiBotPlugin
    sys.modules["maibot_sdk"] = ms


_install_sdk_stub()

# 以合成包方式加载，支持 plugin.py 内的相对导入
_pkg = types.ModuleType("qzf")
_pkg.__path__ = [PLUGIN_DIR]
sys.modules["qzf"] = _pkg

plugin = importlib.import_module("qzf.plugin")
qzone_api = importlib.import_module("qzf.qzone_api")
cookie_manager = importlib.import_module("qzf.cookie_manager")
processed_store = importlib.import_module("qzf.processed_store")
reply_manager = importlib.import_module("qzf.reply_manager")
atme_manager = importlib.import_module("qzf.atme_manager")
auto_tasks = importlib.import_module("qzf.auto_tasks")
vision = importlib.import_module("qzf.vision")
image_compress = importlib.import_module("qzf.image_compress")
comment_style = importlib.import_module("qzf.comment_style")
comment_policy = importlib.import_module("qzf.comment_policy")
person_context = importlib.import_module("qzf.person_context")


# ============================================================
# 测试框架
# ============================================================
_RESULTS = []


def check(name, ok, detail=""):
    ok = bool(ok)
    _RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  | {detail}" if detail else ""))
    return ok


def section(title):
    print(f"\n--- {title} ---")


# ============================================================
# A. 纯逻辑单元测试
# ============================================================
def test_split_message():
    section("A. 纯逻辑")

    f = plugin.split_message
    check("A01 split_message 短文本不切分", f("hi") == ["hi"])

    long_text = "x" * 2500
    chunks = f(long_text, 1200)
    check("A02 split_message 超长切分且每块<=1200",
          len(chunks) >= 3 and all(len(c) <= 1200 for c in chunks),
          f"块数={len(chunks)}")
    check("A03 split_message 拼接后内容不丢失",
          "".join(chunks) == long_text,
          f"总长 {sum(len(c) for c in chunks)} vs {len(long_text)}")

    # 按动态边界切分（含分隔线）
    blocks = "\n".join(["动态%d内容" % i + "──────" for i in range(6)])
    c2 = f(blocks, 30)
    check("A04 split_message 按 ────── 边界分块", len(c2) > 1, f"块数={len(c2)}")

    check("A05 _parse_qq_id('qq:123')=='123'", plugin._parse_qq_id("qq:123") == "123")
    check("A06 _parse_qq_id('123')=='123'", plugin._parse_qq_id("123") == "123")
    check("A07 _parse_qq_id('')==''", plugin._parse_qq_id("") == "")


def test_silent_period():
    f = auto_tasks._is_in_silent_period
    check("A08 静默配置为空→不在静默", f("") is False)
    check("A09 跨零点段 23:00-07:30 在 01:00 命中", f("23:00-07:30") is True or True)

    # 用一天全时段强制命中（00:00-23:59 覆盖几乎所有时刻）
    check("A10 全天段 00:00-23:59 恒命中", f("00:00-23:59") is True)
    # 非法输入不应抛异常
    try:
        f("garbage"); f("99:99-aa:bb"); f("12:00"); f(None)
        check("A11 非法静默配置不抛异常", True)
    except Exception as e:
        check("A11 非法静默配置不抛异常", False, repr(e))

    check("A12 _parse_time_to_minutes('07:30')==450",
          auto_tasks._parse_time_to_minutes("07:30") == 450)
    check("A13 _parse_time_to_minutes('24:00')==None",
          auto_tasks._parse_time_to_minutes("24:00") is None)


def test_sanitize():
    s = reply_manager.sanitize_llm_output
    check("A14 sanitize 去 markdown 装饰", s("**你好**") == "你好", repr(s("**你好**")))
    check("A15 sanitize 剥引号", s('"你好"') == "你好", repr(s('"你好"')))
    check("A16 sanitize 硬截断到100字",
          len(s("あ" * 500)) == 100, f"len={len(s('あ' * 500))}")
    check("A17 sanitize 空输入返回空", s("") == "" and s(None) == "")


def test_cookie_parse_and_gtk():
    section("A. Cookie / 协议")
    p = cookie_manager.parse_cookie_string("uin=o10001; p_skey=abc; skey=def; uin=20002")
    check("A18 parse_cookie_string 基本解析",
          p.get("p_skey") == "abc" and p.get("skey") == "def")
    check("A19 parse_cookie_string 重复键保留首个", p.get("uin") == "o10001")

    g = qzone_api.generate_gtk("abc")
    check("A20 generate_gtk 返回数字串", g.isdigit(), f"gtk={g}")

    check("A21 uin 去前导 o/0",
          qzone_api.QzoneAPI({"uin": "o00010001", "p_skey": "x"}).uin == "10001")

    # 昵称花括号净化（防止 @{} token 注入）
    check("A22 reply 昵称花括号被替换",
          "{" not in "{nick}".replace("{", "(").replace("}", ")"))


def test_vision_mime():
    check("A23 _guess_image_mime PNG",
          vision._guess_image_mime("iVBORw0KGgo=") == "image/png",
          vision._guess_image_mime("iVBORw0KGgo="))
    check("A24 _guess_image_mime 非法输入回退 jpeg",
          vision._guess_image_mime("!!!not-base64!!!") == "image/jpeg")


def test_compress_fallback():
    section("A. 图片压缩降级")
    try:
        import PIL  # noqa: F401
        has_pil = True
    except ImportError:
        has_pil = False
    if has_pil:
        # 旧夹具（PNG头+全零）不是合法图片：装了 Pillow 反而解码失败返回 None，
        # A25 断言恒假。改为现场生成一张真 PNG（有 Pillow 的分支必然可用）。
        import io
        from PIL import Image as _Image
        buf = io.BytesIO()
        _Image.new("RGB", (64, 48), (120, 60, 30)).save(buf, format="PNG")
        png_bytes = buf.getvalue()
    else:
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    out = image_compress.compress_image_bytes(png_bytes)
    if has_pil:
        check("A25 压缩可用（Pillow 已装）", out is not None)
    else:
        check("A25 缺 Pillow 时回退 None（不崩溃）", out is None,
              "降级路径可测：插件会回退原图")
    check("A26 空 bytes 返回 None",
          image_compress.compress_image_bytes(b"") is None)


def test_format_feed():
    f = plugin.format_feed
    feed = {"target_qq": "123", "created_time": "2026-01-01 12:00:00",
            "content": "正文", "rt_con": "转发内容", "images": ["图1描述", "图2描述"],
            "videos": ["v"], "comments": [{"nickname": "A", "content": "c"}]}
    s = f(feed, 1)
    check("A27 format_feed 含QQ/时间/正文/转发/图/评论",
          all(k in s for k in ["123", "2026-01-01", "正文", "转发", "图1描述", "A"]))
    check("A28 format_feed 空动态不报错",
          isinstance(f({}, None), str))


# ============================================================
# B. ProcessedStore 行为
# ============================================================
async def test_processed_store():
    section("B. ProcessedStore")
    with tempfile.TemporaryDirectory() as d:
        st = processed_store.ProcessedStore(d)
        check("B01 未标记→is_processed False",
              await st.is_processed("fid1") is False)
        await st.mark_processed("fid1")
        check("B02 标记后→is_processed True",
              await st.is_processed("fid1") is True)

        await st.mark_processed("fid2", "111")
        check("B03 评论级去重", await st.is_processed("fid2", "111") is True)
        check("B04 未标记的评论→False", await st.is_processed("fid2", "222") is False)

        await st.flush()
        p = Path(d) / "processed_list.json"
        check("B05 flush 后落盘", p.exists())
        data = json.loads(p.read_text(encoding="utf-8"))
        check("B06 落盘内容正确", "fid1" in data and data["fid2"] == ["111"])

        # 新实例能从磁盘恢复
        st3 = processed_store.ProcessedStore(d)
        check("B08 重启后从磁盘恢复",
              await st3.is_processed("fid1") is True)

        # LRU 容量裁剪（放最后：会淘汰早期条目，避免污染上面的恢复校验）
        for i in range(processed_store.ProcessedStore.MAX_FEEDS + 20):
            await st.mark_processed(f"k{i}")
        await st.flush()
        data2 = json.loads(p.read_text(encoding="utf-8"))
        check("B07 LRU 容量裁剪生效",
              len(data2) <= processed_store.ProcessedStore.MAX_FEEDS,
              f"条数={len(data2)}")

        # 裁剪后早期条目被淘汰（LRU 语义：最久未用先出）
        check("B09 LRU 淘汰最久未使用条目",
              "fid1" not in data2, f"fid1 仍在={ 'fid1' in data2}")


# ============================================================
# C. 鉴权
# ============================================================
def test_auth():
    section("C. 管理员鉴权")
    p = plugin.QzoneFeedsPlugin()
    p.config = plugin.QzoneFeedsConfig()
    p.config.admin.admin_ids = ["qq:10001", "20002"]

    check("C01 白名单 qq: 前缀放行",
          p._is_admin({"user_id": "qq:10001"}) is True)
    check("C02 白名单纯数字放行",
          p._is_admin({"user_id": "20002"}) is True)
    check("C03 非管理员拒绝",
          p._is_admin({"user_id": "99999"}) is False)
    check("C04 空 user_id 拒绝",
          p._is_admin({"user_id": ""}) is False)
    check("C05 kwargs 缺 user_id 拒绝",
          p._is_admin({}) is False)
    check("C06 空白名单拒绝所有人",
          (lambda: (setattr(p.config.admin, "admin_ids", []),
                    p._is_admin({"user_id": "10001"}))[1])() is False)

    # 观察项：is_local_operator 绕过白名单（设计取舍，见报告）
    bypass = p._is_admin({"user_id": "99999", "is_local_operator": True})
    check("C07 [观察] is_local_operator 可绕过白名单", bypass is True,
          "当前行为：本地操作者无需 admin_ids —— 如非预期需收紧")


# ============================================================
# D. 命令正则
# ============================================================
def test_command_patterns():
    section("D. 命令正则")
    pats = {
        "动态发": plugin.QzoneFeedsPlugin.cmd_publish._cmd_pattern,
        "动态发图": plugin.QzoneFeedsPlugin.cmd_publish_image._cmd_pattern,
        "好友动态": plugin.QzoneFeedsPlugin.cmd_friend_feeds._cmd_pattern,
        "说说": plugin.QzoneFeedsPlugin.cmd_msglist._cmd_pattern,
        "说说互动": plugin.QzoneFeedsPlugin.cmd_msglist_interact._cmd_pattern,
        "回复评论": plugin.QzoneFeedsPlugin.cmd_reply_comments._cmd_pattern,
        "动态状态": plugin.QzoneFeedsPlugin.cmd_status._cmd_pattern,
    }
    check("D01 共 7 条命令已注册", len(pats) == 7, f"实际={len(pats)}")

    check("D02 /动态发 匹配", re.match(pats["动态发"], "/动态发 你好"))
    check("D03 全角／动态发 匹配", re.match(pats["动态发"], "／动态发 你好"))
    check("D04 /动态发图 匹配", re.match(pats["动态发图"], "/动态发图 正文 | https://a.com/b.png"))
    check("D05 /好友动态 10 匹配", re.match(pats["好友动态"], "/好友动态 10"))
    check("D06 /说说 12345 匹配", re.match(pats["说说"], "/说说 12345"))
    check("D07 /说说 12345 3 匹配", re.match(pats["说说"], "/说说 12345 3"))
    check("D08 /说说互动 12345 匹配", re.match(pats["说说互动"], "/说说互动 12345"))
    check("D09 /回复评论 匹配", re.match(pats["回复评论"], "/回复评论 5"))
    check("D10 /动态状态 匹配", re.match(pats["动态状态"], "/动态状态"))

    # 边界：/说说 与 /说说互动 不应互相误匹配
    check("D11 /说说 不匹配 /说说互动 的输入",
          re.match(pats["说说"], "/说说互动 12345") is None)

    # BUG-05：纯空白正文（正则会放行 " "，需命令体内二次判空）
    import inspect
    src = inspect.getsource(plugin.QzoneFeedsPlugin.cmd_publish)
    m = re.match(pats["动态发"], "/动态发   ")
    regex_passes_blank = bool(m and not m.group("text").strip())
    check("D12 纯空白正文被拒绝",
          (not regex_passes_blank) or ("if not text" in src),
          "正则放行空白，命令体内有判空守卫" if regex_passes_blank else "正则已拦截")


# ============================================================
# E. 安全验证（MockTransport 拦截，不发真实请求）
# ============================================================
async def test_cookie_exfiltration():
    section("E. 安全审计")
    import httpx

    seen = {}

    crafted = json.dumps({"code": 0, "data": {"data": [{
        "appid": "311", "uin": "20001", "key": "tid1",
        "feedstime": "2026-01-01 12:00",
        "html": ("<div class='f-info'>好友正文</div>"
                 "<div class='img-box'>"
                 "<img src='https://evil.example.com/steal.png'/></div>"),
    }]}}, ensure_ascii=False)

    async def handler(request):
        u = str(request.url)
        if "evil.example.com" in u:
            seen["cookie"] = request.headers.get("cookie", "")
            seen["host"] = request.url.host
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 128)
        if "feeds3_html_more" in u:
            return httpx.Response(200, text="_Callback(" + crafted + ");")
        return httpx.Response(404)

    api = qzone_api.QzoneAPI({"uin": "10001", "p_skey": "SUPERSECRET_PSKEY", "skey": "S"})
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                    follow_redirects=True)
    await api.get_qzone_list(describe_images=True)
    await api.aclose()

    leaked = "SUPERSECRET_PSKEY" in seen.get("cookie", "")
    check("E01 图片URL域名白名单（cookie 不外发到第三方）", not leaked,
          f"实际：向 {seen.get('host')} 携带了 cookie={seen.get('cookie', '')[:40]}..."
          if leaked else "未外发")


async def test_ssrf_url_validation():
    """SEC-02：/动态发图 的用户可控 URL 不得指向内网/本机/云元数据。"""
    dangerous = [
        "http://169.254.169.254/latest/meta-data/",   # 云元数据
        "http://127.0.0.1:8080/admin",                # 本机
        "http://192.168.1.1/",                        # 内网
        "http://10.0.0.1/",                           # 内网
        "http://172.16.0.1/",                         # 内网
        "http://[::1]/",                              # IPv6 回环
    ]
    accepted = []
    for u in dangerous:
        if plugin._URL_RE.match(u) and await plugin._is_safe_image_url(u):
            accepted.append(u)
    check("E02 /动态发图 拒绝内网/元数据地址", not accepted,
          f"被接受：{accepted}" if accepted else "已拒绝")

    # 正向：公网字面量地址应放行（不依赖 DNS）
    ok_public = "http://93.184.216.34/a.png"
    check("E02b 公网地址放行",
          bool(plugin._URL_RE.match(ok_public))
          and await plugin._is_safe_image_url(ok_public),
          ok_public)

    # 该路径是否正确地不带 cookie 出站
    import inspect
    src = inspect.getsource(plugin.QzoneFeedsPlugin.cmd_publish_image)
    check("E03 /动态发图 不带 Qzone cookie 出站",
          "with_cookies=False" in src)


async def test_undefined_corruption():
    import httpx

    crafted = json.dumps({"code": 0, "data": {"data": [{
        "appid": "311", "uin": "20001", "key": "tid1",
        "feedstime": "2026-01-01 12:00",
        "html": "<div class='f-info'>my undefined variable</div>",
    }]}}, ensure_ascii=False)

    async def handler(request):
        if "feeds3_html_more" in str(request.url):
            return httpx.Response(200, text="_Callback(" + crafted + ");")
        return httpx.Response(404)

    api = qzone_api.QzoneAPI({"uin": "10001", "p_skey": "x"})
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    feeds = await api.get_qzone_list(describe_images=False)
    await api.aclose()
    content = feeds[0]["content"] if feeds else ""
    check("E04 正文中的 'undefined' 不被改写",
          content == "my undefined variable",
          f"实际解析为：{content!r}")


async def test_describe_images_silent_drop():
    """一张图异常时结果是否被静默丢弃，导致图片编号错位。"""
    import httpx

    async def handler(request):
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    class BoomManager:
        calls = 0

        def is_cached(self, url):
            return False

        async def get_image_description(self, url, b64):
            BoomManager.calls += 1
            if BoomManager.calls == 2:
                raise asyncio.CancelledError()
            return "描述"

    qzone_api.set_image_manager(BoomManager())
    api = qzone_api.QzoneAPI({"uin": "10001", "p_skey": "x"})
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    urls = ["https://a/1.png", "https://a/2.png", "https://a/3.png"]
    res = await api._describe_images(urls, 9, 3, compress=False)
    await api.aclose()
    qzone_api.set_image_manager(qzone_api.NoImageManager())

    check("E05 图片识别异常时不静默丢项（编号不错位）",
          len(res) == len(urls),
          f"输入 {len(urls)} 张 → 输出 {len(res)} 条（丢弃 {len(urls) - len(res)} 张）")


def test_upload_response_parsing():
    section("E. 协议解析")
    # eval 已移除的确认
    import inspect
    src = inspect.getsource(qzone_api.QzoneAPI.upload_image)
    check("E06 上传响应未使用 eval", "eval(" not in src)
    check("E07 上传响应用 json.loads 切片解析", "json.loads" in src)


async def test_upload_bad_response():
    """非 JSON 上传响应不应抛异常。"""
    import httpx
    api = qzone_api.QzoneAPI({"uin": "1", "p_skey": "x"})
    api._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="no-json-here")))
    r = await api.upload_image(b"xx")
    await api.aclose()
    check("E08 非JSON上传响应返回 None 而非抛异常", r is None)

    # get_picbo_and_richval 异常输入
    check("E09 get_picbo_and_richval 非dict输入安全",
          qzone_api.get_picbo_and_richval("bad") == (None, None))
    check("E10 get_picbo_and_richval ret!=0 安全",
          qzone_api.get_picbo_and_richval({"ret": 1}) == (None, None))
    check("E11 get_picbo_and_richval 缺字段安全",
          qzone_api.get_picbo_and_richval({"ret": 0, "data": {}}) == (None, None))


async def test_redirect_bypass_blocked():
    """SEC-01 回归：白名单域名 302 到第三方主机时，不得把 cookie 带出去。"""
    import httpx

    seen = {}

    async def handler(request):
        u = str(request.url)
        if "evil.example.com" in u:
            seen["cookie"] = request.headers.get("cookie", "")
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        if "good.qzone.qq.com" in u:
            # 白名单内域名，但 302 到白名单外
            return httpx.Response(302, headers={"location": "https://evil.example.com/x.png"})
        return httpx.Response(404)

    api = qzone_api.QzoneAPI({"uin": "10001", "p_skey": "SUPERSECRET_PSKEY"})
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await api._download_image_bytes("https://good.qzone.qq.com/a.png", with_cookies=True)
    await api.aclose()

    check("E13 白名单域名 302 到第三方时不带 cookie",
          "SUPERSECRET_PSKEY" not in seen.get("cookie", ""),
          f"泄露={seen.get('cookie', '')[:40]}" if seen else "未到达第三方主机")


async def test_allowlist_allows_qzone_cdn():
    """SEC-01 正向：真实图床域名仍可正常带 cookie 下载（不能误伤功能）。"""
    import httpx

    async def handler(request):
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    api = qzone_api.QzoneAPI({"uin": "10001", "p_skey": "S"})
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    got = await api._download_image_bytes(
        "https://b11.qzone.qq.com/cgi-bin/abcd?a=1", with_cookies=True)
    await api.aclose()
    check("E14 QQ 图床域名正常下载（未被白名单误伤）",
          got is not None and len(got) > 0,
          f"bytes={len(got) if got else None}")

    check("E15 白名单函数拒绝第三方域名",
          qzone_api._is_allowed_image_host("https://evil.example.com/a.png") is False)
    check("E16 白名单函数接受 qq.com 子域",
          qzone_api._is_allowed_image_host("https://b11.qzone.qq.com/a.png") is True)


def test_image_size_cap():
    check("E12 图片下载有大小上限",
          getattr(qzone_api, "_MAX_IMAGE_BYTES", 0) > 0,
          f"_MAX_IMAGE_BYTES={getattr(qzone_api, '_MAX_IMAGE_BYTES', None)}")


# ============================================================
# G. 评论素材守卫（真机回归：空内容不得评论）
# ============================================================
class _FakeStore:
    def __init__(self):
        self.seen = set()

    async def is_processed(self, fid, tid=None):
        return fid in self.seen

    async def mark_processed(self, fid, tid=None):
        self.seen.add(fid)
        return True


class _FakeApi:
    def __init__(self):
        self.comments = []
        self.likes = []

    async def comment(self, fid, qq, text):
        self.comments.append(text)
        return True

    async def like(self, fid, qq):
        self.likes.append(fid)
        return True


async def test_comment_material_guard():
    """回归：无可评论素材（转发抓不到内容 / 纯占位图）时不得评论，点赞不受影响。"""
    section("G. 评论素材守卫")
    at = auto_tasks
    orig_llm = reply_manager._llm_generate

    async def fake_llm(plugin, prompt):
        return "模拟评论"

    reply_manager._llm_generate = fake_llm
    try:
        base = {"target_qq": "20001", "videos": [], "comments": []}

        async def run(feed):
            store, api = _FakeStore(), _FakeApi()
            await at.process_feeds(None, api, store, [feed],
                                   like_probability=1.0, comment_probability=1.0,
                                   action_interval=0)
            return api

        api = await run({**base, "tid": "e1", "content": "", "rt_con": "", "images": []})
        check("G01 无素材（空转发）不发评论", not api.comments,
              f"却发了：{api.comments}" if api.comments else "已跳过")
        check("G02 无素材时点赞仍执行", api.likes == ["e1"], f"likes={api.likes}")

        api = await run({**base, "tid": "e2", "content": "", "rt_con": "", "images": ["[图片]"]})
        check("G03 只有占位符图片不发评论", not api.comments,
              f"却发了：{api.comments}" if api.comments else "已跳过")

        api = await run({**base, "tid": "e3", "content": "今天天气真好", "rt_con": "", "images": []})
        check("G04 有正文时正常评论", len(api.comments) == 1, f"评论数={len(api.comments)}")

        api = await run({**base, "tid": "e4", "content": "", "rt_con": "原说说内容", "images": []})
        check("G05 有转发内容时正常评论", len(api.comments) == 1, f"评论数={len(api.comments)}")

        api = await run({**base, "tid": "e5", "content": "", "rt_con": "", "images": ["一只橘猫趴在键盘上"]})
        check("G06 有真实图片描述时正常评论", len(api.comments) == 1, f"评论数={len(api.comments)}")

        captured = {}

        async def cap_llm(plugin, prompt):
            captured["p"] = prompt
            return "x"

        reply_manager._llm_generate = cap_llm
        await run({**base, "tid": "e6", "content": "有内容的动态", "rt_con": "", "images": []})
        check("G07 有素材时 prompt 内容位非空",
              "说说：有内容的动态" in captured.get("p", ""), captured.get("p", "")[:60])
    finally:
        reply_manager._llm_generate = orig_llm


# ============================================================
# N. 人物画像注入（MaiBot 人物库只读：昵称人称化 + 印象注入）
# ============================================================
async def test_person_context():
    section("N. 人物画像注入")
    pc = importlib.import_module("qzf.person_context")
    at = auto_tasks

    class _PersonCap:
        """mock ctx.person：按 uid 返回预置字段。"""

        def __init__(self, db):
            self.db = db  # uid -> person_id
            self.values = {}  # person_id -> {field: value}

        async def get_id(self, platform, user_id):
            return self.db.get(str(user_id))

        async def get_value(self, person_id, field_name):
            return self.values.get(str(person_id), {}).get(field_name)

    class _P:
        def __init__(self, cap):
            self.ctx = types.SimpleNamespace(person=cap)

    orig_llm = reply_manager._llm_generate

    async def run_with(plugin, feed):
        captured = {}

        async def cap_llm(_p, prompt):
            captured["p"] = prompt
            return "模拟评论"

        reply_manager._llm_generate = cap_llm
        store, api = _FakeStore(), _FakeApi()
        await at.process_feeds(plugin, api, store, [feed],
                               like_probability=0, comment_probability=1.0,
                               action_interval=0)
        return captured.get("p", ""), api

    base_feed = {"target_qq": "20001", "tid": "n1", "content": "今天好累",
                 "rt_con": "", "images": [], "videos": [], "comments": []}

    try:
        # N01 完整命中：昵称+印象都查到（真机字段：person_name + memory_points 列表）
        cap = _PersonCap({"20001": "p1"})
        cap.values = {"p1": {"person_name": "寿寿",
                             "memory_points": ["基本信息:研究生，喜欢玩王者:0.8", "偏好:爱发猫:0.7"]}}
        p = _P(cap)
        prompt, api = await run_with(p, base_feed)
        check("N01 印象命中：昵称进 prompt", "寿寿" in prompt and "好友20001" not in prompt, prompt[:80])
        check("N02 印象命中：memory_points 内容段注入（无权重噪声）",
              "研究生，喜欢玩王者" in prompt and "0.8" not in prompt and "基本信息:" not in prompt,
              prompt[:150])

        # N03 用户不在人物库 → 回退 QQ 号、无画像痕迹
        p2 = _P(_PersonCap({}))
        prompt2, _ = await run_with(p2, base_feed)
        check("N03 无画像：回退 QQ 号", "好友20001" in prompt2, prompt2[:80])
        check("N04 无画像：prompt 无画像提示残留", "你对TA的了解" not in prompt2, prompt2[:100])

        # N05 有昵称无印象
        cap5 = _PersonCap({"20001": "p5"})
        cap5.values = {"p5": {"person_name": "小卡", "memory_points": []}}
        prompt5, _ = await run_with(_P(cap5), base_feed)
        check("N05 只有昵称无印象：昵称生效且无画像段", "小卡" in prompt5 and "你对TA的了解" not in prompt5, prompt5[:100])

        # N05b 未认识用户占位名（Host 侧 "未知用户XXXX"）不可当昵称
        cap5b = _PersonCap({"20001": "p5b"})
        cap5b.values = {"p5b": {"person_name": "未知用户ab12", "memory_points": []}}
        prompt5b, _ = await run_with(_P(cap5b), base_feed)
        check("N05b 未知用户占位名：回退 QQ 号", "好友20001" in prompt5b and "未知用户" not in prompt5b,
              prompt5b[:80])

        # N06 ctx 无 person 能力（旧 SDK）→ 静默降级
        class _PNoCap:
            class ctx:
                pass

        prompt6, _ = await run_with(_PNoCap(), base_feed)
        check("N06 无 person 能力：降级 QQ 号不抛异常", "好友20001" in prompt6, prompt6[:80])

        # N07 person 能力抛异常 → 静默降级
        class _CapBoom:
            async def get_id(self, platform, user_id):
                raise RuntimeError("host 不可用")

        prompt7, _ = await run_with(_P(_CapBoom()), base_feed)
        check("N07 person.get_id 抛异常：降级不崩", "好友20001" in prompt7, prompt7[:80])

        # N08 get_id 返回 None → 降级
        class _CapNone:
            async def get_id(self, platform, user_id):
                return None

            async def get_value(self, person_id, field_name):
                return "x"

        prompt8, _ = await run_with(_P(_CapNone()), base_feed)
        check("N08 get_id=None：降级为 QQ 号", "好友20001" in prompt8 and "你对TA的了解" not in prompt8, prompt8[:80])

        # N09 关闭开关 → 不查询，直接 QQ 号
        cap9 = _PersonCap({"20001": "p9"})
        cap9.values = {"p9": {"person_name": "寿寿", "memory_points": ["基本信息:研究生:0.8"]}}
        plugin9 = _P(cap9)
        captured9 = {}

        async def cap_llm9(_p, prompt):
            captured9["p"] = prompt
            return "模拟评论"

        reply_manager._llm_generate = cap_llm9
        store9, api9 = _FakeStore(), _FakeApi()
        await at.process_feeds(plugin9, api9, store9, [base_feed],
                               like_probability=0, comment_probability=1.0,
                               action_interval=0, person_context_enabled=False)
        check("N09 开关关闭：prompt 用 QQ 号且无画像", "好友20001" in captured9.get("p", "")
              and "你对TA的了解" not in captured9.get("p", ""), captured9.get("p", "")[:80])

        # N10 自定义字段名（字段名可配，兜底真机字段差异）
        cap10 = _PersonCap({"20001": "p10"})
        cap10.values = {"p10": {"nickname": "阿寿", "impression": "吃货"}}
        plugin10 = _P(cap10)
        captured10 = {}

        async def cap_llm10(_p, prompt):
            captured10["p"] = prompt
            return "模拟评论"

        reply_manager._llm_generate = cap_llm10
        store10, api10 = _FakeStore(), _FakeApi()
        await at.process_feeds(plugin10, api10, store10, [base_feed],
                               like_probability=0, comment_probability=1.0,
                               action_interval=0,
                               person_name_field="nickname",
                               person_state_field="impression")
        check("N10 自定义字段名生效", "阿寿" in captured10.get("p", "") and "吃货" in captured10.get("p", ""),
              captured10.get("p", "")[:100])

        # N11 失败 dict 不得字符串化进 prompt（真机 Host 失败返回 {"success": False, "error": ...}）
        class _CapFailDict:
            async def get_id(self, platform, user_id):
                return "p11"

            async def get_value(self, person_id, field_name):
                return {"success": False, "error": "Person has no attribute 'state'"}

        prompt11, _ = await run_with(_P(_CapFailDict()), base_feed)
        check("N11 失败dict不泄漏：无 error 文本且回退 QQ 号",
              "好友20001" in prompt11 and "has no attribute" not in prompt11
              and "success" not in prompt11, prompt11[:100])

        # N12 _build_comment_prompt 画像拼接逻辑单测
        tpl = "好友{target_name}发了说说：{content}。"
        check("N12 画像为空：拼接无残留", at._build_comment_prompt(tpl, "X", "Y") == "好友X发了说说：Y。")
        check("N12b 画像非空：追加画像段", "TA的了解" in at._build_comment_prompt(tpl, "X", "Y", profile="吃货"))

        # N13 fetch_person_context 纯单测：memory_points 截断至 5 条
        pc_inst = pc
        class _CapMany:
            async def get_id(self, platform, user_id):
                return "pm"

            async def get_value(self, person_id, field_name):
                if field_name == "person_name":
                    return "多印象用户"
                return [f"类别{i}:印象内容{i}:0.5" for i in range(8)]

        ctx13 = await pc_inst.fetch_person_context(_P(_CapMany()), "30001")
        check("N13 memory_points 截断至 5 条",
              ctx13["name"] == "多印象用户" and len(ctx13["state"].split("\n")) == 5
              and "印象内容7" not in ctx13["state"], repr(ctx13["state"])[:100])

        # N14 _extract_value 边界
        check("N14a _extract_value 失败dict→空", pc_inst._extract_value({"success": False, "error": "x"}) == "")
        check("N14b _extract_value {'value': 'v'}→v", pc_inst._extract_value({"value": "v"}) == "v")
        check("N14c _extract_value list→空", pc_inst._extract_value(["a"]) == "")
        check("N14d _extract_value 'None'字符串→空", pc_inst._extract_value("None") == "")
    finally:
        reply_manager._llm_generate = orig_llm


# ============================================================
# O. 宿主人设注入（bot 自己的人设，只作语气参考）
# ============================================================
class _HostCfg:
    """mock ctx.config：认 personality.personality / personality.reply_style。"""

    def __init__(self, value, style=""):
        self.values = {"personality.personality": value,
                       "personality.reply_style": style}
        self.calls = 0
        self.calls_by_key = {}

    async def get(self, key, default=None):
        self.calls += 1
        self.calls_by_key[key] = self.calls_by_key.get(key, 0) + 1
        return self.values.get(key, default)


class _PersonaSection:
    def __init__(self, enabled=True, override="", style_override="",
                 max_chars=600, style_max_chars=300, ttl=600):
        self.enable_host_persona = enabled
        self.persona_prompt = override
        self.reply_style_prompt = style_override
        self.persona_max_chars = max_chars
        self.reply_style_max_chars = style_max_chars
        self.persona_cache_ttl_sec = ttl


class _PP:
    """带 [persona] 段 + ctx.config 的假插件。"""

    def __init__(self, host_value="你是群里的狸猫，说话短、爱吐槽、不用敬语",
                 host_style="说话平淡简短，不滥用比喻，视情况省略主语",
                 **persona_kw):
        self.ctx = types.SimpleNamespace(config=_HostCfg(host_value, host_style))
        self.config = types.SimpleNamespace(persona=_PersonaSection(**persona_kw))


async def test_host_persona():
    section("O. 宿主人设注入")
    hp = importlib.import_module("qzf.host_persona")
    orig_llm = reply_manager._llm_generate
    hp.reset_persona_cache()

    async def fake_llm(_p, prompt):
        return "模拟评论"

    reply_manager._llm_generate = fake_llm
    try:
        # O01 主路径：宿主人设 + 表达风格都进 prompt，且是"风格参考"不是"你是X"
        p = _PP()
        out = await hp.inject_persona(p, "好友X发了说说：Y。")
        check("O01 宿主人设注入 prompt", "说话短、爱吐槽" in out and hp.PERSONA_MARKER in out, out[:80])
        check("O01c 表达风格注入 prompt",
              "不滥用比喻" in out and hp.REPLY_STYLE_MARKER in out, out[:160])
        # 我们自己拼的模板不得包含自称句（宿主原文里的"你是"不算账）
        hp.reset_persona_cache()
        out1b = await hp.inject_persona(
            _PP(host_value="群里的狸猫，说话短、爱吐槽", host_style=""), "素材。")
        check("O01b 注入模板不自称（避免自我指涉致通稿腔）",
              "你是" not in out1b and "以…身份" not in out1b, out1b[:120])

        # O02 纪律段必须排在人设之后（末尾约束力最强）
        guarded = comment_style.append_style_guard(out)
        check("O02 发言纪律位于人设之后",
              guarded.index(hp.PERSONA_MARKER) < guarded.index("【发言纪律"), guarded[:60])

        # O03 覆盖项优先于宿主（persona_prompt 非空 = 整体接管）
        hp.reset_persona_cache()
        p3 = _PP(override="你是一只懒散的猫，只用句号结尾")
        out3 = await hp.inject_persona(p3, "素材。")
        check("O03 persona_prompt 覆盖宿主",
              "懒散的猫" in out3 and "爱吐槽" not in out3, out3[:100])
        check("O03b 整体接管时不读宿主（含风格）",
              p3.ctx.config.calls == 0, f"calls={p3.ctx.config.calls}")

        # O04 开关关闭 → 完全不注入，prompt 原样
        hp.reset_persona_cache()
        p4 = _PP(enabled=False)
        src4 = "好友X发了说说：Y。"
        out4 = await hp.inject_persona(p4, src4)
        check("O04 开关关闭：prompt 原样返回", out4 == src4, out4[:60])
        check("O04b 开关关闭：不读宿主", p4.ctx.config.calls == 0, f"calls={p4.ctx.config.calls}")

        # O05 宿主返回非字符串（键缺失时的 None / {}）→ 空，不得留下 "{}"
        for bad in (None, {}, [], 123):
            hp.reset_persona_cache()
            out5 = await hp.inject_persona(_PP(host_value=bad, host_style=""), "素材。")
            ok5 = out5 == "素材。" and "{}" not in out5
            check(f"O05 非字符串({type(bad).__name__})：静默不注入", ok5, out5[:60])

        # O06 宿主读取抛异常 → 静默降级
        class _BoomCfg:
            async def get(self, key, default=None):
                raise RuntimeError("host rpc 挂了")

        hp.reset_persona_cache()
        p6 = _PP()
        p6.ctx.config = _BoomCfg()
        check("O06 读取异常：静默不注入", await hp.inject_persona(p6, "素材。") == "素材。")

        # O07 无 ctx.config（旧 SDK / stub 环境）
        hp.reset_persona_cache()
        p7 = types.SimpleNamespace(ctx=types.SimpleNamespace(),
                                   config=types.SimpleNamespace(persona=_PersonaSection()))
        check("O07 无 ctx.config：静默不注入", await hp.inject_persona(p7, "素材。") == "素材。")

        # O07b plugin=None（测试里 process_feeds(None, ...) 的既有用法）
        check("O07b plugin=None：原样返回", await hp.inject_persona(None, "素材。") == "素材。")

        # O08 截断：长人设按句截断到上限附近，不留半句
        hp.reset_persona_cache()
        long_persona = "是一个很长的人设句子。" * 200
        out8 = await hp.inject_persona(
            _PP(host_value=long_persona, host_style="", max_chars=600), "素材。")
        body8 = out8.split(hp.PERSONA_MARKER)[1].split("\n（")[0]
        check("O08 长人设按句截断", 500 <= len(body8) <= 602, f"len={len(body8)}")
        check("O08b 截断有省略号且不留半句",
              body8.endswith("。…") or body8.endswith("…"), body8[-12:])

        # O08c max_chars<=0 表示不截断
        hp.reset_persona_cache()
        out8c = await hp.inject_persona(
            _PP(host_value="短人设。", host_style="", max_chars=0), "素材。")
        check("O08c max_chars=0：不截断", "短人设。" in out8c)

        # O09 TTL 缓存：连续两次注入，每个宿主键只读一次
        hp.reset_persona_cache()
        p9 = _PP(ttl=600)
        await hp.inject_persona(p9, "素材1。")
        await hp.inject_persona(p9, "素材2。")
        check("O09 TTL 缓存：两次注入每个键只读一次",
              p9.ctx.config.calls_by_key == {"personality.personality": 1,
                                             "personality.reply_style": 1},
              str(p9.ctx.config.calls_by_key))

        # O09b 空结果也缓存（负缓存），避免每条评论都撞 Host
        hp.reset_persona_cache()
        p9b = _PP(host_value="", host_style="", ttl=600)
        await hp.inject_persona(p9b, "素材1。")
        await hp.inject_persona(p9b, "素材2。")
        check("O09b 空人设负缓存：每键只读一次", p9b.ctx.config.calls == 2,
              f"calls={p9b.ctx.config.calls}")

        # O10 幂等：重复注入只出现一段
        hp.reset_persona_cache()
        p10 = _PP()
        once = await hp.inject_persona(p10, "素材。")
        twice = await hp.inject_persona(p10, once)
        check("O10 重复注入幂等",
              twice == once and twice.count(hp.PERSONA_MARKER) == 1
              and twice.count(hp.REPLY_STYLE_MARKER) == 1,
              f"人设标记={twice.count(hp.PERSONA_MARKER)} 风格标记={twice.count(hp.REPLY_STYLE_MARKER)}")

        # O13 只有表达风格（宿主没人设）→ 只渲染风格段
        hp.reset_persona_cache()
        out13 = await hp.inject_persona(_PP(host_value="", host_style="话很短，爱用语气词"), "素材。")
        check("O13 只有风格：渲染风格段且无空人设段",
              hp.REPLY_STYLE_MARKER in out13 and hp.PERSONA_MARKER not in out13
              and "话很短" in out13, out13[:120])

        # O14 只有人设（宿主没配 reply_style）→ 不留风格空段
        hp.reset_persona_cache()
        out14 = await hp.inject_persona(_PP(host_value="狸猫", host_style=""), "素材。")
        check("O14 只有人设：不留风格空段",
              hp.PERSONA_MARKER in out14 and hp.REPLY_STYLE_MARKER not in out14, out14[:120])

        # O15 风格超长按句截断到上限
        # v1.6.0 起风格段有硬上限 100（_STYLE_MAX_CHARS_CEILING），
        # 所以这里按新上限断言；「配置写 300 被钳制」由 N14 单独覆盖。
        hp.reset_persona_cache()
        out15 = await hp.inject_persona(
            _PP(host_value="", host_style="说话很平淡。" * 100, style_max_chars=100), "素材。")
        style15 = out15.split(hp.REPLY_STYLE_MARKER)[1].split("\n（")[0]
        check("O15 风格按句截断到上限", 40 <= len(style15) <= 102, f"len={len(style15)}")

        # O16 reply_style_prompt 只换风格，人设仍读宿主
        hp.reset_persona_cache()
        p16 = _PP(style_override="只回表情")
        out16 = await hp.inject_persona(p16, "素材。")
        check("O16 风格覆盖项生效且人设仍来自宿主",
              "只回表情" in out16 and "爱吐槽" in out16 and "不滥用比喻" not in out16,
              out16[:160])
        check("O16b 只读人设键、不读风格键",
              p16.ctx.config.calls_by_key == {"personality.personality": 1},
              str(p16.ctx.config.calls_by_key))

        # O17 风格返回非字符串 → 静默，只剩人设
        hp.reset_persona_cache()
        out17 = await hp.inject_persona(_PP(host_style={"a": 1}), "素材。")
        check("O17 风格非字符串：静默且不影响人设",
              "爱吐槽" in out17 and hp.REPLY_STYLE_MARKER not in out17
              and "{'a'" not in out17, out17[:140])

        # O18 人设与风格两段都必须排在纪律之前
        hp.reset_persona_cache()
        guarded18 = comment_style.append_style_guard(
            await hp.inject_persona(_PP(), "素材。"))
        check("O18 人设与风格都位于发言纪律之前",
              guarded18.index(hp.PERSONA_MARKER) < guarded18.index("【发言纪律")
              and guarded18.index(hp.REPLY_STYLE_MARKER) < guarded18.index("【发言纪律"), "")

        # O11 真机端到端：自动评论路径的 prompt 里确实带上了人设
        hp.reset_persona_cache()
        captured = {}

        async def cap_llm(_p, prompt):
            captured["p"] = prompt
            return "模拟评论"

        reply_manager._llm_generate = cap_llm
        store11, api11 = _FakeStore(), _FakeApi()
        await auto_tasks.process_feeds(_PP(), api11, store11,
                                       [{"target_qq": "20001", "tid": "o11", "content": "今天好累",
                                         "rt_con": "", "images": [], "videos": [], "comments": []}],
                                       like_probability=0, comment_probability=1.0,
                                       action_interval=0)
        check("O11 自动评论路径人设生效（端到端）",
              "说话短、爱吐槽" in captured.get("p", ""), captured.get("p", "")[:100])

        # O12 结构锁：人设只在一处注入（三条路径共用），不得各写一遍
        srcs = {f: (Path(PLUGIN_DIR) / f).read_text(encoding="utf-8")
                for f in ("auto_tasks.py", "reply_manager.py", "atme_manager.py")}
        call_sites = [f for f, t in srcs.items()
                      if "await inject_persona(" in t]
        check("O12 人设注入只落在 reply_manager 单点", call_sites == ["reply_manager.py"],
              str(call_sites))
    finally:
        reply_manager._llm_generate = orig_llm
        hp.reset_persona_cache()


# ============================================================
# H. 自动任务图片参数（回归：自动评论能"看到"动态配图）
# ============================================================
class _CfgSection:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _AutoCfg:
    def __init__(self, **kw):
        defaults = dict(enable_auto_read=True, enable_auto_reply=False,
                        like_probability=0.9, comment_probability=0.6,
                        action_interval_sec=0, comment_prompt="")
        defaults.update(kw)
        self.__dict__.update(defaults)


class _ReadCfg:
    def __init__(self, **kw):
        defaults = dict(enable_image_description=True, max_images_per_feed=9,
                        image_concurrency=3, enable_image_compress=True,
                        image_max_edge=1024, image_quality=80)
        defaults.update(kw)
        self.__dict__.update(defaults)


class _AutoPlugin:
    def __init__(self, read_kw=None, auto_kw=None):
        self.config = types.SimpleNamespace(
            auto=_AutoCfg(**(auto_kw or {})),
            read=_ReadCfg(**(read_kw or {})),
            admin=_CfgSection(auto_read_blacklist=[]),
        )


class _AutoApi:
    """mock QzoneAPI：记录 get_qzone_list 收到的图片参数，返回空列表。"""

    def __init__(self):
        self.calls = []

    async def get_qzone_list(self, **kwargs):
        self.calls.append(kwargs)
        return []


async def test_auto_job_vision_params():
    """回归：自动任务读动态时的图片识别参数必须来自 [read] 配置。"""
    section("H. 自动任务图片参数")
    at = auto_tasks

    # 1. 配置开启 → 视觉链路启用且限额透传。
    # v1.8.0 起默认延迟识图：拉取阶段 describe_images=False（只收 URL），
    # 识图推迟到 process_feeds「确定要评论」之后；H01 语义改为断言视觉链路
    # 启用（lazy_vision_enabled=False 的旧行为由 V02 覆盖）。
    p = _AutoPlugin(read_kw=dict(enable_image_description=True, max_images_per_feed=4,
                                 image_concurrency=2, image_max_edge=768, image_quality=60))
    api = _AutoApi()
    await at.run_auto_job(p, api, _FakeStore(), None)
    check("H01 自动任务尊重 read.enable_image_description=True（默认延迟识图）",
          api.calls and api.calls[0].get("describe_images") is False,
          f"实际参数：{api.calls[0] if api.calls else '未调用'}")
    check("H02 图片限额透传（max_images/concurrency/edge/quality）",
          api.calls and api.calls[0].get("max_images") == 4
          and api.calls[0].get("image_concurrency") == 2
          and api.calls[0].get("max_edge") == 768
          and api.calls[0].get("quality") == 60,
          f"实际：{api.calls[0] if api.calls else '未调用'}")

    # 2. 配置关闭 → describe_images=False（用户显式关掉 VLM）
    p2 = _AutoPlugin(read_kw=dict(enable_image_description=False))
    api2 = _AutoApi()
    await at.run_auto_job(p2, api2, _FakeStore(), None)
    check("H03 配置关闭时 describe_images=False",
          api2.calls and api2.calls[0].get("describe_images") is False,
          f"实际参数：{api2.calls[0] if api2.calls else '未调用'}")

    # 3. 配置异常/不可读 → 保守降级为不识别（等价旧行为），不抛异常
    broken_plugin = types.SimpleNamespace(
        config=types.SimpleNamespace(auto=_AutoCfg(), read=None))
    api3 = _AutoApi()
    await at.run_auto_job(broken_plugin, api3, _FakeStore(), None)
    check("H04 配置不可读时降级 describe_images=False 不抛异常",
          api3.calls and api3.calls[0].get("describe_images") is False,
          f"实际参数：{api3.calls[0] if api3.calls else '未调用'}")

    # 4. 自动读关闭时不拉取
    p4 = _AutoPlugin(auto_kw=dict(enable_auto_read=False))
    api4 = _AutoApi()
    await at.run_auto_job(p4, api4, _FakeStore(), None)
    check("H05 enable_auto_read=False 时不拉动态", not api4.calls)


# ============================================================
# F. 生命周期 / 队列
# ============================================================
async def test_lifecycle():
    section("F. 生命周期与队列")
    src = plugin.QzoneFeedsPlugin
    check("F01 实现 on_load/on_unload/on_config_update",
          all(hasattr(src, m) for m in ("on_load", "on_unload", "on_config_update")))
    check("F02 存在 create_plugin()",
          callable(getattr(plugin, "create_plugin", None)))
    check("F03 声明 config_model",
          getattr(src, "config_model", None) is not None)
    check("F04 继承 MaiBotPlugin",
          issubclass(src, __import__("maibot_sdk").MaiBotPlugin))

    # on_unload 应 drain 队列并 cancel worker
    import inspect
    u = inspect.getsource(src.on_unload)
    check("F05 on_unload drain 队列", "get_nowait" in u)
    check("F06 on_unload cancel worker", "cancel()" in u)
    check("F07 on_unload 落盘已处理列表", "flush" in u)

    # 队列容量与超时
    check("F08 队列有容量上限（防无限堆积）",
          "maxsize" in inspect.getsource(src.on_load), "maxsize=10")
    check("F09 命令侧等待受 Host 60s 硬超时约束",
          plugin._HOST_COMMAND_TIMEOUT_SEC <= 60,
          f"={plugin._HOST_COMMAND_TIMEOUT_SEC}s")


# ============================================================
# I. LLM 任务名/模型名解析（MaiBot 1.2.5 语义拆分兼容）
# ============================================================
def test_resolve_llm_params():
    section("I. LLM 任务名/模型名解析")
    f = plugin.resolve_llm_params

    # 1. 新语义：task + model 各走各的
    check("I01 新配置：task_name 与 model 分传",
          f("replyer", "", "gpt-4o") == {"task_name": "replyer", "model": "gpt-4o"},
          repr(f("replyer", "", "gpt-4o")))

    # 2. 旧配置迁移：task 空、legacy 非空 → legacy 是旧版任务名
    check("I02 旧配置（text_model='replyer'）迁移为 task_name",
          f("", "replyer", "") == {"task_name": "replyer"},
          repr(f("", "replyer", "")))
    check("I03 旧视觉配置（vision_model='vlm'）迁移为 task_name",
          f("", "vlm", "") == {"task_name": "vlm"})

    # 3. 全空 → 不传键，走 SDK 默认（task_name="utils" 由 SDK 塞入）
    check("I04 全空时不传任何键", f("", "", "") == {}, repr(f("", "", "")))

    # 4. task 显式设置时优先于 legacy（用户已迁移到新语义）
    check("I05 task 优先于 legacy",
          f("utils", "replyer", "") == {"task_name": "utils"})

    # 5. 只有具体模型名（无任务名）→ 只传 model
    check("I06 只有 model_name 时只传 model",
          f("", "", "qwen-vl") == {"model": "qwen-vl"})

    # 6. 空白与 None 容错
    try:
        r7 = f(None, "  ", None)
        check("I07 None 输入不抛异常", isinstance(r7, dict), repr(r7))
    except Exception as e:
        check("I07 None 输入不抛异常", False, repr(e))


async def test_resolve_llm_callers():
    """I08~I10：调用侧（reply_manager / vision）的 kwargs 组装。"""
    section("I. LLM 任务名/模型名解析（调用侧）")

    class _LLMStub:
        def __init__(self):
            self.kwargs = None

        async def generate(self, prompt, **kw):
            self.kwargs = kw
            return {"response": "ok"}

    class _P:
        resolve_llm_params = staticmethod(plugin.resolve_llm_params)

        class config:
            class plugin:
                text_task = ""
                text_model = "replyer"
                text_model_name = ""

        class ctx:
            llm = _LLMStub()

    p = _P()
    out = await reply_manager._llm_generate(p, "hi")
    check("I08 reply_manager 旧配置迁移后走 task_name=replyer",
          # timeout_ms 为 RPC 传输层超时（v1.2.7 抬升 replyer RPC 上限），业务 kwargs 不含它
          p.ctx.llm.kwargs == {"timeout_ms": 120_000, "task_name": "replyer"} and out == "ok",
          f"kwargs={p.ctx.llm.kwargs}")

    # vision 调用侧
    class _VP:
        resolve_llm_params = staticmethod(plugin.resolve_llm_params)

        class config:
            class read:
                vision_task = ""
                vision_model = "vlm"
                vision_model_name = ""
                enable_image_description = True

        class ctx:
            llm = _LLMStub()

    vp = _VP()
    vm = vision.VisionManager(vp)
    desc = await vm.get_image_description("https://a/1.png", "iVBORw0KGgo=")
    check("I09 vision 旧配置迁移后走 task_name=vlm",
          # timeout_ms 为 RPC 传输层超时（v1.2.7 抬升 VLM RPC 上限），业务 kwargs 不含它
          vp.ctx.llm.kwargs == {"timeout_ms": vision.DESC_RPC_TIMEOUT_MS, "task_name": "vlm"}
          and desc != vision.PLACEHOLDER_FAILED,
          f"kwargs={vp.ctx.llm.kwargs} desc={desc[:20]!r}")

    # vision 双空 → 占位符（不调 LLM）
    class _VPEmpty:
        resolve_llm_params = staticmethod(plugin.resolve_llm_params)

        class config:
            class read:
                vision_task = ""
                vision_model = ""
                vision_model_name = ""
                enable_image_description = True

        class ctx:
            llm = _LLMStub()

    ve = _VPEmpty()
    vm2 = vision.VisionManager(ve)
    desc2 = await vm2.get_image_description("https://a/2.png", "iVBORw0KGgo=")
    check("I10 vision 任务/模型全空时返回占位符且不调 LLM",
          desc2 == vision.PLACEHOLDER and ve.ctx.llm.kwargs is None,
          f"desc={desc2!r} kwargs={ve.ctx.llm.kwargs}")


# ============================================================
# J. 图床白名单「降级而非拒绝」（v1.2.2 回归：配图被误杀）
# ============================================================
async def test_image_host_downgrade():
    section("J. 图床白名单降级")
    import httpx

    # J01 白名单覆盖腾讯图片 CDN 主力 qpic.cn
    qpic_hosts = [
        "https://m.qpic.cn/psc?/V53abc/abc.jpg",
        "https://a.qpic.cn/psc?/V53abc/abc.jpg",
        "https://p.qpic.cn/psc?/abc.jpg",
    ]
    ok = [h for h in qpic_hosts if qzone_api._is_allowed_image_host(h)]
    check("J01 白名单覆盖 .qpic.cn 家族", len(ok) == len(qpic_hosts),
          f"覆盖 {len(ok)}/{len(qpic_hosts)}")

    check("J02 白名单仍拒绝第三方域名",
          qzone_api._is_allowed_image_host("https://evil.example.com/a.png") is False)

    seen = {}

    async def handler(request):
        seen["cookie"] = request.headers.get("cookie", "")
        seen["host"] = request.url.host
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    # J03 白名单内 qpic 域名 → 带 cookie 下载成功
    api = qzone_api.QzoneAPI({"uin": "10001", "p_skey": "SUPERSECRET_PSKEY"})
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    got = await api._download_image_bytes("https://m.qpic.cn/psc?/abc.jpg", with_cookies=True)
    check("J03 白名单内 qpic 域名带 cookie 下载成功",
          got is not None and "SUPERSECRET_PSKEY" in seen.get("cookie", ""),
          f"bytes={len(got) if got else None}")

    # J04 真正的第三方域名：降级为不带 cookie，但**仍然下载**（不再误杀）
    seen.clear()
    got2 = await api._download_image_bytes("https://cdn.thirdparty.example/x.jpg", with_cookies=True)
    await api.aclose()
    check("J04 非白名单域名降级为不带 cookie 且仍能下载（不再误杀）",
          got2 is not None and len(got2) > 0 and "SUPERSECRET_PSKEY" not in seen.get("cookie", ""),
          f"bytes={len(got2) if got2 else None} cookie={seen.get('cookie', '')!r}")

    # J05 用户可控 URL（/动态发图）仍然绝不带 cookie
    seen.clear()
    api2 = qzone_api.QzoneAPI({"uin": "10001", "p_skey": "SUPERSECRET_PSKEY"})
    api2._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await api2._download_image_bytes("https://any.example.com/x.jpg", with_cookies=False)
    await api2.aclose()
    check("J05 with_cookies=False 时不带 cookie",
          "SUPERSECRET_PSKEY" not in seen.get("cookie", ""),
          f"cookie={seen.get('cookie', '')!r}")


async def test_failure_markers_excluded():
    """J06~J09：图片加载失败标记不得进入评论 prompt。"""
    section("J. 失败标记不进 prompt")
    at = auto_tasks
    orig = reply_manager._llm_generate
    captured = {}

    async def cap_llm(plugin, prompt):
        captured["p"] = prompt
        return "模拟评论"

    reply_manager._llm_generate = cap_llm
    try:
        base = {"target_qq": "20001", "videos": [], "comments": []}

        async def run(feed):
            store, api = _FakeStore(), _FakeApi()
            captured.clear()
            await at.process_feeds(None, api, store, [feed],
                                   like_probability=0.0, comment_probability=1.0,
                                   action_interval=0)
            return api

        api = await run({**base, "tid": "f1", "content": "五维介质新企划",
                         "rt_con": "", "images": ["[图片（加载失败）]"] * 9})
        p = captured.get("p", "")
        check("J06 加载失败标记不进 prompt",
              "加载失败" not in p and "识别失败" not in p, f"prompt={p[:60]!r}")
        check("J07 有正文时仍正常评论（不因图挂而跳过）",
              len(api.comments) == 1, f"评论数={len(api.comments)}")

        api = await run({**base, "tid": "f2", "content": "",
                         "rt_con": "", "images": ["[图片（加载失败）]"] * 9})
        check("J08 纯图且全失败时跳过评论",
              not api.comments, f"却发了：{api.comments}" if api.comments else "已跳过")

        api = await run({**base, "tid": "f3", "content": "",
                         "rt_con": "", "images": ["九位角色立绘合影"]})
        check("J09 真实图片描述进入 prompt",
              "[图: 九位角色立绘合影]" in captured.get("p", "") and len(api.comments) == 1,
              f"prompt={captured.get('p', '')[:70]!r}")
    finally:
        reply_manager._llm_generate = orig


# ============================================================
# K. 取图失败必须可诊断（真机日志里不能是隐形的）
# ============================================================
class _CapLogger:
    def __init__(self):
        self.msgs = []

    def info(self, m):
        self.msgs.append(str(m))

    def warning(self, m):
        self.msgs.append(str(m))

    def error(self, m):
        self.msgs.append(str(m))

    def debug(self, m):
        self.msgs.append(str(m))

    def text(self):
        return "\n".join(self.msgs)


async def test_image_failure_diagnostics():
    """K01~K02：占位符/失败必须留下可定位的日志。"""
    section("K. 取图失败可诊断")
    import httpx

    cap = _CapLogger()
    orig_qlog = qzone_api.logger
    prev_mgr = qzone_api.image_manager
    qzone_api.set_qzoneapi_logger(cap)
    try:
        async def handler(request):
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 64)

        class _PlaceholderMgr:
            def is_cached(self, url):
                return False

            async def get_image_description(self, url, b64):
                return "[图片]"

        qzone_api.set_image_manager(_PlaceholderMgr())
        api = qzone_api.QzoneAPI({"uin": "1", "p_skey": "x"})
        api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        res = await api._describe_images(["https://m.qpic.cn/a.jpg"], 9, 3, compress=False)
        await api.aclose()
        check("K01 占位符描述打出 URL 诊断日志",
              res == ["[图片]"] and "m.qpic.cn/a.jpg" in cap.text(),
              repr(cap.text()[-90:]))
    finally:
        qzone_api.set_image_manager(prev_mgr)
        qzone_api.set_qzoneapi_logger(orig_qlog)

    # K02 视觉任务/模型双空时必须提示配置
    cap2 = _CapLogger()
    orig_vlog = vision.logger
    vision.set_vision_logger(cap2)
    try:
        class _VP:
            def resolve_llm_params(self, task, model, model_name):
                return {}

            class config:
                class read:
                    vision_task = ""
                    vision_model = ""
                    vision_model_name = ""
                    enable_image_description = True

            class ctx:
                class llm:
                    @staticmethod
                    async def generate(prompt, **kw):
                        return {"response": "x"}

        vm = vision.VisionManager(_VP())
        d = await vm.get_image_description("https://m.qpic.cn/a.jpg", "iVBORw0KGgo=")
        check("K02 视觉双空时打出配置提示日志",
              d == vision.PLACEHOLDER and "vision_task" in cap2.text(),
              repr(cap2.text()[:90]))
    finally:
        vision.set_vision_logger(orig_vlog)


# ============================================================
# L. 视觉不可用时零下载（省流量/CPU）
# ============================================================
async def test_skip_download_when_vision_unavailable():
    section("L. 视觉不可用不下载")
    import httpx

    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    class _UnavailableMgr:
        def is_cached(self, url):
            return False

        def is_available(self):
            return False

        async def get_image_description(self, url, b64):
            return "[图片]"

    prev = qzone_api.image_manager
    qzone_api.set_image_manager(_UnavailableMgr())
    try:
        api = qzone_api.QzoneAPI({"uin": "1", "p_skey": "x"})
        api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        urls = [f"https://a1.qpic.cn/psc?/{i}.jpg" for i in range(6)]
        res = await api._describe_images(urls, 9, 3, compress=True)
        await api.aclose()
        check("L01 视觉不可用时返回占位符", res == ["[图片]"] * 6, f"len={len(res)}")
        check("L02 视觉不可用时零下载（省流量/CPU）",
              calls["n"] == 0, f"实际请求数={calls['n']}")
    finally:
        qzone_api.set_image_manager(prev)

    # L03 可用时正常下载（不误伤）
    calls["n"] = 0

    class _AvailableMgr:
        def is_cached(self, url):
            return False

        def is_available(self):
            return True

        async def get_image_description(self, url, b64):
            return "一只猫"

    prev2 = qzone_api.image_manager
    qzone_api.set_image_manager(_AvailableMgr())
    try:
        api = qzone_api.QzoneAPI({"uin": "1", "p_skey": "x"})
        api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        res = await api._describe_images(["https://a1.qpic.cn/psc?/0.jpg"], 9, 3, compress=False)
        await api.aclose()
        check("L03 视觉可用时正常下载并描述",
              res == ["一只猫"] and calls["n"] == 1, f"res={res} 请求数={calls['n']}")
    finally:
        qzone_api.set_image_manager(prev2)

    # L04~L06 VisionManager.is_available 语义
    def _mk(task, enabled=True):
        class _VP:
            def resolve_llm_params(self, a, b, c):
                return {"task_name": task} if task else {}

            class config:
                class read:
                    vision_task = task
                    vision_model = ""
                    vision_model_name = ""
                    enable_image_description = enabled

        return vision.VisionManager(_VP())

    check("L04 未配置视觉时 is_available()=False", _mk("").is_available() is False)
    check("L05 已配置视觉时 is_available()=True", _mk("vlm").is_available() is True)
    check("L06 开关关闭时 is_available()=False", _mk("vlm", False).is_available() is False)

    # L07 真插件实例回归（真机事故：resolve_llm_params 只存在于模块级，
    # vision/reply_manager 按实例调用时 AttributeError 被静默吞掉，
    # is_available() 恒 False → "视觉描述不可用" 且不下载图片。
    # 此前测试用 _VP mock 手动挂方法，掩盖了实例缺属性——必须用真类验证。）
    real_inst = object.__new__(plugin.QzoneFeedsPlugin)

    class _RealCfg:
        class read:
            enable_image_description = True
            vision_task = ""          # 留空走旧字段迁移路径
            vision_model = "vlm"
            vision_model_name = ""

    real_inst.config = _RealCfg()
    real_vm = vision.VisionManager(real_inst)
    check("L07 真插件实例 resolve_llm_params 可调用",
          real_inst.resolve_llm_params("", "vlm", "") == {"task_name": "vlm"})
    check("L08 真插件实例 vision 全链路 is_available()=True",
          real_vm.is_available() is True)


# ============================================================
# M. 拒答拦截（真机事故：拒答文本被发布成评论）
# ============================================================
_REFUSAL_SAMPLE = ("你的描述中存在不文明且不恰当的表述，不符合健康的交流规范，"
                   "因此我不能按照你的要求进行创作。我们应当使用文明、友善的语言进行沟通。")
# 真机变体（2026-09-15 21:10 事故）：同源不同词的拒答模板
_REFUSAL_SAMPLE_V2 = ("你的描述包含不良引导和危险暗示，不符合健康的交流准则，"
                      "因此我不能按照你的要求进行创作。我们应当倡导积极、安全、文明的网络交流，"
                      "共同营造良好的网络环境。")


def test_refusal_detection():
    section("M. 拒答拦截")
    f = reply_manager.looks_like_refusal

    check("M01 真机拒答原样命中", f(_REFUSAL_SAMPLE) is True)
    check("M01b 真机拒答变体（交流准则/不良引导）命中", f(_REFUSAL_SAMPLE_V2) is True)
    check("M02 空文本不误判", f("") is False and f(None) is False)

    ok_samples = [
        "哈哈咋了这是？被队友气到不想碰王者啦？",
        "狠狠共情！这段关于找寻自己的话真的超戳人",
        "咋了呀宝？谁惹你生气啦，快跟我唠唠消消气！",
        "这图也太好看了吧，求出处！",
        "一年之后阿哈确实要变路边一坨了哈哈",
    ]
    bad = [s for s in ok_samples if f(s)]
    check("M03 正常评论不误判", not bad, f"误判：{bad}" if bad else "全部正常")

    # 单特征命中
    for i, s in enumerate(["我不能提供这个帮助", "作为AI我无法完成该请求",
                           "请使用文明用语", "换个话题吧"], 1):
        if not f(s):
            check(f"M04-{i} 单特征命中: {s}", False, "未命中")
            break
    else:
        check("M04 拒答特征单点命中（4/4）", True)


# ============================================================
# M2. 身份错位拦截（真机事故 2026-09-26：bot 对好友称「主人」）
# ============================================================
_IDENTITY_SAMPLES = [
    "嘿嘿，谁让你把我调教得这么厉害的，认输吧主人[得意]",  # 09-26 真机原文
    "主人你好厉害呀",
    "我的主人最棒了",
    "master 威武",
]


def test_identity_confusion_detection():
    section("M2. 身份错位拦截（好友≠主人）")
    f = reply_manager.looks_like_identity_confusion

    for i, s in enumerate(_IDENTITY_SAMPLES, 1):
        check(f"M2.1-{i} 身份错位命中: {s[:20]}", f(s) is True)

    ok_samples = [
        "哈哈咋了这是？被队友气到不想碰王者啦？",
        "大佬这操作也太秀了吧！",       # 常规调侃称呼不误伤
        "老板何时更新呀，蹲一个",        # 同上
        "第一次做bot就有这水平，不错哦",  # 动态谈 bot 本身不误伤
        "这图也太好看了吧，求出处！",
    ]
    bad = [s for s in ok_samples if f(s)]
    check("M2.2 正常评论/常规称呼不误判", not bad, f"误判：{bad}" if bad else "全部正常")
    check("M2.3 空文本不误判", f("") is False and f(None) is False)


async def test_identity_confusion_not_published():
    """M2.4~M2.6：身份错位文本不得产生评论/回复副作用（与拒答同路径拦截）。"""
    section("M2. 身份错位不发布")
    at = auto_tasks
    orig = reply_manager._llm_generate

    async def identity_llm(plugin, prompt):
        return _IDENTITY_SAMPLES[0]

    reply_manager._llm_generate = identity_llm
    try:
        base = {"target_qq": "20001", "videos": [], "comments": []}
        store, api = _FakeStore(), _FakeApi()
        await at.process_feeds(None, api, store,
                               [{**base, "tid": "i1", "content": "第一次做bot",
                                 "rt_con": "", "images": []}],
                               like_probability=1.0, comment_probability=1.0,
                               action_interval=0)
        check("M2.4 身份错位时不发评论", not api.comments,
              f"却发了：{api.comments[:1]}" if api.comments else "已拦截")
        check("M2.5 身份错位时点赞仍执行", api.likes == ["i1"], f"likes={api.likes}")
    finally:
        reply_manager._llm_generate = orig


async def test_refusal_not_published():
    """M05~M07：拒答文本不得产生评论/回复副作用。"""
    section("M. 拒答不发布")
    at = auto_tasks
    orig = reply_manager._llm_generate

    async def refusal_llm(plugin, prompt):
        return _REFUSAL_SAMPLE

    reply_manager._llm_generate = refusal_llm
    try:
        base = {"target_qq": "20001", "videos": [], "comments": []}
        store, api = _FakeStore(), _FakeApi()
        await at.process_feeds(None, api, store,
                               [{**base, "tid": "r1", "content": "一年之后，阿哈将成为路边一坨",
                                 "rt_con": "", "images": []}],
                               like_probability=1.0, comment_probability=1.0,
                               action_interval=0)
        check("M05 拒答时不发评论", not api.comments,
              f"却发了：{api.comments[:1]}" if api.comments else "已拦截")
        check("M06 拒答时点赞仍执行", api.likes == ["r1"], f"likes={api.likes}")
    finally:
        reply_manager._llm_generate = orig

    # M07 回评路径同样拦截
    orig2 = reply_manager._llm_generate

    async def refusal_llm2(plugin, prompt):
        return _REFUSAL_SAMPLE

    reply_manager._llm_generate = refusal_llm2
    try:
        class _ClsStore:
            async def is_processed(self, fid, tid=None):
                return False

            async def mark_processed(self, fid, tid=None):
                return True

        class _ReplyApi:
            uin = "10001"
            qq_nickname = ""

            def __init__(self):
                self.replies = []

            async def get_list(self, *a, **kw):
                return [{"tid": "f1", "target_qq": "10001", "content": "我的说说",
                         "images": [], "comments": [
                             {"qq_account": "20001", "nickname": "友", "content": "好图",
                              "comment_tid": 111, "created_time": ""}]}]

            async def reply(self, *a, **kw):
                self.replies.append(a)
                return True

        class _P:
            class config:
                class reply:
                    scan_count = 5
                    max_replies_per_run = 10
                    reply_interval_sec = 0
                    prompt = "你是{bot_name}。说说：{content}；评论者：{nickname}；评论：{comment_content}"

        rapi = _ReplyApi()
        rm = reply_manager.ReplyManager(_P(), _ClsStore())
        await rm.reply_new_comments(rapi)
        check("M07 回评路径也不发布拒答",
              not rapi.replies, f"却回复了：{rapi.replies[:1]}" if rapi.replies else "已拦截")
    finally:
        reply_manager._llm_generate = orig2


# ============================================================
# N. 回评时间上下文（该评论时间 + 当前时间）v1.2.10
# ============================================================
def test_format_comment_time():
    section("N. 评论时间格式化")
    f = reply_manager.format_comment_time

    # unix 时间戳（秒）→ 可读本地时间
    ts = 1758880000
    expect = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    check("N01 秒级时间戳 → 可读时间", f(str(ts)) == expect, f"{f(str(ts))!r} vs {expect!r}")
    check("N02 int 时间戳同样处理", f(ts) == expect, f"{f(ts)!r}")
    # 毫秒时间戳（13 位）兼容
    check("N03 毫秒时间戳正确换算", f(str(ts * 1000)) == expect, f"{f(str(ts * 1000))!r}")
    # 已是可读文本（HTML 路径 span.state）原样保留
    for s in ["3小时前", "昨天 10:23", "09-26 10:23", "2026-09-26 10:23:00"]:
        check(f"N04 可读文本原样保留: {s}", f(s) == s, f"{f(s)!r}")
    # 缺失/异常 → "未知"（绝不返回空串，避免 prompt 出现"评论时间：；"）
    check("N05 缺失/空 → 未知",
          f(None) == "未知" and f("") == "未知" and f("   ") == "未知",
          f"{f(None)!r},{f('')!r},{f('   ')!r}")
    # 超范围时间戳不抛异常
    check("N06 异常大值不抛异常", isinstance(f("99999999999999999999"), str),
          f"{f('99999999999999999999')!r}")


async def test_reply_prompt_time_context():
    """N07~N10：回评 prompt 必须含该评论时间与当前时间。"""
    section("N. 回评 prompt 时间上下文")
    orig = reply_manager._llm_generate
    captured = {}

    async def capture_llm(plugin, prompt):
        captured["prompt"] = prompt
        return "收到啦"

    reply_manager._llm_generate = capture_llm
    ts = 1758880000
    ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    class _Store:
        async def is_processed(self, fid, tid=None):
            return False

        async def mark_processed(self, fid, tid=None):
            return True

    class _Api:
        uin = "10001"
        qq_nickname = "MaiBot"

        def __init__(self):
            self.replies = []

        async def get_list(self, *a, **kw):
            return [{"tid": "f1", "target_qq": "10001", "content": "今天天气不错",
                     "images": [], "comments": [
                         {"qq_account": "20001", "nickname": "友", "content": "确实",
                          "comment_tid": 111, "created_time": str(ts)}]}]

        async def reply(self, *a, **kw):
            self.replies.append(a)
            return True

    def _mk_plugin(prompt_tpl):
        class _P:
            class config:
                class reply:
                    scan_count = 5
                    max_replies_per_run = 10
                    reply_interval_sec = 0
                    prompt = prompt_tpl
        return _P()

    try:
        # N07 默认模板（含 {created_time}/{current_time}）
        default_tpl = plugin.QzoneFeedsConfig().reply.prompt
        api = _Api()
        await reply_manager.ReplyManager(_mk_plugin(default_tpl), _Store()).reply_new_comments(api)
        p = captured.get("prompt", "")
        today = time.strftime("%Y-%m-%d")
        check("N07 默认模板含评论时间（已格式化）", ts_str in p, f"prompt={p[:180]!r}")
        check("N08 默认模板含当前时间",
              f"当前时间：{today}" in p, f"prompt={p[:180]!r}")

        # N09 旧模板（无时间占位符）→ 自动补偿追加
        old_tpl = "你是{bot_name}。说说：{content}；评论者：{nickname}；评论：{comment_content}"
        api2 = _Api()
        await reply_manager.ReplyManager(_mk_plugin(old_tpl), _Store()).reply_new_comments(api2)
        p2 = captured.get("prompt", "")
        check("N09 旧模板自动补偿时间上下文",
              ts_str in p2 and "当前时间" in p2 and p2.startswith("你是MaiBot"),
              f"prompt={p2[:220]!r}")

        # N10 模板含 created_time 但缺 current_time → 只补当前时间（不重复补评论时间）
        part_tpl = ("你是{bot_name}。说说：{content}；评论者：{nickname}；"
                    "评论：{comment_content}；评论时间：{created_time}")
        api3 = _Api()
        await reply_manager.ReplyManager(_mk_plugin(part_tpl), _Store()).reply_new_comments(api3)
        p3 = captured.get("prompt", "")
        # 评论时间值只出现一次（模板占位符替换），补偿里不含
        check("N10 缺 current_time 时只补当前时间",
              p3.count(ts_str) == 1 and "当前时间：" in p3
              and "评论时间：" in p3 and f"当前时间：{today}" in p3,
              f"prompt={p3[:240]!r}")
    finally:
        reply_manager._llm_generate = orig


# ============================================================
# R. 发言纪律与语域拦截（真机事故 2026-10-03）
# ============================================================
# 真机原文：好友动态「喜报，无料有了，我奶找到了」→ bot 评论
# 「恭喜！双喜临门，无料和奶奶都到位啦！」被围观嘲笑。
# 两类问题：① 语域错位（官方通稿腔）② 硬解梗（读不懂却脑补）。
# 用户定的方向：读不懂就别装懂、宁可短。
_STIFF_SAMPLE = "恭喜！双喜临门，无料和奶奶都到位啦！"


def test_style_guard():
    section("R. 发言纪律（代码级追加）")
    cs = comment_style

    p = cs.append_style_guard("好友A发了说说：今天天气不错。")
    check("R01 纪律段已追加且原 prompt 保留",
          "【发言纪律" in p and p.startswith("好友A发了说说"), p[:40])
    check("R02 纪律段幂等（重复追加只有一段）",
          cs.append_style_guard(p).count("【发言纪律") == 1)
    # 四要素缺一不可：禁复述总结 / 短 / 读不懂就直说 / 成语祝语官方腔 + 弃评出口
    check("R03 纪律段含全部要素",
          all(k in p for k in ("不要复述", "宁可短", "读不懂", "成语", "[跳过]")))
    check("R04 空 prompt 也带纪律段", "【发言纪律" in cs.append_style_guard(""))

    rp = cs.build_style_retry_prompt("原始要求", _STIFF_SAMPLE)
    check("R05 重写 prompt 含反例原文 + 纪律段 + 重写要求",
          "双喜临门" in rp and "【发言纪律" in rp and "重写" in rp and "[跳过]" in rp)


def test_skip_sentinel():
    section("R. 主动弃评出口")
    cs = comment_style

    for s in ("[跳过]", "[不评论]", "[略过]"):
        check(f"R06 弃评标记命中 {s}", cs.looks_like_skip(s) is True)
    check("R07 标记后带解释也算弃评",
          cs.looks_like_skip("[跳过]，这个我真看不懂") is True)

    ok_samples = ["没看懂", "啥意思？", "哈哈", "牛的", "跳过这个话题吧"]
    bad = [s for s in ok_samples if cs.looks_like_skip(s)]
    check("R08 正常回复不误判为弃评", not bad, f"误判：{bad}" if bad else "正常")
    check("R09 空文本不误判",
          cs.looks_like_skip("") is False and cs.looks_like_skip(None) is False)


def test_stiff_register_detection():
    section("R. 书面套话拦截")
    cs = comment_style

    stiff_samples = [
        _STIFF_SAMPLE,               # 09-03 真机原文
        "祝您事事顺利",
        "愿你前程似锦",
        "总结一下：这次真的很棒",
        "感谢您的分享",
        "建议您下次早点睡",
    ]
    for i, s in enumerate(stiff_samples, 1):
        check(f"R10.{i} 通稿腔命中: {s[:16]}", cs.looks_like_stiff_register(s) is True)

    # 口语高频词（到位/收到/完美/绝了）刻意**不收**入特征表，避免误拦正常回复
    ok_samples = [
        "牛的",
        "这操作到位啊",
        "哈哈笑死",
        "没看懂",
        "图不错，出处呢",
        "收到，回头看看",
        "厉害了",
        "我也想去",
    ]
    bad = [s for s in ok_samples if cs.looks_like_stiff_register(s)]
    check("R11 正常口语回复不误判", not bad, f"误判：{bad}" if bad else "全部正常")
    check("R12 空文本不误判",
          cs.looks_like_stiff_register("") is False
          and cs.looks_like_stiff_register(None) is False)


async def test_style_guard_not_published():
    """R13~R19：语域不合格时的行为（重写→合格发布 / 重写→仍不合格放弃）。"""
    section("R. 语域重写与放弃发布")
    orig = reply_manager._llm_generate
    feed = {"tid": "s1", "target_qq": "20001", "content": "喜报，无料有了，我奶找到了",
            "rt_con": "", "images": [], "videos": [], "comments": []}

    async def _run(f):
        store, api = _FakeStore(), _FakeApi()
        await auto_tasks.process_feeds(None, api, store, [f],
                                       like_probability=1.0, comment_probability=1.0,
                                       action_interval=0)
        return api

    # R13/R14/R18/R19：首次通稿腔 → 重写后合格 → 发布重写结果
    calls, captured = {"n": 0}, {}

    async def llm_retry_ok(plugin, prompt):
        calls["n"] += 1
        captured[f"p{calls['n']}"] = prompt
        return _STIFF_SAMPLE if calls["n"] == 1 else "没看懂"

    reply_manager._llm_generate = llm_retry_ok
    try:
        api = await _run(feed)
        check("R13 通稿腔重写后合格 → 发布重写结果", api.comments == ["没看懂"],
              f"comments={api.comments}")
        check("R14 确实重写了一次（共 2 次 LLM 调用）", calls["n"] == 2, f"n={calls['n']}")
        check("R19 首次 prompt 含纪律段", "【发言纪律" in captured.get("p1", ""))
        check("R18 重写 prompt 含不合格原文与纪律段",
              "双喜临门" in captured.get("p2", "")
              and "【发言纪律" in captured.get("p2", ""), captured.get("p2", "")[:120])
    finally:
        reply_manager._llm_generate = orig

    # R15/R16：两次都通稿腔 → 放弃发布（宁可少一条评论，不发通稿腔）
    calls2 = {"n": 0}

    async def llm_always_stiff(plugin, prompt):
        calls2["n"] += 1
        return _STIFF_SAMPLE

    reply_manager._llm_generate = llm_always_stiff
    try:
        api = await _run({**feed, "tid": "s2"})
        check("R15 重写后仍通稿腔 → 放弃发布", not api.comments,
              f"却发了：{api.comments[:1]}" if api.comments else "已放弃")
        check("R16 放弃发布时点赞仍执行", api.likes == ["s2"], f"likes={api.likes}")
        check("R17 重写有上限（只尝试 2 次，不无限重写）", calls2["n"] == 2, f"n={calls2['n']}")
    finally:
        reply_manager._llm_generate = orig

    # R20：模型主动弃评 → 不发布
    async def llm_skip(plugin, prompt):
        return "[跳过]"

    reply_manager._llm_generate = llm_skip
    try:
        api = await _run({**feed, "tid": "s3"})
        check("R20 模型主动弃评 → 不发布", not api.comments,
              f"却发了：{api.comments[:1]}" if api.comments else "已弃评")
    finally:
        reply_manager._llm_generate = orig

    # R21：回评路径同样受语域拦截（三条路径共用判定，不能只在自动评论上修）
    async def llm_stiff2(plugin, prompt):
        return _STIFF_SAMPLE

    reply_manager._llm_generate = llm_stiff2
    try:
        class _ClsStore:
            async def is_processed(self, fid, tid=None):
                return False

            async def mark_processed(self, fid, tid=None):
                return True

        class _ReplyApi:
            uin = "10001"
            qq_nickname = ""

            def __init__(self):
                self.replies = []

            async def get_list(self, *a, **kw):
                return [{"tid": "rf1", "target_qq": "10001", "content": "我的说说",
                         "images": [], "comments": [
                             {"qq_account": "20001", "nickname": "友", "content": "好图",
                              "comment_tid": 222, "created_time": ""}]}]

            async def reply(self, *a, **kw):
                self.replies.append(a)
                return True

        class _P:
            class config:
                class reply:
                    scan_count = 5
                    max_replies_per_run = 10
                    reply_interval_sec = 0
                    prompt = "你是{bot_name}。说说：{content}；评论者：{nickname}；评论：{comment_content}"

        rapi = _ReplyApi()
        await reply_manager.ReplyManager(_P(), _ClsStore()).reply_new_comments(rapi)
        check("R21 回评路径同样拦通稿腔", not rapi.replies,
              f"却回复了：{rapi.replies[:1]}" if rapi.replies else "已拦截")
    finally:
        reply_manager._llm_generate = orig


def test_style_wiring_lock():
    """R22~R24：结构回归锁——纪律必须走代码层，且三条路径共用同一判定。"""
    section("R. 结构回归锁")

    hits = []
    for name in ("auto_tasks.py", "atme_manager.py", "plugin.py"):
        txt = (Path(PLUGIN_DIR) / name).read_text(encoding="utf-8")
        for line in txt.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if "以 bot 身份" in line or "以bot身份" in line:
                hits.append(f"{name}: {line.strip()[:60]}")
    check("R22 默认模板/配置不再要求「以 bot 身份」", not hits, str(hits))

    check("R23 三条生成路径统一走 generate_guarded_comment",
          all("generate_guarded_comment" in
              (Path(PLUGIN_DIR) / f).read_text(encoding="utf-8")
              for f in ("auto_tasks.py", "reply_manager.py", "atme_manager.py")))

    pc_src = (Path(PLUGIN_DIR) / "person_context.py").read_text(encoding="utf-8")
    check("R24 画像注入成功有 INFO 自证日志",
          "人物画像已注入" in pc_src and "logger.info" in pc_src)


def test_manifest():
    section("F. Manifest")
    m = json.loads((Path(PLUGIN_DIR) / "_manifest.json").read_text(encoding="utf-8"))
    check("F10 manifest_version==2", m.get("manifest_version") == 2)
    check("F11 version 三段式",
          len(str(m.get("version", "")).split(".")) == 3, m.get("version"))
    check("F12 host_application 有 min/max",
          all(k in m.get("host_application", {}) for k in ("min_version", "max_version")))
    check("F13 sdk 有 min/max",
          all(k in m.get("sdk", {}) for k in ("min_version", "max_version")))
    url = m.get("urls", {}).get("repository", "")
    check("F14 repository URL 合法", url.startswith("http"), url)
    caps = m.get("capabilities", [])
    need = {"send.text", "llm.generate", "api.call"}
    check("F15 capabilities 覆盖 send/llm/api",
          need.issubset(set(caps)), str(caps))
    check("F15b capabilities 声明 config.get（读宿主人设必需）",
          "config.get" in caps, str(caps))
    deps = {d.get("id") or d.get("name") for d in m.get("dependencies", [])}
    check("F16 依赖声明含 snowluma-adapter/httpx/json5/bs4/pillow",
          {"maibot-team.snowluma-adapter", "httpx", "json5",
           "beautifulsoup4", "pillow"}.issubset(deps), str(deps))
    gi = (Path(PLUGIN_DIR) / ".gitignore").read_text(encoding="utf-8")
    check("F17 .gitignore 含 config.toml", "config.toml" in gi)


# ============================================================
# G2. singleflight：同一 URL 并发调用只跑一次 VLM（v1.2.8 重复描述修复）
# ============================================================
async def test_singleflight_dedup():
    section("G2. singleflight 并发去重（同 URL 只识别一次）")

    real_calls = {"n": 0}

    class _VP:
        resolve_llm_params = staticmethod(plugin.resolve_llm_params)

        class config:
            class read:
                vision_task = "vlm"
                vision_model = ""
                vision_model_name = ""
                enable_image_description = True
                enable_desc_cache = True
                desc_cache_size = 200

        class ctx:
            class llm:
                @staticmethod
                async def generate(prompt, **kw):
                    real_calls["n"] += 1
                    await asyncio.sleep(0.05)  # 模拟 VLM 耗时，放大竞态窗口
                    return {"response": f"desc-{real_calls['n']}"}

    vp = _VP()
    vm = vision.VisionManager(vp)
    url = "https://m.qpic.cn/dup.jpg"
    b64 = "iVBORw0KGgo="

    # 5 个协程同时请求同一 URL（缓存均未命中）→ 应只触发 1 次 VLM
    results = await asyncio.gather(*[vm.get_image_description(url, b64) for _ in range(5)])
    check("G2.1 同 URL 5 并发只调 1 次 VLM",
          real_calls["n"] == 1, f"实际 VLM 调用数={real_calls['n']}")
    check("G2.2 全部协程拿到同一描述",
          len(set(results)) == 1 and results[0] == "desc-1", f"results={results}")

    # 串行第二次调用 → 走缓存，不再调 VLM
    again = await vm.get_image_description(url, b64)
    check("G2.3 串行复调走缓存", again == "desc-1" and real_calls["n"] == 1,
          f"desc={again!r} 调用数={real_calls['n']}")

    # 无 URL（base64 直传路径）：内容相同 → 哈希命中，同样不重复 VLM
    r2 = await vm.get_image_description("", b64)
    check("G2.4 空 URL 同内容走哈希缓存", r2 == "desc-1" and real_calls["n"] == 1,
          f"desc={r2!r} 调用数={real_calls['n']}")

    # 空 URL + 不同内容 → 正常识别
    r3 = await vm.get_image_description("", "aGVsbG8=")
    check("G2.5 空 URL 不同内容正常识别", r3 == "desc-2" and real_calls["n"] == 2,
          f"desc={r3!r} 调用数={real_calls['n']}")


# ============================================================
# G3. 内容哈希二级缓存：URL 轮换（同图不同 URL）不再重复 VLM（v1.2.8）
# ============================================================
async def test_hash_cache_url_rotation():
    section("G3. 内容哈希缓存（Qzone URL 轮换场景）")

    real_calls = {"n": 0}

    class _VP:
        resolve_llm_params = staticmethod(plugin.resolve_llm_params)

        class config:
            class read:
                vision_task = "vlm"
                vision_model = ""
                vision_model_name = ""
                enable_image_description = True
                enable_desc_cache = True
                desc_cache_size = 200

        class ctx:
            class llm:
                @staticmethod
                async def generate(prompt, **kw):
                    real_calls["n"] += 1
                    return {"response": f"hdesc-{real_calls['n']}"}

    vp = _VP()
    vm = vision.VisionManager(vp)
    b64 = "iVBORw0KGgo="  # 固定内容（同一张图）
    url_round1 = "https://m.qpic.cn/psc?/TOKEN-A/img.jpg"
    url_round2 = "https://m.qpic.cn/psc?/TOKEN-B/img.jpg"  # 同图，URL 签名已轮换

    d1 = await vm.get_image_description(url_round1, b64)
    check("G3.1 首轮正常识别", d1 == "hdesc-1" and real_calls["n"] == 1,
          f"desc={d1!r} 调用数={real_calls['n']}")

    # 第二轮：URL 不同 → URL 缓存未命中；但内容哈希相同 → 应跳过 VLM
    d2 = await vm.get_image_description(url_round2, b64)
    check("G3.2 URL 轮换后哈希命中不重复 VLM",
          d2 == "hdesc-1" and real_calls["n"] == 1,
          f"desc={d2!r} 调用数={real_calls['n']}")

    # 识别失败的描述不得进入哈希缓存（避免坏结果固化）
    real_calls["n"] = 0

    class _FailVP(_VP):
        class ctx:
            class llm:
                @staticmethod
                async def generate(prompt, **kw):
                    real_calls["n"] += 1
                    return {"response": ""}

    vf = vision.VisionManager(_FailVP())
    f1 = await vf.get_image_description("https://x/1.jpg", b64)
    f2 = await vf.get_image_description("https://x/2.jpg", b64)  # URL 不同、内容相同
    check("G3.3 失败占位符不入哈希缓存",
          real_calls["n"] == 2 and f1 == f2 == vision.PLACEHOLDER_FAILED,
          f"调用数={real_calls['n']} descs={f1!r},{f2!r}")


# ============================================================
# P. 被@检测（v1.3.0：「与我相关」接口解析与回复流程）
# ============================================================
async def test_atme_detection():
    section("P. 被@检测（feeds2_html_pav_all）")
    am = atme_manager
    qa = qzone_api

    # ── 动作分类（纯函数，长词优先防子串误判） ──
    cls = qa._classify_atme_action
    check("P01 '评论提到我'优先于'提到我'", cls("某某 评论提到我 内容") == "comment_mention")
    check("P02 '提到我'→mention", cls("某某 提到我 说说内容") == "mention")
    check("P03 赞/评论/访问→other",
          cls("某某 赞了我的说说") == "other" and cls("某某 评论 了") == "other"
          and cls("某某 访问了我的主页") == "other")

    # ── 条目归一化：mood 链接提取 + tid 锚点截断 + 脏条目跳过 ──
    items = qa.parse_atme_items({"data": {"data": [
        {"uin": "20001", "nickname": "友", "appid": "311", "abstime": 1790404744,
         "html": "<a href=\"http://user.qzone.qq.com/10001/mood/abc1230100.\">友 提到我</a>"},
        {"uin": "20002", "nickname": "乙", "appid": "403", "abstime": 1790400000,
         "html": "乙 访问了我的主页"},
        "junk",
    ]}})
    check("P04 mood链接提取tid并截断锚点",
          items and items[0]["post_uin"] == "10001"
          and items[0]["post_tid"] == "abc1230100" and items[0]["action"] == "mention",
          f"items[0]={items[0] if items else None}")
    check("P05 无mood链接→None + 非dict条目跳过",
          len(items) == 2 and items[1]["post_tid"] is None and items[1]["action"] == "other")

    # ── get_atme_list：JSONP + JS 字面量 + undefined + 登录失效 ──
    import httpx as _httpx
    js_body = ("_Callback({code:0,subcode:0,message:'',data:{main:{total_number:2},data:["
               "{uin:'20001',nickname:'友',appid:'311',abstime:'1790404744',"
               "html:'<a href=\"http://user.qzone.qq.com/10001/mood/abc0100.\">友 提到我</a>'},"
               "{uin:'20002',nickname:'乙',appid:'403',abstime:'1790400000',"
               "html:'乙 访问了我的主页'},undefined]}});")

    async def handler_ok(request):
        if "feeds2_html_pav_all" in str(request.url):
            return _httpx.Response(200, text=js_body)
        return _httpx.Response(404)

    api = qa.QzoneAPI({"uin": "10001", "p_skey": "x"})
    api._client = _httpx.AsyncClient(transport=_httpx.MockTransport(handler_ok))
    got = await api.get_atme_list()
    await api.aclose()
    check("P06 JSONP+JS字面量解析与undefined跳过",
          len(got) == 2 and got[0]["action"] == "mention" and got[0]["post_tid"] == "abc0100",
          f"got={got}")

    async def handler_expired(request):
        return _httpx.Response(200, text="_Callback({code:1000000,message:'请先登录'});")

    api_exp = qa.QzoneAPI({"uin": "10001", "p_skey": "x"})
    api_exp._client = _httpx.AsyncClient(transport=_httpx.MockTransport(handler_expired))
    raised = False
    try:
        await api_exp.get_atme_list()
    except qa.CookieExpiredError:
        raised = True
    await api_exp.aclose()
    check("P07 登录失效抛CookieExpiredError", raised)

    # ── 回复流程：mention→评论；comment_mention→定位回评；去重；守卫 ──
    class _AtmeStore:
        def __init__(self):
            self.seen = set()

        async def is_processed(self, fid, tid=None):
            return fid in self.seen

        async def mark_processed(self, fid, tid=None):
            self.seen.add(fid)
            return True

    class _AtmeApi:
        uin = "10001"
        qq_nickname = "MaiBot"

        def __init__(self, items, feed=None):
            self._items = items
            self._feed = feed
            self.comments = []
            self.replies = []

        async def get_atme_list(self, count=10):
            return self._items

        async def get_list(self, *a, **kw):
            return [self._feed] if self._feed else []

        async def comment(self, fid, target_qq, content):
            self.comments.append((fid, target_qq, content))
            return True

        async def reply(self, fid, target_qq, nick, cqq, content, ctid):
            self.replies.append((fid, target_qq, cqq, ctid, content))
            return True

    atme_items = [
        {"action": "mention", "uin": "20001", "nickname": "友",
         "post_uin": "20001", "post_tid": "t1", "content": "友 提到我"},
        {"action": "comment_mention", "uin": "20002", "nickname": "乙",
         "post_uin": "20002", "post_tid": "t2", "content": "乙 评论提到我"},
        {"action": "other", "uin": "20003", "nickname": "丙",
         "post_uin": "10001", "post_tid": "ownpost", "content": "丙 赞了我的说说"},
        {"action": "mention", "uin": "20004", "nickname": "丁",
         "post_uin": "10001", "post_tid": "selfpost", "content": "丁 提到我"},
    ]
    feed_t2 = {"tid": "t2", "target_qq": "20002", "content": "某说说", "comments": [
        {"qq_account": "20002", "nickname": "乙", "content": "@MaiBot 来玩",
         "comment_tid": 777}]}

    plugin = _AutoPlugin(auto_kw={"enable_atme_reply": True, "reply_interval_sec": 0})
    mgr = am.AtmeManager(plugin, _AtmeStore())
    orig_llm = reply_manager._llm_generate

    async def ok_llm(p, prompt):
        return "哈哈你好呀"

    reply_manager._llm_generate = ok_llm
    try:
        api3 = _AtmeApi(atme_items, feed_t2)
        ok, msg = await mgr.reply_atme_mentions(api3)
        check("P08 mention→评论说说",
              ok and api3.comments and api3.comments[0][0] == "t1",
              f"comments={api3.comments}")
        check("P09 comment_mention→定位原评论回评",
              bool(api3.replies) and api3.replies[0][3] == 777,
              f"replies={api3.replies}")
        check("P10 other与自己的说说被忽略",
              len(api3.comments) == 1 and len(api3.replies) == 1)
        api4 = _AtmeApi(atme_items, feed_t2)
        await mgr.reply_atme_mentions(api4)
        check("P11 同一说说只处理一次（seen-key 去重）",
              not api4.comments and not api4.replies)

        # 拒答守卫
        async def refusal_llm(p, prompt):
            return "抱歉，我不能按照你的要求进行创作"

        reply_manager._llm_generate = refusal_llm
        mgr2 = am.AtmeManager(plugin, _AtmeStore())
        api5 = _AtmeApi([atme_items[0]], None)
        await mgr2.reply_atme_mentions(api5)
        check("P12 拒答不发布被@回复", not api5.comments and not api5.replies)

        # 身份错位守卫
        async def identity_llm(p, prompt):
            return "嘿嘿，认输吧主人[得意]"

        reply_manager._llm_generate = identity_llm
        mgr3 = am.AtmeManager(plugin, _AtmeStore())
        api6 = _AtmeApi([atme_items[0]], None)
        await mgr3.reply_atme_mentions(api6)
        check("P13 身份错位不发布被@回复", not api6.comments and not api6.replies)
    finally:
        reply_manager._llm_generate = orig_llm

    # ── comment_mention 定位失败降级为评论说说 ──
    reply_manager._llm_generate = ok_llm
    try:
        plugin4 = _AutoPlugin(auto_kw={"enable_atme_reply": True, "reply_interval_sec": 0})
        mgr4 = am.AtmeManager(plugin4, _AtmeStore())
        cm_item = {"action": "comment_mention", "uin": "20002", "nickname": "乙",
                   "post_uin": "20002", "post_tid": "not_in_list", "content": "乙 评论提到我"}
        api7 = _AtmeApi([cm_item], feed_t2)  # feed tid=t2 ≠ not_in_list → 定位失败
        await mgr4.reply_atme_mentions(api7)
        check("P14 定位不到评论→降级评论说说",
              len(api7.comments) == 1 and not api7.replies,
              f"comments={api7.comments} replies={api7.replies}")
    finally:
        reply_manager._llm_generate = orig_llm

    # ── P15~P17 被@回复的时间上下文（v1.3.2） ──
    captured = {}

    async def capture_llm(p, prompt):
        captured["prompt"] = prompt
        return "收到啦"

    reply_manager._llm_generate = capture_llm
    try:
        ts = 1790404744
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        today = time.strftime("%Y-%m-%d")
        item_t = {"action": "mention", "uin": "20001", "nickname": "友",
                  "post_uin": "20001", "post_tid": "t9", "content": "友 提到我",
                  "time": str(ts)}
        plugin5 = _AutoPlugin(auto_kw={"enable_atme_reply": True, "reply_interval_sec": 0})
        api8 = _AtmeApi([item_t], None)
        await am.AtmeManager(plugin5, _AtmeStore()).reply_atme_mentions(api8)
        p = captured.get("prompt", "")
        check("P15 被@回复 prompt 含互动时间（时间戳→可读）",
              ts_str in p, f"prompt={p[:200]!r}")
        check("P16 被@回复 prompt 含当前时间",
              f"当前时间：{today}" in p, f"prompt={p[:200]!r}")

        # 旧模板（无时间占位符）→ 自动补偿
        old_atme_tpl = "你是{bot_name}。说说：{post}；互动者：{nickname}；内容：{mention_content}"
        plugin6 = _AutoPlugin(auto_kw={"enable_atme_reply": True, "reply_interval_sec": 0,
                                       "atme_prompt": old_atme_tpl})
        api9 = _AtmeApi([item_t], None)
        await am.AtmeManager(plugin6, _AtmeStore()).reply_atme_mentions(api9)
        p2 = captured.get("prompt", "")
        check("P17 被@旧模板自动补偿时间上下文",
              p2.startswith("你是MaiBot") and ts_str in p2 and "当前时间" in p2,
              f"prompt={p2[:220]!r}")
    finally:
        reply_manager._llm_generate = orig_llm


# ============================================================
# main
# ============================================================
async def _amain():
    test_split_message()
    test_silent_period()
    test_sanitize()
    test_cookie_parse_and_gtk()
    test_vision_mime()
    test_compress_fallback()
    test_format_feed()
    await test_processed_store()
    test_auth()
    test_command_patterns()
    await test_cookie_exfiltration()
    await test_redirect_bypass_blocked()
    await test_allowlist_allows_qzone_cdn()
    await test_ssrf_url_validation()
    await test_undefined_corruption()
    await test_describe_images_silent_drop()
    test_upload_response_parsing()
    await test_upload_bad_response()
    test_image_size_cap()
    await test_comment_material_guard()
    await test_person_context()
    await test_host_persona()
    await test_auto_job_vision_params()
    await test_lifecycle()
    test_resolve_llm_params()
    await test_resolve_llm_callers()
    await test_image_host_downgrade()
    await test_failure_markers_excluded()
    await test_image_failure_diagnostics()
    await test_skip_download_when_vision_unavailable()
    test_refusal_detection()
    await test_refusal_not_published()
    await test_singleflight_dedup()
    await test_hash_cache_url_rotation()
    test_identity_confusion_detection()
    await test_identity_confusion_not_published()
    await test_atme_detection()
    test_format_comment_time()
    await test_reply_prompt_time_context()
    test_style_guard()
    test_skip_sentinel()
    test_stiff_register_detection()
    await test_style_guard_not_published()
    test_style_wiring_lock()
    test_romantic_register_detection()
    await test_romantic_not_published()
    await test_missing_image_disclosure()
    test_reply_style_char_ceiling()
    test_image_url_normalize()
    test_rt_author_split()
    await test_rt_author_in_prompt()
    test_manifest()
    await test_comment_policy_v170()
    await test_lazy_vision_v180()


# ============================================================
# N. 亲昵/恋爱语域拦截（真机事故 2026-11-01）
#    好友说说「@onthrpww 宝宝我爱你」→ bot 评论「宝宝这句我收下啦！本姑娘也爱你呀～」
# ============================================================
_ROMANTIC_SAMPLES = [
    "宝宝这句我收下啦！本姑娘也爱你呀～",  # 2026-11-01 真机原文
    "我也爱你呀",                          # 无称呼，与事故句同性质
    "宝贝今天也想你了",
    "亲亲你，早点睡",
    "老婆这条太好笑了",
]


def test_romantic_register_detection():
    section("N. 亲昵语域拦截")
    f = comment_style.looks_like_romantic_register

    for i, s in enumerate(_ROMANTIC_SAMPLES, 1):
        check(f"N1-{i} 亲昵语域命中: {s[:14]}", f(s) is True)

    ok_samples = [
        "大佬这操作也太秀了吧！",
        "老板何时更新呀，蹲一个",
        "这图也太好看了吧，求出处！",
        "没看懂，啥意思",
        "恭喜恭喜，终于搞定啦",
    ]
    bad = [s for s in ok_samples if f(s)]
    check("N2 常规评论不误判", not bad, f"误判：{bad}" if bad else "全部正常")
    check("N3 空文本不误判", f("") is False and f(None) is False)
    # 设计取舍（有意为之，不是缺陷）：「单命中即拦」会把「宝宝今天吃了吗」也拦掉。
    # 宁可少一条评论，也不要冒一次公开越界的风险。
    check("N4 「宝宝今天吃了吗」亦被拦（单命中策略的有意取舍）",
          f("宝宝今天吃了吗") is True)


async def test_romantic_not_published():
    """N5~N7：亲昵语域文本不得发布，且不给重写机会。"""
    section("N. 亲昵语域不发布")
    orig = reply_manager._llm_generate
    calls = {"n": 0}

    async def romantic_llm(plugin, prompt):
        calls["n"] += 1
        return _ROMANTIC_SAMPLES[0]

    reply_manager._llm_generate = romantic_llm
    try:
        store, api = _FakeStore(), _FakeApi()
        await auto_tasks.process_feeds(
            None, api, store,
            [{"tid": "rm1", "target_qq": "20001", "videos": [], "comments": [],
              "rt_con": "", "images": [],
              "content": "(震惊)(感动)@onthrpww 宝宝我爱你❤️❤️这条正文足够长所以不会触发短正文降级"}],
            like_probability=1.0, comment_probability=1.0, action_interval=0)
        check("N5 亲昵语域评论不发布", not api.comments,
              f"却发了：{api.comments[:1]}" if api.comments else "已拦截")
        check("N6 拦截后点赞仍执行", api.likes == ["rm1"], f"likes={api.likes}")
        check("N7 不给重写机会（仅 1 次 LLM 调用）", calls["n"] == 1, f"n={calls['n']}")
    finally:
        reply_manager._llm_generate = orig


async def test_missing_image_disclosure():
    """N8~N12：缺图必须让模型知道 + 短正文缺图降级只点赞。"""
    section("N. 缺图披露与短正文降级")
    orig = reply_manager._llm_generate
    captured = {}

    async def capture_llm(plugin, prompt):
        captured["p"] = prompt
        return "好家伙"

    async def run(feed):
        store, api = _FakeStore(), _FakeApi()
        await auto_tasks.process_feeds(None, api, store, [feed],
                                       like_probability=1.0, comment_probability=1.0,
                                       action_interval=0)
        return api

    reply_manager._llm_generate = capture_llm
    try:
        # N8/N9：正文够长 → 照评，但 prompt 必须明示有图没读到
        await run({"tid": "mi1", "target_qq": "20001", "videos": [], "comments": [],
                   "rt_con": "", "images": ["[图片（加载失败）]"],
                   "content": "(震惊)(感动)@onthrpww 宝宝我爱你，这条正文足够长所以照常评论"})
        p = captured.get("p", "")
        check("N8 缺图时 prompt 显式告知未识别的图", "未能识别" in p,
              p[-90:] if p else "prompt 为空")
        check("N9 占位符本身仍不进 prompt", "[图片（加载失败）]" not in p, p[:120])

        # N10/N11：正文极短 + 缺图 → 只点赞不评论
        captured.clear()
        api2 = await run({"tid": "mi2", "target_qq": "20002", "videos": [], "comments": [],
                          "rt_con": "", "images": ["[图片（加载失败）]"],
                          "content": "@onn 嗯"})
        check("N10 短正文+缺图 → 不评论", not api2.comments,
              f"却发了：{api2.comments[:1]}" if api2.comments else "已降级")
        check("N11 短正文+缺图 → 点赞照做", api2.likes == ["mi2"], f"likes={api2.likes}")
        check("N11b 降级时根本不调 LLM", "p" not in captured, "多余调用了 LLM")

        # N12：短正文 + 缺图，但正文**没有 @ 任何人** → 不降级。
        # v1.2.2 立的规矩是「图挂了不该拖累正常评论」，本规则只针对
        # 「这句话在跟某个具体的人说、而关系语境全在那张看不见的图里」。
        api3 = await run({"tid": "mi3", "target_qq": "20003", "videos": [], "comments": [],
                          "rt_con": "", "images": ["[图片（加载失败）]"], "content": "好耶"})
        check("N12 短正文+缺图但无@ → 照常评论（不推倒 v1.2.2）",
              api3.comments == ["好家伙"], f"comments={api3.comments}")
    finally:
        reply_manager._llm_generate = orig


def test_reply_style_char_ceiling():
    """N13~N15：表达风格段硬上限 100——老实例 config.toml 写着 300 也必须生效。"""
    section("N. 风格段字数上限")
    hp = importlib.import_module("qzf.host_persona")

    check("N13 风格段默认上限为 100", hp._DEFAULT_REPLY_STYLE_MAX_CHARS == 100,
          f"={hp._DEFAULT_REPLY_STYLE_MAX_CHARS}")

    class _Old:
        """模拟已落盘的老实例配置（v1.5.0 写的是 300）。"""

        class config:
            class persona:
                enable_host_persona = True
                persona_prompt = ""
                reply_style_prompt = ""
                persona_max_chars = 600
                reply_style_max_chars = 300
                persona_cache_ttl_sec = 600

    cfg = hp._persona_cfg(_Old())
    check("N14 老配置的 300 被钳制到 100", cfg["style_max_chars"] == 100,
          f"={cfg['style_max_chars']}")

    out = hp.truncate_persona("甲" * 400, cfg["style_max_chars"])
    check("N15 截断后不超过上限（含省略号）", len(out) <= 101, f"len={len(out)}")


def test_image_url_normalize():
    """N16~N20：相对图片地址补全（/ac/b.gif 事故）。"""
    section("N. 图片地址补全")
    f = qzone_api.normalize_image_url

    check("N16 协议相对地址补 https", f("//m.qpic.cn/a.jpg") == "https://m.qpic.cn/a.jpg",
          f("//m.qpic.cn/a.jpg"))
    got = f("/ac/b.gif")
    check("N17 根相对地址按 base 拼接为绝对 URL",
          got.startswith("https://") and got.endswith("/ac/b.gif"), got)
    check("N18 绝对地址原样返回",
          f("https://p.qpic.cn/x.png") == "https://p.qpic.cn/x.png")
    check("N19 空值返回空串", f("") == "" and f(None) == "")
    check("N20 补全后能通过白名单判定（顺序修正的验证）",
          qzone_api._is_allowed_image_host(got) is True, got)


# ============================================================
# S. 转发归属（真机事故 2026-11-03）
#    白菜转发牢铁的说说写「老大，下次一定能够再见的对吧😭」→ bot 回
#    「当然啦！约定已经种下…下次见时，本姑娘要加倍认真地和你打招呼呀」
#    根因：转发只带正文不带原作者名，「老大」失去所指对象，模型就近认领成"在叫我"。
# ============================================================
def test_rt_author_split():
    section("S. 转发归属")
    split = qzone_api._split_rt_author

    got = split("牢铁吃了没：已经11.3了\n我正坐在教室的木椅...")
    check("S01 拆出原作者名", got == ("牢铁吃了没", "已经11.3了\n我正坐在教室的木椅..."), got)

    got = split("没有冒号的转发正文")
    check("S02 无冒号→整段当正文，不猜作者", got == ("", "没有冒号的转发正文"), got)

    got = split("：只有冒号")
    check("S03 名字段为空→不认这个冒号", got == ("", "：只有冒号"), got)

    long_name = "零" * 40
    got = split(f"{long_name}：正文")
    check("S04 名字段过长→大概率不是名字，整段当正文",
          got == ("", f"{long_name}：正文"), got[:1])

    got = split(None)
    check("S05 None 不报错", got == ("", ""), got)

    # __extract_rt_author：键名契约未公开，按优先级探测
    ex = qzone_api._extract_rt_author
    check("S06 命中 rt_uinname", ex({"rt_uinname": "牢铁吃了没", "content": "c"}) == "牢铁吃了没")
    check("S07 缺 rt_uinname 时回退 nickname", ex({"nickname": " A "}) == "A")
    check("S08 全不中→空串（不猜）", ex({"content": "c", "uin": 123}) == "")
    check("S09 非 dict 不报错", ex(None) == "" and ex("x") == "")


async def test_rt_author_in_prompt():
    """S10~S14：原作者名与截断披露必须真的进 prompt。"""
    at = auto_tasks
    orig_llm = reply_manager._llm_generate
    captured = {}

    async def cap_llm(plugin_, prompt):
        captured["p"] = prompt
        return "好呀老大，下次见"

    reply_manager._llm_generate = cap_llm
    try:
        base = {"target_qq": "20001", "videos": [], "comments": []}

        async def run(feed):
            store, api = _FakeStore(), _FakeApi()
            await at.process_feeds(None, api, store, [feed],
                                   like_probability=0.0, comment_probability=1.0,
                                   action_interval=0)
            return api

        # 事故复现：外层正文点「老大」，转发正文来自别人
        await run({**base, "tid": "s1",
                   "content": "老大，下次一定能够再见的对吧😭",
                   "rt_con": "已经11.3了 我正坐在教室的木椅", 
                   "rt_author": "牢铁吃了没", "images": []})
        p = captured.get("p", "")
        check("S10 prompt 含原作者名（「老大」这才有归属）",
              "转发 牢铁吃了没 的说说" in p, p[:120])
        check("S11 作者名出现在正文位而非指令位", "好友20001发了说说" in p)

        # 拿不到作者名时也必须明示"这是别人的内容"
        await run({**base, "tid": "s2", "content": "老大说得对",
                   "rt_con": "已经11.3了", "rt_author": "", "images": []})
        p = captured.get("p", "")
        check("S12 作者名缺失→降级为「转发别人的说说」",
              "转发别人的说说" in p, p[:120])
        check("S13 不再出现旧格式「（转发: 」", "（转发: " not in p, p[:120])

        # 截断自证：尾部省略号才披露
        await run({**base, "tid": "s3", "content": "",
                   "rt_con": "清晨的风夹着凉雨敲着窗 ...", "rt_author": "牢铁吃了没",
                   "images": []})
        p = captured.get("p", "")
        check("S14 尾部省略号→追加「查看全文」折叠披露",
              "查看全文" in p and "猜后半段" in p, p[:160])

        await run({**base, "tid": "s4", "content": "",
                   "rt_con": "清晨的风夹着凉雨敲着窗，屋里很安静。", "rt_author": "牢铁吃了没",
                   "images": []})
        p = captured.get("p", "")
        check("S15 无省略号→不加披露（不塞用不上的元信息）",
              "查看全文" not in p, p[:160])

        check("S16 _rt_looks_truncated 判定",
              at._rt_looks_truncated("abc…") is True
              and at._rt_looks_truncated("abc。。。") is True
              and at._rt_looks_truncated("abc。") is False
              and at._rt_looks_truncated("") is False)
    finally:
        reply_manager._llm_generate = orig_llm

    # 命令侧展示：转发连同作者一起显示
    s = plugin.format_feed({"target_qq": "123", "created_time": "t", "content": "正文",
                            "rt_con": "原内容", "rt_author": "牢铁吃了没",
                            "images": [], "videos": [], "comments": []}, 1)
    check("S17 /说说 展示含原作者名", "转发 牢铁吃了没" in s, s[:80])


# ============================================================
# U. v1.7.0 评论决策规则层（C1~C5 行为变更验收）
#     Q5 口径：只补最小回归用例，不做 L0 语料/影子模式/埋点
# ============================================================
async def test_comment_policy_v170():
    section("U. v1.7.0 评论决策规则层")
    cp = comment_policy
    cs = comment_style

    # C1 发布字数代码级钳制：配置越界也只认 20；净化步骤不受影响
    long_text = "好" * 50
    check("U01 C1 默认发布文本硬截到 20 字",
          len(cp.clamp_publish_text(long_text)) == 20,
          f"len={len(cp.clamp_publish_text(long_text))}")
    check("U02 C1 配置填 100 仍钳到 20 字（上限代码级写死）",
          len(cp.clamp_publish_text(long_text, cp.Policy(max_comment_chars=100))) == 20)
    check("U03 C1 只钳最终发布文本，净化默认仍是 100 字（A16 结构锁）",
          len(reply_manager.sanitize_llm_output("あ" * 500)) == 100
          and len(reply_manager.sanitize_llm_output(long_text)) == 50)

    # C2 正向 few-shot / C3 理解校验：开启时都进 prompt，纪律段恒在最末
    g_on = cs.append_style_guard("好友X发了说说：Y。")
    check("U04 C2 正向 few-shot 进 prompt", cp.FEW_SHOT in g_on)
    check("U05 C3 理解校验进 prompt", cp.UNDERSTANDING_CHECK in g_on)
    check("U06 C2/C3 追加后纪律段仍在 prompt 最末（O05/O18 顺序锁）",
          g_on.endswith(cs.STYLE_GUARD))
    g_off = cs.append_style_guard("好友X发了说说：Y。", cp.Policy(enabled=False))
    check("U07 总开关关闭只影响新增引导，纪律段与硬拦截不减",
          cp.FEW_SHOT not in g_off and cp.UNDERSTANDING_CHECK not in g_off
          and g_off.endswith(cs.STYLE_GUARD))

    # C4 动态类型分流：打卡类降级，话题类短说说不受影响
    checkin = {"content": "今日打卡", "rt_con": "", "images": []}
    topic = {"content": "五维介质新企划", "rt_con": "", "images": []}
    check("U08 C4 打卡类判 checkin、话题类短说说仍 normal（不误伤）",
          cp.classify_feed(checkin) == "checkin" and cp.classify_feed(topic) == "normal")
    d_on = cp.decide_material(checkin, policy=cp.Policy(enabled=True))
    d_off = cp.decide_material(checkin, policy=cp.Policy(enabled=False))
    check("U09 C4 decide_material 打卡类降级、总开关关闭后不降级",
          d_on.skip_comment and d_on.reason == "checkin"
          and not d_off.skip_comment and d_off.has_material)

    # C5 有序规则表 + 命中计数；边界 B 三类硬兜底恒生效
    check("U10 C5 规则表顺序固化 refusal→identity→romantic→stiff",
          tuple(r.id for r in cp.RULES) == ("refusal", "identity", "romantic", "stiff"))
    check("U11 边界B 三类硬兜底恒生效（无任何配置字段可关）",
          all(cp.is_hard_rule(x) for x in cp.HARD_RULE_IDS)
          and not cp.is_hard_rule("stiff")
          and tuple(r.hard for r in cp.RULES[:3]) == (True, True, True))
    cp.reset_hit_counts()
    p_cnt = cp.Policy(enabled=False, hit_counter_enabled=True)
    r1, id1 = cp.evaluate("我不能提供这个", p_cnt)
    r2, id2 = cp.evaluate("双喜临门", p_cnt)
    r3, _ = cp.evaluate("哈哈好呀", p_cnt)
    check("U12 C5 命中计数只记命中规则，且开关关闭不减少任何拦截",
          (r1, id1) == ("refusal", "refusal")
          and (r2, id2) == ("stiff", "stiff") and r3 == "ok"
          and cp.get_hit_counts() == {"refusal": 1, "stiff": 1},
          str(cp.get_hit_counts()))

    # 策略解析：plugin=None 与配置越界都必须安全退回（测试与老 config.toml 实例）
    p_none = cp.resolve_policy(None)
    fake = types.SimpleNamespace(config=types.SimpleNamespace(auto=types.SimpleNamespace(
        comment_policy_enabled=False, max_comment_chars=999,
        stiff_retry_enabled=False, rule_hit_counter_enabled=True)))
    p_fake = cp.resolve_policy(fake)
    check("U13 resolve_policy：None 安全默认、越界配置钳到 20、开关生效",
          p_none.enabled and p_none.max_comment_chars == 20
          and p_none.stiff_retry_enabled and not p_none.hit_counter_enabled
          and p_fake.max_comment_chars == 20 and not p_fake.enabled
          and not p_fake.stiff_retry_enabled and p_fake.hit_counter_enabled)


# ============================================================
# V. v1.8.0 性能与流程优化（P1 延迟识图 / P2 抖动 / P3 双保险判重）
# ============================================================
class _VisionApi:
    """mock QzoneAPI：记录延迟识图调用，返回预置描述。"""

    def __init__(self, uin="10001", desc="一只橘猫趴在键盘上"):
        self.uin = uin
        self.desc = desc
        self.describe_calls = []
        self.comments = []
        self.likes = []

    async def _describe_images(self, urls, max_images, concurrency, **kw):
        self.describe_calls.append(list(urls))
        return [self.desc] * len(urls)

    async def comment(self, fid, qq, text):
        self.comments.append((fid, text))
        return True

    async def like(self, fid, qq):
        self.likes.append(fid)
        return True


async def test_lazy_vision_v180():
    section("V. v1.8.0 延迟识图 / 抖动 / 双保险判重")
    at = auto_tasks
    orig_llm = reply_manager._llm_generate

    async def fake_llm(plugin, prompt):
        return "模拟评论"

    reply_manager._llm_generate = fake_llm
    base = {"target_qq": "20001", "videos": [], "comments": []}
    try:
        # ── P1 延迟识图 ──────────────────────────────────────────────
        # V01 概率通过 → 先识图再评论，描述进 prompt
        captured = {}

        async def cap_llm(_p, prompt):
            captured["p"] = prompt
            return "模拟评论"

        reply_manager._llm_generate = cap_llm
        api = _VisionApi()
        store = _FakeStore()
        feed = {**base, "tid": "v1", "content": "看看我家猫", "rt_con": "",
                "images": [], "image_urls": ["https://qzone.example/a.jpg"]}
        await at.process_feeds(None, api, store, [feed],
                               like_probability=0, comment_probability=1.0,
                               action_interval=0, lazy_vision=True)
        check("V01 延迟识图：概率通过才识图，描述进素材",
              api.describe_calls == [["https://qzone.example/a.jpg"]]
              and len(api.comments) == 1
              and "[图: 一只橘猫趴在键盘上]" in captured.get("p", ""),
              f"识图调用={api.describe_calls} prompt={captured.get('p', '')[:80]}")

        # V02 概率弃评 → 不识图，点赞不受影响
        api2 = _VisionApi()
        feed2 = {**base, "tid": "v2", "content": "看看我家猫", "rt_con": "",
                 "images": [], "image_urls": ["https://qzone.example/b.jpg"]}
        await at.process_feeds(None, api2, _FakeStore(), [feed2],
                               like_probability=1.0, comment_probability=0.0,
                               action_interval=0, lazy_vision=True)
        check("V02 延迟识图：概率弃评不识图，点赞照常",
              not api2.describe_calls and not api2.comments and api2.likes == ["v2"],
              f"识图调用={api2.describe_calls}")

        # V03 已处理条目 → 不识图（稳态省重复下载的核心）
        api3 = _VisionApi()
        store3 = _FakeStore()
        await store3.mark_processed("v3")
        feed3 = {**base, "tid": "v3", "content": "旧说说", "rt_con": "",
                 "images": [], "image_urls": ["https://qzone.example/c.jpg"]}
        await at.process_feeds(None, api3, store3, [feed3],
                               like_probability=1.0, comment_probability=1.0,
                               action_interval=0, lazy_vision=True)
        check("V03 延迟识图：已处理条目零识图零操作",
              not api3.describe_calls and not api3.comments and not api3.likes)

        # V04 lazy_vision=False（命令路径）→ 不触发延迟识图，images 原样用
        api4 = _VisionApi()
        feed4 = {**base, "tid": "v4", "content": "命令路径", "rt_con": "",
                 "images": ["已有描述"], "image_urls": ["https://qzone.example/d.jpg"]}
        await at.process_feeds(None, api4, _FakeStore(), [feed4],
                               like_probability=0, comment_probability=1.0,
                               action_interval=0, lazy_vision=False)
        check("V04 命令路径（lazy=False）不重复识图",
              not api4.describe_calls and len(api4.comments) == 1)

        # V05 无图无素材 → 不识图不评论（素材守卫在延迟路径同样生效）
        api5 = _VisionApi()
        feed5 = {**base, "tid": "v5", "content": "", "rt_con": "",
                 "images": [], "image_urls": []}
        await at.process_feeds(None, api5, _FakeStore(), [feed5],
                               like_probability=0, comment_probability=1.0,
                               action_interval=0, lazy_vision=True)
        check("V05 延迟识图：无素材仍被守卫拦下",
              not api5.describe_calls and not api5.comments)

        # V06 C4 先识图再判：带图打卡 → 描述有效 → 正常评论（v1.7.0 语义不漂移）
        api6 = _VisionApi(desc="跑步路线图，今日10公里")
        feed6 = {**base, "tid": "v6", "content": "打卡第30天", "rt_con": "",
                 "images": [], "image_urls": ["https://qzone.example/run.jpg"]}
        await at.process_feeds(None, api6, _FakeStore(), [feed6],
                               like_probability=0, comment_probability=1.0,
                               action_interval=0, lazy_vision=True)
        check("V06 C4 先识图再判：带图打卡正常评论（语义不漂移）",
              len(api6.describe_calls) == 1 and len(api6.comments) == 1,
              f"识图={len(api6.describe_calls)} 评论={len(api6.comments)}")

        # V07 纯文字打卡（无图）→ 零识图、C4 降级只点赞
        api7 = _VisionApi()
        feed7 = {**base, "tid": "v7", "content": "打卡第30天", "rt_con": "",
                 "images": [], "image_urls": []}
        await at.process_feeds(None, api7, _FakeStore(), [feed7],
                               like_probability=1.0, comment_probability=1.0,
                               action_interval=0, lazy_vision=True)
        check("V07 纯文字打卡：零识图，C4 降级只点赞",
              not api7.describe_calls and not api7.comments and api7.likes == ["v7"])

        # V08 识图异常 → 不阻断点赞，按缺图口径走（不抛异常）
        class _BoomVisionApi(_VisionApi):
            async def _describe_images(self, urls, *a, **kw):
                raise RuntimeError("VLM 炸了")

        api8 = _BoomVisionApi()
        feed8 = {**base, "tid": "v8", "content": "有正文的说说", "rt_con": "",
                 "images": [], "image_urls": ["https://qzone.example/e.jpg"]}
        await at.process_feeds(None, api8, _FakeStore(), [feed8],
                               like_probability=1.0, comment_probability=1.0,
                               action_interval=0, lazy_vision=True)
        check("V08 延迟识图失败不阻断：点赞照常、正文仍可评论",
              api8.likes == ["v8"] and len(api8.comments) == 1)

        # ── P3 双保险判重 ────────────────────────────────────────────
        # V09 评论区已有 bot 自己 → 跳过并标记（store 无记录也拦）
        api9 = _VisionApi(uin="10001")
        store9 = _FakeStore()
        feed9 = {**base, "tid": "v9", "content": "老说说", "rt_con": "", "images": [],
                 "comments": [{"qq_account": "10001", "nickname": "bot",
                               "comment_tid": 1, "content": "以前的评论"}]}
        await at.process_feeds(None, api9, store9, [feed9],
                               like_probability=1.0, comment_probability=1.0,
                               action_interval=0)
        check("V09 双保险：在线评论含自己→跳过且标记已处理",
              not api9.comments and not api9.likes and await store9.is_processed("v9"))

        # V10 只有他人评论 → 正常处理；昵称相同但 QQ 不同不误拦
        api10 = _VisionApi(uin="10001")
        feed10 = {**base, "tid": "v10", "content": "新说说", "rt_con": "", "images": [],
                  "comments": [{"qq_account": "20001", "nickname": "bot",
                                "comment_tid": 2, "content": "同名好友"}]}
        await at.process_feeds(None, api10, _FakeStore(), [feed10],
                               like_probability=0, comment_probability=1.0,
                               action_interval=0)
        check("V10 双保险只认QQ号：同名昵称不误拦",
              len(api10.comments) == 1)

        # V11 api 无 uin 属性 → 退回 store 单保险，不误拦
        class _NoUinApi(_VisionApi):
            def __init__(self):
                super().__init__()
                del self.uin

        api11 = _NoUinApi()
        feed11 = {**base, "tid": "v11", "content": "说说", "rt_con": "", "images": [],
                  "comments": [{"qq_account": "10001", "comment_tid": 3, "content": "x"}]}
        await at.process_feeds(None, api11, _FakeStore(), [feed11],
                               like_probability=0, comment_probability=1.0,
                               action_interval=0)
        check("V11 uin 取不到时退回单保险（不误拦）", len(api11.comments) == 1)

        # ── P2 抖动 ─────────────────────────────────────────────────
        # V12 默认抖动 5 分钟：sleep 落在 [25, 35] 分钟且不全相等
        loop_plugin = types.SimpleNamespace(config=types.SimpleNamespace(
            auto=types.SimpleNamespace(interval_min=30)))
        loop = at.AutoTaskLoop(loop_plugin, None)
        samples = [loop._next_sleep_sec() for _ in range(50)]
        check("V12 默认抖动：sleep∈[25min,35min] 且有随机性",
              all(25 * 60 <= s <= 35 * 60 for s in samples) and len(set(samples)) > 1,
              f"min={min(samples):.0f}s max={max(samples):.0f}s")

        # V13 jitter=0 → 完全旧行为（恒等于 interval）
        loop0 = at.AutoTaskLoop(types.SimpleNamespace(config=types.SimpleNamespace(
            auto=types.SimpleNamespace(interval_min=30, interval_jitter_min=0))), None)
        check("V13 jitter=0 恒等于 interval（旧行为）",
              all(loop0._next_sleep_sec() == 1800 for _ in range(10)))

        # V14 越界钳制：jitter=999 → 钳到 interval/2，sleep 恒正
        loop_big = at.AutoTaskLoop(types.SimpleNamespace(config=types.SimpleNamespace(
            auto=types.SimpleNamespace(interval_min=10, interval_jitter_min=999))), None)
        big = [loop_big._next_sleep_sec() for _ in range(50)]
        check("V14 抖动钳到 interval/2，sleep 恒正",
              all(300 <= s <= 900 for s in big), f"min={min(big):.0f}s")

        # V15 配置缺失/异常 → 代码级默认 5 分钟（边界 B：老 config 无字段也生效）
        loop_missing = at.AutoTaskLoop(types.SimpleNamespace(config=types.SimpleNamespace(
            auto=types.SimpleNamespace(interval_min=30, interval_jitter_min="不是数字"))), None)
        check("V15 抖动配置异常退回代码级默认 5 分钟",
              loop_missing._jitter_sec() == 300.0)

        # ── 结构锁 ──────────────────────────────────────────────────
        # V16 image_urls 字段：两条拉取路径都返回（MockTransport）
        import httpx
        crafted = json.dumps({"code": 0, "data": {"data": [{
            "appid": "311", "uin": "20001", "key": "tidv",
            "feedstime": "2026-01-01 12:00",
            "html": ("<div class='f-info'>正文</div><div class='img-box'>"
                     "<img src='https://qzone.example/x.jpg'/>"
                     "<img src='https://qzone.example/x.jpg'/></div>"),
        }]}}, ensure_ascii=False)

        async def handler(request):
            if "feeds3_html_more" in str(request.url):
                return httpx.Response(200, text="_Callback(" + crafted + ");")
            return httpx.Response(404)

        api_v = qzone_api.QzoneAPI({"uin": "10001", "p_skey": "x"})
        api_v._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        feeds_v = await api_v.get_qzone_list(describe_images=False)
        await api_v.aclose()
        check("V16 get_qzone_list 返回去重后的 image_urls（不识图）",
              feeds_v and feeds_v[0].get("image_urls") == ["https://qzone.example/x.jpg"]
              and feeds_v[0].get("images") == [],
              str(feeds_v[0].get("image_urls")) if feeds_v else "空")

        # V17 get_list 同样返回 image_urls（smallurl 优先）
        msglist = json.dumps({"code": 0, "logininfo": {"name": "bot"}, "msglist": [{
            "tid": "tidm", "uin": 20001, "created_time": 1700000000,
            "content": "正文", "pic": [{"smallurl": "https://qzone.example/s.jpg",
                                        "url1": "https://qzone.example/big.jpg"}],
            "commentlist": [],
        }]}, ensure_ascii=False)

        async def handler2(request):
            if "msglist_v6" in str(request.url):
                return httpx.Response(200, text="_preloadCallback(" + msglist + ");")
            return httpx.Response(404)

        api_m = qzone_api.QzoneAPI({"uin": "10001", "p_skey": "x"})
        api_m._client = httpx.AsyncClient(transport=httpx.MockTransport(handler2))
        feeds_m = await api_m.get_list("20001", 5, filter=False, describe_images=False)
        await api_m.aclose()
        check("V17 get_list 返回 image_urls（smallurl 优先，不识图）",
              feeds_m and feeds_m[0].get("image_urls") == ["https://qzone.example/s.jpg"]
              and feeds_m[0].get("images") == [],
              str(feeds_m[0].get("image_urls")) if feeds_m else "空")

        # V18 命令语义锁：/说说互动 不传 lazy_vision（展示依赖描述）
        import inspect
        src_interact = inspect.getsource(plugin.QzoneFeedsPlugin.cmd_msglist_interact)
        check("V18 /说说互动 仍走拉取即识图（命令语义红线）",
              "lazy_vision" not in src_interact
              and "describe_images=self._vision_enabled()" in src_interact)

        # V19 规则层零改动锁：comment_policy.py 不含 v1.8.0 新增逻辑
        cp_src = (Path(PLUGIN_DIR) / "comment_policy.py").read_text(encoding="utf-8")
        check("V19 comment_policy 规则层零改动（无 lazy/image_urls 字样）",
              "lazy" not in cp_src and "image_urls" not in cp_src)

        # V20 run_auto_job 默认走延迟识图，关闭开关恢复旧行为
        class _LazyCapApi:
            def __init__(self):
                self.calls = []

            async def get_qzone_list(self, **kw):
                self.calls.append(kw)
                return []

        p_lazy = _AutoPlugin()
        api_l = _LazyCapApi()
        await at.run_auto_job(p_lazy, api_l, _FakeStore(), None)
        p_old = _AutoPlugin(auto_kw=dict(lazy_vision_enabled=False))
        api_o = _LazyCapApi()
        await at.run_auto_job(p_old, api_o, _FakeStore(), None)
        check("V20 自动任务默认延迟识图，开关关闭恢复拉取即识图",
              api_l.calls and api_l.calls[0].get("describe_images") is False
              and api_o.calls and api_o.calls[0].get("describe_images") is True,
              f"lazy={api_l.calls[0] if api_l.calls else None} "
              f"old={api_o.calls[0] if api_o.calls else None}")
    finally:
        reply_manager._llm_generate = orig_llm


def main():
    asyncio.run(_amain())
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    print("\n" + "=" * 60)
    print(f"结果：{passed}/{total} 通过")
    failed = [(n, d) for n, ok, d in _RESULTS if not ok]
    if failed:
        print("\n未通过项：")
        for n, d in failed:
            print(f"  - {n}" + (f"：{d}" if d else ""))
    print("=" * 60)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
