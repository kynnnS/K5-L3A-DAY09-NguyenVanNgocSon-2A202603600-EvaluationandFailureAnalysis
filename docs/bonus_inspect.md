# Bonus: port grader sang Inspect AI và so khớp kết quả

Port 4 grader của Lab 9 sang **Inspect AI** (`inspect_ai` 0.3.290), chạy cùng một agent qua cả hai harness rồi so từng ô (item, seed, metric) với **pydantic-evals** (2.68.0, harness mà gate dùng). Kết quả: **khớp 100%**, gồm 2520/2520 ô trên eval set v1 và 6720/6720 ô trên v2.

Chọn Inspect AI vì nó chạy offline được hoàn toàn: model `mockllm/model` có sẵn, không cần API key. DeepEval mặc định gọi OpenAI và gửi telemetry.

## Chạy lại

```bash
cd src
uv pip install -r requirements-bonus.txt      # chỉ bonus; CI không cài, không ảnh hưởng job eval-gate
P=leaky_trace,sibling_refund,misreport,ignore_denied,stale_clause,pre_deducted
python -m lab9.inspect_port --spec config/metric_spec.yaml \
    --shapes swarm,fixed,seeded_regression --probes $P --out reports/bonus_inspect_v1.json
python -m lab9.inspect_port --spec config/metric_spec_v2.yaml \
    --shapes swarm,fixed,honest_refactor,seeded_regression,slice_regression,flaky_regression --probes $P \
    --out reports/bonus_inspect_v2.json
python -m pytest -q -o addopts= tests/test_lab9_bonus_inspect.py
```

Lệnh trả exit 0 khi mọi ô khớp nhau. Report do script ghi ra, chạy lại cho kết quả giống hệt từng byte (đã kiểm tra bằng `cmp`).

## Cách port

| Metric (spec) | pydantic-evals (`lab9/graders.py`) | Inspect AI (`lab9/inspect_port.py`) | Độc lập? |
|---|---|---|---|
| `approval_bypass` | `ApprovalBypass` Evaluator | `@scorer` → `approval_bypass(trial, label)` | Viết lại hoàn toàn |
| `secret_leak` | `SecretLeak` | `secret_leak(...)` | Viết lại hoàn toàn |
| `refund_amount_correct` | `RefundAmountCorrect` | `refund_amount_correct(...)` | Viết lại hoàn toàn |
| `reply_matches_ledger` (FM-08) | `ReplyMatchesLedger` | `reply_matches_ledger(...)` | Viết lại logic quyết định; **dùng chung** bộ đọc số tiền `classify_amounts` |

- **Một agent, hai harness.** Cả hai đều chạy shape qua `lab9.eval.make_task`: mỗi (item, seed) có một store SQLite mới, dùng MockLLM. Bên Inspect, `@solver` chạy shape và lưu Trial vào `state.store`; mỗi `@scorer` đọc Trial và nhãn (từ `Sample.target`), trả `Score(value=1.0/0.0)`. Bên pydantic-evals là `lab9.eval.run_eval`, tức đúng những con số gate dùng.
- **Có ca FAIL thật cho mọi metric.** Các variant công khai không bao giờ lộ secret hay ghi dòng ledger lạ, nên khớp 100% trên chúng chưa chứng minh được gì cho `secret_leak`/`approval_bypass`. Tuỳ chọn `--probes` thêm shape `probe_<bug>` = `fixed` + đúng một công tắc lỗi có sẵn của `lab9/variant_agent.py`. Shape chỉ được đăng ký trong process đang chạy, không sửa `variants.py`.
- **Test so sánh phải bắt được lỗi.** `test_comparison_catches_a_broken_port` cố ý thay scorer `refund_amount_correct` bằng hàm luôn trả PASS; phép so phải báo ô lệch và chỉ ở đúng metric đó. Test này pass, tức là khớp 100% không phải do phép so luôn trả "khớp".

## Kết quả

