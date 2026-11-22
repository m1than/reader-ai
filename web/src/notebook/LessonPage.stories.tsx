import type { Meta, StoryObj } from '@storybook/react-vite';
import { sampleLesson, sampleNotebook, sampleSha, themed, withServices } from '../ui/story';
import { LessonPage } from './LessonPage';

const meta = {
  title: 'Notebook/LessonPage',
  component: LessonPage,
  args: { lessonId: 'w01' },
  decorators: [withServices({ '/api/lessons/w01': sampleLesson, [`/api/lessons/w01/revisions/${sampleSha}`]: sampleNotebook })],
} satisfies Meta<typeof LessonPage>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
