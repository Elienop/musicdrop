// frontend/src/components/icons.ts
//
// The icon concept module (spec §3): pages import CONCEPTS, not glyphs —
// one concept = one Phosphor icon, so swapping a glyph is a one-line change
// here and no two pages drift onto different icons for the same idea.
// Default weight is "light" — glyphs don't pass `weight` themselves. It comes
// from ONE constant (ICON_WEIGHT below), fed to an IconContext by App for the
// shell and by each route that renders outside it (the sign-in page, the
// admission fallback). The sanctioned local overrides are keyed on the glyph's
// SIZE, not on where it sits: at the large step it takes thin, and at the
// smallest control it takes bold.
// Stop is StopCircle, not the bare Stop square: at the app's light weight
// that square reads as an unchecked checkbox on the 40px stopped panel
// (owner's call 2026-09-20, made from the rendered page). The ring carries
// the "control" meaning a fill used to, without leaving the one weight.
// The one spinner is Spinner (CircleNotch) + className "animate-spin".
// `Success` and `Online` intentionally share CheckCircle (spec's map; online
// status always pairs the icon with text, never color alone).
export {
  // Navigation
  SquaresFourIcon as Overview,
  UsersIcon as Artists,
  FadersIcon as Browse,
  BinocularsIcon as FindMusic,
  DownloadSimpleIcon as Downloads,
  TrayIcon as Review,
  FolderPlusIcon as AddFromFolder,
  PlaylistIcon as Playlists,
  CopyIcon as Duplicates,
  GearSixIcon as Settings,
  PulseIcon as Activity,
  MagnifyingGlassIcon as Search,
  // Shell chrome: the mobile-nav hamburger.
  ListIcon as Menu,
  // Status
  CheckCircleIcon as Success,
  WarningIcon as Warning,
  XCircleIcon as Error,
  InfoIcon as Info,
  CheckCircleIcon as Online,
  // Domain
  MusicNotesIcon as MusicFallback,
  FileTextIcon as Lyrics,
  PencilSimpleIcon as Edit,
  ImageSquareIcon as Cover,
  MusicNoteIcon as Track,
  VinylRecordIcon as Albums,
  ClockIcon as Duration,
  HardDrivesIcon as Storage,
  CompassIcon as NotFound,
  ShieldCheckIcon as Resolved,
  MinusIcon as Missing,
  // Actions
  PlusIcon as Add,
  TrashIcon as Remove,
  XIcon as Close,
  PauseIcon as Pause,
  StopCircleIcon as Stop,
  CaretLeftIcon as Back,
  CaretRightIcon as Forward,
  CaretDoubleLeftIcon as SkipBack,
  CaretDoubleRightIcon as SkipForward,
  CircleNotchIcon as Spinner,
  CheckIcon as Confirm,
  ChecksIcon as Merge,
  CaretDownIcon as Expand,
  ArrowSquareOutIcon as External,
  MusicNotesPlusIcon as AddToPlaylist,
  ArrowsClockwiseIcon as Refresh,
  ArrowsLeftRightIcon as Replace,
  ArrowCounterClockwiseIcon as Reset,
  FloppyDiskIcon as SaveArt,
  BroomIcon as Reorganize,
  UploadSimpleIcon as Upload,
  ArrowUpIcon as MoveUp,
  ArrowDownIcon as MoveDown,
  SignOutIcon as SignOut,
} from "@phosphor-icons/react";

// The provider half of the weight rule. Re-exported here so "everything
// icon-related comes from icons.ts" holds literally: the three routes that
// mount their own provider (App, the sign-in page, the admission fallback)
// take the context and the value it carries from the same module, instead of
// reaching past it into Phosphor for one and here for the other.
export { IconContext } from "@phosphor-icons/react";

import type { Icon } from "@phosphor-icons/react";

/**
 * ONE icon weight app-wide, as an IconContext value: every Phosphor glyph
 * without an explicit `weight` renders LIGHT (nav, status, buttons…).
 *
 * Deliberate overrides stay local, and the rule behind them is SIZE, not
 * place. Phosphor's stroke is a fraction of the viewBox, so it scales with the
 * box (Stop: light = 12 of 256 units, thin = 8) — a weight that reads right at
 * 16px reads heavy at 40. So a glyph at the LARGE step takes `thin`, and the
 * smallest control glyph takes `bold`.
 *
 * Census of all 18 `weight=` sites, 2026-09-20: every thin one renders at
 * `size-10` or `size-14`, and they are NOT all detail-rail actions — StatTile
 * (56px, the biggest in the app), the topbar's Activity and Sign out, and
 * Add-to-playlist's rail-sized trigger are not. The one bold is the checkbox's
 * 14px tick, where the light stroke thins away.
 *
 * A module constant so the provider value stays referentially stable across
 * re-renders, and it lives HERE rather than in App because App's provider no
 * longer covers everything: the routes outside the shell (the sign-in page,
 * the admission fallback) mount their own, and a second literal would be free
 * to drift onto a different weight.
 */
export const ICON_WEIGHT = { weight: "light" } as const;

/** The type every primitive's `icon` prop accepts — Phosphor's component
 * type (size/color/weight props, ref to SVGSVGElement). */
export type AppIcon = Icon;
