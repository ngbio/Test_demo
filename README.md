# Flash Sale backend demo

FastAPI + Redis + SQLite, không frontend, không queue. Mục tiêu: minh họa purchase token dùng một lần, giới hạn 3 request/5 giây, mỗi user mua tối đa một đơn cho cùng event/product, và không oversell khi 100 user tranh 10 sản phẩm.

## 1. Chạy trên Windows (PowerShell)

Cần Python 3.10+ và Docker Desktop đã mở, dùng Linux containers. Chạy mọi lệnh từ thư mục project. Redis chỉ bind localhost; dùng riêng namespace `flashsale:*` cho demo này.

```powershell
cd E:\Test_cty_digital\Test_demo
docker compose up -d --wait
docker compose exec redis redis-cli ping
# Mong đợi: PONG

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Không cần activate venv hay thay ExecutionPolicy. Giữ terminal FastAPI mở. Swagger: <http://127.0.0.1:8000/docs>.

Mở terminal PowerShell thứ hai:

```powershell
cd E:\Test_cty_digital\Test_demo
Invoke-RestMethod -Method Post http://127.0.0.1:8000/demo/reset
.\.venv\Scripts\python.exe test_demo.py
Invoke-RestMethod http://127.0.0.1:8000/flash-sale/status | ConvertTo-Json
.\.venv\Scripts\python.exe inspect_db.py
```

Script tự reset trước từng testcase, chạy đủ 4 test và **giữ lại dữ liệu test concurrent cuối cùng** để chụp màn hình. Assertion thất bại sẽ làm script exit khác 0. Không reset sau khi test nếu muốn chụp 10 orders.

Kết quả mong đợi khi chạy thành công (đây là mẫu, không phải log thực thi):

```text
=== CONCURRENT STOCK TEST ===
Initial stock: 10
Total requests: 100
Success: 10
Failed/Sold out: 90
Final Redis stock: 0
Final Database stock: 0
Orders created: 10
Overselling: NO

ALL 4 DEMO TESTS PASSED
```

Lưu log để đưa vào báo cáo:

```powershell
New-Item -ItemType Directory -Force artifacts
.\.venv\Scripts\python.exe test_demo.py 2>&1 | Tee-Object artifacts\demo-results.txt
.\.venv\Scripts\python.exe inspect_db.py 2>&1 | Tee-Object artifacts\database-results.txt
```

Có thể ghi log FastAPI bằng cách thay lệnh start bằng:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 2>&1 | Tee-Object artifacts\backend.log
```

## 2. Postman hoặc PowerShell

Import `postman_collection.json` vào Postman. Collection có biến `baseUrl`, `userId`, `purchaseToken`; request lấy token tự lưu token vào collection. Chạy thủ công theo thứ tự reset → token → purchase → purchase lại → status.

| Method | Endpoint | Body / ý nghĩa |
|---|---|---|
| POST | `/demo/reset` | Không body; reset stock=10, xóa orders/users/token/guard/rate limit |
| POST | `/flash-sale/token` | `{"userId":12,"eventId":1,"productId":1}` |
| POST | `/flash-sale/purchase` | Như trên, thêm `"purchaseToken":"..."` |
| GET | `/flash-sale/status` | Tồn kho và số đơn của event 1/product 1 |

Token tồn tại trên server, là chuỗi ngẫu nhiên 256 bit (nonce), không phải JWT. Redis gắn token với user/event/product và thời điểm hết hạn 60 giây; client không thể sửa các metadata này.

Ví dụ PowerShell thực tế:

```powershell
$baseUrl = 'http://127.0.0.1:8000'
Invoke-RestMethod -Method Post "$baseUrl/demo/reset"
$body = @{ userId = 12; eventId = 1; productId = 1 }
$token = Invoke-RestMethod -Method Post "$baseUrl/flash-sale/token" -ContentType 'application/json' -Body ($body | ConvertTo-Json)
$body.purchaseToken = $token.purchaseToken
Invoke-RestMethod -Method Post "$baseUrl/flash-sale/purchase" -ContentType 'application/json' -Body ($body | ConvertTo-Json)
# Gửi lại: HTTP 409, code TOKEN_ALREADY_USED (PowerShell báo lỗi HTTP là đúng).
Invoke-RestMethod -Method Post "$baseUrl/flash-sale/purchase" -ContentType 'application/json' -Body ($body | ConvertTo-Json)
Invoke-RestMethod "$baseUrl/flash-sale/status"
```

