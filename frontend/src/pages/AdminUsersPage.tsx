import { useCallback, useEffect, useMemo, useState } from "react";

import { useConfirm } from "../components/ui/confirm";
import { useToast } from "../components/ui/toast";
import { useAuth } from "../contexts/auth";
import { copyText } from "../lib/copyText";
import { humanizeYesNo } from "../lib/humanize";
import { ApiError, apiJson } from "../services/apiClient";

const PAGE_SIZE = 50;

type AdminUserActivity = {
  online: boolean;
  last_seen_at?: string | null;
  last_seen_request_id?: string | null;
  last_seen_path?: string | null;
  last_seen_method?: string | null;
  last_seen_status?: number | null;
};

type AdminUserUsage = {
  total_generation_calls: number;
  total_generation_error_calls: number;
  total_generated_chars: number;
  last_generation_at?: string | null;
};

type AdminUser = {
  id: string;
  email: string | null;
  display_name: string | null;
  is_admin: boolean;
  disabled: boolean;
  password_updated_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  activity?: AdminUserActivity;
  usage?: AdminUserUsage;
};

type AdminUsersSummary = {
  generated_at?: string | null;
  online_window_seconds: number;
  total_users: number;
  total_admin_users: number;
  total_disabled_users: number;
  total_online_users: number;
  filtered_total_users: number;
  total_generation_calls: number;
  total_generation_error_calls: number;
  total_generated_chars: number;
};

type AdminUsersPagination = {
  limit: number;
  cursor: string | null;
  next_cursor: string | null;
  has_more: boolean;
};

type AdminUsersResponse = {
  users: AdminUser[];
  summary: AdminUsersSummary;
  pagination: AdminUsersPagination;
};

type CreateUserForm = {
  user_id: string;
  display_name: string;
  email: string;
  is_admin: boolean;
  password: string;
};

function fmtDateTime(value: string | null | undefined): string {
  if (!value) return "-";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleString();
}

function fmtCount(value: number | null | undefined): string {
  const n = Number.isFinite(Number(value)) ? Number(value) : 0;
  return new Intl.NumberFormat("zh-CN").format(Math.max(0, Math.floor(n)));
}

