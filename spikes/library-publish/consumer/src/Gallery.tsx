// The 20 library components ADR-0013 says the reader-ai screens need, each in its smallest composition,
// imported from the packed package by entry (one entry per component, the library's unit of bundle cost).
import { Avatar } from '@acme/ui/avatar';
import { Charts } from '@acme/ui/charts';
import { Chip } from '@acme/ui/chip';
import { DataGrid, type DataGridColumn } from '@acme/ui/data-grid';
import { DateCalendar } from '@acme/ui/date-calendar';
import { Dialog } from '@acme/ui/dialog';
import { Drawer } from '@acme/ui/drawer';
import { Menu } from '@acme/ui/menu';
import { Popover } from '@acme/ui/popover';
import { Progress } from '@acme/ui/progress';
import { ScrollArea } from '@acme/ui/scroll-area';
import { Select } from '@acme/ui/select';
import { Skeleton } from '@acme/ui/skeleton';
import { Slider } from '@acme/ui/slider';
import { Switch } from '@acme/ui/switch';
import { Tabs } from '@acme/ui/tabs';
import { TextField } from '@acme/ui/text-field';
import { Toast } from '@acme/ui/toast';
import { Tooltip } from '@acme/ui/tooltip';
import { TreeView } from '@acme/ui/tree-view';

type Row = { id: string; lesson: string; status: string };
const rows: Row[] = [
  { id: '1', lesson: 'L01 Blockchain basics', status: 'done' },
  { id: '2', lesson: 'L02 Cryptography', status: 'in progress' },
];
const columns: DataGridColumn<Row>[] = [
  { field: 'lesson', headerName: 'Lesson', width: 220 },
  { field: 'status', headerName: 'Status', width: 120 },
];
const toasts = Toast.createToastManager();
toasts.add({ title: 'Saved', description: 'Lesson progress saved.' });

function ToastList() {
  const { toasts: list } = Toast.useToastManager();
  return list.map((toast) => (
    <Toast.Root key={toast.id} toast={toast}>
      <Toast.Title />
      <Toast.Description />
    </Toast.Root>
  ));
}

