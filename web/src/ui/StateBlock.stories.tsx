import type { Meta, StoryObj } from '@storybook/react-vite';
import { SearchOffIcon, WarningIcon } from './icons';
import { StateBlock } from './StateBlock';
import { themed } from './story';

const meta = {
  title: 'UI/StateBlock',
  component: StateBlock,
  args: {
    icon: <SearchOffIcon size={20} />,
    title: 'Not found',
    children: <p className="muted">Nothing lives at this address.</p>,
    actions: <a href="#lessons">Back to lessons</a>,
  },
} satisfies Meta<typeof StateBlock>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
/** An error state with Retry (WEB-UI FR-UI-11). */
export const Error: Story = {
  args: {
    tone: 'danger',
    icon: <WarningIcon size={20} />,
    title: 'The lesson could not be loaded.',
    children: <p className="muted">Check the connection, then retry.</p>,
    actions: (
      <button type="button" className="acme-button" data-variant="outline" data-color="neutral">
        Retry
      </button>
    ),
  },
  decorators: [themed('light')],
};
