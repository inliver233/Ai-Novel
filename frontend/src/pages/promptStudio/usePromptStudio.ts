import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { useToast } from "../../components/ui/toast";
import { ApiError, apiJson } from "../../services/apiClient";
import type { PromptStudioCategory, PromptStudioPresetDetail, PromptStudioPresetSummary } from "./types";

type PromptStudioError = {
  message: string;
  code: string;
  requestId?: string;
};

type PromptStudioCategoriesResponse = {
  categories: PromptStudioCategory[];
};

type PresetResponse = {
  preset: PromptStudioPresetDetail;
};

function toPromptStudioError(error: unknown): PromptStudioError {
  if (error instanceof ApiError) {
    return {
      message: error.message,
      code: error.code,
      requestId: error.requestId,
    };
  }

  return {
    message: "请求失败",
    code: "UNKNOWN_ERROR",
  };
}

function getDefaultPresetId(category: PromptStudioCategory | null | undefined): string | null {
  if (!category) return null;
  return category.presets.find((preset) => preset.is_active)?.id ?? category.presets[0]?.id ?? null;
}

function updateCategoryPresets(
  categories: PromptStudioCategory[],
  categoryKey: string,
  updater: (presets: PromptStudioPresetSummary[]) => PromptStudioPresetSummary[],
): PromptStudioCategory[] {
  return categories.map((category) =>
    category.key === categoryKey
      ? {
          ...category,
          presets: updater(category.presets),
        }
      : category,
  );
}

