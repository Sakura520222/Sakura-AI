import { Component, type ReactNode } from "react";

export class ErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    if (this.state.failed)
      return (
        <div className="auth-screen" role="alert">
          <h1>页面遇到问题 / Something went wrong</h1>
          <p>
            请重新加载页面。未保存的修改不会提交。
            <br />
            Reload this page. Unsaved changes have not been submitted.
          </p>
          <button
            className="button primary"
            onClick={() => window.location.reload()}
          >
            重新加载 / Reload
          </button>
        </div>
      );
    return this.props.children;
  }
}