export function AdminUsersPage() {
  const auth = useAuth();
  const toast = useToast();
  const confirm = useConfirm();

  const [loading, setLoading] = useState(false);
  const [creatingUser, setCreatingUser] = useState(false);
  const [searchInput, setSearchInput] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [onlineOnly, setOnlineOnly] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [cursorHistory, setCursorHistory] = useState<string[]>([]);
  const [reloadVersion, setReloadVersion] = useState(0);

  type RowBusy = { resetPassword?: number; toggleDisabled?: number };
  const [rowBusy, setRowBusy] = useState<Record<string, RowBusy>>({});
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [summary, setSummary] = useState<AdminUsersSummary>({
    generated_at: null,
    online_window_seconds: 300,
    total_users: 0,
    total_admin_users: 0,
    total_disabled_users: 0,
    total_online_users: 0,
    filtered_total_users: 0,
    total_generation_calls: 0,
    total_generation_error_calls: 0,
    total_generated_chars: 0,
  });
  const [pagination, setPagination] = useState<AdminUsersPagination>({
    limit: PAGE_SIZE,
    cursor: null,
    next_cursor: null,
    has_more: false,
  });
  const [tempPasswords, setTempPasswords] = useState<Record<string, string>>({});
  const [form, setForm] = useState<CreateUserForm>({
    user_id: "",
    display_name: "",
    email: "",
    is_admin: false,
    password: "",
  });

  const canManage = auth.status === "authenticated" && Boolean(auth.user?.isAdmin);

  const bumpRowBusy = useCallback((userId: string, action: keyof RowBusy, delta: number) => {
    setRowBusy((prev) => {
      const current = prev[userId] ?? {};
      const nextCount = (current[action] ?? 0) + delta;
      const nextUser: RowBusy = { ...current };
      if (nextCount <= 0) {
        delete nextUser[action];
      } else {
        nextUser[action] = nextCount;
      }
      const next = { ...prev };
      if (Object.keys(nextUser).length === 0) {
        delete next[userId];
        return next;
      }
      next[userId] = nextUser;
      return next;
    });
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      params.set("limit", String(PAGE_SIZE));
      if (cursor) params.set("cursor", cursor);
      if (searchQuery) params.set("q", searchQuery);
      if (onlineOnly) params.set("online_only", "true");

      const path = `/api/auth/admin/users?${params.toString()}`;
      const res = await apiJson<AdminUsersResponse>(path);
      const nextUsers = Array.isArray(res.data.users) ? res.data.users : [];

      setUsers(nextUsers);
      setSummary({
        generated_at: res.data.summary?.generated_at ?? null,
        online_window_seconds: Number(res.data.summary?.online_window_seconds ?? 300),
        total_users: Number(res.data.summary?.total_users ?? 0),
        total_admin_users: Number(res.data.summary?.total_admin_users ?? 0),
        total_disabled_users: Number(res.data.summary?.total_disabled_users ?? 0),
        total_online_users: Number(res.data.summary?.total_online_users ?? 0),
        filtered_total_users: Number(res.data.summary?.filtered_total_users ?? 0),
        total_generation_calls: Number(res.data.summary?.total_generation_calls ?? 0),
        total_generation_error_calls: Number(res.data.summary?.total_generation_error_calls ?? 0),
        total_generated_chars: Number(res.data.summary?.total_generated_chars ?? 0),
      });
      setPagination({
        limit: Number(res.data.pagination?.limit ?? PAGE_SIZE),
        cursor: res.data.pagination?.cursor ?? null,
        next_cursor: res.data.pagination?.next_cursor ?? null,
        has_more: Boolean(res.data.pagination?.has_more),
      });
    } catch (e) {
      const err =
        e instanceof ApiError
          ? e
          : new ApiError({ code: "UNKNOWN", message: String(e), requestId: "unknown", status: 0 });
      toast.toastError(`${err.message} (${err.code})`, err.requestId);
    } finally {
      setLoading(false);
    }
  }, [cursor, onlineOnly, searchQuery, toast]);

  useEffect(() => {
    if (!canManage) return;
    void load();
  }, [canManage, load, reloadVersion]);

  const createUser = useCallback(async () => {
    if (!canManage) return;
    const userId = form.user_id.trim();
    if (!userId) {
      toast.toastError("用户 ID 不能为空");
      return;
    }
    setCreatingUser(true);
    try {
      const res = await apiJson<{ user: AdminUser; temp_password: string | null }>("/api/auth/admin/users", {
        method: "POST",
        body: JSON.stringify({
          user_id: userId,
          display_name: form.display_name.trim() || null,
          email: form.email.trim() || null,
          is_admin: Boolean(form.is_admin),
          password: form.password.trim() || null,
        }),
      });
      const user = res.data.user;
      if (res.data.temp_password) {
        setTempPasswords((v) => ({ ...v, [user.id]: res.data.temp_password ?? "" }));
      }
      toast.toastSuccess("用户已创建", res.request_id);
      setForm((v) => ({ ...v, user_id: "", password: "" }));
      setSearchInput(userId);
      setSearchQuery(userId);
      setOnlineOnly(false);
      setCursor(null);
      setCursorHistory([]);
      setReloadVersion((version) => version + 1);
    } catch (e) {
      const err =
        e instanceof ApiError
          ? e
          : new ApiError({ code: "UNKNOWN", message: String(e), requestId: "unknown", status: 0 });
      toast.toastError(`${err.message} (${err.code})`, err.requestId);
    } finally {
      setCreatingUser(false);
    }
  }, [canManage, form.display_name, form.email, form.is_admin, form.password, form.user_id, toast]);

  const resetPassword = useCallback(
    async (targetUserId: string) => {
      if (!canManage) return;
      const ok = await confirm.confirm({
        title: "重置密码？",
        description: "将生成一次性密码。该密码只会在本页显示一次，复制后会自动隐藏。",
        confirmText: "重置",
        cancelText: "取消",
        danger: true,
      });
      if (!ok) return;
      bumpRowBusy(targetUserId, "resetPassword", 1);
      try {
        const res = await apiJson<{ temp_password: string }>(`/api/auth/admin/users/${targetUserId}/password/reset`, {
          method: "POST",
          body: JSON.stringify({}),
        });
        setTempPasswords((v) => ({ ...v, [targetUserId]: res.data.temp_password }));
        toast.toastSuccess("密码已重置（请复制一次性密码）", res.request_id);
      } catch (e) {
        const err =
          e instanceof ApiError
            ? e
            : new ApiError({ code: "UNKNOWN", message: String(e), requestId: "unknown", status: 0 });
        toast.toastError(`${err.message} (${err.code})`, err.requestId);
      } finally {
        bumpRowBusy(targetUserId, "resetPassword", -1);
      }
    },
    [bumpRowBusy, canManage, confirm, toast],
  );

  const setDisabled = useCallback(
    async (targetUserId: string, disabled: boolean) => {
      if (!canManage) return;
      const ok = await confirm.confirm({
        title: disabled ? "禁用用户？" : "启用用户？",
        description: disabled ? "禁用后该用户将无法登录。可以随时重新启用恢复。" : "启用后该用户将恢复登录权限。",
        confirmText: disabled ? "禁用" : "启用",
        cancelText: "取消",
        danger: disabled,
      });
      if (!ok) return;
      bumpRowBusy(targetUserId, "toggleDisabled", 1);
      try {
        await apiJson<Record<string, never>>(`/api/auth/admin/users/${targetUserId}/disable`, {
          method: "POST",
          body: JSON.stringify({ disabled }),
        });
        toast.toastSuccess(disabled ? "已禁用" : "已启用");
        await load();
      } catch (e) {
        const err =
          e instanceof ApiError
            ? e
            : new ApiError({ code: "UNKNOWN", message: String(e), requestId: "unknown", status: 0 });
        toast.toastError(`${err.message} (${err.code})`, err.requestId);
      } finally {
        bumpRowBusy(targetUserId, "toggleDisabled", -1);
      }
    },
    [bumpRowBusy, canManage, confirm, load, toast],
  );

  const copyTempPassword = useCallback(
    async (userId: string) => {
      const pwd = tempPasswords[userId];
      if (!pwd) return;
      const ok = await copyText(pwd, {
        title: "复制失败：请手动复制一次性密码",
        description: "关闭后将从页面隐藏。",
      });
      if (ok) {
        toast.toastSuccess("已复制一次性密码（已从页面隐藏）");
      } else {
        toast.toastWarning("自动复制失败：已打开手动复制弹窗（关闭后将从页面隐藏）。");
      }
      setTempPasswords((prev) => {
        const next = { ...prev };
        delete next[userId];
        return next;
      });
    },
    [tempPasswords, toast],
  );

  const onApplySearch = useCallback(() => {
    const nextQuery = searchInput.trim();
    setSearchQuery(nextQuery);
    setCursor(null);
    setCursorHistory([]);
  }, [searchInput]);

  const onResetSearch = useCallback(() => {
    setSearchInput("");
    setSearchQuery("");
    setCursor(null);
    setCursorHistory([]);
  }, []);

  const onToggleOnlineOnly = useCallback((next: boolean) => {
    setOnlineOnly(next);
    setCursor(null);
    setCursorHistory([]);
  }, []);

  const onNextPage = useCallback(() => {
    if (!pagination.has_more || !pagination.next_cursor) return;
    setCursorHistory((prev) => [...prev, cursor ?? ""]);
    setCursor(pagination.next_cursor);
  }, [cursor, pagination.has_more, pagination.next_cursor]);

  const onPrevPage = useCallback(() => {
    setCursorHistory((prev) => {
      if (prev.length === 0) return prev;
      const next = [...prev];
      const previousCursor = next.pop() ?? "";
      setCursor(previousCursor || null);
      return next;
    });
  }, []);

  const visibleUsers = useMemo(() => users, [users]);
  const hasPrevPage = cursorHistory.length > 0;
  const hasNextPage = pagination.has_more && Boolean(pagination.next_cursor);

  if (!canManage) {
    return (
      <div className="mx-auto max-w-screen-md px-4 py-10 sm:px-6 lg:px-8">
        <div className="rounded-atelier border border-border bg-surface p-6">
          <div className="font-content text-xl text-ink">管理员用户管理</div>
          <div className="mt-2 text-sm text-subtext">当前账号无管理员权限。请使用管理员账号登录。</div>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-screen-xl px-4 py-5 sm:px-6 sm:py-6 lg:px-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="font-content text-2xl text-ink">管理员用户管理</div>
          <div className="mt-1 text-xs text-subtext">创建用户 / 重置密码 / 启用禁用 / 在线与统计概览</div>
        </div>
        <div className="flex gap-2">
          <button className="btn btn-secondary" disabled={loading} onClick={() => void load()} type="button">
            {loading ? "加载中…" : "刷新列表"}
          </button>
        </div>
      </div>

      <section className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-atelier border border-border bg-surface p-4">
          <div className="text-xs text-subtext">在线用户</div>
          <div className="mt-1 text-2xl font-semibold text-ink">{fmtCount(summary.total_online_users)}</div>
          <div className="mt-1 text-xs text-subtext">
            窗口 {Math.max(1, Math.floor(summary.online_window_seconds / 60))} 分钟
          </div>
        </div>
        <div className="rounded-atelier border border-border bg-surface p-4">
          <div className="text-xs text-subtext">总用户 / 筛选后</div>
          <div className="mt-1 text-2xl font-semibold text-ink">
            {fmtCount(summary.total_users)} / {fmtCount(summary.filtered_total_users)}
          </div>
          <div className="mt-1 text-xs text-subtext">
            管理员 {fmtCount(summary.total_admin_users)}，禁用 {fmtCount(summary.total_disabled_users)}
          </div>
        </div>
        <div className="rounded-atelier border border-border bg-surface p-4">
          <div className="text-xs text-subtext">累计调用次数（LLM API）</div>
          <div className="mt-1 text-2xl font-semibold text-ink">{fmtCount(summary.total_generation_calls)}</div>
          <div className="mt-1 text-xs text-subtext">失败 {fmtCount(summary.total_generation_error_calls)}</div>
        </div>
        <div className="rounded-atelier border border-border bg-surface p-4">
          <div className="text-xs text-subtext">累计生成字数</div>
          <div className="mt-1 text-2xl font-semibold text-ink">{fmtCount(summary.total_generated_chars)}</div>
          <div className="mt-1 text-xs text-subtext">统计更新时间：{fmtDateTime(summary.generated_at)}</div>
        </div>
      </section>

      <form
        className="mt-4 rounded-atelier border border-border bg-surface p-4"
        onSubmit={(e) => {
          e.preventDefault();
          onApplySearch();
        }}
      >
        <div className="text-sm font-medium text-ink">筛选与分页</div>
        <div className="mt-3 grid gap-3 lg:grid-cols-[1fr_auto_auto_auto]">
          <label className="text-sm text-ink">
            <div className="text-xs text-subtext">按用户 ID / 显示名 / 邮箱筛选</div>
            <input
              id="admin_users_search"
              className="input mt-1"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder="输入关键词后回车或点击“应用筛选”"
            />
          </label>
          <label className="flex items-center gap-2 pt-6 text-sm text-ink">
            <input
              id="admin_users_online_only"
              className="checkbox"
              type="checkbox"
              checked={onlineOnly}
              onChange={(e) => onToggleOnlineOnly(e.target.checked)}
            />
            <span>仅看在线用户</span>
          </label>
          <button className="btn btn-secondary self-end" type="submit">
            应用筛选
          </button>
          <button className="btn btn-secondary self-end" onClick={onResetSearch} type="button">
            重置
          </button>
        </div>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-subtext">
          <div>
            当前每页 {fmtCount(pagination.limit)} 条，已加载 {fmtCount(visibleUsers.length)} 条
          </div>
          <div className="flex gap-2">
            <button
              className="btn btn-secondary btn-sm"
              disabled={!hasPrevPage || loading}
              onClick={onPrevPage}
              type="button"
            >
              上一页
            </button>
            <button
              className="btn btn-secondary btn-sm"
              disabled={!hasNextPage || loading}
              onClick={onNextPage}
              type="button"
            >
              下一页
            </button>
          </div>
        </div>
      </form>

      <form
        className="mt-6 rounded-atelier border border-border bg-surface p-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (creatingUser) return;
          void createUser();
        }}
      >
        <div className="text-sm font-medium text-ink">创建用户</div>
        <div className="mt-1 text-xs text-subtext">
          提示：留空“初始密码”会由系统生成一次性密码。一次性密码不会持久化保存，刷新页面后无法找回；建议创建/重置后立即复制并通过安全渠道发送给用户。
        </div>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <label className="text-sm text-ink">
            <div className="text-xs text-subtext">用户 ID（user_id）</div>
            <input
              id="admin_users_user_id"
              className="input mt-1"
              value={form.user_id}
              onChange={(e) => setForm((v) => ({ ...v, user_id: e.target.value }))}
              placeholder="例如：admin2"
            />
          </label>
          <label className="text-sm text-ink">
            <div className="text-xs text-subtext">显示名（display_name）</div>
            <input
              id="admin_users_display_name"
              className="input mt-1"
              value={form.display_name}
              onChange={(e) => setForm((v) => ({ ...v, display_name: e.target.value }))}
              placeholder="例如：管理员 2"
            />
          </label>
          <label className="text-sm text-ink">
            <div className="text-xs text-subtext">邮箱（email，可选）</div>
            <input
              id="admin_users_email"
              className="input mt-1"
              value={form.email}
              onChange={(e) => setForm((v) => ({ ...v, email: e.target.value }))}
              placeholder="例如：admin2@example.com"
            />
          </label>
          <label className="text-sm text-ink">
            <div className="text-xs text-subtext">初始密码（password，可选）</div>
            <input
              id="admin_users_password"
              className="input mt-1"
              type="password"
              autoComplete="new-password"
              value={form.password}
              onChange={(e) => setForm((v) => ({ ...v, password: e.target.value }))}
              placeholder="留空则生成一次性密码"
            />
          </label>
        </div>

        <div className="mt-3 flex items-center justify-between gap-3">
          <label className="flex items-center gap-2 text-sm text-ink">
            <input
              id="admin_users_is_admin"
              className="checkbox"
              type="checkbox"
              checked={form.is_admin}
              onChange={(e) => setForm((v) => ({ ...v, is_admin: e.target.checked }))}
            />
            <span>管理员（is_admin）</span>
          </label>
          <button className="btn btn-primary" disabled={creatingUser} type="submit">
            {creatingUser ? "提交中…" : "创建"}
          </button>
        </div>
      </form>

      <section className="mt-6 rounded-atelier border border-border bg-surface p-4">
        <div className="text-sm font-medium text-ink">用户列表</div>
        <div className="mt-1 text-xs text-subtext">
          安全提示：一次性密码仅用于首次登录/找回；建议用户首次登录后尽快修改。为降低泄露风险，本页默认不显示明文，一键复制后会自动隐藏。
        </div>

        <div className="mt-3 grid gap-3 md:hidden" aria-label="admin_users_cards">
          {visibleUsers.map((u) => (
            <div key={u.id} className="rounded-atelier border border-border bg-canvas p-3">
              <div className="min-w-0">
                <div className="flex items-center justify-between gap-2">
                  <div className="text-sm font-medium text-ink">{u.display_name ?? "-"}</div>
                  <span
                    className={
                      u.activity?.online
                        ? "rounded-full bg-emerald-500/15 px-2 py-0.5 text-[11px] text-emerald-700"
                        : "rounded-full bg-slate-500/15 px-2 py-0.5 text-[11px] text-subtext"
                    }
                  >
                    {u.activity?.online ? "在线" : "离线"}
                  </span>
                </div>
                <div className="mt-1 break-all font-mono text-xs text-subtext">{u.id}</div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-subtext">
                  <span>管理员：{humanizeYesNo(u.is_admin)}</span>
                  <span>已禁用：{humanizeYesNo(u.disabled)}</span>
                  <span>调用：{fmtCount(u.usage?.total_generation_calls ?? 0)}</span>
                  <span>字数：{fmtCount(u.usage?.total_generated_chars ?? 0)}</span>
                  <span className="col-span-2">最后活跃：{fmtDateTime(u.activity?.last_seen_at)}</span>
                </div>
              </div>

              <div className="mt-3 flex flex-wrap gap-2">
                {tempPasswords[u.id] ? (
                  <button
                    className="btn btn-secondary btn-sm"
                    disabled={Boolean(rowBusy[u.id]?.resetPassword)}
                    onClick={() => void copyTempPassword(u.id)}
                    type="button"
                  >
                    复制并隐藏
                  </button>
                ) : null}
                <button
                  className="btn btn-secondary btn-sm"
                  disabled={Boolean(rowBusy[u.id]?.resetPassword)}
                  onClick={() => void resetPassword(u.id)}
                  type="button"
                  title="将生成一次性密码（仅显示在本页，建议立即复制）。"
                >
                  重置密码
                </button>
                <button
                  className="btn btn-secondary btn-sm"
                  disabled={Boolean(rowBusy[u.id]?.toggleDisabled)}
                  onClick={() => void setDisabled(u.id, !u.disabled)}
                  type="button"
                >
                  {u.disabled ? "启用" : "禁用"}
                </button>
              </div>
            </div>
          ))}
          {visibleUsers.length === 0 ? <div className="p-2 text-xs text-subtext">暂无数据</div> : null}
        </div>

        <div className="mt-3 hidden overflow-auto md:block">
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-subtext">
              <tr>
                <th className="py-2 pr-3" scope="col">
                  用户 ID
                </th>
                <th className="py-2 pr-3" scope="col">
                  显示名
                </th>
                <th className="py-2 pr-3" scope="col">
                  管理员
                </th>
                <th className="py-2 pr-3" scope="col">
                  已禁用
                </th>
                <th className="py-2 pr-3" scope="col">
                  在线
                </th>
                <th className="py-2 pr-3" scope="col">
                  最后活跃
                </th>
                <th className="py-2 pr-3" scope="col">
                  调用次数
                </th>
                <th className="py-2 pr-3" scope="col">
                  生成字数
                </th>
                <th className="py-2 pr-3" scope="col">
                  一次性密码
                </th>
                <th className="py-2 pr-3" scope="col">
                  操作
                </th>
              </tr>
            </thead>
            <tbody>
              {visibleUsers.map((u) => (
                <tr key={u.id} className="border-t border-border">
                  <td className="py-2 pr-3 break-all font-mono text-xs">{u.id}</td>
                  <td className="py-2 pr-3">{u.display_name ?? "-"}</td>
                  <td className="py-2 pr-3">{humanizeYesNo(u.is_admin)}</td>
                  <td className="py-2 pr-3">{humanizeYesNo(u.disabled)}</td>
                  <td className="py-2 pr-3">
                    <span
                      className={
                        u.activity?.online
                          ? "rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs text-emerald-700"
                          : "rounded-full bg-slate-500/15 px-2 py-0.5 text-xs text-subtext"
                      }
                    >
                      {u.activity?.online ? "在线" : "离线"}
                    </span>
                  </td>
                  <td className="py-2 pr-3 text-xs text-subtext">{fmtDateTime(u.activity?.last_seen_at)}</td>
                  <td className="py-2 pr-3">{fmtCount(u.usage?.total_generation_calls ?? 0)}</td>
                  <td className="py-2 pr-3">{fmtCount(u.usage?.total_generated_chars ?? 0)}</td>
                  <td className="py-2 pr-3">
                    {tempPasswords[u.id] ? (
                      <button
                        className="btn btn-secondary btn-sm"
                        disabled={Boolean(rowBusy[u.id]?.resetPassword)}
                        onClick={() => void copyTempPassword(u.id)}
                        type="button"
                      >
                        复制并隐藏
                      </button>
                    ) : (
                      <span className="text-subtext">-</span>
                    )}
                  </td>
                  <td className="py-2 pr-3">
                    <div className="flex flex-wrap gap-2">
                      <button
                        className="btn btn-secondary btn-sm"
                        disabled={Boolean(rowBusy[u.id]?.resetPassword)}
                        onClick={() => void resetPassword(u.id)}
                        type="button"
                        title="将生成一次性密码（仅显示在本页，建议立即复制）。"
                      >
                        重置密码
                      </button>
                      <button
                        className="btn btn-secondary btn-sm"
                        disabled={Boolean(rowBusy[u.id]?.toggleDisabled)}
                        onClick={() => void setDisabled(u.id, !u.disabled)}
                        type="button"
                      >
                        {u.disabled ? "启用" : "禁用"}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {visibleUsers.length === 0 ? (
                <tr>
                  <td className="py-3 text-xs text-subtext" colSpan={10}>
                    暂无数据
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
