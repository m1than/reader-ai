import type { Meta, StoryObj } from '@storybook/react-vite';
import { Gallery } from './Gallery';

const meta = { title: 'Spike/Library gallery (Calm)', component: Gallery } satisfies Meta<typeof Gallery>;
export default meta;

export const Light: StoryObj<typeof meta> = {
  decorators: [(Story) => { document.documentElement.dataset.theme = 'light'; return <Story />; }],
};
export const Dark: StoryObj<typeof meta> = {
  decorators: [(Story) => { document.documentElement.dataset.theme = 'dark'; return <Story />; }],
};
