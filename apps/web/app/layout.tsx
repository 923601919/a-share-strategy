import type { Metadata, Viewport } from "next";
import { Suspense } from "react";
import AppNav from "../components/AppNav";
import "./globals.css";

export const metadata: Metadata = {
  title: "分时雷达 · A股策略选股",
  description: "进攻型分时选股与跟踪（研究工具）",
  manifest: "/manifest.webmanifest",
  // iOS 的「添加到主屏幕」读这两个 meta：有了它就全屏独立打开、没有地址栏。
  // 这一条不需要 HTTPS，也是当前（纯 HTTP）唯一能拿到的「像 app」效果。
  appleWebApp: {
    capable: true,
    title: "分时雷达",
    statusBarStyle: "default",
  },
  icons: {
    icon: "/icon-192.png",
    apple: "/apple-touch-icon.png",
  },
  other: {
    "mobile-web-app-capable": "yes",
  },
};

// viewport-fit=cover：让底部 tab 栏能吃掉 iPhone 横条的安全区。
// 刻意不写 maximum-scale —— 那会禁掉用户缩放，是实打实的无障碍问题。
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#f6f7f9",
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
