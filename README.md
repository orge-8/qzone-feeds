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
plugin.py            生命周期 + 6 命令 + 鉴权 + 串行队列 worker + 自动任务启停
qzone_api.py         QQ空间协议层（移植自 Maizone，5 处缺陷修正）
cookie_manager.py    adapter 取 cookie + 节流 + data_dir 落盘
vision.py            VLM 图片描述（llm.generate 多模态）
reply_manager.py     回评流程：扫描/去重/LLM/调用 reply
auto_tasks.py        自动任务循环 + 静默时段解析
processed_store.py   已处理记录（LRU 200 feeds / 100 comments，原子落盘）
```

- **串行队列**：所有 QQ 空间写操作（发/评/赞/回）走单 worker 队列，自动任务与手动命令同队列，防并发风控
- **自动重登**：cookie 失效（登录类错误码）自动强制刷新重试（默认 1 次）
- **去重**：`processed_list.json`（`ctx.paths.data_dir`）记录已处理动态/评论，手动与自动共享
- **反风控**：逐条操作间 `3~4s` 随机间隔；静默时段自动任务全停

## 相对上游 Maizone 的修正

1. 图片下载带 cookies（相册图床 403 规避）
2. 上传响应 `eval()` → `json.loads`
3. cookies/processed_list 落盘到 `ctx.paths.data_dir`（不再污染代码目录）
4. capabilities 只声明实际使用的 3 项（`send.text` / `llm.generate` / `api.call`）
5. 注入真实 VisionManager（上游 `set_image_manager` 从未被调用，图片恒为 `[图片]`）

## 测试

```bash
# 冒烟测试（98 项，FakeHost，不启 MaiBot）
python plugins/qzone-feeds/tests/test_qzone_feeds.py
# 结构自检
python check_plugin.py plugins/qzone-feeds
```

## 部署

1. 整目录复制到 MaiBot 的 `plugins/qzone-feeds/`
2. **完整重启 MaiBot**（热重载对含 manifest 变更的插件不生效）
3. 改 `config.toml` 配置 `admin_ids`（不配则命令全拒）
4. 自动任务默认关闭，按需开 `enable_auto_read` / `enable_auto_reply`
