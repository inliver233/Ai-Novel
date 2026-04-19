import clsx from "clsx";
import { AnimatePresence, LayoutGroup, motion, useReducedMotion } from "framer-motion";
import { LayoutDashboard, PanelLeftClose, PanelLeftOpen, UserCog } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { NavLink, useLocation, useNavigate, useOutlet, useParams } from "react-router-dom";

import { ProjectSwitcher } from "../atelier/ProjectSwitcher";
import { ThemeToggle } from "../atelier/ThemeToggle";
import { useAuth } from "../../contexts/auth";
import { PersistentOutletProvider } from "../../hooks/PersistentOutletProvider";
import { resolveRouteMeta } from "../../lib/routes";
import { UI_COPY } from "../../lib/uiCopy";
import { fadeUpVariants, transition } from "../../lib/motion";
import { getCurrentUserId } from "../../services/currentUser";
import {
  sidebarCollapsedStorageKey,
} from "../../services/uiState";
import {
  APP_SHELL_PRIMARY_PROJECT_NAV_GROUPS,
  APP_SHELL_PROJECT_NAV_GROUP_TITLES,
  getAppShellProjectNavItems,
  type AppShellProjectNavGroup,
} from "./appShellNavConfig";

function useSidebarCollapsed(): [boolean, (v: boolean) => void] {
  const storageKey = sidebarCollapsedStorageKey(getCurrentUserId());
  const [collapsed, setCollapsed] = useState<boolean>(() => localStorage.getItem(storageKey) === "1");
  return [
    collapsed,
    (v) => {
      setCollapsed(v);
      localStorage.setItem(storageKey, v ? "1" : "0");
    },
  ];
}


function SidebarLink(props: {
  to: string;
  icon: React.ReactNode;
  label: string;
  ariaLabel?: string;
  collapsed: boolean;
  onClick?: () => void;
}) {
  return (
    <NavLink
      className={({ isActive }) =>
        clsx(
          "ui-focus-ring ui-transition-fast group relative flex w-full items-center overflow-hidden rounded-atelier py-2 text-sm no-underline hover:no-underline motion-safe:active:scale-[0.99]",
          props.collapsed ? "justify-center px-0" : "justify-start gap-3 px-3",
          isActive ? "text-ink" : "text-subtext hover:bg-canvas hover:text-ink",
        )
      }
      to={props.to}
      aria-label={props.ariaLabel ?? props.label}
      title={props.collapsed ? props.label : undefined}
      onClick={props.onClick}
    >
      {({ isActive }) => (
        <>
          {isActive ? (
            <motion.span
              layoutId="atelier-sidebar-active"
              className="absolute inset-0 rounded-atelier bg-canvas"
              transition={transition.fast}
            />
          ) : null}
          <span className="relative z-10 shrink-0">{props.icon}</span>
          {props.collapsed ? null : <span className="relative z-10 min-w-0 truncate">{props.label}</span>}
        </>
      )}
    </NavLink>
  );
}

function ProjectNavGroupTitle(props: { label: string; collapsed: boolean; className?: string }) {
  if (props.collapsed) return null;
  return <div className={clsx("px-3 pt-2 text-[11px] font-medium text-subtext", props.className)}>{props.label}</div>;
}

function renderProjectNavItems(props: {
  group: AppShellProjectNavGroup;
  projectId: string;
  collapsed: boolean;
  onClick?: () => void;
}) {
  return getAppShellProjectNavItems(props.group).map((item) => {
    const Icon = item.icon;
    return (
      <SidebarLink
        key={item.id}
        collapsed={props.collapsed}
        icon={<Icon size={18} />}
        label={item.label}
        ariaLabel={item.ariaLabel}
        to={item.to(props.projectId)}
        onClick={props.onClick}
      />
    );
  });
}

const PERSISTENT_OUTLET_CACHE_MAX_ENTRIES = 3;
const PERSISTENT_OUTLET_CACHE_WHITELIST: RegExp[] = [/^\/projects\/[^/]+\/writing$/];

function isPersistentOutletCacheable(pathname: string): boolean {
  return PERSISTENT_OUTLET_CACHE_WHITELIST.some((pattern) => pattern.test(pathname));
}

type PersistentOutletCacheState = {
  elementsByKey: Map<string, React.ReactNode>;
  lruKeys: string[];
};

