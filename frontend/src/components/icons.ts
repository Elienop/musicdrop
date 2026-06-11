// frontend/src/components/icons.ts
//
// The icon concept module (spec §3): pages import CONCEPTS, not glyphs —
// one concept = one Phosphor icon, so swapping a glyph is a one-line change
// here and no two pages drift onto different icons for the same idea.
// Default weight is "light", set ONCE by the app-wide IconContext (App.tsx) —
// glyphs don't pass `weight` themselves. The only sanctioned local overrides:
// detail-rail actions = thin (large size-10 glyphs), checkbox tick = bold
// (tiny control glyph needs the stroke).
// The one spinner is Spinner (CircleNotch) + className "animate-spin".
// `Success` and `Online` intentionally share CheckCircle (spec's map; online
// status always pairs the icon with text, never color alone).
export {
  // Navigation
  SquaresFour as Overview,
  Users as Artists,
  Faders as Browse,
  Binoculars as FindMusic,
  DownloadSimple as Downloads,
  Tray as Review,
  FolderPlus as AddFromFolder,
  Playlist as Playlists,
  Copy as Duplicates,
  GearSix as Settings,
  Pulse as Activity,
  MagnifyingGlass as Search,
  // Shell chrome: the mobile-nav hamburger.
  List as Menu,
  // Status
  CheckCircle as Success,
  Warning,
  XCircle as Error,
  Info,
  CheckCircle as Online,
  // Domain
  MusicNotes as MusicFallback,
  FileText as Lyrics,
  PencilSimple as Edit,
  ImageSquare as Cover,
  MusicNote as Track,
  VinylRecord as Albums,
  Clock as Duration,
  HardDrives as Storage,
  Compass as NotFound,
  ShieldCheck as Resolved,
  Minus as Missing,
  // Actions
  Plus as Add,
  Trash as Remove,
  X as Close,
  Stop,
  CaretLeft as Back,
  CaretRight as Forward,
  CircleNotch as Spinner,
  Check as Confirm,
  Checks as Merge,
  CaretDown as Expand,
  ArrowSquareOut as External,
  MusicNotesPlus as AddToPlaylist,
  ArrowsClockwise as Refresh,
  ArrowsLeftRight as Replace,
  ArrowCounterClockwise as Reset,
  FloppyDisk as SaveArt,
  Broom as Reorganize,
  UploadSimple as Upload,
  ArrowUp as MoveUp,
  ArrowDown as MoveDown,
} from "@phosphor-icons/react";

import type { Icon } from "@phosphor-icons/react";

/** The type every primitive's `icon` prop accepts — Phosphor's component
 * type (size/color/weight props, ref to SVGSVGElement). */
export type AppIcon = Icon;
