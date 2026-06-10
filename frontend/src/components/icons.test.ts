// frontend/src/components/icons.test.ts
import { render } from "@testing-library/react";
import { createElement } from "react";
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
  "Brand",
  "MusicFallback",
  "Lyrics",
  "Edit",
  "Cover",
  // Actions
  "Add",
  "Remove",
  "Close",
  "Stop",
  "Back",
  "Forward",
  "Spinner",
] as const satisfies readonly (keyof typeof icons)[];

test.each(CONCEPTS)("%s renders an svg glyph", (name) => {
  // Phosphor components are forwardRef exotics (`typeof` is "object", NOT
  // "function") — assert renderability, not typeof.
  const IconComponent: AppIcon = icons[name];
  const { container, unmount } = render(createElement(IconComponent));
  expect(container.querySelector("svg")).not.toBeNull();
  unmount();
});
