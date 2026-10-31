import { Suspense, useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import * as Dropdown from "@radix-ui/react-dropdown-menu";
import {
  Activity,
  ArrowUpRight,
  Bell,
  Bot,
  Check,
  ChevronDown,
  CircleDot,
  CreditCard,
  Download,
  FolderGit2,
  GitPullRequest,
  House,
  Link2,
  LogOut,
  Menu,
  Monitor,
  Moon,
  Search,
  Settings2,
  ShieldCheck,
  Sun,
  X,
} from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useAuth } from "../../app/auth";
import { useLocale } from "../../app/locale";
import { useTheme } from "../../app/theme";
import { api } from "../../api/client";
import { Dialog } from "../ui/Dialog";
import { LoadingState } from "../common/states";
import { BrandMark } from "../common/BrandMark";
import { UserAvatar } from "../common/UserAvatar";

export default function AppShell() {
  const auth = useAuth();
  const { t, language, setLanguage } = useLocale();
  const { theme, setTheme } = useTheme();
  const client = useQueryClient();
  const location = useLocation();
  const navigate = useNavigate();
  const [mobile, setMobile] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [loggingOut, setLoggingOut] = useState(false);
  const navigation = [
    { to: "/", label: t("概览", "Overview"), icon: House },
    ...(auth.permissions.admin
      ? [
          {
            to: "/repositories",
            label: t("仓库", "Repositories"),
            icon: FolderGit2,
          },
        ]
      : []),
    { to: "/reviews", label: t("代码审查", "Reviews"), icon: GitPullRequest },
    { to: "/issues", label: "Issues", icon: CircleDot },
    { to: "/agent", label: "Agent", icon: Bot },
    { to: "/activity", label: t("运行活动", "Activity"), icon: Activity },
    {
      to: "/github-app",
      label: t("GitHub 授权", "GitHub access"),
      icon: Link2,
    },
    {
      to: "/community",
      label: t("Star 互助", "Star community"),
      icon: CircleDot,
    },
    ...(auth.permissions.super_admin
      ? [
          {
            to: "/memory",
            label: t("仓库记忆", "Repository memory"),
            icon: FolderGit2,
          },
        ]
      : []),
  ];
  const utility = [
    {
      to: "/billing",
      label: t("用量与账单", "Usage & billing"),
      icon: CreditCard,
    },
    { to: "/settings/personal", label: t("设置", "Settings"), icon: Settings2 },
    ...(auth.permissions.admin
      ? [
          {
            to: "/admin",
            label: t("管理", "Administration"),
            icon: ShieldCheck,
          },
        ]
      : []),
  ];
  useEffect(() => {
    setMobile(false);
  }, [location.pathname]);
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const message = params.get("_toast");
    if (!message) return;
    if (params.get("_toast_type") === "error") toast.error(message);
    else toast.success(message);
    params.delete("_toast");
    params.delete("_toast_type");
    navigate(
      {
        pathname: location.pathname,
        search: params.toString(),
        hash: location.hash,
      },
      { replace: true },
    );
  }, [location.search, location.pathname, location.hash, navigate]);
  useEffect(() => {
    const handle = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobile(false);
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setSearchOpen((value) => !value);
      }
    };
    window.addEventListener("keydown", handle);
    return () => window.removeEventListener("keydown", handle);
  }, []);
  async function logout() {
    setLoggingOut(true);
    try {
      await api(auth.links.logout, { method: "POST" });
      client.clear();
      window.location.assign(auth.links.login);
    } catch {
      toast.error(
        t("退出失败，请重试。", "Sign out failed. Please try again."),
      );
      setLoggingOut(false);
    }
  }
  const renderLinks = (items: typeof navigation) =>
    items.map(({ to, label, icon: Icon }) => (
      <NavLink
        key={to}
        to={to}
        end={to === "/"}
        className={({ isActive }) =>
          `nav-link${isActive || (to.startsWith("/settings") && location.pathname.startsWith("/settings")) ? " active" : ""}`
        }
      >
        <Icon size={20} strokeWidth={1.8} />
        <span>{label}</span>
      </NavLink>
    ));
  return (
    <div className="app-shell">
      <a href="#main-content" className="skip-link">
        {t("跳至内容", "Skip to content")}
      </a>
      {mobile && (
        <button
          className="sidebar-scrim"
          aria-label={t("关闭导航", "Close navigation")}
          onClick={() => setMobile(false)}
        />
      )}
      <aside
        className={`sidebar${mobile ? " open" : ""}`}
        aria-label={t("主导航", "Main navigation")}
      >
        <NavLink to="/" className="brand">
          <span className="brand-mark">
            <BrandMark />
          </span>
          <span>Sakura AI</span>
        </NavLink>
        <nav className="primary-nav">{renderLinks(navigation)}</nav>
        <div className="sidebar-bottom">
          <nav>{renderLinks(utility)}</nav>
        </div>
      </aside>
      <div className="app-body">
        <header className="topbar">
          <button
            className="icon-button mobile-toggle"
            onClick={() => setMobile(!mobile)}
            aria-label={t("打开导航", "Open navigation")}
            aria-expanded={mobile}
          >
            {mobile ? <X size={21} /> : <Menu size={21} />}
          </button>
          <button
            className="global-search"
            aria-label={t("搜索页面或设置", "Search pages or settings")}
            onClick={() => setSearchOpen(true)}
          >
            <Search size={18} />
            <span>{t("搜索页面或设置", "Search pages or settings")}</span>
            <kbd>Ctrl K</kbd>
          </button>
          <div className="topbar-actions">
            {auth.permissions.super_admin && (
              <nav
                className="topbar-version-links"
                aria-label={t("版本更新", "Updates")}
              >
                {auth.permissions.super_admin && (
                  <NavLink
                    to="/admin/version"
                    title={t("版本更新", "Updates")}
                    aria-label={t("版本更新", "Updates")}
                  >
                    <Download size={17} />
                    <span className="topbar-update-label">
                      {t("版本更新", "Updates")}
                    </span>
                  </NavLink>
                )}
              </nav>
            )}
            <NavLink
              className="icon-button"
              to="/notifications"
              aria-label={t("通知中心", "Notification center")}
            >
              <Bell size={19} />
            </NavLink>
            <Dropdown.Root>
              <Dropdown.Trigger className="user-trigger">
                <UserAvatar
                  url={auth.user.avatar_url}
                  username={auth.user.username}
                />
                <span className="user-name">
                  {auth.user.display_name || auth.user.username}
                </span>
                <ChevronDown size={15} />
              </Dropdown.Trigger>
              <Dropdown.Portal>
                <Dropdown.Content
                  className="dropdown-menu"
                  align="end"
                  sideOffset={12}
                >
                  <Dropdown.Label className="dropdown-label">
                    {auth.user.username}
                    <small>
                      {auth.user.role === "super_admin"
                        ? t("超级管理员", "Super administrator")
                        : auth.user.role === "admin"
                          ? t("管理员", "Administrator")
                          : t("成员", "Member")}
                    </small>
                  </Dropdown.Label>
                  <Dropdown.Separator className="dropdown-separator" />
                  {(
                    [
                      { value: "light", label: t("浅色", "Light"), Icon: Sun },
                      { value: "dark", label: t("深色", "Dark"), Icon: Moon },
                      {
                        value: "system",
                        label: t("跟随系统", "System"),
                        Icon: Monitor,
                      },
                    ] as const
                  ).map(({ value, label, Icon }) => (
                    <Dropdown.Item
                      className="dropdown-item"
                      key={value}
                      onSelect={() => setTheme(value)}
                    >
                      <Icon size={16} />
                      {label}
                      {theme === value && (
                        <Check size={15} className="trailing-icon" />
                      )}
                    </Dropdown.Item>
                  ))}
                  <Dropdown.Separator className="dropdown-separator" />
                  <Dropdown.Item
                    className="dropdown-item"
                    onSelect={() =>
                      setLanguage(language === "en" ? "zh-CN" : "en")
                    }
                  >
                    {language === "en" ? "简体中文" : "English"}
                  </Dropdown.Item>
                  <Dropdown.Separator className="dropdown-separator" />
                  <Dropdown.Item
                    className="dropdown-item danger-text"
                    disabled={loggingOut}
                    onSelect={logout}
                  >
                    <LogOut size={16} />
                    {t("退出登录", "Sign out")}
                  </Dropdown.Item>
                </Dropdown.Content>
              </Dropdown.Portal>
            </Dropdown.Root>
          </div>
        </header>
        <main id="main-content" className="main-content" tabIndex={-1}>
          <Suspense fallback={<LoadingState />}>
            <Outlet />
          </Suspense>
        </main>
      </div>
      <Dialog
        open={searchOpen}
        onOpenChange={setSearchOpen}
        title={t("快速查找", "Quick find")}
      >
        <div className="search-field">
          <Search size={19} />
          <input
            autoFocus
            aria-label={t("搜索页面或设置", "Search pages or settings")}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t(
              "输入页面名称或配置关键词…",
              "Page name or setting keyword…",
            )}
          />
        </div>
        <div className="command-results">
          {[...navigation, ...utility]
            .filter((item) =>
              item.label.toLowerCase().includes(search.toLowerCase()),
            )
            .map(({ to, label, icon: Icon }) => (
              <button
                key={to}
                onClick={() => {
                  navigate(to);
                  setSearchOpen(false);
                  setSearch("");
                }}
              >
                <Icon size={19} />
                {label}
                <ArrowUpRight size={16} />
              </button>
            ))}
          {search.trim() && (
            <button
              onClick={() => {
                navigate(
                  `/settings/personal?q=${encodeURIComponent(search.trim())}`,
                );
                setSearchOpen(false);
                setSearch("");
              }}
            >
              <Search size={19} />
              <span>
                {t("在所有设置中搜索", "Search all settings")}: {search}
              </span>
              <ArrowUpRight size={16} />
            </button>
          )}
        </div>
      </Dialog>
    </div>
  );
}
