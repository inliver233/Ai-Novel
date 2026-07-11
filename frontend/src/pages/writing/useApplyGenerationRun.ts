import { toastApiError } from "../../lib/apiErrorPresentation";
import { toApiError } from "../../services/apiError";
import { useEffect } from "react";
import type { Dispatch, SetStateAction } from "react";

import type { ConfirmApi } from "../../components/ui/confirm";
import type { ToastApi } from "../../components/ui/toast";
import type { GenerationRun } from "../../components/writing/types";
import { apiJson } from "../../services/apiClient";
import { createChapterMarkerStreamParser } from "../../services/chapterMarkerStreamParser";
import type { Chapter } from "../../types";
import { WRITING_PAGE_COPY } from "./writingPageCopy";
import type { ChapterForm } from "./writingUtils";

export function useApplyGenerationRun(args: {
  applyRunId: string | null;
  activeChapter: Chapter | null;
  form: ChapterForm | null;
  dirty: boolean;
  confirm: ConfirmApi;
  toast: ToastApi;
  saveChapter: () => Promise<boolean>;
  searchParams: URLSearchParams;
  setSearchParams: (next: URLSearchParams, opts?: { replace?: boolean }) => void;
  setForm: Dispatch<SetStateAction<ChapterForm | null>>;
}) {
  const {
    applyRunId,
    activeChapter,
    form,
    dirty,
    confirm,
    toast,
    saveChapter,
    searchParams,
    setSearchParams,
    setForm,
  } = args;

  useEffect(() => {
    if (!applyRunId) return;
    if (!activeChapter || !form) return;

    let canceled = false;
    let shouldClearApplyRunId = true;
    void (async () => {
      try {
        if (dirty) {
          const choice = await confirm.choose(WRITING_PAGE_COPY.confirms.applyGenerationRun);
          if (choice === "cancel") return;
          if (choice === "confirm") {
            const ok = await saveChapter();
            if (!ok) return;
          }
        }

        const res = await apiJson<{ run: GenerationRun }>(`/api/generation_runs/${applyRunId}`);
        if (canceled) return;

        const run = res.data.run;
        if (run.chapter_id && run.chapter_id !== activeChapter.id) {
          // Batch apply can set applyRunId before the chapter switch finishes; keep the param and retry on chapter change.
          shouldClearApplyRunId = false;
          return;
        }
        const raw = typeof run.output_text === "string" ? run.output_text : "";
        if (!raw.trim()) {
          toast.toastError(WRITING_PAGE_COPY.applyRunEmpty, res.request_id);
          return;
        }

        const parser = createChapterMarkerStreamParser();
        let content = "";
        let summary = "";
        const out1 = parser.push(raw);
        content += out1.contentDelta;
        summary += out1.summaryDelta;
        const out2 = parser.finalize();
        content += out2.contentDelta;
        summary += out2.summaryDelta;

        const nextContent = content.trim() || raw.trim();
        const nextSummary = summary.trim();
        setForm((prev) =>
          prev ? { ...prev, content_md: nextContent, summary: nextSummary || prev.summary, status: "drafting" } : prev,
        );
        toast.toastSuccess(WRITING_PAGE_COPY.applyRunSuccess, res.request_id);
      } catch (e) {
        const err = toApiError(e);
        toastApiError(toast, err);
      }
      if (canceled) return;
      if (!shouldClearApplyRunId) return;
      const next = new URLSearchParams(searchParams);
      next.delete("applyRunId");
      setSearchParams(next, { replace: true });
    })();

    return () => {
      canceled = true;
    };
  }, [activeChapter, applyRunId, confirm, dirty, form, saveChapter, searchParams, setForm, setSearchParams, toast]);
}
