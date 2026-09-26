import {
  type SubmitEvent,
  type RefObject,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  ImportConflictError,
  ImportStartRejectedError,
  startErrorSentence,
} from "@/api/useImport";
import {
  ADD_SOURCE_FAILED,
  REMOVE_SOURCE_FAILED,
  type SourceSummary,
  useAddFolderSource,
  useRemoveFolderSource,
  useSources,
} from "@/api/useSources";
import { FolderBrowserDialog } from "@/components/folders/FolderBrowserDialog";
import { Close, Spinner } from "@/components/icons";
import { SettingsSection } from "@/components/system/SettingsSection";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const NAME_ID = "folder-source-name";
const FOLDER_ID = "folder-source-folder";
const ADD_ERROR_ID = "folder-source-add-error";

/** The row whose removal moves focus when it leaves the list: its id, and
 * where it stood, so the row that slides into its place is the next one. */
type PendingFocus = { id: string; index: number };

/**
 * Settings → Sources → Folders: folders the user imports from often, each a
 * name and a folder, nothing else. Adding checks the folder on the server;
 * removing touches no files.
 */
export function FolderSourcesPanel() {
  const sources = useSources();
  const remove = useRemoveFolderSource();
  const rows = sources.data?.sources;

  const nameRef = useRef<HTMLInputElement>(null);
  const removeButtons = useRef(new Map<string, HTMLButtonElement>());
  const pendingFocus = useRef<PendingFocus | null>(null);

  // Focus after a remove: the next row's remove button, else the previous
  // one, else the Add row's Name. Only while focus was lost with the row (it
  // sits on the body): a user who moved on keeps their place.
  useEffect(() => {
    const pending = pendingFocus.current;
    if (pending === null || rows === undefined) return;
    if (rows.some((row) => row.id === pending.id)) return;
    pendingFocus.current = null;
    const active = document.activeElement;
    if (active !== null && active !== document.body) return;
    const target = rows[pending.index] ?? rows[pending.index - 1];
    const button =
      target === undefined ? undefined : removeButtons.current.get(target.id);
    (button ?? nameRef.current)?.focus();
  }, [rows]);

  function onRemove(row: SourceSummary, index: number) {
    if (remove.isPending && remove.variables === row.id) return;
    pendingFocus.current = { id: row.id, index };
    remove.mutate(row.id, {
      onError: () => {
        if (pendingFocus.current?.id === row.id) pendingFocus.current = null;
      },
    });
  }

  return (
    <SettingsSection
      title="Folders"
      description="Folders you import from often. Each gets a button on Add from folder."
    >
      {sources.isPending && (
        <output className="text-muted-foreground block text-sm">
          Loading folders…
        </output>
      )}
      {sources.isError && (
        <p className="text-destructive text-sm" role="alert">
          Couldn’t load folders.
        </p>
      )}
      {rows?.length === 0 && (
        <p className="text-muted-foreground text-sm">No folders yet.</p>
      )}
      {rows !== undefined && rows.length > 0 && (
        <ul className="flex flex-col divide-y">
          {rows.map((row, index) => (
            <li key={row.id} className="flex flex-col gap-0.5 py-2">
              {/* The name line holds Missing and the remove button, so the
                  path below takes the row's whole width. `items-start` keeps
                  the button on the FIRST line of a wrapped name; `-my-1.5` is
                  half the 12px by which the size-8 button exceeds the 20px
                  line, so the two share a centre without the line growing
                  (ReviewPage's Dismiss button, which uses `-mt-1.5`). */}
              <div className="flex items-start gap-3">
                <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-0.5">
                  <span className="min-w-0 text-sm font-medium break-words">
                    {row.name}
                  </span>
                  {!row.exists && <Badge variant="outline">Missing</Badge>}
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Remove ${row.name} from Sources`}
                  // Not `disabled`: that would drop focus to the body mid-remove.
                  aria-disabled={remove.isPending && remove.variables === row.id}
                  className="-my-1.5 shrink-0 aria-disabled:opacity-50"
                  ref={(el) => {
                    if (el === null) removeButtons.current.delete(row.id);
                    else removeButtons.current.set(row.id, el);
                  }}
                  onClick={() => onRemove(row, index)}
                >
                  <Close aria-hidden="true" />
                </Button>
              </div>
              <span className="text-muted-foreground min-w-0 font-mono text-xs wrap-anywhere">
                {row.folder}
              </span>
            </li>
          ))}
        </ul>
      )}
      {/* Always ours: the thrown message can be the browser's own ("Failed
          to fetch"). */}
      {remove.isError && (
        <p className="text-destructive text-sm" role="alert">
          {REMOVE_SOURCE_FAILED}
        </p>
      )}
      <AddFolderSource nameRef={nameRef} />
    </SettingsSection>
  );
}

/** The add row: Name, Folder with the folder browser, then Add. Wraps on a
 * phone. The server's refusal shows under it, in the server's words. */
function AddFolderSource({
  nameRef,
}: Readonly<{ nameRef: RefObject<HTMLInputElement | null> }>) {
  const add = useAddFolderSource();
  const [name, setName] = useState("");
  const [folder, setFolder] = useState("");
  const folderRef = useRef<HTMLInputElement>(null);

  const blank = name.trim() === "" || folder.trim() === "";
  // Every refusal the server words (422, 409, 503) shows in its words; the
  // rest take the generic sentence. Only a 422 or 409 is about what is in the
  // Folder field: a 503 is about the library's layout, which no edit to the
  // field can fix, and a generic failure says nothing is wrong with it.
  const refused =
    add.error instanceof ImportStartRejectedError ||
    add.error instanceof ImportConflictError;
  const failure = startErrorSentence(add.error, add.isError, ADD_SOURCE_FAILED);

  // Clear only a SHOWN error: `reset()` also detaches an add in flight, whose
  // answer would then never reach this row (ImportPage's `onPathChange`).
  function onNameChange(next: string) {
    setName(next);
    if (add.isError) add.reset();
  }

  // Also the browser's setter, so a browsed folder clears a stale refusal
  // exactly as typing does.
  function onFolderChange(next: string) {
    setFolder(next);
    if (add.isError) add.reset();
  }

  function onSubmit(e: SubmitEvent<HTMLFormElement>) {
    e.preventDefault();
    if (blank || add.isPending) return;
    add.mutate(
      { name: name.trim(), folder: folder.trim() },
      {
        onSuccess: () => {
          setName("");
          setFolder("");
          // Add goes disabled with the fields cleared; the next add starts
          // at Name.
          nameRef.current?.focus();
        },
      },
    );
  }

  return (
    <form className="flex flex-col gap-2 border-t pt-4" onSubmit={onSubmit}>
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex w-full flex-col gap-1 sm:w-40">
          <label htmlFor={NAME_ID} className="text-sm font-medium">
            Name
          </label>
          <Input
            id={NAME_ID}
            ref={nameRef}
            value={name}
            onChange={(e) => onNameChange(e.target.value)}
            placeholder="yubal"
            maxLength={40}
            autoComplete="off"
          />
        </div>
        <div className="flex min-w-0 flex-1 basis-64 flex-col gap-1">
          <label htmlFor={FOLDER_ID} className="text-sm font-medium">
            Folder
          </label>
          <div className="flex gap-2">
            <Input
              id={FOLDER_ID}
              ref={folderRef}
              value={folder}
              onChange={(e) => onFolderChange(e.target.value)}
              placeholder="/media/downloads/yubal"
              // The contract's bound (Linux PATH_MAX), as on Add from folder.
              maxLength={4096}
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              className="font-mono"
              aria-invalid={refused}
              aria-describedby={refused ? ADD_ERROR_ID : undefined}
            />
            <FolderBrowserDialog
              value={folder}
              onUse={onFolderChange}
              fieldRef={folderRef}
            />
          </div>
        </div>
        <Button
          type="submit"
          // Blank fields cannot add at all, so `disabled`. Pending is the
          // button's own press: `aria-disabled`, so focus stays on it.
          disabled={blank}
          aria-disabled={add.isPending}
          className="aria-disabled:opacity-50"
        >
          {add.isPending ? (
            <>
              <Spinner className="animate-spin" aria-hidden="true" />
              Adding…
            </>
          ) : (
            "Add"
          )}
        </Button>
      </div>
      {failure !== null && (
        <p
          id={ADD_ERROR_ID}
          className="text-destructive text-sm break-words"
          role="alert"
        >
          {failure}
        </p>
      )}
    </form>
  );
}
