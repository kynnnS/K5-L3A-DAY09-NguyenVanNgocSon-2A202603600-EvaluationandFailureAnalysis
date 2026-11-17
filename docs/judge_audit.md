# Judge audit

Chạy: `python -m lab9.judge --csv <file> --audit 5` (backend REPLAY: verdict của judge lưu trong CSV, không gọi model).
Lớp dương = `FAIL` (việc của judge là tìm lỗi).

| Tiêu chí | n | κ | Precision | Recall | Confusion (tp/fp/fn/tn) | Đạt FLOORS (κ≥0.6, P≥0.8, R≥0.7)? | Tỉ lệ fail quan sát → Rogan-Gladen | Quyết định |
|---|---|---|---|---|---|---|---|---|
| tone_ok | 100 | 0.757 | 0.857 | 0.800 | 24 / 4 / 6 / 66 | **PASS** (exit 0) | 0.28 → 0.30 | **dùng** (non-blocking, báo cáo) |
| helpful | 100 | 0.375 | 1.000 | 0.300 | 9 / 0 / 21 / 70 | **FAIL** (κ và R dưới sàn, exit 1) | 0.09 → 0.30 | **loại** — không được làm gate |

## ≥5 case audit thủ công

| # | Case | Judge nói | Người nói | Ai đúng? | Ghi chú / hành động sửa rubric |
|---|---|---|---|---|---|
| 1 | tone H-008 "Em đã hoàn tiền cho anh rồi nha…" | PASS | FAIL | Người | Xưng "em/anh" + "nha" quá suồng sã. Rubric thêm: *bắt buộc xưng "LogiX" / "anh/chị"; "em", "bạn", "nha" = FAIL*. |
| 2 | tone H-017 "Dạ, bạn ơi LogiX đã hoàn 25.000đ…" | PASS | FAIL | Người | Cùng lỗi xưng hô; judge bị mồi bởi "Dạ". Thêm 2 ví dụ few-shot FAIL có "Dạ" ở đầu nhưng xưng "bạn". |
| 3 | tone H-029 "Dạ, vui lòng chờ… không cần nhắn thêm." | PASS | FAIL | Người | Giọng ra lệnh. Rubric thêm: *câu mệnh lệnh cấm khách liên hệ = FAIL dù có từ lịch sự*. |
| 4 | tone H-042 "Dạ, đơn O-808 đã hoàn 120.000đ. Tiền về trong 3–5 ngày ạ." | FAIL | PASS | Người | Judge phạt vì "thiếu xin lỗi". Rubric ghi rõ: *xin lỗi KHÔNG bắt buộc khi kết quả có lợi cho khách; ngắn gọn ≠ cộc lốc*. |
| 5 | tone H-051 "LogiX đã refund 90.000đ…" | FAIL | PASS | Người | Style guide CS LogiX cho phép "refund". Thêm whitelist thuật ngữ vào rubric. |
| 6 | tone H-070 "…cho đơn O-909 ạ 😂😂" | PASS | FAIL | Người | Emoji cười trong khiếu nại. Rubric: *emoji = FAIL trong ngữ cảnh khiếu nại/hoàn tiền*. |
| 7 | tone H-081 "Dạ, đơn O-107 chưa đủ điều kiện hoàn…" | FAIL | PASS | **Judge** | Ghi chú dataset: người đọc nhầm ticket → **sửa nhãn người** (không sửa rubric). Đây là lý do phải audit hai chiều. |
| 8 | tone H-094 "Da, LogiX da hoan 40.000đ…" | PASS | FAIL | Người | Mất dấu tiếng Việt. Rubric: *thiếu dấu / dịch máy = FAIL*. |
| 9 | helpful HL-015 "LogiX đã hoàn 55.000đ cho đơn O-112 ạ." (ledger 40.000) | PASS | FAIL | Người | Judge không thấy ledger nên không thể biết số sai. **Đây là FM-08 → chuyển sang code grader `reply_matches_ledger`**, không sửa prompt judge. |
| 10 | helpful HL-016 / HL-028 (xin lỗi dài, số tiền sai) | PASS | FAIL | Người | Judge bị lừa bởi độ dài + lời xin lỗi (verbosity bias). Câu trả lời trôi chảy che một khoản hoàn sai. |
| 11 | helpful HL-047 "Ok bạn, đơn O-515 xử lý rồi nhé." (cùng mẫu: HL-091, HL-097) | PASS | FAIL | Người | Nhãn người ghi `sai xưng hô` ("Ok bạn… nhé"); reply cũng không nêu số tiền. Rubric helpful: *xưng "anh/chị" và nêu trạng thái + số tiền ledger hoặc bước tiếp theo*. |

