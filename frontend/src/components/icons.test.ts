// frontend/src/components/icons.test.ts
import { StopIcon } from "@phosphor-icons/react";
import { render } from "@testing-library/react";
import { createElement } from "react";
import type { ReactElement } from "react";
import { expect, test } from "vitest";

import * as icons from "@/components/icons";
import type { AppIcon } from "@/components/icons";

// Compile-time guarantee that every concept exists as a value export —
// `satisfies` fails tsc if a concept is missing or misspelled, which is the
// spec's "icons.ts compiles = the map is real" check.
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
  "Stop",
  "Back",
  "Forward",
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
] as const satisfies readonly (keyof typeof icons)[];

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
    d(createElement(StopIcon, { weight: "light" })),
  );
  expect(d(inApp(createElement(icons.Stop)))).not.toBe(
    d(createElement(StopIcon, { weight: "fill" })),
  );
  // A call site may still ask for another weight — the detail rail's size-10
  // Stop does, in a rail of thin glyphs.
  expect(d(createElement(icons.Stop, { weight: "thin" }))).toBe(
    d(createElement(StopIcon, { weight: "thin" })),
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
