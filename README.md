<div align="center">

# 🏡 CasaOS Backup Manager Pro

<p align="center">
  <b>Hệ thống Sao lưu, Phục hồi độc lập & Đồng bộ đám mây chuyên nghiệp dành riêng cho CasaOS & Docker Homelab</b>
</p>

</div>

---

## 🚀 Giới thiệu
**CasaOS Backup Manager Pro** là giải pháp quản lý sao lưu toàn diện chạy trực tiếp trên hệ thống CasaOS. Được thiết kế tối ưu hóa riêng cho trải nghiệm di động trên Safari iOS với giao diện **Ultra Glassmorphism & Bento Dashboard** siêu mượt (0ms lag), giúp bảo vệ toàn bộ dữ liệu container, cấu hình mạng và biến môi trường an toàn tuyệt đối.

---

## ✨ Tính năng nổi bật

- 📊 **Bento Telemetry Dashboard**: Hiển thị trực quan trạng thái Container, số lượng bản sao lưu và mức sử dụng ổ cứng SSD hệ thống (AppData + Backups).
- 📱 **Seamless Safari iOS Experience**: Giao diện tràn viền liền mạch phía trên (Status Bar) và dưới thanh điều hướng, không bị khựng hình hay mất Header khi cuộn.
- 🗂️ **Smart App Avatars & Search**: Tự động nhận diện biểu tượng vector sắc nét cho từng dịch vụ (Home Assistant, Cloudflare, Z-Wave JS, Scrypted, Uptime Kuma, Syncthing, Portainer...) kèm ô tìm kiếm và bộ lọc nhanh.
- 🔄 **Multi-App Batch Backup**: Hỗ trợ chọn nhiều ứng dụng hoặc sao lưu toàn bộ hệ thống chỉ với 1 cú chạm.
- 🕒 **Dedicated Home Assistant Schedule**: Lịch trình sao lưu độc lập đi kèm tính năng tự động dọn dẹp các tệp rác (`.sync-conflict`, log cũ) trước khi nén gói.
- ☁️ **Cloud Sync & 1-Tap Recovery Key**: Đồng bộ tự động qua Rclone (Google Drive, OneDrive...), mã hóa đầu끝 AES-256 và khôi phục toàn bộ kết nối chỉ bằng một chuỗi khóa duy nhất (`CBMKEY:...`).
- 🛑 **Cancellable Background Tasks**: Mọi tiến trình sao lưu, phục hồi hay đồng bộ đám mây đều có thể bấm **Hủy (Cancel)** ngay lập tức từ giao diện Web.

---

## 🛠️ Cấu trúc dự án

```text
casaos-backup-manager/
├── app/
│   ├── main.py            # Backend FastAPI (Docker API, Rclone wrapper, Caching)
│   └── templates/
│       └── index.html     # Frontend Pro UI (TailwindCSS, Alpine.js)
├── Dockerfile             # Cấu hình container đóng gói độc lập
├── docker-compose.yml     # Khởi chạy dịch vụ gắn kết Docker Socket
└── README.md              # Tài liệu hướng dẫn dự án
