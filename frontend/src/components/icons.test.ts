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

test("Stop is the FILLED glyph, not the app's light default", () => {
  // The one concept that carries its own weight. Compared against Phosphor
  // directly, in both directions: equal to the fill weight, and NOT equal to
  // what the same page renders without one — without the second half the
  // assertion passes against a plain re-export, which is what this replaced.
  const d = (node: ReactElement) => {
    const { container, unmount } = render(node);
    const path = container.querySelector("path")?.getAttribute("d") ?? "";
    unmount();
    return path;
  };
  // The control has to be rendered where the app renders its glyphs: an
  // unweighted Phosphor icon outside the context takes Phosphor's own
  // `regular`, so comparing against that measures a weight the app never ships.
  const inApp = (node: ReactElement) =>
    createElement(icons.IconContext.Provider, { value: icons.ICON_WEIGHT }, node);
  expect(d(createElement(icons.Stop))).toBe(
    d(createElement(StopIcon, { weight: "fill" })),
  );
  // The premise the title rests on: inside the app's provider an unweighted
  // glyph really is the light one.
  expect(d(inApp(createElement(StopIcon)))).toBe(
    d(createElement(StopIcon, { weight: "light" })),
  );
  expect(d(inApp(createElement(icons.Stop)))).not.toBe(
    d(inApp(createElement(StopIcon))),
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
