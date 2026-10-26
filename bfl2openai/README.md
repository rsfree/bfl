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

> **2026-10-26 起走「异步桥」**：上游封 curl_cffi 指纹后，原生方言若直连必 403=>502。
> 现在 sidecar 在位时，`/v1/flux-3-image` 受理即回 `{id, polling_url}`，生成在后台任务里
> 跑浏览器出口（20-30s），`/v1/get_result` 只读任务状态（Generating/Ready/Error），
> Ready 的 `result.sample` 是 24h COS 预签名链（无 COS 时是本服务代理链）。
> 4k/8k 复合同样在后台任务里完成（2k 生成 + CI/MPS 超分）。sidecar 不在才回落直连。

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
| `image` / `image[]` | 必填（≥1 张），任意常见格式（自动转 PNG）。**重复该字段即多图生图**：多图一律走上游 composition：第 1 张作为**编辑目标**、与其余图一起按 `references` 转发，`mode` 由适配器置 `t2i`（2026-10-26 契约修订，见「已知边界」）。两种拼法都接——`image[]` 是 OpenAI 官方 curl 示例的写法，重复 `image` 是 openai-python SDK 的写法（2026-10-20 实测两者均出图） |
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

  补充两轮反例（同素材，产物同在 `saved_images/multi_ref_8animals/`）：
  ① 只点名前 3 只（狮子/大象/熊猫）+「后面 5 只严格按参考图顺序依次补齐」→ 出 7 只、
  **牌 4 整只缺失**，补出来的 4 只是同款卡通猫（物种与造型全错）
  `E_partial_3named.png`；② 改成纯序号指代「从左到右依次是参考图第1张…第8张里的那只
  动物」→ 同样 7 只、牌 5 缺失，除画布狮子外全是瞎编猫 `F_index_refs.png`。
  ⇒ 上游对 references 的消费是**名词驱动**：prompt 没写出的物种不会去参考图里检索，
  「按参考图顺序」「第 N 张」这类序号指代一律无效，必须逐个写出可辨识的物种名。
  **size 像素写法决定档位（2026-10-26 补）**：`"1024x1024"`→1k、`"1536x1024"`→1.5k、
`"2048x2048"`→2k（OpenAI 客户端按 size 预期输出像素，此前一律落默认 1.5k 档，
"1024x1024" 实际出 1536²）；比例写法不受影响；**显式 `resolution` 永远优先**；
长边 >2048 照旧走 MPS 升采样复合。

  `size` 两种拼法都认（2026-10-25 修）：`"3:2"` 比例写法 + `"1536x1024"` 像素写法，
  分隔符 `x` / `×` / `*` / 大小写不敏感，都走同一套解析。修前只认小写 `x` 的像素写法：
  `size:"3:2"` 静默落 `auto`（横图请求回来是 1488×2731 竖图，无报错），
  `"1536×1024"`（全角乘号）同样失效且长边也解析不到。非法值（`"foo"`、`"10:1"`）
  仍 fallback `auto`。像素写法的长边还会参与升采样判断（>2048 走 MPS），
  比例写法只影响 `aspectRatio`。映射单测见 `tests/test_size_ratio.py`。
- `GET /v1/images/results/{imageToken}` — 代理上游 media 字节流（配 `response_format=url` 用）。
- `GET /v1/models` — 规范名 + 全部别名（别名用 `root` 指回规范名）。
- `GET /healthz` — `{ok, model, aliases, upstream}`。

### 模型名与别名（2026-10-26）

上游只有一个模型：`BFL2OPENAI_MODEL`（默认 `flux-precise-edit`）决定它是谁，
请求体里的 `model` **不参与路由**——传什么名字都出同一张图。别名的意义只是
让客户端用自己熟悉的叫法调用、并能在 `/v1/models` 里被发现：

| 名字 | 来源 |
|---|---|
| `flux-precise-edit` | 规范名（`BFL2OPENAI_MODEL`） |
| `flux-3-image` | BFL 官方原生端点的叫法 |
| `flux-kontext-max` / `flux-kontext-pro` | kontext 系列的叫法 |

- 列表与识别均**大小写不敏感**、忽略首尾空白；`BFL2OPENAI_MODEL_ALIASES`
  （逗号或分号分隔）覆盖默认这三个。
- 响应头回写 `X-Bfl-Model-Requested` / `X-Bfl-Model-Resolved`，可确认没落到别的渠道。
- 默认**宽松**：未登记的名字照样出图（兼容层的本分），只记日志；要收紧设
  `BFL2OPENAI_MODEL_STRICT=1` → 未登记名字直接 404 `model_not_found`，
  让「渠道配错了名字」当场暴露，而不是静默扣量。

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

## 上传体积预算（2026-10-26）

两条提交路径（curl_cffi 直连 / 浏览器出口 sidecar）现在共用同一套预算
（`payload.fit_body_budget`）：主图 60%、参考图均分剩余，各图独立降档
（PNG 2048→1536→1024→768，再 JPEG q85/q75），总量压进 4MB。

> 历史回归：`BROWSER_PRIMARY=1` 上线时 sidecar 路径直接转发原始字节，
> 绕过规范化——11.5MB 的图本可压到 2.76MB 正常出图，实际吃上游
> `413 Request Entity Too Large`，还被包成 502。现已在 `server.run_job`
> 与 `native._bridge_run` 两处补齐，413 也单独映射成 `413 +
> invalid_request_error`（提示缩图），不再伪装成服务故障。

## 已知边界

- **频控/免费额度（2026-10-09 实测）**：匿名无登录，顺序 40 连发 + 瞬时 10 并发全部 200，
  当天累计 54+ 次成功提交**未触发任何限流或额度错误**，响应也无 x-ratelimit-* 头。
  即：**可观测区间内没有"每天免费 N 张"的硬上限**。唯一限制是校验型的——
  输入图 <256×256 会被 422 拒绝（不消耗额度）。站点有登录 credits 体系但匿名链路未接，
  BFL 随时可能收紧，请勿高并发滥用；复测脚本 `tests/probe_ratelimit.py`。
  **2026-10-26 修订**：已观测到 `429 You've used today's 30 free generations`（按出口 IP 计），
  早期「未触发任何限流/额度」的结论已不成立；现网靠 socks 代理池轮换出口 IP 规避。
- `references`（参考图）**2026-10-26 实测定型**：上游 `mode` 枚举只有 `t2i` | `i2i`，
  canvas（`image` 部件）与 references **互斥** —— `i2i`+canvas+refs → 400
  `A composition must not include a canvas image.`；`i2i`+refs 无 canvas → 400
  `An image is required for an edit.`；只有 **`t2i`+refs 无 canvas** 能出图。
  语义是「第一张参考图即编辑目标」（上游 prompt 模板原文 `Edit the first reference image.`）。
- **区域编辑与参考图互斥**：`boxes`/`mask` 是画布坐标指令，composition 是整图语义。
  两者同时出现时适配器直接返回 **400**，而不是让上游 400 被包成 502 且 boxes 静默失效。
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
