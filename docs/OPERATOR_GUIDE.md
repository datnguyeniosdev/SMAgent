# Hướng dẫn vận hành (Operator guide)

Tài liệu cho người vận hành Agent Gateway hằng ngày qua Telegram. Cài đặt và demo: xem [README](../README.md).

## 1. Operator là ai, tin nhắn đi đâu

**Operator** là người dùng có user id nằm trong `TELEGRAM_ALLOWED_USER_IDS`. Bot chỉ nhận lệnh và bấm nút của operator **trong control group** (`TELEGRAM_CONTROL_CHAT_ID`). Riêng `/keys` và việc gửi passcode thì làm trong **chat riêng với bot** (mở chat với bot một lần bằng `/start`).

| Nơi nhận | Biến cấu hình | Nội dung |
|---|---|---|
| Control group | `TELEGRAM_CONTROL_CHAT_ID` | Lệnh, câu hỏi của agent (🙋), bản sao yêu cầu giữa các agent (🤝, 💡), agent đang chờ (🆘), việc bị kẹt (⚠️), session chính offline (👑), kết quả `/send`, deploy |
| Report channel | `TELEGRAM_REPORT_CHANNEL_ID` | Báo cáo của agent (📋 `submit_report`) kèm ảnh chụp màn hình |
| Discussion group | `TELEGRAM_DISCUSSION_CHAT_ID` (hoặc `telegram.discussion_chat_id` trong `config/app.yaml`) | Nơi **reply** được xử lý: reply vào báo cáo, câu hỏi, yêu cầu của agent. Có thể chính là control group |
| Notice channel | `TELEGRAM_NOTICE_CHAT_ID` | Thông báo hệ thống, gửi **im lặng** (không rung): 🔌 session kết nối, ⚪ session rời, 📬 yêu cầu đã tới session, 🧭 điều hướng việc, 👑 đổi session chính, 🙋 việc được mời các session rảnh, 📚 skill được nhập, 🧹 lane đóng. Không đặt biến này thì các thông báo này vào control group (vẫn im lặng) |

Dòng đầu mỗi tin có dạng `PREFIX · #project · #task_id`; bấm hashtag để tìm mọi tin liên quan. Một số prefix: ✅ HOÀN THÀNH, ❌ THẤT BẠI, 📋 BÁO CÁO, 💬 PHẢN HỒI, 🙋 CÂU HỎI, 🔐 CẦN DUYỆT, 🚀 TRIỂN KHAI, 🆘 AGENT ĐANG CHỜ, 🤝 AGENT NHỜ, 💡 AGENT TRẢ LỜI, ⚠️ CẢNH BÁO.

## 2. Khái niệm cần biết

| Ký hiệu | Ý nghĩa |
|---|---|
| `s#` | Một session agent đang mở (s1, s2…), xem bằng `/list` |
| `p#` | Một project (p1, p2…) |
| `m#` | Một yêu cầu trong hàng chờ (m1, m2…), xem bằng `/queue` |
| 👑 main | Session chính của project: nhận yêu cầu gửi theo project và điều hướng (`dispatch`) sang session con |
| 🧭 lane | Session con theo luồng việc (dev, deploy, hoặc chủ đề), chạy trong bản sao riêng của project |
| ⚡ push / 🔄 pull | Session nhận việc được đẩy thẳng vào, hoặc tự lấy việc bằng `get_messages` |

## 3. Dùng hằng ngày

**Gửi yêu cầu cho một project hoặc session**

- `/send p1 sửa menu tạm dừng`: gửi theo project. Project ở chế độ `main` thì đi tới 👑 main để điều hướng; ở chế độ `central` thì gateway tự điều hướng.
- `/send s3 chạy lại test`: gửi thẳng vào session s3, việc giữ nguyên ở session đó theo thứ tự.
- Có thể dùng tên project thay cho `p#`. Bot trả về `m#` và cho biết đã đẩy vào session nào, hoặc đang xếp hàng (kèm lý do) và sẽ giao khi có session nhận được.
- Gửi tới `s#` của session đã mất kết nối thì yêu cầu vào hàng chờ của project đó.

