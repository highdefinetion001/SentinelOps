
import { useCallback, useEffect, useState } from "react";
import { getFleet, getHealth, resetServer, runFleetScan, simulateDrift } from "../services/api";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Clock3,
  Command,
  Fingerprint,
  LayoutDashboard,
  Menu,
  Moon,
  RefreshCw,
  Search,
  Server,
  Settings,
  Shield,
  ShieldAlert,
  ShieldCheck,
  Sun,
  X,
  Layers3,
} from "lucide-react";
import "../App.css";

function evidenceValue(evidence) {
  if (!evidence || evidence.state === "missing") return "(missing)";
  if (evidence.state === "null") return "null";
  return typeof evidence.value === "string"
    ? evidence.value
    : JSON.stringify(evidence.value);
}

function mapDetectionToServers(scan, fleet) {
  const states = new Map(fleet.servers.map((entry) => [entry.server_id, entry]));
  const scannedAt = new Date(scan.scanned_at);

  return scan.servers.map((result) => {
    const state = states.get(result.server_id)?.state;
    const findings = (result.findings || []).map((finding) => ({
      field: finding.evidence.field_path,
      expected: evidenceValue(finding.evidence.expected),
      actual: evidenceValue(finding.evidence.actual),
      severity: finding.assessment.severity,
    }));
    const findingPaths = new Set(findings.map((finding) => finding.field));
    for (const violation of result.policy_violations || []) {
      if (!findingPaths.has(violation.field_path)) {
        findings.push({
          field: violation.field_path,
          expected: `Policy: ${violation.policy_id}`,
          actual: evidenceValue(violation.observed),
          severity: violation.severity,
        });
      }
    }

    const status =
      result.outcome === "clean"
        ? result.policy_violations?.length
          ? "Policy violation"
          : "Stable"
        : result.outcome === "drifted"
          ? "Drift detected"
          : "Unreachable";
    const service = result.service_group || "Unknown service";
    const runtime = state?.environment?.runtime_version;

    return {
      id: result.server_id,
      name: result.server_id,
      configVersion: state?.config_version || "Config version unavailable",
      ip: service,
      os: runtime ? `Runtime ${runtime}` : "Not reported",
      environment: service,
      status,
      drift: findings.length,
      fingerprint: result.observed_fingerprint || "Unavailable",
      lastScan: scannedAt.toLocaleString(),
      cpu: null,
      memory: null,
      details: findings,
      peerOutlier: result.peer_comparison?.is_outlier || false,
      collectionError: result.collection_error?.message || result.assessment_error || null,
    };
  });
}

const navigation = [
  { label: "Overview", icon: LayoutDashboard },
  { label: "Server Inventory", icon: Server },
  { label: "Drift Detection", icon: Fingerprint },
  { label: "Peer Analysis", icon: Layers3 },
];

function StatusBadge({ status }) {
  const className =
    status === "Stable"
      ? "status-stable"
      : status === "Unreachable"
        ? "status-unreachable"
        : status === "Scanning"
          ? "status-scanning"
          : status === "Policy violation"
            ? "status-policy"
            : "status-drift";

  return (
    <span className={`status-badge ${className}`}>
      <span className="status-dot" />
      {status}
    </span>
  );
}

function SeverityBadge({ severity }) {
  return (
    <span className={`severity-badge severity-${severity.toLowerCase()}`}>
      {severity}
    </span>
  );
}

function MetricCard({ icon: Icon, label, value, change, tone }) {
  return (
    <div className="metric-card">
      <div className={`metric-icon ${tone}`}>
        <Icon size={19} />
      </div>
      <div className="metric-content">
        <p className="metric-label">{label}</p>
        <div className="metric-bottom">
          <h2>{value}</h2>
          <span className="metric-change">{change}</span>
        </div>
      </div>
    </div>
  );
}

