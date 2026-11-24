// WEB-MANAGE FR-MANAGE-1: one Undo toast at a time for the whole app. Actions push to this store; the host (mounted
// once by the shell through the manage slot, a lazy chunk) renders it, so a toast outlives the row or dialog that
// raised it (a deleted course's group leaves the list while its Undo stays on screen).
import { useSyncExternalStore } from 'react';
import { UndoToast } from './primitives';

export interface ToastSpec {
  text: string;
  restoredText: string;
  onUndo: () => Promise<void>;
}

let current: (ToastSpec & { key: number }) | null = null;
let seq = 0;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

export const showUndo = (spec: ToastSpec): void => {
  seq += 1;
  current = { ...spec, key: seq };
  emit();
};

const close = (key: number) => {
  if (current?.key === key) {
    current = null;
    emit();
  }
};

export function ToastHost() {
  const toast = useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => {
        listeners.delete(l);
      };
    },
    () => current,
    () => current,
  );
  if (!toast) return null;
  return (
    <div className="undo-region">
      <UndoToast key={toast.key} text={toast.text} restoredText={toast.restoredText} onUndo={toast.onUndo} onClose={() => close(toast.key)} />
    </div>
  );
}

export default ToastHost;
