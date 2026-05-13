# Báo cáo Reliability Agent - Day 10

## 1. Tóm tắt kiến trúc

Gateway nhận request, kiểm tra cache trước, sau đó gọi provider theo chuỗi fallback được bảo vệ bởi circuit breaker riêng cho từng provider. Khi tất cả provider lỗi hoặc circuit đang mở, gateway trả static fallback để fail closed thay vì treo request.

```text
User Request
    |
    v
[ReliabilityGateway]
    |
    +--> [Redis/Memory Cache] -- hit --> cached response
    |
    v miss
[CircuitBreaker: primary] -- closed/half-open --> Provider primary
    |
    v open/failure
[CircuitBreaker: backup]  -- closed/half-open --> Provider backup
    |
    v all failed/open
[Static fallback message]
```

## 2. Cấu hình

| Setting | Value | Lý do |
|---|---:|---|
| primary fail_rate | 0.25 | Tạo baseline có lỗi thật để kiểm tra fallback. |
| backup fail_rate | 0.05 | Backup vẫn có thể lỗi nhẹ, gần với production hơn backup hoàn hảo. |
| failure_threshold | 3 | Đủ nhạy để mở circuit nhanh, nhưng tránh mở vì 1-2 lỗi ngẫu nhiên. |
| reset_timeout_seconds | 2 | Cho provider thời gian hồi phục ngắn trước khi probe half-open. |
| success_threshold | 1 | Một probe thành công là đủ để đóng circuit trong lab nhỏ. |
| cache backend | redis | Dùng shared cache để nhiều instance thấy cùng dữ liệu. |
| cache TTL | 300 | 5 phút phù hợp FAQ/policy ngắn hạn, giới hạn rủi ro stale. |
| similarity_threshold | 0.92 | Ngưỡng cao để giảm false-hit semantic cache. |
| load_test requests | 200 | Phase 6 yêu cầu 200+ request; mỗi scenario chạy 200 request. |

## 3. SLO

| SLI | SLO target | Actual value | Đạt? |
|---|---|---:|---|
| Availability | >= 99% | 98.67% | Không |
| Latency P95 | < 2500 ms | 516.35 ms | Có |
| Fallback success rate | >= 95% | 97.80% | Có |
| Cache hit rate | >= 10% | 26.17% | Có |
| Recovery time | < 5000 ms | 3470.55 ms | Có |

## 4. Metrics chính

Nguồn: `reports/metrics_with_cache.json`, chạy với Redis cache và 3 chaos scenario.

| Metric | Value |
|---|---:|
| total_requests | 600 |
| availability | 0.9867 |
| error_rate | 0.0133 |
| latency_p50_ms | 276.41 |
| latency_p95_ms | 516.35 |
| latency_p99_ms | 547.20 |
| fallback_success_rate | 0.9780 |
| cache_hit_rate | 0.2617 |
| circuit_open_count | 45 |
| recovery_time_ms | 3470.553 |
| estimated_cost | 0.170696 |
| estimated_cost_saved | 0.157000 |

## 5. So sánh cache

Nguồn: `reports/cache_comparison_without_cache.json` và `reports/cache_comparison_with_cache.json`, chạy default scenario 200 request. Redis cache là warm cache vì trước đó chaos run đã ghi một số key.

| Metric | Không cache | Có Redis cache | Delta |
|---|---:|---:|---:|
| availability | 0.9750 | 1.0000 | +0.0250 |
| latency_p50_ms | 224.97 | 1.57 | -223.40 ms |
| latency_p95_ms | 521.35 | 477.15 | -44.20 ms |
| estimated_cost | 0.100560 | 0.019960 | -0.080600 |
| estimated_cost_saved | 0.000000 | 0.159000 | +0.159000 |
| cache_hit_rate | 0.0000 | 0.7950 | +0.7950 |

## 6. Redis shared cache

In-memory cache không đủ cho multi-instance deployment vì mỗi process giữ cache riêng; instance A vừa cache một câu trả lời thì instance B vẫn miss và tiếp tục gọi provider. `SharedRedisCache` lưu query/response trong Redis Hash với TTL, nên các gateway instance dùng chung namespace `rl:cache:*`.

Evidence shared state:

```text
('shared report response', 1.0)
```

Output trên được tạo bằng hai instance `SharedRedisCache` khác nhau: `c1.set(...)`, sau đó `c2.get(...)` đọc lại cùng response từ Redis.

Redis CLI evidence:

```text
rl:cache:8baa2cfa11fa
rl:cache:9e413fd814eb
rl:cache:b6af19a70a20
rl:cache:095946136fea
```

Redis cache thêm network hop nhỏ, nhưng trong comparison run P50 giảm mạnh từ `224.97 ms` xuống `1.57 ms` nhờ cache hit rate `79.50%`. P95 giảm nhẹ hơn vì vẫn còn miss/fallback path phải gọi provider thật.

## 7. Chaos scenarios

| Scenario | Kỳ vọng | Quan sát | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary fail 100%, circuit mở, backup xử lý phần lớn request. | Fallback success rate tổng đạt 97.80%, circuit mở nhiều lần. | pass |
| primary_flaky_50 | Primary fail 50%, circuit dao động và traffic trộn primary/fallback. | Recovery time đo từ transition log là 3470.55 ms. | pass |
| cache_stale_candidate | Cache threshold thấp nhưng guardrail phải chặn false-hit khác năm. | Query 2024/2026 không trả cached response, false-hit được log. | pass |

## 8. Failure analysis

Điểm yếu còn lại: circuit breaker state vẫn là in-memory theo từng gateway instance. Nếu production scale ra nhiều pod, mỗi pod sẽ tự học trạng thái lỗi riêng; một pod có thể đã mở circuit nhưng pod khác vẫn tiếp tục gọi provider lỗi, gây retry storm ở cấp cụm.

Cách sửa trước production: đưa circuit state vào Redis hoặc một shared control plane, dùng atomic counter/TTL cho failure count và opened_at. Đồng thời cần thêm per-user rate limiting để tránh một nhóm request lỗi làm cạn capacity backup.

## 9. Next steps

1. Thêm Redis-backed circuit breaker state để đồng bộ trạng thái OPEN/HALF_OPEN/CLOSED giữa các instance.
2. Thêm concurrent load test bằng `ThreadPoolExecutor` theo `load_test.concurrency` để đo P95/P99 dưới áp lực song song.
3. Xuất Prometheus metrics cho request count, latency histogram, cache hit, fallback count và circuit state.
