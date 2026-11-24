// The /import page (WEB-UI FR-UI-12, FR-UI-13 mount note): WEB-PLAN's page unchanged (its one h1 "Import a source"
// and "Your sources"), then WEB-IMPORT's upload sections. When an upload is ready, "Your sources" is refreshed so the
// new source offers "Plan study time".
import { useQueryClient } from '@tanstack/react-query';
import { useServices } from '../app/context';
import { ImportPage as PlanImportPage } from './plan/ImportPage';
import { UploadSections } from './UploadSections';

export function ImportPage() {
  const { upload } = useServices();
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