function PersistentOutlet(props: { activeKey: string }) {
  const outlet = useOutlet();
  const activeIsCacheable = isPersistentOutletCacheable(props.activeKey);
  const [cacheState, setCacheState] = useState<PersistentOutletCacheState>(() => ({
    elementsByKey: activeIsCacheable ? new Map([[props.activeKey, outlet]]) : new Map(),
    lruKeys: activeIsCacheable ? [props.activeKey] : [],
  }));

  const cacheStateWithActive = useMemo(() => {
    if (!activeIsCacheable) return cacheState;

    let nextElementsByKey = cacheState.elementsByKey;
    let nextLruKeys = cacheState.lruKeys;

    if (!nextElementsByKey.has(props.activeKey)) {
      nextElementsByKey = new Map(nextElementsByKey);
      nextElementsByKey.set(props.activeKey, outlet);
    }

    if (nextLruKeys[nextLruKeys.length - 1] !== props.activeKey) {
      nextLruKeys = nextLruKeys.filter((key) => key !== props.activeKey);
      nextLruKeys.push(props.activeKey);
    }

    while (nextLruKeys.length > PERSISTENT_OUTLET_CACHE_MAX_ENTRIES) {
      const evictedKey = nextLruKeys[0];
      nextLruKeys = nextLruKeys.slice(1);
      if (nextElementsByKey.has(evictedKey)) {
        nextElementsByKey = new Map(nextElementsByKey);
        nextElementsByKey.delete(evictedKey);
      }
    }

    if (nextElementsByKey === cacheState.elementsByKey && nextLruKeys === cacheState.lruKeys) return cacheState;
    return { elementsByKey: nextElementsByKey, lruKeys: nextLruKeys };
  }, [activeIsCacheable, cacheState, outlet, props.activeKey]);

  useEffect(() => {
    if (cacheStateWithActive === cacheState) return;
    const id = window.setTimeout(() => setCacheState(cacheStateWithActive), 0);
    return () => window.clearTimeout(id);
  }, [cacheState, cacheStateWithActive]);

  return (
    <>
      {activeIsCacheable ? null : (
        <div key={props.activeKey}>
          <PersistentOutletProvider outletKey={props.activeKey} activeKey={props.activeKey}>
            {outlet}
          </PersistentOutletProvider>
        </div>
      )}
      {Array.from(cacheStateWithActive.elementsByKey.entries()).map(([key, element]) => (
        <div key={key} style={{ display: key === props.activeKey ? "block" : "none" }}>
          <PersistentOutletProvider outletKey={key} activeKey={props.activeKey}>
            {element}
          </PersistentOutletProvider>
        </div>
      ))}
    </>
  );
}

type ContentContainerProps = {
  children: ReactNode;
  className?: string;
};

export function PaperContent(props: ContentContainerProps) {
  const reduceMotion = useReducedMotion();
  return (
    <motion.div
      className={clsx("mx-auto w-full max-w-4xl", props.className)}
      variants={fadeUpVariants}
      initial="initial"
      animate="animate"
      exit="exit"
      transition={reduceMotion ? { duration: 0.01 } : transition.page}
    >
      {props.children}
    </motion.div>
  );
}

export function ToolContent(props: ContentContainerProps) {
  const reduceMotion = useReducedMotion();
  return (
    <motion.div
      className={clsx("mx-auto w-full max-w-screen-xl", props.className)}
      variants={fadeUpVariants}
      initial="initial"
      animate="animate"
      exit="exit"
      transition={reduceMotion ? { duration: 0.01 } : transition.page}
    >
      {props.children}
    </motion.div>
  );
}

