import type { Meta, StoryObj } from '@storybook/react-vite';
import { sampleLessons, sampleMe, themed, withRouter, withServices } from '../ui/story';
import { CourseSidebar } from './CourseSidebar';

const big = {
  lessons: Array.from({ length: 30 }, (_, i) => ({
    lesson_id: `b${i + 1}`,
    course_id: 'big',
    course_title: 'A long course',
    ordinal: i + 1,
    title: `pp. ${i * 4 + 1}–${i * 4 + 4}`,
    updated_at: '2026-11-21T09:00:00Z',
    latest_sha256: i === 0 || i === 16 ? '1'.repeat(64) : null,
  })),
};

const meta = {
  title: 'App/CourseSidebar',
  component: CourseSidebar,
  args: { current: 'w01' },
  decorators: [withRouter, withServices({ '/api/me': sampleMe, '/api/lessons': sampleLessons })],
} satisfies Meta<typeof CourseSidebar>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
/** A course of 30 lessons, 2 published: the open group shows published rows and the current one (FR-UI-17). */
export const LargeCourse: Story = {
  args: { current: 'b17' },
  // Story decorators wrap inside the meta's: this services value is the one the sidebar reads.
  decorators: [withServices({ '/api/me': sampleMe, '/api/lessons': big }), themed('light')],
};
