// frontend/src/components/icons.ts
//
// The icon concept module (spec §3): pages import CONCEPTS, not glyphs —
// one concept = one Phosphor icon, so swapping a glyph is a one-line change
// here and no two pages drift onto different icons for the same idea.
// Default weight is "regular"; active/selected states pass weight="fill".
// The one spinner is Spinner (CircleNotch) + className "animate-spin".
// `Success` and `Online` intentionally share CheckCircle (spec's map; online
// status always pairs the icon with text, never color alone), and
// `CopyAction` (clipboard copy) shares Copy with the `Duplicates` nav concept
// on the same precedent.
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
  // Brand glyph for the sidebar rail — intentionally shares MusicNotes with
  // MusicFallback (same precedent as Success/Online sharing CheckCircle).
  MusicNotes as Brand,
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
  Circle as RadioDot,
  Copy as CopyAction,
  ArrowSquareOut as External,
  ListPlus as AddToPlaylist,
  ArrowsClockwise as Refresh,
  ArrowsLeftRight as Replace,
  ArrowCounterClockwise as Reset,
  UploadSimple as Upload,
  ArrowUp as MoveUp,
  ArrowDown as MoveDown,
} from "@phosphor-icons/react";

import type { Icon } from "@phosphor-icons/react";

/** The type every primitive's `icon` prop accepts — Phosphor's component
 * type (size/color/weight props, ref to SVGSVGElement). */
export type AppIcon = Icon;
