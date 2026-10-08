# BFL flux-tools Precise Editing 接入指南

## 概述

`script_store/bfl/images@v1` 把 Black Forest Labs 官方 Playground 的精确编辑
（flux-tools.bfl.ai/precise-editing）接入适配器。上游是 BFL 自用的三段式异步
接口：**multipart 提交 → pollToken 轮询 → imageToken 取图**，全程匿名，
不需要任何 API Key（2026-10-09 抓包 + 真实流量实测）。

- 提交：`POST https://flux-tools.bfl.ai/api/flux3-image/generate`（multipart）
- 轮询：`GET /api/flux3-image/status?pollToken=...`
- 取图：`GET /api/flux3-image/media?imageToken=...`（PNG 字节流）

匿名链路实测（2026-10-09）：顺序 40 连发 + 瞬时 10 并发全部 200，当天累计 54+
次成功提交**未触发任何频控或额度错误**，响应无 ratelimit 头；`aspectRatio=auto`、
`resolution=1.5k` 为默认值。BFL 随时可能给匿名链路上计费/频控，生产请走
api.bfl.ai 官方 API（另写脚本接入，勿复用本脚本）。

## 配置步骤（New API 侧）

```
X-Upstream-Url:    https://flux-tools.bfl.ai/api/flux3-image/generate
X-Script-Ref:      bfl/images@v1
X-Auth-Emit:       none
X-Async:           poll=2,timeout=300
Authorization:     留空（上游匿名）
```

要点：

- **X-Auth-Emit: none 必须显式声明**（缺省会把调用方的 Authorization 透传给上游，
  虽然上游目前忽略它，但凭证外泄面不该靠上游的忽略兜底）。
- **提交 POST 必须带同源浏览器头**（Origin/Referer/Sec-Fetch-\*），否则上游返回
  **403 空体**（实测）；脚本已内置自动补齐，取值为渠道自身 host，无需配置。
- `X-Async` 是引擎轮询的开关；不声明会直接 400（脚本声明了 poll 相位而渠道没开）。

### 部署形态 A：直连（出口 IP 干净时）

上游地址直填即可，脚本三个端点都从渠道 URL 派生（去掉末段 `/generate` 换
`/status`、`/media`）。

### 部署形态 B：本地伪装出口 sidecar（aiohttp 必被 Vercel challenge）

**Vercel 按 TLS 指纹分级**：适配器的 aiohttp 出口每次提交都吃
`429/503 + X-Vercel-Mitigated: challenge`（2026-10-09 实测；CONNECT 代理救不了——
指纹就是客户端自己的 ClientHello）。解法是仓库自带的 sidecar
`tools/impersonation_egress.py`（curl_cffi chrome 伪装反代）：

```sh
python tools/impersonation_egress.py --port 8766 --allow flux-tools.bfl.ai
```

渠道改为：

```
X-Upstream-Url:    http://127.0.0.1:8766/https/flux-tools.bfl.ai/api/flux3-image/generate
X-Upstream-Proxy:  none
```

并给适配器进程加 `UPSTREAM_ALLOW_PRIVATE_NETWORK=true`（放行 loopback）。脚本对
sidecar 拼写是感知的：**emitted URL 保留 sidecar 前缀**，而 Origin/Referer 头
仍写真实上游域名（写 sidecar 的 host 会被上游当跨站拒绝）。

⚠️ **`X-Upstream-Proxy: none` 在本部署是必需的**：`.env` 设了
`UPSTREAM_PROXY_DEFAULT`（代理池），缺省会把上游调用（含 loopback 目标）塞进池，
报 503/连接拒绝。`none` 是框架 documented 的单渠道直连逃生口。

⚠️ sidecar 会剥掉调用方的身份类头（User-Agent/Accept/Accept-Encoding 等，由伪装
客户端统一供给）——透传 curl 的 UA 会立刻触发 challenge（实测）。语义头
（Content-Type/Origin/Referer/Sec-Fetch-\*/X-\*）原样透传。

