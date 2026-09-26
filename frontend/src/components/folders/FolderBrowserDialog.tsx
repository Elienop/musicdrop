import {
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";

import {
  type FolderBadge,
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

  const landed = query.data;
  if (landed !== undefined && landed !== shown) {
    setShown(landed);
    if (folderPart(text) !== landed.path) setText(withSlash(landed.path));
    if (landed.path !== folder) setFolder(landed.path);
  }

  useFocusWhenListed(shown, focusNext, listRef, contentRef);

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
  const useBlocked = loading || shown === undefined || refusal !== null;

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
    // on screen is the last one and is not what was typed.
    if (loading || !boxNamesShown || first === undefined) return;
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
            type="text"
            value={text}
            onChange={(e) => onTextChange(e.target.value)}
            onKeyDown={onKeyDown}
            aria-label="Folder path"
            // The start route's bound, as on the page's own path box.
            maxLength={4096}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            // Room for the spinner below, so the text never runs under it.
            className="pr-9"
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
        {shown !== undefined && shown.total > shown.folders.length && (
          <p className="text-muted-foreground text-xs">
            Showing {shown.folders.length.toLocaleString("en-US")} of{" "}
            {shown.total.toLocaleString("en-US")}. Type a path to open it.
          </p>
        )}
      </div>

      {refusal !== null && (
        <p id={refusalId} className="text-muted-foreground text-xs break-words">
          {refusal}
        </p>
      )}

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
 * is still where the dialog left it: on the content (just opened), on the
 * body (the clicked row left with its folder), or in the list. Never out of
 * the path box.
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
    const untouched =
      active === null ||
      active === document.body ||
      active === contentRef.current ||
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
      {rows.map((entry) => (
        <li key={entry.path}>
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
            {listing.folders.length > 0
              ? `No folders match “${filter}”.`
              : "No subfolders."}
          </p>
        </li>
      )}
    </ul>
  );
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
