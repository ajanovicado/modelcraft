import { useEffect, useId, useState } from "react";

export function forecastNumberError(text: string, minimum: number, maximum: number, allowAuto = false): string | undefined {
  if (!text.trim() && allowAuto) return undefined;
  const value = Number(text);
  return text.trim() && Number.isInteger(value) && value >= minimum && value <= maximum
    ? undefined : `Use a whole number from ${minimum} to ${maximum}.`;
}

/** Keep incomplete edits visible without putting invalid values into the draft history. */
export function ForecastNumberInput({ label, value, minimum, maximum, allowAuto = false, onValueChange, onValidityChange }: {
  label: string; value: number | null | undefined; minimum: number; maximum: number; allowAuto?: boolean;
  onValueChange: (value: number | null) => void; onValidityChange: (error?: string) => void;
}) {
  const [text, setText] = useState(value == null ? "" : String(value));
  const descriptionId = useId();
  useEffect(() => { setText(value == null ? "" : String(value)); }, [value]);
  const error = forecastNumberError(text, minimum, maximum, allowAuto);
  return <label><span>{label}</span><input type="number" min={minimum} max={maximum} step={1}
    value={text} placeholder={allowAuto ? "Auto" : undefined} aria-label={label} aria-invalid={Boolean(error)} aria-describedby={error || allowAuto ? descriptionId : undefined}
    onChange={event => {
      const next = event.target.value;
      setText(next);
      const nextError = forecastNumberError(next, minimum, maximum, allowAuto);
      onValidityChange(nextError);
      if (!nextError) {
        const nextValue = next.trim() ? Number(next) : null;
        if (nextValue !== (value ?? null)) onValueChange(nextValue);
      }
    }} />
    {(error || allowAuto) && <small id={descriptionId} className={error ? "designer-field-error" : "designer-binding-note"}>{error || "Leave blank for automatic seasonality."}</small>}
  </label>;
}
