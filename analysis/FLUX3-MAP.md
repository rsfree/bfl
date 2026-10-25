# FLUX 3 可用面 / 价格地图（2026-10-24 实测）

> 目的：回答"flux-tools 之外还有哪些 FLUX 3 能力可用、哪条路性价比最高"。
> 本文件只记录**当天实测到的事实**；未验证项明确标注。

## A. Playground 白嫖面 —— flux-tools.bfl.ai（4 个公开工具，全部匿名）

首页只挂 4 个工具页，其余路径（`/deblur`、`/text-to-image` 等）一律 307 跳
`auth.bfl.ai`。**注意**：未知路径也是 307（catch-all 兜底），所以 307 不能证明路由存在，
只有返回 200 的这 4 个是确凿的。

| 工具页 | 上游端点 | 协议 | 2026-10-24 实测 | 官方等价价 |
| --- | --- | --- | --- | --- |
| `/precise-editing` | `POST /api/flux3-image/generate` → `status` → `media` | 三段式轮询 | 已端到端（bfl2openai） | $0.048 / 1K |
| `/outpainting` | `POST /api/outpainting` | **同步**，一次返回 | ✅ 58.3s，1012×512，118KB PNG | $0.045 首MP + $0.006/MP（fast）<br>$0.10/MP（high） |
| `/erase` | `POST /api/erase` | **同步**，一次返回 | ✅ 27.6s，512×512，79KB PNG | $0.03 首MP + $0.004/MP |
| `/virtual-try-on` | `POST /api/vto` | 同步（推断） | ⚠️ 端点匿名可达、字段已探明，**未跑通出图** | $0.0375 首MP + $0.005/MP |

### A1. `/api/outpainting` 契约（JSON，同步）

```jsonc
// 请求（POST，Content-Type: application/json，需 Origin/Referer/Sec-Fetch-*）
{
  "imageDataUrl": "data:image/png;base64,...",   // 必填
  "width": 1012,                                  // 必填，int ≥64
  "height": 512,                                  // 必填，int ≥64
  "referenceOffsetX": 0,                          // 可选（原图在输出画布中的位置）
  "referenceOffsetY": 0,                          // 可选
  "prompt": "...",                                // 可选
  "mode": "fast",                                 // "high" | "fast"，默认 fast
  "source": "..."                                 // 可选，来源标记
}
// 硬约束：width * height ≤ 2097152（2MP），超出 400 "Output exceeds 2MP limit"
// 响应：{"success":true,"imageUrl":"<带 SAS 签名的临时 URL>","requestId":"...","polls":4,"totalMs":3431}
```

注意 `totalMs` 只统计上游推理（实测 3.4s），整请求耗时在前端是 58s —— 差额在网络/排队/上传。

### A2. `/api/erase` 契约（multipart，同步）

| 字段 | 说明 |
| --- | --- |
| `image` | file(png)，待擦除原图 |
| `mask` | file(png)，**白色 = 待擦除区域**（实测圆环区域被擦除） |
| `dilatePixels` | 字符串数字，掩膜外扩像素（实测 8） |
| `prompt` | 可选，擦除后的填充提示 |
| `seed` | 可选 |

响应同 outpainting：`{"success":true,"imageUrl":...}`。curl_cffi 必须用
`CurlMime` + `multipart=`（`files=` 会 NotImplementedError）。

### A3. `/api/vto` 契约（JSON，未完全验证）

```
{"userImageDataUrl": "<dataurl>", "garmentDataUrls": ["<dataurl>", ...]}
```
- 空 `{}` → 400 zod `userImageDataUrl: Required`
- 只给 `userImageDataUrl` → 400 `At least one garment is required`
- 给 `garmentDataUrls:["x"]` → **30s 无响应（超时）**，说明已通过校验进入处理
- 与 erase/outpainting 一样无 401，匿名可提交

### A4. 计费面观察

- `/api/credits/context`、`/api/credits/outpainting` 对匿名一律 **401**（登录用户的预扣接口）。
- 匿名的 outpainting / erase / flux3-image **均不调用 credits 接口**，即计费门只挂在登录态。
  与 2026-10-09 结论一致：匿名链路未接计费，BFL 随时可能收紧。

## B. BFL 官方 API 价格（bfl.ai/pricing，2026-10-24 抓取 JSON-LD）

### 图像

| 模型 | 768² | 1K | 1.5K | 2K | 4K |
| --- | --- | --- | --- | --- | --- |
| FLUX 3 Image | $0.041 | $0.048 | $0.070 | $0.100 | $0.607 |
| **FLUX 3 Image Klein** | **$0.008** | **$0.009** | **$0.011** | **$0.014** | **$0.036** |
| FLUX.2 [pro] | — | $0.03 首MP + $0.015 | | | |
| FLUX.2 [max] | — | $0.07 首MP + $0.03 | | | |
| FLUX.2 [klein] 9B | — | $0.015 首MP + $0.002 | | | |
| FLUX.2 [klein] 4B | — | $0.014 首MP + $0.001 | | | |
| FLUX.2 [flex] | — | $0.05/MP | | | |

