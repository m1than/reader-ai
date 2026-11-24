// WEB-MANAGE's client of the management routes (SRV-LESSON FR-LESSON-21..31, SRV-PIPELINE FR-PIPELINE-20..21,
// SRV-PLAN FR-PLAN-20, SRV-TUTOR FR-TUTOR-20, SRV-INGEST FR-INGEST-30..31, SRV-AUTH FR-AUTH-40..48, SRV-CONFIG
// FR-CONFIG-25..26, SRV-EVENTS FR-EVENTS-31). Same origin, a JSON body on every write (FR-UI-4; deletes carry `{}`),
// every response validated at run time. A 401 `reauth_required` is the page's to show; any other 401 goes to the
// shell's sign-in redirect (FR-MANAGE-1).
import * as v from 'valibot';
import { ApiError, type ApiDeps } from '../app/api';

const str = v.string();
const nstr = v.nullable(v.string());
const num = v.number();

const Course = v.looseObject({
  course_id: str,
  title: str,
  description: v.optional(nstr, null),
  position: v.optional(v.nullable(num), null),
  state: str,
  archived_at: v.optional(nstr, null),
  deleted_at: v.optional(nstr, null),
  version: num,
  lesson_count: v.optional(num, 0),
  material_id: v.optional(nstr, null),
  plan: v.optional(v.nullable(v.looseObject({ total_weeks: num, minutes_per_day: num, start_date: str, finish_by: v.optional(nstr, null) })), null),
});
const Courses = v.object({ courses: v.array(Course) });
const DeletedLessons = v.object({ lessons: v.array(v.looseObject({ lesson_id: str, title: str, ordinal: num, deleted_at: str })) });
const Impact = v.looseObject({ lessons: num, revisions: num, tutor_questions: num, compile_runs: num, spend_rows: v.optional(num, 0), open_runs: v.optional(num, 0) });
const Any = v.unknown();
const StoredPlan = v.looseObject({
  course_id: str,
  inputs: v.looseObject({ material_id: str, total_weeks: num, minutes_per_day: num, rate: str, start_date: str }),
  plan_sha256: str,
});
const Runs = v.object({
  runs: v.array(v.looseObject({ run_id: str, state: str, created_at: str, finished_at: nstr, spent_usd: str, cap_usd: str, steps: num, error: v.optional(nstr, null) })),
});
const Material = v.looseObject({
  id: str,
  status: str,
  page_count: v.nullable(num),
  source: v.optional(str, 'config'),
  title: v.optional(nstr, null),
  display_title: v.optional(nstr, null),
  version: v.optional(v.nullable(num), null),
  courses: v.optional(v.array(v.looseObject({ course_id: str, title: str })), []),
  deleted_at: v.optional(nstr, null),
});
const Materials = v.object({ materials: v.array(Material) });
const MaterialImpact = v.looseObject({ courses: v.array(v.looseObject({ course_id: str, title: str })), lessons: num, uploads_open: num });
const Restored = v.looseObject({ restored_courses: v.optional(v.array(v.looseObject({ course_id: str, title: str })), []) });
const Passkey = v.looseObject({ pid: str, label: nstr, created_at: str, last_used_at: nstr });
const Users = v.object({
  users: v.array(
    v.looseObject({
      id: str,
      handle: str,
      display_name: str,
      role: str,
      source: str,
      state: str,
      disabled_at: nstr,
      locked: v.boolean(),
      created_at: str,
      last_signin_at: nstr,
      passkeys: v.array(Passkey),
      sessions_active: num,
      owns_data: v.boolean(),
    }),
  ),
});
const Credentials = v.object({ credentials: v.array(v.looseObject({ ...Passkey.entries, backed_up: v.boolean(), transports: v.optional(v.array(str), []) })) });
const Sessions = v.object({ sessions: v.array(v.looseObject({ sid: str, created_at: str, expires_at: str, current: v.boolean() })) });
const Scalar = v.union([str, num, v.boolean(), v.null()]);
const Settings = v.object({
  settings: v.array(
    v.looseObject({
      name: str,
      value: Scalar,
      file_value: v.optional(Scalar, null),
      source: str,
      key: nstr,
      read_when: str,
      overridable: v.optional(v.boolean(), false),
      version: v.optional(num, 0),
      ignored: v.optional(nstr, null),
    }),
  ),
});
const Audit = v.object({
  rows: v.array(v.looseObject({ id: num, at: str, actor_id: nstr, action: str, object_kind: str, object_id: str, detail: v.optional(Any) })),
  next_before: v.nullable(num),
});
const Note = v.looseObject({ note_id: str, cell_id: nstr, body: str, created_at: str, updated_at: str });
const Notes = v.object({ notes: v.array(Note) });
const Bookmarks = v.object({ bookmarks: v.array(v.looseObject({ lesson_id: str, cell_id: str, created_at: str, stale: v.boolean() })) });
const Cleared = v.looseObject({ cleared: num });
const Progress = v.looseObject({ done: v.boolean(), done_at: nstr });
const Revert = v.looseObject({ applied: v.boolean(), latest_sha256: v.optional(nstr, null) });

