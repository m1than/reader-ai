import type { Meta, StoryObj } from '@storybook/react-vite';
import { themed } from '../ui/story';
import { UserMenu } from './UserMenu';

const meta = {
  title: 'App/UserMenu',
  component: UserMenu,
  args: { displayName: 'Ivan', defaultOpen: true, onSignOut: () => undefined },
} satisfies Meta<typeof UserMenu>;
export default meta;

type Story = StoryObj<typeof meta>;
/** The menu open: the theme choice and Sign out (WEB-UI FR-UI-16). */
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
