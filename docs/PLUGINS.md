# Plugin và capability

Tài liệu này gộp các tài liệu cũ về plugin, capability registry/matrix và từng plugin. Nguồn sự thật là
code: `app/plugins/`, `app/capabilities.py`, `app/planner.py`, `app/orchestrator.py`,
`app/gateway_config.py` và `config/`. Workflow engine xem [WORKFLOWS.md](WORKFLOWS.md), bảo mật xem
[SECURITY.md](SECURITY.md).

## 1. Mô hình plugin

Plugin domain là một module Python có hàm `create_plugin()` trả về object theo protocol
`app.plugins.base.DomainPlugin`. Plugin chỉ phụ thuộc vào các contract của core (`intake`, `workflow`,
`engine.StepOutput/StepError`, `tools`, `plugins.base`), không phụ thuộc Telegram, REST hay plugin khác.
Một workflow của plugin có thể tham chiếu bất kỳ tool nào đã đăng ký theo tên (đi qua registry).

Plugin đóng góp:

| Thành phần | Ý nghĩa |
|---|---|
| `manifest: PluginManifest` | id, tên, version, mô tả, `categories`, `keywords`, `permissions` (trần quyền cho tool của plugin), `artifact_types`, `operations` (`OperationSpec`) |
| `classify(req, attachments)` | trả `Classification` (score 0..1 + lý do) hoặc `None` nếu không phải việc của plugin |
| `plan(req, task_type, env)` | trả `PlanResult`: một `WorkflowDef`, hoặc một câu hỏi làm rõ (`question` + `options`), kèm `limitations` |
| `tools(services)` | danh sách `ToolDefinition`; tên phải bắt đầu bằng `<id>.` |
| `operations()` | `name -> async fn(ctx, inputs)`, gọi trong workflow bằng step `executor: plugin`, `ref: <id>.<name>` |
| `validators()` | `name -> async fn(ctx, inputs) -> GateResult`, gọi bằng step `executor: validator` |
| `idempotent_operations()` | các operation được phép tự retry/chạy lại khi recovery; còn lại không bao giờ tự retry |

`OperationSpec` gồm `task_type`, `description`, `required_capabilities` (kiểm tra trước khi plan),
`optional_capabilities`, `input_schema` (validate `TaskRequest.data`), `side_effects`, `example`.

`PlanningEnv` (lúc plan) chỉ cho plugin biết: `has(capability)`, danh sách tool, các QA check đã cấu hình,
workspace có được cấu hình không, metadata attachment, và config riêng của plugin. Không có handle thực thi.

`StepContext` (trong operation/validator) cung cấp `read_input(ref)` (chỉ attachment/artifact của task),
`add_artifact()`, `read_artifact()`, `artifact()`, `run_tool()` (qua policy engine), `approval()`,
`notify()`, `workspace()`, `plugin_config()`, `settings` và `agents()`.

## 2. Plugin hiện có

Mặc định `PLUGINS=software,documents,data,media,unity,game_research` (`app/config.py`). Tên trần được
hiểu là `app.plugins.<name>`.

