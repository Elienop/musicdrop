import {
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import {
  type FolderBadge,
  type FolderEntry,
  type FolderListing,
  useFolderListing,
} from "@/api/useFolders";
import {
  BrowseFolders,
  Folder,
  MoveUp,
  Spinner,
} from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

const BADGE_LABEL: Record<FolderBadge, string> = {
  library: "Library",
  musicdrop: "MusicDrop",
};

/** A folder path without its trailing slashes; `/` stays `/`. A loop, not a
 * `/\/+$/` regex, which backtracks on every run of slashes. */
function withoutTrailingSlash(path: string): string {
  let end = path.length;
  while (end > 1 && path[end - 1] === "/") end -= 1;
  return path.slice(0, end);
}

/** How the path box shows a listed folder: with a trailing `/`, so what the
 * user types next narrows its rows. */
function withSlash(folder: string): string {
  return folder === "/" ? "/" : `${folder}/`;
}

/** The folder the box names: its text up to the last `/`. Null when the text
 * is not an absolute path, which then only narrows the listed folder. */
function folderPart(text: string): string | null {
  if (!text.startsWith("/")) return null;
  return withoutTrailingSlash(text.slice(0, text.lastIndexOf("/") + 1));
}

/** What narrows the rows: the box's text after the last `/`. */
function filterPart(text: string): string {
  return text.slice(text.lastIndexOf("/") + 1);
}

/** Where the dialog opens: the field's own path, else the newest Recent
 * folder, else `null`, which asks the server for its default. */
function openingPath(value: string, recent: string | null): string | null {
  const start = [value.trim(), recent?.trim() ?? ""].find((path) =>
    path.startsWith("/"),
  );
  return start === undefined ? null : withoutTrailingSlash(start);
}

/** Where focus goes when the next listing lands. */
type FocusNext = { kind: "first" } | { kind: "row"; path: string } | null;

export interface FolderBrowserDialogProps {
  /** The path field's text. The dialog opens at that folder when it is set. */
  value: string;
  /** The newest Recent folder, tried when the field is empty. */
  recent?: string | null;
  /** The field's own setter, so whatever typing clears (a stale refusal)
   * clears for a browsed folder too. */
  onUse: (path: string) => void;
  /** The field, which takes focus after Use. */
  fieldRef: RefObject<HTMLInputElement | null>;
}

/**
 * The Browse folders button and its dialog: one folder's child folders at a
 * time, from `GET /api/folders`. It only picks a path; the field it fills
 * decides what happens next.
 *
 * Focus: Cancel and Escape return it to this trigger (Radix, through
 * `DialogTrigger`); Use sends it to the field instead.
 */
export function FolderBrowserDialog({
  value,
  recent = null,
  onUse,
  fieldRef,
}: Readonly<FolderBrowserDialogProps>) {
  const [open, setOpen] = useState(false);
  // Set by Use, read after the close by onCloseAutoFocus.
  const used = useRef(false);
  const contentRef = useRef<HTMLDivElement>(null);

  function onOpenChange(next: boolean) {
    if (next) used.current = false;
    setOpen(next);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <Tooltip>
        <TooltipTrigger asChild>
          <DialogTrigger asChild>
            <Button
              type="button"
              variant="outline"
              size="icon"
              aria-label="Browse folders"
            >
              <BrowseFolders aria-hidden="true" />
            </Button>
          </DialogTrigger>
        </TooltipTrigger>
        <TooltipContent>Browse folders</TooltipContent>
      </Tooltip>
      <DialogContent
        ref={contentRef}
        // Not the path box: on a phone that raises the keyboard over the list.
        // The content holds focus until the first row arrives.
        onOpenAutoFocus={(e) => {
          e.preventDefault();
          contentRef.current?.focus();
        }}
        onCloseAutoFocus={(e) => {
          if (!used.current) return;
          e.preventDefault();
          fieldRef.current?.focus();
        }}
      >
        <FolderBrowser
          openAt={openingPath(value, recent)}
          contentRef={contentRef}
          onUse={(path) => {
            used.current = true;
            onUse(path);
            setOpen(false);
          }}
        />
      </DialogContent>
    </Dialog>
  );
}

/** The dialog's body. Mounted only while the dialog is open, so every open
 * starts from `openAt` with no state left from the last one. */
function FolderBrowser({
  openAt,
  contentRef,
  onUse,
}: Readonly<{
  openAt: string | null;
  contentRef: RefObject<HTMLDivElement | null>;
  onUse: (path: string) => void;
}>) {
  const refusalId = useId();
  // The folder asked for; the listing may name another (see useFolderListing).
  const [folder, setFolder] = useState(openAt);
  const [text, setText] = useState(openAt === null ? "" : withSlash(openAt));
  // The last folder that listed. It stays on screen while the next one loads,
  // and after it fails.
  const [shown, setShown] = useState<FolderListing | undefined>(undefined);
  const query = useFolderListing(folder);
  const focusNext = useRef<FocusNext>({ kind: "first" });
  const listRef = useRef<HTMLDivElement>(null);
  const boxRef = useRef<HTMLInputElement>(null);

  const landed = query.data;
  if (landed !== undefined && landed !== shown) {
    setShown(landed);
    if (folderPart(text) !== landed.path) setText(withSlash(landed.path));
    if (landed.path !== folder) setFolder(landed.path);
  }

  useFocusWhenListed(shown, focusNext, listRef, contentRef);

  // Unfocused, the box shows its END: the folder "Use this folder" takes. A
  // listing sets the text while focus is on a row, and a box set that way
  // shows its start, so at 360px the folder's own name was cut off. Focused,
  // the caret decides. The blur has its own line on the box below.
  useLayoutEffect(() => {
    const box = boxRef.current;
    if (box !== null && document.activeElement !== box) {
      box.scrollLeft = box.scrollWidth;
    }
  }, [text]);

  const loading = query.isPending;
  const failure = query.isError ? query.error.message : null;
  // The typed tail narrows only the folder it belongs to; while another
  // folder loads, the last one shows whole.
  const listedPath = shown?.path;
  const boxNamesShown =
    listedPath !== undefined &&
    (folderPart(text) ?? listedPath) === listedPath;
  const filter = boxNamesShown ? filterPart(text) : "";
  const rows =
    shown?.folders.filter((entry) =>
      entry.name.toLowerCase().includes(filter.toLowerCase()),
    ) ?? [];
  const refusal = shown?.refusal ?? null;
  // Use takes the listed folder, so only while the box names it: after a
  // typed path fails, the box shows that path and the list is the last one.
  const useBlocked = loading || !boxNamesShown || refusal !== null;
  // The line under the list: the refusal, else how many are not shown.
  const note =
    refusal ??
    (shown !== undefined && shown.total > shown.folders.length
      ? `Showing ${shown.folders.length.toLocaleString("en-US")} of ${shown.total.toLocaleString("en-US")}. Type a path to open it.`
      : null);

  function openFolder(path: string, next: FocusNext) {
    focusNext.current = next;
    if (path === folder) {
      // The same folder again, after it failed: ask again.
      void query.refetch();
      return;
    }
    setFolder(path);
  }

  function onTextChange(next: string) {
    setText(next);
    const part = folderPart(next);
    if (part !== null && part !== folder) setFolder(part);
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const first = rows[0];
    // Only rows of the folder the box names: after a failed path, the list
    // on screen is the last one and is not what was typed. And only a typed
    // name: at `/media/downloads/` the box names the folder already open.
    if (loading || !boxNamesShown || filter === "" || first === undefined) {
      return;
    }
    // Typing goes on in the box, so focus stays there.
    openFolder(first.path, null);
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>Choose a folder</DialogTitle>
        <DialogDescription className="sr-only">
          Open a folder, then use it.
        </DialogDescription>
      </DialogHeader>

      <div className="flex min-w-0 flex-col gap-2">
        <div className="relative">
          <Input
            ref={boxRef}
            type="text"
            value={text}
            onChange={(e) => onTextChange(e.target.value)}
            onKeyDown={onKeyDown}
            // Chromium scrolls a box back to its start on blur, after `blur`
            // and before `focusout`, which is what React's onBlur listens to
            // (measured in Orca, 2026-09-26), so this line lands after it.
            onBlur={(e) => {
              e.currentTarget.scrollLeft = e.currentTarget.scrollWidth;
            }}
            aria-label="Folder path"
            // The start route's bound, as on the page's own path box.
            maxLength={4096}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            // Room for the spinner below, so the text never runs under it.
            // Mono, as every path box in Settings is.
            className="pr-9 font-mono"
          />
          {/* A later load keeps the last list on screen; the spinner sits in
              the box so nothing below it moves. */}
          {loading && shown !== undefined && (
            <Spinner
              className="text-muted-foreground pointer-events-none absolute top-1/2 right-3 size-4 -translate-y-1/2 animate-spin"
              aria-hidden="true"
            />
          )}
        </div>
        {/* A failure with a list on screen: the list stays, the reason
            sits under the box that asked. With nothing listed yet, the
            reason fills the list box instead. */}
        {failure !== null && shown !== undefined && (
          <p className="text-destructive text-sm break-words" role="alert">
            {failure}
          </p>
        )}
      </div>

      <div className="flex min-w-0 flex-col gap-2">
        <div
          ref={listRef}
          aria-busy={loading}
          className="thin-scrollbar h-[min(18rem,40dvh)] overflow-y-auto rounded-md border"
        >
          {shown === undefined ? (
            <FirstLoad failure={failure} />
          ) : (
            <FolderRows
              listing={shown}
              rows={rows}
              filter={filter}
              onOpen={openFolder}
            />
          )}
        </div>
        {/* Always there, a line tall when empty, so the dialog (centred)
            keeps its height as the refusal and the cap line come and go. */}
        <p
          id={refusalId}
          className="text-muted-foreground min-h-lh text-xs break-words"
        >
          {note}
        </p>
      </div>

      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" variant="outline">
            Cancel
          </Button>
        </DialogClose>
        <Button
          type="button"
          // Never `disabled`: the reason must stay readable on a focused button.
          aria-disabled={useBlocked}
          aria-describedby={refusal === null ? undefined : refusalId}
          className="aria-disabled:opacity-50"
          onClick={() => {
            if (useBlocked || shown === undefined) return;
            onUse(shown.path);
          }}
        >
          Use this folder
        </Button>
      </DialogFooter>
    </>
  );
}

/**
 * Focus when a listing lands, per `focusNext`: the first folder row (the Up
 * row when there is none), or the row the user came up from. Only while focus
 * is still where the dialog left it: outside the dialog (the body, after the
 * clicked row left with its folder, or still the trigger when a cached
 * listing lands before Radix moves focus in), on the content (just opened),
 * or in the list. Never out of the path box or the footer.
 */
function useFocusWhenListed(
  shown: FolderListing | undefined,
  focusNext: RefObject<FocusNext>,
  listRef: RefObject<HTMLDivElement | null>,
  contentRef: RefObject<HTMLDivElement | null>,
) {
  useEffect(() => {
    const target = focusNext.current;
    const list = listRef.current;
    if (shown === undefined || target === null || list === null) return;
    focusNext.current = null;
    const active = document.activeElement;
    const content = contentRef.current;
    const untouched =
      active === null ||
      content === null ||
      !content.contains(active) ||
      active === content ||
      list.contains(active);
    if (!untouched) return;
    const buttons = [...list.querySelectorAll<HTMLButtonElement>("button")];
    const back =
      target.kind === "row"
        ? buttons.find((row) => row.dataset.path === target.path)
        : undefined;
    const first =
      buttons.find((row) => row.dataset.path !== undefined) ?? buttons[0];
    (back ?? first)?.focus();
  }, [shown, focusNext, listRef, contentRef]);
}

/** The list box before anything has listed: the spinner, or the failure. */
function FirstLoad({ failure }: Readonly<{ failure: string | null }>) {
  if (failure !== null) {
    return (
      <p
        className="text-destructive flex h-full items-center justify-center px-3 text-center text-sm break-words"
        role="alert"
      >
        {failure}
      </p>
    );
  }
  return (
    <p className="text-muted-foreground flex h-full items-center justify-center gap-2 text-sm">
      <Spinner className="size-4 animate-spin" aria-hidden="true" />
      Loading&hellip;
    </p>
  );
}

/** The Up row, the folder rows that match, or the empty line. */
function FolderRows({
  listing,
  rows,
  filter,
  onOpen,
}: Readonly<{
  listing: FolderListing;
  rows: FolderListing["folders"];
  filter: string;
  onOpen: (path: string, next: FocusNext) => void;
}>) {
  const { parent } = listing;
  return (
    <ul className="flex min-h-full flex-col divide-y">
      {parent !== null && (
        <li>
          <RowButton
            onClick={() => onOpen(parent, { kind: "row", path: listing.path })}
          >
            <MoveUp className="size-4 shrink-0" aria-hidden="true" />
            Up
          </RowButton>
        </li>
      )}
      {withKeys(rows).map(({ entry, key }) => (
        <li key={key}>
          <RowButton
            path={entry.path}
            onClick={() => onOpen(entry.path, { kind: "first" })}
          >
            <Folder className="size-4 shrink-0" aria-hidden="true" />
            <span className="min-w-0 flex-1 break-words">{entry.name}</span>
            {/* The space keeps the name and the badge two words ("music
                Library", not "musicLibrary"); a flex row does not render it. */}
            {entry.badge !== null && (
              <>
                {" "}
                <Badge variant="secondary">{BADGE_LABEL[entry.badge]}</Badge>
              </>
            )}
          </RowButton>
        </li>
      ))}
      {rows.length === 0 && (
        <li className="flex flex-1 items-center justify-center px-3 py-2">
          <p className="text-muted-foreground text-center text-sm break-words">
            {emptyLine(listing, filter)}
          </p>
        </li>
      )}
    </ul>
  );
}

/** The line when no row shows. Over the cap only the first rows came, so
 * narrowing searched only those. */
function emptyLine(listing: FolderListing, filter: string): string {
  const listed = listing.folders.length;
  if (listed === 0) return "No subfolders.";
  if (listing.total > listed) {
    return `None of the first ${listed.toLocaleString("en-US")} match “${filter}”.`;
  }
  return `No folders match “${filter}”.`;
}

/** Each row with a key of its own. Two folders can display alike (names that
 * are not UTF-8 read the same), so the path alone repeats; a repeat gets its
 * count after it. */
function withKeys(
  rows: FolderListing["folders"],
): { entry: FolderEntry; key: string }[] {
  const seen = new Map<string, number>();
  return rows.map((entry) => {
    const count = (seen.get(entry.path) ?? 0) + 1;
    seen.set(entry.path, count);
    return { entry, key: count === 1 ? entry.path : `${entry.path}\u0000${count}` };
  });
}

/** One row of the list: a plain button, not the Button primitive, whose base
 * is `whitespace-nowrap` and `justify-center`, so a long name cannot wrap. The
 * ring is inset because the list clips anything drawn outside a row. */
function RowButton({
  path,
  onClick,
  children,
}: Readonly<{ path?: string; onClick: () => void; children: ReactNode }>) {
  return (
    <button
      type="button"
      data-path={path}
      onClick={onClick}
      className="focus-ring hover:bg-surface-hover flex w-full min-w-0 items-center gap-3 px-3 py-2 text-left text-sm focus-visible:ring-inset"
    >
      {children}
    </button>
  );
}
