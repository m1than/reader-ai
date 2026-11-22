import type { Meta, StoryObj } from '@storybook/react-vite';
import { themed } from '../ui/story';
import { NoticeRegion } from './notices';

const meta = {
  title: 'App/NoticeRegion',
  component: NoticeRegion,
  args: {
    notices: ['timeout', 'kernel_reset', 'too_many_kernels', 'connect_failed'].map((code, id) => ({ id, code })),
    onDismiss: () => undefined,
  },
} satisfies Meta<typeof NoticeRegion>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