| id | Module | Làm gì | Operations (`task_type`) | Tool đăng ký | Validators | Permissions (manifest) |
|---|---|---|---|---|---|---|
| `software` | `app/plugins/software.py` | Kiểm tra repo, chạy check, tạo file trong worktree riêng, agent implement/review | `inspect_repo`, `run_checks`, `apply_files`, `implement`, `implement_reviewed`, `parallel_implement` | không có (dùng tool core `git.*`, `workspace.*`, `qa.run`) | `software.checks_passed` | `workspace:read`, `workspace:write`, `process:execute`, `artifact:write` |
| `documents` | `app/plugins/documents.py` | Trích xuất cấu trúc/fact từ tài liệu được gửi, so sánh, báo cáo có trích dẫn | `report`, `compare`, `cross_report`, `summarize`, `web_research` | `documents.extract`, `documents.compare` | `documents.report_valid` | `data:read`, `artifact:write` |
| `data` | `app/plugins/data.py` | CSV/JSON: profile, kiểm schema, transform, aggregate, join, convert | `profile`, `transform`, `aggregate`, `join`, `convert` | `data.profile`, `data.validate`, `data.transform`, `data.aggregate`, `data.join`, `data.convert` | `data.output_valid` | `data:read`, `artifact:write` |
| `media` | `app/plugins/media.py` | Đọc/kiểm ảnh; sinh ảnh/3D qua provider đã cấu hình | `inspect`, `generate` | `media.inspect`; `media.generate` (chỉ khi có image provider); `media.generate_3d` (chỉ khi có 3D provider) | `media.image_valid` | `data:read`, `artifact:write`, `provider:image` |
| `unity` | `app/plugins/unity.py` | Đọc metadata project Unity, hỏi Editor qua MCP, pipeline design → assets → implement → review | `inspect_project`, `editor_status`, `feature` | `unity.project_info` | `unity.project_valid` | `workspace:read` |
| `game_research` | `app/plugins/game_research.py` | Kiểm kê game (source hoặc build) thành evidence có checksum, kế hoạch playtest, lưu design mới | `research`, `design` | `game_research.inventory`, `game_research.evidence` | không có | `workspace:read`, `artifact:write` |

Ghi chú theo plugin (đã đối chiếu code):

- **software**: `apply_files` nhận `data.files = {path: content}`, ghi create-only trong worktree của task,
  rồi diff, status, check và `software.change_report`. `implement*` cần capability `workspace.write`
  (một agent runtime ghi được); `implement_reviewed` cần thêm `model.structured`. Không commit, push,
  merge hay deploy; branch là `agw/<task_id>`. Không có QA cấu hình thì báo cáo ghi rõ test không chạy.
- **documents**: chỉ text/Markdown UTF-8, trích xuất theo quy tắc. Mỗi fact gắn `[S<n>, line <k>]`.
  `summarize` cần `model.text`. `web_research` cần `search.web`, hiện không có provider nào cung cấp nên
  bị từ chối (không giả lập tìm kiếm). Link trong tài liệu chỉ được báo cáo, không bao giờ được fetch.
- **data**: chỉ dùng `csv`/`json` của stdlib; không chạy code, SQL hay công thức của người dùng. Mỗi output
  là artifact mới, input gốc không bị sửa. Gate `data.output_valid` parse lại output và đối chiếu số dòng,
  cột với metadata đã ghi.
- **media**: `media.generate` chỉ được đăng ký khi có image provider: fal (provider `type: fal` trong
  `config/providers/models.yaml` và có `FAL_KEY`), nếu không thì OpenAI (`OPENAI_API_KEY` +
  `OPENAI_IMAGE_MODEL`). Khi fal tắt mà 3D (Meshy) đang bật, ảnh đi qua Meshy text-to-image.
  `media.generate` và `media.generate_3d` có `requires_approval=True`, `side_effects=True`. Plugin không
  bịa dữ liệu: sai kích thước hoặc thiếu alpha bị gate báo lỗi, không bị che.
- **unity**: thư mục asset là `PLUGIN_CONFIG.unity.asset_dir`. `editor_status` cần tool MCP
  `mcp.unity.editor_status`, chỉ có khi một MCP server tên `unity` được cấu hình và allowlist tool đó.
- **game_research**: xem mục 6.

Plugin được tải tại runtime có thể bật/tắt bằng `POST /api/v1/plugins/{id}/activate|deactivate` (cần
`API_TOKEN`); plugin bị tắt không bao giờ được route tới (domain chỉ định rõ trả về `not active`). Xem
trạng thái bằng `/plugins` trên Telegram hoặc `GET /api/v1/plugins`.

## 3. Capability registry

Có hai lớp, đừng nhầm:

