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
};

const truncated: ObsSummary = { ...vec, failures: { ...vec.failures, truncated: true }, ingest: { ...vec.ingest, truncated: true } };
const empty: ObsSummary = {
  days: 7,
  compile_runs: [],
  tutor: { count: 0, p50_ms: null, p95_ms: null, cost_usd_sum: 0, cost_usd_mean_per_settled_call: null, slowest: [] },
  failures: { truncated: false, jobs: [], tutor: [] },
  ingest: { truncated: false, rows: [] },
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
