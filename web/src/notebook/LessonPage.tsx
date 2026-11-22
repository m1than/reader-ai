// The lesson route (WEB-NOTEBOOK FR-NOTEBOOK-1): metadata first, then the immutable latest revision; one kernel
// controller per open view, disposed when the view closes (FR-NOTEBOOK-4).
import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo } from 'react';
import { SIGN_IN_URL } from '../app/api';
import { useServices } from '../app/context';
import { queries } from '../app/queries';
import { browserSocketFactory, createKernelController } from './kernel';
import { LessonView, type Notebook } from './LessonView';

function Lesson({ lessonId, notebook }: { lessonId: string; notebook: Notebook }) {
  const { api, notify, navigateAway } = useServices();
  const controller = useMemo(
    () =>
      createKernelController({
        lessonId,
        api,
        openSocket: browserSocketFactory(window.location.host),
        schedule: (fn, ms) => {
          window.setTimeout(fn, ms);
        },
        navigateToSignIn: () => navigateAway(SIGN_IN_URL),
        notify,
        newMsgId: () => crypto.randomUUID().replaceAll('-', ''),
        now: () => new Date().toISOString(),
      }),
    [lessonId, api, notify, navigateAway],
  );
  useEffect(() => () => controller.dispose(), [controller]);
  return <LessonView notebook={notebook} controller={controller} />;
}

export function LessonPage({ lessonId }: { lessonId: string }) {
  const { api } = useServices();
  const q = queries(api);
  const meta = useQuery(q.lesson(lessonId));
  const sha = meta.data?.latest_sha256 ?? null;
  const rev = useQuery({ ...q.revision(lessonId, sha ?? ''), enabled: sha !== null });
  if (meta.isPending) return <p className="page muted">Loading…</p>;
  if (meta.isError) return <p className="page">The lesson could not be loaded.</p>;
  if (sha === null)
    return (
      <main className="page">
        <h1>{meta.data.title}</h1>
        <p className="muted">This lesson is not published yet.</p>
      </main>
    );
  if (rev.isPending) return <p className="page muted">Loading…</p>;
  if (rev.isError) return <p className="page">The lesson could not be loaded.</p>;
  return <Lesson lessonId={lessonId} notebook={rev.data as unknown as Notebook} />;
}
