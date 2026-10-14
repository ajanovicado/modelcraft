import { useState } from "react";
import { ArrowDown, ArrowUp, Eye, EyeOff, Group, ListOrdered, Ungroup } from "lucide-react";
import { bookmarkHides, type Bookmark, type LayerRow } from "./importedPanes";

export type ListedVisual = { id: string; title: string; kind: string; hidden: boolean };

/**
 * The Selection pane: every visual on the page as layers, top of the stack first, with groups and their members (the
 * keyboard and screen-reader way to select one, including hidden visuals, which are only ghosts on the canvas). Show or
 * hide it, change its stacking order, group and ungroup, and set the tab order. With a bookmark chosen, the eye shows
 * and changes what that bookmark hides ("hide in bookmark"), not the page.
 */
export function SelectionList({ visuals, layers, selectedIds, disabled, bookmarks = [], onSelect, onHidden, onArrange, onBookmarkHidden, onGroup, onUngroup, onTabOrder }: {
  visuals: ListedVisual[]; selectedIds: string[]; disabled: boolean;
  /** The page's layers from the render (groups, members, stacking); `visuals` stands in until they arrive. */
  layers?: LayerRow[];
  bookmarks?: Bookmark[];
  onSelect: (id: string, additive: boolean) => void;
  onHidden: (id: string, hidden: boolean) => void;
  onArrange: (id: string, to: "forward" | "backward") => void;
  onBookmarkHidden?: (bookmark: string, id: string, hidden: boolean) => void;
  onGroup?: () => void; onUngroup?: () => void;
  onTabOrder?: () => void;
}) {
  const [bookmarkName, setBookmarkName] = useState("");
  const bookmark = bookmarks.find(item => item.name === bookmarkName);
  const rows: (LayerRow | (ListedVisual & { depth: number; hiddenOwn: boolean }))[] = layers?.length ? layers : visuals.map(item => ({ ...item, depth: 0, hiddenOwn: item.hidden }));
  const count = layers?.length ? layers.filter(row => row.kind !== "group").length : visuals.length;
  return <div className="ie-selection">
    <strong className="ie-selection-title">Visuals on this page ({count})</strong>
    {(onGroup || onUngroup || onTabOrder || (bookmarks.length > 0 && onBookmarkHidden)) && <div className="ie-pane-actions">
      {onGroup && <button type="button" className="secondary" disabled={disabled || selectedIds.length < 2} title="Group the selected visuals (Ctrl+G)" onClick={onGroup}><Group size={13} />Group</button>}
      {onUngroup && <button type="button" className="secondary" disabled={disabled} title="Ungroup the selected group" onClick={onUngroup}><Ungroup size={13} />Ungroup</button>}
      {onTabOrder && <button type="button" className="secondary" disabled={disabled} title="Keyboard users reach the visuals top to bottom, left to right" onClick={onTabOrder}><ListOrdered size={13} />Tab order by position</button>}
      {bookmarks.length > 0 && onBookmarkHidden && <label className="ie-inline ie-selection-bookmark">Show and hide in
        <select value={bookmarkName} onChange={event => setBookmarkName(event.target.value)}><option value="">The page</option>{bookmarks.map(item => <option key={item.name} value={item.name}>Bookmark: {item.displayName}</option>)}</select></label>}
    </div>}
    {bookmark && !bookmark.captures.display && <p className="ie-note">This bookmark does not remember which visuals are shown. Update it with Display on to hide visuals in it.</p>}
    <ul>
      {rows.map(item => {
        const inBookmark = bookmark ? bookmarkHides(bookmark, item.id) : undefined;
        const hidden = bookmark ? inBookmark === true : item.hiddenOwn;
        const eyeLabel = bookmark ? `${hidden ? "Show" : "Hide"} ${item.title} in ${bookmark.displayName}` : `${item.hidden ? "Show" : "Hide"} ${item.title}`;
        return <li key={item.id} className={selectedIds.includes(item.id) ? "selected" : undefined} style={item.depth ? { paddingLeft: item.depth * 14 } : undefined}>
          <button type="button" className="ie-selection-name" aria-pressed={selectedIds.includes(item.id)} onClick={event => onSelect(item.id, event.ctrlKey || event.metaKey)}>{item.title}<small>{item.kind === "group" ? "group" : item.kind}{item.hidden ? " · hidden" : ""}</small></button>
          <span className="ie-selection-tools">
            <button type="button" className="icon-button" disabled={disabled || (bookmark && !bookmark.captures.display)} aria-label={eyeLabel} title={bookmark ? `In ${bookmark.displayName}: ${hidden ? "hidden" : "shown"}` : item.hidden ? "Show" : "Hide"}
              onClick={() => bookmark ? onBookmarkHidden?.(bookmark.name, item.id, !hidden) : onHidden(item.id, !item.hidden)}>{hidden ? <EyeOff size={13} /> : <Eye size={13} />}</button>
            <button type="button" className="icon-button" disabled={disabled} aria-label={`Bring ${item.title} forward`} title="Bring forward" onClick={() => onArrange(item.id, "forward")}><ArrowUp size={13} /></button>
            <button type="button" className="icon-button" disabled={disabled} aria-label={`Send ${item.title} backward`} title="Send backward" onClick={() => onArrange(item.id, "backward")}><ArrowDown size={13} /></button>
          </span>
        </li>;
      })}
    </ul>
  </div>;
}
