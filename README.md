# AI Role Demo

A small, dependency-free web app that sends one prompt through an AI gateway.
The selected role is added as request metadata, and the gateway chooses the
model defined for that role.

## How it works

1. The browser sends `{ role, prompt }` to `POST /api/chat`.
2. `server.py` adds the server-side gateway key and `user_role` metadata.
3. The gateway's saved conditional config selects the model.
4. The response and actual model name are shown in the browser.

This project intentionally has no application-level login. It is built to run
on your own machine, bound to loopback, as a single-user demo. Do not expose it
directly to a network without adding authentication and rate limiting.

## Repository layout

| Path | Purpose |
| --- | --- |
| `app/` | Plain HTML, CSS, and JavaScript |
| `server.py` | Static server, API proxy, and onboarding API |
| `.env` | Local secrets and role settings; ignored by Git |
| `.env.example` | Safe configuration template |
| `portkey-role-routing-config.json` | Routing config written by onboarding; ignored by Git |

## Running it

No install step and no privileges are required:

```bash
python3 server.py
```

Then open `http://127.0.0.1:8080`. The server binds to loopback and listens on
port 8080 by default; set `HOST` explicitly only when you intend to make it
reachable from another machine.

## Onboarding

There is nothing to configure before the first run. The app detects that setup
is incomplete, reports what is missing, and opens **Setup** for you.

1. Enter the Portkey Service API key and LLM profile slug.
2. Name the three roles (or use the defaults)
3. Enter a models for each role, or keep the defaults.
4. Select **Save setup**. The dialog closes once the save succeeds.

Saving creates `.env` and `portkey-role-routing-config.json` next to
`server.py`, both owned by you with `0600` permissions, and both ignored by
Git

