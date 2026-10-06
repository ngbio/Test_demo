# Kết quả kiểm chứng

Đã chạy trên Windows, Python 3.10, ngày 06/10/2026 (Asia/Saigon).

## Regression tests

`python -m pytest -q`: **8 passed** (SQLite thật + fakeredis/Lua).

Bao gồm concurrency 100 user, rate limit, replay, binding/hết hạn token, mua trùng, reset guard, lỗi transaction sau DB decrement, compensation idempotent và SQLite unique constraint. SQLite cũng được test riêng với 100 thao tác concurrent không đi qua Redis: chỉ 10 đơn thành công.

## End-to-end với Redis thật

Docker Compose chạy Redis 7.4; FastAPI chạy một worker tại `127.0.0.1:8000`. Đã chạy `test_demo.py` qua HTTP, không mock Redis hoặc database:

| Bài test | Kết quả thực tế |
|---|---|
| Rate limit | Request 1 SUCCESS; 2–3 TOKEN_ALREADY_USED; 4–10 HTTP 429 |
| Token replay | SUCCESS → TOKEN_ALREADY_USED |
| One user, one product | SUCCESS → ALREADY_PURCHASED → ALREADY_PURCHASED |
| 100 user concurrent | 10 SUCCESS, 90 SOLD_OUT |
| Stock sau test | Redis=0, SQLite=0 |
| Orders | 10 |

Log được lưu tại `artifacts/demo-results.txt`, log backend tại `artifacts/server.stderr.log` và `artifacts/server.stdout.log`. Database cuối cùng là `data/demo.db`. `inspect_db.py` đọc trực tiếp file này để xác nhận 10 orders và stock=0; kết quả lưu ở `artifacts/database-results.txt`.

App đã được khởi động nền để chạy kiểm thử. Nếu vẫn đang chạy, có thể mở <http://127.0.0.1:8000/docs> hoặc <http://127.0.0.1:8000/flash-sale/status>. PID đã lưu trong `artifacts/server.pid`. Muốn chuyển sang terminal hiện log để chụp, xác nhận PID thuộc tiến trình demo rồi dừng:

```powershell
$demoPid = [int](Get-Content artifacts\server.pid)
Get-CimInstance Win32_Process -Filter "ProcessId = $demoPid" | Select-Object ProcessId, CommandLine
# Chỉ dừng nếu CommandLine là uvicorn app.main:app của demo này:
Stop-Process -Id $demoPid
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Việc restart API không xóa dữ liệu. Không gọi reset nếu muốn giữ nguyên 10 orders hiện có để chụp.
