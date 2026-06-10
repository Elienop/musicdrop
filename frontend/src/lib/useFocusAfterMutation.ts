import { useCallback, useLayoutEffect, useRef } from "react";

/** Handle returned by `useFocusAfterMutation`. */
export interface FocusAfterMutation {
  /** Ref-callback body — `ref={(el) => register("row-3:remove", el)}` keeps
   * the registry in sync as rows mount and unmount. */
  register: (key: string, el: HTMLElement | null) => void;
  /** Queue a focus request alongside the state update that mutates the list.
   * After the next commit, the FIRST registered, non-disabled candidate in
   * `keys` receives focus; later keys are fallbacks (next row, previous row,
   * empty-state element, ...). */
  requestFocus: (...keys: string[]) => void;
}

/**
 * Focus restoration for mutating lists (generalized from PlaylistDetailPage):
 * a remove/reorder unmounts the focused control and drops keyboard focus to
 * <body>. Record which control to refocus (`requestFocus`) in the same
 * handler as the state update; a layout effect applies it after the new
 * order paints, before the browser repaints.
 */
export function useFocusAfterMutation(): FocusAfterMutation {
  const registry = useRef(new Map<string, HTMLElement>());
  const pending = useRef<readonly string[] | null>(null);

  const register = useCallback((key: string, el: HTMLElement | null) => {
    if (el) {
      registry.current.set(key, el);
    } else {
      registry.current.delete(key);
    }
  }, []);

  const requestFocus = useCallback((...keys: string[]) => {
    pending.current = keys;
  }, []);

  // No dependency array on purpose: whichever commit re-renders the mutated
  // list fulfils the request, and when nothing is pending the effect is a
  // single ref read. Clearing BEFORE focusing makes the request one-shot.
  useLayoutEffect(() => {
    const keys = pending.current;
    if (keys === null) {
      return;
    }
    pending.current = null;
    for (const key of keys) {
      const el = registry.current.get(key);
      // `:disabled` skips disabled form controls — focusing those is a
      // silent no-op that would strand keyboard focus on <body>.
      if (el && !el.matches(":disabled")) {
        el.focus();
        return;
      }
    }
  });

  return { register, requestFocus };
}
