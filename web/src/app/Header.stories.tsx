import type { Meta, StoryObj } from '@storybook/react-vite';
import { sampleLessons, sampleMe, themed, withRouter, withServices } from '../ui/story';
import { Header, MainNav } from './Header';

const pending = (n: number) => ({
  pending: Array.from({ length: n }, (_, i) => ({
    id: `00000000-0000-4000-8000-00000000000${i}`,
    handle: `lc-p${i}x`,
    display_name: `P${i}`,
    requested_at: '2026-11-23T09:00:00+00:00',
  })),
  accounts: [],
});

const meta = {
  title: 'App/Header',
  component: Header,
  args: { displayName: 'Ivan', owner: true, path: '/', currentLesson: null, onSignOut: () => undefined },
  decorators: [withRouter, withServices({ '/api/me': sampleMe, '/api/lessons': sampleLessons })],
} satisfies Meta<typeof Header>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
/** A learner: no Import link (WEB-UI FR-UI-12). */
export const Learner: Story = { args: { displayName: 'Lena', owner: false }, decorators: [themed('light')] };
/** On a lesson page: Lessons is the current item. */
export const OnALesson: Story = { args: { path: '/lessons/w01', currentLesson: 'w01' }, decorators: [themed('light')] };
/** The owner with three sign-ups waiting: the Sign-ups badge (FR-UI-11, FR-UI-14), from 75rem up. */
export const PendingSignups: Story = {
  decorators: [withServices({ '/api/me': sampleMe, '/api/lessons': sampleLessons, '/api/auth/signups': pending(3) }), themed('light')],
};
/** The main nav as the Courses drawer shows it below 75rem, with the Sign-ups badge ("Sign-ups 3 waiting"). */
export const DrawerNavWithBadge: Story = {
  decorators: [withServices({ '/api/auth/signups': pending(3) }), themed('dark')],
  render: () => <MainNav owner path="/signups" className="drawer-nav story-drawer-nav" />,
};
