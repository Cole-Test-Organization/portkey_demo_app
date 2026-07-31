# Security review

Review date: 2026-07-31

## Scope

The review covered every tracked source and deployment file, browser rendering,
HTTP request parsing, static-file resolution, gateway proxying, onboarding state
writes, secret handling, Git publication contents, and the systemd sandbox.

## Result

No known critical, high, or medium-severity vulnerability remains within the
documented local/private-network deployment model.

Controls verified during the review include:

- The service key remains server-side and is absent from public configuration.
- Browser-controlled and gateway-controlled values are rendered as text, not
  injected as HTML.
- Request bodies, prompts, and upstream responses have explicit size limits.
- Static paths are resolved beneath the asset directory.
- Onboarding validates identifiers, rejects control characters, compares an
  existing key in constant time, and refuses symlink state targets.
- Security headers deny framing, cross-origin resource reuse, and broad browser
  capabilities.
- Fresh local runs bind to loopback.
- The production unit runs as `www-data` with a restricted capability set,
  read-only system access, namespacing restrictions, and a syscall allowlist.
- Tracked files contain no recognized API-key or private-key signatures.

## Accepted residual risks

- There is intentionally no application authentication, authorization, quota,
  or rate limiting. Network reachability is the trust boundary.
- Anyone on that trusted network who can reach the service can consume gateway
  quota and can submit prompts to the configured external provider.
- Prompt and response data is processed by the configured gateway/provider and
  is subject to that service's data-handling policy.
- The Python standard-library server is suitable for this small private demo,
  not direct public-internet exposure.

Reassess these assumptions before making the service internet-accessible or
using it for sensitive production data.
