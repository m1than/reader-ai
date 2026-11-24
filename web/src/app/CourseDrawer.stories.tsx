import type { Meta, StoryObj } from '@storybook/react-vite';
import { sampleLessons, sampleMe, themed, withRouter, withServices } from '../ui/story';
import { CourseDrawer } from './CourseDrawer';

const meta = {
  title: 'App/CourseDrawer',
  component: CourseDrawer,
  args: { open: true, onClose: () => undefined, current: 'w01' },
  decorators: [withRouter, withServices({ '/api/me': sampleMe, '/api/lessons': sampleLessons })],
} satisfies Meta<typeof CourseDrawer>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