function App() {
  const [theme, setTheme] = useState("dark");
  const [activePage, setActivePage] = useState("Overview");
  const [servers, setServers] = useState([]);
  const [apiError, setApiError] = useState("");
  const [apiHealth, setApiHealth] = useState(null);
  const [selectedServer, setSelectedServer] = useState(null);
  const [searchTerm, setSearchTerm] = useState("");
  const [statusFilter, setStatusFilter] = useState("All statuses");
  const [environmentFilter, setEnvironmentFilter] =
    useState("All environments");
  const [lastUpdated, setLastUpdated] = useState("Just now");
  const [isRefreshing, setIsRefreshing] = useState(true);
  const [demoAction, setDemoAction] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const toggleTheme = () => {
    setTheme((current) => (current === "dark" ? "light" : "dark"));
  };

  const refreshData = useCallback(async ({ showSpinner = true } = {}) => {
    if (showSpinner) setIsRefreshing(true);
    try {
      const [health, fleet, scan] = await Promise.all([
        getHealth(),
        getFleet(),
        runFleetScan(),
      ]);
      setApiHealth(health);
      const nextServers = mapDetectionToServers(scan, fleet);
      setServers(nextServers);
      setSelectedServer((current) =>
        current ? nextServers.find((server) => server.id === current.id) || null : null
      );
      setLastUpdated(new Date(scan.scanned_at).toLocaleTimeString());
      setApiError("");
    } catch (error) {
      setApiError(error.message || "Unable to load fleet data from the API.");
    } finally {
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void refreshData({ showSpinner: false });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [refreshData]);

  const totalServers = servers.length;
  const driftedServers = servers.filter(
    (server) => server.status === "Drift detected"
  ).length;
  const stableServers = servers.filter(
    (server) => server.status === "Stable"
  ).length;
  const policyViolationServers = servers.filter(
    (server) => server.status === "Policy violation"
  ).length;
  const unreachableServers = servers.filter(
    (server) => server.status === "Unreachable"
  ).length;
  const totalDrift = servers.reduce((sum, server) => sum + server.drift, 0);
  const peerOutliers = servers.filter((server) => server.peerOutlier);

  const filteredServers = servers.filter((server) => {
    const search = searchTerm.toLowerCase();

    const matchesSearch =
      server.name.toLowerCase().includes(search) ||
      server.id.toLowerCase().includes(search) ||
      server.ip.toLowerCase().includes(search) ||
      server.os.toLowerCase().includes(search);

    const matchesStatus =
      statusFilter === "All statuses" || server.status === statusFilter;

    const matchesEnvironment =
      environmentFilter === "All environments" ||
      server.environment === environmentFilter;

    return matchesSearch && matchesStatus && matchesEnvironment;
  });

  const navigateTo = (page) => {
    setActivePage(page);
    setSidebarOpen(false);
    setSelectedServer(null);
  };

  const runServerScan = async () => {
    await refreshData();
  };

  const runDemoAction = async (serverId, action) => {
    setDemoAction(action);
    try {
      if (action === "simulate") await simulateDrift(serverId);
      else await resetServer(serverId);
      await refreshData();
    } catch (error) {
      setApiError(error.message || "The demo action could not be completed.");
    } finally {
      setDemoAction("");
    }
  };

  return (
    <div className={`app-shell theme-${theme}`}>
      <aside className={`sidebar ${sidebarOpen ? "sidebar-open" : ""}`}>
        <div className="brand">
          <div className="brand-mark">
            <Shield size={22} strokeWidth={2.4} />
          </div>
          <div>
            <div className="brand-name">SentinelOps</div>
            <div className="brand-subtitle">INFRASTRUCTURE INTELLIGENCE</div>
          </div>
          <button
            className="icon-button mobile-close"
            onClick={() => setSidebarOpen(false)}
            aria-label="Close navigation"
          >
            <X size={18} />
          </button>
        </div>

        <div className="workspace-label">WORKSPACE</div>
        <button className="workspace-switcher">
          <div className="workspace-avatar">S</div>
          <div className="workspace-info">
            <strong>Sentinel Workspace</strong>
            <span>Free workspace</span>
          </div>
          <ChevronDown size={15} />
        </button>

        <div className="nav-label">MONITORING</div>
        <nav className="sidebar-nav">
          {navigation.map(({ label, icon: Icon }) => (
            <button
              key={label}
              className={`nav-item ${activePage === label ? "active" : ""}`}
              onClick={() => navigateTo(label)}
            >
              <Icon size={18} />
              <span>{label}</span>
              {label === "Drift Detection" && driftedServers > 0 && (
                <span className="nav-count">{driftedServers}</span>
              )}
            </button>
          ))}
        </nav>

        <div className="nav-label system-label">SYSTEM</div>
        <nav className="sidebar-nav">
          <button
            className={`nav-item ${activePage === "Settings" ? "active" : ""}`}
            onClick={() => navigateTo("Settings")}
          >
            <Settings size={18} />
            <span>Settings</span>
          </button>
          <button
            className={`nav-item ${activePage === "Documentation" ? "active" : ""}`}
            onClick={() => navigateTo("Documentation")}
          >
            <CircleHelp size={18} />
            <span>Documentation</span>
            <ChevronRight className="nav-chevron" size={15} />
          </button>
        </nav>

        <div className="sidebar-spacer" />

        <div className="system-health">
          <div className="health-heading">
            <span className={`health-indicator ${apiHealth?.status === "ok" ? "" : "health-unavailable"}`} />
            <span>{apiHealth?.status === "ok" ? "API process healthy" : "API status unavailable"}</span>
          </div>
          <p>Dependency readiness is not reported by the health endpoint.</p>
          <div className="health-line">
            <span />
          </div>
          <div className="health-footer">
            <span>API process</span>
            <strong>{apiHealth?.status === "ok" ? "Healthy" : "Unknown"}</strong>
          </div>
        </div>

        <div className="sidebar-user">
          <div className="user-avatar">AD</div>
          <div className="user-info">
            <strong>Admin User</strong>
            <span>Administrator</span>
          </div>
          <button className="icon-button" title="User options">
            <ChevronDown size={16} />
          </button>
        </div>
      </aside>

      {sidebarOpen && (
        <button
          className="sidebar-overlay"
          onClick={() => setSidebarOpen(false)}
          aria-label="Close sidebar"
        />
      )}

      <main className="main-area">
        <header className="topbar">
          <div className="topbar-left">
            <button
              className="icon-button mobile-menu"
              onClick={() => setSidebarOpen(true)}
              aria-label="Open navigation"
            >
              <Menu size={20} />
            </button>
            <div className="breadcrumb">
              <span>SentinelOps</span>
              <ChevronRight size={15} />
              <strong>{activePage}</strong>
            </div>
          </div>

          <div className="topbar-actions">
            <div className="system-status">
              <span className={`health-indicator ${apiHealth?.status === "ok" ? "" : "health-unavailable"}`} />
              <span>{apiHealth?.status === "ok" ? "API process healthy" : "API status unavailable"}</span>
            </div>

            <button
              className="theme-toggle"
              onClick={toggleTheme}
              aria-label={`Switch to ${theme === "dark" ? "bright" : "dark"} mode`}
              title={`Switch to ${theme === "dark" ? "bright" : "dark"} mode`}
            >
              {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
              <span>{theme === "dark" ? "Bright mode" : "Dark mode"}</span>
            </button>

            <button
              className={`refresh-button ${isRefreshing ? "refreshing" : ""}`}
              onClick={refreshData}
              disabled={isRefreshing}
            >
              <RefreshCw size={16} />
              <span>{isRefreshing ? "Refreshing" : "Refresh"}</span>
            </button>
          </div>
        </header>

        <div className="page-content">
          {apiError && (
            <div className="panel api-error" role="alert">
              <strong>Unable to refresh fleet data</strong>
              <p>{apiError}</p>
              <button className="text-button" onClick={refreshData}>Retry</button>
            </div>
          )}
          {isRefreshing && totalServers === 0 && (
            <div className="panel" role="status">Loading fleet scan from the API…</div>
          )}
          {activePage === "Overview" && (
            <>
              <section className="page-heading">
                <div>
                  <div className="eyebrow">
                    <span className="eyebrow-dot" />
                    LIVE INFRASTRUCTURE MONITORING
                  </div>
                  <h1>Infrastructure Overview</h1>
                  <p>
                    Monitor server fingerprints, detect configuration drift,
                    and maintain infrastructure integrity.
                  </p>
                </div>
                <div className="updated-label">
                  <Clock3 size={15} />
                  Updated {lastUpdated}
                </div>
              </section>

              <section className="metrics-grid">
                <MetricCard
                  icon={Server}
                  label="Total servers"
                  value={totalServers}
                  change="In inventory"
                  tone="blue"
                />
                <MetricCard
                  icon={ShieldCheck}
                  label="Baseline matched"
                  value={stableServers}
                  change={totalServers ? `${Math.round((stableServers / totalServers) * 100)}% of fleet` : "No servers"}
                  tone="green"
                />
                <MetricCard
                  icon={ShieldAlert}
                  label="Policy violations"
                  value={policyViolationServers}
                  change="Independent of baseline drift"
                  tone="red"
                />
                <MetricCard
                  icon={ShieldAlert}
                  label="Drift detected"
                  value={driftedServers}
                  change="Requires review"
                  tone="red"
                />
                <MetricCard
                  icon={Activity}
                  label="Drift findings"
                  value={totalDrift}
                  change="Across all servers"
                  tone="purple"
                />
              </section>

              <section className="overview-grid">
                <div className="panel fleet-panel">
                  <div className="panel-heading">
                    <div>
                      <h2>Server fleet</h2>
                      <p>Current status of monitored infrastructure</p>
                    </div>
                    <button
                      className="text-button"
                      onClick={() => navigateTo("Server Inventory")}
                    >
                      View inventory <ChevronRight size={15} />
                    </button>
                  </div>

                  <div className="fleet-summary">
                    <div className="fleet-summary-item">
                      <span className="legend-dot stable-dot" />
                      <span>Stable</span>
                      <strong>{stableServers}</strong>
                    </div>
                    <div className="fleet-summary-item">
                      <span className="legend-dot drift-dot" />
                      <span>Drift detected</span>
                      <strong>{driftedServers}</strong>
                    </div>
                    <div className="fleet-summary-item">
                      <span className="legend-dot scanning-dot" />
                      <span>Unreachable</span>
                      <strong>{unreachableServers}</strong>
                    </div>
                  </div>

                  <div className="fleet-bar">
                    {servers.map((server) => (
                      <div
                        key={server.id}
                        className={`fleet-segment ${
                          server.status === "Stable"
                            ? "segment-stable"
                            : server.status === "Unreachable"
                              ? "segment-scanning"
                              : "segment-drift"
                        }`}
                        title={`${server.name}: ${server.status}`}
                      />
                    ))}
                  </div>

                  <div className="server-mini-list">
                    {servers.slice(0, 4).map((server) => (
                      <button
                        className="server-mini-row"
                        key={server.id}
                        onClick={() => setSelectedServer(server)}
                      >
                        <div className="server-mini-icon">
                          <Server size={17} />
                        </div>
                        <div className="server-mini-info">
                          <strong>{server.name}</strong>
                          <span>{server.configVersion}</span>
                        </div>
                        <StatusBadge status={server.status} />
                        <ChevronRight size={16} className="row-arrow" />
                      </button>
                    ))}
                  </div>
                </div>

                <div className="panel findings-panel">
                  <div className="panel-heading">
                    <div>
                      <h2>Recent findings</h2>
                      <p>Configuration differences requiring attention</p>
                    </div>
                    <div className="panel-heading-icon">
                      <AlertTriangle size={17} />
                    </div>
                  </div>

                  <div className="finding-list">
                    {servers
                      .filter((server) => server.drift > 0)
                      .map((server) => (
                        <button
                          className="finding-item"
                          key={server.id}
                          onClick={() => setSelectedServer(server)}
                        >
                          <div className="finding-icon">
                            <AlertTriangle size={17} />
                          </div>
                          <div className="finding-info">
                            <strong>{server.name}</strong>
                            <span>
                              {server.drift} configuration differences found
                            </span>
                            <small>{server.lastScan}</small>
                          </div>
                          <ChevronRight size={16} className="row-arrow" />
                        </button>
                      ))}
                    {driftedServers === 0 && (
                      <div className="empty-state">
                        <CheckCircle2 size={28} />
                        <strong>No drift findings</strong>
                        <span>All monitored servers match their baseline.</span>
                      </div>
                    )}
                  </div>

                  <button
                    className="panel-footer-link"
                    onClick={() => navigateTo("Drift Detection")}
                  >
                    Explore drift findings <ChevronRight size={15} />
                  </button>
                </div>
              </section>

              <section className="panel inventory-panel">
                <div className="panel-heading">
                  <div>
                    <h2>Server inventory</h2>
                    <p>A quick view of your monitored servers</p>
                  </div>
                  <button
                    className="text-button"
                    onClick={() => navigateTo("Server Inventory")}
                  >
                    View all servers <ChevronRight size={15} />
                  </button>
                </div>
                <ServerTable
                  servers={servers.slice(0, 5)}
                  onSelect={setSelectedServer}
                />
              </section>

              <section className="bottom-grid">
                <div className="panel fingerprint-panel">
                  <div className="panel-heading">
                    <div>
                      <h2>Fingerprint engine</h2>
                      <p>Infrastructure identity tracking</p>
                    </div>
                    <div className="panel-heading-icon purple-icon">
                      <Fingerprint size={18} />
                    </div>
                  </div>
                  <div className="engine-status">
                    <div className="engine-visual">
                      <Fingerprint size={30} />
                    </div>
                    <div>
                      <strong>{apiHealth?.status === "ok" ? "Fingerprint service connected" : "Fingerprint service unavailable"}</strong>
                      <p>SHA-256 fingerprints supplied by the backend</p>
                    </div>
                  </div>
                  <div className="engine-stats">
                    <div>
                      <span>Fingerprints tracked</span>
                      <strong>{totalServers}</strong>
                    </div>
                    <div>
                      <span>Last engine update</span>
                      <strong>{lastUpdated}</strong>
                    </div>
                  </div>
                </div>

                <div className="panel clusters-preview">
                  <div className="panel-heading">
                    <div>
                      <h2>Peer outliers</h2>
                      <p>Supplementary similarity signals from the API</p>
                    </div>
                    <button
                      className="text-button"
                      onClick={() => navigateTo("Peer Analysis")}
                    >
                      View all <ChevronRight size={15} />
                    </button>
                  </div>
                  {peerOutliers.length ? peerOutliers.slice(0, 2).map((server) => (
                    <div className="cluster-row" key={server.id}>
                      <div className="cluster-icon"><Layers3 size={17} /></div>
                      <div className="cluster-info">
                        <strong>{server.name}</strong>
                        <span>Peer similarity differs from fleet consensus</span>
                      </div>
                      <SeverityBadge severity="INFO" />
                    </div>
                  )) : <div className="empty-state"><CheckCircle2 size={24} /><strong>No peer outliers</strong><span>Peer comparison is supplementary to baseline detection.</span></div>}
                </div>
              </section>
            </>
          )}

          {activePage === "Server Inventory" && (
            <>
              <section className="page-heading">
                <div>
                  <div className="eyebrow">INFRASTRUCTURE ASSETS</div>
                  <h1>Server Inventory</h1>
                  <p>Search and inspect the servers in your monitored fleet.</p>
                </div>
              </section>
              <section className="panel inventory-panel full-inventory">
                <div className="inventory-toolbar">
                  <div className="search-box">
                    <Search size={17} />
                    <input
                      value={searchTerm}
                      onChange={(event) => setSearchTerm(event.target.value)}
                      placeholder="Search server ID, service group, or runtime..."
                    />
                    {searchTerm && (
                      <button
                        className="clear-search"
                        onClick={() => setSearchTerm("")}
                        aria-label="Clear search"
                      >
                        <X size={15} />
                      </button>
                    )}
                  </div>
                  <select
                    value={statusFilter}
                    onChange={(event) => setStatusFilter(event.target.value)}
                  >
                    <option>All statuses</option>
                    <option>Stable</option>
                    <option>Drift detected</option>
                    <option>Policy violation</option>
                    <option>Unreachable</option>
                  </select>
                  <select
                    value={environmentFilter}
                    onChange={(event) => setEnvironmentFilter(event.target.value)}
                  >
                    <option>All environments</option>
                    {[...new Set(servers.map((server) => server.environment))].map((service) => (
                      <option key={service}>{service}</option>
                    ))}
                  </select>
                </div>
                <div className="results-count">
                  Showing {filteredServers.length} of {servers.length} servers
                </div>
                <ServerTable
                  servers={filteredServers}
                  onSelect={setSelectedServer}
                />
                {filteredServers.length === 0 && (
                  <div className="empty-state table-empty">
                    <Search size={25} />
                    <strong>No matching servers</strong>
                    <span>Try changing your search or filters.</span>
                  </div>
                )}
              </section>
            </>
          )}

          {activePage === "Drift Detection" && (
            <>
              <section className="page-heading">
                <div>
                  <div className="eyebrow">CONFIGURATION INTEGRITY</div>
                  <h1>Drift Detection</h1>
                  <p>
                    Review detected differences between server state and
                    expected baselines.
                  </p>
                </div>
              </section>
              <section className="metrics-grid drift-metrics">
                <MetricCard
                  icon={AlertTriangle}
                  label="Affected servers"
                  value={driftedServers}
                  change="Needs investigation"
                  tone="red"
                />
                <MetricCard
                  icon={Fingerprint}
                  label="Total findings"
                  value={totalDrift}
                  change="Across the fleet"
                  tone="purple"
                />
                <MetricCard
                  icon={ShieldCheck}
                  label="Unchanged servers"
                  value={stableServers}
                  change="No detected drift"
                  tone="green"
                />
              </section>
              <section className="panel">
                <div className="panel-heading">
                  <div>
                    <h2>Detected configuration drift</h2>
                    <p>Select a server to review its recorded differences.</p>
                  </div>
                </div>
                {servers
                  .filter((server) => server.drift > 0)
                  .map((server) => (
                    <button
                      className="drift-server-card"
                      key={server.id}
                      onClick={() => setSelectedServer(server)}
                    >
                      <div className="finding-icon">
                        <AlertTriangle size={18} />
                      </div>
                      <div className="drift-server-info">
                        <strong>{server.name}</strong>
                        <span>
                          {server.id} · {server.ip} · {server.environment}
                        </span>
                      </div>
                      <span className="drift-count">
                        {server.drift} findings
                      </span>
                      <StatusBadge status={server.status} />
                      <ChevronRight size={17} className="row-arrow" />
                    </button>
                  ))}
              </section>
            </>
          )}

          {activePage === "Peer Analysis" && (
            <>
              <section className="page-heading">
                <div>
                  <div className="eyebrow">SUPPLEMENTARY SIMILARITY SIGNALS</div>
                  <h1>Peer Analysis</h1>
                  <p>Peer comparisons add context; the approved baseline remains the source of truth.</p>
                </div>
              </section>
              <section className="metrics-grid">
                <MetricCard icon={Layers3} label="Peer outliers" value={peerOutliers.length} change="Requires review" tone="purple" />
                <MetricCard icon={Server} label="Comparable servers" value={servers.filter((server) => server.status !== "Unreachable").length} change="Validated state" tone="blue" />
                <MetricCard icon={AlertTriangle} label="Collection failures" value={unreachableServers} change="Not counted as drift" tone="red" />
              </section>
              <section className="panel">
                <div className="panel-heading"><div><h2>Peer outliers</h2><p>These signals do not replace baseline comparison.</p></div></div>
                {peerOutliers.length ? peerOutliers.map((server) => (
                  <button className="drift-server-card" key={server.id} onClick={() => setSelectedServer(server)}>
                    <div className="cluster-icon"><Layers3 size={18} /></div>
                    <div className="drift-server-info"><strong>{server.name}</strong><span>{server.environment} · {server.drift} baseline findings</span></div>
                    <SeverityBadge severity="INFO" />
                    <ChevronRight size={17} className="row-arrow" />
                  </button>
                )) : <div className="empty-state"><CheckCircle2 size={28} /><strong>No peer outliers reported</strong><span>There may be too few comparable servers for peer analysis.</span></div>}
              </section>
            </>
          )}

          {activePage === "Settings" && (
            <>
              <section className="page-heading">
                <div>
                  <div className="eyebrow">PREFERENCES</div>
                  <h1>Settings</h1>
                  <p>Customize your dashboard appearance.</p>
                </div>
              </section>
              <section className="panel settings-panel">
                <div className="settings-row">
                  <div className="settings-icon">
                    {theme === "dark" ? <Moon size={20} /> : <Sun size={20} />}
                  </div>
                  <div className="settings-description">
                    <strong>Dashboard appearance</strong>
                    <p>
                      Choose a dark or bright theme for the SentinelOps
                      dashboard.
                    </p>
                  </div>
                  <button className="theme-toggle" onClick={toggleTheme}>
                    {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
                    {theme === "dark" ? "Bright mode" : "Dark mode"}
                  </button>
                </div>
              </section>
            </>
          )}

          {activePage === "Documentation" && (
            <>
              <section className="page-heading">
                <div>
                  <div className="eyebrow">HELP CENTER</div>
                  <h1>Documentation</h1>
                  <p>Understand the main parts of your SentinelOps dashboard.</p>
                </div>
              </section>
              <section className="docs-grid">
                <div className="panel doc-card">
                  <Fingerprint size={23} />
                  <h2>Fingerprints</h2>
                  <p>The API computes SHA-256 fingerprints over normalized configuration and environment data.</p>
                </div>
                <div className="panel doc-card">
                  <ShieldAlert size={23} />
                  <h2>Configuration drift</h2>
                  <p>Drift is determined by comparing each validated server state with the separately approved baseline.</p>
                </div>
                <div className="panel doc-card">
                  <Layers3 size={23} />
                  <h2>Peer analysis</h2>
                  <p>Peer similarity is supplementary evidence and does not replace baseline comparison.</p>
                </div>
              </section>
            </>
          )}

          <footer className="page-footer">
            <span>
              <Command size={14} /> SentinelOps
            </span>
            <span>Infrastructure intelligence dashboard · on-demand scans</span>
            <span>Live data from the ConfigGuard API</span>
          </footer>
        </div>
      </main>

      {selectedServer && (
        <div
          className="drawer-backdrop"
          onClick={() => setSelectedServer(null)}
          role="presentation"
        >
          <aside
            className="details-drawer"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="drawer-header">
              <div>
                <span className="drawer-eyebrow">SERVER DETAILS</span>
                <h2>{selectedServer.name}</h2>
                <p>{selectedServer.id}</p>
              </div>
              <button
                className="icon-button"
                onClick={() => setSelectedServer(null)}
                aria-label="Close server details"
              >
                <X size={19} />
              </button>
            </div>

            <div className="drawer-status">
              <StatusBadge status={selectedServer.status} />
              <span>{selectedServer.environment}</span>
            </div>

            <div className="drawer-section">
              <h3>Server information</h3>
              <div className="detail-list">
                <DetailRow label="Service group" value={selectedServer.ip} />
                <DetailRow label="Runtime" value={selectedServer.os} />
                <DetailRow
                  label="Last scan"
                  value={selectedServer.lastScan}
                />
                <DetailRow
                  label="Fingerprint"
                  value={selectedServer.fingerprint}
                  mono
                />
                <DetailRow
                  label="Drift findings"
                  value={selectedServer.drift}
                />
              </div>
            </div>

            {selectedServer.collectionError && (
              <div className="drawer-section"><h3>Assessment status</h3><p>{selectedServer.collectionError}</p></div>
            )}

            <div className="drawer-section">
              <div className="section-title-row">
                <h3>Configuration differences</h3>
                <span className="section-count">
                  {selectedServer.details.length}
                </span>
              </div>

              {selectedServer.details.length > 0 ? (
                selectedServer.details.map((detail, index) => (
                  <div className="drift-detail-card" key={index}>
                    <div className="drift-detail-heading">
                      <strong>{detail.field}</strong>
                      <SeverityBadge severity={detail.severity} />
                    </div>
                    <div className="comparison-row expected">
                      <span>Expected</span>
                      <code>{detail.expected}</code>
                    </div>
                    <div className="comparison-row actual">
                      <span>Observed</span>
                      <code>{detail.actual}</code>
                    </div>
                  </div>
                ))
              ) : (
                <div className="no-drift-message">
                  <CheckCircle2 size={19} />
                  <div>
                    <strong>No recorded differences</strong>
                    <p>
                      No baseline differences were reported for this scan.
                    </p>
                  </div>
                </div>
              )}
            </div>

            <div className="drawer-actions">
              <button
                className="secondary-button"
                onClick={() => runDemoAction(selectedServer.id, "simulate")}
                disabled={Boolean(demoAction) || isRefreshing || selectedServer.status === "Unreachable"}
              >
                {demoAction === "simulate" ? "Applying..." : "Simulate security drift"}
              </button>
              <button
                className="secondary-button"
                onClick={() => runDemoAction(selectedServer.id, "reset")}
                disabled={Boolean(demoAction) || isRefreshing || selectedServer.status === "Unreachable"}
              >
                {demoAction === "reset" ? "Resetting..." : "Reset configuration"}
              </button>
              <button
                className="primary-button"
                onClick={runServerScan}
                disabled={isRefreshing || Boolean(demoAction)}
              >
                <RefreshCw size={16} />
                {isRefreshing ? "Scanning..." : "Run fleet scan"}
              </button>
              <button
                className="secondary-button"
                onClick={() => setSelectedServer(null)}
              >
                Close
              </button>
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}

function ServerTable({ servers, onSelect }) {
  return (
    <div className="table-scroll">
      <table className="server-table">
        <thead>
          <tr>
            <th>SERVER</th>
            <th>SERVICE GROUP</th>
            <th>RUNTIME</th>
            <th>STATUS</th>
            <th>DRIFT</th>
            <th>LAST SCAN</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {servers.map((server) => (
            <tr
              key={server.id}
              onClick={() => onSelect(server)}
              className="clickable-row"
            >
              <td>
                <div className="table-server">
                  <div className="table-server-icon">
                    <Server size={16} />
                  </div>
                  <div>
                    <strong>{server.name}</strong>
                    <span>{server.configVersion}</span>
                  </div>
                </div>
              </td>
              <td>
                <span className="environment-label">{server.environment}</span>
              </td>
              <td className="os-cell">{server.os}</td>
              <td>
                <StatusBadge status={server.status} />
              </td>
              <td>
                <span className={server.drift > 0 ? "drift-number" : "zero-drift"}>
                  {server.drift}
                </span>
              </td>
              <td className="scan-cell">{server.lastScan}</td>
              <td>
                <ChevronRight size={16} className="row-arrow" />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DetailRow({ label, value, mono = false }) {
  return (
    <div className="detail-row">
      <span>{label}</span>
      <strong className={mono ? "mono-value" : ""}>{value}</strong>
    </div>
  );
}



export default App;
