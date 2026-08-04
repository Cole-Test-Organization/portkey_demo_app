# Repository instructions

- Keep the app dependency-free at runtime: Python standard library plus plain
  HTML, CSS, and JavaScript.
- Never read, print, stage, or commit `.env` or live credential values.
- Preserve the safe local default of `HOST=127.0.0.1`.
- Treat the no-auth design as suitable only for trusted local/private networks.
- Render all gateway-controlled text with `textContent`, never HTML injection.
- Keep the repo minimal: no test suite, CI, or helper scripts.
