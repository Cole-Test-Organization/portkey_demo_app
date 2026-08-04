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

1. Enter the service API key and LLM profile slug.
2. Name the three roles.
3. Enter a Google model for each role, or keep the defaults.
4. Select **Save setup**. The dialog closes once the save succeeds.

Saving creates `.env` and `portkey-role-routing-config.json` next to
`server.py`, both owned by you with `0600` permissions, and both ignored by
Git. The running server picks the change up immediately — no restart.

## Key verification

Before writing anything, the server checks the key with `GET /models` on the
gateway:

| Gateway response | Result |
| --- | --- |
| Success | Saved and marked verified |
| `401` / `403` | Rejected — nothing is written |
| Anything else, or unreachable | Saved, flagged **Key not verified** |

Only an explicit 401/403 blocks a save, so an outage or a firewall never stops
you configuring the app; the status pill reports that the key went unchecked.
A key loaded from `.env` is re-checked in the background at startup, and a key
that has since been revoked shows as **Key rejected**.

Authorization is the key working, not the key matching what is already stored.
That means a rotated key can be entered directly — the earlier rule required
the old key to change the old key, so rotation was impossible from the GUI.

## Gateway routing

Each request carries `user_role` metadata, and the routing config sends it to a
target named after the role:

| Role name | Role ID | Target |
| --- | --- | --- |
| Sales | `sales` | `sales_route` |
| People Ops | `people-ops` | `people-ops_route` |

The role ID is the role name lowercased with non-alphanumerics collapsed to
hyphens. Because the naming is derived rather than positional, the gateway
config stays valid when roles are renamed or reordered — set it up once against
the `<role>_route` convention. `portkey-role-routing-config.json` is written on
every save as a reference copy of what the routing should look like.

`.env` is read at startup, but the service API key is deliberately never placed
in the process environment, so it does not appear in `/proc/<pid>/environ` or
get inherited by child processes.

To configure without the GUI, copy `.env.example` to `.env` and edit it by
hand; the values are equivalent.

## Before publishing

`.env` is excluded by `.gitignore`; `.env.example` is the commit-safe template.
The generated routing JSON, private-key formats, caches, and build output are
also excluded. Confirm `git status --ignored` shows `.env` as ignored.
