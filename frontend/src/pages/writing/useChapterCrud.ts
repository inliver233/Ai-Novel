import { toastApiError } from "../../lib/apiErrorPresentation";
import { toApiError } from "../../services/apiError";
import { useCallback, useState } from "react";

import type { ConfirmApi } from "../../components/ui/confirm";
import type { ToastApi } from "../../components/ui/toast";
import type { CreateChapterForm } from "../../components/writing/types";
import { chapterStore } from "../../services/chapterStore";
import { markWizardProjectChanged } from "../../services/wizard";
import type { Chapter, ChapterListItem } from "../../types";
import { WRITING_PAGE_COPY } from "./writingPageCopy";
import { nextChapterNumber } from "./writingUtils";

export function useChapterCrud(args: {
  projectId: string | undefined;
  chapters: ChapterListItem[];
  activeChapter: Chapter | null;
  setActiveId: (next: string | null) => void;
  requestSelectChapter: (chapterId: string) => Promise<void>;
  toast: ToastApi;
  confirm: ConfirmApi;
  bumpWizardLocal: () => void;
  refreshWizard: () => Promise<void>;
}) {
  const {
    projectId,
    chapters,
    activeChapter,
    setActiveId,
    requestSelectChapter,
    toast,
    confirm,
    bumpWizardLocal,
    refreshWizard,
  } = args;

  const [createOpen, setCreateOpen] = useState(false);
  const [createSaving, setCreateSaving] = useState(false);
  const [createForm, setCreateForm] = useState<CreateChapterForm>({ number: 1, title: "", plan: "" });

  const openCreate = useCallback(() => {
    setCreateForm({ number: nextChapterNumber(chapters), title: "", plan: "" });
    setCreateOpen(true);
  }, [chapters]);

  const createChapter = useCallback(async () => {
    if (!projectId) return;
    if (createSaving) return;
    if (!createForm.number || createForm.number < 1) {
      toast.toastError(WRITING_PAGE_COPY.chapterNumberInvalid);
      return;
    }
    setCreateSaving(true);
    try {
      const chapter = await chapterStore.createProjectChapter(projectId, {
        number: createForm.number,
        title: createForm.title.trim() || null,
        plan: createForm.plan.trim() || null,
        status: "planned",
      });
      markWizardProjectChanged(projectId);
      bumpWizardLocal();
      void refreshWizard();
      toast.toastSuccess(WRITING_PAGE_COPY.createSuccess);
      setCreateOpen(false);
      await requestSelectChapter(chapter.id);
    } catch (e) {
      const err = toApiError(e);
      toastApiError(toast, err);
    } finally {
      setCreateSaving(false);
    }
  }, [bumpWizardLocal, createForm, createSaving, projectId, refreshWizard, requestSelectChapter, toast]);

  const deleteChapter = useCallback(async () => {
    if (!activeChapter) return;
    const ok = await confirm.confirm({ ...WRITING_PAGE_COPY.confirms.deleteChapter, danger: true });
    if (!ok) return;

    try {
      await chapterStore.deleteProjectChapter(activeChapter.id, { projectId: activeChapter.project_id });
      markWizardProjectChanged(activeChapter.project_id);
      bumpWizardLocal();
      void refreshWizard();
      toast.toastSuccess(WRITING_PAGE_COPY.deleteSuccess);
      const idx = chapters.findIndex((c) => c.id === activeChapter.id);
      const next = chapters[idx - 1]?.id ?? chapters[idx + 1]?.id ?? null;
      setActiveId(next);
    } catch (e) {
      const err = toApiError(e);
      toastApiError(toast, err);
    }
  }, [activeChapter, bumpWizardLocal, chapters, confirm, refreshWizard, setActiveId, toast]);

  return {
    createOpen,
    setCreateOpen,
    createSaving,
    createForm,
    setCreateForm,
    openCreate,
    createChapter,
    deleteChapter,
  };
}
