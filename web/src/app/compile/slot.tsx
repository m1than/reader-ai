// The app's compile slot (WEB-UI FR-UI-6, FR-UI-15): the owner's CompileControl renders through the slot's Status on
// every lesson row and on the lesson page; for anyone else the Status renders nothing, so the default status shows and
// no compile request is made. The control (with the library's Dialog and Chip) is a lazy chunk, loaded only for the
// owner, so the initial chunk stays within NFR-UI-1.
import { useQuery } from '@tanstack/react-query';
import { lazy, Suspense } from 'react';
import { useServices, type CompileSlot, type SlotProps } from '../context';
import { queries } from '../queries';

const CompileControl = lazy(() => import('./CompileControl').then((m) => ({ default: m.CompileControl })));

function OwnerCompileStatus({ lesson, place }: SlotProps) {
  const { api } = useServices();
  const me = useQuery(queries(api).me());
  if (me.data?.role !== 'owner') return null;
  return (
    <Suspense fallback={null}>
      <CompileControl lesson={lesson} source={place} />
    </Suspense>
  );
}

export const ownerCompileSlot: CompileSlot = { Status: OwnerCompileStatus };
