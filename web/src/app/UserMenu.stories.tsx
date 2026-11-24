import type { Meta, StoryObj } from '@storybook/react-vite';
import { themed, withRouter } from '../ui/story';
import { UserMenu } from './UserMenu';

const meta = {
  title: 'App/UserMenu',
  component: UserMenu,
  args: { displayName: 'Ivan', defaultOpen: true, onSignOut: () => undefined },
} satisfies Meta<typeof UserMenu>;
export default meta;

type Story = StoryObj<typeof meta>;
/** The menu open: the theme choice, Settings (WEB-MANAGE) and Sign out (WEB-UI FR-UI-16); Settings is a router link. */
export const Light: Story = { decorators: [withRouter, themed('light')] };
export const Dark: Story = { decorators: [withRouter, themed('dark')] };
