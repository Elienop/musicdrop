import { type RefObject, useEffect } from "react";

const PINNED_TOP_PX = 96; // the rail's md:top-24 sticky offset
const BOTTOM_GAP_PX = 24; // the 1.5rem breathing room kept below the rail

/**
 * Correct the sticky filter rail's max-height BEFORE the sticky pins.
 *
 * The CSS fallback (`md:max-h-[calc(100vh-7.5rem)]`) assumes the rail sits
 * at its pinned offset (6rem). While the page header is still in view the
 * rail starts lower, so the static max-height pushes the scroll box's
 * bottom — and the last facet group — below the viewport, where the box's
 * own scrollbar can never reveal it (there is no CSS `:stuck` to branch
 * on). Measure the real top on scroll/resize (rAF-throttled, passive) and
 * size the box to end at the bottom gap instead; at the pinned position
 * this reproduces the CSS value exactly. Only active at md+ — below that
 * the rail is a fixed-height box (`max-h-72`) and the inline style is
 * cleared so the class rules.
 */
export function useRailMaxHeight(ref: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const el = ref.current;
    if (el === null) return;
    const media = window.matchMedia("(min-width: 768px)");
    let frame = 0;

    const measure = () => {
      frame = 0;
      if (!media.matches) {
        el.style.maxHeight = "";
        return;
      }
      const top = Math.max(el.getBoundingClientRect().top, PINNED_TOP_PX);
      el.style.maxHeight = `${window.innerHeight - top - BOTTOM_GAP_PX}px`;
    };
    const schedule = () => {
      if (frame === 0) frame = requestAnimationFrame(measure);
    };

    measure();
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule, { passive: true });
    media.addEventListener("change", schedule);
    return () => {
      if (frame !== 0) cancelAnimationFrame(frame);
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      media.removeEventListener("change", schedule);
      el.style.maxHeight = "";
    };
  }, [ref]);
}
