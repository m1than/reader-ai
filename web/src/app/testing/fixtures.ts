// Test helper: reads spec fixtures (spec/fixtures/**) and repository files by repository-relative path.
// Vitest runs with web/ as the working directory (npm test in web/).
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { parse } from 'yaml';

export const webRoot = `${resolve(process.cwd())}/`;
export const repoRoot = `${resolve(process.cwd(), '..')}/`;

export const readText = (rel: string): string => readFileSync(`${repoRoot}${rel}`, 'utf8');
export const readJson = <T = unknown>(rel: string): T => JSON.parse(readText(rel)) as T;
export const readYaml = <T = unknown>(rel: string): T => parse(readText(rel)) as T;