export function AppShell() {
  const auth = useAuth();
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = useSidebarCollapsed();
  const [mobileNavOpenForPath, setMobileNavOpenForPath] = useState<string | null>(null);
  const { projectId } = useParams();
  const location = useLocation();
  const reduceMotion = useReducedMotion();

  const pathname = location.pathname;
  const routeMeta = useMemo(() => resolveRouteMeta(pathname), [pathname]);
  const title = routeMeta.title;
  const mainMaxWidth =
    routeMeta.layout === "home" ? "max-w-5xl" : routeMeta.layout === "paper" ? "max-w-4xl" : "max-w-screen-xl";
  const sessionExpireAtText = auth.session?.expireAt ? new Date(auth.session.expireAt * 1000).toLocaleString() : null;
  const mobileNavOpen = mobileNavOpenForPath === pathname;

  const CollapseIcon = collapsed ? PanelLeftOpen : PanelLeftClose;
  const collapseLabel = collapsed ? "展开侧边栏" : "收起侧边栏";

  const openMobileNav = () => setMobileNavOpenForPath(pathname);
  const closeMobileNav = () => setMobileNavOpenForPath(null);

  return (
    <div className="min-h-screen bg-canvas text-ink">
      <div className="flex">
        <AnimatePresence>
          {mobileNavOpen ? (
            <motion.div
              className="fixed inset-0 z-50 flex bg-black/30 lg:hidden"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={reduceMotion ? { duration: 0.01 } : transition.base}
              onClick={(e) => {
                if (e.target === e.currentTarget) closeMobileNav();
              }}
              role="dialog"
              aria-modal="true"
              aria-label={UI_COPY.nav.navMenu}
            >
              <motion.aside
                className="h-full w-[280px] shrink-0 overflow-x-hidden border-r border-border bg-surface p-4 shadow-sm"
                initial={{ x: -12, opacity: 0 }}
                animate={{ x: 0, opacity: 1 }}
                exit={{ x: -12, opacity: 0 }}
                transition={reduceMotion ? { duration: 0.01 } : transition.base}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="font-content text-lg">{UI_COPY.brand.appName}</div>
                  <button
                    className="btn btn-secondary btn-icon"
                    onClick={closeMobileNav}
                    aria-label={UI_COPY.nav.closeNav}
                    title={UI_COPY.nav.closeNav}
                    type="button"
                  >
                    <PanelLeftClose size={18} />
                  </button>
                </div>

                <div className="mt-4">
                  <ProjectSwitcher />
                </div>

                <LayoutGroup id="atelier-sidebar-mobile">
                  <nav className="mt-4 flex flex-col gap-1">
                    <SidebarLink
                      collapsed={false}
                      icon={<LayoutDashboard size={18} />}
                      label={UI_COPY.nav.home}
                      ariaLabel="首页 (nav_home)"
                      to="/"
                      onClick={closeMobileNav}
                    />
                    <div className="my-2 h-px bg-border" />
                    {projectId ? (
                      <>
                        {APP_SHELL_PRIMARY_PROJECT_NAV_GROUPS.map((group, index) => (
                          <div key={group}>
                            <ProjectNavGroupTitle
                              collapsed={false}
                              className={index === 0 ? undefined : "mt-2"}
                              label={APP_SHELL_PROJECT_NAV_GROUP_TITLES[group]}
                            />
                            {renderProjectNavItems({
                              group,
                              projectId,
                              collapsed: false,
                              onClick: closeMobileNav,
                            })}
                          </div>
                        ))}

                      </>
                    ) : (
                      <div className="rounded-atelier border border-border bg-canvas p-3 text-xs text-subtext">
                        {UI_COPY.nav.chooseProjectHint}
                      </div>
                    )}
                    <div className="my-2 h-px bg-border" />
                    <div className="px-3 pt-2 text-[11px] font-medium text-subtext">{UI_COPY.nav.groupAdmin}</div>
                    <SidebarLink
                      collapsed={false}
                      icon={<UserCog size={18} />}
                      label={UI_COPY.nav.adminUsers}
                      ariaLabel="用户管理 (nav_admin_users)"
                      to="/admin/users"
                      onClick={closeMobileNav}
                    />
                  </nav>
                </LayoutGroup>
              </motion.aside>
            </motion.div>
          ) : null}
        </AnimatePresence>

        <aside
          className={clsx(
            "hidden min-h-screen shrink-0 overflow-x-hidden border-r border-border bg-surface motion-safe:transition-[width] motion-safe:duration-atelier motion-safe:ease-atelier lg:block",
            collapsed ? "w-14 p-2" : "w-[260px] p-4",
          )}
        >
          <div className={clsx("flex gap-2", collapsed ? "flex-col items-center" : "items-center justify-between")}>
            {collapsed ? null : <div className="font-content text-lg">{UI_COPY.brand.appName}</div>}
            <div className={clsx("flex gap-2", collapsed ? "flex-col items-center" : "items-center")}>
              <ThemeToggle />
              <button
                className="btn btn-secondary btn-icon"
                onClick={() => setCollapsed(!collapsed)}
                aria-label={collapseLabel}
                title={collapseLabel}
                type="button"
              >
                <CollapseIcon size={18} />
              </button>
            </div>
          </div>

          <div className={clsx("mt-4", collapsed && "hidden")}>
            <ProjectSwitcher />
          </div>

          <LayoutGroup id="atelier-sidebar-desktop">
            <nav className="mt-4 flex flex-col gap-1">
              <SidebarLink
                collapsed={collapsed}
                icon={<LayoutDashboard size={18} />}
                label={UI_COPY.nav.home}
                ariaLabel="首页 (nav_home)"
                to="/"
              />
              <div className="my-2 h-px bg-border" />
              {projectId ? (
                <>
                  {APP_SHELL_PRIMARY_PROJECT_NAV_GROUPS.map((group, index) => (
                    <div key={group}>
                      <ProjectNavGroupTitle
                        collapsed={collapsed}
                        className={index === 0 ? undefined : "mt-2"}
                        label={APP_SHELL_PROJECT_NAV_GROUP_TITLES[group]}
                      />
                      {renderProjectNavItems({
                        group,
                        projectId,
                        collapsed,
                      })}
                    </div>
                  ))}

                </>
              ) : (
                <div
                  className={clsx(
                    "rounded-atelier border border-border bg-canvas p-3 text-xs text-subtext",
                    collapsed && "hidden",
                  )}
                >
                  {UI_COPY.nav.chooseProjectHint}
                </div>
              )}
              <div className="my-2 h-px bg-border" />
              {collapsed ? null : (
                <div className="px-3 pt-2 text-[11px] font-medium text-subtext">{UI_COPY.nav.groupAdmin}</div>
              )}
              <SidebarLink
                collapsed={collapsed}
                icon={<UserCog size={18} />}
                label={UI_COPY.nav.adminUsers}
                ariaLabel="用户管理 (nav_admin_users)"
                to="/admin/users"
              />
            </nav>
          </LayoutGroup>
        </aside>

        <main className="flex-1">
          <header className="border-b border-border bg-canvas">
            <div className={clsx("mx-auto px-4 py-3 sm:px-6 sm:py-5 lg:px-8", mainMaxWidth)}>
              <div className="flex items-center justify-between gap-2 sm:gap-4">
                <div className="flex min-w-0 items-center gap-3">
                  <button
                    className="btn btn-secondary btn-icon lg:hidden"
                    onClick={openMobileNav}
                    aria-label={UI_COPY.nav.openNav}
                    title={UI_COPY.nav.openNav}
                    type="button"
                  >
                    <PanelLeftOpen size={18} />
                  </button>
                  <h1 className="min-w-0 truncate font-content text-lg sm:text-2xl md:text-3xl">{title}</h1>
                </div>
                <div className="flex items-center gap-2">
                  <div className="hidden sm:block text-right text-xs text-subtext">
                    <div className="truncate">
                      {auth.status === "authenticated"
                        ? `${auth.user?.displayName ?? auth.user?.id ?? "user"} (${auth.user?.id ?? "unknown"})`
                        : UI_COPY.auth.devFallbackTag}
                    </div>
                    {auth.status === "authenticated" && sessionExpireAtText ? (
                      <div className="truncate">
                        {UI_COPY.auth.sessionExpireAtPrefix}
                        {sessionExpireAtText}
                      </div>
                    ) : null}
                  </div>

                  {auth.status === "authenticated" ? (
                    <span className="inline-flex h-7 w-7 items-center justify-center rounded-full bg-accent/15 text-[11px] font-medium text-accent sm:hidden">
                      {(auth.user?.displayName ?? auth.user?.id ?? "U").slice(0, 2).toUpperCase()}
                    </span>
                  ) : null}

                  {auth.status === "authenticated" ? (
                    <button
                      className="btn btn-secondary"
                      onClick={async () => {
                        await auth.logout();
                        navigate("/login", { replace: true });
                      }}
                      type="button"
                    >
                      {UI_COPY.auth.logout}
                    </button>
                  ) : (
                    <NavLink className="btn btn-secondary" to="/login">
                      {UI_COPY.auth.login}
                    </NavLink>
                  )}

                  <a
                    className="btn btn-secondary"
                    href="https://linux.do"
                    rel="noreferrer"
                    target="_blank"
                  >
                    {UI_COPY.auth.linuxdoFriendLink}
                  </a>

                  <div className="hidden">
                    <ThemeToggle />
                  </div>
                </div>
              </div>
            </div>
          </header>
          <div className={clsx("mx-auto px-4 py-6 sm:px-6 sm:py-8 lg:px-8", mainMaxWidth)}>
            <PersistentOutlet activeKey={pathname} />
          </div>
        </main>
      </div>
    </div>
  );
}
