# Reflection (≤1 trang)

**Kết quả:** practice 50.0 (TP 33, FP 0, FN 0) · public 46.9 (TPR 0.974,
FPR 0, 240/220 credits, cả 4 trụ cột đạt `high`) · **private 32.92 (TPR 0.667,
FPR 0.0137, không vượt ngân sách)**. Điểm tụt từ public xuống private là do tầng
lỗi tinh vi (subtle): khoảng 1/3 số lỗi ở private nằm trong vùng mà ngưỡng của
tôi coi là bình thường, và có 2 sự kiện sạch vượt ngưỡng.

**Loại lỗi nào khó phát hiện nhất, và tại sao?**

- **Lỗi tinh vi của `data_batch` (checks).** Volume spike, null spike và
  freshness lag ở mức "obvious" nằm rất xa ngoài dải 3σ của baseline. Nhưng các
  lỗi tinh vi có thể nằm trong dải đó ở từng chiều riêng lẻ. Tôi đổi mỗi chỉ số
  thành z-score. Với chỉ số hai phía (số hàng, giá trị trung bình), tâm và σ lấy
  từ biên của baseline. Với chỉ số một phía (null rate, staleness), tâm là trung
  vị trượt của các sự kiện sạch trước đó. Sau đó tôi báo động khi một z đơn lẻ
  ≥ 2.6, hoặc khi chuẩn Euclid của các độ lệch dương ≥ 3.4. Đây là phần cần thống
  kê thực sự, không thể chỉ so `value > max`.
- **Embedding drift sát biên.** Centroid shift của dữ liệu sạch lên tới ~0.039,
  biên công bố là 0.0435, và drift nhẹ nằm ngay trên dải sạch. Khoảng cách giữa
  hai lớp chỉ vài phần nghìn, nên ngưỡng tĩnh gần như không có biên an toàn.
- **Feature skew.** Ở đây baseline công bố bị *sai theo hướng nguy hiểm*: các
  batch sạch vượt `feature_mean_shift_sigma_max` (0.41). Nếu dùng nó làm ngưỡng
  cứng sẽ sinh báo động giả, nên tôi đặt ngưỡng ở giữa giá trị lớn nhất của dữ
  liệu sạch và giá trị nhỏ nhất của lỗi (ngưỡng 0.6).
- **Lineage `missing_upstream`.** Lỗi này không cần thống kê, nhưng có bẫy. Trường
  `inputs` trong payload chỉ ghi `raw.orders`, kể cả với các run sạch, nên so
  `actual_upstream` với payload là vô ích. Mốc tham chiếu phải là tập phụ thuộc
  thực của model (`raw.orders` + `raw.customers`), hợp với những gì quan sát được
  ở các run sạch.

Tôi không chỉnh ngưỡng để đuổi theo lỗi bị sót ở public, và không chạy lại private
sau khi thấy điểm. Khớp theo phản hồi đó chính là kiểu overfit mà RULES.md cảnh báo.

**Bạn sẽ thay đổi gì trong cân bằng chi phí/độ phủ nếu được làm lại?**

Tôi vẫn giữ quyết định gọi đúng một tool cho mọi sự kiện, kể cả khi vượt ngân
sách. Bỏ sót một lỗi mất ~0.5/n_faulty điểm (≈0.9 điểm ở private), còn mỗi credit
vượt ngân sách chỉ mất 0.2/budget (≈0.09 điểm ở public). Bỏ qua một sự kiện
2 credit để tiết kiệm 0.18 điểm nhưng mất ~0.25 điểm TPR kỳ vọng là một đánh đổi
tồi. Ở public, quét toàn bộ tốn 1.8 điểm overage, và không chính sách bỏ qua nào
gỡ lại được số đó mà không mất nhiều hơn ở TPR.

Điều tôi sẽ thay đổi là *ngưỡng*, không phải độ phủ. Cũng do sự bất đối xứng đó,
một báo động giả (≈0.2 điểm) rẻ hơn bỏ sót một lỗi khoảng 4 lần. Ngưỡng 2.6σ cho
từng chỉ số vẫn còn thận trọng. Tôi sẽ đặt ngưỡng theo điểm kỳ vọng tối ưu (báo
động khi P(lỗi) > ~0.2), thay vì quan điểm "FPR = 0 bằng mọi giá" mà tài liệu
khóa học gợi ý.

**Với thêm thời gian, bạn sẽ cải tiến cơ chế thống kê nào trong `ctx.state`?**

Hiện `ctx.state` lưu lịch sử sạch cho từng chỉ số, nhưng chỉ dùng để tính tâm
(trung vị trượt). Độ phân tán vẫn lấy từ biên 3σ công bố. Tôi sẽ:
1. Ước lượng cả độ phân tán theo thời gian thực, bằng MAD của các sự kiện được
   đánh giá là sạch. Hai báo động giả ở private cho thấy σ công bố không phải σ
   thực của luồng dữ liệu ở một số chỉ số.
2. Theo dõi hiệp phương sai giữa các chỉ số của `data_batch`, để phép đo tổng hợp
   thành khoảng cách Mahalanobis thay vì cộng các z độc lập. Khi đó các dao động
   bình thường có tương quan (nhiều hàng *và* staleness cao) sẽ không làm phồng
   chuẩn, và có thể hạ ngưỡng mà không thêm báo động giả.
3. Duy trì bộ tích lũy drift ngắn hạn theo từng luồng (CUSUM trên centroid shift
   của embedding và độ lệch của feature). Một lỗi tinh vi nằm sát biên ở một batch
   sẽ lộ rõ khi so với xu hướng gần đây.