## 字段映射

| 规范字段 | 上游字段 | 说明 |
|---|---|---|
| `image`（str/list，必填） | `image` 文件部件 | data URI / bare base64 / URL 均收；**恰好 1 张**，多张按参数报错；非 PNG 自动转 PNG；**小于 256×256 本地拒绝**（上游 422 原话：`Images[0] dimensions must be at least 256x256 pixels`，校验在计费之前） |
| `prompt` | `prompt` | 可为空串，**前提是 boxes 里至少一条 instruction**；两者全空本地 400 |
| `boxes`（list 或 JSON 字符串） | `boxes`（JSON 串） | 精确区域编辑，本渠道的核心。每项 `{"box":{"x","y","w","h"},"instruction"}`，0~1 归一化坐标，越界**钳制不报错**（Playground 同款行为）；`id` 缺省自动生成，`source` 缺省 `user` |
| `aspect_ratio` | `aspectRatio` | 字符串透传（缺省 `auto`）；未声明时**不派生**——i2i 出图跟随输入图，size 在本渠道是装饰性的 |
| `resolution` | `resolution` | 字符串透传（缺省 `1.5k`） |
| `size` | 不转发 | 装饰性：i2i 输出跟随输入图，派生一个上游没有的尺寸方言只会制造 4xx |
| `n` | 不转发 | 引擎按 n 聚合调用 |
| `mask` | **丢弃** | BFL 无 mask 原语（区域=boxes+指令），脚本无法诚实推导外接框。与 ark 渠道同款裁决：不转发、不报错、文档可见。要区域就传 `boxes` |
| `response_format` | — | `b64_json`（缺省）本地取图编码；`url` 转存对象存储（见下） |

## 响应映射

上游 ready 态只给 `imageToken`，对应 media URL 是**短时效签名链接**（实测约
25 分钟的 token 已会挂起）。脚本因此**绝不把 media URL 直接交给调用方**：

- `response_format=b64_json`（缺省）：本地下载并编码
- `response_format=url`：经 `rehost_image` 转存对象存储；未配存储时**降级为
  b64_json**（AC-04 的"退回上游自身形态"在此不适用——上游形态是会过期的链接，
  交出去等于交付一个定时失效的 URL）

## 重要注意事项

1. **edit-only**：`mode=i2i` 是 Playground 唯一暴露的模式，无图请求本地 400
   （不花生成额度去验证未证实的 t2i 路径）。
2. **上游是 FastAPI 形状**（422 错误体 `{"detail":[{"loc":["body","images"],...}]}`，
   图片以 base64 进 `images` 字段），flux-tools 后端是包了一层的内部模型 API，
   改版风险高于公开文档化的厂商；`script_store/manifest.json` 已登记 sha256。
3. **`references` 未接**：非空结构未验证（Playground 需上传参考图才能看到形状）。
4. 匿名上游建议只作演示/低价值渠道；计费、限流、审计都不在你手里。

## 测试验证

- 单元：`tests/unit/test_bfl_images_script.py`（25 项，全部对捏造应答断言，
  不打真实上游；含 sidecar 前缀保持与真实 Origin 两个派生用例）。
- 实链路（2026-10-09，image-adapter 全链路真实生成）：`/v1/images/edits`
  multipart 进（prompt 空 + boxes 指令）→ 提交 ~1.6s → 引擎轮询 ~27s →
  出图 **1536×1536 PNG，中心像素 (241,114,26) 纯橙**（boxes 区域语义正确），
  全链路 ~32s，响应为标准 OpenAI 信封。
- 契约来源：Playground 页面 fetch hook 实抓 FormData + 网络面板三段式抓包；
  逆向过程见 `~/PycharmProjects/AI/reverse/bfl/analysis/NOTES.md`。

## 参考文档

- Playground: https://flux-tools.bfl.ai/precise-editing
- 官方付费 API（生产推荐）: https://api.bfl.ai（FLUX 系列，`x-key` 头鉴权）
