import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

type ReorganizeNoticeValue = {
  notice: string | null;
  showNotice: (message: string) => void;
};

const ReorganizeNoticeContext = createContext<ReorganizeNoticeValue>({
  notice: null,
  showNotice: () => {},
});

/**
 * Holds a transient top-of-page notice (e.g. "nothing to reorganize") that a
 * ReorganizeControl raises and the ReorganizeBanner renders — so a preview
 * result shows in the banner row, like a running job, rather than inline next to
 * the button. Auto-clears after a few seconds.
 */
export function ReorganizeNoticeProvider({
  children,
}: {
  children: ReactNode;
}) {
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 6000);
    return () => clearTimeout(t);
  }, [notice]);

  return (
    <ReorganizeNoticeContext.Provider value={{ notice, showNotice: setNotice }}>
      {children}
    </ReorganizeNoticeContext.Provider>
  );
}

export function useReorganizeNotice(): ReorganizeNoticeValue {
  return useContext(ReorganizeNoticeContext);
}
