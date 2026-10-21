# Media & Images Plugin (`media`)

`app/plugins/media.py` · permissions: `data:read, artifact:write, provider:image`.

| Operation | Needs | Behaviour | Verified |
|---|---|---|---|
| `inspect` | `media.inspect` | Read each image (PNG/JPEG/WebP/GIF headers): format, width, height, alpha, colour mode, bytes, SHA-256. Then the `media.image_valid` gate and a JSON report | **real (demo P5-D1)** |
| `generate` | capability `image.generate` + tool `media.generate` | approval (paid external call) → provider generates → bytes must be a recognised image → stored artifact → gate checks the requested size/alpha | mocked provider in tests; real: **NOT CONFIGURED** (demo P5-D2: `missing capabilities ['image.generate', 'tool:media.generate']`) |

- `media.generate` is **only registered when a provider is configured**: `OPENAI_API_KEY` + `OPENAI_IMAGE_MODEL`, adapter `POST /v1/images/generations` with `b64_json`, `background=transparent`, `output_format=png`.
- The provider must pass a free `GET /models` check to become routable.
- Nothing is fabricated: no placeholder PNGs, no URLs, no assumed dimensions. A size or alpha mismatch is reported by the gate, not hidden.

**Setup to enable generation.** Set `OPENAI_API_KEY` and `OPENAI_IMAGE_MODEL` (a GPT Image model your account can use), restart, and check `/capabilities` shows `image_provider:openai_images` as validated.

## fal.ai design provider

`app/connectors/fal.py` implements image generation over the documented fal queue API:
- **Submit:** `POST https://queue.fal.run/{model}` with `Authorization: Key $FAL_KEY`.
- **Wait:** poll the returned `status_url` until `COMPLETED`.
- **Fetch:** `GET response_url`, then download `images[0].url`.

It's enabled when `FAL_KEY` is set. `FAL_MODEL` is optional and defaults to `fal-ai/flux/schnell`. Extra model input goes in the provider's `options` in `config/providers/models.yaml`.

**Safety:**
- The key is sent only to `queue.fal.run`.
- Images are downloaded only over https from `fal.media`/`fal.run`/`fal.ai` hosts.
- Downloads must be real images (signature check), and data-URI results are refused.
- Timeouts and cancellation send `PUT cancel_url`.
- Exact size and transparency are not assumed. FLUX produces no alpha channel, so `transparent: true` fails the `media.image_valid` gate instead of being faked.

**Use it** with `task_type: design_image` (workflow `design.image` v1).
- Input: `{"prompt": ..., "width": 512, "height": 512, "transparent": false}`.
- Approval is required (`tool:media.generate` policy), because generation is paid.
- The image is validated and attached to the end-of-workflow Telegram message.

```bash
curl -s -H "Authorization: Bearer $API_TOKEN" -H 'content-type: application/json' -d '{"objective":"coin icon","task_type":"design_image","data":{"prompt":"flat gold coin icon","width":512,"height":512,"transparent":false}}' http://127.0.0.1:8000/api/v1/tasks
```

Status: **MOCKED** (queue flow, errors, untrusted hosts, cancel, and the full workflow are tested against a mocked fal API). A real run is waiting for `FAL_KEY`. Verify with `RUN_INTEGRATION=1 RUN_PAID=1 uv run pytest tests/integration -k fal`.
