// Stories of the upload sections of /import (WEB-IMPORT FR-IMPORT-1, -3): the drop zone, then each processing state
// from spec/fixtures/WEB-IMPORT/responses.json's shapes. Synthetic bodies only ("Synthetic chapter 1.pdf").
import type { Meta, StoryObj } from '@storybook/react-vite';
import { themed } from '../ui/story';
import type { ConverterView, UploadApi, UploadView } from './api';
import { UploadSections } from './UploadSections';

const ID = '11111111-1111-4111-8111-111111111111';

const view = (over: Partial<UploadView>): UploadView => ({
  id: ID,
  material_id: 'up-0123456789ab',
  filename: 'Synthetic chapter 1.pdf',
  size: 2048,
  language: 'en',
  state: 'queued',
  code: null,
  pages_done: null,
  pages_total: null,
  attempts: 0,
  created_at: '2026-11-23T10:00:00Z',
  retry: null,
  ...over,
});

const online: ConverterView = {
  state: 'online',
  seen_at: '2026-11-23T10:00:00Z',
  converter_version: 'docling-2.151.1+r2',
  strays: 0,
  busy: 'none',
  stale: false,
};

const api = (shown: UploadView, converter: ConverterView = online): UploadApi => ({
  upload: () => ({ done: new Promise(() => undefined), abort: () => undefined }),
  view: async () => shown,
  converter: async () => converter,
  cancel: async () => undefined,
  discard: async () => undefined,
  retry: async () => ({ kind: 'restarted' }),
});

const meta = {
  title: 'Import/UploadSections',
  component: UploadSections,
  args: { api: api(view({})), onReady: () => undefined, locale: 'en-GB' },
} satisfies Meta<typeof UploadSections>;
export default meta;

type Story = StoryObj<typeof meta>;
const at = (shown: UploadView, converter?: ConverterView) => ({ api: api(shown, converter), initialUpload: ID });

export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
export const Queued: Story = { args: at(view({})), decorators: [themed('light')] };
export const QueuedBehindTheCli: Story = { args: at(view({}), { ...online, busy: 'cli_running' }), decorators: [themed('light')] };
export const ConverterOffline: Story = {
  args: at(view({}), { ...online, state: 'offline', seen_at: null, converter_version: null }),
  decorators: [themed('light')],
};
export const SetupMissing: Story = { args: at(view({}), { ...online, state: 'setup_missing' }), decorators: [themed('light')] };
export const Warnings: Story = { args: at(view({}), { ...online, strays: 2, stale: true }), decorators: [themed('dark')] };
export const Converting: Story = { args: at(view({ state: 'converting', pages_done: 50, pages_total: 120 })), decorators: [themed('light')] };
export const ConvertingDark: Story = { args: at(view({ state: 'converting', pages_done: 50, pages_total: 120 })), decorators: [themed('dark')] };
export const Importing: Story = { args: at(view({ state: 'importing' })), decorators: [themed('light')] };
export const Indexing: Story = { args: at(view({ state: 'indexing' })), decorators: [themed('light')] };
export const Ready: Story = { args: at(view({ state: 'ready' })), decorators: [themed('light')] };
export const FailedReconvert: Story = {
  args: at(view({ state: 'failed', code: 'convert_failed', retry: 'reconvert' })),
  decorators: [themed('light')],
};
export const FailedMemory: Story = {
  args: at(view({ state: 'failed', code: 'memory_limit', retry: 'reconvert' })),
  decorators: [themed('dark')],
};
export const FailedRequeue: Story = {
  args: at(view({ state: 'failed', code: 'artifact_invalid', retry: 'requeue' })),
  decorators: [themed('light')],
};
