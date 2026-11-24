// WEB-UI FR-UI-18: the owner's observability page in Light and Dark with VEC-OBS-3's data (its expected JSON),
// and the truncated and empty cases.
import type { Decorator, Meta, StoryObj } from '@storybook/react-vite';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { stubFetch, themed } from '../../ui/story';
import { createApi } from '../api';
import { ServicesContext } from '../context';
import { createObsApi, SUMMARY_PATH, type ObsSummary } from './api';
import { ObservabilityPage } from './ObservabilityPage';

const vec: ObsSummary = {
  days: 7,
  compile_runs: [
    {
      run_id: '10000000-0000-4000-8000-000000000001',
      lesson_id: '20000000-0000-4000-8000-000000000001',
      started_at: '2026-11-24T10:00:00+00:00',
      finished_at: null,
      spent_usd: 1.86,
      steps: [
        { step: 1, stage: 'draft', cost_usd: 0.62, duration_s: 95, model: 'claude-sonnet-5-5', trace_id: 'cf8a1cbe5cc23a546382dc30e5e2b16a' },
        { step: 2, stage: 'fact_check', cost_usd: 0.21, duration_s: 40, model: 'claude-sonnet-5-5', trace_id: '3a271ca9f9843c931881cdac01a154be' },
      ],
    },
  ],
  tutor: {
    count: 3,
    p50_ms: 4000,
    p95_ms: 9000,
    cost_usd_sum: 0.13,
    cost_usd_mean_per_settled_call: 0.0325,
    slowest: [
      { question_id: '30000000-0000-4000-8000-000000000003', ms: 9000, trace_id: 'e269fa025e791ad769c583de0d89160b' },
      { question_id: '30000000-0000-4000-8000-000000000002', ms: 4000, trace_id: '1685649cad2678802af0f99d36205543' },
      { question_id: '30000000-0000-4000-8000-000000000001', ms: 2000, trace_id: '92c3332accbc4bdf45924ae427a4c0cb' },
    ],
  },
  failures: {
    truncated: false,
    jobs: [
      { kind: 'ingest.import', error_code: 'ValueError', count: 1, system: true, trace_ids: ['f751ff316a35f92486a722815c09d412'] },
      { kind: 'pipeline.stage', error_code: 'exhausted', count: 1, system: false, trace_ids: ['85b524475f9f651f8b30426af5fbb218'] },
      { kind: 'search.embed', error_code: 'exhausted', count: 1, system: true, trace_ids: ['c049c37c22fa5a48cbefc90389647cef'] },
    ],
    tutor: [{ error_code: 'provider_unavailable', count: 1, trace_ids: ['8f76441a086e06da18ab6655ef8f50a1'] }],
  },
  ingest: {
    truncated: false,
    rows: [
      {
        material_sha256: 'aa11aa11aa11aa11aa11aa11aa11aa11aa11aa11aa11aa11aa11aa11aa11aa11',
        page_count: 594,
        import_s: 120,
        pages_per_min_import: 297.0,
        trace_id: 'c7c885850da1d6bbde90e0f9302cbf7f',
      },
    ],
  },
  // SRV-OBS FR-OBS-17: VEC-OBS-3's expected `lessons` view.
  lessons: {
    truncated: false,
    open_reserves: 1,
    rows: [
      {
        lesson_id: '20000000-0000-4000-8000-000000000001',
        title: 'Lesson one',
        course_id: '50000000-0000-4000-8000-000000000001',
        course_title: 'Course one',
        deleted: false,
        compile_usd: 1.86,
        runs: 1,
        last_compile_usd: 1.86,
        tutor_usd: 0,
        total_usd: 1.86,
        open_reserves: 1,
        chars: 2000,
        usd_per_1k_chars: 0.93,
        mlflow_url: 'https://localhost:8443/#/experiments/0/traces?q=20000000-0000-4000-8000-000000000001',
      },
      {
        lesson_id: '20000000-0000-4000-8000-000000000002',
        title: 'Lesson two',
        course_id: '50000000-0000-4000-8000-000000000001',
        course_title: 'Course one',
        deleted: false,
        compile_usd: 0,
        runs: 0,
        last_compile_usd: null,
        tutor_usd: 0.13,
        total_usd: 0.13,
        open_reserves: 0,
        chars: null,
        usd_per_1k_chars: null,
        mlflow_url: 'https://localhost:8443/#/experiments/0/traces?q=20000000-0000-4000-8000-000000000002',
      },
      {
        lesson_id: '20000000-0000-4000-8000-000000000003',
        title: 'Lesson three',
        course_id: '50000000-0000-4000-8000-000000000001',
        course_title: 'Course one',
        deleted: false,
        compile_usd: 0.15,
        runs: 2,
        last_compile_usd: 0.05,
        tutor_usd: 0,
        total_usd: 0.15,
        open_reserves: 0,
        chars: 0,
        usd_per_1k_chars: null,
        mlflow_url: 'https://localhost:8443/#/experiments/0/traces?q=20000000-0000-4000-8000-000000000003',
      },
      {
        // spend on a deleted lesson stays, so the totals equal the ledger (code review round 1)
        lesson_id: '20000000-0000-4000-8000-000000000007',
        title: null,
        course_id: null,
        course_title: null,
        deleted: true,
        compile_usd: 0.07,
        runs: 1,
        last_compile_usd: 0.07,
        tutor_usd: 0,
        total_usd: 0.07,
        open_reserves: 0,
        chars: null,
        usd_per_1k_chars: null,
        mlflow_url: 'https://localhost:8443/#/experiments/0/traces?q=20000000-0000-4000-8000-000000000007',
      },
    ],
    courses: [
      {
        course_id: '50000000-0000-4000-8000-000000000001',
        title: 'Course one',
        compile_usd: 2.01,
        tutor_usd: 0.13,
        total_usd: 2.14,
        chars: 2000,
        usd_per_1k_chars: 1.07,
      },
    ],
  },
  // WEB-UI FR-UI-33 (SRV-OBS FR-OBS-25, -32): one banner per alert and the stack row.
  alerts: [
    { code: 'daily_spend_high', subject: 'tutor', value: 2.5, limit: 2.4 },
    { code: 'dead_jobs', subject: 'pipeline.stage', value: 1, limit: 0 },
  ],
  stack: {
    mlflow: { ok: true, checked_at: '2026-11-25T12:00:00Z' },
    rotel: { ok: false, checked_at: '2026-11-25T11:30:00Z' },
  },
};