Mỗi user có tối đa 3 request purchase được xét trong cửa sổ trượt 5 giây. Rate limit chạy trước kiểm tra token nên lần 4 sẽ trả 429 ngay cả khi token đã dùng. HTTP 429 có header `Retry-After`. Request không qua validation của FastAPI (422) không vào bộ đếm này.

| HTTP | code | Diễn giải |
|---|---|---|
| 200 | `SUCCESS` | Database đã commit đơn hàng |
| 400 | `TOKEN_NOT_FOUND` | Token không tồn tại/đã bị dọn khỏi Redis |
| 403 | `TOKEN_MISMATCH` | Token không thuộc user/event/product được gửi |
| 410 | `TOKEN_EXPIRED` | Token chưa dùng, đã quá 60 giây |
| 409 | `TOKEN_ALREADY_USED` | Token đã được dùng để giữ chỗ |
| 409 | `ALREADY_PROCESSING` | User đang có reservation |
| 409 | `ALREADY_PURCHASED` | User đã mua sản phẩm trong event |
| 409 | `SOLD_OUT` | Không còn tồn kho |
| 429 | `RATE_LIMIT_EXCEEDED` | Vượt 3 request/5 giây |
| 404 | `SALE_NOT_FOUND` | Demo chỉ hỗ trợ eventId=1/productId=1 |
| 503 | `DEMO_NOT_INITIALIZED` | Cần gọi reset; không tự seed khi thiếu Redis stock |
| 503 | `REDIS_UNAVAILABLE` / `DATABASE_ERROR` | Redis không truy cập được / giao dịch DB thất bại |
| 409/503 | `DEMO_BUSY` / `RESET_IN_PROGRESS` | Reset và request đang chạy được loại trừ lẫn nhau |
| 403 | `DEMO_DISABLED` | Endpoint reset bị tắt bởi `DEMO_MODE=false` |

Token đã dùng vẫn được nhận diện là USED trong thời gian giữ bản ghi một giờ; token chưa dùng quá 60 giây trả EXPIRED. Sau một giờ bản ghi bị dọn thì trả NOT_FOUND. Request bị SOLD_OUT/ALREADY_PURCHASED không tiêu thụ token; token chỉ USED khi reservation thành công. Rollback không mở lại token, phải xin token mới.

## 3. Kiến trúc và concurrency

```text
Client → FastAPI → Redis (Lua atomic reservation) → SQLite transaction
                    ↑                                |
                    └── mark PURCHASED / compensate ─┘
```

1. Redis sliding-window rate limit theo user, sử dụng thời gian server Redis.
2. Lua kiểm tra token, binding, hạn dùng, user guard và stock. Nếu hợp lệ, `DECR stock`, ghi guard bằng nonce và đánh dấu token USED trong cùng một script.
3. SQLite thực hiện `BEGIN IMMEDIATE`, kiểm tra mua trùng, `UPDATE products SET stock=stock-1 WHERE id=? AND stock>0`, tạo user/order và commit. `CHECK(stock>=0)`, unique `(user_id,event_id,product_id)` và unique `request_id` là lớp bảo vệ cuối cùng.
4. Sau commit, Redis chuyển guard sang PURCHASED. Nếu DB transaction thất bại, rollback DB rồi Lua hoàn Redis stock và giải phóng guard. Lua chỉ bù tồn kho khi nonce sở hữu reservation khớp, nên gọi bù hai lần không tăng stock hai lần. Nếu DB phát hiện đơn trùng, hoàn reservation dư và khôi phục guard PURCHASED.

