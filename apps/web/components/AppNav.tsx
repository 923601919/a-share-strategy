"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  AuthUser,
  createInvite,
  getAccessToken,
  getAuthStatus,
  getMe,
  logout,
} from "../lib/api";

const LINKS = [
  { href: "/", label: "选股" },
  { href: "/watch", label: "跟踪" },
  { href: "/sim", label: "模拟盘" },
  { href: "/review", label: "复盘" },
  { href: "/stats", label: "验证" },
];

export default function AppNav() {
  const pathname = usePathname();
  const router = useRouter();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [authRequired, setAuthRequired] = useState(false);
  const [inviteCode, setInviteCode] = useState("");
  const [inviteErr, setInviteErr] = useState("");
  const isLogin = pathname?.startsWith("/login");

  useEffect(() => {
    if (isLogin) return;
    let cancelled = false;
    (async () => {
      try {
        const st = await getAuthStatus();
        if (cancelled) return;
        setAuthRequired(st.auth_required);
        if (!st.auth_required) return;
        if (!getAccessToken()) {
          router.replace(`/login?next=${encodeURIComponent(pathname || "/")}`);
          return;
        }
        const me = await getMe();
        if (!cancelled) setUser(me);
      } catch {
        /* health/auth 失败时留给各页报错 */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isLogin, pathname, router]);

  if (isLogin) return null;

  async function onInvite() {
    setInviteErr("");
    setInviteCode("");
    try {
      const inv = await createInvite();
      setInviteCode(inv.code);
    } catch (e) {
      setInviteErr(e instanceof Error ? e.message : String(e));
    }
  }

  function onLogout() {
    logout();
    setUser(null);
    if (authRequired) router.replace("/login");
  }

  return (
    <nav className="nav">
      <div className="brand">
        <span className="brand-mark">分</span>
        <span>分时雷达</span>
      </div>
      {LINKS.map((l) => (
        <Link key={l.href} href={l.href} className={pathname === l.href ? "active" : undefined}>
          {l.label}
        </Link>
      ))}
      <div className="nav-right">
        {user && (
          <span className="muted" style={{ fontSize: 13 }}>
            {user.username}
            {user.role === "admin" ? " · 管理" : ""}
          </span>
        )}
        {user?.role === "admin" && (
          <button type="button" className="ghost" onClick={onInvite}>
            邀请码
          </button>
        )}
        {(user || authRequired) && (
          <button type="button" className="ghost" onClick={onLogout}>
            退出
          </button>
        )}
      </div>
      {(inviteCode || inviteErr) && (
        <p className="muted" style={{ width: "100%", margin: "8px 0 0", fontSize: 13 }}>
          {inviteCode ? `邀请码（发给朋友一次）：${inviteCode}` : inviteErr}
        </p>
      )}
    </nav>
  );
}
