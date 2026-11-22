import type { Meta, StoryObj } from '@storybook/react-vite';
import { sampleLessons, sampleMe, stubFetch, themed } from '../ui/story';
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