1. **`CapabilityRegistry`** (`app/capabilities.py`): mỗi agent, runtime, provider, tool, MCP server, plugin
   và image provider có một `CapabilityManifest` với id dạng `<kind>:<name>` (ví dụ `tool:data.transform`,
   `plugin:data`, `image_provider:fal`). Trường `provides` khai báo capability trừu tượng như `model.text`,
   `model.vision`, `model.structured`, `workspace.write`, `image.generate`, `tool:<name>`.
   Xem bằng `GET /api/v1/capabilities` (`?refresh=true` chạy lại health check) hoặc `/capabilities`.
2. **Capability theo cấu hình** (`config/capabilities/*.yaml`, `app/agent_registry.py`,
   `app/capability_exec.py`): capability có `input_schema`/`output_schema` và danh sách `executors`
   (`provider`, `runtime`, `tool`, `plugin`, `internal`) theo `priority`. Agent logic trong
   `config/agents/agents.yaml` khai báo capability nào nó có. Step `executor: capability` chọn executor
   phù hợp nhất và chỉ fallback sang executor kế tiếp khi lỗi thuộc nhóm availability/transport
   (`unavailable`, `auth`, `network`, `rate_limit`, `timeout`, ...).

### Thang trạng thái

| Status | Ý nghĩa |
|---|---|
| `declared` | code có, mặc định |
| `discovered` | thấy lúc runtime nhưng chưa bật (ví dụ MCP `tools/list` trả về mà chưa allowlist) |
| `configured` | có cấu hình, chưa kiểm (image provider có key + model) |
| `validated` | một kiểm tra thật miễn phí đã qua; tool và plugin đăng ký hợp lệ vào thẳng mức này |
| `operational` | một lần thực thi thật đã thành công trong process này (`record_execution(ok=True)`) |
| `degraded` | thực thi thật thất bại sau khi đã routable |
| `unavailable` | không cấu hình, health check lỗi, hoặc plugin tải lỗi |

Chỉ `validated`, `operational`, `degraded` là routable. Health check không bao giờ hạ `operational`
xuống `validated`; health check lỗi chỉ làm capability của chính nó `unavailable`. Trạng thái nằm trong
bộ nhớ process, được dựng lại mỗi lần khởi động.

### Kiểm tra capability khi route

`Planner.has(capability)`: `tool:<name>` đúng khi tool đã đăng ký **và** manifest của nó routable; capability
trừu tượng đúng khi có ít nhất một manifest routable `provides` nó. `required_capabilities` được kiểm khi
plan (từ `OperationSpec`) và khi validate từng step. Thiếu capability thì task fail với lý do nêu tên
capability; không có hạ cấp âm thầm.

### Ma trận capability hiện tại

| Capability | Ai cung cấp | Cần gì để routable |
|---|---|---|
| `tool:workspace.list_files/read_file/create_file`, `tool:git.status/diff`, `tool:artifact.hash`, `tool:image.validate`, `tool:qa.run` | tool core (`app/builtin_tools.py`) | luôn đăng ký; `qa.run` chỉ chạy các lệnh trong `QA_COMMANDS` |
| `tool:<plugin>.*` | plugin (bảng mục 2) | plugin được tải và tool qua kiểm tra trần quyền |
| `tool:mcp.<server>.<tool>` | MCP client (`app/mcp_client.py`) | server có trong cấu hình và tool nằm trong `allowed_tools` |
| `model.text`, `model.vision`, `model.structured` | connector model (OpenAI-compatible, DeepSeek, Gemini) và runtime | có key/model và health check qua |
| `workspace.write` | runtime agent `claude_code` (`CLAUDE_CODE_ENABLED`) hoặc `opencode` (`OPENCODE_ENABLED`) | runtime bật và kiểm tra được |
| `image.generate` | `image_provider:fal` hoặc `image_provider:openai_images` | key + model; switch `generate_art` và `art:images` (hoặc `art:3d`) đang bật |
| `search.web` | không có | chưa được triển khai |

