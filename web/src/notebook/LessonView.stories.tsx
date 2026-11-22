import type { Meta, StoryObj } from '@storybook/react-vite';
import { sampleNotebook, staticController, themed } from '../ui/story';
import { LessonView, type Notebook } from './LessonView';

const meta = {
  title: 'Notebook/LessonView',
  component: LessonView,
  args: { notebook: sampleNotebook as Notebook, controller: staticController() },
} satisfies Meta<typeof LessonView>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
export const Running: Story = {
  args: {
    controller: staticController({
      l: { state: 'running', outputs: [{ output_type: 'stream', name: 'stdout', text: 'working…\n' }], executionCount: null, marker: null, notes: [] },
    }),
  },
  decorators: [themed('light')],
};
