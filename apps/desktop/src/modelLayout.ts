/** Viewport-aware arrangement with the fact table between its related tables. */
export const MODEL_NODE_WIDTH = 240;

export function arrangeModel(
  tables: readonly { id: string; role?: string }[],
  relationships: readonly { fromTableId: string; toTableId: string }[],
  heights: ReadonlyMap<string, number>,
  viewportWidth: number,
  viewportHeight: number,
) {
  const width = Math.max(640, viewportWidth);
  const positions = new Map<string, { x: number; y: number }>();
  const degree = new Map<string, number>();
  for (const r of relationships) {
    degree.set(r.fromTableId, (degree.get(r.fromTableId) ?? 0) + 1);
    degree.set(r.toTableId, (degree.get(r.toTableId) ?? 0) + 1);
  }
  const ranked = [...tables].sort((a, b) =>
    Number(b.role === "fact") - Number(a.role === "fact") ||
    (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0),
  );
  const hub = relationships.length ? ranked[0] : undefined;
  let contentHeight = 0;
  if (hub) {
    const hubHeight = heights.get(hub.id) ?? 100;
    positions.set(hub.id, { x: (width - MODEL_NODE_WIDTH) / 2, y: 0 });
    const related = tables.filter(t => t.id !== hub.id);
    let rowY = width >= 920 ? hubHeight * .65 + 48 : hubHeight + 88;
    contentHeight = hubHeight;
    for (let i = 0; i < related.length; i += 2) {
      const row = related.slice(i, i + 2);
      for (let j = 0; j < row.length; j++) {
        positions.set(row[j].id, { x: j === 0 ? 32 : width - MODEL_NODE_WIDTH - 32, y: rowY });
      }
      contentHeight = Math.max(contentHeight, rowY + Math.max(...row.map(t => heights.get(t.id) ?? 100)));
      rowY = contentHeight + 64;
    }
  } else {
    const columns = width >= 920 ? 3 : 2;
    for (let i = 0; i < tables.length; i += columns) {
      const row = tables.slice(i, i + columns);
      const rowWidth = row.length * MODEL_NODE_WIDTH + (row.length - 1) * 48;
      row.forEach((t, j) => positions.set(t.id, { x: (width - rowWidth) / 2 + j * (MODEL_NODE_WIDTH + 48), y: contentHeight }));
      contentHeight += Math.max(...row.map(t => heights.get(t.id) ?? 100)) + 64;
    }
    if (tables.length) contentHeight -= 64;
  }
  const height = Math.max(360, viewportHeight, contentHeight + 96);
  const top = (height - contentHeight) / 2;
  for (const position of positions.values()) position.y += top;
  return { width, height, positions };
}
