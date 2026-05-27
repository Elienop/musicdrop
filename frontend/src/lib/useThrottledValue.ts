import { useEffect, useRef, useState } from "react";

/**
 * Throttle how often `value` is surfaced. Returns the latest value but updates
 * at most once per `intervalMs`: leading-edge (the first value, and any change
 * arriving after the interval has elapsed, emits immediately) with a trailing
 * edge (rapid changes within the window coalesce, then the latest is emitted
 * when the window closes). Used to keep a polling-driven `aria-live` region
 * from announcing on every 1s poll while sighted users read the un-throttled
 * visible copy.
 */
export function useThrottledValue<T>(value: T, intervalMs: number): T {
  const [throttled, setThrottled] = useState(value);
  const lastEmit = useRef(Date.now());
  const latest = useRef(value);
  latest.current = value;

  useEffect(() => {
    const sinceLast = Date.now() - lastEmit.current;
    if (sinceLast >= intervalMs) {
      lastEmit.current = Date.now();
      setThrottled(value);
      return;
    }
    const timer = setTimeout(() => {
      lastEmit.current = Date.now();
      setThrottled(latest.current);
    }, intervalMs - sinceLast);
    return () => clearTimeout(timer);
  }, [value, intervalMs]);

  return throttled;
}