const truncated: ObsSummary = {
  ...vec,
  failures: { ...vec.failures, truncated: true },
  ingest: { ...vec.ingest, truncated: true },
  lessons: { ...vec.lessons, truncated: true },
};
const empty: ObsSummary = {
  days: 7,
  compile_runs: [],
  tutor: { count: 0, p50_ms: null, p95_ms: null, cost_usd_sum: 0, cost_usd_mean_per_settled_call: null, slowest: [] },
  failures: { truncated: false, jobs: [], tutor: [] },
  ingest: { truncated: false, rows: [] },
  lessons: { truncated: false, open_reserves: 0, rows: [], courses: [] },
  alerts: [],
  stack: {},
};

const withSummary =
  (body: ObsSummary): Decorator =>
  (Story) => {
    const deps = { fetch: stubFetch({ [SUMMARY_PATH]: body }), onUnauthorized: () => undefined };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const services = { api: createApi(deps), obs: createObsApi(deps), notify: () => undefined, navigateAway: () => undefined };
    return (
      <ServicesContext.Provider value={services}>
        <QueryClientProvider client={client}>
          <Story />
        </QueryClientProvider>
      </ServicesContext.Provider>
    );
  };

const meta = { title: 'App/ObservabilityPage', component: ObservabilityPage } satisfies Meta<typeof ObservabilityPage>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [withSummary(vec), themed('light')] };
export const Dark: Story = { decorators: [withSummary(vec), themed('dark')] };
export const Truncated: Story = { decorators: [withSummary(truncated), themed('light')] };
export const Empty: Story = { decorators: [withSummary(empty), themed('dark')] };
