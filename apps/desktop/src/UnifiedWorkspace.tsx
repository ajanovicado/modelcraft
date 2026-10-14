import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefCallback } from "react";
import { ArrowUp, ChevronRight, ExternalLink, FileBarChart2, FileSpreadsheet, Folder, HardDrive, Home, Loader2, Maximize2, Minimize2, MessageSquareText, PenLine, Search, X } from "lucide-react";
import { BROWSE_HOME_LABEL, filterBrowseEntries, pathSegments, quickAccessPaths, rememberedBrowsePath, rememberBrowsePath, type PathLink } from "./pathBrowser";
import type { BrowseEntry, BrowseResult } from "./types";
import { SegmentedControl } from "./SegmentedControl";

export type AuthoringView = "report" | "data" | "model" | "dax";
export type ConversationContext = { page_id?: string; visual_id?: string; table_id?: string; measure_id?: string; relationship_id?: string; label: string };

const workspaceViews = [{ value: "report", label: "Report" }, { value: "data", label: "Data" }, { value: "model", label: "Model" }, { value: "dax", label: "DAX Lab" }] as const;

export function UnifiedWorkspace({ view, onView, chatOpen, onChat, conversation, daxConversationRef, report, data, model, dax, toolbar, visible = true }: {
  view: AuthoringView; onView: (value: AuthoringView) => void; chatOpen: boolean; onChat: () => void;
  conversation: ReactNode; report: ReactNode; data: ReactNode; model: ReactNode; dax: ReactNode; toolbar: ReactNode; visible?: boolean;
  daxConversationRef?: RefCallback<HTMLDivElement>;
}) {
  const workspaceRef = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const root = workspaceRef.current;
    const contextBar = view === "dax" ? undefined : root?.querySelector<HTMLElement>(".chat-context-bar");
    if (!root) return;
    if (!chatOpen || !visible || !contextBar) {
      root.style.removeProperty("--workspace-context-row-height");
      return;
    }
    const alignHeaders = () => {
      if (contextBar.offsetHeight) root.style.setProperty("--workspace-context-row-height", `${contextBar.offsetHeight}px`);
    };
    const observer = new ResizeObserver(alignHeaders);
    observer.observe(contextBar);
    alignHeaders();
    return () => observer.disconnect();
  }, [chatOpen, conversation, visible, view]);
  const [chatExpanded, setChatExpanded] = useState(() => {
    try { return localStorage.getItem("studio.conversationSize.v1") === "expanded"; }
    catch { return false; }
  });
  const toggleChatSize = () => {
    const expanded = !chatExpanded;
    setChatExpanded(expanded);
    try { localStorage.setItem("studio.conversationSize.v1", expanded ? "expanded" : "compact"); }
    catch { /* Resizing still works when browser storage is unavailable. */ }
  };
  return <section ref={workspaceRef} className={`unified-workspace ${chatOpen ? "with-conversation" : ""}${chatExpanded ? " conversation-expanded" : ""}`} hidden={!visible} aria-label="Authoring workspace">
    <div className="unified-body">
      <aside id="workspace-conversation" className="unified-conversation" hidden={!chatOpen} aria-label={`${view === "dax" ? "DAX Lab" : "Workspace"} assistant`}>
        <div className="conversation-heading"><div className="assistant-heading-label"><strong>Local AI</strong><span className="assistant-scope" aria-label="Assistant context">{view === "dax" ? "DAX Lab" : "Workspace"}</span></div><div className="conversation-heading-actions">
          <button type="button" className="conversation-size-toggle" aria-label={chatExpanded ? "Collapse conversation" : "Expand conversation"} aria-expanded={chatExpanded} aria-controls="workspace-conversation" title={chatExpanded ? "Return to compact conversation" : "Expand conversation to one-third of the workspace"} onClick={toggleChatSize}>
            {chatExpanded ? <Minimize2 size={14} aria-hidden="true" /> : <Maximize2 size={14} aria-hidden="true" />}{chatExpanded ? "Collapse" : "Expand"}
          </button>
          <button type="button" className="icon-button" aria-label="Hide conversation" title="Hide conversation" onClick={onChat}><X size={16} /></button>
        </div></div>
        <div className="assistant-conversation-slot" hidden={view === "dax"}>{conversation}</div>
        <div className="assistant-conversation-slot" ref={daxConversationRef} hidden={view !== "dax"} />
      </aside>
      <div className="unified-workbench">
        <header className="unified-toolbar">
          <SegmentedControl mode="tabs" label="Workspace views" className="workspace-view-tabs" buttonClassName="workspace-view-tab" options={workspaceViews} value={view} onChange={onView} tabIdPrefix="workspace-tab-" panelIdPrefix="workspace-panel-" />
          <div className="unified-build-actions">
            {!chatOpen && <button type="button" className="secondary conversation-toggle" aria-label={`Show ${view === "dax" ? "DAX Lab" : "Workspace"} assistant`} aria-expanded={false} aria-controls="workspace-conversation" onClick={onChat}><MessageSquareText size={16} aria-hidden="true" />Assistant</button>}
            {toolbar}
          </div>
        </header>
        <div className="unified-content">
          <div id="workspace-panel-report" role="tabpanel" aria-labelledby="workspace-tab-report" hidden={view !== "report"}>{report}</div>
          <div id="workspace-panel-data" role="tabpanel" aria-labelledby="workspace-tab-data" hidden={view !== "data"}>{data}</div>
          <div id="workspace-panel-model" role="tabpanel" aria-labelledby="workspace-tab-model" hidden={view !== "model"}>{model}</div>
          <div id="workspace-panel-dax" role="tabpanel" aria-labelledby="workspace-tab-dax" hidden={view !== "dax"}>{dax}</div>
        </div>
      </div>
    </div>
  </section>;
}

