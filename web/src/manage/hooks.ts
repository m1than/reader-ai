// WEB-MANAGE shared hooks: the client built from the shell's fetch and 401 hook, the queries the pages share, and the
// owner check (INV-MANAGE-2: owner-only reads run only for the owner).
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo } from 'react';
import { useServices } from '../app/context';
import { queries } from '../app/queries';
import { createManageApi, type CourseState } from './api';

export const useManageApi = () => {
  const { http } = useServices();
  // Stories without the shell's deps get a client on the global fetch.
  return useMemo(() => createManageApi(http ?? { fetch: (...a) => globalThis.fetch(...a), onUnauthorized: () => undefined }), [http]);
};

export const useOwner = (): boolean => {
  const { api } = useServices();
  return useQuery(queries(api).me()).data?.role === 'owner';
};

export const KEYS = {
  courses: (state: CourseState) => ['manage', 'courses', state] as const,
  deletedLessons: (courseId: string) => ['manage', 'deleted-lessons', courseId] as const,
  materials: (deleted: boolean) => ['manage', 'materials', deleted ? 'deleted' : 'active'] as const,
  people: ['manage', 'people'] as const,
  credentials: ['manage', 'credentials'] as const,
  sessions: ['manage', 'sessions'] as const,
  settings: ['manage', 'settings'] as const,
  audit: ['manage', 'audit'] as const,
  notes: (lessonId: string) => ['manage', 'notes', lessonId] as const,
  bookmarks: ['manage', 'bookmarks'] as const,
};

export const useCourses = (state: CourseState, enabled = true) => {
  const api = useManageApi();
  return useQuery({ queryKey: KEYS.courses(state), queryFn: () => api.courses(state), enabled, staleTime: 30_000 });
};

/** After a change to courses or lessons: the shell's lists and every management list refetch. */
export const useRefresh = () => {
  const client = useQueryClient();
  return () =>
    Promise.all([
      client.invalidateQueries({ queryKey: ['lessons'] }),
      client.invalidateQueries({ queryKey: ['lesson'] }),
      client.invalidateQueries({ queryKey: ['manage'] }),
      client.invalidateQueries({ queryKey: ['plan', 'materials'] }),
    ]).then(() => undefined);
};