export function Gallery() {
  return (
    <main style={{ display: 'grid', gap: 24, padding: 24, maxInlineSize: 760 }}>
      <Dialog.Root>
        <Dialog.Trigger>Open dialog</Dialog.Trigger>
        <Dialog.Portal>
          <Dialog.Backdrop />
          <Dialog.Popup>
            <Dialog.Title>Gate review</Dialog.Title>
            <Dialog.Close>Close</Dialog.Close>
          </Dialog.Popup>
        </Dialog.Portal>
      </Dialog.Root>
      <Tabs.Root defaultValue="theory">
        <Tabs.List aria-label="Lesson">
          <Tabs.Tab value="theory">Theory</Tabs.Tab>
          <Tabs.Tab value="lab">Lab</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="theory">Theory text</Tabs.Panel>
        <Tabs.Panel value="lab">Lab cells</Tabs.Panel>
      </Tabs.Root>
      <Menu.Root>
        <Menu.Trigger>Actions</Menu.Trigger>
        <Menu.Portal>
          <Menu.Positioner>
            <Menu.Popup>
              <Menu.Item>Run all</Menu.Item>
            </Menu.Popup>
          </Menu.Positioner>
        </Menu.Portal>
      </Menu.Root>
      <Tooltip.Root>
        <Tooltip.Trigger>Hint</Tooltip.Trigger>
        <Tooltip.Portal>
          <Tooltip.Positioner>
            <Tooltip.Popup>Ask the tutor</Tooltip.Popup>
          </Tooltip.Positioner>
        </Tooltip.Portal>
      </Tooltip.Root>
      <Toast.Provider toastManager={toasts} timeout={0}>
        <Toast.Viewport aria-label="Notifications">
          <ToastList />
        </Toast.Viewport>
      </Toast.Provider>
      <Popover.Root>
        <Popover.Trigger>Source</Popover.Trigger>
        <Popover.Portal>
          <Popover.Positioner>
            <Popover.Popup>
              <Popover.Title>Bashir, p. 128</Popover.Title>
            </Popover.Popup>
          </Popover.Positioner>
        </Popover.Portal>
      </Popover.Root>
      <Drawer.Root>
        <Drawer.Trigger>Tutor</Drawer.Trigger>
        <Drawer.Portal>
          <Drawer.Backdrop />
          <Drawer.Popup>
            <Drawer.Title>Tutor</Drawer.Title>
            <Drawer.Close>Close</Drawer.Close>
          </Drawer.Popup>
        </Drawer.Portal>
      </Drawer.Root>
      <DataGrid.Root rows={rows} columns={columns}>
        <DataGrid.Grid aria-label="Lessons" style={{ blockSize: 160 }}>
          <DataGrid.Header>{(column: DataGridColumn) => <DataGrid.ColumnHeader column={column}>{column.headerName}</DataGrid.ColumnHeader>}</DataGrid.Header>
          <DataGrid.Body>
            {(row: Row) => <DataGrid.Row row={row}>{(column: DataGridColumn<Row>) => <DataGrid.Cell row={row} column={column} />}</DataGrid.Row>}
          </DataGrid.Body>
        </DataGrid.Grid>
      </DataGrid.Root>
      <TreeView.Root aria-label="Course">
        <TreeView.Item value="s02">
          <TreeView.ItemContent>
            <TreeView.ItemLabel>S02 Cryptography</TreeView.ItemLabel>
          </TreeView.ItemContent>
        </TreeView.Item>
      </TreeView.Root>
      <Progress.Root value={40}>
        <Progress.Label>Course</Progress.Label>
        <Progress.Track>
          <Progress.Indicator />
        </Progress.Track>
        <Progress.Value />
      </Progress.Root>
      <div>
        <Switch.Root id="reduced-motion">
          <Switch.Thumb />
        </Switch.Root>
        <Switch.Label htmlFor="reduced-motion">Reduced motion</Switch.Label>
      </div>
      <Slider.Root defaultValue={30}>
        <Slider.Label>Daily minutes</Slider.Label>
        <Slider.Track>
          <Slider.Range />
          <Slider.Thumb aria-label="Daily minutes" />
        </Slider.Track>
      </Slider.Root>
      <TextField.Root>
        <TextField.Label>Question</TextField.Label>
        <TextField.Input name="question" />
      </TextField.Root>
      <Select.Root>
        <Select.Trigger aria-label="Kernel">
          <Select.Value placeholder="Kernel" />
        </Select.Trigger>
        <Select.Portal>
          <Select.Positioner>
            <Select.Popup>
              <Select.Item value="python3">
                <Select.ItemText>Python 3</Select.ItemText>
              </Select.Item>
            </Select.Popup>
          </Select.Positioner>
        </Select.Portal>
      </Select.Root>
      <Chip.Root>
        <Chip.Label>ECDSA</Chip.Label>
      </Chip.Root>
      <Avatar.Root>
        <Avatar.Fallback>IG</Avatar.Fallback>
      </Avatar.Root>
      <DateCalendar.Root defaultValue="2026-11-20" referenceDate="2026-11-20" locale="en-GB">
        <DateCalendar.Header>
          <DateCalendar.PrevMonth aria-label="Previous month">‹</DateCalendar.PrevMonth>
          <DateCalendar.Heading />
          <DateCalendar.NextMonth aria-label="Next month">›</DateCalendar.NextMonth>
        </DateCalendar.Header>
        <DateCalendar.DayGrid />
      </DateCalendar.Root>
      <Charts.Root series={[{ id: 'minutes', type: 'bar', label: 'Minutes', data: [60, 45, 70] }]} xAxis={[{ data: ['Mon', 'Tue', 'Wed'], scaleType: 'band' }]} height={200}>
        <Charts.Title>Study minutes</Charts.Title>
        <Charts.Surface>
          <Charts.XAxis />
          <Charts.YAxis />
          <Charts.BarPlot />
        </Charts.Surface>
      </Charts.Root>
      <Skeleton.Root aria-label="Loading">
        <Skeleton.Item width="60%" />
      </Skeleton.Root>
      <ScrollArea.Root style={{ blockSize: 80 }}>
        <ScrollArea.Viewport aria-label="Notes">
          <ScrollArea.Content>{Array.from({ length: 10 }, (_, i) => <p key={i}>Note {i + 1}</p>)}</ScrollArea.Content>
        </ScrollArea.Viewport>
        <ScrollArea.Scrollbar orientation="vertical">
          <ScrollArea.Thumb />
        </ScrollArea.Scrollbar>
      </ScrollArea.Root>
    </main>
  );
}
