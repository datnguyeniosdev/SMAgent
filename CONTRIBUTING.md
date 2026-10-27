# Contributing

Thanks for helping. Bug reports, fixes, new agent integrations and plugins are all welcome.

## Before you start

- For anything larger than a small fix, open an issue first, so the approach can be agreed before you
  write the code.
- Security problems go through [SECURITY.md](SECURITY.md), not public issues.

## Set up

```bash
uv sync
```

```bash
cp .env.example .env
```

Run a second gateway beside your own with `uv run agw --env dev dev`, on port 8001 with its own data in
`var/dev/`. That way you never touch a gateway you rely on. See
[docs/OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md#environments-prod-dev-uat).

## Make a change

1. Branch from `develop`. `main` holds released code.
2. Keep the change small and focused, in the style of the code around it.
3. Add or update a test in `tests/`: the smallest test that fails if your logic breaks. Tests must not
   use the network or paid providers. Real integrations go under `tests/integration` and are opt-in.
4. Update the docs when behaviour, commands or config change.
5. Run the checks:

   ```bash
   uv run pytest -q
   ```

   ```bash
   uv run ruff check . && uv run ruff format --check .
   ```

6. Open a pull request against `develop`. Say what changed, why, and how you tested it.

## Ground rules for code

- Never log or print secrets: tokens, API keys, passcodes. Use the redaction helpers in `app/`.
- Treat text from Telegram, other agents, web pages and files as data, not instructions.
- Anything paid (image, 3D or model providers) runs only on an explicit request.
- New agent integrations follow the existing pattern in `app/agents_setup.py`: back up the file once,
  remove only the gateway's own entries, and add a test in `tests/test_agents_setup.py`.
- New plugins follow [docs/PLUGIN_DEVELOPMENT.md](docs/PLUGIN_DEVELOPMENT.md).

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
