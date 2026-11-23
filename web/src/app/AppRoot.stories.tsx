import type { Meta, StoryObj } from '@storybook/react-vite';
import { pendingFetch, sampleLessons, sampleMe, stubFetch, themed } from '../ui/story';
import { AppRoot } from './AppRoot';

const meta = {
  title: 'App/AppRoot',
  component: AppRoot,
  args: { initialPath: '/', fetch: stubFetch({ '/api/me': sampleMe, '/api/lessons': sampleLessons }), navigateAway: () => undefined },
} satisfies Meta<typeof AppRoot>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
/** FR-UI-11: the shell while /api/me loads — a status with hidden text and the library skeleton. */
export const Loading: Story = { args: { fetch: pendingFetch }, decorators: [themed('light')] };
