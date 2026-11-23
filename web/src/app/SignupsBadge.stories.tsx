import type { Meta, StoryObj } from '@storybook/react-vite';
import { themed, withServices } from '../ui/story';
import { SignupsBadge } from './SignupsBadge';

const pending = (n: number) => ({
  pending: Array.from({ length: n }, (_, i) => ({
    id: `00000000-0000-4000-8000-00000000000${i}`,
    handle: `lc-p${i}x`,
    display_name: `P${i}`,
    requested_at: '2026-11-23T09:00:00+00:00',
  })),
  accounts: [],
});

/** FR-UI-14: the Sign-ups nav entry's count (inside a link in the header; shown here alone). */
const meta = {
  title: 'App/SignupsBadge',
  component: SignupsBadge,
  decorators: [withServices({ '/api/auth/signups': pending(3) })],
} satisfies Meta<typeof SignupsBadge>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
/** Nothing waiting: no badge. */
export const NoneWaiting: Story = { decorators: [withServices({ '/api/auth/signups': pending(0) }), themed('light')] };
