import type { Meta, StoryObj } from '@storybook/react-vite';
import { sampleLessons, themed, withRouter } from '../ui/story';
import { LessonList } from './LessonList';

const meta = {
  title: 'App/LessonList',
  component: LessonList,
  args: { lessons: sampleLessons.lessons, locale: 'en' },
  decorators: [withRouter],
} satisfies Meta<typeof LessonList>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
export const Empty: Story = { args: { lessons: [] }, decorators: [themed('light')] };
