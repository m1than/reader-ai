import type { Meta, StoryObj } from '@storybook/react-vite';
import { sampleLessons, sampleMe, themed, withServices } from '../../ui/story';
import { CompileControl } from './CompileControl';

// WEB-UI FR-UI-13: the owner's compile control in each state, and its confirm dialog.
const lesson = sampleLessons.lessons[1]!;
const status = (state: string, extra: Record<string, unknown> = {}) => ({
  run_id: 'run-1', lesson_id: lesson.lesson_id, state, cap_usd: '8.00',
  steps: [{ step: 1, stage: 'draft', model: 'claude-opus-5-5', cost_usd: '0.600000', duration_s: 12, reused: false }, { step: 2, stage: 'fact_check', status: 'running', reused: false }],
  spent_usd: '0.60', task_budgets: {}, refusals: [], g2: null, latest_sha256: null, ...extra,
});
const estimate = { lesson_id: lesson.lesson_id, kchars: '22.104', low_usd: '0.47', high_usd: '1.11', run_cap_usd: '8.00', basis: 'L02', basis_kchars: '58.938', extrapolated: true };
const routes = (compile?: unknown) => ({
  '/api/me': sampleMe,
  [`/api/lessons/${lesson.lesson_id}/estimate`]: estimate,
  ...(compile ? { [`/api/lessons/${lesson.lesson_id}/compile`]: compile } : {}),
});

const meta = {
  title: 'App/CompileControl',
  component: CompileControl,
  args: { lesson, read: true },
  decorators: [withServices(routes())],
} satisfies Meta<typeof CompileControl>;
export default meta;

type Story = StoryObj<typeof meta>;
/** Not compiled yet: the status and the Compile button. */
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
export const Compiling: Story = { decorators: [withServices(routes(status('running'))), themed('light')] };
export const Stopped: Story = {
  decorators: [withServices(routes(status('stage_failed', { steps: [{ step: 1, stage: 'draft', status: 'dead', reused: false }], error: null }))), themed('light')],
};
export const ProviderMissing: Story = {
  decorators: [withServices(routes(status('stage_failed', { steps: [{ step: 1, stage: 'draft', status: 'dead', reused: false }], error: 'provider_missing' }))), themed('dark')],
};
export const BudgetStop: Story = {
  decorators: [withServices(routes(status('budget_exhausted', { refusals: ['budget_exhausted'], spent_usd: '7.95' }))), themed('light')],
};
/** The confirm with the lesson's estimate, its extrapolation note and the cap. */
export const Confirm: Story = { args: { defaultOpen: true }, decorators: [themed('light')] };
export const ConfirmDark: Story = { args: { defaultOpen: true }, decorators: [themed('dark')] };
