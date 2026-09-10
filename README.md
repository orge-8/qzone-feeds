# qzone-feeds（QQ空间动态）

MaiBot 插件：发 QQ 动态、读好友动态、VLM 识别动态配图、回复自己动态下的评论、定时自动读好友动态并点赞评论。所有命令需管理员鉴权。

基于 [Maizone](https://github.com/internetsb/Maizone) 协议层移植重构，参考 [qzone-toolkit](https://github.com/gfhdhytghd/qzone-toolkit) 的风控思路。

## 命令（全部仅管理员）

| 命令 | 说明 |
| --- | --- |
| `/动态发 <正文>` | 发纯文本说说 |
| `/动态发图 <正文> \| <图片URL>` | 发带图说说（先传图再发） |
| `/好友动态 [条数]` | 拉好友动态流（默认5，上限15），图片经 VLM 转文字描述 |
| `/说说 <QQ号> [条数]` | 读指定 QQ 的说说列表（**纯读**，不点赞不评论） |
| `/说说互动 <QQ号> [条数]` | 读指定 QQ 的说说列表，并对未处理过的动态逐条点赞+评论（概率 1.0，已处理条目自动跳过） |
| `/回复评论 [条数]` | 扫自己最新动态的新评论，LLM 生成回复并发送 |
| `/动态状态` | cookie 年龄、队列深度、自动任务状态、最近发布/回评结果 |

支持全角 `/`。未授权统一回复「该命令仅管理员可用」。

### /说说互动 命令的点赞评论说明

`/说说互动 <QQ号>` 读到动态后会逐条处理未处理过的条目（**注意：这是写操作，会真实点赞评论**）：

- 点赞 + 评论概率固定 1.0（显式指令即显式意图，不走配置概率）
- 评论内容由 LLM 生成（使用 `[auto] comment_prompt` 模板），发布前经过净化（剥 markdown、截断 100 字）
- 去重走 `processed_list.json`：已处理过的动态不会再点赞评论
- 评论文本包含动态的正文、转发内容与图片的文字描述（`[图: ...]`）
- **评论素材守卫（v1.1.2）**：正文、转发内容、图片描述三者全为空时**跳过评论**（点赞照常）。
  否则 LLM 会拿到一个空白内容并凭空发挥 —— 真机曾因此产出「哈哈转发了个寂寞，原内容是啥呀」
  这类无意义评论（转发型动态的原内容当前尚未被解析，见下方「已知限制」）

## 已知限制

- **转发型动态的原内容未被解析**：`get_qzone_list` 只解析正文容器 `div.f-info` 与 `div.txt-box`，
  转发卡片里的原说说内容没有对应选择器，因此转发动态会落入「无可评论素材」分支被跳过评论。
  待拿到真机转发动态的原始 HTML 后可补上选择器。
- 转发语含中文冒号时，`txt-box` 的 `split("：", 1)` 会截断冒号前的部分。

## 配置

`config.toml`（首次运行自动生成默认值，需手动改）：

```toml
[admin]
admin_ids = ["qq:你的QQ号"]   # 必填，否则所有命令都会被拒

[read]
vision_model = "vlm"          # 视觉模型任务名；留空则图片显示为 [图片]
max_images_per_feed = 9       # 单条动态最多识别几张图（QQ空间上限9，越多越耗时）
image_concurrency = 3         # 图片识别并发数
enable_image_compress = true  # 送 VLM 前压缩图片（省 token/加速；false=用原图）
image_max_edge = 1024         # 压缩后长边像素上限（256~4096）
image_quality = 80            # JPEG 压缩质量 10~95
enable_desc_cache = true      # 缓存图片VLM描述（同一图片URL不重复识别）
desc_cache_size = 200         # VLM描述缓存容量（LRU条数，重启清空）

[auto]
enable_auto_read = false      # 定时自动读好友动态并点赞/评论
enable_auto_reply = false     # 自动回复自己动态的新评论
interval_min = 30             # 循环间隔（分钟）
silent_hours = "23:00-07:30"  # 静默时段（支持跨零点，逗号分隔多段）
like_probability = 0.9
comment_probability = 0.6
```

## 依赖与登录态

- 硬依赖 **napcat-adapter**：cookie 通过 `adapter.napcat.account.get_cookies` 自动获取，无需扫码
- python 依赖：httpx / bs4 / json5 / pillow（manifest 已声明）

## 架构

```
plugin.py            生命周期 + 7 命令 + 鉴权 + 串行队列 worker + 自动任务启停
qzone_api.py         QQ空间协议层（移植自 Maizone，5 处缺陷修正；共享 httpx client）
cookie_manager.py    adapter 取 cookie + 节流 + data_dir 落盘
vision.py            VLM 图片描述（llm.generate 多模态 + URL LRU 缓存）
reply_manager.py     回评流程：扫描/去重/LLM/调用 reply
auto_tasks.py        自动任务循环 + 静默时段解析
processed_store.py   已处理记录（LRU 200 feeds / 100 comments，防抖批量落盘）
```

- **串行队列**：所有 QQ 空间写操作（发/评/赞/回）走单 worker 队列，自动任务与手动命令同队列，防并发风控
- **自动重登**：cookie 失效（登录类错误码）自动强制刷新重试（默认 1 次）
- **去重**：`processed_list.json`（`ctx.paths.data_dir`）记录已处理动态/评论，手动与自动共享
- **反风控**：逐条操作间 `3~4s` 随机间隔；静默时段自动任务全停
- **凭据与出站防护**（v1.1.1）：
  - 读动态下载图片时，只对 QQ 图床域名（`.qzone.qq.com` / `.gtimg.cn` / `.qq.com`）携带 cookie；手动跟随重定向且**每一跳都重新校验域名**，防止 `p_skey` 被一条恶意 `<img src>` 或 302 带到第三方主机
  - `/动态发图` 的用户可控地址做 SSRF 校验（拒绝内网 / 回环 / 链路本地 / 保留网段，主机名解析后逐条判定），且该链路下载时不带 Qzone cookie
- **性能优化**（v1.1.0）：
  - 共享 httpx 连接池：每个 job 内所有请求复用同一 AsyncClient（省 TCP+TLS 握手）
  - 已处理列表防抖落盘：mark 只改内存，2s 防抖批量写盘 + job 边界兜底（写盘次数降一个数量级）
  - VLM 描述缓存：同一图片 URL 不重复下载/识别（单张省 20s+ 与全部 token，命中日志 `图片描述命中缓存`）
  - VLM 下载小图优先（`smallurl`），下载字节数降 5~10 倍
  - 图片压缩走内存 bytes 直传，只在送 VLM 边界做一次 base64 编码

## 相对上游 Maizone 的修正

1. 图片下载带 cookies（相册图床 403 规避）
2. 上传响应 `eval()` → `json.loads`
3. cookies/processed_list 落盘到 `ctx.paths.data_dir`（不再污染代码目录）
4. capabilities 只声明实际使用的 3 项（`send.text` / `llm.generate` / `api.call`）
5. 注入真实 VisionManager（上游 `set_image_manager` 从未被调用，图片恒为 `[图片]`）

## 测试

```bash
# 行为测试（90 项：逻辑 + 鉴权 + 命令正则 + 安全验证 + 生命周期/Manifest）
python tests/run_tests.py
```

不依赖 MaiBot 与真实网络（maibot_sdk 用 stub 注入，出站请求用 `httpx.MockTransport` 拦截），
可直接在插件目录下运行。上线前需 **90/90 全过**。

## 部署

1. 整目录复制到 MaiBot 的 `plugins/qzone-feeds/`
2. **完整重启 MaiBot**（热重载对含 manifest 变更的插件不生效）
3. 改 `config.toml` 配置 `admin_ids`（不配则命令全拒）
4. 自动任务默认关闭，按需开 `enable_auto_read` / `enable_auto_reply`
