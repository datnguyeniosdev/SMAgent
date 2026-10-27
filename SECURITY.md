# Security policy

## Reporting a vulnerability

Please do not open a public issue. Report it privately through
[GitHub security advisories](https://github.com/datnguyeniosdev/SMAgent/security/advisories/new).
Include:

- the affected version or commit
- the steps to reproduce
- the impact

You should get an answer within 7 days. Once the fix is released, you will be credited, unless you
prefer not to be.

## Supported versions

The project is alpha, so only the latest release on `main` gets fixes.

## Scope

In scope:
- the gateway's REST API and `/mcp` server
- Telegram access control (the allowlist, passcode confirmations, key entry)
- secret handling
- the setup commands that change agent configs

Out of scope:
- the limits documented in [docs/SECURITY.md](docs/SECURITY.md#known-limitations), for example agent
  runtimes that run as your user without a sandbox

How the gateway is secured is described in [docs/SECURITY.md](docs/SECURITY.md) and
[docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).
