# Local HA Agent — Iteration 4 Design Stub (Frontend rework)

**Date:** 2026-09-02
**Status:** Idea / stub — not brainstormed or planned yet
**Builds on:** iteration 3 (Assist, streaming, UI — the vanilla-JS chat page)

> Captured so it isn't lost. This is a placeholder, not an approved design.
> Run `superpowers:brainstorming` before turning it into a real spec.

## Motivation

Iteration 3's chat UI is deliberately vanilla JS with no build step (simplicity,
offline-safe). It works but looks poor and is awkward to extend. Iteration 4
reworks the frontend into a proper **TypeScript + small UI framework** app while
keeping every App/ingress constraint intact.

## The constraint that shapes everything

The UI runs inside HA's **ingress iframe** under a dynamic path prefix, and the
App must work **offline** (no CDN). A build step is fine as long as:

1. **Relative asset base** — Vite `base: './'` so emitted asset URLs resolve
   under the ingress prefix (never `/assets/...`). API calls stay relative
   (`api/chat`, `api/chat/stream`), as today.
2. **Everything bundled** — no runtime CDN `<script>`/`<link>`; the bundler
   vendors all deps into `frontend/dist/`. Runtime stays static files.
3. **Build at image-build time, not runtime** — multi-stage Dockerfile: a Node
   stage builds → copy `dist/` into the Python image. Final image is Node-free.
4. FastAPI serves `frontend/dist/` instead of `frontend/`.

## Options (decide during brainstorming)

- **Vite + TS + Svelte** — compiles away, tiny runtime, good DX. Default pick.
- **Vite + TS + Preact** — ~4 KB, JSX if preferred.
- **Lit + TS** — the stack HA's own frontend uses; most "native" feel, standards
  based, slightly more verbose.

## Nice-to-haves

- Style against HA CSS custom properties (`--primary-color`,
  `--card-background-color`, …) so the panel matches the user's active HA theme.
- Reuse the iteration-3 **SSE event protocol** unchanged (token / tool_call /
  tool_result / done / error) — this is a presentation swap, not a protocol change.
- Keep the standalone (non-App) deployment working — the built assets are served
  the same way regardless of packaging.

## Explicit tradeoff to accept

Relaxes iteration 3's "no build step" rule: adds a Node toolchain at build time
(CI + Dockerfile). Runtime cost is zero. Worth it for maintainability + looks.

## Out of scope (for now)

- Backend/API changes — the SSE + `/api/chat` contracts stay as iteration 3.
- New agent capabilities or tools.
