import type { CSSProperties, KeyboardEvent } from "react";

type Segment<Value extends string> = { value: Value; label: string };
type SegmentedControlProps<Value extends string> = {
  options: readonly Segment<Value>[];
  value: Value;
  onChange: (value: Value) => void;
  label: string;
  className?: string;
  buttonClassName?: string;
} & ({ mode: "tabs"; tabIdPrefix: string; panelIdPrefix: string } | { mode: "filter" });

/** Equal tracks keep selection movement in CSS and labels stationary. */
export function SegmentedControl<Value extends string>(props: SegmentedControlProps<Value>) {
  const { options, value, onChange, label, className = "", buttonClassName = "" } = props;
  if (!options.length) return null;
  const selectedIndex = options.findIndex(option => option.value === value);
  const style = {
    "--segment-count": options.length,
    "--segment-index": Math.max(0, selectedIndex),
  } as CSSProperties;
  const handleTabKey = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const targetIndex = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1
      : event.key === "ArrowRight" ? (index + 1) % options.length
      : event.key === "ArrowLeft" ? (index + options.length - 1) % options.length : undefined;
    if (targetIndex === undefined) return;
    event.preventDefault();
    onChange(options[targetIndex].value);
    event.currentTarget.parentElement?.querySelector<HTMLButtonElement>(`[data-segment-index="${targetIndex}"]`)?.focus();
  };
  return <div className={`segmented-control ${className}`} role={props.mode === "tabs" ? "tablist" : "group"} aria-label={label} style={style}>
    <span className="segmented-control__indicator" aria-hidden="true" hidden={selectedIndex < 0} />
    {options.map((option, index) => <button key={option.value} type="button" className={`segmented-control__button ${buttonClassName}`} data-segment-index={index} data-selected={value === option.value}
      role={props.mode === "tabs" ? "tab" : undefined}
      id={props.mode === "tabs" ? `${props.tabIdPrefix}${option.value}` : undefined}
      aria-controls={props.mode === "tabs" ? `${props.panelIdPrefix}${option.value}` : undefined}
      aria-selected={props.mode === "tabs" ? value === option.value : undefined}
      aria-pressed={props.mode === "filter" ? value === option.value : undefined}
      tabIndex={props.mode === "tabs" ? (value === option.value ? 0 : -1) : undefined}
      onClick={() => onChange(option.value)}
      onKeyDown={props.mode === "tabs" ? event => handleTabKey(event, index) : undefined}
    >{option.label}</button>)}
  </div>;
}
