// The /import page (WEB-UI FR-UI-12, FR-UI-13 mount note): WEB-PLAN's page unchanged (its one h1 "Import a source"
// and "Your sources"), then WEB-IMPORT's upload sections. When an upload is ready, "Your sources" is refreshed so the
// new source offers "Plan study time".
import { useQueryClient } from '@tanstack/react-query';
import { useMemo } from 'react';
import { useServices } from '../app/context';
import { createUploadApi } from './api';
import { ImportPage as PlanImportPage } from './plan/ImportPage';
import { UploadSections } from './UploadSections';

export function ImportPage() {
  // NFR-UI-1: the upload client is built here, in this lazy chunk, from the shell's fetch and 401 hook.
  const { upload: given, http } = useServices();
  const upload = useMemo(() => given ?? (http ? createUploadApi(http) : undefined), [given, http]);
  const client = useQueryClient();
  return (
    <>
      <PlanImportPage />
      {upload ? (
        <div className="page import-page">
          <UploadSections api={upload} onReady={() => void client.invalidateQueries({ queryKey: ['plan', 'materials'] })} />
        </div>
      ) : null}
    </>
  );
}
