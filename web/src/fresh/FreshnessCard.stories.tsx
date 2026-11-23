import type { Meta, StoryObj } from '@storybook/react-vite';
import bodies from '../../../spec/fixtures/WEB-FRESH/bodies.json';
import { themed } from '../ui/story';
import type { FreshApi, Freshness } from './api';
import { FreshnessCard } from './FreshnessCard';

type StateName = keyof typeof bodies.states;

/** A client answering every GET with one VEC-FRESHUI-1 body; Save and Run succeed. */
const fixtureApi = (name: StateName): FreshApi => {
  const body = bodies.states[name].body as unknown as Freshness;
  return {
    get: async () => structuredClone(body),
    saveBibliography: async () => structuredClone(bodies.bibliography_saved) as unknown as NonNullable<Freshness['bibliography']>,
    run: async () => bodies.post_accepted.body,
  };
};

const at = (name: StateName) => ({ materialId: bodies.states[name].material_id, api: fixtureApi(name) });

const meta = {
  title: 'Fresh/FreshnessCard',
  component: FreshnessCard,
  args: at('partly_outdated'),
} satisfies Meta<typeof FreshnessCard>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
export const NoCheck: Story = { args: at('empty'), decorators: [themed('light')] };
export const ReadyToRun: Story = { args: at('estimate'), decorators: [themed('light')] };
export const Running: Story = { args: { ...at('running'), pollMs: 3_600_000 }, decorators: [themed('light')] };
export const Current: Story = { args: at('current'), decorators: [themed('light')] };
export const Outdated: Story = { args: at('outdated'), decorators: [themed('dark')] };
export const Stale: Story = { args: at('stale'), decorators: [themed('light')] };
export const Failed: Story = { args: at('failed'), decorators: [themed('light')] };
export const BudgetRefused: Story = { args: at('budget_refused'), decorators: [themed('light')] };
export const ScanOnly: Story = { args: at('scan_only'), decorators: [themed('light')] };
