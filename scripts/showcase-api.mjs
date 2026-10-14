// An in-memory fixture responder shared by screenshot capture and the reviewer demo.
// It never opens local source files, calls a model, executes SQL/DAX, or launches Power BI.
export function createShowcaseResponder(fixture) {
  let messages = structuredClone(fixture.messages);
  const json = body => ({ status: 200, contentType: "application/json", body });
  const invalid = detail => ({ status: 422, contentType: "application/json", body: { detail } });
  const sse = events => ({ status: 200, contentType: "text/event-stream", body: events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join("") });
  return (method, path, body = {}) => {
    if (method === "GET") {
      if (path === "/v1/ping") return json({ ready: true, features: ["chat-context", "draft-restore"] });
      if (path === "/v1/health") return json(fixture.health);
      if (path === "/v1/model/install") return json({ state: "installed", installed: true, running: false, download_available: false });
      if (path === "/v1/workspaces") return json([fixture.workspace]);
      if (path === `/v1/workspaces/${fixture.workspace.id}`) return json(fixture.workspace);
      if (path === `/v1/snapshots/${fixture.snapshot.id}`) return json(fixture.snapshot);
      if (path === `/v1/drafts/${fixture.draft.id}`) return json(fixture.draft);
      if (path === `/v1/workspaces/${fixture.workspace.id}/messages`) return json(messages);
      if ([`/v1/workspaces/${fixture.workspace.id}/events`, `/v1/workspaces/${fixture.workspace.id}/builds`, "/v1/report-themes"].includes(path)) return json([]);
    }
    if (method === "POST" && path === `/v1/snapshots/${fixture.snapshot.id}/preview`) {
      const profile = fixture.snapshot.tables.find(item => item.id === body.table_id || item.table.id === body.table_id)?.table;
      if (!profile || !Array.isArray(body.column_ids)) return invalid("Choose a synthetic demo table.");
      const columns = profile.columns.filter(column => body.column_ids.includes(column.id));
      let rows = [...fixture.records[profile.id]];
      for (const filter of Array.isArray(body.filters) ? body.filters.slice(0, 4) : []) {
        if (!profile.columns.some(column => column.id === filter.column_id)) return invalid("Unknown synthetic field.");
        rows = rows.filter(row => {
          const value = row[filter.column_id], expected = typeof value === "number" ? Number(filter.value) : filter.value;
          switch (filter.operator) {
            case "equals": return value === expected;
            case "not_equals": return value !== expected;
            case "contains": return String(value).toLowerCase().includes(String(expected).toLowerCase());
            case "gt": return value > expected;
            case "gte": return value >= expected;
            case "lt": return value < expected;
            case "lte": return value <= expected;
            case "is_blank": return value == null || value === "";
            case "is_not_blank": return value != null && value !== "";
            default: return false;
          }
        });
      }
      if (body.sort && profile.columns.some(column => column.id === body.sort.column_id)) {
        const { column_id, direction } = body.sort;
        rows.sort((a, b) => (typeof a[column_id] === "number" ? a[column_id] - b[column_id] : String(a[column_id]).localeCompare(String(b[column_id]))) * (direction === "desc" ? -1 : 1));
      }
      const offset = Math.max(0, Math.floor(Number(body.offset) || 0));
      return json({ columns: columns.map(({ id, name, data_type }) => ({ id, name, data_type })), rows: rows.slice(offset, offset + 50).map(row => columns.map(column => row[column.id])),
        offset, total_rows: rows.length, snapshot_rows: fixture.records[profile.id].length, has_more: rows.length > offset + 50, truncated_cells: false, elapsed_ms: 1 });
    }
    if (method === "POST" && path === `/v1/workspaces/${fixture.workspace.id}/dax/evaluate`) {
      const expression = String(body.expression || "").replaceAll(/\s/g, "").toLowerCase();
      const examples = new Map([["sum('orders'[sales])", ["Total Sales", fixture.totalSales]], ["sum('orders'[profit])", ["Total Profit", fixture.totalProfit]],
        ["countrows('orders')", ["Order Count", fixture.records.orders.length]], ["distinctcount('orders'[orderid])", ["Order Count", fixture.records.orders.length]],
        ["divide(sum('orders'[profit]),sum('orders'[sales]))", ["Profit Margin", fixture.totalProfit / fixture.totalSales]]]);
      const result = examples.get(expression);
      if (!result) return invalid("Synthetic demo: try SUM('Orders'[Sales]), SUM('Orders'[Profit]) or COUNTROWS('Orders'). A DAX engine is not running in this demo.");
      const [column, value] = result;
      return json({ engine: "snapshot", columns: [column], rows: [{ [column]: value }], row_count: 1, truncated: false, elapsed_ms: 1,
        plan: "Synthetic showcase fixture: prepared example result; no DAX engine was executed." });
    }
    if (method === "POST" && path === "/v1/chat/stream") {
      const answer = `**Prepared demo answer.** This reviewer mode uses synthetic data and example replies.\n\nThe snapshot contains 72 orders across four regions, with total sales of **$${fixture.totalSales.toLocaleString("en-US")}**. Explore Data for column profiles, Model for relationships, and DAX Lab to try the prepared calculations. No model is running.`;
      const id = messages.length + 1;
      messages.push({ id, role: "user", content: String(body.message || "").slice(0, 4000), citations: [], created_at: "2025-07-01T12:05:00Z" },
        { id: id + 1, role: "assistant", content: answer, citations: [], created_at: "2025-07-01T12:05:01Z" });
      return sse([["token", { text: answer }], ["done", {}]]);
    }
    if (method === "POST" && path === "/v1/dax/assist") {
      return sse([["assistant", { kind: "advice" }], ["token", { text: "**Prepared demo explanation**\n\n```dax\nSUM('Orders'[Sales])\n```\n\nAdds Sales across the rows in the current filter context. The synthetic demo's overall total is **$145,260**.\n\nTry `SUM('Orders'[Profit])` or `COUNTROWS('Orders')` next. This is an example reply; no AI model or DAX engine is running." }], ["done", {}]]);
    }
    return invalid("This action is unavailable in the synthetic reviewer demo. No files, models or Power BI projects were changed.");
  };
}
