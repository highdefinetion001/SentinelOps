import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  ShieldCheck,
  Eye,
  EyeOff,
  Mail,
  Lock,
  ArrowRight,
  Activity,
  Shield,
  Server,
  CheckCircle2,
} from "lucide-react";
import "../styles/auth.css";

export default function OwnerLogin() {
  const navigate = useNavigate();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleLogin = (e) => {
    e.preventDefault();
    setError("");

    const validEmail = /^[a-zA-Z0-9._%+-]+@gmail\.com$/i.test(
      email.trim()
    );

    if (!validEmail || password !== "123") {
      setError("Invalid credentials. Use a Gmail address and password 123.");
      return;
    }

    setLoading(true);

    sessionStorage.setItem("sentinelops-demo-session", "true");

    // Brief loading effect before entering the dashboard.
    setTimeout(() => {
      navigate("/home", { replace: true });
    }, 650);
  };

  return (
    <main className="sentinel-login">
      <div className="login-grid-bg" />
      <div className="login-glow login-glow-1" />
      <div className="login-glow login-glow-2" />

      <header className="login-topbar">
        <a className="login-brand" href="/login">
          <span className="login-brand-icon">
            <ShieldCheck size={25} />
          </span>
          <span>
            <strong>SENTINELOPS</strong>
            <small>SECURITY OPERATIONS</small>
          </span>
        </a>

        <div className="login-system-status">
          <span className="status-pulse" />
          ALL SYSTEMS OPERATIONAL
        </div>
      </header>

      <section className="login-main">
        <div className="login-intro">
          <div className="login-tag">
            <Activity size={14} />
            NEXT-GENERATION INFRASTRUCTURE SECURITY
          </div>

          <h1>
            Secure your
            <br />
            infrastructure.
            <br />
            <span>Own your defense.</span>
          </h1>

          <p className="login-description">
            One command center for your servers, configuration drift,
            infrastructure health, and security operations.
          </p>

          <div className="login-feature-list">
            <div className="login-feature">
              <span className="feature-icon">
                <Shield size={18} />
              </span>
              <span>
                <strong>Threat visibility</strong>
                <small>Stay ahead of infrastructure risks.</small>
              </span>
              <CheckCircle2 className="feature-check" size={17} />
            </div>

            <div className="login-feature">
              <span className="feature-icon">
                <Server size={18} />
              </span>
              <span>
                <strong>Fleet monitoring</strong>
                <small>Keep your server fleet in view.</small>
              </span>
              <CheckCircle2 className="feature-check" size={17} />
            </div>

            <div className="login-feature">
              <span className="feature-icon">
                <Activity size={18} />
              </span>
              <span>
                <strong>Real-time awareness</strong>
                <small>Track operational status at a glance.</small>
              </span>
              <CheckCircle2 className="feature-check" size={17} />
            </div>
          </div>

          <div className="login-decoration">
            <div className="decoration-line" />
            <span>INTELLIGENCE · VISIBILITY · CONTROL</span>
          </div>
        </div>

        <div className="login-card">
          <div className="login-card-top">
            <div className="login-card-shield">
              <ShieldCheck size={27} />
            </div>

            <span className="login-access-label">
              <span className="status-pulse" />
              SECURE ACCESS
            </span>
          </div>

          <div className="login-card-heading">
            <p className="login-overline">OWNER CONSOLE</p>
            <h2>Welcome back<span>.</span></h2>
            <p>Enter your credentials to access your workspace.</p>
          </div>

          <form className="login-form" onSubmit={handleLogin}>
            <div className="login-field">
              <label htmlFor="owner-email">EMAIL ADDRESS</label>

              <div className="login-input-box">
                <Mail size={18} />
                <input
                  id="owner-email"
                  type="email"
                  placeholder="you@gmail.com"
                  value={email}
                  onChange={(e) => {
                    setEmail(e.target.value);
                    setError("");
                  }}
                  autoComplete="email"
                  required
                />
                {email.trim() && /^[a-zA-Z0-9._%+-]+@gmail\.com$/i.test(email.trim()) && (
                  <CheckCircle2 className="input-valid-icon" size={17} />
                )}
              </div>
            </div>

            <div className="login-field">
              <label htmlFor="owner-password">PASSWORD</label>

              <div className="login-input-box">
                <Lock size={18} />
                <input
                  id="owner-password"
                  type={showPassword ? "text" : "password"}
                  placeholder="Enter your password"
                  value={password}
                  onChange={(e) => {
                    setPassword(e.target.value);
                    setError("");
                  }}
                  autoComplete="current-password"
                  required
                />

                <button
                  className="password-toggle"
                  type="button"
                  onClick={() => setShowPassword((current) => !current)}
                  aria-label={showPassword ? "Hide password" : "Show password"}
                >
                  {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              </div>
            </div>

            {error && (
              <div className="login-error" role="alert">
                <Shield size={17} />
                <span>{error}</span>
              </div>
            )}

            <button
              className="login-submit"
              type="submit"
              disabled={loading}
            >
              <span>{loading ? "Authenticating..." : "Access dashboard"}</span>
              {loading ? (
                <span className="login-spinner" />
              ) : (
                <ArrowRight size={19} />
              )}
            </button>
          </form>

          <div className="login-divider">
            <span />
            <small>PROTECTED WORKSPACE</small>
            <span />
          </div>

          <div className="login-card-footer">
            <ShieldCheck size={16} />
            <span>Your security operations, in one place.</span>
          </div>

          <p className="login-demo-hint">
            DEMO ACCESS · Any valid Gmail address · Password: 123
          </p>
        </div>
      </section>

      <footer className="login-bottom">
        <span>© 2026 SENTINELOPS</span>
        <span className="login-bottom-center">
          BUILT FOR INFRASTRUCTURE DEFENSE
        </span>
        <span className="login-version">CONSOLE V1.0.0</span>
      </footer>
    </main>
  );
}