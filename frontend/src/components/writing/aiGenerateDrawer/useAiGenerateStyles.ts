import { toApiError } from "../../../services/apiError";
import { useEffect, useMemo, useState } from "react";

import { ApiError, apiJson } from "../../../services/apiClient";
import type { WritingStyle } from "./aiGenerateDrawerModels";

type WritingStylePayload = {
  styles: WritingStyle[];
};

type ProjectWritingStyleDefaultPayload = {
  default: { style_id?: string | null };
};

export function useAiGenerateStyles(args: { open: boolean; projectId?: string }) {
  const { open, projectId } = args;
  const [stylesLoading, setStylesLoading] = useState(false);
  const [presets, setPresets] = useState<WritingStyle[]>([]);
  const [userStyles, setUserStyles] = useState<WritingStyle[]>([]);
  const [projectDefaultStyleId, setProjectDefaultStyleId] = useState<string | null>(null);
  const [stylesError, setStylesError] = useState<ApiError | null>(null);

  useEffect(() => {
    if (!open || !projectId) return;
    let cancelled = false;
    void (async () => {
      setStylesLoading(true);
      setStylesError(null);
      try {
        const [presetRes, userRes, defaultRes] = await Promise.all([
          apiJson<WritingStylePayload>("/api/writing_styles/presets"),
          apiJson<WritingStylePayload>("/api/writing_styles"),
          apiJson<ProjectWritingStyleDefaultPayload>(`/api/projects/${projectId}/writing_style_default`),
        ]);
        if (cancelled) return;
        setPresets(presetRes.data.styles ?? []);
        setUserStyles(userRes.data.styles ?? []);
        setProjectDefaultStyleId(defaultRes.data.default?.style_id ?? null);
      } catch (error) {
        if (cancelled) return;
        const nextError = toApiError(error);
        setStylesError(nextError);
      } finally {
        if (!cancelled) {
          setStylesLoading(false);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [open, projectId]);

  const allStyles = useMemo(() => [...presets, ...userStyles], [presets, userStyles]);
  const projectDefaultStyle = useMemo(
    () => allStyles.find((style) => style.id === projectDefaultStyleId) ?? null,
    [allStyles, projectDefaultStyleId],
  );

  return {
    stylesLoading,
    presets,
    userStyles,
    stylesError,
    projectDefaultStyle,
  };
}
