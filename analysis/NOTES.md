# flux-tools.bfl.ai/precise-editing 逆向笔记（2026-10-09）

## 方法

1. bsk 打开页面 → `debug start` 先于导航抓包。
2. 进入编辑器、填 prompt、点 GENERATE，抓到三段式 API：
   `generate` (multipart POST) → `status?pollToken` (GET 轮询) → `media?imageToken` (GET 取图)。
3. 扩展抓不到 multipart 请求体 → 页面内 hook `window.fetch`，枚举 `FormData` 条目，拿到全部字段。
4. 前端 chunk 用页面内 `fetch` 原文取回（站点 JS 在 analysis/js/chunks/）。
   注意：API 客户端代码（flux3-image 路径构造）不在已抓到的 chunk 里，是运行时懒加载的；
   静态搜 "flux3-image" 无命中，别再浪费时间全量翻 chunk，运行时 hook 是权威。
5. curl_cffi (impersonate=chrome, trust_env=False) 直连验证 + E2E。

## 坑

- **POST /api/flux3-image/generate 缺 Origin/Referer/Sec-Fetch-* → 403 空体**。
  GET 端点不需要这些头。这是本次唯一卡点，报错为空容易误判成 WAF 拦截。
- Vercel Security Checkpoint 只拦普通 curl（返回 HTML 拦截页），curl_cffi chrome 指纹直接过。
- 端点全部匿名：`/api/credits/context`(401) 与 `/api/auth/refresh`(401) 是站点计费/登录体系，
  与生成链路无关，别在上面花时间。
- media?imageToken 有时效（约 25 分钟前的 token 下载会挂起/失败），出图后立刻取。
- bsk 会话环境易变：任务 tab 两次被导航去别的页面、会话被提前回收。
  一切命令显式带 `--tab-id`；证据（hook 结果、token、图片）当轮落盘。
- curl_cffi 0.16 上传用 `CurlMime` + `multipart=`，`files=` 已移除（NotImplementedError）。

## 上游契约（权威，来自运行时 hook + 实测）

见 bfl2openai/README.md「逆向结论」一节，两个字段未验证：
`references` 非空结构、`aspectRatio`/`resolution` 合法枚举（仅实测 auto / 1.5k）。

## 频控实测（2026-10-09）

方法：64→256px 纯色微图、prompt="red"、只提交不轮询不取图（最小消耗探针，
tests/probe_ratelimit.py）。当天此 IP 先前已有 4 次正常生成。

- 顺序 40 连发（间隔 2-3s）：**全部 200**，无 x-ratelimit-* / retry-after 头。
- 瞬时 10 并发：**全部 200**（1.4~2.5s 返回）。
- 当天累计 54+ 次成功提交，未触发任何 429/403/额度错误。
- 唯一实测到的限制是**校验型**：图片 <256×256 → 422 `Images[0] dimensions must be
  at least 256x256 pixels`（校验在配费之前，不消耗额度）。该 422 body 同时暴露
  上游内部结构：`{"detail":[{"loc":["body","images"],...}]}` 是 FastAPI/Pydantic
  风格，image 以 base64 进 `images` 字段——flux-tools 后端是包了一层的内部模型 API。
- 结论：**没有可观测的"每天免费 N 张"上限**（≤54 张区间内），也无短窗口频控。
  站点有登录/credits 体系（insufficient_credits/moderation/billing_access 错误码），
  匿名链路疑似尚未接计费或上限极高，BFL 随时可收紧——商用别依赖，走 api.bfl.ai。
- "每天重置"无法在一次会话内验证，需隔日复跑 probe 脚本对比。

## 产出

- bfl2openai/：OpenAI Images 兼容服务（E2E 通过：/v1/images/edits 全链路 33s 出 1536x1536 PNG）。
- tests/e2e_result.png、tests/e2e_result_direct.png：真实生成结果。
