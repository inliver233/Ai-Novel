import { toastApiError } from "../../lib/apiErrorPresentation";
import { toApiError } from "../../services/apiError";
import { useCallback } from "react";

import type { ConfirmApi } from "../../components/ui/confirm";
import type { ToastApi } from "../../components/ui/toast";
import { apiJson } from "../../services/apiClient";
import { chapterStore } from "../../services/chapterStore";
import { markWizardProjectChanged } from "../../services/wizard";
import type { Project } from "../../types";
import { WRITING_PAGE_COPY } from "./writingPageCopy";

export function useOutlineSwitcher(args: {
  projectId: string | undefined;
  activeOutlineId: string;
  dirty: boolean;
  confirm: ConfirmApi;
  toast: ToastApi;
  saveChapter: () => Promise<boolean>;
  bumpWizardLocal: () => void;
  refreshWizard: () => Promise<void>;
  refreshChapters: () => Promise<unknown>;
  refreshWriting: () => Promise<void>;
}) {
  const {
    projectId,
    activeOutlineId,
    dirty,
    confirm,
    toast,
    saveChapter,
    bumpWizardLocal,
    refreshWizard,
    refreshChapters,
    refreshWriting,
  } = args;

  return useCallback(
    async (nextOutlineId: string) => {
      if (!projectId) return;
      if (!nextOutlineId || nextOutlineId === activeOutlineId) return;

      if (dirty) {
        const choice = await confirm.choose(WRITING_PAGE_COPY.confirms.switchOutline);
        if (choice === "cancel") return;
        if (choice === "confirm") {
          const ok = await saveChapter();
          if (!ok) return;
        }
      }

      try {
        await apiJson<{ project: Project }>(`/api/projects/${projectId}`, {
          method: "PUT",
          body: JSON.stringify({ active_outline_id: nextOutlineId }),
        });
        markWizardProjectChanged(projectId);
        bumpWizardLocal();
        chapterStore.invalidateProjectChapters(projectId, { dropDetails: true });
        await refreshWriting();
        await refreshChapters();
        await refreshWizard();
        toast.toastSuccess(WRITING_PAGE_COPY.switchedOutline);
      } catch (e) {
        const err = toApiError(e);
        toastApiError(toast, err);
      }
    },
    [
      activeOutlineId,
      bumpWizardLocal,
      confirm,
      dirty,
      projectId,
      refreshChapters,
      refreshWriting,
      refreshWizard,
      saveChapter,
      toast,
    ],
  );
}