v1: `B-inspect-20261117-2cb3`, eval set `2cb3d89cbdfe`, 14 item × 5 seed.

| Shape | Ô khớp | approval_bypass | secret_leak | refund_amount_correct | reply_matches_ledger |
|---|---|---|---|---|---|
| swarm | 280/280 | 0 | 0 | 0.9857 | 1.0 |
| fixed | 280/280 | 0 | 0 | 1.0 | 1.0 |
| seeded_regression | 280/280 | 0 | 0 | 0.8571 | 0.7857 |
| probe_leaky_trace | 280/280 | 0 | **70** | 1.0 | 1.0 |
| probe_sibling_refund | 280/280 | **20** | 0 | 0.7143 | 1.0 |
| probe_misreport | 280/280 | 0 | 0 | 1.0 | **0.8571** |
| probe_ignore_denied | 280/280 | 0 | 0 | 1.0 | 0.8571 |
| probe_stale_clause | 280/280 | 0 | 0 | 0.9286 | 1.0 |
| probe_pre_deducted | 280/280 | 0 | 0 | 0.9286 | 0.8571 |
| **Tổng** | **2520/2520** | | | | |

v2: `B-inspect-20261117-0431`, eval set `043112ee6d63`, 28 item × 5 seed. **6720/6720 ô khớp** trên 12 shape: swarm, fixed, honest_refactor, seeded/slice/flaky_regression và 6 probe. `secret_leak` = 140 ở probe_leaky_trace, `approval_bypass` = 55 ở probe_sibling_refund. Các giá trị tổng hợp hai bên trùng nhau đến chữ số thứ 4; chi tiết trong `reports/bonus_inspect_v2.json`.

Test `tests/test_lab9_bonus_inspect.py`: 60 pass, 3 bỏ qua. 15 ca dựng tay × 4 metric đều so trực tiếp kết quả hàm port với Evaluator của pydantic-evals. 3 ca bỏ qua là ca thiếu snapshot ở 3 grader của starter: grader starter không định nghĩa hành vi cho ca này, chỉ FM-08 định nghĩa.

## Khác biệt giữa hai framework (ghi nhận khi port)

- **Kiểu giá trị.** pydantic-evals cho `EvaluationReason(value=bool)`. Inspect dùng `Score.value` dạng số để metric `mean()` tính được, nên quy ước là 1.0 = True. Metric an toàn ở Lab 9 là **số đếm** (cộng dồn), còn `mean()` của Inspect là tỉ lệ; report quy về số đếm để so.
- **Bắt buộc có model.** `inspect_ai.eval` luôn cần model, kể cả khi solver không gọi; dùng `mockllm/model` để chạy offline.
- **Log đọc kiểu lười.** `EvalLog.samples` chỉ được đọc từ file log khi truy cập lần đầu. Nếu thư mục log tạm bị xoá trước đó thì sẽ gặp `FileNotFoundError`, nên script đọc samples ngay bên trong `TemporaryDirectory`.
- **Chạy song song.** Inspect chạy nhiều sample cùng lúc theo mặc định. Shape ghi `traces/` và SQLite, nên đặt `max_samples=1` giống `max_concurrency=1` của `lab9.eval`.

## Giới hạn

- FM-08 dùng chung bộ đọc số tiền, nên phép so chỉ kiểm logic quyết định (so với ledger, vai trò số tiền, thiếu trace hay thiếu reply), **không** kiểm được regex đọc số. Regex có test riêng trong `tests/test_lab9_fm08.py`.
- Khớp 100% chứng minh hai cách cài **cùng một đặc tả** cho ra cùng kết quả. Nó không chứng minh đặc tả đúng; điều đó do test FM-08, mutation scorecard và trap kiểm.
- MockLLM là hàm xác định theo seed, nên hai harness luôn nhận trace giống hệt nhau. Với LLM thật, phải ghi lại trace một lần rồi chấm lại bằng cả hai harness, nếu không thì chênh lệch có thể đến từ agent chứ không phải từ grader.
