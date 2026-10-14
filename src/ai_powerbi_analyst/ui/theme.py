# ruff: noqa: E501
"""Design tokens and CSS extracted from design/app-concept.png."""

APP_CSS = """
<style>
:root {
  --ink: #11182b;
  --muted: #5f697b;
  --line: #d9dee7;
  --line-soft: #edf0f5;
  --blue: #0b3f96;
  --blue-bright: #1e60c8;
  --orange: #f36a21;
  --green: #16865f;
  --canvas: #ffffff;
  --soft: #f6f7f9;
  --radius: 8px;
}

html, body, [class*="css"] { font-family: "Aptos", "Segoe UI", Arial, sans-serif; }
body { color: var(--ink); background: var(--canvas); }
#MainMenu, footer, [data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stDecoration"] { display: none !important; }
[data-testid="stAppViewContainer"] { background: #fff; }
[data-testid="stMain"] { background: #fff; }
.stMainBlockContainer { max-width: none !important; padding: 0 !important; }
[data-testid="stHorizontalBlock"] { gap: 0 !important; }
.sr-only { position: absolute !important; width: 1px !important; height: 1px !important;
  padding: 0 !important; margin: -1px !important; overflow: hidden !important;
  clip: rect(0, 0, 0, 0) !important; white-space: nowrap !important; border: 0 !important; }

.app-topbar {
  height: 64px; display: flex; align-items: center; border-bottom: 1px solid var(--line);
  padding: 0 26px; box-sizing: border-box;
}
.brand { display: flex; align-items: center; gap: 14px; font-size: 21px; font-weight: 700; color: var(--ink); }
.brand svg { color: var(--ink); }
.project-meta { height: 64px; display: flex; justify-content: center; align-items: center; gap: 24px;
  border-bottom: 1px solid var(--line); font-size: 15px; color: var(--ink); }
.project-meta strong { font-weight: 650; }
.meta-divider { width: 1px; height: 24px; background: var(--line); }
.top-action-marker { display: none; }
[data-testid="stColumn"]:has(.top-action-marker) { height: 64px; border-bottom: 1px solid var(--line);
  padding: 12px 18px 0 0; box-sizing: border-box; }
[data-testid="stColumn"]:has(.top-action-marker) [data-testid="stVerticalBlock"] { gap: 0; }

[data-testid="stColumn"]:has(.left-rail-marker) { min-height: calc(100vh - 64px); border-right: 1px solid var(--line);
  padding: 22px 10px 30px 10px; box-sizing: border-box; }
.left-rail-marker { display: none; }
[data-testid="stColumn"]:has(.left-rail-marker) [data-testid="stVerticalBlock"] { gap: 4px !important; }
[data-testid="stColumn"]:has(.left-rail-marker) .stButton > button { min-height: 50px !important; justify-content: flex-start;
  padding: 0 16px !important; border: 0 !important; border-left: 3px solid transparent !important; border-radius: 0 !important;
  background: transparent !important; color: #394458 !important; font-size: 15px !important; font-weight: 500 !important; }
[data-testid="stColumn"]:has(.left-rail-marker) .stButton > button[kind="primary"] { color: #0d2d66 !important;
  border-left-color: var(--blue) !important; background: #f3f7fd !important; font-weight: 700 !important; }
[data-testid="stColumn"]:has(.left-rail-marker) .stButton > button:hover { background: #f6f8fb !important;
  color: #0d2d66 !important; }

[data-testid="stColumn"]:has(.main-workspace-marker) {
  min-height: calc(100vh - 64px); padding: 30px 46px 24px 46px; box-sizing: border-box;
}
.main-workspace-marker { position: absolute; width: 1px; height: 1px; overflow: hidden; }
.page-header { margin: 0 0 24px; padding-bottom: 20px; border-bottom: 1px solid var(--line-soft); }
.page-eyebrow { color: var(--blue); font-size: 11px; font-weight: 750; letter-spacing: .09em; text-transform: uppercase; }
.page-header h1 { color: var(--ink); font-size: 28px; line-height: 1.2; letter-spacing: -.02em; margin: 5px 0 7px; padding: 0; }
.page-header p { max-width: 720px; color: var(--muted); font-size: 14px; line-height: 1.5; margin: 0; }
h2.section-heading { color: var(--ink); font-size: 16px !important; line-height: 1.3 !important; letter-spacing: 0 !important;
  margin: 24px 0 10px !important; padding: 0 !important; }
.summary-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.summary-card { border: 1px solid var(--line); border-radius: 8px; padding: 15px 16px; background: #fff;
  box-shadow: 0 1px 2px rgba(17, 24, 43, .035); }
.summary-card span { display: block; color: var(--muted); font-size: 11px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; }
.summary-card strong { display: block; color: var(--ink); font-size: 19px; margin-top: 6px; }
.summary-card small { display: block; color: var(--muted); font-size: 12px; margin-top: 3px; }
.workspace-page-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.workspace-page-card { display: grid; grid-template-columns: 30px 1fr; gap: 11px; min-height: 104px; padding: 15px;
  border: 1px solid var(--line); border-radius: 8px; background: #fbfcfe; box-sizing: border-box; }
.workspace-page-card > span { width: 27px; height: 27px; display: flex; align-items: center; justify-content: center;
  border-radius: 50%; background: #eaf1fb; color: var(--blue); font-size: 12px; font-weight: 750; }
.workspace-page-card strong { color: var(--ink); font-size: 14px; }
.workspace-page-card p { color: var(--muted); font-size: 12px; line-height: 1.4; margin: 5px 0 0; }
.workspace-activity-list { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.workspace-activity { display: grid; grid-template-columns: 22px 1fr auto; gap: 10px; align-items: start; padding: 12px 14px; }
.workspace-activity + .workspace-activity { border-top: 1px solid var(--line-soft); }
.workspace-activity strong { color: var(--ink); font-size: 13px; }
.workspace-activity p { color: var(--muted); font-size: 12px; margin: 2px 0 0; }
.workspace-activity time { color: var(--muted); font-size: 11px; }
.empty-state { min-height: 300px; display: flex; flex-direction: column; align-items: center; justify-content: center;
  text-align: center; border: 1px dashed #c7cfdb; border-radius: 10px; background: #fbfcfe; padding: 28px; box-sizing: border-box; }
.empty-icon { width: 44px; height: 44px; display: flex; align-items: center; justify-content: center; border-radius: 50%;
  background: #eaf1fb; color: var(--blue); font-size: 21px; }
.empty-state h2 { color: var(--ink); font-size: 17px !important; margin: 14px 0 6px !important; padding: 0 !important; }
.empty-state p { max-width: 480px; color: var(--muted); font-size: 13px; line-height: 1.5; margin: 0; }
.connection-card { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border: 1px solid var(--line);
  border-radius: 8px; overflow: hidden; }
.connection-card > div { padding: 14px 16px; }
.connection-card > div + div { border-left: 1px solid var(--line-soft); }
.connection-card span { display: block; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
.connection-card strong { display: block; color: var(--ink); font-size: 13px; margin-top: 5px; }
.right-rail { min-height: calc(100vh - 64px); border-left: 1px solid var(--line); padding: 22px 24px 24px 24px; }
h2.right-heading { font-size: 15px !important; line-height: 1.3 !important; font-weight: 700 !important;
  margin: 0 0 10px 0 !important; padding: 0 !important; letter-spacing: 0 !important; white-space: nowrap; }
.rail-divider { border-top: 1px solid var(--line); margin: 12px 0; }
.metric-line { display: flex; align-items: center; gap: 9px; min-height: 31px; font-size: 14px; color: #414b5d; }
.check { width: 16px; height: 16px; border: 1px solid var(--green); color: var(--green); border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 800; flex: 0 0 auto; }
.compat-detail { color: var(--muted); font-size: 13px; line-height: 1.45; padding-left: 25px; max-width: 215px; }

.prompt-row { display: flex; gap: 15px; align-items: flex-start; margin-bottom: 24px; }
.avatar { width: 36px; height: 36px; border: 1px solid #cbd2dd; border-radius: 50%; display: flex; align-items: center;
  justify-content: center; color: #33415a; flex: 0 0 auto; background: #fff; }
.prompt-bubble { flex: 1; min-height: 52px; display: flex; align-items: center; padding: 0 18px; border-radius: 10px;
  background: linear-gradient(90deg, #f5f5f6 0%, #f9f9fa 100%); color: #242e40; font-size: 15px; }
h2.plan-heading { display: flex; align-items: center; gap: 13px; margin: 0 0 4px 0 !important;
  padding: 0 !important; font-size: 17px !important; line-height: 1.3 !important;
  letter-spacing: 0 !important; font-weight: 700 !important; }
.approved { display: inline-flex; align-items: center; gap: 6px; color: var(--green); font-size: 14px; font-weight: 500; }
.plan-count { color: var(--muted); font-size: 13px; margin: 0 0 9px 0; }
.plan-provenance { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border: 1px solid var(--line-soft);
  border-radius: 6px; background: #fafbfc; margin: 0 0 10px 0; overflow: hidden; }
.plan-provenance-item { min-width: 0; padding: 7px 10px; color: #354056; font-size: 11px;
  line-height: 1.25; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.plan-provenance-item + .plan-provenance-item { border-left: 1px solid var(--line-soft); }
.plan-provenance-item span { color: var(--muted); font-size: 10px; font-weight: 700; letter-spacing: .04em;
  margin-right: 6px; text-transform: uppercase; }
.plan-list { border-top: 1px solid var(--line); }
.plan-row { display: grid; grid-template-columns: 44px 42px minmax(190px, 1.3fr) 1fr 1fr;
  min-height: 82px; align-items: center; border-bottom: 1px solid var(--line); }
.page-number { font-size: 24px; color: var(--ink); text-align: center; border-right: 1px solid var(--line); }
.page-icon { color: #2f3f5a; display: flex; justify-content: center; }
.page-copy { padding: 0 16px; }
.page-title { font-size: 15px; font-weight: 700; margin-bottom: 3px; }
.page-description { font-size: 13px; color: #505b6d; line-height: 1.35; }
.plan-detail { font-size: 12px; color: var(--muted); padding: 0 12px; }
.plan-detail strong { display: block; color: var(--ink); font-size: 14px; font-weight: 650; margin-top: 4px; }
.section-rule { border-top: 1px solid var(--line); margin: 14px 0 18px; }
.report-ready { border: 1px solid #cbd8eb; border-left: 4px solid var(--blue); border-radius: 8px;
  background: linear-gradient(135deg, #f7faff 0%, #fbfcfe 100%); padding: 20px 22px; margin: 8px 0 20px; }
.report-ready-head { display: flex; align-items: center; gap: 9px; color: var(--ink); font-size: 17px;
  font-weight: 700; }
.report-ready-copy { color: #465166; font-size: 14px; line-height: 1.5; margin-top: 7px; }
.report-ready-pages { color: #213b69; font-size: 13px; font-weight: 650; margin-top: 12px; }
.report-ready-next { color: var(--muted); font-size: 13px; margin-top: 12px; }

.answer-wrap { border-top: 1px solid var(--line); margin-top: 16px; padding-top: 18px; }
.answer-head { display: flex; gap: 15px; align-items: flex-start; }
.bot-avatar { width: 36px; height: 36px; border: 1px solid #cbd2dd; border-radius: 50%; display: flex;
  align-items: center; justify-content: center; color: #243c68; flex: 0 0 auto; font-weight: 700; }
h2.answer-question { font-weight: 700 !important; font-size: 14px !important; line-height: 1.35 !important;
  letter-spacing: 0 !important; margin: 0 0 8px 0 !important; padding: 0 !important; }
.answer-text { font-size: 14px; line-height: 1.55; color: #344052; }
.evidence-title { font-weight: 700; font-size: 14px; margin: 15px 0 2px; }
.evidence-meta { border-left: 1px solid var(--line); padding-left: 24px; font-size: 13px; line-height: 1.55; color: #394458; }
.evidence-meta strong { color: var(--ink); }
.evidence-review { margin-top: 12px; padding: 12px; border: 1px solid #cbd6e8;
  border-radius: 6px; background: #f6f9fe; font-size: 12px; line-height: 1.5; color: #344052; }
.scenario-panel { padding: 8px 0 2px; }
.scenario-card-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 9px; }
.scenario-card { min-width: 0; border: 1px solid var(--line); border-radius: 7px; background: #fff;
  padding: 11px 12px; box-shadow: 0 1px 2px rgba(17, 24, 43, .04); }
.scenario-card-label { color: var(--muted); font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .035em; }
.scenario-card-value { color: var(--ink); font-size: 19px; font-weight: 700; margin-top: 4px; white-space: nowrap; }
.scenario-card-delta { color: var(--green); font-size: 11px; margin-top: 3px; }
.scenario-context { display: grid; grid-template-columns: 82px 1fr; gap: 8px; color: #465166;
  font-size: 11px; line-height: 1.4; padding: 8px 2px 0; }
.scenario-context strong { color: var(--ink); }
.scenario-assumptions { border-top: 1px solid var(--line-soft); margin-top: 8px; }

.activity-item { display: grid; grid-template-columns: 18px 1fr auto; column-gap: 9px; margin-bottom: 8px; }
.activity-dot { width: 16px; height: 16px; border: 1.5px solid var(--green); border-radius: 50%; color: var(--green);
  display: flex; align-items: center; justify-content: center; font-size: 9px; margin-top: 2px; }
.activity-title { font-size: 14px; font-weight: 650; color: #2f394c; }
.activity-copy { font-size: 12px; line-height: 1.35; color: var(--muted); margin-top: 2px; }
.activity-time { color: var(--muted); font-size: 11px; white-space: nowrap; }
.trust-line { display: grid; grid-template-columns: 22px 1fr auto; align-items: center; min-height: 31px;
  font-size: 14px; color: #394458; }
.trust-value.ok { color: var(--green); }
.trust-value.pending { color: #7b8493; }

.stButton > button, [data-testid="stFormSubmitButton"] > button { border: 1px solid #aeb8c7 !important; border-radius: 5px !important; box-shadow: none !important;
  min-height: 40px; font-family: "Aptos", "Segoe UI", sans-serif !important; font-size: 14px !important; font-weight: 600 !important; }
.stButton > button[kind^="primary"], [data-testid="stFormSubmitButton"] > button[kind^="primary"] { background: linear-gradient(180deg, #0e48a5 0%, #073a89 100%) !important;
  color: #fff !important; border-color: #073a89 !important; padding: 0 24px !important; }
.stButton > button:hover, [data-testid="stFormSubmitButton"] > button:hover { border-color: var(--blue) !important; color: var(--blue) !important; }
.stButton > button[kind^="primary"]:hover, [data-testid="stFormSubmitButton"] > button[kind^="primary"]:hover { color: #fff !important; filter: brightness(1.04); }
label:has(input[role="switch"]:checked) > div:first-of-type { background-color: var(--blue) !important; }
[data-testid="stChatInput"] { border-color: #aab4c4 !important; border-radius: 5px !important; box-shadow: none !important; }
[data-testid="stChatInput"] > div { border-color: #aab4c4 !important; box-shadow: none !important; }
[data-testid="stChatInput"]:focus-within > div { border-color: var(--blue) !important;
  box-shadow: 0 0 0 1px rgba(11, 63, 150, .12) !important; }
[data-testid="stChatInput"] textarea { font-family: "Aptos", "Segoe UI", sans-serif !important; }
[data-testid="stStatusWidget"] { border-radius: 6px; border-color: var(--line); }

@media (max-width: 1050px) {
  [data-testid="stColumn"]:has(.left-rail-marker) { padding-left: 6px; padding-right: 6px; }
  [data-testid="stColumn"]:has(.left-rail-marker) .stButton > button { padding-left: 8px !important; padding-right: 8px !important;
    font-size: 12px !important; }
  .main-workspace { padding-left: 24px; padding-right: 24px; }
  .plan-row { grid-template-columns: 38px 36px minmax(160px, 1.4fr) 1fr; }
  .plan-detail:last-child { display: none; }
  .plan-provenance-item { padding-left: 7px; padding-right: 7px; }
}
@media (max-width: 760px) {
  [data-testid="stHorizontalBlock"]:has(.left-rail-marker) { display: flex !important; flex-direction: column !important;
    width: 100% !important; max-width: 100% !important; }
  [data-testid="stHorizontalBlock"]:has(.left-rail-marker) > [data-testid="stColumn"] { flex: 1 1 100% !important;
    width: 100% !important; min-width: 0 !important; max-width: 100% !important; }
  .project-meta { display: none; }
  .app-topbar { padding-left: 16px; }
  .brand { font-size: 17px; }
  .right-rail { border-left: 0; min-height: auto; padding-top: 10px; }
  [data-testid="stColumn"]:has(.left-rail-marker) { min-height: auto; border-right: 0; padding: 8px; }
  [data-testid="stColumn"]:has(.left-rail-marker) [data-testid="stVerticalBlock"] { display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr)); }
  [data-testid="stColumn"]:has(.left-rail-marker) [data-testid="stElementContainer"]:has(.left-rail-marker) { display: none; }
  [data-testid="stColumn"]:has(.left-rail-marker) .stButton > button { min-height: 42px !important; justify-content: center;
    border-left: 0 !important; border-bottom: 2px solid transparent !important; font-size: 10px !important;
    letter-spacing: -.015em; line-height: 1.2 !important; padding: 4px 2px !important; white-space: normal; }
  [data-testid="stColumn"]:has(.left-rail-marker) .stButton > button[kind="primary"] { border-bottom-color: var(--blue) !important; }
  [data-testid="stColumn"]:has(.main-workspace-marker) { min-height: auto; padding: 22px 16px; }
  .summary-grid, .workspace-page-grid, .connection-card { grid-template-columns: 1fr; }
  .connection-card > div + div { border-left: 0; border-top: 1px solid var(--line-soft); }
  .plan-row { grid-template-columns: 34px 34px 1fr; padding: 10px 0; }
  .plan-detail { display: none; }
  .plan-provenance { grid-template-columns: 1fr; }
  .plan-provenance-item + .plan-provenance-item { border-left: 0; border-top: 1px solid var(--line-soft); }
  .scenario-card-grid { grid-template-columns: 1fr; }
}
</style>
"""
