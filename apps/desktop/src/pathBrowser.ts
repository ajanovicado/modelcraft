import type { BrowseEntry } from "./types";

/**
 * Helpers behind the in-app picker (see PathPrompt in UnifiedWorkspace).
 *
 * They stay pure so the breadcrumb, quick-access and search behaviour can be
 * covered without a running local engine: the engine only ever returns names
 * and kinds (see /v1/browse), and these helpers turn that into the
 * Explorer-style controls most people already know.
 */

export type PathLink = { name: string; path: string };

export const BROWSE_PATH_STORAGE_KEY = "studio.browsePath.v1";
export const BROWSE_HOME_LABEL = "Home";
export const QUICK_ACCESS_FOLDERS = ["Desktop", "Documents", "Downloads"] as const;

/** Split an absolute Windows, UNC or POSIX path into clickable breadcrumbs. */
export function pathSegments(path: string): PathLink[] {
  const trimmed = path.trim();
  if (!trimmed) return [];
  const separator = trimmed.includes("\\") ? "\\" : "/";
  const drive = /^([A-Za-z]:)/.exec(trimmed);
  const unc = !drive && trimmed.startsWith("\\\\");
  const base = drive ? `${drive[1]}${separator}` : unc ? separator.repeat(2) : trimmed.startsWith("/") ? separator : "";
  const names = (unc ? trimmed.slice(2) : drive ? trimmed.slice(drive[1].length) : trimmed).split(/[\\/]+/).filter(Boolean);
  const crumbs: PathLink[] = base ? [{ name: base, path: base }] : [];
  for (const name of names) {
    const previous = crumbs.at(-1)?.path ?? "";
    const next = !previous || previous.endsWith(separator) ? `${previous}${name}` : `${previous}${separator}${name}`;
    crumbs.push({ name, path: next });
  }
  return crumbs;
}

/** Desktop/Documents/Downloads under `home`, the folders Explorer also pins. */
export function quickAccessPaths(home: string): PathLink[] {
  const separator = home.includes("\\") ? "\\" : "/";
  const base = home.replace(/[\\/]+$/, "");
  if (!base) return [];
  return QUICK_ACCESS_FOLDERS.map((name) => ({ name, path: `${base}${separator}${name}` }));
}

/** Case-insensitive name filter behind the picker's search box. */
export function filterBrowseEntries(entries: BrowseEntry[], query: string): BrowseEntry[] {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return entries;
  return entries.filter((entry) => entry.name.toLocaleLowerCase().includes(needle));
}

/** Last folder the picker opened, so the next dialog opens where they left off. */
export function rememberedBrowsePath(): string | undefined {
  try { return localStorage.getItem(BROWSE_PATH_STORAGE_KEY) || undefined; } catch { return undefined; }
}

export function rememberBrowsePath(path: string): void {
  try { localStorage.setItem(BROWSE_PATH_STORAGE_KEY, path); } catch { /* Remembering the folder is optional. */ }
}