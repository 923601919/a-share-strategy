"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  ApiError,
  bootstrapAdmin,
  getAuthStatus,
  login,
  register,
  setAccessToken,
} from "../../lib/api";

type Mode = "login" | "register" | "bootstrap";

export default function LoginForm() {
  const router = useRouter();
  const search = useSearchParams();
  const nextPath = useMemo(() => {
    const n = search.get("next") || "/";
    return n.startsWith("/") ? n : "/";
  }, [search]);

  const [mode, setMode] = useState<Mode>("login");
  const [authRequired, setAuthRequired] = useState(true);
  const [bootstrapAvailable, setBootstrapAvailable] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [invite, setInvite] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getAuthStatus()
      .then((s) => {
        setAuthRequired(s.auth_required);
        setBootstrapAvailable(s.bootstrap_available);
        if (!s.auth_required) {
          router.replace(nextPath);
          return;
        }
        if (s.bootstrap_available) setMode("bootstrap");
      })
      .catch(() => {
        /* 后端未启动时仍显示登录表单 */
      });
  }, [nextPath, router]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const session =
        mode === "bootstrap"
          ? await bootstrapAdmin(username.trim(), password)
          : mode === "register"
            ? await register(username.trim(), password, invite.trim())
            : await login(username.trim(), password);
      setAccessToken(session.access_token);
      router.replace(nextPath);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel" style={{ maxWidth: 420, margin: "40px auto" }}>
      <h1 style={{ marginTop: 0, fontSize: 22 }}>
        {mode === "bootstrap" ? "创建管理员" : mode === "register" ? "邀请注册" : "登录"}
      </h1>
      <p className="muted" style={{ marginTop: -4 }}>
        {authRequired
          ? mode === "bootstrap"
            ? "首次部署：创建管理员账号后，再生成邀请码给朋友。"
            : "每人独立自选与模拟盘，需登录后使用。"
          : "当前未开启鉴权，正在进入…"}
      </p>

      <form onSubmit={onSubmit} style={{ gap: 12, display: "flex", flexDirection: "column" }}>
        <label className="muted">
          用户名
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            required
            minLength={2}
          />
        </label>
        <label className="muted">
          密码
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            required
            minLength={6}
          />
        </label>
        {mode === "register" && (
          <label className="muted">
            邀请码
            <input
              value={invite}
              onChange={(e) => setInvite(e.target.value)}
              required
              placeholder="管理员发给你的邀请码"
            />
          </label>
        )}
        {error && <p className="err">{error}</p>}
        <button type="submit" disabled={busy}>
          {busy ? "提交中…" : mode === "bootstrap" ? "创建并登录" : mode === "register" ? "注册并登录" : "登录"}
        </button>
      </form>

      <div className="row" style={{ marginTop: 16, gap: 12 }}>
        {mode !== "login" && !bootstrapAvailable && (
          <button type="button" className="ghost" onClick={() => setMode("login")}>
            已有账号
          </button>
        )}
        {mode !== "register" && !bootstrapAvailable && (
          <button type="button" className="ghost" onClick={() => setMode("register")}>
            用邀请码注册
          </button>
        )}
        {bootstrapAvailable && mode !== "bootstrap" && (
          <button type="button" className="ghost" onClick={() => setMode("bootstrap")}>
            首次创建管理员
          </button>
        )}
      </div>
    </div>
  );
}
