import { Component, type ErrorInfo, type ReactNode } from "react";

type Props = { children: ReactNode };
type State = { error?: Error };

export class ErrorBoundary extends Component<Props, State> {
  state: State = {};

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Studio UI crashed", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <main className="content-page" role="alert" style={{ padding: "2rem", maxWidth: "40rem" }}>
        <h1>Something went wrong in the studio UI</h1>
        <p>
          Reload the studio to recover saved work. Unsaved edits may need to be entered again.
          If this keeps happening, restart the desktop app.
        </p>
        <details><summary>Error details</summary><p className="muted">{this.state.error.message}</p></details>
        <button type="button" className="primary" onClick={() => window.location.reload()}>
          Reload studio
        </button>
      </main>
    );
  }
}
