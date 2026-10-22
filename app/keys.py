"""Provider keys and models set remotely (Telegram /keys): only the variables the provider config names
(api_key_env, model_env). They are kept in the gateway database, encrypted with AGW_SECRET_KEY from .env
(Fernet; `agw setup` generates it), and put into the process environment at start and after each change,
where the providers read them. A value saved here wins over .env; clearing it falls back to .env.
Values are never logged or shown back: status shows only the last 4 characters."""

from __future__ import annotations

import os
import re

from cryptography.fernet import Fernet, InvalidToken

KIND = "setting"
SECRET_ENV = "AGW_SECRET_KEY"  # noqa: S105 - the variable name, not a secret
# one line of printable ASCII without spaces, quotes or backslashes
_VALUE = re.compile(r"[!#-&(-\[\]-~]{1,500}")


def names(cfg) -> list[tuple[str, bool]]:
    """(variable, secret?) for every provider in the config, keys before models."""
    out: dict[str, bool] = {}
    for p in cfg.providers.values() if cfg else []:
        if p.api_key_env:
            out[p.api_key_env] = True
        if p.model_env:
            out.setdefault(p.model_env, False)
    return sorted(out.items(), key=lambda kv: (not kv[1], kv[0]))


def mask(value: str | None, secret: bool) -> str:
    if not value:
        return "missing"
    if not secret:
        return value
    return f"set (…{value[-4:]})" if len(value) > 8 else "set"


def check(value: str) -> str:
    value = value.strip()
    if not _VALUE.fullmatch(value):
        raise ValueError("one line, no spaces, quotes or backslashes, at most 500 characters")
    return value


def new_secret() -> str:
    return Fernet.generate_key().decode()


def _fernet() -> Fernet:
    key = os.environ.get(SECRET_ENV)
    if not key:
        raise ValueError(
            f"{SECRET_ENV} is not set: run `uv run agw setup` (it adds one) and restart"
        )
    try:
        return Fernet(key.encode())
    except ValueError as e:
        raise ValueError(
            f"{SECRET_ENV} is not a valid key (44 characters, from `agw setup`)"
        ) from e


def save(store, name: str, value: str) -> None:
    store.put(
        KIND, name, {"name": name, "enc": _fernet().encrypt(value.encode()).decode()}, state="set"
    )
    os.environ[name] = value


def clear(store, name: str, env_value: str | None) -> None:
    """Forget the saved value (the row keeps no secret); the .env value, if any, applies again."""
    store.put(KIND, name, {"name": name, "enc": None}, state="cleared")
    if env_value:
        os.environ[name] = env_value
    else:
        os.environ.pop(name, None)


def saved(store) -> set[str]:
    return {r["name"] for r in store.list(KIND, states=["set"])}


def load(store) -> list[str]:
    """Put every saved value into the environment. Returns the names that could not be decrypted
    (AGW_SECRET_KEY missing or changed); those keep their .env value."""
    rows = store.list(KIND, states=["set"])
    if not rows:
        return []
    try:
        f = _fernet()
    except ValueError:
        return [r["name"] for r in rows]
    bad = []
    for r in rows:
        try:
            os.environ[r["name"]] = f.decrypt(r["enc"].encode()).decode()
        except (InvalidToken, AttributeError):
            bad.append(r["name"])
    return bad
