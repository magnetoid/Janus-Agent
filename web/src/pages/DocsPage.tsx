import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent,
  type ReactNode,
} from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { BookOpen, ChevronRight, ExternalLink, FileQuestion, List, Search, TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/i18n";
import { api } from "@/lib/api";
import type { DocsNavNode, DocsPageResponse, DocsTocEntry, DocsTreeResponse } from "@/lib/api";
import { cn } from "@/lib/utils";
import { PluginSlot } from "@/plugins";

/** Where the docs live outside this dashboard. Offered only when the
 *  install has none of its own to show. */
export const JANUS_DOCS_URL = "https://github.com/magnetoid/janus";

type PageState =
  | { id: string; page: DocsPageResponse }
  | { id: string; problem: "missing" | "failed" };

interface FlatDoc {
  id: string;
  label: string;
  /** The categories the page sits under, outermost first. */
  trail: string[];
}

function flatten(nodes: DocsNavNode[], trail: string[] = []): FlatDoc[] {
  return nodes.flatMap((node) =>
    node.type === "doc"
      ? [{ id: node.id, label: node.label, trail }]
      : flatten(node.items, [...trail, node.label]),
  );
}

function holds(node: DocsNavNode, id: string | null): boolean {
  if (id === null) return false;
  return node.type === "doc" ? node.id === id : node.items.some((child) => holds(child, id));
}

/**
 * The documentation, read from this install.
 *
 * The server renders each Markdown page of the checkout to HTML (raw HTML
 * in a page is escaped there, so what arrives is safe to place in the
 * document) and sends the navigation from `website/sidebars.ts`. Nothing
 * is loaded from the network, so the page works offline and always matches
 * the version that is running.
 *
 * The page being read is in the address (`?page=…#section`), so it can be
 * reloaded, bookmarked and opened in a new tab.
 */
export default function DocsPage() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const location = useLocation();
  const [params] = useSearchParams();

  const [tree, setTree] = useState<DocsTreeResponse | null>(null);
  const [treeFailed, setTreeFailed] = useState(false);
  const [state, setState] = useState<PageState | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [query, setQuery] = useState("");
  const [toggled, setToggled] = useState<Record<string, boolean>>({});
  const [browsing, setBrowsing] = useState(false);

  const scrollerRef = useRef<HTMLDivElement>(null);
  // Held in state, not a ref: the page text can arrive before the page
  // list does, and the article is only put on screen once both are here.
  // The effects below have to run when the element appears, not only when
  // the text changes.
  const [article, setArticle] = useState<HTMLElement | null>(null);

  useEffect(() => {
    let stale = false;
    api
      .getDocsTree()
      .then((response) => {
        if (!stale) setTree(response);
      })
      .catch(() => {
        if (!stale) setTreeFailed(true);
      });
    return () => {
      stale = true;
    };
  }, [attempt]);

  const pageId = params.get("page") || tree?.home || null;

  useEffect(() => {
    if (!pageId) return;
    let stale = false;
    api
      .getDocsPage(pageId)
      .then((page) => {
        if (!stale) setState({ id: pageId, page });
      })
      .catch((error: unknown) => {
        if (stale) return;
        const missing = error instanceof Error && error.message.startsWith("404:");
        setState({ id: pageId, problem: missing ? "missing" : "failed" });
      });
    return () => {
      stale = true;
    };
  }, [pageId, attempt]);

  // What is on screen belongs to the page in the address, or it is still
  // loading. A slow answer for a page that was left never shows.
  const current = state && state.id === pageId ? state : null;
  const page = current && "page" in current ? current.page : null;
  const html = page?.html ?? null;
  // One object per page text. React writes `innerHTML` again whenever it
  // is handed a new object here, even with the same text in it, and that
  // would throw away the pictures filled in below and the reader's place
  // on every unrelated re-render.
  const markup = useMemo(() => (html === null ? undefined : { __html: html }), [html]);

  // Images: `/api/` needs the session, which an <img> cannot send, so each
  // one is fetched here and shown from memory.
  useEffect(() => {
    if (!article || html === null) return;
    let stale = false;
    const urls: string[] = [];
    article.querySelectorAll<HTMLImageElement>("img[data-doc-asset]").forEach((img) => {
      const path = img.dataset.docAsset;
      if (!path) return;
      api
        .getDocsAsset(path)
        .then((blob) => {
          if (stale) return;
          const url = URL.createObjectURL(blob);
          urls.push(url);
          img.src = url;
        })
        .catch(() => {
          // The alt text stays in place of the picture.
        });
    });
    return () => {
      stale = true;
      urls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [article, html]);

  // A new page starts at its top; a `#section` in the address is shown.
  useEffect(() => {
    if (!article || html === null) return;
    const id = decodeURIComponent(location.hash.slice(1));
    const target = id ? article.querySelector(`#${CSS.escape(id)}`) : null;
    if (target) {
      target.scrollIntoView({ block: "start" });
    } else {
      scrollerRef.current?.scrollTo({ top: 0 });
    }
  }, [article, html, location.hash]);

  const open = useCallback(
    (id: string, anchor = "") => {
      setBrowsing(false);
      navigate({ search: `?page=${id}`, hash: anchor ? `#${anchor}` : "" });
    },
    [navigate],
  );

  // Links inside the rendered page: another page opens here, a section
  // scrolls, anything else is an ordinary link that opens in a new tab.
  const onArticleClick = (event: MouseEvent<HTMLElement>) => {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = (event.target as HTMLElement).closest("a");
    if (!link) return;
    const docId = link.getAttribute("data-doc-id");
    if (docId) {
      event.preventDefault();
      open(docId, link.getAttribute("data-doc-anchor") ?? "");
      return;
    }
    const href = link.getAttribute("href") ?? "";
    if (href.startsWith("#")) {
      event.preventDefault();
      navigate({ search: location.search, hash: href }, { replace: true });
    }
  };

  const docs = useMemo(() => (tree ? flatten(tree.tree) : []), [tree]);
  const needle = query.trim().toLowerCase();
  const matches = useMemo(
    () => (needle ? docs.filter((doc) => doc.label.toLowerCase().includes(needle)).slice(0, 60) : []),
    [docs, needle],
  );

  const outside = (
    <a
      href={JANUS_DOCS_URL}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1.5 rounded-md text-sm font-medium text-primary underline-offset-4 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring"
    >
      <ExternalLink className="size-3.5" />
      {t.app.openDocumentation}
    </a>
  );

  let body: ReactNode;
  if (treeFailed) {
    body = (
      <EmptyState
        className="m-auto"
        icon={TriangleAlert}
        title={t.docs.failedTitle}
        action={
          <Button
            outlined
            size="sm"
            onClick={() => {
              setTreeFailed(false);
              setAttempt((n) => n + 1);
            }}
          >
            {t.docs.retry}
          </Button>
        }
      />
    );
  } else if (tree === null) {
    body = (
      <div className="flex min-h-0 flex-1 gap-8">
        <div className="hidden w-64 shrink-0 flex-col gap-2 lg:flex">
          {Array.from({ length: 9 }, (_, i) => (
            <Skeleton key={i} className="h-6" style={{ width: `${55 + ((i * 17) % 40)}%` }} />
          ))}
        </div>
        <ArticleSkeleton />
      </div>
    );
  } else if (!tree.available) {
    body = (
      <EmptyState
        className="m-auto"
        icon={BookOpen}
        title={t.docs.unavailableTitle}
        description={t.docs.unavailableBody}
        action={outside}
      />
    );
  } else {
    const pageList = (
      <DocsNav
        activeId={pageId}
        filter={t.docs.filter}
        matches={matches}
        noMatches={t.docs.noMatches.replace("{query}", query.trim())}
        nodes={tree.tree}
        onQuery={setQuery}
        onToggle={(key, isOpen) => setToggled((prev) => ({ ...prev, [key]: !isOpen }))}
        query={query}
        searching={needle !== ""}
        toggled={toggled}
        onPick={() => setBrowsing(false)}
      />
    );

    body = (
      <div className="flex min-h-0 flex-1 gap-6 xl:gap-10">
        <nav
          aria-label={t.app.nav.documentation}
          className="hidden min-h-0 w-64 shrink-0 flex-col lg:flex"
        >
          {pageList}
        </nav>

        <div ref={scrollerRef} className="min-h-0 min-w-0 flex-1 lg:overflow-y-auto lg:pr-2">
          <Button outlined size="sm" className="mb-4 lg:hidden" onClick={() => setBrowsing(true)}>
            <List />
            {t.docs.browse}
          </Button>

          {current === null ? (
            <ArticleSkeleton />
          ) : page ? (
            <article
              ref={setArticle}
              className="docs-prose"
              onClick={onArticleClick}
              // Rendered by the server from this install's own docs, with
              // raw HTML switched off (janus_cli/dashboard_docs.py).
              dangerouslySetInnerHTML={markup}
            />
          ) : "problem" in current && current.problem === "missing" ? (
            <EmptyState
              icon={FileQuestion}
              title={t.docs.missingTitle}
              description={t.docs.missingBody}
              action={
                tree.home && tree.home !== pageId ? (
                  <Button outlined size="sm" onClick={() => open(tree.home as string)}>
                    {t.docs.openFirst}
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <EmptyState
              icon={TriangleAlert}
              title={t.docs.failedTitle}
              action={
                <Button outlined size="sm" onClick={() => setAttempt((n) => n + 1)}>
                  {t.docs.retry}
                </Button>
              }
            />
          )}
        </div>

        {page && page.toc.length > 1 && (
          <aside className="hidden min-h-0 w-52 shrink-0 overflow-y-auto xl:block">
            <OnThisPage
              active={decodeURIComponent(location.hash.slice(1))}
              entries={page.toc}
              heading={t.docs.onThisPage}
              search={location.search}
            />
          </aside>
        )}

        <Sheet open={browsing} onOpenChange={setBrowsing}>
          <SheetContent className="p-3">
            <SheetTitle className="sr-only">{t.docs.browse}</SheetTitle>
            {pageList}
          </SheetContent>
        </Sheet>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 w-full min-w-0 flex-1 flex-col pt-1">
      <PluginSlot name="docs:top" />
      {body}
      <PluginSlot name="docs:bottom" />
    </div>
  );
}

function ArticleSkeleton() {
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-3" aria-busy="true">
      <Skeleton className="h-9 w-2/5" />
      <Skeleton className="mt-2 h-4 w-full" />
      <Skeleton className="h-4 w-11/12" />
      <Skeleton className="h-4 w-4/5" />
      <Skeleton className="mt-4 h-28 w-full" />
      <Skeleton className="mt-2 h-4 w-3/4" />
      <Skeleton className="h-4 w-2/3" />
    </div>
  );
}

const ROW =
  "flex w-full min-w-0 items-center gap-1.5 rounded-md px-2 py-1.5 text-left text-sm outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring";

interface DocsNavProps {
  activeId: string | null;
  filter: string;
  matches: FlatDoc[];
  noMatches: string;
  nodes: DocsNavNode[];
  onPick: () => void;
  onQuery: (query: string) => void;
  onToggle: (key: string, isOpen: boolean) => void;
  query: string;
  searching: boolean;
  toggled: Record<string, boolean>;
}

/** The page list: a filter box, then either the sidebar's categories or,
 *  while filtering, the pages whose name matches. */
function DocsNav({
  activeId,
  filter,
  matches,
  noMatches,
  nodes,
  onPick,
  onQuery,
  onToggle,
  query,
  searching,
  toggled,
}: DocsNavProps) {
  return (
    <>
      <div className="relative mb-2 shrink-0">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-text-tertiary" />
        <Input
          type="search"
          value={query}
          onChange={(event) => onQuery(event.target.value)}
          placeholder={filter}
          aria-label={filter}
          className="pl-8"
        />
      </div>

      <div className="-mx-1 min-h-0 flex-1 overflow-y-auto px-1 pb-4">
        {!searching ? (
          <NavLevel
            activeId={activeId}
            nodes={nodes}
            onPick={onPick}
            onToggle={onToggle}
            path=""
            toggled={toggled}
          />
        ) : matches.length === 0 ? (
          <p className="px-2 py-3 text-sm text-text-tertiary">{noMatches}</p>
        ) : (
          <ul className="flex flex-col gap-0.5">
            {matches.map((doc) => (
              <li key={doc.id}>
                <PageLink active={doc.id === activeId} id={doc.id} onPick={onPick}>
                  <span className="flex min-w-0 flex-col">
                    <span className="truncate">{doc.label}</span>
                    {doc.trail.length > 0 && (
                      <span className="truncate text-xs text-text-tertiary">
                        {doc.trail.join(" / ")}
                      </span>
                    )}
                  </span>
                </PageLink>
              </li>
            ))}
          </ul>
        )}
      </div>
    </>
  );
}

function NavLevel({
  activeId,
  nodes,
  onPick,
  onToggle,
  path,
  toggled,
}: {
  activeId: string | null;
  nodes: DocsNavNode[];
  onPick: () => void;
  onToggle: (key: string, isOpen: boolean) => void;
  path: string;
  toggled: Record<string, boolean>;
}) {
  return (
    <ul className="flex flex-col gap-0.5">
      {nodes.map((node) => {
        if (node.type === "doc") {
          return (
            <li key={node.id}>
              <PageLink active={node.id === activeId} id={node.id} onPick={onPick}>
                <span className="truncate">{node.label}</span>
              </PageLink>
            </li>
          );
        }
        const key = `${path}/${node.label}`;
        // A category opens by itself for the page being read; after that
        // it is the reader's to open and close.
        const isOpen = toggled[key] ?? holds(node, activeId);
        return (
          <li key={key}>
            <button
              type="button"
              aria-expanded={isOpen}
              onClick={() => onToggle(key, isOpen)}
              className={cn(ROW, "cursor-pointer font-medium text-text-secondary hover:bg-accent hover:text-foreground")}
            >
              <ChevronRight
                className={cn("size-3.5 shrink-0 text-text-tertiary transition-transform", isOpen && "rotate-90")}
              />
              <span className="truncate">{node.label}</span>
            </button>
            {isOpen && (
              <div className="ml-3.5 border-l border-border pl-1.5">
                <NavLevel
                  activeId={activeId}
                  nodes={node.items}
                  onPick={onPick}
                  onToggle={onToggle}
                  path={key}
                  toggled={toggled}
                />
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function PageLink({
  active,
  children,
  id,
  onPick,
}: {
  active: boolean;
  children: ReactNode;
  id: string;
  onPick: () => void;
}) {
  return (
    <Link
      to={{ search: `?page=${id}` }}
      aria-current={active ? "page" : undefined}
      onClick={onPick}
      className={cn(
        ROW,
        active
          ? "bg-accent font-medium text-foreground"
          : "text-text-secondary hover:bg-accent hover:text-foreground",
      )}
    >
      {children}
    </Link>
  );
}

function OnThisPage({
  active,
  entries,
  heading,
  search,
}: {
  active: string;
  entries: DocsTocEntry[];
  heading: string;
  search: string;
}) {
  return (
    <nav aria-label={heading}>
      <p className="mb-2 px-2 text-xs font-medium text-text-tertiary">{heading}</p>
      <ul className="flex flex-col border-l border-border">
        {entries.map((entry) => (
          <li key={entry.id}>
            <Link
              to={{ search, hash: `#${entry.id}` }}
              replace
              className={cn(
                "-ml-px block border-l py-1 pr-2 text-sm leading-snug outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring",
                entry.level === 3 ? "pl-6" : "pl-3",
                entry.id === active
                  ? "border-primary text-foreground"
                  : "border-transparent text-text-secondary hover:border-border-strong hover:text-foreground",
              )}
            >
              {entry.text}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
