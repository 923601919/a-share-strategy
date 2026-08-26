import type { Metadata } from "next";
import Link from "next/link";
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
          <nav className="nav">
            <div className="brand">
              分时<span>雷达</span>
            </div>
            <Link href="/">选股</Link>
            <Link href="/watch">跟踪</Link>
            <Link href="/sim">模拟盘</Link>
            <Link href="/review">复盘</Link>
          </nav>
          {children}
          <p className="footer-note">
            研究工具，非投资建议。规则参考公开短线方法论归纳，请人工确认分时形态。
          </p>
        </main>
      </body>
    </html>
  );
}
