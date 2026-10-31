# Workflow engine

Tài liệu này gộp các tài liệu cũ về workflow, workflow engine, recovery và workflow security. Nguồn sự thật:
`app/workflow.py` (định nghĩa, validate cấu trúc, state machine, contract), `app/engine.py` (scheduler),
`app/planner.py` (route + validate plan), `app/orchestrator.py` (task, executor, approval, recovery,
retention) và `config/workflows/*.yaml`. Plugin xem [PLUGINS.md](PLUGINS.md), mô hình bảo mật chung xem
[SECURITY.md](SECURITY.md).

## 1. Từ request tới workflow

`Planner.route()` quyết định theo thứ tự:

| Thứ tự | Điều kiện | Kết quả (`RoutingDecision.mode`) |
|---|---|---|
| 1 | request có `workflow` (định nghĩa inline) | `explicit_workflow` |
| 2 | request có `workflow_id` | `config_workflow` (workflow trong `config/workflows`) |
| 3 | request có `tool` hoặc `mode=direct_tool` | `direct_tool` |
| 4 | khớp một route trong `config/routing/routing.yaml` (theo `task_type`, `priority`) | route cấu hình |
| 5 | `domain=general` | `explicit`, task `answer` |
| 6 | `domain=<plugin>` | `explicit` (plugin không active → `rejected`) |
| 7 | plugin classifier: điểm cao nhất ≥ 0.5 và hơn ứng viên kế ≥ 0.15 | `classified` |
| 8 | nhiều plugin cùng ≥ 0.5 nhưng sát nhau | `clarify` (hỏi người dùng) |
| 9 | có `routing.default_workflow` (hiện là `general.answer`) | `config_default` |
| 10 | có capability `model.text` | `fallback` (trả lời bằng model, chỉ đọc) |
| 11 | còn lại | `clarify` |

Tối đa 3 vòng làm rõ; quá 3 vòng thì task fail. Plan được validate bằng `Planner.validate` trước khi tạo
`WorkflowRun`; constraint của request được chép vào workflow và không thể bị plan bỏ đi.

Route cấu hình hiện có: `unity_ui → unity.ui_task`, `code_change → code.implement_and_test`,
`design_image → design.image`, `design_model3d → design.model3d`, `research_report →
research.document_report`, `write_file → files.write_with_approval`. Workflow cấu hình khác:
`general.answer`, `visual_feedback`, `feedback.acknowledge`, `repo.status`.

## 2. Định nghĩa workflow (`WorkflowDef`)

```yaml
id: demo.parallel
version: "1"
domain: general
max_concurrency: 4          # 1..16
timeout_s: 3600
failure_policy: fail_fast   # hoặc continue
allow_partial: false
final_report: true          # false: run thành công không gửi tin tổng kết
steps:
  - {id: a, executor: tool, ref: qa.run, inputs: {check: sleep_a}}
  - {id: b, executor: tool, ref: qa.run, inputs: {check: sleep_b}}
  - id: agg
    executor: internal
    ref: aggregate
    depends_on: [a, b]
    inputs: {a: $steps.a.output.status, b: $steps.b.output.status}
    condition: {ref: $steps.a.output.status, op: eq, value: passed}
    retry: {max_attempts: 3, delay_s: 1, backoff: 2, max_elapsed_s: 30, retry_on: [timeout, transient]}
compensations: []
success_criteria: [{kind: step_succeeded, step: agg}]
outputs: {result: $steps.agg.output}
```

Trường của step (`StepDef`): `id`, `executor`, `ref`, `agent` (cho step `capability`), `title`,
`depends_on`, `condition` (`eq|ne|truthy|falsy|in|gt|lt`), `inputs`, `output_schema`,
`required_capabilities`, `permissions`, `timeout_s` (≤ 7200), `retry`, `idempotent`, `requires_approval`,
`on_failure`, `compensate_with`, `mandatory_gate` (validator: fail thì chặn thành công).

**Executors**

