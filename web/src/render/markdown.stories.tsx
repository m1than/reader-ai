import type { Meta, StoryObj } from '@storybook/react-vite';
import { themed } from '../ui/story';
import { Markdown } from './markdown';

const meta = {
  title: 'Render/Markdown',
  component: Markdown,
  args: {
    source:
      '## Digests\n\nFor an $n$-bit output a collision is expected after about\n\n$$2^{n/2}$$\n\ntries.\n\n| n | tries |\n|---|---|\n| 128 | 2^64 |\n\n```python\nimport hashlib\nprint(hashlib.sha256(b"abc").hexdigest())\n```\n\n<script>shown as text</script> [a link](https://example.com)',
  },
} satisfies Meta<typeof Markdown>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
