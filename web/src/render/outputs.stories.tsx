import type { Meta, StoryObj } from '@storybook/react-vite';
import { themed } from '../ui/story';
import { Output } from './outputs';

const meta = {
  title: 'Render/Output',
  component: Output,
  args: { output: { output_type: 'stream', name: 'stdout', text: 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad\n' } },
} satisfies Meta<typeof Output>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
export const ErrorOutput: Story = {
  args: { output: { output_type: 'error', ename: 'ZeroDivisionError', evalue: 'division by zero', traceback: ['\u001b[0;31mZeroDivisionError\u001b[0m: division by zero'] } },
  decorators: [themed('light')],
};
export const Json: Story = {
  args: { output: { output_type: 'execute_result', execution_count: 1, data: { 'application/json': { digest_bits: 256 } }, metadata: {} } },
  decorators: [themed('light')],
};
export const HtmlAsText: Story = {
  args: { output: { output_type: 'display_data', data: { 'text/html': '<b>bold</b>', 'text/plain': 'bold' }, metadata: {} } },
  decorators: [themed('light')],
};
