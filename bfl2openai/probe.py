"""端到端探针：定期用真实接口跑一张最小图。

为什么需要它：`/healthz` 只能证明进程活着，sidecar 的 `pages_ready` 只能证明
页面池非空 —— 两者都不能证明「用户真能拿到图」。2026-11-06 凌晨池子空掉
1.5 小时，应用日志里 0 个 5xx，自报状态骗过了所有指标。

探针走和外部客户端一样的路径：
  POST /v1/flux-3-image（带 x-key）→ 裸 GET polling_url 轮询 → 下载 sample 校验 PNG
唯一区别是把 polling_url 的 host 换回容器回环，**只测服务本身**；公网抖动
交给外部监控，不让它污染这条信号。

开关：BFL2OPENAI_PROBE_ENABLE=1（默认关：本地/单测不产生真实上游任务）
间隔：BFL2OPENAI_PROBE_INTERVAL_S（默认 1800）
超时：BFL2OPENAI_PROBE_TIMEOUT_S（默认 150）
基址：BFL2OPENAI_PROBE_BASE（默认容器回环 http://127.0.0.1:8000）
档位：BFL2OPENAI_PROBE_RESOLUTION（默认 768sq，最省的一档）
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from urllib.parse import urlsplit, urlunsplit

from curl_cffi.requests import AsyncSession

from . import store

log = logging.getLogger("bfl2openai.probe")

ENABLE = os.environ.get("BFL2OPENAI_PROBE_ENABLE", "") == "1"
INTERVAL_S = float(os.environ.get("BFL2OPENAI_PROBE_INTERVAL_S", "1800"))
TIMEOUT_S = float(os.environ.get("BFL2OPENAI_PROBE_TIMEOUT_S", "150"))
BASE = os.environ.get("BFL2OPENAI_PROBE_BASE",
                     "http://127.0.0.1:8000").rstrip("/")
RESOLUTION = os.environ.get("BFL2OPENAI_PROBE_RESOLUTION", "768sq")
STARTUP_DELAY_S = float(os.environ.get("BFL2OPENAI_PROBE_STARTUP_DELAY", "90"))

#: 探针和外部客户端用同一把钥匙；缺省（本地/未设鉴权）就不带头。
KEY = (os.environ.get("BFL_NATIVE_API_KEY")
       or os.environ.get("BFL2OPENAI_API_KEY") or "")

_LAST_KEY = "probe:last"
_LAST_TTL_S = int(max(INTERVAL_S * 3, 3600))


def _png_size(data: bytes) -> str:
    """校验字节确实是可解码的图片，返回 WxH。坏字节抛 ValueError。"""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        size = f"{im.size[0]}x{im.size[1]}"
        im.verify()
    return size


def _localise(url: str) -> str:
    """polling_url 的 host 换成容器回环：只测服务，排除 nginx/公网。"""
    u = urlsplit(url)
    host = BASE.split("//", 1)[-1]
    return urlunsplit(("http", host, u.path, u.query, ""))


async def run_once() -> dict:
    """跑一次端到端探针。不抛异常，失败信息放进返回值。"""
    t0 = time.time()
    steps: list[dict] = []

    def done(ok: bool, error: str | None = None, **extra) -> dict:
        return {"ok": ok, "error": error, "steps": steps,
                "elapsed_s": round(time.time() - t0, 1),
                "ts": int(time.time()), **extra}

    headers = {"x-key": KEY} if KEY else {}
    payload = {"prompt": "a plain gray square on white",
               "resolution": RESOLUTION, "aspect_ratio": "1:1"}
    try:
        async with AsyncSession(trust_env=False, timeout=30) as s:
            r = await s.post(f"{BASE}/v1/flux-3-image", json=payload,
                             headers=headers)
            steps.append({"step": "submit", "code": r.status_code,
                          "ms": int((time.time() - t0) * 1000)})
            if r.status_code != 200:
                return done(False,
                            f"submit HTTP {r.status_code}: {r.text[:140]}")
            doc = r.json()
            poll = _localise(doc.get("polling_url") or "")
            if not poll:
                return done(False, f"submit 200 但没有 polling_url: {doc}")

            deadline = time.monotonic() + TIMEOUT_S
            final: dict = {}
            rounds = 0
            while time.monotonic() < deadline:
                q = await s.get(poll)          # 裸 GET：和官方客户端一致
                rounds += 1
                if q.status_code != 200:
                    return done(False,
                                f"poll HTTP {q.status_code}: {q.text[:140]}")
                final = q.json()
                if final.get("status") in ("Ready", "Error"):
                    break
                await asyncio.sleep(2)
            steps.append({"step": "poll", "rounds": rounds,
                          "status": final.get("status"),
                          "ms": int((time.time() - t0) * 1000)})
            if final.get("status") != "Ready":
                err = (final.get("details") or {}).get("error") or "no terminal state"
                return done(False, f"{final.get('status')}: {str(err)[:140]}")

            sample = (final.get("result") or {}).get("sample")
            if not sample:
                return done(False, "Ready 但没有 result.sample")
            m = await s.get(sample)
            steps.append({"step": "sample", "code": m.status_code,
                          "bytes": len(m.content),
                          "ms": int((time.time() - t0) * 1000)})
            if m.status_code != 200:
                return done(False, f"sample HTTP {m.status_code}")
            try:
                size = _png_size(m.content)
            except Exception as exc:  # noqa: BLE001
                return done(False, f"sample 不是有效图片: {exc}")
            return done(True, None, image=size)
    except Exception as exc:  # noqa: BLE001 - 传输层/超时
        return done(False, f"{type(exc).__name__}: {str(exc)[:160]}")


async def _loop() -> None:
    await asyncio.sleep(STARTUP_DELAY_S)   # 让页面池先建好
    while True:
        # 多 worker 去重：同一时间桶只有一个 worker 抢到锁，避免 3 份真实任务
        bucket = int(time.time() // INTERVAL_S)
        try:
            claimed = await store.set_nx(f"probe:claim:{bucket}",
                                         {"pid": os.getpid()},
                                         ttl_s=int(INTERVAL_S) + 60)
        except Exception:  # noqa: BLE001 - 存储挂了宁可多跑一次
            claimed = True
        if claimed:
            res = await run_once()
            try:
                await store.put(_LAST_KEY, res, ttl_s=_LAST_TTL_S)
            except Exception:  # noqa: BLE001
                pass
            if res["ok"]:
                log.info("probe ok image=%s elapsed=%.1fs",
                         res.get("image"), res["elapsed_s"])
            else:
                log.warning("probe FAILED elapsed=%.1fs error=%s",
                            res["elapsed_s"], res["error"])
        await asyncio.sleep(max(30.0, INTERVAL_S / 4))


async def main() -> None:
    """独立进程入口：`python -m bfl2openai.probe`。

    为什么独立跑：探针必须能在被测对象挂掉时继续报告。放在 app 的 lifespan
    里意味着 app 一死探针也死 —— 那正是最需要它的时候。镜像里带这个入口，
    compose 单开一个容器复用同一镜像即可，不需要额外打包。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("probe standalone: base=%s interval=%.0fs timeout=%.0fs "
             "resolution=%s", BASE, INTERVAL_S, TIMEOUT_S, RESOLUTION)
    # 首次先等启动期：探针和 app/sidecar 同时拉起时，8011 还没监听，
    # 会把「启动竞态」记成一次失败（2026-11-08 实测）。
    delay = STARTUP_DELAY_S
    while True:
        await asyncio.sleep(delay)
        res = await run_once()
        try:
            await store.put(_LAST_KEY, res, ttl_s=_LAST_TTL_S)
        except Exception:  # noqa: BLE001 - 状态写不进去也别停探针
            pass
        if res["ok"]:
            log.info("probe ok image=%s elapsed=%.1fs",
                     res.get("image"), res["elapsed_s"])
        else:
            log.error("probe FAILED elapsed=%.1fs error=%s",
                      res["elapsed_s"], res["error"])
        # 成功 → 正常间隔；失败 → 2 分钟后再确认，别让一次抖动挂 30 分钟
        delay = INTERVAL_S if res["ok"] else min(INTERVAL_S, 120.0)


def start() -> None:
    if not ENABLE:
        log.info("probe disabled (set BFL2OPENAI_PROBE_ENABLE=1 to enable)")
        return
    asyncio.create_task(_loop())
    log.info("probe enabled: every %.0fs via %s resolution=%s",
             INTERVAL_S, BASE, RESOLUTION)


async def status() -> dict:
    last = None
    try:
        last = await store.get(_LAST_KEY)
    except Exception:  # noqa: BLE001
        pass
    return {"enabled": ENABLE, "interval_s": INTERVAL_S,
            "resolution": RESOLUTION, "last": last}


async def summary() -> dict:
    """给 /healthz 的轻量摘要（不带 steps / 完整 error）。"""
    last = None
    try:
        last = await store.get(_LAST_KEY)
    except Exception:  # noqa: BLE001
        pass
    if not last:
        return {"enabled": ENABLE, "last_ok": None, "age_s": None}
    return {"enabled": ENABLE,
            "last_ok": bool(last.get("ok")),
            "age_s": int(time.time()) - int(last.get("ts") or 0),
            "elapsed_s": last.get("elapsed_s"),
            "error": (last.get("error") or "")[:120] or None}


if __name__ == "__main__":
    asyncio.run(main())
