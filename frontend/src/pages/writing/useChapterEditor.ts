import { toastApiError } from "../../lib/apiErrorPresentation";
import { toApiError } from "../../services/apiError";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ConfirmApi } from "../../components/ui/confirm";
import type { ToastApi } from "../../components/ui/toast";
import { useAutoSave } from "../../hooks/useAutoSave";
import { useChapterMetaList } from "../../hooks/useChapterMetaList";
import { useSaveHotkey } from "../../hooks/useSaveHotkey";
import { createRequestSeqGuard } from "../../lib/requestSeqGuard";
import type { ApiError } from "../../services/apiClient";
import { chapterStore } from "../../services/chapterStore";
import { markWizardProjectChanged } from "../../services/wizard";
import type { Chapter, ChapterListItem } from "../../types";
import { WRITING_PAGE_COPY } from "./writingPageCopy";
import { chapterToForm } from "./writingUtils";
import type { ChapterForm } from "./writingUtils";

export function useChapterEditor(args: {
  projectId: string | undefined;
  requestedChapterId: string | null;
  searchParams: URLSearchParams;
  setSearchParams: (next: URLSearchParams, opts?: { replace?: boolean }) => void;
  toast: ToastApi;
  confirm: ConfirmApi;
  refreshWizard: () => Promise<void>;
  bumpWizardLocal: () => void;
}) {
  const {
    projectId,
    requestedChapterId,
    searchParams,
    setSearchParams,
    toast,
    confirm,
    refreshWizard,
    bumpWizardLocal,
  } = args;

  const [activeId, setActiveId] = useState<string | null>(null);
  const [activeChapter, setActiveChapter] = useState<Chapter | null>(null);
  const [baseline, setBaseline] = useState<ChapterForm | null>(null);
  const [form, setForm] = useState<ChapterForm | null>(null);
  const [loadingChapter, setLoadingChapter] = useState(false);
  const [chapterLoadError, setChapterLoadError] = useState<ApiError | null>(null);
  const [saving, setSaving] = useState(false);
  const requestedChapterHandledRef = useRef(false);
  const chapterLoadGuardRef = useRef(createRequestSeqGuard());
  const saveGuardRef = useRef(createRequestSeqGuard());
  const refreshWizardDebounceRef = useRef<number | null>(null);
  const activeChapterRef = useRef<Chapter | null>(null);
  const formRef = useRef<ChapterForm | null>(null);
  const savingRef = useRef(false);
  const saveQueuedRef = useRef(false);
  const queuedSnapshotRef = useRef<ChapterForm | null>(null);
  const queuedSilentRef = useRef(true);
  const queuedPromiseRef = useRef<Promise<boolean> | null>(null);
  const queuedPromiseResolveRef = useRef<((ok: boolean) => void) | null>(null);
  const queuedToastShownRef = useRef(false);
  const chaptersQuery = useChapterMetaList(projectId);
  const chapters = chaptersQuery.chapters as ChapterListItem[];
  const refreshChapters = chaptersQuery.refresh;
  const loading = !chaptersQuery.hasData && chaptersQuery.loading;

  useEffect(() => {
    const loadGuard = chapterLoadGuardRef.current;
    const saveGuard = saveGuardRef.current;
    return () => {
      loadGuard.invalidate();
      saveGuard.invalidate();
      if (refreshWizardDebounceRef.current !== null) {
        window.clearTimeout(refreshWizardDebounceRef.current);
        refreshWizardDebounceRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    activeChapterRef.current = activeChapter;
    formRef.current = form;
  }, [activeChapter, form]);

  const dirty = useMemo(() => {
    if (!baseline || !form) return false;
    return (
      form.title !== baseline.title ||
      form.plan !== baseline.plan ||
      form.content_md !== baseline.content_md ||
      form.summary !== baseline.summary ||
      form.status !== baseline.status
    );
  }, [baseline, form]);

  useEffect(() => {
    setActiveId((prev) => {
      if (prev && chapters.some((chapter) => chapter.id === prev)) return prev;
      return chapters[0]?.id ?? null;
    });
  }, [chapters]);

  useEffect(() => {
    if (requestedChapterHandledRef.current) return;
    if (!requestedChapterId) return;
    if (!chapters.some((c) => c.id === requestedChapterId)) return;
    requestedChapterHandledRef.current = true;
    setActiveId(requestedChapterId);
    const next = new URLSearchParams(searchParams);
    next.delete("chapterId");
    setSearchParams(next, { replace: true });
  }, [chapters, requestedChapterId, searchParams, setSearchParams]);

  const loadChapter = useCallback(async (chapterId: string, force = false) => {
    const seq = chapterLoadGuardRef.current.next();
    setActiveChapter(null);
    setBaseline(null);
    setForm(null);
    setChapterLoadError(null);
    setLoadingChapter(true);
    try {
      const chapter = await chapterStore.loadChapterDetail(chapterId, { force });
      if (!chapterLoadGuardRef.current.isLatest(seq)) return;
      setActiveChapter(chapter);
      const next = chapterToForm(chapter);
      setBaseline(next);
      setForm(next);
    } catch (e) {
      if (!chapterLoadGuardRef.current.isLatest(seq)) return;
      setChapterLoadError(toApiError(e));
      setActiveChapter(null);
      setBaseline(null);
      setForm(null);
    } finally {
      if (chapterLoadGuardRef.current.isLatest(seq)) {
        setLoadingChapter(false);
      }
    }
  }, []);

  useEffect(() => {
    if (!activeId) {
      chapterLoadGuardRef.current.invalidate();
      setActiveChapter(null);
      setBaseline(null);
      setForm(null);
      setChapterLoadError(null);
      setLoadingChapter(false);
      return;
    }
    void loadChapter(activeId);
  }, [activeId, loadChapter]);

  const retryChapter = useCallback(async () => {
    if (!activeId) return;
    await loadChapter(activeId, true);
  }, [activeId, loadChapter]);

  const saveChapter = useCallback(
    async (opts?: { snapshot?: ChapterForm; silent?: boolean }) => {
      const chapter = activeChapterRef.current;
      const current = formRef.current;
      if (!chapter || !current) return false;

      const silent = Boolean(opts?.silent);
      const snapshot = opts?.snapshot ?? current;
      if (!dirty && !opts?.snapshot) return true;

      const scheduleWizardRefresh = () => {
        if (refreshWizardDebounceRef.current !== null) {
          window.clearTimeout(refreshWizardDebounceRef.current);
        }
        refreshWizardDebounceRef.current = window.setTimeout(() => void refreshWizard(), 1200);
      };

      const enqueue = () => {
        saveQueuedRef.current = true;
        queuedSnapshotRef.current = snapshot;
        queuedSilentRef.current = queuedSilentRef.current && silent;
        if (!silent && !queuedToastShownRef.current) {
          toast.toastWarning(WRITING_PAGE_COPY.saveQueued);
          queuedToastShownRef.current = true;
        }
        if (!queuedPromiseRef.current) {
          queuedPromiseRef.current = new Promise<boolean>((resolve) => {
            queuedPromiseResolveRef.current = resolve;
          });
        }
        return queuedPromiseRef.current;
      };

      const performSave = async (nextSnapshot: ChapterForm, nextSilent: boolean): Promise<boolean> => {
        const latestChapter = activeChapterRef.current;
        if (!latestChapter) return false;

        const seq = saveGuardRef.current.next();
        savingRef.current = true;
        setSaving(true);
        try {
          const nextChapter = await chapterStore.updateChapterDetail(latestChapter.id, {
            title: nextSnapshot.title.trim(),
            plan: nextSnapshot.plan,
            content_md: nextSnapshot.content_md,
            summary: nextSnapshot.summary,
            status: nextSnapshot.status,
          });
          if (!saveGuardRef.current.isLatest(seq)) return true;
          setActiveChapter(nextChapter);
          const nextBaseline = chapterToForm(nextChapter);
          setBaseline(nextBaseline);
          setForm((prev) => {
            if (!prev) return prev;
            if (
              prev.title === nextSnapshot.title &&
              prev.plan === nextSnapshot.plan &&
              prev.content_md === nextSnapshot.content_md &&
              prev.summary === nextSnapshot.summary &&
              prev.status === nextSnapshot.status
            ) {
              return nextBaseline;
            }
            return prev;
          });
          markWizardProjectChanged(latestChapter.project_id);
          bumpWizardLocal();
          if (nextSilent) scheduleWizardRefresh();
          else await refreshWizard();
          if (!nextSilent) toast.toastSuccess(WRITING_PAGE_COPY.saveSuccess);
          return true;
        } catch (e) {
          const err = toApiError(e);
          toastApiError(toast, err);
          return false;
        } finally {
          setSaving(false);
          savingRef.current = false;
        }
      };

      if (savingRef.current) {
        return enqueue();
      }

      let ok = await performSave(snapshot, silent);

      while (saveQueuedRef.current) {
        const nextSnapshot = queuedSnapshotRef.current ?? formRef.current;
        const nextSilent = queuedSilentRef.current;
        saveQueuedRef.current = false;
        queuedSnapshotRef.current = null;
        queuedSilentRef.current = true;
        queuedToastShownRef.current = false;

        if (!nextSnapshot || !activeChapterRef.current) break;
        ok = await performSave(nextSnapshot, nextSilent);
        if (!ok) break;
      }

      if (queuedPromiseResolveRef.current) {
        queuedPromiseResolveRef.current(ok);
        queuedPromiseResolveRef.current = null;
        queuedPromiseRef.current = null;
        queuedToastShownRef.current = false;
      }

      return ok;
    },
    [bumpWizardLocal, dirty, refreshWizard, toast],
  );

  useSaveHotkey(() => void saveChapter(), dirty);

  useAutoSave({
    enabled: Boolean(projectId && activeChapter && form) && !loadingChapter,
    dirty,
    saveOnIdle: false,
    getSnapshot: () => (formRef.current ? { ...formRef.current } : null),
    onSave: async (snapshot) => {
      await saveChapter({ snapshot, silent: true });
    },
  });

  const requestSelectChapter = useCallback(
    async (id: string) => {
      if (id === activeId) return;
      if (dirty) {
        const choice = await confirm.choose(WRITING_PAGE_COPY.confirms.switchChapter);
        if (choice === "cancel") return;
        if (choice === "confirm") {
          const ok = await saveChapter();
          if (!ok) return;
        }
      }
      setActiveId(id);
    },
    [activeId, confirm, dirty, saveChapter],
  );

  return {
    loading,
    chapters,
    chapterListError: chaptersQuery.error,
    chapterListHasData: chaptersQuery.hasData,
    chapterListHasLoaded: chaptersQuery.hasLoaded,
    refreshChapters,
    retryChapterList: refreshChapters,
    activeId,
    setActiveId,
    activeChapter,
    baseline,
    form,
    setForm,
    dirty,
    saveChapter,
    requestSelectChapter,
    loadingChapter,
    chapterLoadError,
    retryChapter,
    saving,
  };
}
