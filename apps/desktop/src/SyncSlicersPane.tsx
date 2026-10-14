import { RefreshCcw } from "lucide-react";
import type { Operation } from "./importedEditing";
import { syncMatrix, syncOp, type LayerVisual, type PaneMeta } from "./importedPanes";

/**
 * Desktop's Sync slicers pane as a matrix: this page's slicers against every page, "Sync" (the slicer on that page shares
 * this one's selection) and "Visible" (the synced slicer shows there; hidden, it still filters). Each click is one edit.
 */
export function SyncSlicersPane({ pageId, pages, visuals, groups, disabled, onEdit }: {
  pageId: string; pages: { id: string; title: string }[]; visuals: Record<string, LayerVisual>; groups: PaneMeta["syncGroups"]; disabled: boolean;
  onEdit: (operations: Operation[], label: string) => Promise<boolean>;
}) {
  const slicers = Object.values(visuals).filter(visual => visual.kind === "slicer").sort((a, b) => a.title.localeCompare(b.title))
    .map(visual => ({ id: visual.id, title: visual.title, syncGroup: visual.syncGroup?.groupName }));
  if (!slicers.length) return <p className="ie-empty">This page has no slicers. Add a slicer, then sync it to other pages here.</p>;
  const rows = syncMatrix(slicers, pages, pageId, groups);
  const send = (op: Operation | undefined, label: string) => { if (op) void onEdit([op], label); };
  return <div className="ie-sync">
    <p className="ie-note">Sync: the slicer on that page shares this one's selection. Visible: it shows there (hidden, it still filters the page).</p>
    {rows.map(row => <section key={row.visual} className="ie-sync-slicer" aria-label={`Sync ${row.title}`}>
      <header><strong>{row.title}</strong>{row.group && <small>Group “{row.group}”</small>}
        {row.group && <button type="button" className="icon-button" disabled={disabled} aria-label={`Stop syncing ${row.title} everywhere`} title="Stop syncing on every page" onClick={() => send({ op: "unsync_slicer", visual: row.visual }, "Stop syncing the slicer")}><RefreshCcw size={12} /></button>}
      </header>
      <table>
        <thead><tr><th scope="col">Page</th><th scope="col">Sync</th><th scope="col">Visible</th></tr></thead>
        <tbody>{pages.map(page => {
          const cell = row.cells[page.id];
          return <tr key={page.id} className={cell.own ? "own" : undefined}>
            <th scope="row">{page.title}{cell.own && <small> (this page)</small>}</th>
            <td><input type="checkbox" aria-label={`Sync ${row.title} on ${page.title}`} checked={cell.synced} disabled={disabled || cell.own}
              onChange={event => send(syncOp(row, page.id, event.target.checked ? "sync" : "unsync"), event.target.checked ? "Syncing the slicer" : "Stop syncing the slicer")} /></td>
            <td><input type="checkbox" aria-label={`Show ${row.title} on ${page.title}`} checked={cell.visible} disabled={disabled || cell.own || !cell.synced}
              onChange={event => send(syncOp(row, page.id, event.target.checked ? "show" : "hide"), event.target.checked ? "Showing the synced slicer" : "Hiding the synced slicer")} /></td>
          </tr>;
        })}</tbody>
      </table>
    </section>)}
  </div>;
}
