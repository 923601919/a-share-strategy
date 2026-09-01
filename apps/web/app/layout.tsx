import type { Metadata } from "next";
import { Suspense } from "react";
import AppNav from "../components/AppNav";
import "./globals.css";

export const metadata: Metadata = {
  title: "分时雷达 · A股策略选股",
  description: "进攻型分时选股与跟踪（研究工具）",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <main>
          <Suspense fallback={<nav className="nav" />}>
            <AppNav />
          </Suspense>
          {children}
          <p className="footer-note">
            研究工具，非投资建议。规则参考公开短线方法论归纳，请人工确认分时形态。
          </p>
        </main>
      </body>
    </html>
  );
}
