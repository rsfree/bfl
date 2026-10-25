# bfl2openai — flux-tools.bfl.ai 独立适配器（OpenAI Images + BFL 官方原生 双端点）

把 [flux-tools.bfl.ai/precise-editing](https://flux-tools.bfl.ai/precise-editing)
（Black Forest Labs 官方 Playground）的编辑能力逆向后，包装成**独立服务**，同时暴露两种方言：

1. **OpenAI Images API**（`/v1/images/*`）——本项目的起点；
2. **BFL 官方原生格式**（`/v1/flux-3-image` + `/v1/get_result`，对齐
   [docs.bfl.ai](https://docs.bfl.ai/flux_3/flux3_image_generate) 与
   `api.bfl.ai/openapi.json`）——官方 API 客户端可无缝换 base 指到本服务。

上游匿名可用（无需 BFL API Key），通过 `curl_cffi` Chrome 指纹通过 Vercel 检查点。

## 生成分辨率档位（2026-10-09 全档实测，i2i，256px 输入，aspect auto）

| `resolution` | 实测输出 | 状态 |
|---|---|---|
| `768sq` | 768×768 | OK（30s） |
| `1k` | 1024×1024 | OK（40s） |
| `1.5k` | 1536×1536 | OK（Playground 默认） |
| `2k` | 2048×2048 | OK（36s） |
| `4k` | — | **403 "4K resolution is available to BFL accounts only."**（匿名拒绝） |

**4k/8k 走复合**：`resolution=4k|8k`（原生端点）= 2k 生成 + 腾讯云 MPS 超分 ×2/×4
（ProcessImage `AdvancedSuperResolutionConfig`，Type=ultra, Mode=percent；
输出 SignUrl 约 1 小时有效）。需要 `MPS_SECRET_ID`/`MPS_SECRET_KEY` 环境变量，
未配置时 4k/8k 请求返回 503（**在任何生成消耗之前**拒绝）。参考设计：image-adapter
`docs/08`（归档于 `../analysis/image-adapter-draft/`，本项目为独立实现）。

## 上传句柄与异步任务（jimeng 形状）

```
POST /v1/uploads                            multipart file（可重复）
→ {"files":[{"id","ref":"@image#1:Clipboard_Screenshot.png","filename","size","expires_in":3600}]}

POST /async/v1/images/generations           受理：202 + {"task_id"} + Location
                                            body = 原生 generate 形状；prompt 里可写
                                            "@image#1:Clipboard_Screenshot.png" 句柄
                                            （受理时即改写为 BFL <ref_image_K> 并并入 images）
GET  /async/v1/images/generations/{task_id} 非终态 202 {task_id,status}；
                                            success 200 {data:[{url}],created,usage}；
                                            failure 200 {task_id,status:"failure",error}；
                                            未知 404（存在性与属主合并，不泄漏）
GET  /async/v1/images/generations           本凭证任务列表
DELETE /async/v1/images/generations/{task}  终态才可删；in-flight 400（上游无取消）
```

任务五态：`queued / in_progress / success / failure / canceled`（顶层 status 一个字段判全程；
失败也是 200，HTTP 5xx 会误触调用方重试）。句柄与任务都是**进程内存**（TTL 1h / 重启即失），
要持久资产请直接传 URL/base64。鉴权：跟随全局开关（默认关）；属主按凭证指纹隔离。

## 弹性（拉取回退：重试 + 代理）

所有对上游的 GET（media 取图、引用图下载）走 `_resilient_get`：
**3 次直连重试**（间隔 1s/3s，429/403/503 才重试，业务 4xx 立即返回），
之后若配置了 `BFL2OPENAI_FALLBACK_PROXY`（如轮换代理池入口）再走 **2 次代理出口**
（新会话 + 新出口 IP）。默认未配置代理，仅直连重试。

## 4k/8k 复合（MPS 超分）

匿名生成上限 2k（4k 上游直接 403），因此**任何超过 2k 的客户端请求自动走复合**，
不存在独立的超分端点：

- 原生门：`resolution: "4k" | "8k"`
- OpenAI 门：`size` 长边 >2048（如 `"4096x4096"`）或 `resolution: "4k"|"8k"`

流程：2k 生成 → 产物经本服务上传 COS（`agi-1436636638` @ ap-singapore）→
MPS `ProcessImage`（COS 输入，Type=ultra，percent ×2/×4）→ `DescribeImageTaskDetail`
轮询 → 输出 COS 对象 → 预签名下载链。实测：2k 生成 ~30s + 超分 ~5-15s，
产物 4096² PNG（17MB）。注意 MPS 自己去拉 playground media URL 会被 429
（实测任务失败记录），所以输入必须经 COS 中转。

## BFL 官方原生端点

```
POST /v1/flux-3-image        官方 JSON：prompt*、images(string|[string]，URL/base64，≤10)、
                             aspect_ratio(auto)、resolution(见上表，默认 1k)、
                             safety_tolerance/grounding/version(接受即丢弃，上游无对应旋钮)、
                             bounding_boxes(扩展：[{"id","bbox":[x1,y1,x2,y2],"desc"}]，0~1000 坐标，
                                            映射到 Playground 区域指令通道)
→ {"id":"<pollToken>","polling_url":"…/v1/get_result?id=…","cost":null,"input_mp":null}

GET  /v1/get_result?id=      → {"id","status":"Pending|Ready|Error","result":{"sample":url},
                                "progress","details"}
GET  /v1/flux3-image/results/{imageToken}    sample URL 的实际图片（PNG）
POST /v1/flux3-image/upscale 同步超分：{"image":"<公网URL或imageToken>","percent":2.0}
                             → {"url":"<SignUrl>","percent":2.0}
```

说明：

- **鉴权**：接受官方 `x-key` 头；设 `BFL_NATIVE_API_KEY` 后强制校验，缺省不校验。
- **bbox/layout 是 prompt 约定**（官方文档原文）：`{"id","bbox":[0-1000],"desc"}` 行
  JSON 拼进 prompt + `<id>` 占位符 —— 该写法原样透传即可，模型直接消费；
  `bounding_boxes` 扩展字段才走 Playground 的区域指令通道（每区域独立 instruction）。
- **webhook 不支持**（仅轮询）；`images` 的 URL 输入由本服务代为下载。
- t2i（无 `images`）按 `mode=t2i` 提交上游 —— **未验证路径**，上游拒绝时原样透出。

## 逆向结论（上游契约）

浏览器 fetch hook + 网络抓包实测：

### 1. 提交任务

```
POST https://flux-tools.bfl.ai/api/flux3-image/generate
Content-Type: multipart/form-data
（匿名，无 Authorization；但 POST 必须带 Origin/Referer/sec-fetch-* 同源头，否则 403 空体）
```

| 字段 | 类型 | 实测值 |
| --- | --- | --- |
| `mode` | string | `i2i` |
| `prompt` | string | 全局指令；region 模式下可为空串 |
| `aspectRatio` | string | `auto` |
| `resolution` | string | `1.5k` |
| `boxes` | JSON 字符串 | `[{"id":"b-muzslh3m-4","box":{"x":0.3,"y":0.3,"w":0.4,"h":0.4},"instruction":"change the wheels to gold","source":"user"}]`（0~1 归一化坐标） |
| `image` | file(png) | 当前画布整图（约 4MB PNG） |
| `references` | JSON 字符串 | `[]`（参考图，未验证非空结构） |

响应：`{"status":"submitted","requestId":"<uuid>","pollToken":"...","prompt":"..."}`

### 2. 轮询

```
GET /api/flux3-image/status?pollToken=...
→ {"status":"pending"}
→ {"status":"ready","imageToken":"..."}     （实测约 20~30s 出图）
→ {"status":"error","error":"..."}          （如 token 非法：400 + "Invalid or expired image result token."）
```

### 3. 取图

```
GET /api/flux3-image/media?imageToken=...   → image/png 字节流
```

### 防护与鉴权

- 无需登录、无需 cookie（`/api/flux3-image/*` 三个端点全部匿名）。
- 站点前面是 Vercel Security Checkpoint：普通 curl 直接吃 HTML 拦截页；
  `curl_cffi impersonate="chrome"` 可直接通过（GET/POST 均可）。
- **POST generate 缺 `Origin`/`Referer`/`Sec-Fetch-*` 头 → 403 空体**；补上即 200。
- `/api/credits/context`、`/api/auth/refresh` 是站点计费/登录体系，适配器不需要。

## OpenAI 兼容接口

### `POST /v1/images/edits`（核心，已端到端验证）

multipart 字段：

| 字段 | 说明 |
| --- | --- |
| `image` / `image[]` | 必填（≥1 张），任意常见格式（自动转 PNG）。**重复该字段即多图生图**：第 1 张是画布、其余作为上游参考图文件部件转发。两种拼法都接——`image[]` 是 OpenAI 官方 curl 示例的写法，重复 `image` 是 openai-python SDK 的写法（2026-10-20 实测两者均出图） |
| `prompt` | 全局编辑指令 |
| `mask` | 可选，OpenAI 语义（透明=可编辑区）；自动求连通域外接矩形 → 上游 `boxes`，每框 instruction=prompt |
| `n` | 1~4，并发提交 |
| `size` | `1024x1024` 等 → 自动映射最近似的 `aspectRatio` |
| `response_format` | `b64_json`（默认）或 `url` |
| `boxes` **（扩展）** | 直接传上游 JSON，玩转精确区域编辑（可含每区域独立 instruction） |
| `aspect_ratio` / `resolution` / `mode` **（扩展）** | 透传上游（默认 `auto` / `1.5k` / `i2i`） |

多图生图（2026-10-20 端到端实测）：主图「白瓷马克杯+木桌」+ 参考图「黄色橡皮鸭」，
prompt 要求把鸭子放进杯子，输出同时保留了两张源的要素（杯体/木桌/光影来自主图，
鸭子造型与橙喙来自参考图），50~55s 出图。合成基准图与产物在 `saved_images/multi_ref/`。

一条命令的现成脚本：`bfl2openai/tests/curl_multi_edit.sh`（自动读 `.env` 取 key、自动
`--noproxy '*'`、解码 b64 落盘，图片数量不限）。

```sh
cd bfl2openai/tests
./curl_multi_edit.sh out.png "Put the duck into the mug" mug.png duck.png
SPELL=image ./curl_multi_edit.sh out_sdk.png "Put the duck into the mug" mug.png duck.png
```

脚本内部就是下面这条 curl（`SPELL` 只换字段名）：

```sh
curl -s --noproxy '*' -X POST http://127.0.0.1:8977/v1/images/edits \
  -H "Authorization: Bearer $BFL2OPENAI_API_KEY" \
  -F "image[]=@mug.png" -F "image[]=@duck.png" \
  -F "prompt=Put the duck into the mug" -F "resolution=1k" -F "response_format=b64_json"
```

两条拼法实跑对照（2026-10-20）：`image[]` 46s / 1.24MB，`image` 47s / 1.27MB，
画面一致（`saved_images/multi_ref/curl_bracket.png` vs `curl_sdk.png`）。

响应（OpenAI 格式）：

```json
{"created": 1791479981, "data": [{"b64_json": "..."}], "usage": {...}}
```

### 其他端点

- `POST /v1/images/generations` — 文生图，**也支持多垫图**（2026-10-25 实测）：
  `image` 可为字符串或字符串数组（base64 / data URI / URL / `@image#N` handle），
  `image[0]` 是画布、其余作为上游参考图部件转发，最多 10 张（超出 400 `at most 10 images`）。
  不传 `image` 时按 `mode=t2i` 提交。

  ⚠️ **prompt 必须明确指名要放入/改变的物体**，否则上游会近乎原样返回画布（实测模糊指令
  「把参考图里的所有物体摆到一起」的输出与画布 mean pixel diff ≈ 2~3、>32 差异像素占比 0%，
  看起来像"多图没生效"）。点名物体后两张/三张垫图都确实被消费：
  「把第二张的黄鸭放进杯子」→ 鸭子进杯；再加「第三张的红方块放到杯旁」→ 红方块出现。

  再实测（2026-10-25，8 张「动物持数字牌 1~8」竖图）：`image` 传 8 条 URL、prompt 只说
  「严格按参考图的顺序让动物一字排开合影」→ 输出只有画布那张（狮子举 1）被搬进广场，
  其余 7 张零贡献；改成逐只点名（狮子牌1／大象牌2／熊猫牌3／长颈鹿牌4／斑马牌5／
  棕熊牌6／企鹅牌7／兔子牌8）后 **8 只全部出现、顺序与牌号全对、造型与参考图同款**
  （产物 `saved_images/multi_ref_8animals/D_named_8animals.png`）。⇒ 参考图张数不是
  瓶颈（8 张照吃），瓶颈全在 prompt 有没有逐个指名物体。
  `size` 两种拼法都认（2026-10-25 修）：`"3:2"` 比例写法 + `"1536x1024"` 像素写法。
  修前只认像素写法，`size:"3:2"` 静默落 `auto` → 横图请求回来是 1488×2731 竖图；
  同 prompt 改传 `aspect_ratio:"3:2"` 才得到 2512×1664。非法值（`"foo"`、`"10:1"`）
  仍 fallback `auto`，映射单测见 `tests/test_size_ratio.py`（27 例）。
- `GET /v1/images/results/{imageToken}` — 代理上游 media 字节流（配 `response_format=url` 用）。
- `GET /v1/models` / `GET /healthz`。

### 鉴权（适配器自身）

设 `BFL2OPENAI_API_KEY` 后要求 `Authorization: Bearer <key>`；不设则不鉴权。

## 运行

```sh
/Users/betterme/.workbuddy/binaries/python/envs/default/bin/pip install -r requirements.txt
cd ..   # bfl2openai 的上级（bfl/）
/Users/betterme/.workbuddy/binaries/python/envs/default/bin/python -m uvicorn bfl2openai.server:app --host 127.0.0.1 --port 8977
```

### 快速验证

```sh
curl -s -X POST http://127.0.0.1:8977/v1/images/edits \
  -F "image=@tests/sample_input.png" \
  -F "prompt=change the background color to solid blue" \
  -F "response_format=b64_json" | python3 -c "import json,sys,base64;d=json.load(sys.stdin);open('out.png','wb').write(base64.b64decode(d['data'][0]['b64_json']))"
```

实测：提交 1.6s，生成约 26s，全链路约 33s 返回 1536×1536 PNG。

### 精确区域编辑（boxes 扩展）

```sh
curl -s -X POST http://127.0.0.1:8977/v1/images/edits \
  -F "image=@photo.png" \
  -F 'boxes=[{"box":{"x":0.3,"y":0.3,"w":0.4,"h":0.4},"instruction":"change the wheels to gold"}]' \
  -F "response_format=b64_json"
```

## 已知边界

- **频控/免费额度（2026-10-09 实测）**：匿名无登录，顺序 40 连发 + 瞬时 10 并发全部 200，
  当天累计 54+ 次成功提交**未触发任何限流或额度错误**，响应也无 x-ratelimit-* 头。
  即：**可观测区间内没有"每天免费 N 张"的硬上限**。唯一限制是校验型的——
  输入图 <256×256 会被 422 拒绝（不消耗额度）。站点有登录 credits 体系但匿名链路未接，
  BFL 随时可能收紧，请勿高并发滥用；复测脚本 `tests/probe_ratelimit.py`。
- `references`（参考图）非空结构未验证（Playground 里需要上传参考图才能看到）。
- `aspectRatio` 只实测 `auto`（其余值透传由上游校验）；`resolution` 五档已全实测（见顶部档位表）。
- 上游为 BFL 官方免费 Playground，**有速率限制与内容审核**（站点计费体系未接入），
  请勿高并发滥用；商用请走 BFL 官方 API（api.bfl.ai）。
- 兼容性依赖上游未公开契约，站点改版可能失效——`analysis/` 留有抓包与前端 chunk 便于复溯。

## 目录

```
bfl2openai/
├── server.py       # FastAPI：OpenAI 兼容层
├── upstream.py     # curl_cffi 上游客户端（generate/status/media）
├── bridging.py     # mask→boxes、size→aspectRatio、图片规范化
├── requirements.txt
└── tests/          # 测试图、E2E 响应样例、结果图
analysis/           # 逆向素材：网络抓包导出、前端 JS chunk、分析笔记
```
