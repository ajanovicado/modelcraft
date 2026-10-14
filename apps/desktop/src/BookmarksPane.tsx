import { useState } from "react";
import { ArrowDown, ArrowUp, Bookmark as BookmarkIcon, FolderPlus, FolderX, GripVertical, Pencil, Play, Plus, RefreshCw, Trash2 } from "lucide-react";
import { EditorDialog } from "./EditorDialog";
import type { Operation } from "./importedEditing";
import {
  addBookmarkOp, bookmarkTree, bookmarkUsers, DEFAULT_CAPTURE, moveBookmarkOp, nextBookmarkName, nudgeBookmarkOp, updateBookmarkOp,
  type Bookmark, type CaptureOptions, type LayerVisual,
} from "./importedPanes";

function CaptureChoices({ value, selectedCount, onChange }: { value: CaptureOptions; selectedCount: number; onChange: (next: CaptureOptions) => void }) {
  return <fieldset className="ie-capture">
    <legend>What the bookmark remembers</legend>
    <label><input type="checkbox" checked={value.data} onChange={event => onChange({ ...value, data: event.target.checked })} />Data (filters, slicers, sorts)</label>
    <label><input type="checkbox" checked={value.display} onChange={event => onChange({ ...value, display: event.target.checked })} />Display (which visuals are shown)</label>
    <label><input type="checkbox" checked={value.currentPage} onChange={event => onChange({ ...value, currentPage: event.target.checked })} />Current page (opening it goes to this page)</label>
    <label><input type="checkbox" checked={value.selectedOnly} disabled={!selectedCount && !value.selectedOnly} onChange={event => onChange({ ...value, selectedOnly: event.target.checked })} />Selected visuals only{selectedCount ? ` (${selectedCount})` : ""}</label>
    {value.data && <small className="ie-note">A bookmark that remembers data resets every filter it did not capture when it is opened.</small>}
  </fieldset>;
}

/**
 * The Bookmarks pane (Desktop's View > Bookmarks): add one from the page as it is now with Desktop's capture options,
 * rename, reorder (drag, or the arrow buttons for keyboard users), group, update, delete (after naming the buttons and
 * navigators that use it) and apply. Each change is one edit; Apply only shows the page as the bookmark does.
 */
