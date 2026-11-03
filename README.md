# Kệ Truyện Local

Ứng dụng đọc truyện cho một người trên máy Windows, chạy bằng Python và trình duyệt. Không cần đăng nhập hoặc mua premium trong ứng dụng này. Đây là dự án độc lập, chưa triển khai đầy đủ mọi chức năng của vBook.

## Chạy ứng dụng

Cần Python 3.10 trở lên và Chrome hoặc Edge. Phần Python dùng thư viện chuẩn, không cần cài thêm gói.

Nhấp đúp `Start.cmd`, hoặc chạy trong thư mục dự án:

```powershell
python -X utf8 run.py
```

Mở `http://127.0.0.1:8765` nếu trình duyệt không tự mở. Giữ cửa sổ máy chủ mở khi đọc; nhấn `Ctrl+C` để dừng. Có thể đổi cổng bằng `--port 8768` hoặc tắt mở trình duyệt tự động bằng `--no-browser`.

Bản clone mới có thư viện trống. Dùng **Nhập file**, **Nhập thư viện**, hoặc **Khám phá** để thêm truyện.

## Chức năng hiện có

- Kệ sách có tìm kiếm, bộ lọc và nhóm; lưu chương, vị trí và phần trăm đã đọc.
- QQ, Full và Hako: tìm truyện, xem chi tiết, thêm vào kệ, mục lục và tải chương. Khả năng truy cập phụ thuộc trang nguồn và kết nối mạng.
- Nhập thư viện từ thư mục hoặc ZIP xuất của vBook, giữ dữ liệu tiến độ có trong bản xuất. Chỉ khôi phục được thông tin và tệp thực sự có trong bản xuất; không có đồng bộ với tài khoản vBook.
- Nhập tệp EPUB, PDF, TXT và các định dạng khác được liệt kê trong **Thiết lập**. PDF có OCR Windows để tìm kiếm/nghe và nút mở bản gốc; chất lượng phụ thuộc gói ngôn ngữ OCR đã cài.
- Chỉnh font, cỡ chữ, giãn dòng/đoạn, độ rộng, căn chữ, màu nền; cuộn dọc hoặc lật ngang. Có mục lục, tìm trong chương/toàn truyện, ghi chú, dấu trang và sửa nội dung.
- Tải chương để đọc offline. Chương và ảnh đã tải thành công được giữ trên máy; danh sách tác vụ tải hiện chưa được giữ khi khởi động lại.
- TTS qua giọng của trình duyệt, gồm offline/online khi máy có cung cấp; tốc độ, cao độ và hẹn giờ. Không kèm model giọng hoặc bảo đảm có sẵn giọng tiếng Việt.
- QT/VietPhrase với từ điển nhập riêng, sửa nghĩa chung/riêng từng truyện và chế độ nguyên văn/bản dịch/song ngữ. Không kèm dữ liệu từ điển; gói luật ngữ pháp gốc chưa tương thích đầy đủ.
- Thùng rác có khôi phục, xóa vĩnh viễn theo xác nhận và tự xóa sau 30 ngày khi ứng dụng đang chạy hoặc mở lại.
- **Lối tắt Windows:** bấm vào truyện trên kệ → **Thêm lối tắt** để tạo shortcut trên Desktop, dùng ảnh bìa làm icon khi có bìa hợp lệ. Nhấp shortcut để tự khởi động app hoặc dùng máy chủ đã chạy, rồi mở thẳng vị trí đọc đã lưu. Toàn bộ công cụ đọc và nút về kệ vẫn sử dụng bình thường. Không có bìa thì dùng icon mặc định. Xóa shortcut không xóa truyện; truyện đã xóa hoặc nằm trong thùng rác sẽ được báo khi mở shortcut. Nếu di chuyển thư mục app/thư viện, cần tạo lại shortcut.

## Dữ liệu cá nhân và sao lưu

Dữ liệu được tạo trong `user-data/`. Máy chủ chỉ lắng nghe tại địa chỉ loopback của máy đang chạy. Đọc tệp đã nhập và chương đã tải không cần Internet; tải nội dung mới và giọng TTS online cần mạng.

**Dừng máy chủ rồi sao chép toàn bộ `user-data/` để sao lưu**, gồm thư viện, tiến độ, tệp nhập và từ điển. Khôi phục bằng cách chép lại thư mục khi máy chủ đang dừng. Có thể chọn thư mục dữ liệu khác bằng `--data-dir`.

Nhập bản xuất vBook hoặc gói từ điển bằng dòng lệnh:

```powershell
python -X utf8 run.py --import-backup "C:\duong-dan\ban-xuat"
python -X utf8 run.py --import-qt "C:\duong-dan\tu-dien.zip"
```

Repo chỉ chứa mã nguồn, hướng dẫn và kiểm thử cùng mẫu trang công khai cần cho parser. `.gitignore` dùng danh sách cho phép để giữ dữ liệu cá nhân, bản xuất, log, ảnh chụp, tài liệu phân tích và file app gốc ngoài Git. **Clone repo không khôi phục thư viện cá nhân.**

## Kiểm tra

```powershell
python -X utf8 -m unittest discover -s tests -v
node --check web/app.js
node --check web/reader.js
```

Node.js chỉ cần khi kiểm tra cú pháp JavaScript. Kiểm thử dùng dữ liệu tạm, không sửa thư viện thật; mẫu QQ/Full/Hako không cần tải mạng. Kiểm thử tích hợp OCR/TTS phụ thuộc Windows và các thành phần ngôn ngữ/giọng đã cài.

## Giới hạn

Parser nguồn đã được kiểm tra bằng các mẫu HTML, chưa nghiệm thu đầy đủ mọi luồng truy cập trực tiếp; thay đổi phía trang nguồn có thể làm gián đoạn tải truyện. OCR PDF hiện chạy đồng bộ, tối đa 500 trang. TTS trong giao diện dùng trình duyệt; xuất audio gốc chưa sẵn sàng. Chưa có đầy đủ định dạng tệp của vBook, xuất sách, quản lý model giọng, đồng bộ tài khoản chính thống, AI hoặc community.
