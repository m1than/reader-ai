// Stories of the planning part of /import (WEB-PLAN FR-IMPORT-4, -5, -8): synthetic bodies only.
import type { Meta, StoryObj } from '@storybook/react-vite';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ServicesContext } from '../../app/context';
import { themed } from '../../ui/story';
import type { PlanApi, Preview } from './api';
import { ImportPage } from './ImportPage';
import './plan.css';
import { PlanSection } from './PlanSection';

const preview = (fits: boolean, small = false): Preview => ({
  plan_sha256: 'c'.repeat(64),
  inputs: { material_id: 'synthetic-plan', total_weeks: 2, minutes_per_day: fits ? 60 : 10, rate: '2.64', start_date: '2026-01-01', today: '2026-01-01' },
  lessons: Array.from({ length: small ? 3 : 14 }, (_, i) => ({
    id: `p${2 * i + 1}-${2 * i + 2}`,
    pages: [2 * i + 1, 2 * i + 2] as [number, number],
    minutes: 11,
    fixed: false,
    title: `Synthetic chapter ${Math.floor(i / 3) + 1}`,
  })),
  summary: {
    lesson_count: small ? 3 : 14,
    fixed_count: 0,
    new_count: small ? 3 : 14,
    study_days: 14,
    start_date: '2026-01-01',
    end: '2026-01-14',
    days_left: 14,
    spare_days: small ? 11 : 0,
    minutes_mean: 11,
    minutes_max: 12,
    required_minutes: 154,
    finish_by: small ? '2026-01-03' : '2026-01-14',
    fits,
    reasons: fits ? [] : ['minutes'],
    suggest: fits ? null : { minutes_per_day: 15, total_weeks: 3 },
  },
  estimate: {
    course_low_usd: '1.23',
    course_high_usd: '2.90',
    first_lesson: { low_usd: '0.09', high_usd: '0.21' },
    run_cap_usd: '8.00',
    basis: 'L02',
    basis_kchars: '58.938',
    mean_lesson_kchars: '4.100',
    extrapolated: true,
  },
});

const api = (mode: 'fits' | 'small' | 'no-fit' | 'loading' | 'error'): PlanApi => ({
  materials: async () => ({ materials: [{ id: 'synthetic-plan', status: 'imported', page_count: 30 }] }),
  stored: async () => null,
  preview: () =>
    mode === 'loading'
      ? new Promise<Preview>(() => undefined)
      : mode === 'error'
        ? Promise.reject(new Error('network'))
        : Promise.resolve(preview(mode !== 'no-fit', mode === 'small')),
  confirm: () => new Promise(() => undefined),
});

const stored = (mode: Parameters<typeof api>[0]) => ({
  course_id: 'c',
  plan_course_id: 'plan-cccccccccccc',
  inputs: { material_id: 'synthetic-plan', total_weeks: 2, minutes_per_day: mode === 'no-fit' ? 10 : 60, rate: '2.64', start_date: '2026-01-01' },
  plan_sha256: 'c'.repeat(64),
  started: 0,
});

const meta = {
  title: 'Import/PlanSection',
  component: PlanSection,
  args: { api: api('fits'), materialId: 'synthetic-plan', stored: stored('fits'), locale: 'en-GB', today: () => '2026-01-01' },
} satisfies Meta<typeof PlanSection>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
export const SmallSource: Story = { args: { api: api('small') }, decorators: [themed('light')] };
export const DoesNotFit: Story = { args: { api: api('no-fit'), stored: stored('no-fit') }, decorators: [themed('light')] };
export const Loading: Story = { args: { api: api('loading') }, decorators: [themed('light')] };
export const ErrorState: Story = { args: { api: api('error') }, decorators: [themed('light')] };
export const Empty: Story = { args: { stored: null }, decorators: [themed('light')] };

export const Sources: Story = {
  decorators: [themed('light')],
  render: () => (
    <ServicesContext.Provider value={{ api: {} as never, notify: () => undefined, navigateAway: () => undefined, plan: api('fits') }}>
      <QueryClientProvider client={new QueryClient()}>
        <ImportPage />
      </QueryClientProvider>
    </ServicesContext.Provider>
  ),
};