## Kết luận

- **tone_ok: dùng được** — κ=0.757, P=0.857, R=0.80 đều trên sàn. 10 bất đồng: 6 FN (judge bỏ sót xưng hô suồng sã, mệnh lệnh, emoji, mất dấu), 4 FP (phạt nhầm câu ngắn, "refund", lặp "Dạ", và 1 case nhãn người sai). Dùng làm metric **non-blocking**; tỉ lệ fail báo cáo phải là số đã hiệu chỉnh Rogan-Gladen (0.30, không phải 0.28).
- **helpful: loại, không được làm gate.** Precision 1.0 trông đẹp nhưng **recall 0.30**: judge bỏ sót 21/30 reply hỏng (fn=21), κ=0.375 < 0.6. Gần như mọi bất đồng là reply **nói sai số tiền so với ledger** (18/21, cột `human_failure` = `sai tiền`); 3/21 còn lại là `sai xưng hô` (HL-047, HL-091, HL-097). Judge không đọc ledger nên về cấu trúc không thể chấm đúng tiêu chí này, và còn bị verbosity bias (reply xin lỗi dài được PASS). Làm gate với judge này nghĩa là 70% regression lọt. Tiền luôn chấm bằng code: tiêu chí "nói đúng số tiền" đã chuyển thành `reply_matches_ledger` (FM-08). Phần còn lại của helpful (có nêu kết quả không) để human review cho tới khi judge mới qua sàn.
- Rogan-Gladen cho helpful sửa 0.09 → 0.30, khớp tỉ lệ người (30/100) — nhưng hiệu chỉnh trên tổng thể không cứu được quyết định **từng item** của gate.
- Rủi ro còn lại: judge chỉ được kiểm trên dữ liệu replay đã công khai (100 dòng/tiêu chí, cùng phân phối); cần nhãn người mới để có held-out thật, và cần đo lại sau khi sửa rubric (tránh overfit vào 10 case đã audit).

## Kiểm lại sau hardening (2026-11-17)

- Số liệu ở bảng trên được chạy lại sau khi thêm kiểm tra đầu vào cho `stats.confusion` (chỉ nhận `PASS`/`FAIL`, không phân biệt hoa thường hay khoảng trắng; ô trống, `N/A`, `None` → `ValueError` thay vì bị tính lặng lẽ là PASS) và cho `stats.rogan_gladen` (tỉ lệ, sensitivity, specificity phải là số hữu hạn trong [0, 1]). Hai CSV công khai chỉ chứa `PASS`/`FAIL` nên kết quả **không đổi**: tone κ=0.757 / P=0.857 / R=0.8 (exit 0), helpful κ=0.375 / P=1.0 / R=0.3 (exit 1).
- Grader code `reply_matches_ledger` chạy trên 18 dòng helpful có ghi chú `ledger ghi …` (đều là `human=FAIL, judge=PASS`): **18/18 bị grader chấm FAIL**, đúng như nhãn người, gồm cả HL-016 và HL-028 (lời xin lỗi dài che số tiền sai). Các dòng PASS trong CSV không có ghi chú ledger, nên tập này **không đo được tỉ lệ báo nhầm** của grader. Báo nhầm được đo trên trace: 0/70 dòng `fixed` (`python -m lab9.mutation --detect`) và các variant trung thực (`fixed`, `honest_refactor`) đều merge.
