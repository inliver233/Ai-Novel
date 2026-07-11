import { useCallback, useEffect, useRef, useState } from "react";

import { useChapterMetaList } from "./useChapterMetaList";
import { useProjectData } from "./useProjectData";
import { apiJson, type ApiError } from "../services/apiClient";
import { computeWizardProgress, onWizardProgressInvalidated, type WizardProgress } from "../services/wizard";
import type { ChapterListItem, Character, LLMPreset, LLMProfile, Outline, Project, ProjectSettings } from "../types";

type WizardLoaded = {
  project: Project;
  settings: ProjectSettings;
  characters: Character[];
  outline: Outline;
  llmPreset: LLMPreset;
  profiles: LLMProfile[];
};

const EMPTY_CHARACTERS: Character[] = [];
const EMPTY_CHAPTERS: ChapterListItem[] = [];

export function useWizardProgress(projectId: string | undefined): {
  loading: boolean;
  error: ApiError | null;
  blockingError: ApiError | null;
  refreshError: ApiError | null;
  progress: WizardProgress;
  refresh: () => Promise<void>;
  reload: () => Promise<void>;
  bumpLocal: () => void;
} {
  const [, setVersion] = useState(0);
  const chapterListQuery = useChapterMetaList(projectId, { toastOnError: false });

  const wizardQuery = useProjectData<WizardLoaded>(projectId, async (id) => {
    const [pRes, settingsRes, charsRes, outlineRes, presetRes, profilesRes] = await Promise.all([
      apiJson<{ project: Project }>(`/api/projects/${id}`),
      apiJson<{ settings: ProjectSettings }>(`/api/projects/${id}/settings`),
      apiJson<{ characters: Character[] }>(`/api/projects/${id}/characters`),
      apiJson<{ outline: Outline }>(`/api/projects/${id}/outline`),
      apiJson<{ llm_preset: LLMPreset }>(`/api/projects/${id}/llm_preset`),
      apiJson<{ profiles: LLMProfile[] }>(`/api/llm_profiles`),
    ]);
    return {
      project: pRes.data.project,
      settings: settingsRes.data.settings,
      characters: charsRes.data.characters,
      outline: outlineRes.data.outline,
      llmPreset: presetRes.data.llm_preset,
      profiles: profilesRes.data.profiles,
    };
  });
  const { data, refresh } = wizardQuery;

  const refreshDebounceRef = useRef<number | null>(null);
  const loadingRef = useRef(false);

  useEffect(() => {
    loadingRef.current =
      (wizardQuery.data === null && wizardQuery.loading) || (!chapterListQuery.hasData && chapterListQuery.loading);
  }, [chapterListQuery.hasData, chapterListQuery.loading, wizardQuery.data, wizardQuery.loading]);

  const bumpLocal = useCallback(() => {
    setVersion((v) => v + 1);
  }, []);

  const refreshChapters = chapterListQuery.refresh;
  const resetWizardError = wizardQuery.resetError;
  const refreshAll = useCallback(async () => {
    resetWizardError();
    await Promise.allSettled([refresh(), refreshChapters()]);
  }, [refresh, refreshChapters, resetWizardError]);

  useEffect(() => {
    if (!projectId) return;
    const off = onWizardProgressInvalidated((detail) => {
      if (detail.projectId !== projectId) return;
      bumpLocal();
      if (!detail.refresh) return;
      if (refreshDebounceRef.current !== null) {
        window.clearTimeout(refreshDebounceRef.current);
      }
      refreshDebounceRef.current = window.setTimeout(() => {
        refreshDebounceRef.current = null;
        if (loadingRef.current) return;
        void refreshAll();
      }, 80);
    });
    return () => {
      off();
      if (refreshDebounceRef.current !== null) {
        window.clearTimeout(refreshDebounceRef.current);
        refreshDebounceRef.current = null;
      }
    };
  }, [bumpLocal, projectId, refreshAll]);

  const project = data?.project ?? null;
  const selectedProfileId = project?.llm_profile_id ?? null;
  const profiles = data?.profiles ?? [];
  const llmProfile = selectedProfileId ? (profiles.find((p) => p.id === selectedProfileId) ?? null) : null;
  const progress = computeWizardProgress({
    project,
    settings: data?.settings ?? null,
    characters: data?.characters ?? EMPTY_CHARACTERS,
    outline: data?.outline ?? null,
    chapters: (chapterListQuery.chapters as ChapterListItem[]) ?? EMPTY_CHAPTERS,
    llmPreset: data?.llmPreset ?? null,
    llmProfile,
  });

  const blockingError =
    wizardQuery.data === null ? wizardQuery.error : !chapterListQuery.hasData ? chapterListQuery.error : null;
  const refreshError =
    wizardQuery.data !== null && wizardQuery.error
      ? wizardQuery.error
      : chapterListQuery.hasData
        ? chapterListQuery.error
        : null;

  return {
    loading:
      (wizardQuery.data === null && wizardQuery.loading) ||
      (wizardQuery.data !== null && !chapterListQuery.hasData && chapterListQuery.loading),
    error: blockingError ?? refreshError,
    blockingError,
    refreshError,
    progress,
    refresh: refreshAll,
    reload: refreshAll,
    bumpLocal,
  };
}
