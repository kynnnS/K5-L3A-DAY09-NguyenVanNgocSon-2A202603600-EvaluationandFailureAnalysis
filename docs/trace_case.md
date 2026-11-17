# Trace case: từ trace tới quyết định sai đầu tiên

Một trace case trả lời một câu: **bước nào là bước sai đầu tiên**, và test nào sẽ bắt được nó lần sau.

```bash
cd src
python -m lab9.trace_case --shape swarm --claim C-002 --seed 55               # trial fail trong baseline
python -m lab9.trace_case --shape seeded_regression --claim C-002 --seed 11
python -m lab9.trace_case --shape fixed --claim C-002 --seed 55               # đối chứng
```

## Case: C-002 — tin ticket hơn DB

| Trường | Nội dung |
|---|---|
| Trial | `swarm C-002@55`, run `E-swarm-20261117-2cb3` (`reports/baseline_v1.json`, dòng `E-002`/seed 55). Lặp lại ở `seeded_regression C-002@{11..55}` (run `E-seeded_regression-20261117-2cb3`). |
| Triệu chứng | `TrialPass` FAIL (`status resolved != denied`), `refund_amount_correct` FAIL, `db_fact_kept` FAIL. Ledger ghi **20.000đ** cho O-202; nhãn: `denied`, 0đ. Ở `seeded_regression` reply còn nói "Dạ, LogiX đã hoàn 20.000đ cho đơn O-202." — trôi chảy, khớp ledger, nhưng ledger sai. |
| Bước sai đầu tiên | **Event 3 `model_override`** — `{"delay_days": 5}` |
| Vì sao là bước *đầu tiên* | Ev 1 `handoff` đúng (task `check_applicable_policy`, scope `owner_order_only`). Ev 2 `investigator` đọc đúng DB: O-202 `delay_days=2`, `status=delivered` → dưới ngưỡng SHIP-LATE-03 (`min_delay_days=3`). Ev 3 ghi đè 2 → 5 bằng số khách tự khai trong ticket "giao trễ 5 ngày". Ev 4 `quote` 20.000 chỉ là hệ quả đúng của input đã bị sửa. |
| Failure mode | **FM-03 ticket_trust** (`docs/failure_taxonomy.md`) |
| Đối chứng | `fixed C-002@55`: ev 2 giống hệt; **không có** `model_override`; ev 3 `quote {"amount_vnd": 0, "reason": "no_entitlement"}`; ev 4 reply "chưa đủ điều kiện hoàn" → PASS. Khác biệt duy nhất là bước 3. Nguyên nhân gốc ở reference swarm: `seed % 5 == 0` thì tin ticket (chỉ seed 55 trong bộ 11..55), nên `pass_rate` baseline = 0.9857 chứ không phải 1.0. |
| Regression test | `src/tests/test_lab9_trace_case.py::test_c002_ticket_override_is_first_wrong_step` — chạy trial thật qua `lab9.trace_case.investigate`, assert event `model_override` đứng ngay sau `investigator`, `DbFactKept` FAIL và ledger 20.000 trên `seeded_regression`; cùng claim@seed trên `fixed` thì không có override, `DbFactKept` PASS, ledger 0. Đã thấy fail trên variant lỗi và pass trên `fixed`. Ngoài ra item `E-002` mang `provenance.from_trace` trỏ về trial này. Test thứ hai `test_c008_misreport_caught_only_by_reply_metric` khoá FM-08. |
| Chưa chắc chắn | Trace không cho biết *vì sao* model chọn tin ticket (MockLLM quyết định theo seed, không có reasoning). Với LLM thật, tỉ lệ override có thể khác 1/5 và phụ thuộc cách viết ticket; 5 seed không đủ để ước lượng tỉ lệ đó. Trace cũng không chứng minh `quiet_override` (cùng lỗi, không ghi event) bị bắt bởi `db_fact_kept` — chỉ `refund_amount_correct`/`pass_rate` ở outcome mới bắt. |

## Quy tắc

- Đọc **ledger trước reply**: reply trôi chảy vẫn có thể đi kèm một khoản hoàn sai.
- "Bước sai đầu tiên" phải dẫn số event cụ thể, không phải "agent bị nhầm".
- Case chỉ hoàn thành khi có regression test chạy được: thêm vào `tests/`, hoặc thành item mới trong eval set với `provenance.from_trace`.
