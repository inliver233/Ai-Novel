import { toastApiError } from "../../lib/apiErrorPresentation";
import { toApiError } from "../../services/apiError";
import { type ComponentProps, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { WizardNextBar } from "../../components/atelier/WizardNextBar";
import { useToast } from "../../components/ui/toast";
import { useAuth } from "../../contexts/auth";
import { useProjects } from "../../contexts/projects";
import { useAutoSave } from "../../hooks/useAutoSave";
import { usePersistentOutletIsActive } from "../../hooks/usePersistentOutlet";
import { useProjectData } from "../../hooks/useProjectData";
import { useQueuedSave } from "../../hooks/useQueuedSave";
import { useSaveHotkey } from "../../hooks/useSaveHotkey";
import { useWizardProgress } from "../../hooks/useWizardProgress";
import { apiJson, type ApiError } from "../../services/apiClient";
import { markWizardProjectChanged } from "../../services/wizard";
import type { Project, ProjectSettings } from "../../types";
import {
  createDefaultProjectForm,
  createDefaultSettingsForm,
  mapLoadedSettingsToForms,
  type ProjectForm,
  type ProjectMembershipItem,
  type SaveSnapshot,
  type SettingsForm,
  type SettingsLoaded,
} from "./models";
import { SettingsCoreSections } from "./SettingsCoreSections";

type SettingsPageBlockingLoadError = {
  message: string;
  code: string;
  requestId?: string;
};

type SettingsPageState = {
  loading: boolean;
  blockingLoadError: SettingsPageBlockingLoadError | null;
  refreshLoadError: ApiError | null;
  reloadAll: () => Promise<void>;
  dirty: boolean;
  outletActive: boolean;
  coreSectionsProps: ComponentProps<typeof SettingsCoreSections>;
  wizardBarProps: ComponentProps<typeof WizardNextBar>;
};

export function useSettingsPageState(): SettingsPageState {
  const { projectId } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const auth = useAuth();
  const { refresh } = useProjects();
  const outletActive = usePersistentOutletIsActive();
  const wizard = useWizardProgress(projectId);
  const refreshWizard = wizard.refresh;
  const bumpWizardLocal = wizard.bumpLocal;

  const wizardRefreshTimerRef = useRef<number | null>(null);
  const projectsRefreshTimerRef = useRef<number | null>(null);
  const baselineProjectRef = useRef<Project | null>(null);
  const baselineSettingsRef = useRef<ProjectSettings | null>(null);
  const [baselineProject, setBaselineProject] = useState<Project | null>(null);
  const [baselineSettings, setBaselineSettings] = useState<ProjectSettings | null>(null);
  const [projectForm, setProjectForm] = useState<ProjectForm>(() => createDefaultProjectForm());
  const [settingsForm, setSettingsForm] = useState<SettingsForm>(() => createDefaultSettingsForm());

  const settingsQuery = useProjectData<SettingsLoaded>(projectId, async (id) => {
    const [pRes, sRes] = await Promise.all([
      apiJson<{ project: Project }>(`/api/projects/${id}`),
      apiJson<{ settings: ProjectSettings }>(`/api/projects/${id}/settings`),
    ]);
    return { project: pRes.data.project, settings: sRes.data.settings };
  });

  const dirty = useMemo(() => {
    if (!baselineProject || !baselineSettings) return false;
    return (
      projectForm.name !== baselineProject.name ||
      projectForm.genre !== (baselineProject.genre ?? "") ||
      projectForm.logline !== (baselineProject.logline ?? "") ||
      settingsForm.world_setting !== baselineSettings.world_setting ||
      settingsForm.style_guide !== baselineSettings.style_guide ||
      settingsForm.constraints !== baselineSettings.constraints
    );
  }, [baselineProject, baselineSettings, projectForm, settingsForm]);

  const dirtyRef = useRef(false);
  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);

  useEffect(() => {
    if (!settingsQuery.data) return;
    const { project, settings } = settingsQuery.data;
    // 刷新成功也不得覆盖未保存表单（fail-closed：dirty 时保留用户编辑）；
    // 切换到另一项目（project.id 变化）时必须重新映射，不受 dirty 保护。
    if (dirtyRef.current && baselineProjectRef.current?.id === project.id) return;
    const mapped = mapLoadedSettingsToForms(settingsQuery.data);
    baselineProjectRef.current = project;
    baselineSettingsRef.current = settings;
    setBaselineProject(project);
    setBaselineSettings(settings);
    setProjectForm(mapped.projectForm);
    setSettingsForm(mapped.settingsForm);
  }, [settingsQuery.data]);

  const [membershipsLoading, setMembershipsLoading] = useState(false);
  const [membershipSaving, setMembershipSaving] = useState(false);
  const [memberships, setMemberships] = useState<ProjectMembershipItem[]>([]);
  const [inviteUserId, setInviteUserId] = useState("");
  const [inviteRole, setInviteRole] = useState<"viewer" | "editor">("viewer");

  const canManageMemberships = useMemo(() => {
    if (!baselineProject) return false;
    const uid = auth.user?.id ?? "";
    return Boolean(uid) && baselineProject.owner_user_id === uid;
  }, [auth.user?.id, baselineProject]);

  const loadMemberships = useCallback(async () => {
    if (!projectId) return;
    setMembershipsLoading(true);
    try {
      const res = await apiJson<{ memberships: ProjectMembershipItem[] }>(`/api/projects/${projectId}/memberships`);
      const next = Array.isArray(res.data.memberships) ? res.data.memberships : [];
      next.sort((a, b) => String(a.user?.id ?? "").localeCompare(String(b.user?.id ?? "")));
      setMemberships(next);
    } catch (e) {
      const err = toApiError(e);
      toastApiError(toast, err);
    } finally {
      setMembershipsLoading(false);
    }
  }, [projectId, toast]);

  useEffect(() => {
    if (!canManageMemberships) return;
    void loadMemberships();
  }, [canManageMemberships, loadMemberships]);

  const inviteMember = useCallback(async () => {
    if (!projectId) return;
    const targetUserId = inviteUserId.trim();
    if (!targetUserId) {
      toast.toastError("user_id 不能为空");
      return;
    }
    setMembershipSaving(true);
    try {
      await apiJson<{ membership: unknown }>(`/api/projects/${projectId}/memberships`, {
        method: "POST",
        body: JSON.stringify({ user_id: targetUserId, role: inviteRole }),
      });
      setInviteUserId("");
      toast.toastSuccess("已邀请成员");
      await loadMemberships();
    } catch (e) {
      const err = toApiError(e);
      toastApiError(toast, err);
    } finally {
      setMembershipSaving(false);
    }
  }, [inviteRole, inviteUserId, loadMemberships, projectId, toast]);

  const updateMemberRole = useCallback(
    async (targetUserId: string, role: "viewer" | "editor") => {
      if (!projectId) return;
      setMembershipSaving(true);
      try {
        await apiJson<{ membership: unknown }>(`/api/projects/${projectId}/memberships/${targetUserId}`, {
          method: "PUT",
          body: JSON.stringify({ role }),
        });
        toast.toastSuccess("已更新角色");
        await loadMemberships();
      } catch (e) {
        const err = toApiError(e);
        toastApiError(toast, err);
      } finally {
        setMembershipSaving(false);
      }
    },
    [loadMemberships, projectId, toast],
  );

  const removeMember = useCallback(
    async (targetUserId: string) => {
      if (!projectId) return;
      setMembershipSaving(true);
      try {
        await apiJson<Record<string, never>>(`/api/projects/${projectId}/memberships/${targetUserId}`, {
          method: "DELETE",
        });
        toast.toastSuccess("已移除成员");
        await loadMemberships();
      } catch (e) {
        const err = toApiError(e);
        toastApiError(toast, err);
      } finally {
        setMembershipSaving(false);
      }
    },
    [loadMemberships, projectId, toast],
  );

  useEffect(() => {
    return () => {
      if (wizardRefreshTimerRef.current !== null) window.clearTimeout(wizardRefreshTimerRef.current);
      if (projectsRefreshTimerRef.current !== null) window.clearTimeout(projectsRefreshTimerRef.current);
    };
  }, []);

  const performSave = useCallback(
    async (opts?: { silent?: boolean; snapshot?: SaveSnapshot }): Promise<boolean> => {
      if (!projectId) return false;
      const silent = Boolean(opts?.silent);
      const snapshot = opts?.snapshot;
      const nextProjectForm = snapshot?.projectForm ?? projectForm;
      const nextSettingsForm = snapshot?.settingsForm ?? settingsForm;

      const currentBaselineProject = baselineProjectRef.current;
      const currentBaselineSettings = baselineSettingsRef.current;
      if (!currentBaselineProject || !currentBaselineSettings) return false;
      const projectDirty =
        nextProjectForm.name.trim() !== currentBaselineProject.name ||
        nextProjectForm.genre.trim() !== (currentBaselineProject.genre ?? "") ||
        nextProjectForm.logline.trim() !== (currentBaselineProject.logline ?? "");
      const settingsDirty =
        nextSettingsForm.world_setting !== currentBaselineSettings.world_setting ||
        nextSettingsForm.style_guide !== currentBaselineSettings.style_guide ||
        nextSettingsForm.constraints !== currentBaselineSettings.constraints;
      if (!projectDirty && !settingsDirty) return true;

      const scheduleWizardRefresh = () => {
        if (wizardRefreshTimerRef.current !== null) window.clearTimeout(wizardRefreshTimerRef.current);
        wizardRefreshTimerRef.current = window.setTimeout(() => void refreshWizard(), 1200);
      };
      const scheduleProjectsRefresh = () => {
        if (projectsRefreshTimerRef.current !== null) window.clearTimeout(projectsRefreshTimerRef.current);
        projectsRefreshTimerRef.current = window.setTimeout(() => void refresh(), 1200);
      };

      try {
        const [pRes, sRes] = await Promise.all([
          projectDirty
            ? apiJson<{ project: Project }>(`/api/projects/${projectId}`, {
                method: "PUT",
                body: JSON.stringify({
                  name: nextProjectForm.name.trim(),
                  genre: nextProjectForm.genre.trim() || null,
                  logline: nextProjectForm.logline.trim() || null,
                }),
              })
            : null,
          settingsDirty
            ? apiJson<{ settings: ProjectSettings }>(`/api/projects/${projectId}/settings`, {
                method: "PUT",
                body: JSON.stringify({
                  world_setting: nextSettingsForm.world_setting,
                  style_guide: nextSettingsForm.style_guide,
                  constraints: nextSettingsForm.constraints,
                }),
              })
            : null,
        ]);

        if (pRes) {
          baselineProjectRef.current = pRes.data.project;
          setBaselineProject(pRes.data.project);
        }
        if (sRes) {
          baselineSettingsRef.current = sRes.data.settings;
          setBaselineSettings(sRes.data.settings);
        }
        markWizardProjectChanged(projectId);
        bumpWizardLocal();
        if (silent) {
          scheduleProjectsRefresh();
          scheduleWizardRefresh();
        } else {
          await refresh();
          await refreshWizard();
          toast.toastSuccess("已保存");
        }
        return true;
      } catch (e) {
        const err = toApiError(e);
        toastApiError(toast, err);
        return false;
      }
    },
    [bumpWizardLocal, projectForm, projectId, refresh, refreshWizard, settingsForm, toast],
  );

  const { save: queueSettingsSave, saving } = useQueuedSave(performSave);
  const save = useCallback(
    (opts?: { silent?: boolean; snapshot?: SaveSnapshot }) => {
      const snapshot = opts?.snapshot;
      return queueSettingsSave({
        silent: Boolean(opts?.silent),
        snapshot: {
          projectForm: { ...(snapshot?.projectForm ?? projectForm) },
          settingsForm: { ...(snapshot?.settingsForm ?? settingsForm) },
        },
      });
    },
    [projectForm, queueSettingsSave, settingsForm],
  );

  useSaveHotkey(() => void save(), dirty);

  useAutoSave({
    enabled: Boolean(projectId && baselineProject && baselineSettings),
    dirty,
    delayMs: 1200,
    getSnapshot: () => ({ projectForm: { ...projectForm }, settingsForm: { ...settingsForm } }),
    onSave: async (snapshot) => {
      await save({ silent: true, snapshot });
    },
    deps: [
      projectForm.name,
      projectForm.genre,
      projectForm.logline,
      settingsForm.world_setting,
      settingsForm.style_guide,
      settingsForm.constraints,
    ],
  });

  const gotoCharacters = useCallback(async () => {
    if (!projectId) return;
    if (saving) return;
    if (dirty) {
      const ok = await save();
      if (!ok) return;
    }
    navigate(`/projects/${projectId}/characters`);
  }, [dirty, navigate, projectId, save, saving]);

  // data 到达与 baseline 映射之间存在一帧空档：此时 coreSectionsProps 仍为 null，
  // 必须继续视为 loading，否则表单会以空 props 渲染崩溃。
  const ready = baselineProject !== null && baselineSettings !== null;
  const loading = settingsQuery.data === null ? settingsQuery.loading : !ready;

  return {
    loading,
    blockingLoadError:
      !loading && settingsQuery.data === null
        ? settingsQuery.error
          ? {
              message: settingsQuery.error.message,
              code: settingsQuery.error.code,
              requestId: settingsQuery.error.requestId === "unknown" ? undefined : settingsQuery.error.requestId,
            }
          : { message: "项目加载失败", code: "UNKNOWN_ERROR" }
        : null,
    refreshLoadError: settingsQuery.data !== null ? settingsQuery.error : null,
    reloadAll: async () => {
      await settingsQuery.refresh();
    },
    dirty,
    outletActive,
    coreSectionsProps:
      baselineProject && baselineSettings
        ? {
            projectForm,
            setProjectForm,
            settingsForm,
            setSettingsForm,
            dirty,
            saving,
            onGoToCharacters: () => void gotoCharacters(),
            onSave: () => void save(),
            baselineProject,
            canManageMemberships,
            currentUserId: auth.user?.id ?? "unknown",
            membershipsLoading,
            membershipSaving,
            memberships,
            inviteUserId,
            onChangeInviteUserId: setInviteUserId,
            inviteRole,
            onChangeInviteRole: (role) => setInviteRole(role),
            onInviteMember: () => void inviteMember(),
            onLoadMemberships: () => void loadMemberships(),
            onUpdateMemberRole: (targetUserId, role) => void updateMemberRole(targetUserId, role),
            onRemoveMember: (targetUserId) => void removeMember(targetUserId),
          }
        : (null as never),
    wizardBarProps: {
      projectId,
      currentStep: "settings",
      progress: wizard.progress,
      loading: wizard.loading,
      loadError: wizard.error,
      onRetryLoad: wizard.reload,
      retryBlockedReason: dirty ? "请先保存或放弃未保存修改再重试" : undefined,
      dirty,
      saving,
      onSave: save,
    },
  };
}
