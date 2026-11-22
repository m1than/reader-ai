// TanStack Query options (WEB-UI FR-UI-4): lists and lesson metadata are fresh for 30 s (lessons change only on
// republish); a revision is immutable, so it is fresh forever under its sha256.
import { queryOptions } from '@tanstack/react-query';
import type { Api } from './api';

export const LIST_STALE_MS = 30_000;

export const queries = (api: Api) => ({
  me: () => queryOptions({ queryKey: ['me'], queryFn: () => api.me(), staleTime: LIST_STALE_MS }),
  lessons: () => queryOptions({ queryKey: ['lessons'], queryFn: () => api.listLessons(), staleTime: LIST_STALE_MS }),
  lesson: (lessonId: string) =>
    queryOptions({ queryKey: ['lesson', lessonId], queryFn: () => api.getLesson(lessonId), staleTime: LIST_STALE_MS }),
  revision: (lessonId: string, sha256: string) =>
    queryOptions({ queryKey: ['revision', lessonId, sha256], queryFn: () => api.getRevision(lessonId, sha256), staleTime: Infinity }),
});
