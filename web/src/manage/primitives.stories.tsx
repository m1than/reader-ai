// WEB-MANAGE FR-MANAGE-1 [story]: the primitives in Calm light and dark, with synthetic titles only.
import type { Meta, StoryObj } from '@storybook/react-vite';
import { themed } from '../ui/story';
import { ActionsMenu, ConfirmDialog, RenameField, UndoToast } from './primitives';

function Primitives() {
  const none = async () => undefined;
  return (
    <div className="page">
      <h2>
        Fixture course{' '}
        <ActionsMenu
          label="Fixture course"
          items={[
            { label: 'Rename', onSelect: () => undefined },
            { label: 'Archive', onSelect: () => undefined },
            { label: 'Delete…', onSelect: () => undefined, danger: true },
          ]}
        />
      </h2>
      <RenameField value="Fixture course" label="Course title" hint="Lessons compiled before keep the old name in their text until they are compiled again." onSave={none} onCancel={() => undefined} />
      <UndoToast text="Course deleted." restoredText="Course restored." onUndo={none} onClose={() => undefined} ms={3_600_000} />
      <ConfirmDialog open title="Delete the course “Fixture course”?" confirmLabel="Delete" typed="Fixture course" onConfirm={none} onClose={() => undefined}>
        <p>3 lessons, 5 compiled revisions, 7 tutor questions and 2 compile runs are hidden.</p>
      </ConfirmDialog>
    </div>
  );
}

const meta = { title: 'Manage/Primitives', component: Primitives } satisfies Meta<typeof Primitives>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [themed('light')] };
export const Dark: Story = { decorators: [themed('dark')] };
