// The upload sections' pure rules (WEB-IMPORT FR-IMPORT-1, -3, INV-IMPORT-1, -2): client checks, the language
// default, the controls each state allows, and the step list.
import type { Language, UploadState, UploadView } from './api';

export const UPLOAD_BYTES_MAX = 209_715_200; // 200 MiB, SRV-INGEST FR-INGEST-18
export const POLL_MS = 2000;
export const CONVERTER_POLL_MS = 10_000;

const HEADER = '%PDF-';

/** FR-IMPORT-1's checks in order; the first 5 bytes are the only bytes of the file the page ever reads. */
export const checkFile = async (files: readonly File[]): Promise<string | null> => {
  if (files.length !== 1) return 'one_file';
  const file = files[0]!;
  if (!file.name.toLowerCase().endsWith('.pdf') && file.type !== 'application/pdf') return 'not_pdf';
  if (file.size === 0) return 'empty';
  if (file.size > UPLOAD_BYTES_MAX) return 'too_large';
  const head = new Uint8Array(await file.slice(0, 5).arrayBuffer());
  return String.fromCharCode(...head) === HEADER ? null : 'not_pdf';
};

export const defaultLanguage = (navLanguage: string): Language => {
  const lower = navLanguage.toLowerCase();
  if (lower.startsWith('ru')) return 'ru';
  if (lower.startsWith('en')) return 'en';
  return 'other';
};

export type Control = 'Cancel' | 'Retry' | 'Discard';

/** INV-IMPORT-2: exactly the controls a view's state allows. */
export const controls = (view: UploadView): Control[] => {
  if (view.state === 'queued' || view.state === 'claimed' || view.state === 'converting') return ['Cancel'];
  if (view.state !== 'failed') return [];
  if (view.retry === 'reconvert') return ['Retry', 'Discard'];
  if (view.retry === 'requeue') return ['Retry'];
  return [];
};

export const STEPS = ['Uploaded', 'Converting on your Mac', 'Importing', 'Indexing for search', 'Ready'] as const;

/** The current step of a state (failed keeps the step it failed at: conversion unless the import had started). */
export const stepOf = (state: UploadState, retry: UploadView['retry'] = null): number => {
  switch (state) {
    case 'queued':
      return 0;
    case 'claimed':
    case 'converting':
    case 'sending':
      return 1;
    case 'importing':
      return 2;
    case 'indexing':
      return 3;
    case 'ready':
      return 4;
    case 'failed':
      return retry === 'requeue' ? 2 : 1;
  }
};

export const terminal = (state: UploadState): boolean => state === 'ready' || state === 'failed';
