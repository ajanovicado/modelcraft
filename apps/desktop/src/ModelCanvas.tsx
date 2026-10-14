import { useId, useState } from "react";
import { CalendarDays, ChevronDown, ChevronUp, Hash, KeyRound, Table2, ToggleLeft, Type } from "lucide-react";
import { arrangeModel, MODEL_NODE_WIDTH } from "./modelLayout";

export interface ModelCanvasColumn {
  id: string;
  name: string;
  dataType?: string;
  candidateKey?: boolean;
  hidden?: boolean;
}

export interface ModelCanvasTable {
  id: string;
  name: string;
  displayName?: string;
  role?: string;
  rowCount?: number;
  columns?: readonly ModelCanvasColumn[];
}

export interface ModelCanvasRelationship {
  id: string;
  fromTableId: string;
  toTableId: string;
  fromColumnId: string;
  toColumnId: string;
  cardinality: string;
  reviewStatus: "pending" | "approved" | "rejected";
  blockers: readonly string[];
  crossFilterDirection?: "single" | "both";
  active?: boolean;
}

export interface ModelCanvasProps {
  tables: readonly ModelCanvasTable[];
  relationships: readonly ModelCanvasRelationship[];
  selectedIds: ReadonlySet<string>;
  tableLabel: (tableId: string) => string;
  activeTableId?: string;
  onTableSelect?: (id: string) => void;
  onRelationshipSelect?: (id: string) => void;
  activeRelationshipId?: string;
  viewportWidth?: number;
  viewportHeight?: number;
  zoom?: number;
}

const NODE_W = MODEL_NODE_WIDTH;
const HEADER_H = 56;
const COL_H = 28;
const NODE_PAD = 10;
const MAX_COLS = 6;
const FIELD_ICONS = { key: KeyRound, date: CalendarDays, number: Hash, boolean: ToggleLeft, text: Type };

const EDGE_STATUSES = ["approved", "selected", "pending", "blocked"] as const;
type EdgeStatus = (typeof EDGE_STATUSES)[number];

const STATUS_TEXT: Record<EdgeStatus, string> = {
  approved: "Approved",
  selected: "Selected for approval",
  pending: "Pending review",
  blocked: "Blocked",
};

const truncate = (value: string, max: number) => (value.length > max ? `${value.slice(0, max - 1)}…` : value);

const shortColumnName = (value: string) => {
  const segments = value.split(/[.[\]]/).filter(Boolean);
  return segments.at(-1) ?? value;
};

const formatRows = (value?: number) =>
  value === undefined
    ? "row count unknown"
    : `${new Intl.NumberFormat(undefined, { notation: value >= 100_000 ? "compact" : "standard", maximumFractionDigits: 1 }).format(value)} rows`;

interface Anchor {
  x: number;
  y: number;
  nx: number;
  ny: number;
}

