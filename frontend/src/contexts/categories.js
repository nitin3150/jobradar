// Four scanner domains surfaced as Navbar tabs — one per backend
// endpoint. Job-board scans get their own dedicated ``/jobs`` page
// (see the NavLink in Navbar.jsx) rather than a tab here, so the
// operator manages the full review queue (status, score, board,
// dates) from a single page without going through the dashboard.
//
// Note on localStorage migration: any operator with the previous
// ``"boards"`` category still in localStorage from the 5-tab build
// will silently fall through to ``DEFAULT_CATEGORY`` here — the
// ``CATEGORIES.includes(stored)`` check in CategoryContext returns
// false for the now-removed key, so they land on the new default.
// No data loss; the dashboard just opens on the Funding News feed.
// Kept in their own module so fast-refresh can pick up changes
// without disabling the react-refresh export rule in
// CategoryContext.jsx.
export const CATEGORIES = ['funding', 'ngos', 'remote', 'oss'];
export const DEFAULT_CATEGORY = 'funding';
