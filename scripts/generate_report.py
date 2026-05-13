from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--out", default="reports/final_report.md")
    args = parser.parse_args()
    metrics = json.loads(Path(args.metrics).read_text())
    lines = [
        "# Báo cáo Reliability Agent - Day 10",
        "",
        "## Tóm tắt metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key, value in metrics.items():
        if key == "scenarios":
            continue
        lines.append(f"| {key} | {value} |")
    lines += ["", "## Chaos scenarios", "", "| Scenario | Status |", "|---|---|"]
    for key, value in metrics.get("scenarios", {}).items():
        lines.append(f"| {key} | {value} |")
    lines += [
        "",
        "## Phân tích",
        "",
        "Gateway dùng cache trước, sau đó route qua primary/backup provider được bảo vệ bởi circuit breaker.",
        "Fallback path hoạt động khi primary lỗi hoặc circuit mở; static fallback chỉ dùng khi toàn bộ provider không khả dụng.",
        "Điểm cần cải thiện trước production là chia sẻ circuit breaker state giữa nhiều instance và thêm load test song song.",
    ]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
