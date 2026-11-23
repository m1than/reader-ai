import type { Decorator, Meta, StoryObj } from '@storybook/react-vite';
import { sampleLessons, themed, withRouter } from '../ui/story';
import { ServicesContext, type CompileSlot } from './context';
import { LessonList } from './LessonList';

const large = Array.from({ length: 30 }, (_, i) => ({
  lesson_id: `b${i + 1}`,
  course_id: 'big',
  course_title: 'A long course',
  ordinal: i + 1,
  title: `pp. ${i * 4 + 1}–${i * 4 + 4}`,
  updated_at: `2026-11-${String(10 + (i % 9)).padStart(2, '0')}T09:00:00Z`,
  latest_sha256: i === 0 || i === 16 ? '1'.repeat(64) : null,
}));

/** A stub compile slot (the shape WEB-UI FR-UI-15 fills): a status and a Compile action on unpublished rows. */
const stubSlot: CompileSlot = {
  Status: ({ lesson }) => (lesson.latest_sha256 ? null : <span className="chip">not compiled yet</span>),
  Action: ({ lesson }) =>
    lesson.latest_sha256 ? null : (
      <button type="button" className="acme-button" data-variant="ghost" data-color="neutral" data-size="sm">
        Compile
      </button>
    ),
};
const withSlot: Decorator = (Story) => (
  <ServicesContext.Provider value={{ api: {} as never, notify: () => undefined, navigateAway: () => undefined, compileSlot: stubSlot }}>
    <Story />
  </ServicesContext.Provider>
);

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
/** 30 lessons, 2 published: the group starts collapsed to its published rows (FR-UI-6). */
export const LargeCourse: Story = { args: { lessons: [...sampleLessons.lessons, ...large] }, decorators: [themed('light')] };
export const WithCompileSlot: Story = { decorators: [withSlot, themed('light')] };
