"""并发驱动：全形态 + size 两套矩阵 10 用例 4 线程并发跑。

用法：python tests/run_concurrent.py           # 全部 10 发
      python tests/run_concurrent.py g1 n1    # 只跑指定用例
"""
import concurrent.futures
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import matrix_prod as mp  # noqa: E402
import size_matrix_prod as sz  # noqa: E402

JOBS = [(f"mp.{c.__name__}", c) for c in mp.ALL] + [(f"sz.{c.__name__}", c) for c in sz.CASES]

if __name__ == "__main__":
    only = set(sys.argv[1:]) or None
    jobs = [j for j in JOBS if not only or j[0].split(".", 1)[1] in only
            or j[0] in only]
    t0 = time.time()
    print(f"并发跑 {len(jobs)} 发（4 workers）...", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(fn): name for name, fn in jobs}
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001
                mp.record(name, False, {"error": str(exc)[:200]})
    time.sleep(0.5)
    results = mp.RESULTS + sz.RESULTS
    ok = sum(r["ok"] for r in results)
    wall = time.time() - t0
    print(f"\nCONCURRENT MATRIX: {'ALL PASS' if ok == len(results) else 'HAS FAILURES'} "
          f"({ok}/{len(results)})  wall={wall:.0f}s", flush=True)
    json.dump(results, open(Path(__file__).parent / "concurrent_results.json", "w"),
              ensure_ascii=False, indent=1)
