import { toApiError } from "../services/apiError";
import { toastApiError } from "../lib/apiErrorPresentation";
import { useCallback, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { DebugPageShell } from "../components/atelier/DebugPageShell";
import { useToast } from "../components/ui/toast";
import { UI_COPY } from "../lib/uiCopy";
import { apiJson } from "../services/apiClient";

type SearchItem = {
  source_type: string;
  source_id: string;
  title: string;
  snippet: string;
  jump_url: string | null;
};

type SearchQueryResponse = {
  items: SearchItem[];
  next_offset: number | null;
  mode?: string;
  fts_enabled?: boolean;
};

const ACTIVE_SOURCE_TYPES = new Set(["chapter", "outline", "character", "story_memory", "source_document", "entry"]);

const SOURCE_OPTIONS: Array<{ key: string; label: string }> = [
  { key: "chapter", label: UI_COPY.search.sourceLabels.chapter },
  { key: "outline", label: UI_COPY.search.sourceLabels.outline },
  { key: "character", label: UI_COPY.search.sourceLabels.character },
  { key: "story_memory", label: UI_COPY.search.sourceLabels.storyMemory },
  { key: "source_document", label: UI_COPY.search.sourceLabels.sourceDocument },
  { key: "entry", label: UI_COPY.search.sourceLabels.entry },
];

function filterActiveItems(items: SearchItem[]): SearchItem[] {
  return items.filter((item) => ACTIVE_SOURCE_TYPES.has(item.source_type));
}

function dedupeItems(items: SearchItem[]): SearchItem[] {
  const out: SearchItem[] = [];
  const seen = new Set<string>();
  for (const it of items) {
    const key = `${it.source_type}:${it.source_id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(it);
  }
  return out;
}

export function SearchPage() {
  const { projectId } = useParams();
  const toast = useToast();
  const navigate = useNavigate();

  const [query, setQuery] = useState("");
  const [sourcesState, setSourcesState] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(SOURCE_OPTIONS.map((s) => [s.key, true])),
  );

  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<SearchItem[]>([]);
  const [nextOffset, setNextOffset] = useState<number | null>(null);

  const selectedSources = useMemo(() => {
    const selected = SOURCE_OPTIONS.filter((s) => sourcesState[s.key]).map((s) => s.key);
    return selected.length === SOURCE_OPTIONS.length ? null : selected.length ? selected : null;
  }, [sourcesState]);

  const runQuery = useCallback(
    async (opts?: { append?: boolean }) => {
      if (!projectId) return;
      const append = Boolean(opts?.append);
      const q = query.trim();
      if (!q) return;
      if (loading) return;
      setLoading(true);
      try {
        const offset = append ? (nextOffset ?? 0) : 0;
        const res = await apiJson<SearchQueryResponse>(`/api/projects/${projectId}/search/query`, {
          method: "POST",
          body: JSON.stringify({
            q,
            sources: selectedSources ?? [],
            limit: 20,
            offset,
          }),
        });

        const data = res.data;
        const nextItems = filterActiveItems(Array.isArray(data.items) ? data.items : []);
        setItems((prev) => (append ? dedupeItems([...prev, ...nextItems]) : dedupeItems(nextItems)));
        setNextOffset(typeof data.next_offset === "number" ? data.next_offset : null);
      } catch (e) {
        const err = toApiError(e);
        toastApiError(toast, err);
      } finally {
        setLoading(false);
      }
    },
    [loading, nextOffset, projectId, query, selectedSources, toast],
  );

  const clear = useCallback(() => {
    setQuery("");
    setItems([]);
    setNextOffset(null);
  }, []);

  const toggleSource = useCallback((key: string) => {
    setSourcesState((prev) => ({ ...prev, [key]: !prev[key] }));
  }, []);

  const sourceLabel = useCallback((sourceType: string) => {
    switch (sourceType) {
      case "chapter":
        return UI_COPY.search.sourceLabels.chapter;
      case "outline":
        return UI_COPY.search.sourceLabels.outline;
      case "character":
        return UI_COPY.search.sourceLabels.character;
      case "story_memory":
        return UI_COPY.search.sourceLabels.storyMemory;
      case "source_document":
        return UI_COPY.search.sourceLabels.sourceDocument;
      case "entry":
        return UI_COPY.search.sourceLabels.entry;
      default:
        return sourceType;
    }
  }, []);

  const canJump = useCallback((it: SearchItem) => {
    if (it.jump_url && it.jump_url.startsWith("/")) return true;
    return (
      it.source_type === "chapter" ||
      it.source_type === "outline" ||
      it.source_type === "character" ||
      it.source_type === "source_document" ||
      it.source_type === "entry"
    );
  }, []);

  const jump = useCallback(
    (it: SearchItem) => {
      if (!projectId) return;
      {
        const raw = String(it.jump_url || "").trim();
        if (raw && raw.startsWith("/")) {
          navigate(raw);
          return;
        }
      }
      if (it.source_type === "chapter") {
        navigate(`/projects/${projectId}/writing?chapterId=${encodeURIComponent(it.source_id)}`);
        return;
      }
      if (it.source_type === "outline") {
        navigate(`/projects/${projectId}/outline`);
        return;
      }
      if (it.source_type === "character") {
        navigate(`/projects/${projectId}/characters`);
        return;
      }
      if (it.source_type === "entry") {
        navigate(`/projects/${projectId}/entries`);
        return;
      }
      toast.toastWarning(`该来源暂不支持跳转：${it.source_type}`);
    },
    [navigate, projectId, toast],
  );

  return (
    <DebugPageShell
      title={UI_COPY.search.title}
      description={UI_COPY.search.subtitle}
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            className="btn btn-secondary"
            aria-label="search_clear"
            disabled={loading && Boolean(query.trim())}
            onClick={clear}
          >
            {UI_COPY.search.clear}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            aria-label="search_submit"
            disabled={!projectId || !query.trim() || loading}
            onClick={() => void runQuery({ append: false })}
          >
            {loading ? UI_COPY.common.loading : UI_COPY.search.search}
          </button>
        </div>
      }
    >
      <div className="grid gap-3">
        <div className="grid gap-2">
          <input
            className="input w-full"
            id="search_query"
            name="search_query"
            aria-label="search_query"
            placeholder={UI_COPY.search.queryPlaceholder}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void runQuery({ append: false });
            }}
          />

          <div className="flex flex-wrap items-center gap-3">
            <div className="text-xs text-subtext">{UI_COPY.search.sourcesTitle}</div>
            {SOURCE_OPTIONS.map((s) => (
              <label key={s.key} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  aria-label={`search_source_${s.key}`}
                  name={`search_source_${s.key}`}
                  className="checkbox"
                  checked={Boolean(sourcesState[s.key])}
                  onChange={() => toggleSource(s.key)}
                />
                <span>{s.label}</span>
              </label>
            ))}
          </div>
        </div>

        <div className="grid gap-2" aria-label="search_results">
          {!items.length ? (
            <div className="text-sm text-subtext">{UI_COPY.search.emptyHint}</div>
          ) : (
            items.map((it) => (
              <div key={`${it.source_type}:${it.source_id}`} className="panel p-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-ink">{it.title || it.source_id}</div>
                    <div className="mt-0.5">
                      <span className="inline-flex items-center rounded-atelier border border-border/60 bg-canvas px-2 py-0.5 text-[11px] text-subtext">
                        {sourceLabel(it.source_type)}
                      </span>
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    {canJump(it) ? (
                      <button
                        type="button"
                        className="btn btn-primary"
                        aria-label="search_jump"
                        disabled={false}
                        onClick={() => jump(it)}
                      >
                        {UI_COPY.search.jump}
                      </button>
                    ) : (
                      <span title={UI_COPY.search.jumpDisabledHint}>
                        <button type="button" className="btn btn-primary" aria-label="search_jump" disabled>
                          {UI_COPY.search.jump}
                        </button>
                      </span>
                    )}
                  </div>
                </div>
                {it.snippet ? (
                  <div className="mt-2 whitespace-pre-wrap break-words rounded-atelier border border-border bg-canvas px-3 py-2 text-xs text-ink">
                    {it.snippet}
                  </div>
                ) : null}
              </div>
            ))
          )}
        </div>

        {nextOffset !== null ? (
          <div className="flex justify-center">
            <button
              type="button"
              className="btn btn-secondary"
              aria-label="search_load_more"
              disabled={loading}
              onClick={() => void runQuery({ append: true })}
            >
              {UI_COPY.search.loadMore}
            </button>
          </div>
        ) : null}
      </div>
    </DebugPageShell>
  );
}