| `executor` | `ref` |
|---|---|
| `tool` | tên tool đã đăng ký, chạy qua policy engine |
| `agent` | role + `required_capabilities`, route qua registry |
| `plugin` | `<plugin>.<operation>` |
| `validator` | gate core (`core.artifacts_readable`, `core.json_schema`, `core.non_empty`) hoặc của plugin |
| `internal` | `aggregate`, `select`, `assert`, `context.load`, `prompt.build`, `report.publish` |
| `approval` | checkpoint chờ người duyệt |
| `capability` | id capability trong `config/capabilities`, kèm `agent` |

**Mapping**: `$input.<path>` (objective + `data` của request), `$task.objective|task_id|attachment_ids`,
`$steps.<id>.output|artifacts|state.<path>`. Một mapping `$steps` chỉ được đọc step **tổ tiên**.

Gửi trực tiếp một workflow: `POST /api/v1/tasks {"objective": "...", "workflow": {...}}`. Nó được validate
giống hệt plan do plugin sinh ra.

### Validate trước khi chạy

Bị từ chối với danh sách lỗi (`InvalidWorkflow.errors`): workflow rỗng, id trùng hoặc sai mẫu, dependency
không tồn tại, tự phụ thuộc, vòng lặp (in ra đường vòng), mapping sai hoặc đọc step không phải tổ tiên,
compensation không tồn tại, thiếu `ref`, tool/executor/validator/internal op không biết, tool không
routable. Thiếu capability bị báo `unsupported`.

## 3. Trạng thái

### Step

```
pending → ready → running → succeeded
   │        │        ├→ retry_waiting → running      (idempotent + lỗi retry được + còn lượt + còn giờ)
   │        │        ├→ failed | timed_out | unknown | cancelled
   │        ├→ waiting_for_approval → running          (→ cancelled khi bị từ chối/hết hạn)
   └→ skipped (condition false hoặc dependency fail/skip)
running / retry_waiting / waiting_for_approval → ready  (chỉ khi recovery)
```

Mọi chuyển trạng thái được kiểm (`StepRecord.transition`) và lưu trước khi engine đi tiếp. Record giữ các
attempt (thời gian, lỗi, loại lỗi), executor thực dùng, output, artifact ids, approval id và người duyệt,
thời điểm bắt đầu/kết thúc.

### Run và task

- `WorkflowRun.state`: `running`, `waiting`, `succeeded`, `failed`, `cancelled`, `unknown`, `partial`.
  Run chỉ `succeeded` khi không step nào ở trạng thái xấu, mọi validator `mandatory_gate` đã qua và mọi
  `success_criteria` đúng. Có step `unknown` → run `unknown`. Bị huỷ hoặc approval bị từ chối/hết hạn →
  `cancelled`. Có `allow_partial` và ít nhất một step thành công → `partial`. Còn lại `failed`.
- `TaskState`: `created`, `queued`, `planning`, `running`, `waiting_for_user`, `waiting_for_approval`,
  `validating`, `succeeded`, `failed`, `cancelled`, `unknown`, `expired`.

### Scheduling

- Đánh giá `pending → ready` theo thứ tự định nghĩa; step phụ thuộc chỉ mở khi mọi step trước đã
  **succeeded**.
- Chỉ khởi chạy khi số step đang chạy < `max_concurrency` (backpressure, không có hàng đợi vô hạn).
- Output được kiểm theo `output_schema`; mọi artifact step trả về phải tồn tại và thuộc task family (hoặc
  là input được truyền rõ) trước khi step sau được mở.
- `fail_fast` (mặc định) huỷ các step đang chạy khi có lỗi; `continue` cho các nhánh độc lập chạy tiếp.

## 4. Retry, timeout, idempotency, compensation

| Tình huống | Hành vi |
|---|---|
| Step idempotent, lỗi nằm trong `retry_on`, còn lượt | `retry_waiting`, chờ `delay_s × backoff^n`, chạy lại |
| Step không idempotent | không bao giờ tự retry (lỗi ghi `not retried: step has side effects`) |
| Timeout, step idempotent | `timed_out` |
| Timeout hoặc crash, step không idempotent | `unknown` (tác dụng phụ có thể đã xảy ra) |

