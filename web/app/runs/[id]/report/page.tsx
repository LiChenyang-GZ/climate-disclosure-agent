"use client";

import Link from "next/link";
import { useParams } from "next/navigation";

// Placeholder until U4 (paused until the "check types" discussion in note 13 settles).
export default function ReportPage() {
  const { id } = useParams<{ id: string }>();
  return (
    <section className="space-y-4">
      <h1 className="text-2xl font-semibold">Report</h1>
      <p className="text-slate-600">The report page isn&apos;t built yet.</p>
      <Link href={`/runs/${id}`} className="underline">
        Back to check progress
      </Link>
    </section>
  );
}