export function usePromptStudio() {
  const { projectId } = useParams();
  const toast = useToast();

  const categoriesRequestRef = useRef(0);
  const presetRequestRef = useRef(0);
  const selectedCategoryKeyRef = useRef("");
  const selectedPresetIdRef = useRef<string | null>(null);

  const [categories, setCategories] = useState<PromptStudioCategory[]>([]);
  const [selectedCategoryKey, setSelectedCategoryKeyState] = useState("");
  const [selectedPresetId, setSelectedPresetIdState] = useState<string | null>(null);
  const [presetDetail, setPresetDetail] = useState<PromptStudioPresetDetail | null>(null);
  const [draftName, setDraftName] = useState("");
  const [draftContent, setDraftContent] = useState("");
  const [loading, setLoading] = useState(true);
  const [presetLoading, setPresetLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<PromptStudioError | null>(null);
  const [presetError, setPresetError] = useState<PromptStudioError | null>(null);

  const setSelectedCategoryKey = useCallback((key: string) => {
    selectedCategoryKeyRef.current = key;
    setSelectedCategoryKeyState(key);
  }, []);

  const setSelectedPresetId = useCallback((presetId: string | null) => {
    selectedPresetIdRef.current = presetId;
    setSelectedPresetIdState(presetId);
  }, []);

  const resetEditor = useCallback((options?: { clearPresetError?: boolean }) => {
    setPresetDetail(null);
    setDraftName("");
    setDraftContent("");
    if (options?.clearPresetError !== false) {
      setPresetError(null);
    }
  }, []);

  const loadCategories = useCallback(
    async (options?: { silent?: boolean; preferredCategoryKey?: string | null; preferredPresetId?: string | null }) => {
      if (!projectId) {
        setCategories([]);
        setSelectedCategoryKey("");
        setSelectedPresetId(null);
        resetEditor();
        setLoadError(null);
        setLoading(false);
        return;
      }

      const requestSeq = ++categoriesRequestRef.current;
      if (!options?.silent) setLoading(true);

      try {
        const res = await apiJson<PromptStudioCategoriesResponse>(
          `/api/projects/${projectId}/prompt-studio/categories`,
        );
        if (requestSeq !== categoriesRequestRef.current) return;

        const nextCategories = res.data.categories ?? [];
        const currentCategoryKey = selectedCategoryKeyRef.current;
        const currentPresetId = selectedPresetIdRef.current;

        const nextCategoryKey =
          options?.preferredCategoryKey &&
          nextCategories.some((category) => category.key === options.preferredCategoryKey)
            ? options.preferredCategoryKey
            : currentCategoryKey && nextCategories.some((category) => category.key === currentCategoryKey)
              ? currentCategoryKey
              : (nextCategories[0]?.key ?? "");

        const nextCategory = nextCategories.find((category) => category.key === nextCategoryKey) ?? null;
        const nextPresetId =
          options?.preferredPresetId && nextCategory?.presets.some((preset) => preset.id === options.preferredPresetId)
            ? options.preferredPresetId
            : currentPresetId && nextCategory?.presets.some((preset) => preset.id === currentPresetId)
              ? currentPresetId
              : getDefaultPresetId(nextCategory);

        setCategories(nextCategories);
        setLoadError(null);
        setSelectedCategoryKey(nextCategoryKey);
        setSelectedPresetId(nextPresetId);

        if (
          !nextCategory ||
          !nextPresetId ||
          nextCategoryKey !== currentCategoryKey ||
          nextPresetId !== currentPresetId
        ) {
          resetEditor();
        }
      } catch (error) {
        if (requestSeq !== categoriesRequestRef.current) return;
        const nextError = toPromptStudioError(error);
        setLoadError(nextError);
        toast.toastError(`${nextError.message} (${nextError.code})`, nextError.requestId);
      } finally {
        if (requestSeq === categoriesRequestRef.current && !options?.silent) {
          setLoading(false);
        }
      }
    },
    [projectId, resetEditor, setSelectedCategoryKey, setSelectedPresetId, toast],
  );

  const loadPresetDetail = useCallback(
    async (categoryKey: string, presetId: string) => {
      if (!projectId) return;

      const requestSeq = ++presetRequestRef.current;
      setPresetLoading(true);
      setPresetError(null);

      try {
        const params = new URLSearchParams({ category: categoryKey });
        const res = await apiJson<PresetResponse>(
          `/api/projects/${projectId}/prompt-studio/presets/${presetId}?${params.toString()}`,
        );
        if (requestSeq !== presetRequestRef.current) return;

        const nextPreset = res.data.preset;
        setPresetDetail(nextPreset);
        setDraftName(nextPreset.name);
        setDraftContent(nextPreset.content);
        setPresetError(null);
      } catch (error) {
        if (requestSeq !== presetRequestRef.current) return;
        const nextError = toPromptStudioError(error);
        setPresetError(nextError);
        resetEditor({ clearPresetError: false });
        toast.toastError(`${nextError.message} (${nextError.code})`, nextError.requestId);
      } finally {
        if (requestSeq === presetRequestRef.current) {
          setPresetLoading(false);
        }
      }
    },
    [projectId, resetEditor, toast],
  );

  useEffect(() => {
    void loadCategories();
  }, [loadCategories]);

  useEffect(() => {
    if (!projectId || !selectedCategoryKey || !selectedPresetId) {
      setPresetLoading(false);
      resetEditor();
      return;
    }

    void loadPresetDetail(selectedCategoryKey, selectedPresetId);
  }, [loadPresetDetail, projectId, resetEditor, selectedCategoryKey, selectedPresetId]);

  const selectedCategory = useMemo(
    () => categories.find((category) => category.key === selectedCategoryKey) ?? null,
    [categories, selectedCategoryKey],
  );

  const selectedPresetSummary = useMemo(
    () => selectedCategory?.presets.find((preset) => preset.id === selectedPresetId) ?? null,
    [selectedCategory, selectedPresetId],
  );

  const hasChanges = useMemo(() => {
    if (!presetDetail) return false;
    return draftName !== presetDetail.name || draftContent !== presetDetail.content;
  }, [draftContent, draftName, presetDetail]);

  const selectCategory = useCallback(
    (key: string) => {
      if (!key || key === selectedCategoryKeyRef.current) return;
      const nextCategory = categories.find((category) => category.key === key) ?? null;
      setSelectedCategoryKey(key);
      setSelectedPresetId(getDefaultPresetId(nextCategory));
      resetEditor();
    },
    [categories, resetEditor, setSelectedCategoryKey, setSelectedPresetId],
  );

  const selectPreset = useCallback(
    (presetId: string) => {
      if (!presetId || presetId === selectedPresetIdRef.current) return;
      setSelectedPresetId(presetId);
      resetEditor();
    },
    [resetEditor, setSelectedPresetId],
  );

  const createPreset = useCallback(
    async (name: string, content: string): Promise<PromptStudioPresetDetail | null> => {
      if (!projectId) return null;
      const categoryKey = selectedCategoryKeyRef.current;
      if (!categoryKey) {
        toast.toastError("请先选择分类");
        return null;
      }

      const nextName = name.trim();
      if (!nextName) {
        toast.toastError("请输入预设名称");
        return null;
      }
      if (!content.trim()) {
        toast.toastError("请输入预设内容");
        return null;
      }

      setBusy(true);
      try {
        const params = new URLSearchParams({ category: categoryKey });
        const res = await apiJson<PresetResponse>(
          `/api/projects/${projectId}/prompt-studio/presets?${params.toString()}`,
          {
            method: "POST",
            body: JSON.stringify({
              name: nextName,
              content,
            }),
          },
        );
        const nextPreset = res.data.preset;

        setCategories((prev) =>
          updateCategoryPresets(prev, categoryKey, (presets) => [
            ...presets,
            {
              id: nextPreset.id,
              name: nextPreset.name,
              is_active: nextPreset.is_active,
            },
          ]),
        );
        setSelectedPresetId(nextPreset.id);
        setPresetDetail(nextPreset);
        setDraftName(nextPreset.name);
        setDraftContent(nextPreset.content);
        setPresetError(null);
        setLoadError(null);
        toast.toastSuccess("已创建预设", res.request_id);
        return nextPreset;
      } catch (error) {
        const nextError = toPromptStudioError(error);
        toast.toastError(`${nextError.message} (${nextError.code})`, nextError.requestId);
        return null;
      } finally {
        setBusy(false);
      }
    },
    [projectId, setSelectedPresetId, toast],
  );

  const updatePreset = useCallback(async (): Promise<PromptStudioPresetDetail | null> => {
    if (!projectId) return null;
    const presetId = selectedPresetIdRef.current;
    const categoryKey = selectedCategoryKeyRef.current;
    if (!presetId) return null;

    const nextName = draftName.trim();
    if (!nextName) {
      toast.toastError("请输入预设名称");
      return null;
    }
    if (!draftContent.trim()) {
      toast.toastError("请输入预设内容");
      return null;
    }

    setBusy(true);
    try {
      const res = await apiJson<PresetResponse>(`/api/projects/${projectId}/prompt-studio/presets/${presetId}`, {
        method: "PUT",
        body: JSON.stringify({
          name: nextName,
          content: draftContent,
        }),
      });
      const nextPreset = res.data.preset;

      setPresetDetail(nextPreset);
      setDraftName(nextPreset.name);
      setDraftContent(nextPreset.content);
      setPresetError(null);
      setCategories((prev) =>
        updateCategoryPresets(prev, categoryKey, (presets) =>
          presets.map((preset) =>
            preset.id === presetId
              ? {
                  ...preset,
                  name: nextPreset.name,
                  is_active: nextPreset.is_active,
                }
              : preset,
          ),
        ),
      );
      toast.toastSuccess("已保存预设", res.request_id);
      return nextPreset;
    } catch (error) {
      const nextError = toPromptStudioError(error);
      toast.toastError(`${nextError.message} (${nextError.code})`, nextError.requestId);
      return null;
    } finally {
      setBusy(false);
    }
  }, [draftContent, draftName, projectId, toast]);

  const deletePreset = useCallback(async (): Promise<boolean> => {
    if (!projectId) return false;
    const categoryKey = selectedCategoryKeyRef.current;
    const presetId = selectedPresetIdRef.current;
    if (!categoryKey || !presetId) return false;

    setBusy(true);
    try {
      const res = await apiJson<Record<string, never>>(`/api/projects/${projectId}/prompt-studio/presets/${presetId}`, {
        method: "DELETE",
      });

      const nextCategories = updateCategoryPresets(categories, categoryKey, (presets) =>
        presets.filter((preset) => preset.id !== presetId),
      );
      const nextCategory = nextCategories.find((category) => category.key === categoryKey) ?? null;
      const nextPresetId = getDefaultPresetId(nextCategory);

      setCategories(nextCategories);
      setSelectedPresetId(nextPresetId);
      resetEditor();
      toast.toastSuccess("已删除预设", res.request_id);
      return true;
    } catch (error) {
      const nextError = toPromptStudioError(error);
      toast.toastError(`${nextError.message} (${nextError.code})`, nextError.requestId);
      return false;
    } finally {
      setBusy(false);
    }
  }, [categories, projectId, resetEditor, setSelectedPresetId, toast]);

  const activatePreset = useCallback(async (): Promise<PromptStudioPresetDetail | null> => {
    if (!projectId) return null;
    const categoryKey = selectedCategoryKeyRef.current;
    const presetId = selectedPresetIdRef.current;
    if (!categoryKey || !presetId) return null;

    setBusy(true);
    try {
      const params = new URLSearchParams({ category: categoryKey });
      const res = await apiJson<PresetResponse>(
        `/api/projects/${projectId}/prompt-studio/presets/${presetId}/activate?${params.toString()}`,
        {
          method: "PUT",
        },
      );
      const nextPreset = res.data.preset;

      setPresetDetail(nextPreset);
      setDraftName(nextPreset.name);
      setDraftContent(nextPreset.content);
      setPresetError(null);
      setCategories((prev) =>
        updateCategoryPresets(prev, categoryKey, (presets) =>
          presets.map((preset) => ({
            ...preset,
            name: preset.id === presetId ? nextPreset.name : preset.name,
            is_active: preset.id === presetId,
          })),
        ),
      );
      toast.toastSuccess("已切换生效预设", res.request_id);
      return nextPreset;
    } catch (error) {
      const nextError = toPromptStudioError(error);
      toast.toastError(`${nextError.message} (${nextError.code})`, nextError.requestId);
      return null;
    } finally {
      setBusy(false);
    }
  }, [projectId, toast]);

  const syncBuiltinDefaults = useCallback(async (): Promise<boolean> => {
    if (!projectId) return false;
    setBusy(true);
    try {
      const res = await apiJson<{ synced: number; resource_keys: string[] }>(
        `/api/projects/${projectId}/prompt_presets/sync_builtin_defaults`,
        { method: "POST" },
      );
      await loadCategories({ silent: true });
      toast.toastSuccess(`已同步 ${res.data.synced} 组内置提示词`, res.request_id);
      return true;
    } catch (error) {
      const nextError = toPromptStudioError(error);
      toast.toastError(`${nextError.message} (${nextError.code})`, nextError.requestId);
      return false;
    } finally {
      setBusy(false);
    }
  }, [loadCategories, projectId, toast]);

  return {
    projectId,
    categories,
    selectedCategoryKey,
    selectedCategory,
    selectedPresetId,
    selectedPresetSummary,
    presetDetail,
    draftName,
    draftContent,
    setDraftName,
    setDraftContent,
    loading,
    presetLoading,
    busy,
    loadError,
    presetError,
    hasChanges,
    loadCategories,
    selectCategory,
    selectPreset,
    createPreset,
    updatePreset,
    deletePreset,
    activatePreset,
    syncBuiltinDefaults,
  };
}
