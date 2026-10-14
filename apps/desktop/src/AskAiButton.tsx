import { Sparkles } from "lucide-react";

/** Opens a contextual conversation; asking a question remains an explicit send. */
export function AskAiButton({ about, onClick, disabled = false, disabledReason, className = "" }: {
  about: string;
  onClick: () => void;
  disabled?: boolean;
  disabledReason?: string;
  className?: string;
}) {
  return <button type="button" className={`ai-action ${className}`.trim()}
    disabled={disabled} aria-label={`Ask AI about ${about}`} aria-controls="workspace-conversation"
    title={disabled && disabledReason ? disabledReason : `Ask about ${about} in the conversation`}
    onClick={onClick}><Sparkles size={16} aria-hidden="true" />Ask AI</button>;
}