### 工具

| 能力 | 价格 |
| --- | --- |
| FLUX Outpainting [fast] | $0.045 首MP + $0.006/MP |
| FLUX Outpainting [high] | $0.10/MP |
| FLUX Erase | $0.03 首MP + $0.004/MP |
| FLUX Deblur | $0.03 首MP + $0.004/MP |
| FLUX VTO | $0.0375 首MP + $0.005/MP |
| FLUX 3 Upscale [precise] | $0.035 / MP·s |
| FLUX 3 Upscale [creative] | $0.05 / MP·s |
| FLUX Video Edit [fast] | $0.03 / 秒 |

### 视频（按输出秒计费）

| 档位 | FLUX 3 Video (BFL) | fal FLUX 3 |
| --- | --- | --- |
| HD / 720p | $0.17/s（**Draft $0.06/s**） | $0.17/s |
| FHD / 1080p | $0.29/s | $0.29/s |
| QHD / 2K | $0.40/s | $0.32/s |
| UHD / 4K | $0.80/s | $0.47/s |
| v2v HD | $0.41/s | — |

10 秒 720p 成片 = $1.70；Draft 先试 = $0.60/10s。

## C. 自建：FLUX 3 Image Klein（真·零边际成本）

- 2026-10-15 开源权重：`black-forest-labs/flux-3-image-klein`（HF gated，需接受协议）
  - distilled（16 步，无 guidance）= 出图用；base（50 步，CFG 3.5）= 微调/LoRA 用
  - 5.6B transformer + Qwen3-VL-4B-Instruct 文本编码器（**Apache 2.0**）
  - 权重：BF16 11.2GB / FP8 5.6GB / INT8 5.8GB（ComfyUI 仓库有 int8-convrot / NVFP4）
- 显存：BFL 称 INT8 可上 8GB 卡（3070 级）；但 transformer+encoder 的 INT8 合计 10.7GB，
  8GB 必须 offload。另需 9B 级 prompt upsampler（约 18.8GB BF16 / 9.8GB INT8），
  不挂 upsampler 质量明显下滑。**实际规划按 32GB 卡算。**
- 速度（BFL 自报）：RTX 5090 INT8 约 4s/1MP，RTX 4070 约 <9s/1MP
- License：FLUX Kommunity License —— 个人 / 年收入 <$5M 的公司可免费商用；以上需商业授权
- 能力对齐旗舰：原生 4K、bbox（0~1000 网格，`[y_min,x_min,y_max,x_max]`）、最多 10 张参考图
- 生态：ComfyUI / Diffusers / vLLM-Omni / SGLang-Diffusion 首日支持

## D. 第三方渠道

| 渠道 | FLUX 3 Image | Klein | 备注 |
| --- | --- | --- | --- |
| fal | 发布期五折 $0.0205(768) / $0.024(1K)，**促销 10-08 已结束** | $0.009/1K | t2i 与 edit 分开两个端点；`enable_prompt_expansion` 默认 false |
| Replicate | 首周半价 | — | `images` 最多 10 张，**传图即切 edit 模式**；grounding 默认开 |
| OpenRouter | 发布价同上 | — | 按分辨率计价，一次请求带多参考图编辑 |
| fal FLUX 3 Max（视频） | — | — | $0.17/s 720p，**75% off = $0.0425/s，至 11-07** |

## E. 结论：性价比排序

1. **playground 4 件套（$0）** —— 已经吃下 1 个（precise-editing），
   `outpainting` / `erase` 实测可出图，`vto` 待跑通。等于把官方 $0.03~0.10/张的
   三个能力也变成免费，且都是**同步单请求**，接入成本比三段式低得多。
2. **Klein（$0.008~0.014 或自建）** —— 官方价的 1/5，能力只差最后一档质量；
   有 32GB 卡就是零边际成本，且可 LoRA 定制。
3. **fal / Replicate / OpenRouter 促销** —— 只在发布期便宜，现已回归原价，不建议长期依赖。
4. **视频只走 Draft 试片** —— $0.06/s 是唯一不肉疼的入口；成片再上 720p $0.17/s。

## F. 复现脚本

- `tools-probe/probe_outpainting.py`（同步扩图，含产物下载）
- `tools-probe/probe_erase.py`（CurlMime multipart 擦除）
- 探针图与产物：本次落在 `/tmp/bfl_probe/`（base.png / outpainting_result.png / erase_result.png）