Capability theo cấu hình hiện có: `general.answer`, `design.layout_analysis`, `prompt.implementation`,
`implementation.handoff`, `report.publish`, `context.load`, `workspace.write_file`,
`design.image_generate`, `design.model3d_generate` (`config/capabilities/core.yaml`) và
`document.extract`, `document.report`, `research.web_search`, `repo.inspect`
(`config/capabilities/research.yaml`). `research.web_search` trỏ tới `mcp.search.web_search`, chỉ
dùng được khi có MCP server `search`.

## 4. Viết một plugin

### Cấu trúc tối thiểu

```python
from app.plugins.base import OperationSpec, PluginManifest

class MyPlugin:
    manifest = PluginManifest(
        id="mydomain", name="My Domain", version="1.0.0", description="...",
        categories=["..."], keywords=["..."],
        permissions=["data:read", "artifact:write"],
        artifact_types=["report"],
        operations=[OperationSpec(task_type="do_thing", description="...",
                                  required_capabilities=["tool:mydomain.tool"])],
    )
    def classify(self, req, attachments): ...
    def plan(self, req, task_type, env): ...          # -> PlanResult(workflow=WorkflowDef(...))
    def tools(self, services): ...                    # tên "mydomain.*"
    def operations(self): return {"build": self.op_build}
    def validators(self): return {"valid": self.gate_valid}
    def idempotent_operations(self): return {"build"}

def create_plugin():
    return MyPlugin()
```

### Quy tắc core bắt buộc (`PluginManager.load`)

- Manifest được validate bằng pydantic; id khớp `^[a-z][a-z0-9_]{1,30}$`; id trùng với plugin đang active
  bị từ chối.
- Tool phải có tiền tố `<id>.`; quyền của tool phải nằm trong `manifest.permissions ∩ PLUGIN_PERMISSIONS`.
  Khi chạy, quyền thực tế còn bị giới hạn bởi `TOOL_PERMISSIONS` (và `step.permissions` nếu step thu hẹp).
- Tên operation không được chứa dấu `.`.
- Lỗi import/load, classify hay plan chỉ ảnh hưởng plugin đó; lỗi hiện trong `/plugins` và
  `GET /api/v1/plugins`. Plugin lỗi được ghi capability `plugin:<name>` ở trạng thái `unavailable`.
- Tool mà plugin chỉ cung cấp sau khi khởi động (ví dụ key provider được đặt sau qua `/keys`) được đăng ký
  thêm bằng `refresh_tools`, vẫn qua cùng kiểm tra tiền tố và quyền.
- Mọi plan đều được validate (đồ thị, executor, capability, constraint) trước khi chạy. Validator không chạy
  được trả `unknown`/`skipped`; chỉ `passed` mới tính là qua, nên plugin không thể đánh dấu task thành
  công khi gate fail.

### Đăng ký và bật

1. Thêm module vào `PLUGINS` (ví dụ `PLUGINS=software,data,mypackage.my_plugin`).
2. Khi có cấu hình YAML (`CONFIG_DIR`, mặc định `config`), module phải nằm trong
   `policies.trusted_plugin_modules` (mặc định chỉ 6 plugin có sẵn). Module ngoài danh sách bị ghi lỗi
   `module not in policies.trusted_plugin_modules` và không được tải.
3. Nếu tool cần quyền mới, thêm quyền đó vào `PLUGIN_PERMISSIONS` và `TOOL_PERMISSIONS`. `mcp:execute`
   không được cấp mặc định.
4. Option riêng: `PLUGIN_CONFIG='{"mydomain": {...}}'`, đọc bằng `ctx.plugin_config()` hoặc trong `env`.

### Test

Unit-test `classify` / `plan` / validators trực tiếp; chạy workflow thật qua `tests.conftest.build_orch` +
`TaskRequest`. Tham khảo `tests/test_platform.py`, `tests/test_game_research.py` và
`tests/fixtures/slow_plugin.py` (plugin tối thiểu dùng cho test recovery). Chạy: `uv run pytest`.

