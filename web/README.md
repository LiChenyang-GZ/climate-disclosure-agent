# Web UI

Next.js front end for the Climate Disclosure Agent API (`specs/02-design.md` §11.1).

```
npm ci
npm run dev                      # http://localhost:3000, proxies /api/* to CDA_API_URL
```

`CDA_API_URL` defaults to `http://127.0.0.1:8000` (the compose `api` service). The demo user is the `cda_user` cookie, set by the switch in the top bar; the proxy turns it into `X-Demo-User` (D80).

Checks:

```
npm run lint
npm run typecheck
npx playwright install chromium   # once
npx playwright test               # builds, starts on :3100, API mocked in the browser (D81)
```

Smoke test against the real API (local only; creates a uniquely named Wattlebrook pack): start the API and `npx next start -p 3101` with `CDA_API_URL` set, then

```
SMOKE_URL=http://127.0.0.1:3101 npx playwright test -c playwright.smoke.config.ts
```

`smoke/new-check.spec.ts` also needs the worker (`python -m cda.runner.main`) on the same database. Run "fake model" with `CDA_FAKE_LLM=1` on the API and worker (no OpenAI calls, D83), and "rejected key" with the fake off and `OPENAI_API_KEY` set to an invalid value such as `sk-invalid-smoke-test`.
