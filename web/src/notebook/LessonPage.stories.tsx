import type { Meta, StoryObj } from '@storybook/react-vite';
import { sampleLesson, sampleNotebook, sampleSha, themed, withRouter, withServices } from '../ui/story';
import { LessonPage } from './LessonPage';

const routes = { '/api/lessons/w01': sampleLesson, [`/api/lessons/w01/revisions/${sampleSha}`]: sampleNotebook };

const meta = {
  title: 'Notebook/LessonPage',
  component: LessonPage,
  args: { lessonId: 'w01' },
  // The router sits inside the services: the page's back links are router links (FR-NOTEBOOK-11, -12).
  decorators: [withRouter, withServices(routes)],
} satisfies Meta<typeof LessonPage>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
/** FR-NOTEBOOK-12: an unknown lesson answers 404 — "Lesson not found" with a way back only. */
export const NotFound: Story = { args: { lessonId: 'missing' }, decorators: [themed('light')] };
