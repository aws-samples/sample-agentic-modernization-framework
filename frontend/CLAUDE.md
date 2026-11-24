# CLAUDE.md — frontend/

React + TypeScript + Vite SPA using the CloudScape Design System. See the root `CLAUDE.md` for the
overall architecture. Run `pnpm exec tsc --noEmit` and `pnpm test` (vitest) before finishing frontend work. pnpm only; `pnpm-lock.yaml` is the lockfile.

## Layout

- `src/api/` — one module per backend resource; all calls go through `api/client.ts`
  (`apiClient`, `API_BASE` = `VITE_API_BASE_URL` or `/api/v1`, Cognito bearer + dev-auth). Build URLs from
  `API_BASE`, never a literal `/api/v1`. The Vite dev server proxies `/api` → `VITE_API_PROXY_TARGET`
  (default `http://localhost:8000`), so the SPA runs locally against the cloud.
- `src/pages/` — screens: `WorkflowPage` (the pipeline + per-phase review + edit modal), `DiscoveryPage`
  (`/inventory`, "L1 Discovery"), `WavePlanningPage`, `GovernancePage` (`/governance`, run + approval-gate audit),
  `ChatPage`, `DashboardPage`, `ProgramPage`, `SettingsPage` (Agent Skills + Infrastructure tabs), `HowWeDoPage`.
- `src/workflow/` — `status.ts` (`statusLabel` = the single status authority; `phaseStatusLabel`), `phases.ts`
  (canonical `PHASES` / `getPhaseIndex` / `phaseOf`), `gateGaps.ts` (fill completeness gaps at a gate),
  `strategyOptions.ts`, `readiness.ts`, `requiredInputs.ts`, `validators.ts`, `screenModel.ts`,
  `transformCatalog.ts`, `types.ts`; tests in `__tests__/`.
- `src/components/workflow/` — shared phase-screen pieces (`RiskScorePanel` is used; `PhaseScreen` /
  `ScreenTemplate` / `ApprovalWrapper` / `ExecutionDetailsPanel` are an unwired refactor covered only by tests).
- `src/auth/` — `cognito.ts` (Hosted UI + PKCE), `devAuth.ts` (`VITE_DEV_AUTH` bypass), `AuthContext`, `ProtectedRoute`.
- `src/CapabilitiesContext.tsx` — `useCapabilities()` from `GET /system/capabilities` (environment, graph_backend,
  wave_planning_enabled, aws_region, agentcore_harness_id, bedrock_model_id, sfn_state_machine_name,
  supported_target_regions, validation_poll_ms, validation_poll_max, workflow_poll_ms). Defaults hide Wave Planning
  until the fetch returns; `aws_region` defaults to `''` (console links render nothing until it is known).
- `src/lib/aws.ts` — `consoleUrl(region, path)` (the only place the console domain lives), `targetRegionOptions(caps)`
  (`supported_target_regions`, else `[aws_region]`), poll fallbacks. `src/lib/mermaid.ts` — lazy `import('mermaid')`.
- `App.tsx` lazy-loads every page (`React.lazy` + `Suspense`); keep new pages lazy and keep mermaid out of static imports.
- `src/demo.ts` — `DEMO_READ_ONLY` (`VITE_DEMO_READ_ONLY=true`): data stays live, mutating actions are disabled.

## Workflow phase model (WorkflowPage.tsx)

- `PHASES` (7): **Discovery · Assessment · Design · Execution · Validation · Cutover · Handover**.
  Execution's sub-stages (DB Migration / Infra / CI-CD) are shown *inside* the Execution screen, not as columns.
- `PHASES` / `getPhaseIndex(phase)` (ASL `current_phase` → column) / `phaseOf(step)` live only in
  `workflow/phases.ts`; WorkflowPage imports them.
- `STEP_LABELS` maps the ASL approval steps (`discovery_review` … `handover_review`) → human labels. Only
  those seven gates exist; don't add branches for other step names.
- Close-out strategies (`retire`/`retain`/`repurchase`) skip Assessment→Cutover: `skippedPhases` renders
  those as "Skipped" (mirrors the ASL `close_out` branch).
- `statusLabel` shows "Awaiting Approval" only when `current_step` contains `review`/`approval`; otherwise
  "Running"/"Done"/"Failed"/"Cancelled" (`phaseStatusLabel` adds "Not Started"/"Skipped"). Don't invent others.
- The approval modal renders the gate's `decision_summary` (changes, risk, warnings, consequences).

## Rules

- **No hardcoded environment values.** Region, harness id, model and state-machine name come from
  `useCapabilities()`; resource names for the Settings → Infrastructure tab from `GET /infrastructure/`
  (`api/infrastructure.ts`). Never bake account ids, harness ids or regions into code.
- Every backend call must map to a real route — after changing `api/*.ts`, verify against the backend
  routers (a call to a removed endpoint like the old `/design/generate` is a bug).
- `deployment_mode` is always `agentcore`. Legitimate `ecs`/`fargate` values in domain data (target
  compute, 7R) stay; they are not the old deployment mode.
