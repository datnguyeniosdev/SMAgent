"""Model providers shared by every project, and each project's model plan (Telegram /providers, /plan).

1. Providers: config/providers/models.yaml, plus OpenAI-compatible ones added from Telegram presets
   (kept in the database, merged into the config at start and on reload). Keys go through /keys.
2. Models: each provider has its default model plus any models added to it; a step can run on any of
   them (executor "provider:<id>@<model>").
3. Plans: per project and workflow role (designer, implementer, ...), an ordered list of executors
   tried before the role's defaults: "provider:openrouter@qwen/qwen3-coder", "provider:gemini",
   "runtime:claude_code". A project without a plan behaves as before.
"""

from __future__ import annotations

from app.gateway_config import ProviderDef

PRESETS: dict[str, dict] = {  # OpenAI-compatible endpoints; local ones take any non-empty key
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1"},
    "groq": {"label": "Groq", "base_url": "https://api.groq.com/openai/v1"},
    "mistral": {"label": "Mistral", "base_url": "https://api.mistral.ai/v1"},
    "xai": {"label": "xAI (Grok)", "base_url": "https://api.x.ai/v1"},
    "together": {"label": "Together", "base_url": "https://api.together.xyz/v1"},
    "fireworks": {"label": "Fireworks", "base_url": "https://api.fireworks.ai/inference/v1"},
    "dashscope": {"label": "Qwen (DashScope)", "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
    "moonshot": {"label": "Moonshot (Kimi)", "base_url": "https://api.moonshot.ai/v1"},
    "zhipu": {"label": "Zhipu GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    "zai": {"label": "Z.AI GLM (international)", "base_url": "https://api.z.ai/api/paas/v4"},
    "ollama": {"label": "Ollama (this Mac)", "base_url": "http://127.0.0.1:11434/v1"},
    "lmstudio": {"label": "LM Studio (this Mac)", "base_url": "http://127.0.0.1:1234/v1"},
}  # fmt: skip

KIND_PROVIDER, KIND_MODELS, KIND_PLAN = "provider_def", "provider_models", "model_plan"


# ---------- 1. providers ----------


def preset_def(pid: str) -> ProviderDef:
    p = PRESETS[pid]
    return ProviderDef(
        id=pid,
        type="openai_compatible",
        base_url=p["base_url"],
        api_key_env=f"{pid.upper()}_API_KEY",
        model_env=f"{pid.upper()}_MODEL",
        capabilities=["model.text", "model.structured"],
        json_mode="json_object",  # widely supported; json_schema is not
    )


def added(store) -> list[ProviderDef]:
    return [ProviderDef(**r["def"]) for r in store.list(KIND_PROVIDER, states=["on"])]


def add_provider(store, cfg, pid: str) -> ProviderDef:
    if pid not in PRESETS:
        raise ValueError(f"unknown preset {pid!r}")
    if pid in cfg.providers:
        raise ValueError(f"{pid} is already a provider")
    d = preset_def(pid)
    store.put(KIND_PROVIDER, pid, {"def": d.model_dump()}, state="on")
    cfg.providers[pid] = d
    return d


def remove_provider(store, cfg, pid: str) -> None:
    if store.get(KIND_PROVIDER, pid) is None:
        raise ValueError(
            f"{pid} comes from config/providers/models.yaml; edit that file to remove it"
        )
    store.put(KIND_PROVIDER, pid, {"def": None}, state="removed")
    cfg.providers.pop(pid, None)


def merge(store, cfg) -> None:
    """Providers added from Telegram join the config (a YAML provider with the same id wins)."""
    for d in added(store):
        cfg.providers.setdefault(d.id, d)


# ---------- 2. models per provider ----------


def models(store, pid: str) -> list[str]:
    rec = store.get(KIND_MODELS, pid)
    return list(rec["models"]) if rec else []


def add_model(store, pid: str, model: str) -> None:
    have = models(store, pid)
    if model not in have:
        store.put(KIND_MODELS, pid, {"models": [*have, model]})


def remove_model(store, pid: str, model: str) -> None:
    store.put(KIND_MODELS, pid, {"models": [m for m in models(store, pid) if m != model]})


async def remote_models(pdef: ProviderDef, key: str | None, client) -> list[str]:
    """The provider's own model list (GET /models, free); [] when it cannot be read."""
    if pdef.type != "openai_compatible" or not pdef.base_url:
        return []
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        r = await client.get(pdef.base_url.rstrip("/") + "/models", headers=headers, timeout=15)
        r.raise_for_status()
        return sorted(m["id"] for m in r.json().get("data", []) if m.get("id"))
    except Exception:  # noqa: BLE001 - any failure means "no list", the operator can still type an id
        return []


# ---------- 3. per-project plans ----------


def entry(pid: str, model: str | None = None) -> str:
    return f"provider:{pid}@{model}" if model else f"provider:{pid}"


def choices(store, cfg, runtimes: list[str]) -> list[str]:
    """Everything a plan can use: each provider (default model), each added model, the runtimes."""
    out = []
    for pid in cfg.providers:
        if cfg.providers[pid].type == "fal":
            continue
        out.append(entry(pid))
        out += [entry(pid, m) for m in models(store, pid)]
    return out + [f"runtime:{r}" for r in runtimes]


def plan(store, project: str) -> dict[str, list[str]]:
    rec = store.get(KIND_PLAN, project)
    return {k: list(v) for k, v in (rec or {}).get("roles", {}).items()}


def set_role(store, project: str, role: str, entries: list[str]) -> None:
    roles = plan(store, project)
    if entries:
        roles[role] = list(dict.fromkeys(entries))
    else:
        roles.pop(role, None)
    store.put(KIND_PLAN, project, {"project": project, "roles": roles})


def label(e: str) -> str:
    kind, _, ref = e.partition(":")
    pid, _, model = ref.partition("@")
    return (
        f"{pid} · {model}"
        if model
        else (f"{pid} (default model)" if kind == "provider" else f"{pid} (runtime)")
    )
