import type { Meta, StoryObj } from '@storybook/react-vite';
import { sampleLessons, sampleMe, themed, withRouter, withServices } from '../ui/story';
import { Header } from './Header';

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
