import { useEffect, useId, useRef, useState } from "react";
import type { InputHTMLAttributes } from "react";
import { numericInputError } from "../lib/featureCommands";

/** Text entry avoids native wheel increments; retain numeric form constraints. */
export function NumericInput({ min, max, step = "any", onChange, onInvalid, ...props }:
  Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "inputMode">) {
  const ref = useRef<HTMLInputElement>(null);
  const errorId = useId();
  const [error, setError] = useState("");
  useEffect(() => {
    if (ref.current) {
      ref.current.setCustomValidity(numericInputError(ref.current.value, min, max, step));
      if (ref.current.validity.valid && error) setError("");
    }
  });
  return <><input {...props} ref={ref} type="text" inputMode="decimal"
    aria-invalid={error ? true : props["aria-invalid"]}
    aria-describedby={[props["aria-describedby"], error ? errorId : ""].filter(Boolean).join(" ") || undefined}
    onInvalid={event => { setError(event.currentTarget.validationMessage); onInvalid?.(event); }} onChange={event => {
    event.currentTarget.setCustomValidity(numericInputError(event.currentTarget.value, min, max, step));
    if (error) setError(event.currentTarget.validationMessage);
    onChange?.(event);
  }} />{error && <span id={errorId} role="alert" className="block text-red">{error}</span>}</>;
}
