// The dashboard calls the release and staging readers inside Next.js, whose
// fetch reads a `next.revalidate` hint to cache Hugging Face responses; Bun
// ignores the field. Next.js declares it on RequestInit for the dashboard.
// This declaration mirrors Next's NextFetchRequestConfig and is seen only by
// the package's own typecheck, so the package typechecks without Next.js.
interface RequestInit {
  next?: { revalidate?: number | false; tags?: string[] } | undefined;
}
