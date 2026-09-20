// frontend/src/components/icons.test.ts
import { StopCircleIcon } from "@phosphor-icons/react";
import { render } from "@testing-library/react";
import { createElement } from "react";
import type { ReactElement } from "react";
import { expect, test } from "vitest";

import * as icons from "@/components/icons";
import type { AppIcon } from "@/components/icons";

// Every concept the module exports, and the list both checks below run on.
//
// `satisfies` is the compile-time half: tsc fails if a concept here is
// misspelled or not exported. It proves each entry EXISTS and says nothing
// about the list being COMPLETE, which is what the comment used to claim and
// what "every glyph is listed" (below) now actually tests.
const CONCEPTS = [
  // Navigation
  "Overview",
  "Artists",
  "Browse",
  "FindMusic",
  "Downloads",
  "Review",
  "AddFromFolder",
  "Playlists",
  "Duplicates",
  "Settings",
  "Activity",
  "Search",
  "Menu",
  // Status
  "Success",
  "Warning",
  "Error",
  "Info",
  "Online",
  // Domain
  "MusicFallback",
  "Lyrics",
  "Edit",
  "Cover",
  "Albums",
  "Track",
  "Duration",
  "Storage",
  "NotFound",
  "Resolved",
  "Missing",
  // Actions
  "Add",
  "Remove",
  "Close",
  "Pause",
  "Stop",
  "Back",
  "Forward",
  "SkipBack",
  "SkipForward",
  "Spinner",
  "Confirm",
  "Merge",
  "Expand",
  "External",
  "AddToPlaylist",
  "Refresh",
  "Replace",
  "Reset",
  "SaveArt",
  "Reorganize",
  "Upload",
  "MoveUp",
  "MoveDown",
  "SignOut",
] as const satisfies readonly (keyof typeof icons)[];

// The module's two non-glyph value exports: the weight rule's provider and the
// value it carries, re-exported from icons.ts so nothing icon-related has to
// reach past it into Phosphor. Everything else icons.ts exports is a glyph.
const NON_GLYPHS: readonly string[] = ["IconContext", "ICON_WEIGHT"] satisfies
  readonly (keyof typeof icons)[];

test("every glyph icons.ts exports is listed as a concept", () => {
  // The completeness half. Four glyphs were exported and unlisted until
  // 2026-09-20 — Pause, SignOut, SkipBack, SkipForward — so the renderability
  // sweep below never ran on them and nothing said so. Pause is the Stop
  // button's direct sibling, exactly the shape of mistake this list is for.
  const glyphs = Object.keys(icons).filter((name) => !NON_GLYPHS.includes(name));
  // One assertion, both directions: an unlisted glyph fails it, and so would
  // an empty namespace — so a pass cannot be vacuous.
  expect([...glyphs].sort()).toEqual([...CONCEPTS].sort());
});

test("Stop takes the app's one weight, like every other concept", () => {
  // It forced `fill` through a wrapper until the owner's call on 2026-09-20
  // put it back on the app's light stroke, so every job's Stop button matches
  // the rest of the shell. Asserted in BOTH directions: equal to light inside
  // the provider, and not equal to the fill it used to render — without the
  // second half a re-added fill wrapper still passes.
  const d = (node: ReactElement) => {
    const { container, unmount } = render(node);
    const path = container.querySelector("path")?.getAttribute("d") ?? "";
    unmount();
    return path;
  };
  // Measured where the app renders its glyphs: an unweighted Phosphor icon
  // outside the context takes Phosphor's own `regular`, a weight we never ship.
  const inApp = (node: ReactElement) =>
    createElement(icons.IconContext.Provider, { value: icons.ICON_WEIGHT }, node);
  expect(d(inApp(createElement(icons.Stop)))).toBe(
    d(createElement(StopCircleIcon, { weight: "light" })),
  );
  expect(d(inApp(createElement(icons.Stop)))).not.toBe(
    d(createElement(StopCircleIcon, { weight: "fill" })),
  );
  // A call site may still ask for another weight — the detail rail's size-10
  // Stop does, in a rail of thin glyphs.
  expect(d(createElement(icons.Stop, { weight: "thin" }))).toBe(
    d(createElement(StopCircleIcon, { weight: "thin" })),
  );
});

test.each(CONCEPTS)("%s renders an svg glyph", (name) => {
  // Phosphor components are forwardRef exotics (`typeof` is "object", NOT
  // "function") — assert renderability, not typeof.
  const IconComponent: AppIcon = icons[name];
  const { container, unmount } = render(createElement(IconComponent));
  expect(container.querySelector("svg")).not.toBeNull();
  unmount();
});