export function BookmarksPane({ bookmarks, pageId, selected, visuals, disabled, onEdit, onApply, applied }: {
  bookmarks: Bookmark[]; pageId: string; selected: string[]; visuals: Record<string, LayerVisual>; disabled: boolean;
  onEdit: (operations: Operation[], label: string) => Promise<boolean>;
  /** Shows the page as the bookmark does (the canvas renders it with `bookmark`); undefined when the canvas cannot. */
  onApply?: (bookmark: Bookmark | undefined) => void;
  applied?: string;
}) {
  const [adding, setAdding] = useState<{ name: string; capture: CaptureOptions; group: string } | undefined>();
  const [renaming, setRenaming] = useState<{ bookmark: Bookmark; name: string }>();
  const [updating, setUpdating] = useState<{ bookmark: Bookmark; capture: CaptureOptions }>();
  const [deleting, setDeleting] = useState<Bookmark>();
  const [grouping, setGrouping] = useState<{ names: string[]; name: string }>();
  const [checked, setChecked] = useState<string[]>([]);
  const [dragging, setDragging] = useState<number>();
  const tree = bookmarkTree(bookmarks);
  const groups = tree.flatMap(entry => entry.kind === "group" ? [entry] : []);
  const indexOf = (bookmark: Bookmark) => bookmarks.findIndex(b => b.name === bookmark.name);
  const toggle = (name: string, on: boolean) => setChecked(current => on ? [...new Set([...current, name])] : current.filter(item => item !== name));

  const row = (bookmark: Bookmark, indent: boolean) => {
    const at = indexOf(bookmark);
    const isApplied = applied === bookmark.name;
    return <li key={bookmark.name} className={`${indent ? "indent" : ""}${dragging === at ? " dragging" : ""}${isApplied ? " applied" : ""}`} draggable={!disabled}
      onDragStart={event => { setDragging(at); event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData("text/plain", bookmark.name); }}
      onDragOver={event => { if (dragging !== undefined) { event.preventDefault(); event.dataTransfer.dropEffect = "move"; } }}
      onDrop={event => { event.preventDefault(); const from = dragging; setDragging(undefined); if (from === undefined) return; const op = moveBookmarkOp(bookmarks, from, at); if (op) void onEdit([op], "Moving the bookmark"); }}
      onDragEnd={() => setDragging(undefined)}>
      <GripVertical size={12} className="ie-grip" aria-hidden="true" />
      <input type="checkbox" aria-label={`Select ${bookmark.displayName}`} checked={checked.includes(bookmark.name)} onChange={event => toggle(bookmark.name, event.target.checked)} />
      <span className="ie-bookmark-name" title={bookmark.page && bookmark.page !== pageId ? "This bookmark opens another page" : undefined}>
        <BookmarkIcon size={12} aria-hidden="true" />{bookmark.displayName}
        <small>{[bookmark.captures.data ? "data" : "", bookmark.captures.display ? "display" : "", bookmark.captures.page ? "page" : ""].filter(Boolean).join(" · ")}{bookmark.targets ? ` · ${bookmark.targets.length} visual${bookmark.targets.length === 1 ? "" : "s"}` : ""}</small>
      </span>
      <span className="ie-row-tools">
        {onApply && <button type="button" className="icon-button" aria-pressed={isApplied} aria-label={`${isApplied ? "Stop showing" : "Show the page as"} ${bookmark.displayName}`} title={isApplied ? "Back to the page as it is" : "Apply: show the page as this bookmark does"} onClick={() => onApply(isApplied ? undefined : bookmark)}><Play size={12} /></button>}
        <button type="button" className="icon-button" disabled={disabled} aria-label={`Move ${bookmark.displayName} up`} title="Move up" onClick={() => { const op = nudgeBookmarkOp(bookmarks, bookmark.name, -1); if (op) void onEdit([op], "Moving the bookmark"); }}><ArrowUp size={12} /></button>
        <button type="button" className="icon-button" disabled={disabled} aria-label={`Move ${bookmark.displayName} down`} title="Move down" onClick={() => { const op = nudgeBookmarkOp(bookmarks, bookmark.name, 1); if (op) void onEdit([op], "Moving the bookmark"); }}><ArrowDown size={12} /></button>
        <button type="button" className="icon-button" disabled={disabled} aria-label={`Update ${bookmark.displayName}`} title="Update: remember the page as it is now" onClick={() => setUpdating({ bookmark, capture: { data: bookmark.captures.data, display: bookmark.captures.display, currentPage: bookmark.captures.page, selectedOnly: bookmark.targets !== null } })}><RefreshCw size={12} /></button>
        <button type="button" className="icon-button" disabled={disabled} aria-label={`Rename ${bookmark.displayName}`} title="Rename" onClick={() => setRenaming({ bookmark, name: bookmark.displayName })}><Pencil size={12} /></button>
        <button type="button" className="icon-button" disabled={disabled} aria-label={`Delete ${bookmark.displayName}`} title="Delete" onClick={() => setDeleting(bookmark)}><Trash2 size={12} /></button>
      </span>
    </li>;
  };

  return <div className="ie-bookmarks">
    <div className="ie-pane-actions">
      <button type="button" className="secondary" disabled={disabled} onClick={() => setAdding({ name: nextBookmarkName(bookmarks), capture: { ...DEFAULT_CAPTURE }, group: "" })}><Plus size={13} />Add bookmark</button>
      <button type="button" className="secondary" disabled={disabled || checked.length < 1} title="Put the checked bookmarks into a group" onClick={() => setGrouping({ names: checked, name: `Group ${groups.length + 1}` })}><FolderPlus size={13} />Group</button>
    </div>
    {!bookmarks.length && <p className="ie-empty">No bookmarks yet. Set the page up the way you want it to open, then Add bookmark.</p>}
    <ul className="ie-bookmark-list" aria-label="Bookmarks">
      {tree.map(entry => entry.kind === "bookmark" ? row(entry.bookmark, false) : <li key={`group-${entry.name}`} className="ie-bookmark-group">
        <div className="ie-bookmark-group-head"><strong>{entry.displayName}</strong>
          <button type="button" className="icon-button" disabled={disabled} aria-label={`Ungroup ${entry.displayName}`} title="Ungroup" onClick={() => void onEdit([{ op: "ungroup_bookmarks", group: entry.name }], "Ungrouping the bookmarks")}><FolderX size={12} /></button></div>
        <ul>{entry.items.map(item => row(item.bookmark, true))}</ul>
      </li>)}
    </ul>
    {adding && <EditorDialog title="Add a bookmark" onCancel={() => setAdding(undefined)}
      footer={<><button type="button" className="secondary" onClick={() => setAdding(undefined)}>Cancel</button>
        <button type="button" className="primary yellow" disabled={!adding.name.trim()} onClick={() => { const op = addBookmarkOp(adding.name, pageId, adding.capture, selected, adding.group || undefined); setAdding(undefined); if (op) void onEdit([op], "Adding the bookmark"); }}>Add</button></>}>
      <label className="ie-field-block">Name<input data-autofocus value={adding.name} maxLength={100} onChange={event => setAdding({ ...adding, name: event.target.value })} /></label>
      <CaptureChoices value={adding.capture} selectedCount={selected.length} onChange={capture => setAdding({ ...adding, capture })} />
      {groups.length > 0 && <label className="ie-field-block">Group<select value={adding.group} onChange={event => setAdding({ ...adding, group: event.target.value })}><option value="">No group</option>{groups.map(group => <option key={group.name} value={group.name}>{group.displayName}</option>)}</select></label>}
    </EditorDialog>}
    {renaming && <EditorDialog title="Rename the bookmark" onCancel={() => setRenaming(undefined)}
      footer={<><button type="button" className="secondary" onClick={() => setRenaming(undefined)}>Cancel</button>
        <button type="button" className="primary yellow" disabled={!renaming.name.trim() || renaming.name.trim() === renaming.bookmark.displayName} onClick={() => { const target = renaming; setRenaming(undefined); void onEdit([{ op: "rename_bookmark", bookmark: target.bookmark.name, name: target.name.trim().slice(0, 100) }], "Renaming the bookmark"); }}>Rename</button></>}>
      <label className="ie-field-block">Name<input data-autofocus value={renaming.name} maxLength={100} onChange={event => setRenaming({ ...renaming, name: event.target.value })} /></label>
    </EditorDialog>}
    {updating && <EditorDialog title={`Update “${updating.bookmark.displayName}”?`} onCancel={() => setUpdating(undefined)}
      footer={<><button type="button" className="secondary" onClick={() => setUpdating(undefined)}>Cancel</button>
        <button type="button" className="primary yellow" onClick={() => { const target = updating; setUpdating(undefined); void onEdit([updateBookmarkOp(target.bookmark.name, target.capture, selected)], "Updating the bookmark"); }}>Update</button></>}>
      <p className="ie-dialog-text">The bookmark takes the page as it is now. Visuals it hid or showed that are still on the page keep that.</p>
      <CaptureChoices value={updating.capture} selectedCount={selected.length} onChange={capture => setUpdating({ ...updating, capture })} />
    </EditorDialog>}
    {grouping && <EditorDialog title="Group the bookmarks" onCancel={() => setGrouping(undefined)}
      footer={<><button type="button" className="secondary" onClick={() => setGrouping(undefined)}>Cancel</button>
        <button type="button" className="primary yellow" disabled={!grouping.name.trim()} onClick={() => { const target = grouping; setGrouping(undefined); setChecked([]); void onEdit([{ op: "group_bookmarks", bookmarks: target.names, name: target.name.trim().slice(0, 100) }], "Grouping the bookmarks"); }}>Group</button></>}>
      <p className="ie-dialog-text">{grouping.names.length} bookmark{grouping.names.length === 1 ? "" : "s"} go into the group.</p>
      <label className="ie-field-block">Group name<input data-autofocus value={grouping.name} maxLength={100} onChange={event => setGrouping({ ...grouping, name: event.target.value })} /></label>
    </EditorDialog>}
    {deleting && (() => {
      const users = bookmarkUsers(deleting, visuals);
      return <EditorDialog title={`Delete “${deleting.displayName}”?`} tone="warn" onCancel={() => setDeleting(undefined)}
        footer={<><button type="button" className="secondary" data-autofocus onClick={() => setDeleting(undefined)}>Cancel</button>
          <button type="button" className="primary yellow" onClick={() => { const target = deleting; setDeleting(undefined); void onEdit([{ op: "delete_bookmark", bookmark: target.name }], "Deleting the bookmark"); }}>Delete bookmark</button></>}>
        <p className="ie-dialog-text">{users.length
          ? "These visuals on this page may use it. If a button or navigator points at it, you will be asked before its link is removed."
          : "If a button or navigator on another page points at it, you will be asked before its link is removed. You can undo this from History."}</p>
        {users.length > 0 && <ul className="ie-dialog-list">{users.map(user => <li key={user.id}>{user.title}</li>)}</ul>}
      </EditorDialog>;
    })()}
  </div>;
}
