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
/** FR-NOTEBOOK-11: with the tutor slot, so the contents end with "Ask the tutor". */
export const WithAside: Story = {
  args: { aside: (id: string | null, label: string | null) => <section id="tutor">{`Focused: ${id ?? 'none'} ${label ?? ''}`}</section> },
  decorators: [themed('light')],
};
/** FR-NOTEBOOK-9: the lab's plain text area while the CodeMirror chunk loads (held pending here). */
export const EditorLoading: Story = { args: { loadEditor: () => new Promise(() => undefined) }, decorators: [themed('light')] };
export const Running: Story = {
  args: {
    controller: staticController({
      l: { state: 'running', outputs: [{ output_type: 'stream', name: 'stdout', text: 'working…\n' }], executionCount: null, marker: null, notes: [] },
    }),
  },
  decorators: [themed('light')],
};
