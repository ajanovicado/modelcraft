import type { ComponentType, ButtonHTMLAttributes } from "react";
import { Check, Loader2, RotateCcw } from "lucide-react";
import type { AsyncActionStatus } from "./asyncAction";
import "./async-action-button.css";

/** Feedback is controlled by the caller's real request/receipt, with no success timer. */
export function AsyncActionButton({ status = "idle", label, pendingLabel, successLabel, retryLabel, icon: Icon, className = "secondary", disabled, ...props }: Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> & {
  status?: AsyncActionStatus;
  label: string;
  pendingLabel: string;
  successLabel: string;
  retryLabel: string;
  icon?: ComponentType<{ size?: number; "aria-hidden"?: boolean }>;
}) {
  const labels = { idle: label, pending: pendingLabel, success: successLabel, error: retryLabel };
  return <button {...props} type={props.type ?? "button"} className={`${className} async-action-button`} data-action-state={status} disabled={disabled || status === "pending" || status === "success"} aria-busy={status === "pending"}>
    <span className="async-action-icon" aria-hidden="true">{status === "pending" ? <Loader2 size={14} className="async-action-spinner" /> : status === "success" ? <Check size={14} /> : status === "error" ? <RotateCcw size={14} /> : Icon ? <Icon size={14} /> : null}</span>
    <span className="async-action-labels">{Object.entries(labels).map(([phase, text]) => <span key={phase} className={phase === status ? "async-action-label is-visible" : "async-action-label"} aria-hidden={phase !== status}>{text}</span>)}</span>
  </button>;
}
