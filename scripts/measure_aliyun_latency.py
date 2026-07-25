"""实测阿里云文本审核 PLUS 的调用延时（用于评估入站同步审核给主链路增加的 RT）。

用法：
    .venv/bin/python scripts/measure_aliyun_latency.py [--n 10] [--timeout-ms 5000]

读取 .env 中的 ALIYUN_ACCESS_KEY_ID/SECRET。直接调用 _review_text_with_aliyun_impl，
绕过失败率告警，避免污染监控状态。会打印冷启动（含建连）与稳态（连接复用）两段延时。
"""

import argparse
import statistics
import time

from app.config import settings
from app.platform.moderation import aliyun_review

# 一条明显安全的文本和一条疑似命中的文本，观察命中与否对 RT 是否有差异。
SAMPLES = [
    "今天天气不错，我们聊聊周末去哪里玩吧。",
    "你最近过得怎么样，有没有遇到什么开心的事情？",
]


def _run_once(text: str, idx: int):
    t0 = time.monotonic()
    result = aliyun_review._review_text_with_aliyun_impl(
        account_id="latency-probe",
        text=text,
        data_id=f"latency-probe-{idx}",
    )
    wall_ms = (time.monotonic() - t0) * 1000.0
    return wall_ms, result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=10, help="稳态测量次数")
    parser.add_argument("--timeout-ms", type=int, default=None, help="覆盖 read/connect 超时（默认用配置值）")
    args = parser.parse_args()

    if args.timeout_ms is not None:
        settings.moderation_aliyun_timeout_ms = args.timeout_ms

    ak = (getattr(settings, "aliyun_access_key_id", "") or "").strip()
    if not ak:
        print("ERROR: 未配置 ALIYUN_ACCESS_KEY_ID，无法实测。")
        return

    print(
        f"endpoint={settings.moderation_aliyun_endpoint} "
        f"service={settings.moderation_aliyun_service} "
        f"timeout_ms={settings.moderation_aliyun_timeout_ms}"
    )

    # 冷启动：第一次调用包含客户端构建 + TLS 建连。
    cold_ms, cold = _run_once(SAMPLES[0], 0)
    print(f"\n[冷启动] wall={cold_ms:.0f}ms sdk_latency={cold.latency_ms}ms "
          f"level={cold.level} error={cold.error}")

    # 稳态：连接复用后的逐次调用。
    warm = []
    for i in range(1, args.n + 1):
        text = SAMPLES[i % len(SAMPLES)]
        wall_ms, res = _run_once(text, i)
        warm.append(wall_ms)
        print(f"[稳态 {i:2d}] wall={wall_ms:6.0f}ms sdk_latency={res.latency_ms}ms "
              f"level={res.level} error={res.error}")

    if warm:
        warm_sorted = sorted(warm)
        p50 = statistics.median(warm)
        p90 = warm_sorted[min(len(warm_sorted) - 1, int(round(0.9 * (len(warm_sorted) - 1))))]
        print(
            f"\n稳态汇总(n={len(warm)}): "
            f"min={min(warm):.0f}ms  p50={p50:.0f}ms  p90={p90:.0f}ms  "
            f"max={max(warm):.0f}ms  avg={statistics.mean(warm):.0f}ms"
        )


if __name__ == "__main__":
    main()