Idempotent hiệu lực (`Orchestrator.is_idempotent`): `tool` khi tool không `side_effects`; `agent` khi không
cần `workspace.write`; `plugin` khi operation nằm trong `idempotent_operations()`; `capability` khi
capability không `side_effects` và không executor nào là runtime hoặc tool có side effect. Khai báo
`idempotent: false` chỉ làm step **kém** idempotent hơn, không bao giờ ngược lại.

`compensate_with` trỏ tới một step trong `compensations`. Compensation chỉ chạy khi run kết thúc `failed`,
`unknown`, `cancelled` hoặc `partial`, step hoàn thành mới nhất trước; kết quả ghi vào `run.compensation`.
Compensation **không** phải là cam kết rollback.

**Huỷ** (`/stop`, `POST /api/v1/tasks/{id}/cancel`, nút Dừng): connector runtime nhận SIGINT → SIGTERM →
SIGKILL theo process group; approval/làm rõ đang chờ bị huỷ; engine huỷ từng step; QA subprocess bị kill.
Step chưa chạy → `cancelled`; step không idempotent đang chạy → `unknown`.

## 5. Approvals

Một step phải chờ duyệt (`Orchestrator.needs_approval`) khi:

- `requires_approval: true` hoặc `executor: approval`;
- tool có `requires_approval` (ví dụ `media.generate`, `media.generate_3d`, tool MCP không read-only);
- step `agent` cần `workspace.write`;
- khớp luật `policies.approvals` trong `config/policies/policies.yaml` (fnmatch trên executor id). Hiện có:
  `runtime:claude_code.new_execution`, `runtime:opencode.new_execution`,
  `capability:workspace.write_file`, `tool:media.generate`.

Luồng: một bản ghi `approval` (`apr_...`) được lưu với trạng thái `pending`, task chuyển
`waiting_for_approval`, tin nhắn có nút ✅ Duyệt / ❌ Từ chối / 🛑 Dừng được gửi vào **control group**. Quyết
định ghi người duyệt và thời điểm, sự kiện `approval.granted`/`approval.denied`. Hết `ACTION_TTL_S` (mặc
định 3600 s) không trả lời → `expired` và step bị từ chối. Quyết định chỉ dùng một lần; id không tồn tại
hoặc đã quyết → lỗi. Qua REST cần `OPERATOR_TOKEN` (`POST /api/v1/approvals/{id}`), vì agent giữ
`API_TOKEN`.

Ngoại lệ duy nhất trong code: task tạo ảnh (`ART_TASK_TYPES`) mà operator tự yêu cầu bản sửa trên Telegram
(`data.operator_requested`) được ghi là đã duyệt bởi operator, không hỏi lại.

Auto-accept (`/auto`) chỉ trả lời câu hỏi `request_human_input` của agent, **không** duyệt các step approval
của workflow.

## 6. Artifacts

`ArtifactStore` (`app/storage.py`): file nằm dưới `DATA_DIR/artifacts/<task_id>/<artifact_id>_<tên>`, ghi
create-only (`open(..., "xb")`), tối đa 50 MB mỗi artifact, metadata (kind, sha256, size, MIME, producer,
step, validation status) lưu trong store. Khi đọc: path phải nằm trong thư mục gốc, không đọc symlink, và
sha256 phải khớp, nếu không thì lỗi `content changed on disk`. Một task chỉ đọc được artifact của task
family hoặc artifact được truyền trong `artifact_ids`. Nội dung tải qua `GET
/api/v1/artifacts/{id}/content` (cần `API_TOKEN`).

## 7. Sự kiện

Lưu trong bảng events, xem bằng `GET /api/v1/tasks/{id}/events`: `workflow.created/started/completed`,
`step.scheduled/started/completed/failed/retry_scheduled`, `approval.requested/granted/denied`,
`artifact.produced`, `cancellation.requested`, `compensation.executed`, `tool.started/finished`,
`task.state`, `task.routed`, `task.recovered`, `clarification.answered`. Telegram chỉ nhận lỗi step,
retry, approval, kết quả tool và báo cáo cuối (cộng step hoàn thành khi `TELEGRAM_PROGRESS_UPDATES=true`).