**Đọc báo cáo.** Agent gửi báo cáo bằng `submit_report`; báo cáo vào report channel, gồm tiêu đề, nội dung trích dẫn, trạng thái agent tự khai (`agent: …`) và trạng thái gateway tự kiểm (`gateway: …`, chỉ hiện khi gateway có kiểm). Ảnh chụp màn hình (tối đa `max_report_photos`, mặc định 4) được gửi thành tin reply vào báo cáo; ảnh quá 10 MB gửi dạng file.

**Tiếp tục cùng một session.** **Reply** vào báo cáo (chữ, hoặc ảnh kèm caption) trong discussion group: góp ý được đẩy vào đúng session đã gửi báo cáo; session đó không còn thì đi tới project (main điều hướng). Ảnh trong reply được gửi kèm cho agent dưới dạng đường dẫn screenshot. Bot xác nhận bằng 💬 PHẢN HỒI ("Pushed into the live Claude Code session" hoặc "Queued…").

Cách khác: `/feedback <tsk_…|rep_…> <nội dung>` (ảnh tuỳ chọn, đặt lệnh này làm caption).

## 4. Bảng lệnh

Menu "/" của bot chỉ hiện các lệnh hằng ngày; `/help` liệt kê tất cả. `/menu` (hoặc `/start`) mở bảng nút: Sessions, Agents, Queue, Open project, Keys, Providers, Plans, Deploy, Setup, Agent tools, Templates, 🌙 Auto, Help.

**Session và việc**