export function ModelCanvas({ tables, relationships, selectedIds, tableLabel, activeTableId, onTableSelect, onRelationshipSelect, activeRelationshipId, viewportWidth = 960, viewportHeight = 560, zoom = 1 }: ModelCanvasProps) {
  const markerId = useId().replaceAll(":", "");
  const [expandedTable, setExpandedTable] = useState<string>();
  const live = relationships.filter((rel) => rel.reviewStatus !== "rejected");
  const connected = new Set(live.flatMap((rel) => [rel.fromTableId, rel.toTableId]));

  const joinRole = new Map<string, Map<string, "one" | "many">>();
  const noteJoin = (tableId: string, columnId: string, role: "one" | "many") => {
    const table = joinRole.get(tableId) ?? new Map<string, "one" | "many">();
    table.set(columnId, role);
    joinRole.set(tableId, table);
  };
  live.forEach((rel) => {
    const oneRole: "one" | "many" = rel.cardinality === "many-to-many" ? "many" : "one";
    noteJoin(rel.toTableId, rel.toColumnId, oneRole);
    noteJoin(rel.fromTableId, rel.fromColumnId, rel.cardinality === "one-to-one" ? "one" : "many");
  });

  const displayCols = new Map(
    tables.map((table) => {
      const columns = table.columns ?? [];
      const joins = joinRole.get(table.id) ?? new Map<string, "one" | "many">();
      const rank = (column: ModelCanvasColumn) => (joins.has(column.id) ? 0 : column.candidateKey ? 1 : column.hidden ? 3 : 2);
      const sorted = [...columns].sort((a, b) => rank(a) - rank(b));
      const limit = expandedTable === table.id ? 30 : MAX_COLS;
      return [table.id, { visible: sorted.slice(0, limit), extra: Math.max(0, sorted.length - limit), expandable: sorted.length > MAX_COLS }] as const;
    }),
  );

  const nodeHeight = new Map(
    tables.map((table) => {
      const display = displayCols.get(table.id);
      const rows = (display?.visible.length ?? 0) + (display?.expandable ? 1 : 0);
      return [table.id, HEADER_H + rows * COL_H + NODE_PAD] as const;
    }),
  );

  const { width, height, positions } = arrangeModel(tables, live, nodeHeight, viewportWidth, viewportHeight);
  const scale = zoom || Math.min(1, viewportWidth / width, viewportHeight / height);

  const columnRowY = (tableId: string, columnId: string) => {
    const display = displayCols.get(tableId);
    const position = positions.get(tableId);
    if (!display || !position) return undefined;
    const index = display.visible.findIndex((column) => column.id === columnId);
    if (index < 0) return undefined;
    return position.y + HEADER_H + index * COL_H + COL_H / 2;
  };

  const anchorFor = (selfId: string, otherId: string, columnId: string): Anchor | undefined => {
    const self = positions.get(selfId);
    const other = positions.get(otherId);
    if (!self || !other) return undefined;
    const selfHeight = nodeHeight.get(selfId) ?? HEADER_H + NODE_PAD;
    const otherHeight = nodeHeight.get(otherId) ?? HEADER_H + NODE_PAD;
    const cx = self.x + NODE_W / 2;
    const cy = self.y + selfHeight / 2;
    const dx = other.x + NODE_W / 2 - cx;
    const dy = other.y + otherHeight / 2 - cy;
    const rowY = columnRowY(selfId, columnId);
    if (Math.abs(dx) > NODE_W + 24) {
      const sign = Math.sign(dx) || 1;
      return { x: cx + (sign * NODE_W) / 2, y: rowY ?? cy, nx: sign, ny: 0 };
    }
    const sign = Math.sign(dy) || 1;
    return { x: cx + Math.sign(dx) * NODE_W * .25, y: cy + (sign * selfHeight) / 2, nx: 0, ny: sign };
  };

  const edges = live.flatMap((rel) => {
    const many = anchorFor(rel.fromTableId, rel.toTableId, rel.fromColumnId);
    const one = anchorFor(rel.toTableId, rel.fromTableId, rel.toColumnId);
    if (!many || !one) return [];
    const status: EdgeStatus = rel.reviewStatus === "approved" ? "approved" : rel.blockers.length > 0 ? "blocked" : selectedIds.has(rel.id) ? "selected" : "pending";
    const start = one;
    const end = many;
    const path = start.nx && end.nx
      ? `M ${start.x} ${start.y} H ${(start.x + end.x) / 2} V ${end.y} H ${end.x}`
      : `M ${start.x} ${start.y} V ${(start.y + end.y) / 2} H ${end.x} V ${end.y}`;
    const join = `${tableLabel(rel.fromTableId)}[${shortColumnName(rel.fromColumnId)}] → ${tableLabel(rel.toTableId)}[${shortColumnName(rel.toColumnId)}]`;
    return [
      {
        id: rel.id,
        status,
        path,
        start,
        end,
        fromCard: rel.cardinality === "one-to-one" ? "1" : "*",
        toCard: rel.cardinality === "many-to-many" ? "*" : "1",
        both: rel.crossFilterDirection === "both",
        label: `${join} — ${STATUS_TEXT[status]}`,
      },
    ];
  });

  return (
    <div className="mwb-canvas" style={{ width: Math.max(viewportWidth, width * scale), height: Math.max(viewportHeight, height * scale) }}>
      <svg className="mwb-canvas-stage" style={{ width: width * scale, height: height * scale }} viewBox={`0 0 ${width} ${height}`} role="group" aria-label="Table relationship map">
        <defs>
          {Object.entries(FIELD_ICONS).map(([kind, Icon]) => <g key={kind} id={`${markerId}-field-${kind}`}><Icon width={14} height={14} strokeWidth={1.6}/></g>)}
          {EDGE_STATUSES.map((status) => (
            <marker key={status} id={`${markerId}-arrow-${status}`} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" className={`mwb-edge-arrow ${status}`} />
            </marker>
          ))}
        </defs>
        {edges.map((edge) => (
          <g key={edge.id} role="button" tabIndex={0} aria-label={edge.label + ". Inspect relationship"} aria-pressed={activeRelationshipId === edge.id} onClick={() => onRelationshipSelect?.(edge.id)} onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onRelationshipSelect?.(edge.id); } }}>
            <path className="mwb-canvas-edge-hit" d={edge.path}/>
            <path
              className={`mwb-canvas-edge ${edge.status}`}
              d={edge.path}
              markerEnd={`url(#${markerId}-arrow-${edge.status})`}
              markerStart={edge.both ? `url(#${markerId}-arrow-${edge.status})` : undefined}
            />
            <text className="mwb-canvas-card" x={edge.start.x + edge.start.nx * 14} y={edge.start.y + edge.start.ny * 14 + 3} textAnchor="middle">
              {edge.toCard}
            </text>
            <text className="mwb-canvas-card" x={edge.end.x + edge.end.nx * 14} y={edge.end.y + edge.end.ny * 14 + 3} textAnchor="middle">
              {edge.fromCard}
            </text>
            <title>{edge.label}</title>
          </g>
        ))}
        {tables.map((table) => {
          const position = positions.get(table.id);
          if (!position) return null;
          const display = displayCols.get(table.id);
          const joins = joinRole.get(table.id) ?? new Map<string, "one" | "many">();
          const meta = `${table.role || "Table"} · ${formatRows(table.rowCount)}`;
          const nodeH = nodeHeight.get(table.id) ?? HEADER_H + NODE_PAD;
          return (
            <g key={table.id} className={`mwb-canvas-node${connected.has(table.id) ? "" : " isolated"}${activeTableId === table.id ? " selected" : ""}`} transform={`translate(${position.x} ${position.y})`}>
              <g className="mwb-node-inspect" role="button" tabIndex={0} aria-label={`Inspect ${tableLabel(table.id)}`} onClick={() => onTableSelect?.(table.id)} onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onTableSelect?.(table.id); } }}>
              <rect className="node-surface" width={NODE_W} height={nodeH} rx={5} />
              <Table2 className="node-table-icon" x={12} y={15} width={16} height={16} strokeWidth={1.6}/>
              <text className="name" x={37} y={25}>
                {truncate(tableLabel(table.id), 23)}
              </text>
              <text className="meta" x={37} y={42}>
                {truncate(meta, 34)}
              </text>
              <line className="divider" x1={0} x2={NODE_W} y1={HEADER_H - 3} y2={HEADER_H - 3} />
              {display?.visible.map((column, index) => {
                const join = joins.get(column.id);
                const rowTop = HEADER_H + index * COL_H;
                const cls = join ? ` join ${join}` : column.candidateKey ? " candidate" : column.hidden ? " hidden" : "";
                const icon = join || column.candidateKey ? "key" : column.dataType?.includes("date") ? "date" : column.dataType === "number" || column.dataType === "integer" ? "number" : column.dataType === "boolean" ? "boolean" : "text";
                return (
                  <g key={column.id} className={`mwb-canvas-col${cls}`} transform={`translate(0 ${rowTop})`}>
                    <use className="field-icon" href={`#${markerId}-field-${icon}`} x={12} y={7}/>
                    <text className="col-name" x={35} y={18}>
                      {truncate(column.name, 19)}
                    </text>
                    {column.dataType ? (
                      <text className="col-type" x={NODE_W - 10} y={18} textAnchor="end">
                        {truncate(column.dataType, 9)}
                      </text>
                    ) : null}
                  </g>
                );
              })}
              <title>{`${tableLabel(table.id)} — ${meta}`}</title>
              </g>
              {display?.expandable && <g className="mwb-node-expand" role="button" tabIndex={0} aria-label={`${expandedTable === table.id ? "Show fewer fields in" : "Show more fields in"} ${tableLabel(table.id)}`} transform={`translate(0 ${HEADER_H + display.visible.length * COL_H})`} onClick={() => { setExpandedTable(expandedTable === table.id ? undefined : table.id); onTableSelect?.(table.id); }} onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setExpandedTable(expandedTable === table.id ? undefined : table.id); onTableSelect?.(table.id); } }}>
                <rect className="expand-hit" x={1} y={0} width={NODE_W - 2} height={COL_H + 7}/>
                <text x={12} y={19}>{expandedTable === table.id ? "Show fewer fields" : `Show ${Math.min(display.extra, 30 - MAX_COLS)} more fields`}</text>
                {expandedTable === table.id ? <ChevronUp x={NODE_W - 27} y={7} width={14} height={14}/> : <ChevronDown x={NODE_W - 27} y={7} width={14} height={14}/>}
                {display.extra > 0 && expandedTable === table.id && <title>{display.extra} additional fields are available in Table properties</title>}
              </g>}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

export default ModelCanvas;
