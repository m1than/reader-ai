import type { Meta, StoryObj } from '@storybook/react-vite';
import { AskIcon, ChevronIcon, CloseIcon, CopyIcon, InfoIcon, MarkIcon, MenuIcon, MoreIcon, PlayIcon, SearchOffIcon, WarningIcon } from './icons';
import { themed } from './story';

const ALL = { MarkIcon, MenuIcon, ChevronIcon, PlayIcon, MoreIcon, CopyIcon, CloseIcon, InfoIcon, WarningIcon, AskIcon, SearchOffIcon };

/** Every app-owned icon (WEB-UI FR-UI-11): inline SVG in currentColor, hidden from assistive technology. */
const Gallery = () => (
  <ul style={{ display: 'flex', flexWrap: 'wrap', gap: 16, listStyle: 'none', margin: 0, padding: 0 }} role="list">
    {Object.entries(ALL).map(([name, Icon]) => (
      <li key={name} style={{ display: 'grid', justifyItems: 'center', gap: 4, fontSize: 12 }}>
        <Icon />
        <span>{name}</span>
      </li>
    ))}
  </ul>
);

const meta = { title: 'UI/Icons', component: MarkIcon, render: () => <Gallery /> } satisfies Meta<typeof MarkIcon>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