## 5. Kết quả chung (result envelope)

Mỗi task có `ResultEnvelope` (`GET /api/v1/tasks/{id}/result`): task id, domain, operation, status (gồm
`partial`), summary, artifacts, validations (`GateResult`), evidence theo step (executor, state, attempts,
duration, error), failed/skipped steps, limitations, routing, next actions.

## 6. Game research và lớp domain game_intel

### Plugin `game_research`

- `research` (`WORKSPACE_ROOT` trỏ vào game): `inventory` (tool, phát hiện engine và `source`/`build`,
  ghi mọi file trong phạm vi với size + sha256 → `evidence_manifest.json`) → `evidence` (tool, kiểm lại file
  theo manifest, so với manifest cũ nếu truyền trong `artifact_ids`, trích fact số, ghi conflict record
  giữ mọi claim) → `coverage` (operation, gộp capture playtest dạng JSON thành bản đồ theo level; ô chưa
  thấy giữ `?`, ô mâu thuẫn thành `!`) → `runtime_plan` (operation, playtest **automated** khi có tool khớp
  `control_tools` của adapter, ngược lại **assisted**).
- `design`: lưu design mới thành file Markdown có version trong `DATA_DIR/creative/<game>/` (hoặc
  `PLUGIN_CONFIG.game_research.creative_root`); artifact được trích dẫn được kiểm checksum. Không bao giờ
  ghi vào thư mục game hay vào kho artifact/attachment.
- Adapter có sẵn: `fixture`, `unity`. Thêm engine không cần sửa code: thêm adapter vào
  `PLUGIN_CONFIG.game_research.adapters` (`name`, `source_markers`, `build_markers`, `skip_dirs`,
  `control_tools`). Mọi adapter phải qua `tests/test_game_adapter_contract.py`.
- Giới hạn: tối đa 5000 file mỗi lần kiểm kê (phần dư đếm vào `over_limit`); luôn bỏ qua `.git`,
  `node_modules`, `.agw-children` và symlink; fact chỉ khoá theo tên nên cùng tên ở nhiều level có thể
  hiện thành conflict; screenshot chưa được đọc, chỉ được liệt kê là chưa chép lại.
- Task thuộc domain trong `RETENTION_EXEMPT_DOMAINS` (mặc định `["game_research"]`) cùng sub-task và
  artifact của chúng không bị dọn theo retention (xem [SECURITY.md](SECURITY.md)).

### `app/plugins/game_intel/` (lớp domain)

Không phải plugin riêng: là model và kho dữ liệu dùng `Store` và `ArtifactStore` của gateway, không có
database thứ hai. Hiện chỉ được dùng trong `tests/test_game_intel.py`, chưa được nối vào workflow của
`game_research`.

| Thành phần | Nội dung |
|---|---|
| `models.py` | Entity schema version 1: `GameProject`, `GameBuild`, `EvidenceArtifact`, `Observation`, `ClaimEvidenceLink`, `Claim`, `ValidationResult`. Observation là điều thấy trực tiếp (phải cite evidence); Claim là diễn giải có trạng thái |
| `claim_transition` | `hypothesis → supported` (≥1 link ủng hộ, không có phản bác); `supported → verified` (ủng hộ từ ≥2 method khác nhau, không phản bác); bất kỳ → `contradicted` (≥1 phản bác); bất kỳ → `unresolved` (có cả hai); `contradicted`/`unresolved` → `hypothesis` (mở lại). Không có xác suất, `strength` chỉ đếm |
| `repository.py` | `ProjectRegistry` (project/build, root phải nằm trong các thư mục được duyệt, symlink được resolve) và `EvidenceRepository` (evidence bất biến, idempotent theo sha256 + type + method trong cùng run; sửa = version mới `derived_from` bản cũ; đọc luôn kiểm sha256) |

Record kinds trong store: `game_project`, `game_build`, `game_evidence`, `game_observation`,
`game_claim`, `game_claim_link`, `game_validation`.