SQLite dùng một connection cho mỗi transaction, timeout 30 giây và WAL; tác vụ DB chạy trong thread pool nên event loop vẫn nhận các request khác. Lua mới là cơ chế atomic reservation, không có Python lock tuần tự hóa tất cả purchase. Redis đảm bảo script chạy atomic ([Redis documentation](https://redis.io/docs/latest/develop/programmability/eval-intro/)); SQLite mô tả transaction `BEGIN IMMEDIATE` tại [SQLite documentation](https://www.sqlite.org/lang_transaction.html).

Các file chính:

```text
app/main.py              API, orchestration, demo reset guard
app/database.py          schema, transaction, atomic stock update
app/lua/*.lua            token, rate limit, reserve, finalize/rollback
test_demo.py             4 test qua HTTP, gồm 100 request concurrent
tests/test_safety.py     regression tests và lỗi DB
inspect_db.py            đọc trực tiếp SQLite để chụp bằng chứng
postman_collection.json  request sẵn dùng
compose.yaml            Redis local
```

## 4. Kiểm thử bổ sung

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Regression tests dùng SQLite thật và **fakeredis + Lua**, không cần Docker. Chúng kiểm tra concurrency, replay, rate limit, token sai user/hết hạn, mua trùng, reset khi đang xử lý, rollback sau khi trừ DB stock, bù tồn kho idempotent và unique constraint. Đây không thay thế bài test end-to-end với Redis thật bằng `test_demo.py`.

## 5. Những màn hình nên chụp

1. Terminal FastAPI: các dòng `[REQUEST]`, `[TOKEN_CREATED]`, `[TOKEN_USED]`, `[RESERVED]`, `[ORDER_CREATED]`, `[SOLD_OUT]`, `[RATE_LIMITED]`.
2. `=== RATE LIMIT TEST ===`: request 4–10 trả `429 RATE_LIMIT_EXCEEDED`.
3. `=== TOKEN REPLAY TEST ===`: SUCCESS rồi TOKEN_ALREADY_USED.
4. `=== ONE USER ONE PRODUCT TEST ===`: token đầu SUCCESS, token tiếp theo ALREADY_PURCHASED.
5. `=== CONCURRENT STOCK TEST ===`: 100 request, đúng 10 success, 90 sold out, stock=0, Overselling: NO.
6. Postman GET `/flash-sale/status`: `{"redisStock":0,"databaseStock":0,"orderCount":10}`.
7. Terminal `inspect_db.py`: hiện đủ 10 orders, stock=0 và DATABASE CHECK: PASS. Có thể mở `data/demo.db` bằng DB Browser for SQLite và chạy:

```sql
SELECT * FROM products;
SELECT * FROM orders ORDER BY id;
SELECT COUNT(*) AS total_orders FROM orders;
SELECT user_id, event_id, product_id, COUNT(*) AS total
FROM orders GROUP BY user_id, event_id, product_id HAVING COUNT(*) > 1;
-- Query cuối phải trả 0 dòng.
```

`[ROLLBACK]` chỉ xuất hiện khi transaction thất bại, không có trong happy path. Test regression tạo SQLite trigger báo lỗi sau bước decrement để kiểm chứng rollback thực sự.

## 6. Phạm vi demo và xử lý sự cố

- Chỉ chạy **một FastAPI process với `--workers 1`** và một demo trên namespace này. Reset guard là in-process, không bảo vệ nhiều process. Purchase vẫn concurrent trong một worker. Không dùng reload trong lúc load test.
- Chỉ một event và một product. User ID dương tự tạo từ request, không phải authentication. Các cơ chế này minh họa giảm spam/replay, **không chứng minh client là người thật**; bot đổi userId vẫn có thể gửi request. Endpoint token chưa có anti-abuse riêng.
- Redis và SQLite không chung transaction. Process chết sau reservation có thể để lại guard và giảm tồn Redis; Redis mất kết nối sau DB commit có thể để lại trạng thái PROCESSING. Demo không có tự phục hồi sau crash. DB vẫn chống oversell/mua trùng, nhưng không đảm bảo Redis và DB luôn bằng nhau khi hỏng giữa chừng. Log `RECONCILIATION_REQUIRED` cần kiểm tra; reset để bắt đầu lượt demo mới (reset xóa orders).
- Nếu DB đã commit mà bước Redis cuối lỗi, API vẫn trả SUCCESS và không hoàn stock sai. Nếu mất phản hồi HTTP thì client chưa chắc biết kết quả; kiểm tra DB/status. Chưa có API tra cứu trạng thái riêng theo nonce.
- `/status` đọc hai hệ thống riêng biệt nên trong lúc purchase đang chạy có thể thấy stock tạm thời lệch nhau. Chụp sau khi script hoàn tất.
- Khởi động app không tự reset dữ liệu. Sau lần đầu hoặc Redis bị xóa stock, hãy POST `/demo/reset`. Reset không được thực hiện cùng purchase; nếu reset hỏng, stock key bị vô hiệu hóa và có thể gọi reset lại.
- Đổi cấu hình trong PowerShell trước khi start app: `$env:REDIS_URL='redis://127.0.0.1:6379/0'`, `$env:DATABASE_PATH='E:\Test_cty_digital\Test_demo\data\demo.db'`. Không đặt các ứng dụng khác trong namespace `flashsale:*` vì reset sẽ xóa namespace này.
- Docker báo không tìm thấy pipe/daemon: mở Docker Desktop, đợi engine chạy, rồi chạy lại `docker compose up -d --wait`. Cổng 6379 bận: dừng Redis khác hoặc đổi host port trong Compose và REDIS_URL tương ứng.
- Dừng FastAPI bằng Ctrl+C; dừng container bằng `docker compose down` (giữ volume Redis).
