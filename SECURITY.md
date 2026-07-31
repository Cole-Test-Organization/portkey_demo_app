# Security

## Intended deployment

AI Role Demo is a small local or private-network demonstration. It does not
implement user authentication, authorization, quotas, or rate limiting. Anyone
who can reach the service can submit prompts that consume the configured
gateway account. Do not expose it directly to the public internet without
adding those controls.

The development server binds to `127.0.0.1` by default. The included systemd
unit binds to all interfaces for use behind a trusted private reverse proxy.

## Secrets

- Keep the service API key only in `.env` or an external secret manager.
- Never put a real key in `.env.example`, frontend files, routing JSON, logs,
  issues, screenshots, or commits.
- `.env`, generated routing state, and common private-key formats are ignored.
- Run `./scripts/check` before every push.
- If a key is committed or shared accidentally, revoke it at the provider
  immediately. Removing it in a later commit does not remove it from history.

## Reporting a vulnerability

If the GitHub repository has private vulnerability reporting enabled, use a
private security advisory. Otherwise contact the repository owner privately;
do not publish credentials or exploit details in a public issue.
