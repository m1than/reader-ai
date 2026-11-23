import type { Meta, StoryObj } from '@storybook/react-vite';
import { failingFetch, pendingFetch, sampleLesson, sampleLessons, sampleMe, sampleNotebook, sampleSha, themed, withRouter, withServices } from '../ui/story';
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
/** FR-NOTEBOOK-12: the status and the library skeleton while the lesson loads. */
export const Loading: Story = { decorators: [withServices({}, pendingFetch), themed('light')] };
/** FR-NOTEBOOK-12: a server error — the message, Retry and a way back. */
export const ServerError: Story = { decorators: [withServices({}, failingFetch), themed('light')] };
/** FR-NOTEBOOK-12 (amended): a lesson not published yet, for the owner — WEB-UI FR-UI-15's compile control. */
export const NotPublishedOwner: Story = {
  args: { lessonId: 'w02' },
  decorators: [withServices({ '/api/me': sampleMe, '/api/lessons/w02': { lesson: sampleLessons.lessons[1], revisions: [] } }), themed('light')],
};
/** FR-NOTEBOOK-12 (amended): a published draft whose compile still runs — the chip and status above the lesson. */
export const PublishedCompilingOwner: Story = {
  decorators: [
    withServices({
      ...routes,
      '/api/me': sampleMe,
      '/api/lessons/w01/compile': {
        run_id: 'run-1', lesson_id: 'w01', state: 'running', cap_usd: '8.00', spent_usd: '0.60', task_budgets: {}, refusals: [], g2: null,
        latest_sha256: sampleSha, error: null,
        steps: [{ step: 1, stage: 'draft', model: 'claude-opus-5-5', cost_usd: '0.600000', duration_s: 12, reused: false }, { step: 2, stage: 'fact_check', status: 'running', reused: false }],
      },
    }),
    themed('light'),
  ],
};