export type Course = v.InferOutput<typeof Course>;
export type DeletedLesson = v.InferOutput<typeof DeletedLessons>['lessons'][number];
export type Impact = v.InferOutput<typeof Impact>;
export type RunRow = v.InferOutput<typeof Runs>['runs'][number];
export type Material = v.InferOutput<typeof Material>;
export type MaterialImpact = v.InferOutput<typeof MaterialImpact>;
export type Person = v.InferOutput<typeof Users>['users'][number];
export type Credential = v.InferOutput<typeof Credentials>['credentials'][number];
export type Session = v.InferOutput<typeof Sessions>['sessions'][number];
export type Setting = v.InferOutput<typeof Settings>['settings'][number];
export type AuditPage = v.InferOutput<typeof Audit>;
export type Note = v.InferOutput<typeof Note>;
export type Bookmark = v.InferOutput<typeof Bookmarks>['bookmarks'][number];
export type CourseState = 'active' | 'archived' | 'deleted';

const seg = encodeURIComponent;
type Method = 'GET' | 'POST' | 'DELETE';

export const createManageApi = (deps: ApiDeps) => {
  const call = async <T>(method: Method, path: string, schema: v.GenericSchema<unknown, T> | null, body?: unknown): Promise<T> => {
    const init: RequestInit = { method, credentials: 'same-origin' };
    if (method !== 'GET') {
      init.body = JSON.stringify(body ?? {});
      init.headers = { 'Content-Type': 'application/json' };
    }
    let res: Response;
    try {
      res = await deps.fetch(path, init);
    } catch {
      throw new ApiError(0, 'network');
    }
    if (!res.ok) {
      const err = (await res.json().catch(() => null)) as { error?: unknown } | null;
      const code = typeof err?.error === 'string' ? err.error : `http_${res.status}`;
      if (res.status === 401 && code !== 'reauth_required') deps.onUnauthorized();
      throw new ApiError(res.status, code);
    }
    if (schema === null || res.status === 204) return undefined as T;
    const parsed = v.safeParse(schema, await res.json().catch(() => undefined));
    if (!parsed.success) throw new ApiError(res.status, 'bad_response');
    return parsed.output;
  };
  return {
    courses: (state: CourseState) => call('GET', state === 'active' ? '/api/courses' : `/api/courses?state=${state}`, Courses),
    deletedLessons: (courseId: string) => call('GET', `/api/courses/${seg(courseId)}/lessons?state=deleted`, DeletedLessons),
    courseImpact: (courseId: string) => call('GET', `/api/courses/${seg(courseId)}/impact`, Impact),
    editCourse: (courseId: string, body: { version: number; title?: string; description?: string }) => call('POST', `/api/courses/${seg(courseId)}`, Course, body),
    orderCourses: (ids: string[]) => call('POST', '/api/courses/order', null, { course_ids: ids }),
    archive: (courseId: string) => call('POST', `/api/courses/${seg(courseId)}/archive`, null, {}),
    unarchive: (courseId: string, at: 'previous' | 'first') => call('POST', `/api/courses/${seg(courseId)}/unarchive`, null, { at }),
    deleteCourse: (courseId: string, confirm: string) => call('DELETE', `/api/courses/${seg(courseId)}`, null, { confirm }),
    restoreCourse: (courseId: string, at: 'previous' | 'first') => call('POST', `/api/courses/${seg(courseId)}/restore`, null, { at }),
    storedPlan: (courseId: string) => call('GET', `/api/plans?course_id=${seg(courseId)}`, StoredPlan),
    orderLessons: (courseId: string, ids: string[]) => call('POST', `/api/courses/${seg(courseId)}/lessons/order`, null, { lesson_ids: ids }),
    renameLesson: (lessonId: string, version: number, title: string) => call('POST', `/api/lessons/${seg(lessonId)}`, null, { version, title }),
    skip: (lessonId: string, skip: boolean) => call('POST', `/api/lessons/${seg(lessonId)}/${skip ? 'skip' : 'unskip'}`, null, {}),
    deleteLesson: (lessonId: string) => call('DELETE', `/api/lessons/${seg(lessonId)}`, null, {}),
    restoreLesson: (lessonId: string) => call('POST', `/api/lessons/${seg(lessonId)}/restore`, null, {}),
    progress: (lessonId: string, done: boolean) => call('POST', `/api/lessons/${seg(lessonId)}/progress`, Progress, { done }),
    revert: (lessonId: string, sha256: string) => call('POST', `/api/lessons/${seg(lessonId)}/revert`, Revert, { sha256 }),
    history: (lessonId: string) => call('GET', `/api/lessons/${seg(lessonId)}/compiles`, Runs),
    cancelRun: (runId: string) => call('POST', `/api/compiles/${seg(runId)}/cancel`, null, {}),
    clearTutor: (lessonId: string) => call('POST', `/api/lessons/${seg(lessonId)}/questions/clear`, Cleared, {}),
    materials: (deleted = false) => call('GET', deleted ? '/api/materials?state=deleted' : '/api/materials', Materials),
    materialImpact: (id: string) => call('GET', `/api/materials/${seg(id)}/impact`, MaterialImpact),
    renameMaterial: (id: string, version: number, title: string) => call('POST', `/api/materials/${seg(id)}`, null, { version, title }),
    deleteMaterial: (id: string, confirm: string, cascade: boolean) =>
      call('DELETE', `/api/materials/${seg(id)}`, null, cascade ? { confirm, cascade: true } : { confirm }),
    restoreMaterial: (id: string) => call('POST', `/api/materials/${seg(id)}/restore`, Restored, {}),
    people: () => call('GET', '/api/auth/users', Users),
    revokePerson: (id: string) => call('POST', `/api/auth/users/${seg(id)}/sessions/revoke`, null, {}),
    removePersonPasskey: (id: string, pid: string) => call('DELETE', `/api/auth/users/${seg(id)}/credentials/${seg(pid)}`, null, {}),
    deletePerson: (id: string, confirm: string) => call('DELETE', `/api/auth/users/${seg(id)}`, null, { confirm }),
    disablePerson: (id: string) => call('POST', `/api/auth/signups/${seg(id)}/disable`, null, {}),
    enablePerson: (id: string) => call('POST', `/api/auth/signups/${seg(id)}/enable`, null, {}),
    credentials: () => call('GET', '/api/auth/credentials', Credentials),
    labelCredential: (pid: string, label: string) => call('POST', `/api/auth/credentials/${seg(pid)}`, null, { label }),
    removeCredential: (pid: string) => call('DELETE', `/api/auth/credentials/${seg(pid)}`, v.looseObject({ revoked_sessions: v.optional(num, 0) }), {}),
    sessions: () => call('GET', '/api/auth/sessions', Sessions),
    revokeSession: (sid: string) => call('DELETE', `/api/auth/sessions/${seg(sid)}`, null, {}),
    revokeOthers: () => call('POST', '/api/auth/sessions/revoke-others', null, {}),
    settings: () => call('GET', '/api/settings', Settings),
    setSetting: (key: string, value: string | number, version: number) => call('POST', `/api/settings/${seg(key)}`, null, { value, version }),
    resetSetting: (key: string) => call('DELETE', `/api/settings/${seg(key)}`, null, {}),
    audit: (before: number | null) => call('GET', before === null ? '/api/audit' : `/api/audit?before=${before}`, Audit),
    notes: (lessonId: string) => call('GET', `/api/lessons/${seg(lessonId)}/notes`, Notes),
    addNote: (lessonId: string, body: string, cellId: string | null) =>
      call('POST', `/api/lessons/${seg(lessonId)}/notes`, Note, cellId ? { body, cell_id: cellId } : { body }),
    editNote: (lessonId: string, noteId: string, body: string) => call('POST', `/api/lessons/${seg(lessonId)}/notes/${seg(noteId)}`, null, { body }),
    deleteNote: (lessonId: string, noteId: string) => call('DELETE', `/api/lessons/${seg(lessonId)}/notes/${seg(noteId)}`, null, {}),
    bookmarks: () => call('GET', '/api/bookmarks', Bookmarks),
    bookmark: (lessonId: string, cellId: string, on: boolean) => call(on ? 'POST' : 'DELETE', `/api/lessons/${seg(lessonId)}/bookmarks/${seg(cellId)}`, null, {}),
  };
};

export type ManageApi = ReturnType<typeof createManageApi>;