## 8. Recovery sau khi khởi động lại

**Lưu trữ**: SQLite tại `DATA_DIR/STATE_DB` (mặc định `var/state.db`), `journal_mode=WAL`,
`synchronous=FULL`. Được lưu: task (gồm request đã chuẩn hoá và quyết định route), workflow run (cả định
nghĩa), step record, approval, metadata artifact/attachment, agent session, bản ghi tool, sự kiện.
**Không** lưu: Quick Action token (nút Telegram, chỉ trong bộ nhớ, nút cũ trả lời "unknown or expired
action") và trạng thái capability (dựng lại khi start). `STATE_DB=:memory:` tắt độ bền (dùng trong test).
Mỗi database chỉ một process gateway.

**`Orchestrator.recover()`** (gọi khi start):

1. Mọi approval `pending` chuyển `expired` (step của chúng đã chết cùng process).
2. Với mỗi task gốc chưa kết thúc:

| Tình trạng task | Hành động |
|---|---|
| `queued` / `planning` / `waiting_for_user`, hoặc chưa có run | plan lại từ request đã lưu (plan không có side effect) |
| đã có run | tải run + step record → `engine.prepare_recovery` → chạy tiếp |

`prepare_recovery` cho từng step:

| Trạng thái lúc crash | Idempotent? | Kết quả |
|---|---|---|
| đã kết thúc | – | giữ nguyên, không chạy lại |
| `running`, `retry_waiting` | có | → `ready`, chạy lại |
| `running`, `retry_waiting` | **không** | → `unknown` ("interrupted by restart; non-idempotent step may have completed"), **không replay**; step sau bị skip, task kết thúc `unknown` để đối soát thủ công |
| `waiting_for_approval` | – | → `ready`, approval được hỏi lại (yêu cầu mới) |

Bằng chứng: `tests/test_recovery.py` dùng process thật bị kill bằng `os._exit(9)`; step idempotent chạy
tiếp và thành công, step không idempotent thành `unknown`, log side effect chỉ có đúng một lần.

**Giới hạn**: session runtime đang chạy chết cùng gateway, step của nó là `unknown`, không tự đối soát. Git
worktree còn lại nhưng không tự gắn vào task mới. Chưa có API đối soát tự động: xem events/workflow của task,
rồi `/retry` (tạo task mới) nếu phù hợp.

## 9. Bảo mật của workflow

| Mối đe doạ | Biện pháp |
|---|---|
| Workflow tham chiếu tool/executor không tồn tại hoặc không khả dụng | `Planner.validate` từ chối trước khi chạy bất cứ thứ gì |
| Workflow gọi tool vượt policy | Step tool chỉ chạy qua `ToolExecutor` với `TOOL_PERMISSIONS ∩ step.permissions`; định nghĩa chỉ thu hẹp được, không mở rộng |
| Bỏ qua approval | Tool cần duyệt, agent `workspace.write`, step `approval` và luật `policies.approvals` luôn chờ quyết định đã lưu; quyết định một lần, có hạn |
| Lặp lại side effect | Step không idempotent không tự retry; timeout/crash/huỷ → `unknown`; recovery không replay |
| Khai step có side effect là idempotent để được retry | `idempotent` chỉ hạ được |
| Step sau dùng artifact thiếu hoặc của task khác | Artifact phải tồn tại và thuộc task family trước khi mở step sau; đọc luôn kiểm hash |
| Đọc dữ liệu chéo task | `read_input` chỉ resolve attachment/artifact của chính task; attachment gắn với một task |
| Ghi đồng thời vào một workspace | Mỗi worktree một writer; worktree của task tạo đúng một lần dưới lock |
| Chạy vô hạn | Timeout step và workflow, `max_concurrency`, retry có giới hạn và `max_elapsed_s`, tối đa 3 vòng làm rõ |
| Gửi trùng | `idempotency_key` trả về cùng task |

Chưa có: cách ly multi-tenant, quota theo người dùng, workflow có chữ ký.
