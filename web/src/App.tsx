import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useLiveState } from "./hooks/useLiveState";
import AccountsPage from "./pages/AccountsPage";
import AssetsPage from "./pages/AssetsPage";
import CalendarPage from "./pages/CalendarPage";
import PresetsPage from "./pages/PresetsPage";
import ProjectPage from "./pages/ProjectPage";
import ProjectsPage from "./pages/ProjectsPage";
import QueuePage from "./pages/QueuePage";
import SystemPage from "./pages/SystemPage";
import WorkersPage from "./pages/WorkersPage";

// Три разных занятия, и мешать их в одном списке незачем: с работой имеешь дело
// каждый день, настройки трогаешь изредка, служебное — когда что-то сломалось.
const NAV: { title: string; items: { to: string; label: string; counter?: "jobs" }[] }[] = [
  {
    title: "Работа",
    items: [
      { to: "/projects", label: "Проекты" },
      { to: "/calendar", label: "Календарь" },
    ],
  },
  {
    title: "Настройка",
    items: [
      { to: "/presets", label: "Пресеты" },
      { to: "/assets", label: "Материалы" },
      { to: "/accounts", label: "Аккаунты" },
    ],
  },
  {
    title: "Служебное",
    items: [
      { to: "/queue", label: "Очередь", counter: "jobs" },
      { to: "/workers", label: "Воркеры" },
      { to: "/system", label: "Диагностика" },
    ],
  },
];

export default function App() {
  const { state, connected } = useLiveState();
  const activeJobs = state.jobs.filter(
    (job) => job.status === "running" || job.status === "queued",
  ).length;
  const waiting = state.segments.filter((segment) => segment.status === "candidate").length;

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <b>Shorts</b>
          <span>монтажная</span>
        </div>

        {NAV.map((group) => (
          <div key={group.title} className="nav-group">
            <span className="nav-title">{group.title}</span>
            {group.items.map((item) => (
              <NavLink key={item.to} to={item.to} className="nav-link">
                <span>{item.label}</span>
                {item.counter === "jobs" && activeJobs > 0 && (
                  <span className="badge">{activeJobs}</span>
                )}
              </NavLink>
            ))}
          </div>
        ))}

        <div className="rail-foot">
          {waiting > 0 && (
            <span className="link-state">
              <i style={{ background: "var(--amber)" }} />
              Ждут ревью: {waiting}
            </span>
          )}
          <span className={`link-state ${connected ? "live" : "down"}`}>
            <i />
            {connected ? "Связь есть" : "Нет связи"}
          </span>
        </div>
      </aside>

      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/projects" replace />} />
          <Route path="/projects" element={<ProjectsPage live={state} />} />
          <Route path="/projects/:id" element={<ProjectPage live={state} />} />
          <Route path="/queue" element={<QueuePage live={state} />} />
          <Route path="/calendar" element={<CalendarPage />} />
          <Route path="/presets" element={<PresetsPage />} />
          <Route path="/assets" element={<AssetsPage />} />
          <Route path="/accounts" element={<AccountsPage />} />
          <Route path="/workers" element={<WorkersPage />} />
          <Route path="/system" element={<SystemPage />} />
          <Route
            path="*"
            element={
              <div className="empty">
                Такой страницы нет. <a href="/projects">Вернуться к проектам</a>
              </div>
            }
          />
        </Routes>
      </main>
    </div>
  );
}
