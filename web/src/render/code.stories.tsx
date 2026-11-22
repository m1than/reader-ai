import type { Meta, StoryObj } from '@storybook/react-vite';
import { themed } from '../ui/story';
import { Code } from './code';

const meta = {
  title: 'Render/Code',
  component: Code,
  args: { code: 'import hashlib\n# a digest\nd = hashlib.sha256(b"abc").hexdigest()\nprint(len(d), 64)', language: 'python' },
} satisfies Meta<typeof Code>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
