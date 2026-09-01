"use client";

import { Suspense } from "react";
import LoginForm from "./LoginForm";

export default function LoginPage() {
  return (
    <Suspense fallback={<div className="panel">加载中…</div>}>
      <LoginForm />
    </Suspense>
  );
}
