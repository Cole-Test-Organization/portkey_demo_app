# AI Role Demo

A small, dependency-free web app that sends one prompt through an AI gateway.
The selected role is added as request metadata, and the gateway chooses the
model defined for that role.

## How it works

1. The browser sends `{ role, prompt }` to `POST /api/chat`.
2. `server.py` adds the server-side gateway key and `user_role` metadata.
3. The gateway's saved conditional config selects the model.
4. The response and actual model name are shown in the browser.

The top-right role switcher also previews a bright Sales, warm HR, or technical
Devs visual theme.

The API key never goes into frontend code.

This project intentionally has no application-level login. Run it only on a
trusted machine or private network; do not expose it directly to the public
internet without adding authentication and rate limiting.

## Repository layout

| Path | Purpose |
| --- | --- |
| `app/` | Plain HTML, CSS, and JavaScript |
| `server.py` | Static server, API proxy, and onboarding API |
| `configure` | Command-line onboarding fallback |
| `.env` | Local secrets and role settings; ignored by Git |
| `.env.example` | Safe configuration template |
| `portkey-role-routing-config.example.json` | Default routing template |
| `portkey-model-console.service` | systemd unit |
| `tests/` | Python unit and HTTP tests |
| `scripts/check` | Complete local validation and tracked-secret check |

Local development writes the generated routing file inside the repository as
`portkey-role-routing-config.json`; that file is ignored by Git. The included
systemd service instead uses `/root/portkey-role-routing-config.json`.

## Onboarding

Open the site and select **Setup**.

1. Enter the service API key and LLM profile slug.
2. Name the three roles.
3. Enter a Google model for each role, or keep the defaults.
4. Select **Save setup**.
5. Copy the generated JSON into the saved gateway config attached to the
   service key.

Saving writes `.env` and `/root/portkey-role-routing-config.json`. Existing
installations require the current service key before setup can be changed.

CLI fallback:

```bash
sudo ./configure
```

## Local development

```bash
cp .env.example .env
python3 server.py
```

Edit `.env`, then open `http://127.0.0.1:8080`. The development server binds to
loopback by default. Set `HOST` explicitly only when you intend to make it
reachable from another machine.

Run the tests:

```bash
./scripts/check
```

The check runs the Python test suite, syntax validation, JSON validation,
whitespace checks, and a scan for common committed credential formats.

## Deployment

The production service runs from `/root/portkey-model-console`:

```bash
systemctl restart portkey-model-console
systemctl status portkey-model-console
```

`.env` is excluded by `.gitignore`; `.env.example` is the commit-safe template.
The generated routing JSON, private-key formats, caches, and build output are
also excluded. Before publishing, confirm `git status --ignored` shows `.env`
as ignored and run `./scripts/check`.