/** Native dialog provides focus trapping, inert background, and Escape dismissal. */
export function ReviewDialog({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useLayoutEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const dialog = ref.current;
    dialog?.showModal();
    return () => { dialog?.close(); if (previous?.isConnected) previous.focus(); };
  }, []);
  return <dialog className="unified-review" ref={ref} aria-labelledby="review-title" onCancel={onClose}><header><div><h2 id="review-title">Review changes</h2><p>Your Power BI project stays unchanged until you build.</p></div><button type="button" className="icon-button" aria-label="Close review" onClick={onClose}><X size={18} /></button></header>{children}</dialog>;
}

/** Browser-mode path entry: the native picker only exists inside Tauri, and
 * window.prompt can be suppressed by embedded previews. When `onBrowse` is
 * provided the dialog becomes an Explorer-style picker backed by /v1/browse:
 * quick access and drives on the left, clickable breadcrumbs above, a list
 * where opening a folder is a click and selectable files fill the choice.
 * Typing a path stays available behind "Type path" for anyone who wants it.
 * Without `onBrowse` it remains a plain path field. */
const isPbixPath = (path: string) => /\.pbix$/i.test(path.trim());

export function PathPrompt({ title, description, placeholder, confirmLabel = "Continue", startPath, acceptPbix = false, reportOnly = false, onBrowse, onSubmit, onClose }: {
  title: string; description: string; placeholder?: string; confirmLabel?: string;
  startPath?: string;
  acceptPbix?: boolean;
  reportOnly?: boolean;
  onBrowse?: (path?: string) => Promise<BrowseResult>;
  onSubmit: (value: string) => void; onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [value, setValue] = useState("");
  const [browsePath, setBrowsePath] = useState("");
  const [parent, setParent] = useState<string | null>(null);
  const [entries, setEntries] = useState<BrowseEntry[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [browseError, setBrowseError] = useState<string>();
  const [browsing, setBrowsing] = useState(false);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string>();
  const [active, setActive] = useState<string>();
  const [quick, setQuick] = useState<PathLink[]>([]);
  const [drives, setDrives] = useState<PathLink[]>([]);
  const [addressOpen, setAddressOpen] = useState(false);
  const [address, setAddress] = useState("");
  const applyResult = useCallback((result: BrowseResult) => {
    setBrowsePath(result.path);
    setParent(result.parent);
    setEntries(result.entries);
    setTruncated(result.truncated);
    setSelected(undefined);
    if (result.path) { setValue(result.path); setAddress(result.path); rememberBrowsePath(result.path); }
  }, []);
  const load = useCallback((path?: string) => {
    if (!onBrowse) return;
    setBrowsing(true);
    setBrowseError(undefined);
    setQuery("");
    return onBrowse(path).then((result) => {
      applyResult(result);
      const normalize = (candidate: string) => candidate.trim().replace(/\\/g, "/").toLowerCase();
      const file = result.entries.find((entry) => entry.kind === "file" && normalize(entry.path) === normalize(path ?? ""));
      if (file && (file.selectable || isPbixPath(file.path))) {
        setSelected(file.path);
        setValue(file.path);
        setAddress(file.path);
      }
    }).catch((reason: unknown) => {
      setBrowseError(reason instanceof Error ? reason.message : "This folder cannot be listed");
      setEntries([]);
    }).finally(() => setBrowsing(false));
  }, [applyResult, onBrowse]);
  // Quick access mirrors Explorer: known user folders first, then drives. Both
  // come from the same read-only listing the list uses, so the sidebar never
  // offers a folder the local engine cannot open.
  useEffect(() => {
    const browse = onBrowse;
    if (!browse) return;
    let alive = true;
    void browse().then(async (root) => {
      if (!alive) return;
      const home = root.entries.find((entry) => entry.kind === "dir" && entry.name === BROWSE_HOME_LABEL)?.path;
      setDrives(root.entries
        .filter((entry) => entry.kind === "dir" && entry.path !== home)
        .map((entry) => ({ name: entry.name, path: entry.path })));
      if (!home) return;
      let links: PathLink[] = [{ name: BROWSE_HOME_LABEL, path: home }, ...quickAccessPaths(home)];
      try {
        const listing = await browse(home);
        const folders = new Set(listing.entries.filter((entry) => entry.kind === "dir").map((entry) => entry.name.toLocaleLowerCase()));
        links = links.filter((link) => link.name === BROWSE_HOME_LABEL || folders.has(link.name.toLocaleLowerCase()));
      } catch { /* Home alone is still a useful shortcut when it cannot be listed. */ }
      if (alive) setQuick(links);
    }).catch(() => { /* Quick access is optional; the list still works without it. */ });
    return () => { alive = false; };
  }, [onBrowse]);
  // Open where the user left off, or at This PC when nothing is remembered. A
  // remembered folder can be gone, so that case falls back instead of failing.
  useEffect(() => {
    const browse = onBrowse;
    if (!browse) return;
    const initial = startPath || rememberedBrowsePath();
    if (!initial) { void load(); return; }
    let alive = true;
    setBrowsing(true);
    setBrowseError(undefined);
    setQuery("");
    void browse(initial).then((result) => { if (alive) applyResult(result); })
      .catch(() => { if (alive) return load(); return undefined; })
      .finally(() => { if (alive) setBrowsing(false); });
    return () => { alive = false; };
  }, [applyResult, load, onBrowse, startPath]);
  useLayoutEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const dialog = ref.current;
    dialog?.showModal();
    (onBrowse ? listRef.current : inputRef.current)?.focus();
    return () => { dialog?.close(); if (previous?.isConnected) previous.focus(); };
  }, [onBrowse]);
  useEffect(() => { if (addressOpen) inputRef.current?.focus(); }, [addressOpen]);
  const visible = useMemo(() => filterBrowseEntries(entries, query), [entries, query]);
  useEffect(() => { setActive(visible[0]?.path); }, [visible]);
  const crumbs = useMemo(() => pathSegments(browsePath), [browsePath]);
  const activeIndex = visible.findIndex((entry) => entry.path === active);
  const goUp = () => { void load(parent ?? undefined); };
  const pbixSelected = isPbixPath(value) && value !== browsePath;
  const canSubmit = Boolean(value.trim()) && (!pbixSelected || acceptPbix) && (!reportOnly || pbixSelected);
  const submit = () => { if (canSubmit) onSubmit(value.trim()); };
  const openFolder = (entry: BrowseEntry) => { if (entry.path !== browsePath) void load(entry.path); };
  const openEntry = (entry: BrowseEntry) => {
    if (entry.kind === "dir") openFolder(entry);
    else if (entry.selectable || isPbixPath(entry.path)) { setActive(entry.path); setSelected(entry.path); setValue(entry.path); }
  };
  const move = (offset: number) => {
    if (!visible.length) return;
    const index = visible.findIndex((entry) => entry.path === active);
    const next = index === -1 ? (offset > 0 ? 0 : visible.length - 1) : Math.min(visible.length - 1, Math.max(0, index + offset));
    setActive(visible[next].path);
  };
  return <dialog className={`unified-review path-prompt${pbixSelected ? " with-pbix-help" : ""}`} ref={ref} aria-labelledby="path-prompt-title" onCancel={onClose}>
    <header><div><h2 id="path-prompt-title">{title}</h2><p>{description}</p></div>
      <button type="button" className="icon-button" aria-label="Cancel" onClick={onClose}><X size={18} /></button></header>
    <form onSubmit={(event) => { event.preventDefault(); submit(); }}>
      {!onBrowse && <label className="path-prompt-field"><span>Local path</span>
        <input ref={inputRef} value={value} onChange={(event) => setValue(event.target.value)} placeholder={placeholder} spellCheck={false} autoComplete="off" />
      </label>}
      {onBrowse && <div className="path-browser">
        <div className="path-browser-layout">
          <nav className="path-browser-places" aria-label="Quick access">
            {quick.map((link) => <button type="button" key={link.path} className="path-browser-place" title={link.path}
              aria-current={browsePath === link.path ? "true" : undefined} onClick={() => void load(link.path)}>
              {link.name === BROWSE_HOME_LABEL ? <Home size={15} aria-hidden="true" /> : <Folder size={15} aria-hidden="true" />}<span>{link.name}</span>
            </button>)}
            {quick.length > 0 && drives.length > 0 && <div className="path-browser-separator" role="presentation" />}
            {drives.map((drive) => <button type="button" key={drive.path} className="path-browser-place" title={drive.path}
              aria-current={browsePath === drive.path ? "true" : undefined} onClick={() => void load(drive.path)}>
              <HardDrive size={15} aria-hidden="true" /><span>{drive.name}</span>
            </button>)}
          </nav>
          <div className="path-browser-main">
            <div className="path-browser-bar">
              <button type="button" className="secondary path-browser-up" onClick={goUp} disabled={browsing || browsePath === ""} aria-label="Go up one folder">
                <ArrowUp size={15} aria-hidden="true" />Up
              </button>
              <nav className="path-browser-crumbs" aria-label="Current folder">
                {crumbs.length === 0
                  ? <span className="path-browser-crumb-current">This PC</span>
                  : crumbs.map((crumb, index) => <Fragment key={crumb.path}>
                    {index > 0 && <ChevronRight className="path-browser-crumb-separator" size={13} aria-hidden="true" />}
                    {index === crumbs.length - 1
                      ? <span className="path-browser-crumb-current" title={crumb.path}>{crumb.name}</span>
                      : <button type="button" className="path-browser-crumb" title={crumb.path} onClick={() => void load(crumb.path)}>{crumb.name}</button>}
                  </Fragment>)}
              </nav>
              <button type="button" className={`path-browser-address-toggle${addressOpen ? " active" : ""}`} aria-expanded={addressOpen} aria-controls="path-browser-address" title="Type a path instead" onClick={() => setAddressOpen((open) => !open)}>
                <PenLine size={14} aria-hidden="true" />Type path
              </button>
            </div>
            {addressOpen && <div className="path-prompt-input" id="path-browser-address">
              <label className="sr-only" htmlFor="path-browser-address-input">Local path</label>
              <input id="path-browser-address-input" ref={inputRef} value={address} placeholder={placeholder} spellCheck={false} autoComplete="off"
                onChange={(event) => { setAddress(event.target.value); setValue(event.target.value); }}
                onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); void load(address.trim() || undefined); } }} />
              <button type="button" className="secondary" onClick={() => void load(address.trim() || undefined)} disabled={browsing}>Go</button>
            </div>}
            <div className="path-browser-search">
              <Search size={15} aria-hidden="true" />
              <label className="sr-only" htmlFor="path-browser-search-input">Search this folder</label>
              <input id="path-browser-search-input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search this folder" spellCheck={false} autoComplete="off" />
            </div>
            <div ref={listRef} className="path-browser-list" role="listbox" tabIndex={0} aria-label="Folders and files"
              aria-activedescendant={activeIndex >= 0 ? `path-option-${activeIndex}` : undefined}
              onKeyDown={(event) => {
                if (event.altKey && event.key === "ArrowUp") { event.preventDefault(); goUp(); return; }
                if (event.key === "Backspace") { event.preventDefault(); goUp(); return; }
                if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); listRef.current?.focus(); move(event.key === "ArrowDown" ? 1 : -1); return; }
                if (event.key === "Home") { event.preventDefault(); listRef.current?.focus(); setActive(visible[0]?.path); return; }
                if (event.key === "End") { event.preventDefault(); listRef.current?.focus(); setActive(visible.at(-1)?.path); return; }
                const current = visible.find((entry) => entry.path === active);
                if (!current) return;
                if (event.key === "ArrowRight" && current.kind === "dir") { event.preventDefault(); openFolder(current); return; }
                if ((event.key === "Enter" || event.key === " ") && event.target === listRef.current) { event.preventDefault(); openEntry(current); }
              }}>
              {browsing && <div className="path-browser-status"><Loader2 size={15} className="spin" aria-hidden="true" />Loading…</div>}
              {!browsing && browseError && <div className="path-browser-status">{browseError}</div>}
              {!browsing && !browseError && visible.map((entry, index) => (
                <button type="button" key={entry.path} id={`path-option-${index}`} role="option" aria-selected={selected === entry.path}
                  aria-disabled={entry.kind === "file" && !entry.selectable && !isPbixPath(entry.path) ? true : undefined}
                  className={`path-browser-row path-browser-${entry.kind}${active === entry.path ? " path-browser-active" : ""}`}
                  title={entry.kind === "file" && isPbixPath(entry.path) ? "View options for this Power BI report" : entry.kind === "file" && !entry.selectable ? "Only CSV, Excel, Parquet and PBIP files can be selected" : `${entry.path} — open`}
                  disabled={entry.kind === "file" && !entry.selectable && !isPbixPath(entry.path)} onClick={() => openEntry(entry)}>
                  {entry.kind === "dir" ? <Folder size={15} aria-hidden="true" /> : isPbixPath(entry.path) ? <FileBarChart2 size={15} aria-hidden="true" /> : <FileSpreadsheet size={15} aria-hidden="true" />}
                  <span className="path-browser-name">{entry.name}</span>
                  {entry.kind === "dir"
                    ? <span className="path-browser-open" aria-hidden="true"><ChevronRight size={14} /></span>
                    : isPbixPath(entry.path) ? <span className="path-browser-tag">Power BI report</span> : entry.selectable ? null : <span className="path-browser-tag">not supported</span>}
                </button>
              ))}
              {!browsing && !browseError && !visible.length && <div className="path-browser-status">{query.trim() ? `No match for “${query.trim()}” in this folder` : "This folder is empty"}</div>}
            </div>
            {truncated && <p className="path-browser-note">Showing the first 500 entries only. Search or open a subfolder to narrow the list.</p>}
            <p className="path-browser-hint">{reportOnly ? "Choose a .pbix file to open its report and embedded data in Studio." : "CSV, Excel and Parquet contain importable data. PBIP projects can be inspected for compatibility. Select a PBIX to open its report."}</p>
          </div>
        </div>
      </div>}
      {pbixSelected && <section className="path-browser-report-help" aria-labelledby="pbix-help-title" aria-live="polite">
        <FileBarChart2 size={21} aria-hidden="true" />
        <div>
          <h3 id="pbix-help-title">{acceptPbix ? "Open this report in Studio" : "Import this report from Choose local data"}</h3>
          <p id="pbix-help-description">Import the original pages, visual definitions, measures, relationships and embedded tables into a new workspace. Studio preserves the source file. Native visuals and complex DAX can be opened in Power BI Desktop.</p>
          <details>
            <summary>Check a PBIP project in Studio</summary>
            <p>Use <strong>File → Save As → Power BI Project (.pbip)</strong> in Desktop, then find <strong>Inspect existing PBIP</strong> in Studio’s command search. This checks compatibility; it does not load the existing report into Studio.</p>
            <a href="https://learn.microsoft.com/en-us/power-bi/developer/projects/projects-overview#save-as-a-project" target="_blank" rel="noreferrer">Microsoft’s PBIP setup guide <ExternalLink size={13} aria-hidden="true" /></a>
          </details>
        </div>
      </section>}
      <div className="path-prompt-actions">
        {onBrowse && <p className="path-browser-choice" title={value || undefined}>{value ? <>Selected: <strong>{value}</strong></> : "Nothing selected yet"}</p>}
        <button type="button" className="secondary" onClick={onClose}>Cancel</button>
        <button type="submit" className="primary" disabled={!canSubmit} aria-describedby={pbixSelected ? "pbix-help-description" : undefined}>{pbixSelected && acceptPbix ? "Open report" : confirmLabel}</button>
      </div>
    </form>
  </dialog>;
}

export function ReviewChecklist({ items }: { items: Array<{ id: string; label: string; detail: string; status: "ready" | "action" | "blocked" }> }) {
  return <ol className="review-checklist" aria-label="Review checklist">
    {items.map((item, index) => <li key={item.id} id={`review-check-${item.id}`} className={`review-check review-check-${item.status}`}>
      <span className="review-check-index" aria-hidden="true">{index + 1}</span>
      <span><strong>{item.label}</strong><small>{item.detail}</small></span>
    </li>)}
  </ol>;
}