| Lệnh | Cú pháp | Tác dụng |
|---|---|---|
| `/menu`, `/start` | `/menu` | Bảng nút chính, kèm số session, số yêu cầu mở, số agent đang chờ |
| `/list` (`/live`) | `/list`, `/list all` | Session (s#) và project (p#), lane con của từng project; `all` hiện cả project rảnh và lane đã đóng |
| `/send` | `/send <s#\|p#\|project> <nội dung>` | Giao yêu cầu cho agent |
| `/queue` | `/queue` | Yêu cầu đang chờ / đã gán / đang làm (m#), tuổi tính bằng phút |
| `/cancel` | `/cancel <m#>` | Huỷ yêu cầu. Đang làm dở thì session được báo dừng, câu trả lời đến muộn bị đánh dấu huỷ |
| `/asks` | `/asks` | Các yêu cầu giữa agent còn mở |
| `/reviews` | `/reviews` | Các lượt game-review (`review_project`), lượt đang chạy có nút 🛑 Stop |
| `/open` | `/open`, `/open <folder\|p#> [opencode\|codex\|antigravity] [new]` | Mở session agent cho project (mặc định Claude Code) trong Terminal trên máy; không đối số thì hiện nút chọn project; `new` mở thêm session dù đã có |
| `/close` | `/close <s#>` | Đóng session (push: dừng tiến trình; pull: dừng vòng lặp) |
| `/off`, `/on` | `/off <s#>`, `/on <s#>` | Ngừng / tiếp tục giao việc cho session |
| `/main` | `/main [p#] <s#>` · `/main p# auto` · `/main auto on\|off` | Ghim session làm 👑 main (không phải session con) · bỏ ghim · bật/tắt tự chọn main |
| `/route` | `/route` · `/route p# central\|main` · `/route p# tool\|product` | Xem / đổi người điều hướng và loại project (mục 7) |
| `/model` | `/model` · `/model p# child\|router <opus\|sonnet\|haiku\|default>` | Model Claude của session con và router tạm, áp dụng cho session mới |
| `/children` | `/children prune <lane\|folder>` | Xoá thư mục của một lane con đã đóng (branch giữ lại), có nút xác nhận |
| `/forget` | `/forget <p#>` | Bỏ tên project cũ khỏi `/list` (không đụng ổ đĩa) |
| `/auto` | `/auto` | Bật/tắt 🌙 auto-accept (mục 6) |
| `/agents` | `/agents` | Mọi agent: 🖥 app, ⌨️ CLI (nút ⏸ Pause / ▶️ Resume, ✖ Close có hỏi lại), ☁️ API (bật/tắt) |

**Cài đặt**

| Lệnh | Cú pháp | Tác dụng |
|---|---|---|
| `/setup` | `/setup` | Trạng thái hệ thống; nút 🔄 Refresh, ⬇️ Install / update global, 🔗 Connect / 🔌 Disconnect agent, 🗑 Remove global (hỏi lại). Chỉ sửa được từ môi trường prod |
| `/mcp` | `/mcp` | Bật/tắt từng tool agent gọi (mục 9) |
| `/keys` | `/keys` | API key và model; giá trị chỉ nhập trong chat riêng, bot xoá tin ngay |
| `/providers` | `/providers` · `/findmodel <provider> [text]` · `/addmodel <provider> <model-id>` | Provider dùng chung, tìm và thêm model |
| `/plan` | `/plan [p#]` | Model theo từng role của project |
| `/deploy` | `/deploy` · `/deploy [project\|p#] [uat\|testflight\|appstore\|review] [branch] [dry\|real]` | Chạy `deploy.sh` của project; phần nào bỏ trống thì hỏi bằng nút; luôn có bước xác nhận; deploy đang chạy có 🛑 Stop |

**Lệnh khác** (không có trong menu "/"): `/task <mô tả>` (kèm file/album), `/status`, `/report <task_id>`, `/steps <task_id>`, `/retry <task_id>`, `/stop <id>`, `/answer <task_id|hin_…> <nội dung>`, `/feedback <task_id|report_id> <nội dung>`, `/approvals`, `/templates` (reply vào một tin để có lệnh điền sẵn id), `/tools`, `/tool <name> <json args>`, `/plugins`, `/capabilities`, `/roles`, `/agents_status`, `/sessions`.

Ảnh/file gửi vào control group mà không có caption `/task …` và không phải reply thì bị bỏ qua (bot báo lại).

## 5. Trả lời câu hỏi của agent

Agent hỏi bằng `request_human_input`; câu hỏi (🙋 CÂU HỎI, mã `hin_…`) vào control group, tối đa 6 lựa chọn.

| Cách trả lời | Kết quả |
|---|---|
| Bấm một nút lựa chọn | Câu trả lời là đúng nhãn nút |
| Reply vào câu hỏi bằng chữ | Nhận cả câu trả lời tự do, kể cả khi câu hỏi có nút |
| `/answer hin_… <nội dung>` | Như reply |

Agent nhận câu trả lời ngay trong session đã hỏi (được đánh thức, không cần poll): `💬 The operator answered your question hin_…:` rồi nội dung. Sau khi trả lời, mọi bản sao của câu hỏi trên Telegram mất nút; trả lời lần hai bị từ chối ("request … is answered").

**Câu hỏi hỏi trong chat riêng của agent.** Codex (hook `Stop`) và Cursor (`afterAgentResponse`) gửi tin cuối của lượt cho gateway. Nếu tin kết thúc bằng câu hỏi hay xin xác nhận, câu hỏi được đưa lên Telegram với ✅ Đồng ý / ❌ Không; nếu nhắc tới commit, push, merge, release, deploy hoặc git pull thì thành câu hỏi bảo mật. Bỏ qua nếu agent vừa tự hỏi bằng `request_human_input` trong 10 phút trước.

**Nhắc lại.** Câu hỏi chưa ai trả lời được gửi lại sau mỗi 30 phút ("🔁 Dạ, nhắc lại câu hỏi…") cho tới khi hết hạn. Trả lời ở bản nào cũng được.

**Câu hỏi tự đóng.** Câu hỏi được đóng (mất nút, không nhắc nữa, không đẩy lại cho agent) khi:
- agent gọi `close_question` vì đã được trả lời trong session; group nhận "✅ hin_… đã được trả lời trong session";
- cùng session hỏi câu mới, gửi báo cáo (`submit_report`) hoặc trả lời một yêu cầu (`reply_message`): câu hỏi cũ coi như đã xử lý trong session;
- với câu hỏi chuyển từ chat riêng của Codex/Cursor: lượt sau của agent kết thúc (đã được trả lời trong chat).

### Câu hỏi bảo mật (passcode)

Dùng cho release, deploy, commit, push, merge, git pull. Tin có 🔐 ở đầu.

| Nút | Hành vi |
|---|---|
| Lựa chọn "giữ lại" (bắt đầu bằng ❌, 🚫, Chưa, Không, Từ chối, Huỷ, Dừng, Để sau, No, Not now, Cancel, Reject, Decline) | Trả lời ngay, không cần passcode |
| Lựa chọn khác, hiện dạng `🔐 <lựa chọn>` | Bot nhắn riêng xin passcode |
| `✅ Duyệt (nhập passcode)` | Khi câu hỏi không có lựa chọn |
| `❌ Từ chối` | Bot tự thêm nếu không có lựa chọn "giữ lại" nào |

Quy trình duyệt:
1. Bấm nút 🔐. Bot nhắn riêng: gửi passcode trong 5 phút, còn N lần thử, `/cancel` để thôi. Nếu bot không nhắn riêng được, nó đưa link `https://t.me/<bot>`: mở chat, rồi bấm lại.
2. Gửi passcode **trong chat riêng**. Bot xoá tin ngay (xoá không được thì nhắc anh tự xoá); passcode không được lưu hay ghi log.
3. Đúng: agent nhận `✅ Duyệt (đã xác minh passcode): <lựa chọn đã bấm>`, group nhận thông báo đã duyệt.
4. Sai: còn 2, rồi 1 lần. **Sai 3 lần thì câu hỏi bị huỷ**; agent được báo là KHÔNG được duyệt, phải bỏ bước đó và báo chưa làm.
5. Quá 5 phút: bấm lại nút.

Lưu ý:
- Reply bằng chữ vào câu hỏi bảo mật trong group: tin bị xoá, **không được kiểm** như passcode; bot nhắc bấm nút và gửi passcode trong chat riêng. Lỡ gõ passcode trong group thì nên đổi passcode.
- Không đặt `TELEGRAM_CONFIRM_PASSCODE`: trên Telegram chỉ còn các nút từ chối/giữ lại; muốn duyệt thì xác nhận trực tiếp trên máy.

## 6. Auto-accept 🌙

Khi vắng mặt, bật auto-accept để agent không phải chờ.

- **Bật:** `/auto` hoặc nút 🌙 Auto trong `/menu`. Bot xin passcode trong chat riêng (5 phút, xoá tin ngay). Sai 3 lần: vẫn tắt. Cần có `TELEGRAM_CONFIRM_PASSCODE`.
- **Tắt:** `/auto` hoặc nút 🌙 lần nữa, tắt **ngay**, không cần passcode.
- Khi bật, các câu hỏi đang chờ được trả lời luôn (group báo số câu), câu hỏi mới mang dòng "🌙 Auto-accept đang bật".

Cách tự trả lời:

| Loại câu hỏi | Câu trả lời agent nhận |
|---|---|
| Bảo mật | `✅ Duyệt (đã xác minh passcode): <lựa chọn đầu tiên không phải "giữ lại">`, tức là **commit/release cũng được duyệt** |
| Có lựa chọn | Lựa chọn đầu tiên |
| Không có lựa chọn | "Bạn tự quyết theo đề xuất tốt nhất của bạn" |

## 7. Điều hướng: `/route`

`/route` không đối số: Gateway Agent hiện tại và mỗi project dạng `p# tên · <mode> · <kind>`.

**Ai điều hướng** (`/route p# central|main`, mặc định `main`):

| Mode | Ý nghĩa |
|---|---|
| `main` | 👑 session chính của project điều hướng; main offline thì group được hỏi (mục 8) |
| `central` | Gateway tự điều hướng (luật, rồi tới Gateway Agent) |

**Loại project** (`/route p# tool|product`, mặc định `product`):

| Kind | Cách chạy |
|---|---|
| `product` | Repo game/app. **Một worker** (lane `dev`: mọi việc code, theo thứ tự) và **một session deploy** (lane `deploy`: build, release, deploy). Lane khác đều về `dev`. QA, UI/UX, PO, research là project `tool` riêng, worker sẽ hỏi chúng |
| `tool` | Project agent (UIUX, PO, QA, research). Yêu cầu chạy song song, **mỗi chủ đề một session**: việc tiếp theo hoặc câu hỏi về việc đang làm giữ lane đó (cùng session, cùng ngữ cảnh); chủ đề mới mở lane mới. Lane chung `task` chia cho session rảnh, hoặc mở thêm khi số session còn dưới giới hạn (mặc định 3). Chủ đề mới có thể được mời các session rảnh trước (🙋 trong notice channel) |

Lane con chạy trong bản sao riêng `<repo>/.agw-children/<lane>-<id>` trên branch `agw/<lane>-<id>`; con rảnh 30 phút thì đóng (thư mục giữ lại, mở lại khi lane có việc).

## 8. Việc bị kẹt và agent chờ nhau

### Việc bị kẹt (⚠️)

Yêu cầu bị giữ ở trạng thái *stalled* và báo lên group khi: agent dừng vì rate limit / usage limit / lỗi API (hook báo lỗi), không ai trả lời trong lease 60 phút, session rảnh mà không nhận việc trong 10 phút, hoặc main không điều hướng. Session đó không nhận việc mới cho tới khi chạy lại.

| Nút | Tác dụng |
|---|---|
| `↪️ s# <tên>` | Chuyển sang session khác đang mở của project (tối đa 4) |
| `⏳ Chờ session hồi phục` | Đợi; khi session chạy lại bot hỏi lại |
| `✅ Xử lý tiếp ở đây` | Chỉ có khi session đã chạy lại ("🔄 … hoạt động lại") |
| `❌ Huỷ` | Huỷ yêu cầu |

Không có session nào khác của project thì bot gợi ý `/open`. Chuyển mà không có đích thì yêu cầu về hàng chờ, session đầu tiên của project mở ra sẽ nhận.

### Session chính offline (👑)

Yêu cầu theo project đang chờ main điều hướng. Nút: `👑 s# …` (biến session đang mở thành main), `🆕 Mở session chính mới`, `⏳ Chờ session chính`.

### Agent nhờ agent (🤝 / 🆘)

Mọi yêu cầu `ask_agent` giữa các agent được sao vào group (🤝 AGENT NHỜ, nút ✍️ Answer myself / ❌ Cancel) cùng câu trả lời (💡). Khi có trục trặc, gateway gửi 🆘 AGENT ĐANG CHỜ kèm "Problem":
- không session nào nhận được yêu cầu trong 2 phút;
- session nhận đã im lặng, hoặc quá 60 phút từ lúc giao mà chưa trả lời.

| Nút | Tác dụng |
|---|---|
| `🔁 Resend` | Gửi lại (cùng session, hoặc hàng chờ) |
| `➡️ Other session` | Chọn session khác từ danh sách |
| `🚀 Open it` | Mở project đích; yêu cầu tới khi session kết nối |
| `✍️ Answer myself` | Bot hiện ô reply; nội dung trả lời đi thẳng về agent đã hỏi |
| `❌ Cancel` | Huỷ; agent hỏi được báo |

Reply trực tiếp vào tin 🤝 / 🆘 cũng là trả lời. Gửi escalation lên Telegram thất bại thì gateway thử lại sau 5 phút.

## 9. Bật/tắt tool của agent: `/mcp`

`/mcp` hiện mỗi tool MCP agent gọi (`generate_art`, `delegate_task`, `ask_agent`, `dispatch`, `request_human_input`, `submit_report`, `review_project`…) với ✅ bật / ⛔ tắt. Bấm để đảo; áp dụng cho lần gọi kế tiếp, không cần khởi động lại; lựa chọn được lưu qua các lần khởi động. Dòng cuối có công tắc provider cho `generate_art`: 🎨 images (fal), 🧊 3D (Meshy), chỉ hiện khi provider đã cấu hình. Tool chưa từng chỉnh thì theo cấu hình mặc định (đa số là bật).

## 10. Tình huống thường gặp

| Tình huống | Làm gì |
|---|---|
| Session không trả lời | `/queue` xem m# đang ở đâu, `/list` xem trạng thái session. Chờ nút ⚠️ (lease 60 phút, hoặc 10 phút nếu session rảnh không nhận), hoặc `/cancel m#` rồi `/send` sang session khác. Session hỏng: `/close s#`, rồi `/open p# new` |
| `/open` báo project mở trong Claude desktop nhưng không kết nối | Waiter đã dừng (app khởi động lại?). Gõ một tin bất kỳ trong session đó để nó tự kết nối lại, hoặc `/open <tên> new` để mở Terminal |
| Thấy cùng một câu hỏi hai lần | Thường là bản nhắc lại 30 phút: trả lời một bản, các bản kia mất nút. Câu hỏi đã trả lời trong session thì agent gọi `close_question`, hoặc tự đóng khi session hỏi câu mới |
| Bấm nút báo "… is answered/expired/cancelled" | Câu hỏi đã xong ở nơi khác; không cần làm gì |
| Telegram flood control (quá nhiều tin) | Bot tự chờ theo `retry_after` của Telegram và thử lại tối đa 3 lần. Câu hỏi không gửi được thì agent nhận lỗi "the question did not reach Telegram" và hỏi lại sau; escalation thử lại sau 5 phút. Đặt `TELEGRAM_NOTICE_CHAT_ID` để tách thông báo hệ thống khỏi group |
| Reply vào báo cáo mà bot báo "I can't tell which task this refers to" | Reply vào tin do bot gửi có tag `#tsk_…`, hoặc dùng `/feedback <task_id> <nội dung>` |
| Muốn ngừng giao việc cho một session một lúc | `/off s#`, rồi `/on s#` |
| Lỡ gửi passcode vào group | Đổi `TELEGRAM_CONFIRM_PASSCODE` |

## 11. Cấu hình Telegram liên quan

| Biến | Ý nghĩa |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token bot (bí mật) |
| `TELEGRAM_CONTROL_CHAT_ID` | Control group (bắt buộc khi có token) |
| `TELEGRAM_REPORT_CHANNEL_ID` | Report channel (bắt buộc khi có token; bot là admin có quyền "Post messages") |
| `TELEGRAM_NOTICE_CHAT_ID` | Notice channel cho thông báo hệ thống (tuỳ chọn) |
| `TELEGRAM_DISCUSSION_CHAT_ID` | Discussion group nơi reply được xử lý; ghi đè `config/app.yaml` |
| `TELEGRAM_ALLOWED_USER_IDS` | Danh sách user id của operator (không được rỗng) |
| `TELEGRAM_CONFIRM_PASSCODE` | Passcode cho câu hỏi bảo mật và để bật auto-accept (bí mật). Không đặt: chỉ từ chối được từ Telegram |
| `TELEGRAM_MODE` | `polling` (mặc định) hoặc `webhook` (cần `TELEGRAM_WEBHOOK_SECRET`, URL `https://`) |
| `TELEGRAM_PROGRESS_UPDATES` | Gửi cập nhật tiến độ (mặc định tắt) |

Môi trường dev/uat phải có bot riêng; token của prod bị từ chối ở đó.
