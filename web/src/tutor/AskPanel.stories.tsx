// SRV-TUTOR FR-TUTOR-11 (design pass): the Ask panel's states over a stub tutor API; synthetic text only.
import type { Meta, StoryObj } from '@storybook/react-vite';
import { themed } from '../ui/story';
import type { HistoryItem, QuestionState, TutorApi } from './api';
import { AskPanel } from './AskPanel';

const LESSON = '11111111-1111-4111-8111-111111111111';
const QID = '22222222-2222-4222-8222-222222222222';

const answered = (grounded: boolean): QuestionState => ({
  state: 'answered',
  grounded,
  answer: {
    blocks: [
      { text: 'A digest maps any input to a **fixed-length** value.', supported: true, quote: 'q', citations: [{ span: `${'c'.repeat(64)}:0`, page: 12, material_title: 'Sample material' }] },
      ...(grounded ? [] : [{ text: 'A claim the source does not make.', supported: false, quote: 'q', citations: [] }]),
    ],
    insufficient: false,
    retrieval: 'ok',
    prompt_sha256: 'b'.repeat(64),
    model: 'stub',
  },
});
const past = (n: number): HistoryItem => ({
  question_id: `${String(n).padStart(8, '0')}-4444-4444-8444-444444444444`,
  question: `Synthetic earlier question ${n}?`,
  cell_id: null,
  state: 'answered',
  grounded: true,
  error_code: null,
  created_at: '2026-11-22T10:00:00Z',
});

const api = (opts: { history?: HistoryItem[]; current?: QuestionState }): TutorApi => {
  const queued = opts.current ? [{ ...past(99), question_id: QID, question: 'What does a digest map its input to?', state: 'queued' as const, grounded: null }] : [];
  return {
    ask: async () => ({ question_id: QID }),
    getQuestion: async (_l, id) => (id === QID && opts.current ? opts.current : answered(true)),
    listQuestions: async () => ({ questions: [...queued, ...(opts.history ?? [])] }),
  } as TutorApi;
};

const meta = {
  title: 'Tutor/AskPanel',
  component: AskPanel,
  args: { lessonId: LESSON, revision: 'a'.repeat(64), cellId: 'w01-explain', cellLabel: 'Digests', api: api({ current: answered(true) }), open: true, onClose: () => undefined },
} satisfies Meta<typeof AskPanel>;
export default meta;

type Story = StoryObj<typeof meta>;
/** Open, with an answer and its citations. */
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
export const Closed: Story = { args: { open: false }, decorators: [themed('light')] };
export const OpenEmpty: Story = { args: { api: api({}), cellId: null }, decorators: [themed('light')] };
export const Waiting: Story = { args: { api: api({ current: { state: 'queued' } as QuestionState }) }, decorators: [themed('light')] };
export const Ungrounded: Story = { args: { api: api({ current: answered(false) }) }, decorators: [themed('light')] };
export const Failed: Story = {
  args: { api: api({ current: { state: 'failed', error_code: 'tutor_failed' } as QuestionState }) },
  decorators: [themed('light')],
};
/** 8 earlier questions: 3 shown, the rest behind "Show 5 earlier questions". */
export const History: Story = { args: { api: api({ history: [1, 2, 3, 4, 5, 6, 7, 8].map(past) }) }, decorators: [themed('light')] };
