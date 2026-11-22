import type { Meta, StoryObj } from '@storybook/react-vite';
import { themed } from '../ui/story';
import { Latex } from './latex';

const meta = {
  title: 'Render/Latex',
  component: Latex,
  args: { source: '$$\\Pr[\\text{collision}] \\approx 1 - e^{-k^2/2N}$$' },
} satisfies Meta<typeof Latex>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
export const ParseError: Story = { args: { source: '$\\frac{$' }, decorators: [themed('light')] };
