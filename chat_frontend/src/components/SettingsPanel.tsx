/**
 * SettingsPanel — full-screen settings overlay for admin users.
 * Replaces the <main> chat area when open.
 */
import { useState } from "react";
import {
  LayoutDashboardIcon,
  BrainIcon,
  MessageSquareIcon,
  GitBranchIcon,
  ServerIcon,
  FolderIcon,
  UsersIcon,
  LinkIcon,
  XIcon,
} from "lucide-react";
import { SettingsDashboard } from "@/components/settings/SettingsDashboard";
import { SettingsLLM } from "@/components/settings/SettingsLLM";
import { SettingsChat } from "@/components/settings/SettingsChat";
import { SettingsGit } from "@/components/settings/SettingsGit";
import { SettingsSystem } from "@/components/settings/SettingsSystem";
import { SettingsProjects } from "@/components/settings/SettingsProjects";
import { SettingsUsers } from "@/components/settings/SettingsUsers";
import { SettingsChannels } from "@/components/settings/SettingsChannels";
import { SettingsHost } from "@/components/settings/SettingsHost";

export type SettingsPage =
  | "dashboard"
  | "llm"
  | "chat"
  | "git"
  | "system"
  | "host"
  | "projects"
  | "users"
  | "channels";

interface SettingsPanelProps {
  onClose: () => void;
}

const PAGE_LABELS: Record<SettingsPage, string> = {
  dashboard: "Overview",
  llm: "LLM / AI",
  chat: "Chat Platform",
  git: "Git Provider",
  system: "System",
  host: "Host Mode",
  projects: "Projects",
  users: "Users",
  channels: "Channels",
};

interface NavItem {
  page: SettingsPage;
  icon: React.ReactNode;
  label: string;
}

const NAV: Array<{ section?: string; items: NavItem[] }> = [
  {
    items: [
      { page: "dashboard", icon: <LayoutDashboardIcon className="size-4" />, label: "Overview" },
    ],
  },
  {
    section: "Providers",
    items: [
      { page: "llm", icon: <BrainIcon className="size-4" />, label: "LLM / AI" },
      { page: "chat", icon: <MessageSquareIcon className="size-4" />, label: "Chat Platform" },
      { page: "git", icon: <GitBranchIcon className="size-4" />, label: "Git Provider" },
      { page: "system", icon: <ServerIcon className="size-4" />, label: "System" },
      { page: "host", icon: <ServerIcon className="size-4" />, label: "Host Mode" },
    ],
  },
  {
    section: "Management",
    items: [
      { page: "projects", icon: <FolderIcon className="size-4" />, label: "Projects" },
      { page: "users", icon: <UsersIcon className="size-4" />, label: "Users" },
      { page: "channels", icon: <LinkIcon className="size-4" />, label: "Channels" },
    ],
  },
];

export function SettingsPanel({ onClose }: SettingsPanelProps) {
  const [page, setPage] = useState<SettingsPage>("dashboard");

  function renderPage() {
    switch (page) {
      case "dashboard": return <SettingsDashboard onNavigate={setPage} />;
      case "llm":       return <SettingsLLM />;
      case "chat":      return <SettingsChat />;
      case "git":       return <SettingsGit />;
      case "system":    return <SettingsSystem />;
      case "host":      return <SettingsHost />;
      case "projects":  return <SettingsProjects />;
      case "users":     return <SettingsUsers />;
      case "channels":  return <SettingsChannels />;
    }
  }

  return (
    <div className="flex-1 flex overflow-hidden">
      {/* Left nav */}
      <nav className="w-[220px] shrink-0 flex flex-col border-r border-border bg-card overflow-y-auto">
        <div className="px-4 pt-5 pb-3">
          <p className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">Settings</p>
        </div>
        <div className="flex-1 px-2 pb-4">
          {NAV.map((group, gi) => (
            <div key={gi} className="mb-3">
              {group.section && (
                <p className="px-3 py-1 text-[11px] font-medium uppercase tracking-wider text-muted-foreground/60">
                  {group.section}
                </p>
              )}
              {group.items.map((item) => (
                <button
                  key={item.page}
                  onClick={() => setPage(item.page)}
                  className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-colors text-left ${
                    page === item.page
                      ? "bg-accent text-foreground font-medium"
                      : "text-muted-foreground hover:text-foreground hover:bg-accent/50"
                  }`}
                >
                  {item.icon}
                  {item.label}
                </button>
              ))}
            </div>
          ))}
        </div>
      </nav>

      {/* Content area */}
      <div className="flex-1 flex flex-col overflow-hidden bg-background">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-border shrink-0">
          <h1 className="text-base font-semibold text-foreground">{PAGE_LABELS[page]}</h1>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
            title="Back to chat"
          >
            <XIcon className="size-4" />
          </button>
        </div>
        {/* Page content */}
        <div className="flex-1 overflow-y-auto p-6">
          {renderPage()}
        </div>
      </div>
    </div>
  );
}
