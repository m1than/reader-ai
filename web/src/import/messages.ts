// One plain-text catalogue keyed by code (WEB-IMPORT FR-IMPORT-7): client checks, SRV-INGEST codes, network and
// response failures, and an `unknown` fallback. No server text is ever rendered.
const CATALOGUE: Record<string, string> = {
  // client checks (FR-IMPORT-1)
  one_file: 'Choose one file at a time.',
  not_pdf: 'This file is not a PDF.',
  empty: 'This file is empty.',
  too_large: 'This file is larger than 200 MiB.',
  // upload replies (SRV-INGEST FR-INGEST-18)
  language: 'Choose the language of the source.',
  filename: 'The file name cannot be used. Rename the file and try again.',
  owner_only: 'Only the owner can import sources.',
  body_timeout: 'The upload took too long. Try again.',
  upload_busy: 'Another upload is in progress. Try again when it ends.',
  inbox_full: 'The upload queue is full. Discard failed uploads, cancel queued ones, or wait for them to finish.',
  id_collision: 'A source with the same short id already exists.',
  content_type: 'The upload was not sent as a PDF.',
  config_unavailable: 'The server cannot read its configuration; check reader.yaml',
  inbox_invalid: "The server's upload folder is not usable; see the server log",
  disconnected: 'The upload was interrupted. Try again.',
  // conversion and import failures (SRV-INGEST FR-INGEST-19, -20)
  host_lost: 'The converter on your Mac stopped answering three times.',
  convert_failed: 'The converter could not convert this file.',
  memory_limit: 'The conversion used more memory than allowed.',
  work_limit: 'The conversion wrote more data than allowed.',
  timeout: 'The conversion took too long.',
  fetch_mismatch: 'The file reached your Mac damaged. Retry the upload.',
  inbox_missing: 'The uploaded file is missing on the server. Discard it and upload it again.',
  receive_failed: 'The server refused the converted file.',
  bad_reply: 'The converter received an unexpected reply from the server.',
  import_missing: 'The import was removed on the server. Retry to convert again.',
  artifact_missing: 'The converted file is missing on the server.',
  artifact_invalid: 'The converted file on the server is damaged.',
  upload_missing: 'The upload was removed before its import.',
  version_frozen: 'This source was imported with another converter version.',
  EmbedUnavailable: 'Indexing for search could not reach the embedding service.',
  // Retry and other replies
  busy: 'Already restarting; try again in a moment',
  state: 'This upload has changed. The page shows its current state.',
  not_found: 'This upload no longer exists',
  unauthenticated: 'Your session has ended. Sign in again.',
  network: 'The server cannot be reached. Check the connection and retry.',
  bad_response: 'The server sent an unexpected response.',
  unknown: 'Something went wrong.',
};

export const CODES: readonly string[] = Object.keys(CATALOGUE);

export const message = (code: string): string => CATALOGUE[code] ?? CATALOGUE.unknown!;

/** FR-IMPORT-3: the cross-run residual of SRV-INGEST FR-INGEST-22 has no other signal than this symptom. */
export const MEMORY_REMEDY = 'If files that used to convert now fail with this, restart your Mac';
